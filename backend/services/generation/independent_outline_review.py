"""An opt-in, side-effect-free semantic review boundary (ADR-0008 / ADR-0011).

The injected GenerationRuntime owns paid-attempt accounting. This module owns
the versioned evidence transport and local validation, never a repository,
candidate unlock, state mutation, or completion certificate.
"""
from __future__ import annotations

import hashlib
import json
from base64 import urlsafe_b64encode
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Literal, Mapping, Sequence

from bson import ObjectId
from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model, model_validator
from pydantic_core import PydanticCustomError

from backend.llm.models import TokenUsage
from backend.llm.exceptions import LLMError, LLMStructuredRepairError
from backend.llm.schemas.scene_contract_pydantic import (
    BeatEvidenceSchema,
    ChapterOutlineAdherenceEvidenceV5Schema,
    OutlineContractFindingV3Schema,
    OutlineSemanticUnknownSchema,
)
from backend.services.generation.outline_adherence import (
    OUTLINE_ADHERENCE_SYSTEM_PROMPT,
    OutlineAdherenceValidationError,
    assess_outline_adherence_evidence,
    serialize_outline_contract,
)
from backend.services.llm.generation_runtime import (
    AttemptUsage,
    ConservativeGenerationBoundExceeded,
    GenerationPlan,
    GenerationRuntime,
    PromptPlan,
    StaleGenerationPlan,
    StructuredOutputMode,
    StructuredOutputByteBudgetExceeded,
    StructuredStreamProgress,
    STRUCTURED_REPAIR_PROMPT_REVISION,
    EMBEDDED_SCHEMA_REPAIR_PROMPT_REVISION,
    UnsettledGenerationAttempts,
)


ANCHOR_PROTOCOL = "exact_scene_prose_anchor_view.v6"
REVIEW_PROTOCOL = "independent_outline_review.v7"
ANCHOR_WIDTH = 120
ANCHOR_BREAKS = frozenset("。！？!?；;\n")
ANCHOR_CLOSERS = frozenset("”’\"」』）)\n\r")
MAX_QUOTE_LENGTH = 500
MAX_PROSE_CODEPOINTS = 100_000
REVIEW_TASK = (
    "阅读完整当前正文、章纲与获准上下文，仅报告符合度证据，不决定通过或严重度。"
    "按章纲顺序覆盖所有 beat，检查必要事件、进入与结束状态、禁止条件及重复规则。"
    "本次只审查完成证据，不生成质量画像或质量维度报告；quality_dimensions 保持空数组。"
    "每个锚点都绑定唯一 scene_id；beat 证据只能引用同场锚点。"
    "引用默认只返回 anchor_id，服务器把该片段原文还原为精确引文；不要抄写或概述原文。"
    "锚点按原文顺序排列，文本不重叠；依次拼接同场锚点就是该场完整原文。"
    "需要连续多个片段时加 through_anchor_id，表示到该片段末尾的完整连续原文；必须同场、正序且总长最多 500 字符。"
    "例如 spans=[{\"anchor_id\":\"输入中的实际片段ID\"}]；不要输出示例占位符，不计算字符位置。"
    "选择足以证明具体状态变化的最小片段或范围，不能因为附近提到人物或事件就标为 satisfied。"
    "仅在必须精确裁剪片段内部时提供可选 quote，须为连续逐字原文，不能加省略号、改字或拼接；此时省略 through_anchor_id。"
    "可选 quote 最多 500 字符，首字须位于 anchor_id 的片段内，同场跨片段引用仍须唯一匹配。"
    "保持输出简洁：summary 用一句话，每项 explanation 只写支撑判断的具体变化，通常不超过 30 字。"
    "不复述整段情节，不重复解释同一结论，不省略必要 beat、实际偏离或语义不确定。"
    "findings 是偏离问题清单，只收录确实违反章纲或无法确定是否违反的事项；正常兑现只在 beat_evidence 中记录。"
    "没有问题时 findings=[]，不要为每个检查类别填写一条正常观察。"
    "finding.status=violation 表示确实观察到偏离，不表示观察到了正常事件；无法确定时使用 unknown。"
    "例如顺序正确、冲突已兑现、悬念成立、未触犯禁止条件、事件仅发生一次且未重复，都不得写成 finding。"
    "字段组合规则：satisfied、mentioned、contradicted 的 beat 必须有 spans；missing 的 spans 必须为空。"
    "finding 的 status=violation 必须至少有一处证明偏离的原文 spans；无法提供证据时保留 unknown，不编造引文。"
    "forbidden_condition 必须给出 scene_id 和该场章纲已有的非空 condition_ids，event_key 留空。"
    "event_repetition 必须给出 scene_id 和章纲已有的 event_key，condition_ids 保持空数组。"
    "其余 finding 类别不得携带 condition_ids 或 event_key；可省略这两个无关字段。"
    "修正只限格式或证据定位；有效的语义问题不得改成通过。"
)
_EVIDENCE_SPAN_FIELDS = (
    ("beat_evidence", BeatEvidenceSchema, "spans"),
    ("findings", OutlineContractFindingV3Schema, "spans"),
    ("unknowns", OutlineSemanticUnknownSchema, "spans"),
)


def _jsonable(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        normalized = (
            value.replace(tzinfo=UTC)
            if value.tzinfo is None
            else value.astimezone(UTC)
        )
        return normalized.isoformat(timespec="milliseconds").replace(
            "+00:00",
            "Z",
        )
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _json(value: Any) -> str:
    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class AnchoredProseSpan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    anchor_id: str = Field(pattern=r"^[A-Za-z0-9_-]{22}:[0-9]{1,6}$")
    through_anchor_id: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9_-]{22}:[0-9]{1,6}$",
        description="仅跨连续片段时填写末片段 ID；通常省略。",
    )
    quote: str | None = Field(
        default=None, min_length=1, max_length=MAX_QUOTE_LENGTH,
        description="通常省略。仅精确裁剪片段内部时逐字复制原文，禁止省略或拼接。",
    )

    @model_validator(mode="after")
    def validate_reference_mode(self) -> "AnchoredProseSpan":
        if self.quote is not None and self.through_anchor_id is not None:
            raise ValueError("quote and through_anchor_id are mutually exclusive")
        return self


class AnchoredContractFinding(OutlineContractFindingV3Schema):
    """Transport names an actual deviation; local policy keeps its V5 vocabulary."""

    status: Literal["violation", "unknown"] = Field(
        description="violation=确实违反章纲；unknown=无法判定是否违反。正常兑现不进入 findings。",
    )
    spans: list[AnchoredProseSpan] = deepcopy(OutlineContractFindingV3Schema.model_fields["spans"])

    @model_validator(mode="after")
    def validate_violation_evidence(self) -> "AnchoredContractFinding":
        if self.status == "violation" and not self.spans:
            raise ValueError("a violation finding requires at least one evidence span")
        return self


def _anchor_transport_schema() -> type[BaseModel]:
    # Inherit V5's *entire* field contract and validators, changing only the
    # transport of spans. Copy FieldInfo so its min/max/defaults cannot drift.
    replacements = {}
    for field, base, span_field in _EVIDENCE_SPAN_FIELDS:
        anchored_item = AnchoredContractFinding if field == "findings" else create_model(
            f"Anchored{base.__name__}",
            __base__=base,
            **{span_field: (list[AnchoredProseSpan], deepcopy(base.model_fields[span_field]))},
        )
        field_info = deepcopy(ChapterOutlineAdherenceEvidenceV5Schema.model_fields[field])
        if field == "findings":
            field_info.description = "只列实际偏离或语义不确定；正常完成不得写入，无问题时为空数组。"
        replacements[field] = (list[anchored_item], field_info)
    return create_model(
        "AnchoredOutlineAdherenceEvidenceV5",
        __base__=ChapterOutlineAdherenceEvidenceV5Schema,
        schema_version=(Literal["anchored_outline_adherence_evidence.v5"], Field()),
        view_digest=(str, Field(pattern=r"^[0-9a-f]{64}$")),
        **replacements,
    )


AnchoredOutlineAdherenceEvidenceV5 = _anchor_transport_schema()


@dataclass(frozen=True)
class OutlineReviewSceneRange:
    """A deterministic prose span owned by one outline scene."""

    scene_id: str
    start: int
    end: int
    content_digest: str


@dataclass(frozen=True)
class OutlineReviewSnapshot:
    source_run_id: str
    source_run_revision: int
    source_content_digest: str
    prose: str
    outline_json: str
    authorized_context: str
    scene_ranges: tuple[OutlineReviewSceneRange, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.source_run_id, str)
            or not self.source_run_id
            or len(self.source_run_id) > 128
            or type(self.source_run_revision) is not int
            or self.source_run_revision < 1
            or not isinstance(self.prose, str)
            or not self.prose.strip()
            or len(self.prose) > MAX_PROSE_CODEPOINTS
            or _digest(self.prose) != self.source_content_digest
            or not isinstance(self.authorized_context, str)
        ):
            raise ValueError("independent review source is invalid")
        outline = json.loads(self.outline_json)
        if not isinstance(outline, dict) or outline.get("scene_contract_version") != "scene_transition_contract.v2":
            raise ValueError("independent review requires a V2 outline")
        scenes = outline.get("scenes")
        if not isinstance(scenes, list) or len(scenes) != len(self.scene_ranges):
            raise ValueError("independent review scene ranges are invalid")
        previous_end = 0
        for index, (scene, scene_range) in enumerate(
            zip(scenes, self.scene_ranges, strict=True)
        ):
            expected_start = 0 if index == 0 else previous_end + 2
            if (
                not isinstance(scene, dict)
                or type(scene_range) is not OutlineReviewSceneRange
                or scene_range.scene_id != scene.get("scene_id")
                or type(scene_range.start) is not int
                or type(scene_range.end) is not int
                or scene_range.start != expected_start
                or scene_range.end <= scene_range.start
                or scene_range.end > len(self.prose)
                or (index and self.prose[previous_end:scene_range.start] != "\n\n")
                or scene_range.content_digest
                != _digest(self.prose[scene_range.start:scene_range.end])
            ):
                raise ValueError("independent review scene ranges are invalid")
            previous_end = scene_range.end
        if previous_end != len(self.prose):
            raise ValueError("independent review scene ranges are invalid")
        object.__setattr__(self, "outline_json", _json(outline))

    @classmethod
    def create(
        cls,
        *,
        source_run_id: str,
        source_run_revision: int,
        source_content_digest: str,
        prose: str,
        outline: Mapping[str, Any],
        authorized_context: str,
        scene_ranges: Sequence[Mapping[str, Any] | OutlineReviewSceneRange] | None = None,
    ) -> "OutlineReviewSnapshot":
        outline_value = dict(outline)
        outline_scenes = outline_value.get("scenes")
        if scene_ranges is None:
            if not isinstance(outline_scenes, list) or len(outline_scenes) != 1:
                raise ValueError("independent review scene ranges are required")
            scene_ranges = ({
                "scene_id": outline_scenes[0].get("scene_id"),
                "start": 0,
                "end": len(prose),
            },)
        normalized_ranges = []
        for value in scene_ranges:
            if isinstance(value, OutlineReviewSceneRange):
                scene_id, start, end = value.scene_id, value.start, value.end
            elif isinstance(value, Mapping) and set(value) == {"scene_id", "start", "end"}:
                scene_id, start, end = value["scene_id"], value["start"], value["end"]
            else:
                raise ValueError("independent review scene ranges are invalid")
            if (
                not isinstance(scene_id, str)
                or not scene_id
                or type(start) is not int
                or type(end) is not int
                or start < 0
                or end < start
                or end > len(prose)
            ):
                raise ValueError("independent review scene ranges are invalid")
            normalized_ranges.append(OutlineReviewSceneRange(
                scene_id=scene_id,
                start=start,
                end=end,
                content_digest=_digest(prose[start:end]),
            ))
        return cls(
            source_run_id=source_run_id,
            source_run_revision=source_run_revision,
            source_content_digest=source_content_digest,
            prose=prose,
            outline_json=serialize_outline_contract(outline_value),
            authorized_context=authorized_context,
            scene_ranges=tuple(normalized_ranges),
        )

    @property
    def view_digest(self) -> str:
        return _digest(_json({
            "protocol": ANCHOR_PROTOCOL,
            "source_run_id": self.source_run_id,
            "source_run_revision": self.source_run_revision,
            "source_content_digest": self.source_content_digest,
            "outline_digest": _digest(self.outline_json),
            "context_digest": _digest(self.authorized_context),
            "scene_ranges": [asdict(item) for item in self.scene_ranges],
        }))


@dataclass(frozen=True)
class IndependentReviewPlan:
    generation: GenerationPlan
    writer_model: str
    input_token_bound: int
    max_response_bytes: int
    protocol: Literal["independent_outline_review.v7"] = REVIEW_PROTOCOL

    def __post_init__(self) -> None:
        if (
            self.protocol != REVIEW_PROTOCOL
            or not isinstance(self.generation, GenerationPlan)
            or not isinstance(self.writer_model, str)
            or not self.writer_model.strip()
            or not isinstance(self.generation.provider_model, str)
            or not self.generation.provider_model.strip()
            or self.writer_model.strip().casefold() == self.generation.provider_model.strip().casefold()
            or self.generation.reviewer_alias is not None
            or self.generation.mode not in {
                StructuredOutputMode.PROMPT_JSON, StructuredOutputMode.JSON_OBJECT,
            }
            or type(self.generation.max_semantic_attempts) is not int
            or self.generation.max_semantic_attempts != 2
            or type(self.generation.max_output_tokens) is not int
            or self.generation.max_output_tokens < 1
            or type(self.generation.timeout_seconds) is not int
            or self.generation.timeout_seconds < 1
            or type(self.input_token_bound) is not int
            or self.input_token_bound < 1
            or type(self.generation.max_context_tokens) is not int
            or self.generation.max_context_tokens < 1
            or self.input_token_bound + self.generation.max_output_tokens > self.generation.max_context_tokens
            or type(self.max_response_bytes) is not int
            or self.max_response_bytes < 1
        ):
            raise ValueError("independent review plan is not frozen or supported")

    @property
    def max_total_tokens(self) -> int:
        return 2 * (self.input_token_bound + int(self.generation.max_output_tokens))

    @property
    def contract_digest(self) -> str:
        """Local protocol identity, not a readiness or permission to execute."""
        return _digest(_json({
            "plan": asdict(self),
            "semantic_protocol_digest": independent_review_semantic_protocol_digest(),
            "structured_correction_revision": STRUCTURED_REPAIR_PROMPT_REVISION,
            "require_settled_attempts": True,
        }))


def independent_review_semantic_protocol_digest() -> str:
    """Provider-independent identity for fair comparisons of this review task.

    ``IndependentReviewPlan.contract_digest`` intentionally includes the exact
    Provider plan.  A blind comparison needs a second identity that proves two
    different Providers received the same task, anchor transport, local schema
    and correction rules without pretending their generation plans are equal.
    """

    return _digest(_json({
        "protocol": REVIEW_PROTOCOL,
        "anchor_protocol": ANCHOR_PROTOCOL,
        "anchor_width": ANCHOR_WIDTH,
        "anchor_breaks": sorted(ANCHOR_BREAKS),
        "anchor_closers": sorted(ANCHOR_CLOSERS),
        "max_quote_length": MAX_QUOTE_LENGTH,
        "max_prose_codepoints": MAX_PROSE_CODEPOINTS,
        "task": REVIEW_TASK,
        "system_prompt": OUTLINE_ADHERENCE_SYSTEM_PROMPT,
        "schema": AnchoredOutlineAdherenceEvidenceV5.model_json_schema(),
        "structured_correction_revision": STRUCTURED_REPAIR_PROMPT_REVISION,
        "embedded_schema_correction_revision": EMBEDDED_SCHEMA_REPAIR_PROMPT_REVISION,
        "require_settled_attempts": True,
    }))


IndependentReviewFailureCode = Literal[
    "review_uncertain", "review_plan_stale", "review_not_natural_end",
    "review_evidence_invalid", "review_response_limit", "review_budget_exhausted",
    "review_accounting_invalid", "review_generation_failed", "review_dispatch_rejected",
]


@dataclass(frozen=True)
class IndependentReviewResult:
    evidence: dict[str, Any] | None
    failure_code: IndependentReviewFailureCode | None
    usage: TokenUsage
    attempts: tuple[AttemptUsage, ...]


@dataclass(frozen=True)
class _ProseAnchor:
    scene_id: str
    start: int
    primary_end: int
    text: str


def _anchors(snapshot: OutlineReviewSnapshot) -> dict[str, _ProseAnchor]:
    result = {}
    ordinal = 0
    # A short source tag avoids echoing the full SHA-256 for every quote.
    # The complete view digest remains mandatory and is checked separately;
    # this 128-bit tag is a locator, never the source's authorization identity.
    view_tag = urlsafe_b64encode(bytes.fromhex(snapshot.view_digest)[:16]).decode("ascii").rstrip("=")
    for scene_range in snapshot.scene_ranges:
        start = scene_range.start
        while start < scene_range.end:
            limit = min(start + ANCHOR_WIDTH, scene_range.end)
            primary_end = limit
            for index in range(start, limit):
                if snapshot.prose[index] in ANCHOR_BREAKS and snapshot.prose[start:index + 1].strip():
                    primary_end = index + 1
                    while primary_end < limit and snapshot.prose[primary_end] in ANCHOR_CLOSERS:
                        primary_end += 1
                    break
            # Quotes may overlap anchor chunks, but never cross a semantic
            # scene boundary. That boundary is part of the signed view.
            text = snapshot.prose[
                start:min(primary_end + MAX_QUOTE_LENGTH - 1, scene_range.end)
            ]
            result[f"{view_tag}:{ordinal}"] = _ProseAnchor(
                scene_id=scene_range.scene_id,
                start=start,
                primary_end=primary_end,
                text=text,
            )
            ordinal += 1
            start = primary_end
    return result


def _assess_anchored(
    value: BaseModel,
    snapshot: OutlineReviewSnapshot,
    anchors: Mapping[str, _ProseAnchor],
) -> dict[str, Any]:
    payload = value.model_dump(mode="json")
    if payload.pop("view_digest") != snapshot.view_digest:
        raise PydanticCustomError("review_source_mismatch", "review source does not match")
    payload["schema_version"] = "chapter_outline_adherence_evidence.v5"
    for field, _base, span_field in _EVIDENCE_SPAN_FIELDS:
        for item_index, item in enumerate(payload[field]):
            if field == "findings" and item["status"] == "violation":
                # A typed transport conversion, never inference from prose or
                # explanation text. Unknown remains unknown under local policy.
                item["status"] = "observed"
            located = []
            for span_index, span in enumerate(item[span_field]):
                def invalid_quote(code: str) -> ValidationError:
                    return ValidationError.from_exception_data(
                        "AnchoredOutlineAdherenceEvidenceV5",
                        [{
                            "type": PydanticCustomError(code, "exact quote location failed"),
                            "loc": (field, item_index, span_field, span_index),
                        }],
                    )

                anchor = anchors.get(span["anchor_id"])
                if anchor is None:
                    raise invalid_quote("review_anchor_unknown")
                if (
                    field == "beat_evidence"
                    and anchor.scene_id != item.get("scene_id")
                ):
                    raise invalid_quote("review_quote_scene_mismatch")
                if span["quote"] is None:
                    ending = anchors.get(span["through_anchor_id"] or span["anchor_id"])
                    if ending is None:
                        raise invalid_quote("review_anchor_unknown")
                    if ending.scene_id != anchor.scene_id:
                        raise invalid_quote("review_quote_scene_mismatch")
                    if ending.start < anchor.start:
                        raise invalid_quote("review_anchor_range_reversed")
                    if ending.primary_end - anchor.start > MAX_QUOTE_LENGTH:
                        raise invalid_quote("review_anchor_range_too_long")
                    # Materialize only the selected, immutable original range.
                    # No search, normalization, omitted text or model-written quote.
                    located.append({
                        "start": anchor.start, "end": ending.primary_end,
                        "quote": snapshot.prose[anchor.start:ending.primary_end],
                    })
                    continue
                start, primary_end, text = (
                    anchor.start, anchor.primary_end, anchor.text
                )
                quote = span["quote"]
                offset = text.find(quote)
                if offset < 0 or start + offset >= primary_end:
                    raise invalid_quote("review_quote_missing")
                second = text.find(quote, offset + 1)
                if second >= 0 and start + second < primary_end:
                    raise invalid_quote("review_quote_ambiguous")
                located.append({"start": start + offset, "end": start + offset + len(quote), "quote": quote})
            item[span_field] = located
    try:
        return assess_outline_adherence_evidence(
            payload,
            outline=json.loads(snapshot.outline_json),
            prose=snapshot.prose,
            source_prose_run_id=snapshot.source_run_id,
            source_prose_run_revision=snapshot.source_run_revision,
            source_content_digest=snapshot.source_content_digest,
        )
    except OutlineAdherenceValidationError as exc:
        # Only the existing closed reason code enters correction guidance;
        # neither the exception text nor raw manuscript values become a code.
        raise PydanticCustomError(exc.code, "local evidence validation failed") from None


class IndependentOutlineReviewer:
    def __init__(self, runtime: GenerationRuntime) -> None:
        self._runtime = runtime

    def matches_plan(self, plan: IndependentReviewPlan) -> bool:
        """Local configuration check for both dispatch and no-call replay."""
        try:
            return self._runtime.plan_structured(plan.generation.target) == plan.generation
        except (TypeError, ValueError):
            return False

    async def review(
        self,
        snapshot: OutlineReviewSnapshot,
        plan: IndependentReviewPlan,
        *,
        stream_progress: Callable[[StructuredStreamProgress], Any] | None = None,
    ) -> IndependentReviewResult:
        if not self.matches_plan(plan):
            return IndependentReviewResult(None, "review_plan_stale", TokenUsage(), ())
        anchors = _anchors(snapshot)

        def validate_current_evidence(value):
            _assess_anchored(value, snapshot, anchors)
            return value

        schema = create_model(
            "SnapshotBoundIndependentReview",
            __base__=AnchoredOutlineAdherenceEvidenceV5,
            __validators__={"validate_current_evidence": model_validator(mode="after")(validate_current_evidence)},
        )
        prompt = _json({
            "task": REVIEW_TASK,
            "schema": schema.model_json_schema(),
            "input": {
                "view_digest": snapshot.view_digest,
                "source_run_id": snapshot.source_run_id,
                "source_run_revision": snapshot.source_run_revision,
                "source_content_digest": snapshot.source_content_digest,
                "outline": json.loads(snapshot.outline_json),
                "authorized_context": snapshot.authorized_context,
                "anchors": [
                    {
                        "anchor_id": key,
                        "scene_id": item.scene_id,
                        # Keep overlap only in the local locator. The model
                        # receives every source character exactly once.
                        "text": item.text[:item.primary_end - item.start],
                    }
                    for key, item in anchors.items()
                ],
            },
        })
        offset = len(self._runtime.attempts)
        if self._runtime.uncertain_attempt_count:
            return IndependentReviewResult(None, "review_uncertain", TokenUsage(), ())
        try:
            generated = await self._runtime.generate_structured(
                plan.generation, schema, PromptPlan(prompt, prompt),
                system_prompt=OUTLINE_ADHERENCE_SYSTEM_PROMPT,
                max_tokens=plan.generation.max_output_tokens,
                max_conservative_input_tokens=plan.input_token_bound,
                max_conservative_total_tokens=plan.max_total_tokens,
                max_structured_raw_output_bytes=plan.max_response_bytes,
                require_settled_attempts=True,
                stream_json_output=True,
                stream_progress=stream_progress,
            )
            if not self.matches_plan(plan):
                return IndependentReviewResult(
                    None, "review_plan_stale", generated.usage, generated.attempts,
                )
            if generated.finish_reason != "stop":
                return IndependentReviewResult(
                    None, "review_not_natural_end", generated.usage, generated.attempts,
                )
            evidence = _assess_anchored(
                schema.model_validate(generated.value.model_dump(mode="json")),
                snapshot, anchors,
            )
            return IndependentReviewResult(evidence, None, generated.usage, generated.attempts)
        except Exception as exc:
            if self._runtime.uncertain_attempt_count:
                failure = "review_uncertain"
            elif isinstance(exc, UnsettledGenerationAttempts):
                failure = "review_accounting_invalid"
            elif isinstance(exc, StaleGenerationPlan):
                failure = "review_plan_stale"
            elif isinstance(exc, StructuredOutputByteBudgetExceeded):
                failure = "review_response_limit"
            elif isinstance(exc, ConservativeGenerationBoundExceeded):
                failure = "review_budget_exhausted"
            elif isinstance(exc, (ValidationError, LLMStructuredRepairError)):
                failure = "review_evidence_invalid"
            elif isinstance(exc, LLMError):
                failure = "review_generation_failed"
            elif getattr(exc, "provider_request_not_dispatched", False) is True:
                failure = "review_dispatch_rejected"
            else:
                # Programming errors are not evidence that the model failed.
                # The caller still owns the authoritative attempt ledger.
                raise
            attempts = self._runtime.attempts[offset:]
            usage = TokenUsage(
                input_tokens=sum(item.usage.input_tokens for item in attempts),
                output_tokens=sum(item.usage.output_tokens for item in attempts),
                total_tokens=sum(item.usage.total_tokens for item in attempts),
            )
            return IndependentReviewResult(None, failure, usage, attempts)
