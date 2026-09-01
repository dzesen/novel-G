"""An opt-in, side-effect-free semantic review boundary (ADR-0008).

The injected GenerationRuntime owns paid-attempt accounting. This module owns
the versioned evidence transport and local validation, never a repository,
candidate unlock, state mutation, or completion certificate.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model, model_validator
from pydantic_core import PydanticCustomError

from backend.llm.models import TokenUsage
from backend.llm.exceptions import LLMError, LLMStructuredRepairError
from backend.llm.schemas.scene_contract_pydantic import (
    BeatEvidenceSchema,
    ChapterOutlineAdherenceEvidenceV4Schema,
    OutlineContractFindingV3Schema,
    OutlineQualityDimensionSchema,
    OutlineSemanticUnknownSchema,
    SceneQualityProfileSchema,
)
from backend.services.generation.outline_adherence import (
    OUTLINE_ADHERENCE_SYSTEM_PROMPT,
    OutlineAdherenceValidationError,
    assess_outline_adherence_evidence,
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
    STRUCTURED_REPAIR_PROMPT_REVISION,
    UnsettledGenerationAttempts,
)


ANCHOR_PROTOCOL = "exact_prose_anchor_view.v1"
REVIEW_PROTOCOL = "independent_outline_review.v1"
ANCHOR_WIDTH = 512
MAX_QUOTE_LENGTH = 500
MAX_PROSE_CODEPOINTS = 100_000
REVIEW_TASK = (
    "阅读完整当前正文、章纲与获准上下文，仅报告符合度证据，不决定通过或严重度。"
    "按章纲顺序覆盖所有 beat，并为每场提供完整质量画像。"
    "引用须逐字复制，返回锚点 ID 和 quote，不计算字符位置。"
    "锚点按原文顺序排列，每段前 512 个字符是本段引用起点范围；"
    "右侧至多 499 个字符仅供跨边界引用，不代表正文重复。"
    "同一锚点内起点范围中必须唯一匹配；有歧义时使用更完整的原文引文。"
    "修正只限格式或证据定位；有效的语义问题不得改成通过。"
)
_EVIDENCE_SPAN_FIELDS = (
    ("beat_evidence", BeatEvidenceSchema, "spans"),
    ("findings", OutlineContractFindingV3Schema, "spans"),
    ("quality_dimensions", OutlineQualityDimensionSchema, "spans"),
    ("unknowns", OutlineSemanticUnknownSchema, "spans"),
    ("scene_quality_profiles", SceneQualityProfileSchema, "representative_spans"),
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class AnchoredProseSpan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    anchor_id: str = Field(pattern=r"^[0-9a-f]{64}:[0-9]{1,3}$")
    quote: str = Field(min_length=1, max_length=MAX_QUOTE_LENGTH)


def _anchor_transport_schema() -> type[BaseModel]:
    # Inherit V4's *entire* field contract and validators, changing only the
    # transport of spans. Copy FieldInfo so its min/max/defaults cannot drift.
    replacements = {}
    for field, base, span_field in _EVIDENCE_SPAN_FIELDS:
        anchored_item = create_model(
            f"Anchored{base.__name__}",
            __base__=base,
            **{span_field: (list[AnchoredProseSpan], deepcopy(base.model_fields[span_field]))},
        )
        replacements[field] = (
            list[anchored_item],
            deepcopy(ChapterOutlineAdherenceEvidenceV4Schema.model_fields[field]),
        )
    return create_model(
        "AnchoredOutlineAdherenceEvidenceV1",
        __base__=ChapterOutlineAdherenceEvidenceV4Schema,
        schema_version=(Literal["anchored_outline_adherence_evidence.v1"], Field()),
        view_digest=(str, Field(pattern=r"^[0-9a-f]{64}$")),
        **replacements,
    )


AnchoredOutlineAdherenceEvidenceV1 = _anchor_transport_schema()


@dataclass(frozen=True)
class OutlineReviewSnapshot:
    source_run_id: str
    source_run_revision: int
    source_content_digest: str
    prose: str
    outline_json: str
    authorized_context: str

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
    ) -> "OutlineReviewSnapshot":
        return cls(
            source_run_id, source_run_revision, source_content_digest,
            prose, _json(dict(outline)), authorized_context,
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
        }))


@dataclass(frozen=True)
class IndependentReviewPlan:
    generation: GenerationPlan
    writer_model: str
    input_token_bound: int
    max_response_bytes: int
    protocol: Literal["independent_outline_review.v1"] = REVIEW_PROTOCOL

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
        "max_quote_length": MAX_QUOTE_LENGTH,
        "max_prose_codepoints": MAX_PROSE_CODEPOINTS,
        "task": REVIEW_TASK,
        "system_prompt": OUTLINE_ADHERENCE_SYSTEM_PROMPT,
        "schema": AnchoredOutlineAdherenceEvidenceV1.model_json_schema(),
        "structured_correction_revision": STRUCTURED_REPAIR_PROMPT_REVISION,
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


def _anchors(snapshot: OutlineReviewSnapshot) -> dict[str, tuple[int, int, str]]:
    result = {}
    for ordinal, start in enumerate(range(0, len(snapshot.prose), ANCHOR_WIDTH)):
        primary_end = min(start + ANCHOR_WIDTH, len(snapshot.prose))
        # An exact quote can start at every code point in the primary range,
        # including the last one, and extend a full 500 characters to the right.
        text = snapshot.prose[start:primary_end + MAX_QUOTE_LENGTH - 1]
        result[f"{snapshot.view_digest}:{ordinal}"] = (start, primary_end, text)
    return result


def _assess_anchored(
    value: BaseModel,
    snapshot: OutlineReviewSnapshot,
    anchors: Mapping[str, tuple[int, int, str]],
) -> dict[str, Any]:
    payload = value.model_dump(mode="json")
    if payload.pop("view_digest") != snapshot.view_digest:
        raise PydanticCustomError("review_source_mismatch", "review source does not match")
    payload["schema_version"] = "chapter_outline_adherence_evidence.v4"
    for field, _base, span_field in _EVIDENCE_SPAN_FIELDS:
        for item_index, item in enumerate(payload[field]):
            located = []
            for span_index, span in enumerate(item[span_field]):
                def invalid_quote(code: str) -> ValidationError:
                    return ValidationError.from_exception_data(
                        "AnchoredOutlineAdherenceEvidenceV1",
                        [{
                            "type": PydanticCustomError(code, "exact quote location failed"),
                            "loc": (field, item_index, span_field, span_index),
                        }],
                    )

                anchor = anchors.get(span["anchor_id"])
                if anchor is None:
                    raise invalid_quote("review_anchor_unknown")
                start, primary_end, text = anchor
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
        self, snapshot: OutlineReviewSnapshot, plan: IndependentReviewPlan,
    ) -> IndependentReviewResult:
        if not self.matches_plan(plan):
            return IndependentReviewResult(None, "review_plan_stale", TokenUsage(), ())
        anchors = _anchors(snapshot)

        def validate_current_evidence(value):
            _assess_anchored(value, snapshot, anchors)
            return value

        schema = create_model(
            "SnapshotBoundIndependentReview",
            __base__=AnchoredOutlineAdherenceEvidenceV1,
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
                "anchors": [{"anchor_id": key, "text": item[2]} for key, item in anchors.items()],
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
