"""Deferred chapter tail: candidates first, one deterministic formal commit last."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from itertools import islice
from typing import Any, Awaitable, Callable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.services.generation.chapter_generation_application import (
    ChapterGenerationResult,
    ChapterGenerationStage,
    ProseCandidateSource,
)
from backend.services.generation.chapter_finalization import (
    MAX_FINALIZATION_REPAIR_CYCLES,
)
from backend.services.generation.headless_generation import (
    GeneratedProseCandidate,
)
from backend.services.generation.prose_runs import chapter_content_digest


_MAX_REPAIR_SCENE_INDEXES = 20
_MAX_REPAIR_CARD_IDS = 20
_MAX_REPAIR_CARD_ID_LENGTH = 64
_MAX_DROPPED_REFERENCE_COUNT = 1_000
_MAX_PIPELINE_ATTEMPTS = 512
_MAX_PIPELINE_TRUNCATIONS = 32
_MAX_TOKEN_COUNT = 1_000_000_000

PROSE_REPAIR_REQUEST_SCHEMA = "prose_candidate_repair_request.v1"
PROSE_REPAIR_RECEIPT_SCHEMA = "prose_candidate_repair_receipt.v1"
STATE_REPAIR_REQUEST_SCHEMA = "state_candidate_repair_request.v1"
STATE_REPAIR_RECEIPT_SCHEMA = "state_candidate_repair_receipt.v1"

ProseRepairReason = Literal[
    "completion_contract_failed",
    "outline_adherence_failed",
]
StateRepairReason = Literal[
    "consistency_conflict",
    "invalid_internal_reference",
]


class OutlineIssueCategory(StrEnum):
    SCENE_COVERAGE = "scene_coverage"
    SCENE_ORDER = "scene_order"
    CORE_CONFLICT = "core_conflict"
    ENDING_HOOK = "ending_hook"
    UNPLANNED_MAJOR_EVENT = "unplanned_major_event"
    VOLUME_ARC = "volume_arc"


_OUTLINE_ISSUE_CATEGORIES = frozenset(OutlineIssueCategory)


class _RepairContract(BaseModel):
    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="forbid",
        frozen=True,
        strict=True,
    )


class CandidateUsageSummary(_RepairContract):
    input_tokens: int = Field(default=0, ge=0, le=_MAX_TOKEN_COUNT)
    output_tokens: int = Field(default=0, ge=0, le=_MAX_TOKEN_COUNT)
    total_tokens: int = Field(default=0, ge=0, le=_MAX_TOKEN_COUNT)


class CandidateAttemptSummary(_RepairContract):
    schema_version: Literal["chapter_candidate_attempt.v1"] = (
        "chapter_candidate_attempt.v1"
    )
    attempt_id: str = Field(min_length=1, max_length=128)
    provider_alias: str = Field(default="unreported", min_length=1, max_length=64)
    phase: Literal[
        "primary",
        "schema_fallback",
        "repair",
        "reviewer",
        "text",
        "unknown",
    ] = "unknown"
    state: Literal[
        "accounted",
        "settled",
        "released_pre_dispatch",
        "uncertain",
        "resolved_retry",
        "resolved_skip",
        "unknown",
    ] = "unknown"
    usage: CandidateUsageSummary = Field(default_factory=CandidateUsageSummary)


class CandidateTruncationSummary(_RepairContract):
    schema_version: Literal["chapter_candidate_truncation.v1"] = (
        "chapter_candidate_truncation.v1"
    )
    step: str = Field(min_length=1, max_length=64)
    truncated_section_count: int = Field(ge=0, le=100)
    dropped_item_count: int = Field(ge=0, le=10_000)


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        projected = dump()
        if isinstance(projected, Mapping):
            return projected
    return {}


def _bounded_non_negative_int(value: Any, *, maximum: int) -> int:
    if type(value) is not int or value < 0:
        return 0
    return min(value, maximum)


def _usage_summary(value: Any) -> CandidateUsageSummary:
    raw = _as_mapping(value)
    input_tokens = _bounded_non_negative_int(
        raw.get("input_tokens"),
        maximum=_MAX_TOKEN_COUNT,
    )
    output_tokens = _bounded_non_negative_int(
        raw.get("output_tokens"),
        maximum=_MAX_TOKEN_COUNT,
    )
    supplied_total = _bounded_non_negative_int(
        raw.get("total_tokens"),
        maximum=_MAX_TOKEN_COUNT,
    )
    return CandidateUsageSummary(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=min(
            _MAX_TOKEN_COUNT,
            max(supplied_total, input_tokens + output_tokens),
        ),
    )


_SAFE_IDENTIFIER_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
)


def _safe_identifier(value: Any, *, maximum: int) -> str:
    if (
        isinstance(value, str)
        and 0 < len(value) <= maximum
        and all(character in _SAFE_IDENTIFIER_CHARACTERS for character in value)
    ):
        return value
    return "unreported"


def _attempt_summary(value: Any) -> CandidateAttemptSummary:
    raw = _as_mapping(value)
    raw_phase = raw.get("phase")
    phase = (
        raw_phase
        if raw_phase
        in {"primary", "schema_fallback", "repair", "reviewer", "text"}
        else "unknown"
    )
    raw_state = raw.get("state")
    state = (
        raw_state
        if raw_state
        in {
            "accounted",
            "settled",
            "released_pre_dispatch",
            "uncertain",
            "resolved_retry",
            "resolved_skip",
        }
        else "unknown"
    )
    return CandidateAttemptSummary(
        attempt_id=_safe_identifier(raw.get("attempt_id"), maximum=128),
        provider_alias=_safe_identifier(
            raw.get("provider_alias") or raw.get("provider"),
            maximum=64,
        ),
        phase=phase,
        state=state,
        usage=_usage_summary(raw.get("usage")),
    )


def _truncation_counts(value: Any) -> tuple[int, int]:
    raw = _as_mapping(value)
    if "truncated_section_count" in raw or "dropped_item_count" in raw:
        return (
            _bounded_non_negative_int(
                raw.get("truncated_section_count"),
                maximum=100,
            ),
            _bounded_non_negative_int(
                raw.get("dropped_item_count"),
                maximum=10_000,
            ),
        )
    sections = raw.get("truncated_sections")
    raw_counts = raw.get("dropped_item_counts")
    section_count = min(100, len(sections)) if isinstance(sections, list) else 0
    dropped_count = 0
    if isinstance(raw_counts, Mapping):
        dropped_count = min(
            10_000,
            sum(
                _bounded_non_negative_int(count, maximum=10_000)
                for count in islice(raw_counts.values(), 100)
            ),
        )
    return section_count, dropped_count


def _sanitize_generation_result(
    result: ChapterGenerationResult,
) -> ChapterGenerationResult:
    if len(result.attempts) > _MAX_PIPELINE_ATTEMPTS:
        raise ValueError("repair receipt attempt evidence exceeds the V1 bound")
    truncated_section_count, dropped_item_count = _truncation_counts(
        result.truncation
    )
    return result.model_copy(
        update={
            "usage": _usage_summary(result.usage).model_dump(mode="json"),
            "attempts": [
                _attempt_summary(item).model_dump(mode="json")
                for item in result.attempts
            ],
            "truncation": {
                "truncated_section_count": truncated_section_count,
                "dropped_item_count": dropped_item_count,
            },
        },
        deep=True,
    )


class _RepairReceipt(_RepairContract):
    generation: ChapterGenerationResult

    @field_validator("generation")
    @classmethod
    def sanitize_generation(
        cls,
        value: ChapterGenerationResult,
    ) -> ChapterGenerationResult:
        return _sanitize_generation_result(value)


class ProseCandidateRepairRequest(_RepairContract):
    schema_version: Literal["prose_candidate_repair_request.v1"] = (
        PROSE_REPAIR_REQUEST_SCHEMA
    )
    cycle: int = Field(ge=1, le=MAX_FINALIZATION_REPAIR_CYCLES)
    source_run_id: str = Field(min_length=1, max_length=128)
    source_run_revision: int = Field(ge=0)
    source_content_digest: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    trigger: Literal["completion", "outline_adherence"]
    reason_codes: tuple[ProseRepairReason, ...] = Field(
        min_length=1,
        max_length=2,
    )
    issue_categories: tuple[OutlineIssueCategory, ...] = Field(
        min_length=1,
        max_length=len(_OUTLINE_ISSUE_CATEGORIES),
    )
    scene_indexes: tuple[int, ...] = Field(
        max_length=_MAX_REPAIR_SCENE_INDEXES,
    )

    @field_validator("scene_indexes")
    @classmethod
    def validate_scene_indexes(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if (
            len(set(value)) != len(value)
            or any(index < 1 or index > _MAX_REPAIR_SCENE_INDEXES for index in value)
        ):
            raise ValueError("scene indexes must be unique and within the V1 bound")
        return value

    @field_validator("reason_codes", "issue_categories")
    @classmethod
    def validate_unique_labels(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("repair labels cannot contain duplicates")
        return value


class ProseCandidateRepairReceipt(_RepairReceipt):
    schema_version: Literal["prose_candidate_repair_receipt.v1"] = (
        PROSE_REPAIR_RECEIPT_SCHEMA
    )
    source: ProseCandidateSource


class StateCandidateRepairRequest(_RepairContract):
    schema_version: Literal["state_candidate_repair_request.v1"] = (
        STATE_REPAIR_REQUEST_SCHEMA
    )
    cycle: int = Field(ge=1, le=MAX_FINALIZATION_REPAIR_CYCLES)
    proposal_id: str = Field(min_length=1, max_length=128)
    source_run_id: str = Field(min_length=1, max_length=128)
    source_run_revision: int = Field(ge=0)
    source_content_digest: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    reason_codes: tuple[StateRepairReason, ...] = Field(
        min_length=1,
        max_length=2,
    )
    consistency_issue_count: int = Field(ge=0, le=_MAX_REPAIR_CARD_IDS)
    affected_card_ids: tuple[str, ...] = Field(max_length=_MAX_REPAIR_CARD_IDS)
    dropped_reference_count: int = Field(
        ge=0,
        le=_MAX_DROPPED_REFERENCE_COUNT,
    )

    @field_validator("affected_card_ids")
    @classmethod
    def validate_card_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if (
            len(set(value)) != len(value)
            or any(
                not item or len(item) > _MAX_REPAIR_CARD_ID_LENGTH
                for item in value
            )
        ):
            raise ValueError("card ids must be unique and within the V1 bound")
        return value

    @field_validator("reason_codes")
    @classmethod
    def validate_unique_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("repair reasons cannot contain duplicates")
        return value


class StateCandidateRepairReceipt(_RepairReceipt):
    schema_version: Literal["state_candidate_repair_receipt.v1"] = (
        STATE_REPAIR_RECEIPT_SCHEMA
    )


@dataclass(frozen=True)
class ChapterCandidatePipelineProgress:
    """Metadata-only evidence retained when a candidate pipeline stops."""

    tokens: int = 0
    attempts: tuple[CandidateAttemptSummary, ...] = ()
    truncations: tuple[CandidateTruncationSummary, ...] = ()
    completed_steps: tuple[str, ...] = ()
    repair_cycles_used: int = 0
    prose_run_id: str | None = None
    prose_run_revision: int | None = None
    prose_content_digest: str | None = None
    state_proposal_id: str | None = None


class ChapterCandidatePipelineBlocked(ValueError):
    """A candidate gate failed before the formal chapter commit."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "candidate_gate_blocked",
        progress: ChapterCandidatePipelineProgress | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.progress = progress or ChapterCandidatePipelineProgress()

    def attach_progress(self, progress: ChapterCandidatePipelineProgress) -> None:
        self.progress = progress

    @property
    def attempts(self) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in self.progress.attempts]

    @property
    def usage(self) -> dict[str, int]:
        return {"total_tokens": self.progress.tokens}


class ChapterCandidatePipelineDependencyFailed(RuntimeError):
    """A non-retryable dependency stop with all prior bounded evidence attached."""

    def __init__(self, progress: ChapterCandidatePipelineProgress) -> None:
        super().__init__("候选管线依赖调用硬暂停")
        self.code = "candidate_dependency_failed"
        self.progress = progress

    @property
    def attempts(self) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in self.progress.attempts]

    @property
    def usage(self) -> dict[str, int]:
        return {"total_tokens": self.progress.tokens}


@dataclass(frozen=True)
class ChapterCandidatePipelineDeps:
    generate_prose_candidate: Callable[
        [str, dict[str, Any]],
        Awaitable[GeneratedProseCandidate],
    ]
    review_prose_candidate: Callable[
        [str, dict[str, Any], ProseCandidateSource],
        Awaitable[ChapterGenerationResult],
    ]
    generate_state_candidate: Callable[
        [str, dict[str, Any], ProseCandidateSource],
        Awaitable[ChapterGenerationResult],
    ]
    finalize: Callable[
        [
            str,
            dict[str, Any],
            ProseCandidateSource,
            Mapping[str, Any],
            Mapping[str, Any],
            int,
        ],
        Awaitable[Mapping[str, Any]],
    ]
    repair_prose_candidate: Callable[
        [
            str,
            str,
            str,
            ProseCandidateRepairRequest,
        ],
        Awaitable[ProseCandidateRepairReceipt],
    ] | None = None
    repair_state_candidate: Callable[
        [
            str,
            str,
            str,
            StateCandidateRepairRequest,
        ],
        Awaitable[StateCandidateRepairReceipt],
    ] | None = None


@dataclass(frozen=True)
class ChapterCandidatePipelineResult:
    tokens: int
    attempts: tuple[CandidateAttemptSummary, ...]
    truncations: tuple[CandidateTruncationSummary, ...]
    outline_adherence: dict[str, Any]
    consistency_issues: tuple[dict[str, Any], ...]
    prose_run_id: str
    prose_run_revision: int
    prose_content_digest: str
    state_proposal_id: str
    repair_cycles_used: int
    finalization: dict[str, Any]


def _truncation(
    step: str,
    result: ChapterGenerationResult,
) -> CandidateTruncationSummary | None:
    truncated_section_count, dropped_item_count = _truncation_counts(
        result.truncation
    )
    if not truncated_section_count and not dropped_item_count:
        return None
    return CandidateTruncationSummary(
        step=step,
        truncated_section_count=truncated_section_count,
        dropped_item_count=dropped_item_count,
    )


@dataclass
class _PipelineTrace:
    tokens: int = 0
    attempts: list[CandidateAttemptSummary] = field(default_factory=list)
    truncations: list[CandidateTruncationSummary] = field(default_factory=list)
    completed_steps: list[str] = field(default_factory=list)
    repair_cycles_used: int = 0
    source: ProseCandidateSource | None = None
    state_proposal_id: str | None = None

    def record(self, step: str, result: ChapterGenerationResult) -> None:
        usage = _usage_summary(result.usage)
        if len(result.attempts) > _MAX_PIPELINE_ATTEMPTS:
            raise ChapterCandidatePipelineBlocked(
                "候选管线调用证据超过 V1 上限"
            )
        summaries = [_attempt_summary(item) for item in result.attempts]
        self._ensure_attempt_capacity(len(summaries))
        self.tokens = min(_MAX_TOKEN_COUNT, self.tokens + usage.total_tokens)
        self.attempts.extend(summaries)
        truncation = _truncation(step, result)
        if truncation is not None:
            if len(self.truncations) >= _MAX_PIPELINE_TRUNCATIONS:
                raise ChapterCandidatePipelineBlocked(
                    "候选管线截断证据超过 V1 上限"
                )
            self.truncations.append(truncation)
        self.completed_steps.append(step)

    def record_failure(self, exc: Exception) -> None:
        raw_attempts = getattr(exc, "attempts", None)
        outcome = getattr(exc, "outcome", None)
        if not isinstance(raw_attempts, (list, tuple)) and outcome is not None:
            raw_attempts = getattr(outcome, "attempts", None)
        if (
            isinstance(raw_attempts, (list, tuple))
            and len(raw_attempts) > _MAX_PIPELINE_ATTEMPTS
        ):
            raise ChapterCandidatePipelineBlocked(
                "候选管线失败调用证据超过 V1 上限"
            )
        summaries = [
            _attempt_summary(item)
            for item in (
                raw_attempts if isinstance(raw_attempts, (list, tuple)) else ()
            )
        ]
        known_ids = {item.attempt_id for item in self.attempts}
        new_summaries = [
            item for item in summaries if item.attempt_id not in known_ids
        ]
        self._ensure_attempt_capacity(len(new_summaries))
        self.attempts.extend(new_summaries)

        usage = _usage_summary(getattr(exc, "usage", None))
        if usage.total_tokens == 0 and outcome is not None:
            usage = CandidateUsageSummary(
                total_tokens=_bounded_non_negative_int(
                    getattr(outcome, "tokens", None),
                    maximum=_MAX_TOKEN_COUNT,
                )
            )
        attempt_tokens = sum(item.usage.total_tokens for item in new_summaries)
        failure_tokens = (
            max(usage.total_tokens, attempt_tokens)
            if new_summaries or not summaries
            else 0
        )
        self.tokens = min(_MAX_TOKEN_COUNT, self.tokens + failure_tokens)

        raw_truncations = getattr(exc, "truncations", None)
        if not isinstance(raw_truncations, (list, tuple)) and outcome is not None:
            raw_truncations = getattr(outcome, "truncations", None)
        for raw in (
            raw_truncations
            if isinstance(raw_truncations, (list, tuple))
            else ()
        ):
            truncated_count, dropped_count = _truncation_counts(raw)
            if not truncated_count and not dropped_count:
                continue
            if len(self.truncations) >= _MAX_PIPELINE_TRUNCATIONS:
                break
            self.truncations.append(
                CandidateTruncationSummary(
                    step="dependency_failure",
                    truncated_section_count=truncated_count,
                    dropped_item_count=dropped_count,
                )
            )

    def _ensure_attempt_capacity(self, additional: int) -> None:
        if len(self.attempts) + additional > _MAX_PIPELINE_ATTEMPTS:
            raise ChapterCandidatePipelineBlocked(
                "候选管线调用证据超过 V1 上限"
            )

    def snapshot(self) -> ChapterCandidatePipelineProgress:
        source = self.source
        return ChapterCandidatePipelineProgress(
            tokens=self.tokens,
            attempts=tuple(self.attempts),
            truncations=tuple(self.truncations),
            completed_steps=tuple(self.completed_steps),
            repair_cycles_used=self.repair_cycles_used,
            prose_run_id=source.source_run_id if source is not None else None,
            prose_run_revision=(
                source.source_run_revision if source is not None else None
            ),
            prose_content_digest=(
                source.source_content_digest if source is not None else None
            ),
            state_proposal_id=self.state_proposal_id,
        )


def _adherence_matches_source(
    adherence: Mapping[str, Any],
    source: ProseCandidateSource,
) -> bool:
    run_id = adherence.get("source_prose_run_id")
    revision = adherence.get("source_prose_run_revision")
    digest = adherence.get("source_content_digest")
    return bool(
        isinstance(run_id, str)
        and run_id == source.source_run_id
        and type(revision) is int
        and revision >= 0
        and revision == source.source_run_revision
        and isinstance(digest, str)
        and digest == source.source_content_digest
    )


def _validate_adherence_gate(
    adherence: Mapping[str, Any],
    chapter: Mapping[str, Any],
) -> None:
    if adherence.get("verdict") != "pass":
        raise ChapterCandidatePipelineBlocked("正文候选未精确通过章纲符合度")
    issues = adherence.get("issues")
    if not isinstance(issues, list) or issues:
        raise ChapterCandidatePipelineBlocked("章纲符合度仍包含偏离问题")
    outline = chapter.get("outline")
    scenes = outline.get("scenes") if isinstance(outline, Mapping) else None
    coverage = adherence.get("scene_coverage")
    if (
        not isinstance(scenes, list)
        or not scenes
        or not isinstance(coverage, list)
        or len(coverage) != len(scenes)
    ):
        raise ChapterCandidatePipelineBlocked("章纲符合度没有覆盖全部场景")
    scene_indexes: list[int] = []
    for item in coverage:
        if not isinstance(item, Mapping):
            raise ChapterCandidatePipelineBlocked("章纲场景覆盖证据格式无效")
        scene_index = item.get("scene_index")
        if type(scene_index) is not int or item.get("status") != "covered":
            raise ChapterCandidatePipelineBlocked("章纲场景尚未全部落实")
        scene_indexes.append(scene_index)
    if (
        len(set(scene_indexes)) != len(scene_indexes)
        or set(scene_indexes) != set(range(1, len(scenes) + 1))
    ):
        raise ChapterCandidatePipelineBlocked("章纲场景覆盖不是完整唯一集合")


def _adherence_metadata(adherence: Mapping[str, Any]) -> dict[str, Any]:
    issues = list(adherence.get("issues") or [])
    categories = sorted(
        {
            str(item.get("category"))
            for item in issues
            if isinstance(item, Mapping) and item.get("category")
        }
    )
    return {
        "verdict": "pass",
        "scene_count": len(list(adherence.get("scene_coverage") or [])),
        "issue_count": len(issues),
        "issue_categories": categories,
        "source_prose_run_id": adherence["source_prose_run_id"],
        "source_prose_run_revision": adherence[
            "source_prose_run_revision"
        ],
        "source_content_digest": adherence["source_content_digest"],
    }


def _strict_repair_limit(value: Any) -> int:
    if (
        type(value) is not int
        or value < 0
        or value > MAX_FINALIZATION_REPAIR_CYCLES
    ):
        raise ValueError(
            "max_repair_cycles must be an integer between 0 and "
            f"{MAX_FINALIZATION_REPAIR_CYCLES}"
        )
    return value


def _outline_scene_indexes(chapter: Mapping[str, Any]) -> tuple[int, ...]:
    outline = chapter.get("outline")
    scenes = outline.get("scenes") if isinstance(outline, Mapping) else None
    if not isinstance(scenes, list) or not scenes:
        return ()
    if len(scenes) > _MAX_REPAIR_SCENE_INDEXES:
        raise ChapterCandidatePipelineBlocked(
            "章纲场景数量超过自动修复 V1 上限"
        )
    return tuple(range(1, len(scenes) + 1))


def _completion_repair_request(
    *,
    cycle: int,
    source: ProseCandidateSource,
    chapter: Mapping[str, Any],
) -> ProseCandidateRepairRequest:
    return ProseCandidateRepairRequest(
        cycle=cycle,
        source_run_id=source.source_run_id,
        source_run_revision=source.source_run_revision,
        source_content_digest=source.source_content_digest,
        trigger="completion",
        reason_codes=("completion_contract_failed",),
        issue_categories=(OutlineIssueCategory.SCENE_COVERAGE,),
        scene_indexes=_outline_scene_indexes(chapter),
    )


def _adherence_repair_request(
    *,
    cycle: int,
    source: ProseCandidateSource,
    adherence: Mapping[str, Any],
    chapter: Mapping[str, Any],
) -> ProseCandidateRepairRequest:
    categories: list[OutlineIssueCategory] = []
    issues = adherence.get("issues")
    if isinstance(issues, list):
        for item in issues:
            if not isinstance(item, Mapping):
                continue
            try:
                category = OutlineIssueCategory(str(item.get("category") or ""))
            except ValueError:
                continue
            categories.append(category)
    expected_indexes = set(_outline_scene_indexes(chapter))
    covered_indexes: set[int] = set()
    repair_indexes: set[int] = set()
    coverage = adherence.get("scene_coverage")
    if isinstance(coverage, list):
        for item in coverage:
            if not isinstance(item, Mapping):
                continue
            index = item.get("scene_index")
            if type(index) is not int or index not in expected_indexes:
                continue
            if item.get("status") == "covered":
                covered_indexes.add(index)
            else:
                repair_indexes.add(index)
    repair_indexes.update(expected_indexes - covered_indexes)
    if repair_indexes:
        categories.append(OutlineIssueCategory.SCENE_COVERAGE)
    stable_categories = tuple(dict.fromkeys(categories))
    return ProseCandidateRepairRequest(
        cycle=cycle,
        source_run_id=source.source_run_id,
        source_run_revision=source.source_run_revision,
        source_content_digest=source.source_content_digest,
        trigger="outline_adherence",
        reason_codes=("outline_adherence_failed",),
        issue_categories=stable_categories
        or (OutlineIssueCategory.SCENE_COVERAGE,),
        scene_indexes=tuple(sorted(repair_indexes)),
    )


def _dropped_reference_count(value: Any) -> int:
    if isinstance(value, Mapping):
        return min(
            _MAX_DROPPED_REFERENCE_COUNT,
            sum(_dropped_reference_count(item) for item in value.values()),
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return min(_MAX_DROPPED_REFERENCE_COUNT, len(value))
    return int(bool(value))


def _state_repair_request(
    *,
    cycle: int,
    proposal_id: str,
    source: ProseCandidateSource,
    declared_card_ids: frozenset[str],
    state: Mapping[str, Any],
    dropped: Mapping[str, Any],
) -> StateCandidateRepairRequest:
    raw_issues = state.get("consistency_issues")
    issues = raw_issues if isinstance(raw_issues, list) else []
    card_ids = tuple(sorted({
        card_id
        for item in issues[:_MAX_REPAIR_CARD_IDS]
        if isinstance(item, Mapping)
        for card_id in (item.get("card_id"),)
        if (
            isinstance(card_id, str)
            and 0 < len(card_id) <= _MAX_REPAIR_CARD_ID_LENGTH
            and card_id in declared_card_ids
        )
    }))
    dropped_count = _dropped_reference_count(dropped)
    reason_codes: list[StateRepairReason] = []
    if issues:
        reason_codes.append("consistency_conflict")
    if dropped_count:
        reason_codes.append("invalid_internal_reference")
    return StateCandidateRepairRequest(
        cycle=cycle,
        proposal_id=proposal_id,
        source_run_id=source.source_run_id,
        source_run_revision=source.source_run_revision,
        source_content_digest=source.source_content_digest,
        reason_codes=tuple(reason_codes),
        consistency_issue_count=(
            len(issues) if isinstance(raw_issues, list) else 1
        ),
        affected_card_ids=card_ids,
        dropped_reference_count=dropped_count,
    )


def _declared_character_card_ids(
    chapter: Mapping[str, Any],
) -> frozenset[str]:
    outline = chapter.get("outline")
    raw_ids = (
        outline.get("present_character_card_ids")
        if isinstance(outline, Mapping)
        else None
    )
    if not isinstance(raw_ids, list):
        return frozenset()
    return frozenset(
        card_id
        for card_id in raw_ids[:_MAX_REPAIR_CARD_IDS]
        if (
            isinstance(card_id, str)
            and 0 < len(card_id) <= _MAX_REPAIR_CARD_ID_LENGTH
        )
    )


def _validate_prose_candidate(
    generated: GeneratedProseCandidate | ProseCandidateRepairReceipt,
) -> tuple[ChapterGenerationResult, ProseCandidateSource]:
    prose = generated.generation
    source = generated.source
    if prose.stage is not ChapterGenerationStage.PROSE or prose.accepted:
        raise ChapterCandidatePipelineBlocked(
            "正文候选不是未接受的延迟生成结果"
        )
    if type(prose.value) is not str or prose.value != source.text:
        raise ChapterCandidatePipelineBlocked("正文候选值与来源投影不一致")
    if chapter_content_digest(source.text) != source.source_content_digest:
        raise ChapterCandidatePipelineBlocked("正文候选摘要与正文不一致")
    source_completion = dict(source.completion)
    if dict(prose.completion) != source_completion:
        raise ChapterCandidatePipelineBlocked("正文候选完成投影不一致")
    if (
        source_completion.get("source_run_id") != source.source_run_id
        or type(source_completion.get("source_run_revision")) is not int
        or source_completion.get("source_run_revision")
        != source.source_run_revision
        or source_completion.get("source_run_digest")
        != source.source_content_digest
    ):
        raise ChapterCandidatePipelineBlocked("正文候选来源身份不一致")
    return prose, source


def _completion_passed(source: ProseCandidateSource) -> bool:
    completion = source.completion
    return bool(
        completion.get("can_write_formal_prose") is True
        and completion.get("status") == "complete"
    )


def _prose_repair_advanced_revision(
    previous: ProseCandidateSource,
    current: ProseCandidateSource,
) -> bool:
    return bool(
        current.source_run_id == previous.source_run_id
        and current.source_run_revision > previous.source_run_revision
    )


def _next_repair_cycle(used: int, limit: int) -> int:
    if used >= limit:
        raise ChapterCandidatePipelineBlocked(
            "候选仍未通过闸门，已达到授权的修复次数上限"
        )
    return used + 1


def _validate_state_shape(
    state_result: ChapterGenerationResult,
) -> tuple[dict[str, Any], str, str, tuple[dict[str, Any], ...]]:
    if (
        state_result.stage is not ChapterGenerationStage.STATE
        or state_result.accepted
    ):
        raise ChapterCandidatePipelineBlocked(
            "状态候选不是未接受的延迟生成结果"
        )
    if not isinstance(state_result.value, Mapping):
        raise ChapterCandidatePipelineBlocked("状态候选不是有效映射")
    state = dict(state_result.value)
    proposal_id = state.get("proposal_id")
    acceptance_token = state.get("acceptance_token")
    if (
        not isinstance(proposal_id, str)
        or not proposal_id
        or not isinstance(acceptance_token, str)
        or not acceptance_token
    ):
        raise ChapterCandidatePipelineBlocked("状态候选缺少接受回执")
    raw_issues = state.get("consistency_issues")
    if not isinstance(raw_issues, list) or any(
        not isinstance(item, Mapping) for item in raw_issues
    ):
        raise ChapterCandidatePipelineBlocked("状态候选冲突证据格式无效")
    if len(raw_issues) > _MAX_REPAIR_CARD_IDS:
        raise ChapterCandidatePipelineBlocked("状态候选冲突数量超过 V1 上限")
    issues = tuple(dict(item) for item in raw_issues)
    return state, proposal_id, acceptance_token, issues


class ChapterCandidatePipeline:
    """Hide the ordered candidate gates behind one safe orchestration interface."""

    def __init__(self, deps: ChapterCandidatePipelineDeps) -> None:
        self._deps = deps

    async def _apply_prose_repair(
        self,
        *,
        owner_id: str | None,
        novel_id: str,
        chapter_id: str,
        source: ProseCandidateSource,
        request: ProseCandidateRepairRequest,
        trace: _PipelineTrace,
    ) -> tuple[ProseCandidateSource, bool]:
        repair = self._deps.repair_prose_candidate
        if repair is None:
            raise ChapterCandidatePipelineBlocked("正文候选没有授权修复入口")
        trace.repair_cycles_used = request.cycle
        if not isinstance(owner_id, str) or not owner_id:
            raise ChapterCandidatePipelineBlocked(
                "自动修复缺少 owner-scoped 身份"
            )
        receipt = await repair(
            owner_id,
            novel_id,
            chapter_id,
            request,
        )
        if not isinstance(receipt, ProseCandidateRepairReceipt):
            raise ChapterCandidatePipelineBlocked("正文修复回执版本无效")
        trace.record(
            f"prose_repair_{request.cycle}",
            receipt.generation,
        )
        _repaired_prose, repaired_source = _validate_prose_candidate(receipt)
        if not _prose_repair_advanced_revision(source, repaired_source):
            raise ChapterCandidatePipelineBlocked(
                "正文修复没有产生新候选",
                code="repair_no_progress",
            )
        same_digest = (
            repaired_source.source_content_digest
            == source.source_content_digest
        )
        trace.source = repaired_source
        return repaired_source, same_digest

    async def run(
        self,
        *,
        owner_id: str | None = None,
        novel_id: str,
        chapter: dict[str, Any],
        max_repair_cycles: int = 0,
    ) -> ChapterCandidatePipelineResult:
        repair_limit = _strict_repair_limit(max_repair_cycles)
        trace = _PipelineTrace()
        try:
            return await self._run(
                novel_id=novel_id,
                owner_id=owner_id,
                chapter=chapter,
                repair_limit=repair_limit,
                trace=trace,
            )
        except ChapterCandidatePipelineBlocked as exc:
            exc.attach_progress(trace.snapshot())
            raise
        except Exception as exc:
            try:
                trace.record_failure(exc)
            except ChapterCandidatePipelineBlocked as projection_error:
                projection_error.attach_progress(trace.snapshot())
                raise projection_error from exc
            raise ChapterCandidatePipelineDependencyFailed(
                trace.snapshot()
            ) from exc

    async def _run(
        self,
        *,
        owner_id: str | None,
        novel_id: str,
        chapter: dict[str, Any],
        repair_limit: int,
        trace: _PipelineTrace,
    ) -> ChapterCandidatePipelineResult:
        chapter_id = str(chapter.get("_id") or "")
        if not chapter_id:
            raise ChapterCandidatePipelineBlocked("章节候选缺少内部章节 ID")
        generated = await self._deps.generate_prose_candidate(novel_id, chapter)
        trace.record("prose", generated.generation)
        _prose, source = _validate_prose_candidate(generated)
        trace.source = source

        review_count = 0
        last_repair_kept_digest = False
        while True:
            if not _completion_passed(source):
                if last_repair_kept_digest:
                    raise ChapterCandidatePipelineBlocked(
                        "正文摘要未变化且完成闸门复检仍未通过",
                        code="repair_no_progress",
                    )
                if self._deps.repair_prose_candidate is None:
                    raise ChapterCandidatePipelineBlocked(
                        "正文候选未通过完成闸门"
                    )
                cycle = _next_repair_cycle(
                    trace.repair_cycles_used,
                    repair_limit,
                )
                source, last_repair_kept_digest = (
                    await self._apply_prose_repair(
                        owner_id=owner_id,
                        novel_id=novel_id,
                        chapter_id=chapter_id,
                        source=source,
                        request=_completion_repair_request(
                            cycle=cycle,
                            source=source,
                            chapter=chapter,
                        ),
                        trace=trace,
                    )
                )
                continue

            reviewed = await self._deps.review_prose_candidate(
                novel_id,
                chapter,
                source,
            )
            review_count += 1
            trace.record(
                (
                    "outline_adherence"
                    if review_count == 1
                    else f"outline_adherence_recheck_{review_count}"
                ),
                reviewed,
            )
            if reviewed.stage is not ChapterGenerationStage.OUTLINE_ADHERENCE:
                raise ChapterCandidatePipelineBlocked(
                    "章纲符合度返回了错误阶段"
                )
            if not isinstance(reviewed.value, Mapping):
                raise ChapterCandidatePipelineBlocked(
                    "章纲符合度不是有效映射"
                )
            adherence = dict(reviewed.value)
            if not _adherence_matches_source(adherence, source):
                raise ChapterCandidatePipelineBlocked(
                    "章纲符合度没有绑定正文候选"
                )
            try:
                _validate_adherence_gate(adherence, chapter)
            except ChapterCandidatePipelineBlocked as gate_error:
                if last_repair_kept_digest:
                    raise ChapterCandidatePipelineBlocked(
                        "正文摘要未变化且章纲复检仍未通过",
                        code="repair_no_progress",
                    ) from gate_error
                if self._deps.repair_prose_candidate is None:
                    raise
                cycle = _next_repair_cycle(
                    trace.repair_cycles_used,
                    repair_limit,
                )
                source, last_repair_kept_digest = (
                    await self._apply_prose_repair(
                        owner_id=owner_id,
                        novel_id=novel_id,
                        chapter_id=chapter_id,
                        source=source,
                        request=_adherence_repair_request(
                            cycle=cycle,
                            source=source,
                            adherence=adherence,
                            chapter=chapter,
                        ),
                        trace=trace,
                    )
                )
                continue
            break

        state_result = await self._deps.generate_state_candidate(
            novel_id,
            chapter,
            source,
        )
        trace.record("state", state_result)
        while True:
            state, proposal_id, _acceptance_token, consistency_issues = (
                _validate_state_shape(state_result)
            )
            trace.state_proposal_id = proposal_id
            dropped = dict(state_result.dropped or {})
            if not consistency_issues and not dropped:
                break
            if self._deps.repair_state_candidate is None:
                raise ChapterCandidatePipelineBlocked(
                    "状态候选仍有一致性冲突或无效引用"
                )
            cycle = _next_repair_cycle(
                trace.repair_cycles_used,
                repair_limit,
            )
            request = _state_repair_request(
                cycle=cycle,
                proposal_id=proposal_id,
                source=source,
                declared_card_ids=_declared_character_card_ids(chapter),
                state=state,
                dropped=dropped,
            )
            trace.repair_cycles_used = cycle
            if not isinstance(owner_id, str) or not owner_id:
                raise ChapterCandidatePipelineBlocked(
                    "自动修复缺少 owner-scoped 身份"
                )
            receipt = await self._deps.repair_state_candidate(
                owner_id,
                novel_id,
                chapter_id,
                request,
            )
            if not isinstance(receipt, StateCandidateRepairReceipt):
                raise ChapterCandidatePipelineBlocked("状态修复回执版本无效")
            state_result = receipt.generation
            trace.record(f"state_repair_{cycle}", state_result)
            _next_state, next_proposal_id, _next_token, _next_issues = (
                _validate_state_shape(state_result)
            )
            if next_proposal_id == proposal_id:
                raise ChapterCandidatePipelineBlocked(
                    "状态修复没有产生新候选",
                    code="repair_no_progress",
                )
            trace.state_proposal_id = next_proposal_id

        finalization = dict(
            await self._deps.finalize(
                novel_id,
                chapter,
                source,
                adherence,
                state,
                trace.repair_cycles_used,
            )
        )
        progress = trace.snapshot()
        return ChapterCandidatePipelineResult(
            tokens=progress.tokens,
            attempts=progress.attempts,
            truncations=progress.truncations,
            outline_adherence=_adherence_metadata(adherence),
            consistency_issues=consistency_issues,
            prose_run_id=source.source_run_id,
            prose_run_revision=source.source_run_revision,
            prose_content_digest=source.source_content_digest,
            state_proposal_id=proposal_id,
            repair_cycles_used=trace.repair_cycles_used,
            finalization=finalization,
        )
