"""Deferred chapter tail: candidates first, one deterministic formal commit last."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import islice
from typing import Any, Awaitable, Callable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.llm.schemas.scene_contract_pydantic import (
    MAX_V2_ADHERENCE_ISSUES,
    MAX_V3_LOCAL_ADHERENCE_ISSUES,
    ValidatedChapterOutlineAdherenceEvidenceSchema,
    ValidatedChapterOutlineAdherenceEvidenceV3Schema,
    ValidatedChapterOutlineAdherenceEvidenceV4Schema,
)
from backend.scene_contract_versions import (
    LEGACY_OUTLINE_ADHERENCE_EVIDENCE_VERSION,
    LEGACY_LOCAL_OUTLINE_ADHERENCE_EVIDENCE_VERSION,
    OUTLINE_ADHERENCE_EVIDENCE_VERSION,
)
from backend.services.generation.chapter_generation_application import (
    ChapterGenerationResult,
    ChapterGenerationStage,
    ProseCandidateSource,
)
from backend.services.generation.chapter_finalization import (
    MAX_FINALIZATION_REPAIR_CYCLES,
)
from backend.services.generation.chapter_repair_policy import (
    ChapterRepairPolicy,
    RepairBudgetExhausted,
    RepairBudgetLimitsV1,
    RepairChargeV1,
    RepairComponent,
    RepairComponentUsageV1,
    RepairConvergenceEvidenceV1,
    RepairIssueV1,
    default_repair_budget_limits,
    repair_next_step,
)
from backend.services.generation.candidate_repair_contracts import (
    MAX_CANDIDATE_OUTLINE_SCENES,
    MAX_CHAPTER_CANDIDATE_COMPONENT_REPAIRS,
    MAX_CHAPTER_CANDIDATE_PIPELINE_CHECKPOINTS,
    AdherenceCandidateCheckpoint,
    AdherenceCandidateCheckpointV1,
    AdherenceCandidateCheckpointV3,
    AdherenceCandidateCheckpointV4,
    AdherenceCandidateCheckpointV5,
    CandidateCompletionProjectionV1,
    CandidatePipelineCheckpointConflict,
    CandidatePipelineCheckpointV1,
    CandidateSceneCoverageV1,
    CandidateSourceIdentityV1,
    CandidateTruncationProjectionV1,
    ProseCandidateCheckpointV1,
    StateCandidateCheckpoint,
    StateCandidateCheckpointV1,
    StateCandidateCheckpointV3,
    candidate_pipeline_checkpoint_digest,
    is_safe_candidate_identifier,
    parse_candidate_pipeline_checkpoint,
    replay_candidate_pipeline_checkpoints,
)
from backend.services.generation.headless_generation import (
    GeneratedProseCandidate,
)
from backend.services.generation.outline_adherence import (
    OUTLINE_ISSUE_CATEGORIES,
    OutlineIssueCategory,
    OutlineAdherenceValidationError,
    validate_complete_outline_adherence,
)
from backend.services.generation.prose_runs import chapter_content_digest
from backend.services.generation.prose_completion_contract import (
    completion_allows_formal_write,
)
from backend.services.novel.state_fact_accounting import (
    StateFactAccountingError,
    automatic_state_fact_decision,
)
from backend.services.llm.pre_dispatch_boundaries import (
    AttemptCapacityExceeded,
    TokenBudgetExceeded,
)
from backend.services.generation.state_repair_contracts import (
    MAX_STATE_REPAIR_CARD_ID_LENGTH,
    MAX_STATE_REPAIR_CARD_IDS,
    MAX_STATE_REPAIR_DROPPED_REFERENCES,
    StateRepairDirective,
    StateRepairReason,
)


_LOCAL_POLICY_EVIDENCE_VERSIONS = frozenset({
    LEGACY_LOCAL_OUTLINE_ADHERENCE_EVIDENCE_VERSION,
    OUTLINE_ADHERENCE_EVIDENCE_VERSION,
})


_MAX_REPAIR_SCENE_INDEXES = MAX_CANDIDATE_OUTLINE_SCENES
_MAX_PIPELINE_ATTEMPTS = 512
_MAX_PIPELINE_TRUNCATIONS = 32
_MAX_PIPELINE_UNATTRIBUTED_USAGE = 32
_MAX_TOKEN_COUNT = 1_000_000_000
_MAX_RESUMED_ADHERENCE_COVERAGE = MAX_CANDIDATE_OUTLINE_SCENES
_MAX_DROPPED_PROJECTION_DEPTH = 8
_MAX_DROPPED_PROJECTION_NODES = 1_000
_MAX_DROPPED_PROJECTION_MAPPING_KEYS = 100

PROSE_REPAIR_REQUEST_SCHEMA = "prose_candidate_repair_request.v1"
PROSE_REPAIR_RECEIPT_SCHEMA = "prose_candidate_repair_receipt.v1"
STATE_REPAIR_REQUEST_SCHEMA = "state_candidate_repair_request.v1"
STATE_REPAIR_RECEIPT_SCHEMA = "state_candidate_repair_receipt.v1"

ProseRepairReason = Literal[
    "completion_contract_failed",
    "outline_adherence_failed",
]


_OUTLINE_ISSUE_CATEGORIES = OUTLINE_ISSUE_CATEGORIES


class CandidateAttemptPhase(StrEnum):
    PRIMARY = "primary"
    SCHEMA_FALLBACK = "schema_fallback"
    REPAIR = "repair"
    REVIEWER = "reviewer"
    TEXT = "text"
    UNKNOWN = "unknown"


class CandidateAttemptState(StrEnum):
    ACCOUNTED = "accounted"
    SETTLED = "settled"
    RELEASED_PRE_DISPATCH = "released_pre_dispatch"
    UNCERTAIN = "uncertain"
    RESOLVED_RETRY = "resolved_retry"
    RESOLVED_SKIP = "resolved_skip"
    RESOLVED_ABORT = "resolved_abort"
    UNKNOWN = "unknown"


class CandidateUsageEvidenceKind(StrEnum):
    EXACT = "exact"
    INCOMPLETE = "incomplete"
    LOWER_BOUND = "lower_bound"


class CandidateUnattributedUsageReason(StrEnum):
    MISSING_ATTEMPT_IDENTITY = "missing_attempt_identity"
    ATTEMPT_EVIDENCE_INVALID = "attempt_evidence_invalid"
    AGGREGATE_USAGE_INVALID = "aggregate_usage_invalid"
    AGGREGATE_RESIDUAL_UNATTRIBUTED = "aggregate_residual_unattributed"
    CHARGED_ATTEMPT_USAGE_MISSING = "charged_attempt_usage_missing"
    ATTEMPT_LEDGER_CONFLICT = "attempt_ledger_conflict"
    ATTEMPT_LEDGER_CAPACITY_EXCEEDED = "attempt_ledger_capacity_exceeded"
    USAGE_PROJECTION_OVERFLOW = "usage_projection_overflow"
    RELEASED_PREDISPATCH_USAGE_INVALID = (
        "released_predispatch_usage_invalid"
    )


class _ResumePhase(StrEnum):
    START = "start"
    PROSE = "prose"
    ADHERENCE = "adherence"
    STATE = "state"


_CHARGED_ATTEMPT_STATES = frozenset({
    CandidateAttemptState.ACCOUNTED,
    CandidateAttemptState.SETTLED,
    CandidateAttemptState.UNCERTAIN,
    CandidateAttemptState.RESOLVED_RETRY,
    CandidateAttemptState.RESOLVED_SKIP,
    CandidateAttemptState.RESOLVED_ABORT,
})
_JUDGE_OR_SCHEMA_RETRY_PHASES = frozenset({
    CandidateAttemptPhase.SCHEMA_FALLBACK,
    CandidateAttemptPhase.REPAIR,
    CandidateAttemptPhase.REVIEWER,
})


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


class CandidateUnattributedUsageSummary(_RepairContract):
    schema_version: Literal["chapter_candidate_unattributed_usage.v1"] = (
        "chapter_candidate_unattributed_usage.v1"
    )
    reason: CandidateUnattributedUsageReason
    usage: CandidateUsageSummary
    evidence_kind: CandidateUsageEvidenceKind = CandidateUsageEvidenceKind.EXACT


class CandidateAttemptSummary(_RepairContract):
    schema_version: Literal["chapter_candidate_attempt.v1"] = (
        "chapter_candidate_attempt.v1"
    )
    attempt_id: str = Field(min_length=1, max_length=128)
    provider_alias: str = Field(default="unreported", min_length=1, max_length=64)
    phase: CandidateAttemptPhase = CandidateAttemptPhase.UNKNOWN
    state: CandidateAttemptState = CandidateAttemptState.UNKNOWN
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


class _EvidenceProjectionError(ValueError):
    pass


class _UsageProjectionOverflow(_EvidenceProjectionError):
    pass


class _IncompleteAggregateUsageProjectionError(_EvidenceProjectionError):
    def __init__(
        self,
        message: str,
        *,
        evidence_kind: CandidateUsageEvidenceKind,
    ) -> None:
        super().__init__(message)
        self.evidence_kind = evidence_kind


class _UnattributedUsageProjectionError(_EvidenceProjectionError):
    def __init__(
        self,
        message: str,
        *,
        reason: CandidateUnattributedUsageReason,
        usage: CandidateUsageSummary,
        attempts: tuple[CandidateAttemptSummary, ...],
        evidence_kind: CandidateUsageEvidenceKind = (
            CandidateUsageEvidenceKind.EXACT
        ),
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.usage = usage
        self.attempts = attempts
        self.evidence_kind = evidence_kind


def _strict_token_count(value: Any, *, field: str) -> int:
    if type(value) is not int or value < 0:
        raise _EvidenceProjectionError(f"{field} 不是有效的有界 Token 用量")
    if value > _MAX_TOKEN_COUNT:
        raise _UsageProjectionOverflow(f"{field} 超过 V1 Token 用量上限")
    return value


def _usage_summary(
    value: Any,
    *,
    aggregate: bool = False,
) -> CandidateUsageSummary:
    if value is None:
        raw: Mapping[str, Any] = {}
    else:
        raw = _as_mapping(value)
        if not raw and value not in ({},):
            raise _EvidenceProjectionError("Token 用量证据格式无效")
    present = {
        field
        for field in ("input_tokens", "output_tokens", "total_tokens")
        if field in raw
    }
    if not present:
        if aggregate:
            raise _EvidenceProjectionError("聚合 Token 用量证据缺失")
        return CandidateUsageSummary()
    if not aggregate and present != {
        "input_tokens",
        "output_tokens",
        "total_tokens",
    }:
        raise _EvidenceProjectionError("Token 用量证据不完整")
    if aggregate and "total_tokens" not in present:
        raise _EvidenceProjectionError("聚合 Token 用量证据不完整")
    input_tokens = (
        _strict_token_count(raw["input_tokens"], field="input_tokens")
        if "input_tokens" in present
        else 0
    )
    output_tokens = (
        _strict_token_count(raw["output_tokens"], field="output_tokens")
        if "output_tokens" in present
        else 0
    )
    supplied_total = _strict_token_count(
        raw["total_tokens"],
        field="total_tokens",
    )
    total_tokens = max(supplied_total, input_tokens + output_tokens)
    if total_tokens > _MAX_TOKEN_COUNT:
        raise _UsageProjectionOverflow("Token 用量证据超过 V1 上限")
    return CandidateUsageSummary(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


def _safe_identifier(value: Any, *, maximum: int) -> str:
    if is_safe_candidate_identifier(value, maximum=maximum):
        return value
    return "unreported"


def _attempt_summary(value: Any) -> CandidateAttemptSummary:
    raw = _as_mapping(value)
    if not raw:
        raise _EvidenceProjectionError("调用证据格式无效")
    attempt_id = _safe_identifier(raw.get("attempt_id"), maximum=128)
    if attempt_id == "unreported":
        raise _EvidenceProjectionError("调用证据缺少稳定 attempt_id")
    raw_phase = raw.get("phase")
    try:
        phase = (
            CandidateAttemptPhase(raw_phase)
            if isinstance(raw_phase, str)
            else CandidateAttemptPhase.UNKNOWN
        )
    except ValueError:
        phase = CandidateAttemptPhase.UNKNOWN
    raw_state = raw.get("state")
    try:
        state = (
            CandidateAttemptState(raw_state)
            if isinstance(raw_state, str)
            else CandidateAttemptState.UNKNOWN
        )
    except ValueError:
        state = CandidateAttemptState.UNKNOWN
    return CandidateAttemptSummary(
        attempt_id=attempt_id,
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


def _project_attempt_batch(
    attempts: list[Any] | tuple[Any, ...],
) -> tuple[CandidateAttemptSummary, ...]:
    if len(attempts) > _MAX_PIPELINE_ATTEMPTS:
        raise _EvidenceProjectionError("调用证据超过 V1 上限")
    projected: dict[str, CandidateAttemptSummary] = {}
    ordered: list[CandidateAttemptSummary] = []
    for raw_attempt in attempts:
        summary = _attempt_summary(raw_attempt)
        existing = projected.get(summary.attempt_id)
        if existing is None:
            projected[summary.attempt_id] = summary
            ordered.append(summary)
        elif existing != summary:
            raise _EvidenceProjectionError("同一 attempt_id 的调用证据冲突")
    return tuple(ordered)


def _conservative_usage_max(
    left: CandidateUsageSummary,
    right: CandidateUsageSummary,
) -> CandidateUsageSummary:
    input_tokens = max(left.input_tokens, right.input_tokens)
    output_tokens = max(left.output_tokens, right.output_tokens)
    component_total = _checked_token_add(input_tokens, output_tokens)
    total_tokens = max(
        left.total_tokens,
        right.total_tokens,
        component_total,
    )
    return CandidateUsageSummary(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


def _checked_token_add(left: int, right: int) -> int:
    total = left + right
    if total > _MAX_TOKEN_COUNT:
        raise _UsageProjectionOverflow("Token 用量证据超过 V1 上限")
    return total


def _usage_component_floor(value: Any) -> CandidateUsageSummary:
    raw = _as_mapping(value)

    def component(field: str) -> int:
        candidate = raw.get(field)
        if type(candidate) is not int or candidate < 0:
            return 0
        if candidate > _MAX_TOKEN_COUNT:
            raise _UsageProjectionOverflow(
                f"{field} 超过 V1 Token 用量上限"
            )
        return candidate

    input_tokens = component("input_tokens")
    output_tokens = component("output_tokens")
    supplied_total = component("total_tokens")
    return CandidateUsageSummary(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=max(
            supplied_total,
            _checked_token_add(input_tokens, output_tokens),
        ),
    )


def _project_aggregate_usage(
    value: Any,
) -> tuple[CandidateUsageSummary, _EvidenceProjectionError | None]:
    raw = _as_mapping(value)
    declared_kinds: list[CandidateUsageEvidenceKind] = []
    if "usage_evidence_kind" in raw:
        try:
            declared_kinds.append(
                CandidateUsageEvidenceKind(raw.get("usage_evidence_kind"))
            )
        except (TypeError, ValueError):
            return (
                _usage_component_floor(raw),
                _EvidenceProjectionError("Token 用量完整性标记无效"),
            )
    if "usage_is_complete" in raw or "usage_is_lower_bound" in raw:
        is_complete = raw.get("usage_is_complete")
        is_lower_bound = raw.get("usage_is_lower_bound")
        if type(is_complete) is not bool or type(is_lower_bound) is not bool:
            return (
                _usage_component_floor(raw),
                _EvidenceProjectionError("Token 用量完整性标记无效"),
            )
        if is_lower_bound and is_complete:
            return (
                _usage_component_floor(raw),
                _EvidenceProjectionError("完整 Token 用量不能标记为下界"),
            )
        declared_kinds.append(
            CandidateUsageEvidenceKind.EXACT
            if is_complete
            else (
                CandidateUsageEvidenceKind.LOWER_BOUND
                if is_lower_bound
                else CandidateUsageEvidenceKind.INCOMPLETE
            )
        )
    if len(set(declared_kinds)) > 1:
        return (
            _usage_component_floor(raw),
            _IncompleteAggregateUsageProjectionError(
                "新旧 Token 用量完整性标记相互冲突",
                evidence_kind=CandidateUsageEvidenceKind.INCOMPLETE,
            ),
        )
    if declared_kinds and (
        declared_kinds[0] is not CandidateUsageEvidenceKind.EXACT
    ):
        return (
            _usage_component_floor(raw),
            _IncompleteAggregateUsageProjectionError(
                "Token 用量不是精确完整投影",
                evidence_kind=declared_kinds[0],
            ),
        )
    try:
        return _usage_summary(value, aggregate=True), None
    except _UsageProjectionOverflow:
        raise
    except _EvidenceProjectionError as exc:
        return _usage_component_floor(value), exc


def _usage_overflow_error(
    message: str,
) -> _UnattributedUsageProjectionError:
    return _UnattributedUsageProjectionError(
        message,
        reason=CandidateUnattributedUsageReason.USAGE_PROJECTION_OVERFLOW,
        usage=CandidateUsageSummary(total_tokens=_MAX_TOKEN_COUNT),
        attempts=(),
        evidence_kind=CandidateUsageEvidenceKind.LOWER_BOUND,
    )


def _merge_usage_evidence_kind(
    *kinds: CandidateUsageEvidenceKind,
) -> CandidateUsageEvidenceKind:
    if CandidateUsageEvidenceKind.LOWER_BOUND in kinds:
        return CandidateUsageEvidenceKind.LOWER_BOUND
    if CandidateUsageEvidenceKind.INCOMPLETE in kinds:
        return CandidateUsageEvidenceKind.INCOMPLETE
    return CandidateUsageEvidenceKind.EXACT


def _aggregate_usage_evidence_kind(
    error: _EvidenceProjectionError | None,
) -> CandidateUsageEvidenceKind:
    if isinstance(error, _IncompleteAggregateUsageProjectionError):
        return error.evidence_kind
    if error is not None:
        return CandidateUsageEvidenceKind.INCOMPLETE
    return CandidateUsageEvidenceKind.EXACT


def _bounded_unattributed_attempt_usage(
    attempts: list[Any] | tuple[Any, ...],
) -> CandidateUsageSummary:
    if len(attempts) > _MAX_PIPELINE_ATTEMPTS:
        return CandidateUsageSummary()
    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    for raw_attempt in attempts:
        raw = _as_mapping(raw_attempt)
        if not raw:
            continue
        usage = _usage_component_floor(raw.get("usage"))
        input_tokens = _checked_token_add(
            input_tokens,
            usage.input_tokens,
        )
        output_tokens = _checked_token_add(
            output_tokens,
            usage.output_tokens,
        )
        total_tokens = _checked_token_add(
            total_tokens,
            usage.total_tokens,
        )
    return CandidateUsageSummary(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=max(
            total_tokens,
            _checked_token_add(input_tokens, output_tokens),
        ),
    )


def _project_attempt_batch_with_aggregate(
    attempts: list[Any] | tuple[Any, ...],
    aggregate: CandidateUsageSummary,
    *,
    aggregate_evidence_kind: CandidateUsageEvidenceKind,
) -> tuple[CandidateAttemptSummary, ...]:
    try:
        return _project_attempt_batch(attempts)
    except _EvidenceProjectionError as exc:
        try:
            unattributed_usage = _conservative_usage_max(
                aggregate,
                _bounded_unattributed_attempt_usage(attempts),
            )
        except _UsageProjectionOverflow as overflow:
            raise _usage_overflow_error(str(overflow)) from overflow
        raise _UnattributedUsageProjectionError(
            str(exc),
            reason=CandidateUnattributedUsageReason.ATTEMPT_EVIDENCE_INVALID,
            usage=unattributed_usage,
            attempts=(),
            evidence_kind=_merge_usage_evidence_kind(
                aggregate_evidence_kind,
                CandidateUsageEvidenceKind.INCOMPLETE,
            ),
        ) from exc


def _invalid_aggregate_error(
    error: _EvidenceProjectionError,
    *,
    aggregate_floor: CandidateUsageSummary,
    attempts: tuple[CandidateAttemptSummary, ...],
) -> _UnattributedUsageProjectionError:
    try:
        usage = _conservative_usage_max(
            aggregate_floor,
            _summed_usage(attempts),
        )
    except _UsageProjectionOverflow as overflow:
        return _usage_overflow_error(str(overflow))
    return _UnattributedUsageProjectionError(
        str(error),
        reason=CandidateUnattributedUsageReason.AGGREGATE_USAGE_INVALID,
        usage=usage,
        attempts=attempts,
        evidence_kind=_aggregate_usage_evidence_kind(error),
    )


def _effective_usage(
    aggregate: CandidateUsageSummary,
    attempts: tuple[CandidateAttemptSummary, ...],
) -> CandidateUsageSummary:
    return _conservative_usage_max(aggregate, _summed_usage(attempts))


def _usage_residual(
    usage: CandidateUsageSummary,
    accounted: CandidateUsageSummary,
) -> CandidateUsageSummary:
    input_tokens = max(0, usage.input_tokens - accounted.input_tokens)
    output_tokens = max(0, usage.output_tokens - accounted.output_tokens)
    total_tokens = max(
        0,
        usage.total_tokens - accounted.total_tokens,
        _checked_token_add(input_tokens, output_tokens),
    )
    return CandidateUsageSummary(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


def _usage_add(
    left: CandidateUsageSummary,
    right: CandidateUsageSummary,
) -> CandidateUsageSummary:
    input_tokens = _checked_token_add(
        left.input_tokens,
        right.input_tokens,
    )
    output_tokens = _checked_token_add(
        left.output_tokens,
        right.output_tokens,
    )
    left_floor = max(
        left.total_tokens,
        _checked_token_add(left.input_tokens, left.output_tokens),
    )
    right_floor = max(
        right.total_tokens,
        _checked_token_add(right.input_tokens, right.output_tokens),
    )
    total_tokens = _checked_token_add(
        left_floor,
        right_floor,
    )
    return CandidateUsageSummary(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


def _usage_delta(
    current: CandidateUsageSummary,
    accounted: CandidateUsageSummary,
) -> tuple[CandidateUsageSummary, CandidateUsageEvidenceKind]:
    current_floor = max(
        current.total_tokens,
        _checked_token_add(current.input_tokens, current.output_tokens),
    )
    accounted_floor = max(
        accounted.total_tokens,
        _checked_token_add(accounted.input_tokens, accounted.output_tokens),
    )
    total_delta = max(0, current_floor - accounted_floor)
    if (
        current.input_tokens >= accounted.input_tokens
        and current.output_tokens >= accounted.output_tokens
    ):
        input_delta = current.input_tokens - accounted.input_tokens
        output_delta = current.output_tokens - accounted.output_tokens
        if total_delta >= _checked_token_add(input_delta, output_delta):
            return (
                CandidateUsageSummary(
                    input_tokens=input_delta,
                    output_tokens=output_delta,
                    total_tokens=total_delta,
                ),
                CandidateUsageEvidenceKind.EXACT,
            )
    return (
        CandidateUsageSummary(total_tokens=total_delta),
        CandidateUsageEvidenceKind.INCOMPLETE,
    )


def _summed_usage_values(
    usages: tuple[CandidateUsageSummary, ...],
) -> CandidateUsageSummary:
    total = CandidateUsageSummary()
    for usage in usages:
        total = _usage_add(total, usage)
    return total


def _summed_usage(
    attempts: tuple[CandidateAttemptSummary, ...],
) -> CandidateUsageSummary:
    return _summed_usage_values(tuple(item.usage for item in attempts))


def _attempt_usage_violation(
    attempt: CandidateAttemptSummary,
) -> tuple[str, CandidateUnattributedUsageReason] | None:
    usage_floor = _usage_component_floor(attempt.usage)
    if (
        attempt.state is CandidateAttemptState.RELEASED_PRE_DISPATCH
        and usage_floor.total_tokens != 0
    ):
        return (
            "派发前释放的 attempt 不能包含实际 Token 用量",
            CandidateUnattributedUsageReason.RELEASED_PREDISPATCH_USAGE_INVALID,
        )
    if (
        attempt.state in _CHARGED_ATTEMPT_STATES
        and usage_floor.total_tokens == 0
    ):
        return (
            "已计费 attempt 缺少 Token 用量",
            CandidateUnattributedUsageReason.CHARGED_ATTEMPT_USAGE_MISSING,
        )
    return None


def _unattributed_usage_error(
    message: str,
    *,
    reason: CandidateUnattributedUsageReason,
    aggregate: CandidateUsageSummary,
    attempts: tuple[CandidateAttemptSummary, ...],
    evidence_kind: CandidateUsageEvidenceKind = CandidateUsageEvidenceKind.EXACT,
) -> _UnattributedUsageProjectionError:
    return _UnattributedUsageProjectionError(
        message,
        reason=reason,
        usage=_effective_usage(aggregate, attempts),
        attempts=attempts,
        evidence_kind=evidence_kind,
    )


def _attribute_aggregate_usage(
    aggregate: CandidateUsageSummary,
    attempts: tuple[CandidateAttemptSummary, ...],
    *,
    aggregate_evidence_kind: CandidateUsageEvidenceKind,
) -> tuple[CandidateAttemptSummary, ...]:
    if not attempts:
        if aggregate.total_tokens:
            raise _unattributed_usage_error(
                "聚合 Token 用量缺少 attempt 归属",
                reason=(
                    CandidateUnattributedUsageReason.MISSING_ATTEMPT_IDENTITY
                ),
                aggregate=aggregate,
                attempts=attempts,
                evidence_kind=aggregate_evidence_kind,
            )
        return attempts
    violation = next(
        (
            violation
            for item in attempts
            if (violation := _attempt_usage_violation(item)) is not None
        ),
        None,
    )
    if violation is not None:
        message, reason = violation
        raise _unattributed_usage_error(
            message,
            reason=reason,
            aggregate=aggregate,
            attempts=attempts,
            evidence_kind=_merge_usage_evidence_kind(
                aggregate_evidence_kind,
                CandidateUsageEvidenceKind.INCOMPLETE,
            ),
        )
    residual = _usage_residual(aggregate, _summed_usage(attempts))
    unreported_indexes = [
        index
        for index, item in enumerate(attempts)
        if (
            item.state is CandidateAttemptState.UNKNOWN
            and item.usage.total_tokens == 0
        )
    ]
    if residual.total_tokens == 0:
        return attempts
    if len(unreported_indexes) != 1:
        raise _unattributed_usage_error(
            "聚合 Token 用量无法归属到唯一 attempt",
            reason=(
                CandidateUnattributedUsageReason.AGGREGATE_RESIDUAL_UNATTRIBUTED
            ),
            aggregate=aggregate,
            attempts=attempts,
            evidence_kind=aggregate_evidence_kind,
        )
    target = unreported_indexes[0]
    projected = list(attempts)
    projected[target] = projected[target].model_copy(
        update={"usage": residual}
    )
    return tuple(projected)


def _project_result_evidence(
    result: ChapterGenerationResult,
) -> tuple[CandidateUsageSummary, tuple[CandidateAttemptSummary, ...]]:
    try:
        aggregate, aggregate_error = _project_aggregate_usage(result.usage)
        attempts = _project_attempt_batch_with_aggregate(
            result.attempts,
            aggregate,
            aggregate_evidence_kind=_aggregate_usage_evidence_kind(
                aggregate_error
            ),
        )
        if aggregate_error is not None and (
            not attempts
            or isinstance(
                aggregate_error,
                _IncompleteAggregateUsageProjectionError,
            )
        ):
            raise _invalid_aggregate_error(
                aggregate_error,
                aggregate_floor=aggregate,
                attempts=attempts,
            ) from aggregate_error
        attempts = _attribute_aggregate_usage(
            aggregate,
            attempts,
            aggregate_evidence_kind=_aggregate_usage_evidence_kind(
                aggregate_error
            ),
        )
        if aggregate_error is not None:
            raise _invalid_aggregate_error(
                aggregate_error,
                aggregate_floor=aggregate,
                attempts=attempts,
            ) from aggregate_error
        return _effective_usage(aggregate, attempts), attempts
    except _UsageProjectionOverflow as overflow:
        raise _usage_overflow_error(str(overflow)) from overflow


def _sanitize_generation_result(
    result: ChapterGenerationResult,
) -> ChapterGenerationResult:
    usage, attempts = _project_result_evidence(result)
    truncated_section_count, dropped_item_count = _truncation_counts(
        result.truncation
    )
    return result.model_copy(
        update={
            "usage": usage.model_dump(mode="json"),
            "attempts": [
                item.model_dump(mode="json") for item in attempts
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


class StateCandidateRepairRequest(StateRepairDirective):
    schema_version: Literal["state_candidate_repair_request.v1"] = (
        STATE_REPAIR_REQUEST_SCHEMA
    )
    proposal_id: str = Field(min_length=1, max_length=128)
    source_run_id: str = Field(min_length=1, max_length=128)
    source_run_revision: int = Field(ge=0)
    source_content_digest: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )


class StateCandidateRepairReceipt(_RepairReceipt):
    schema_version: Literal["state_candidate_repair_receipt.v1"] = (
        STATE_REPAIR_RECEIPT_SCHEMA
    )
    request_id: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )


@dataclass(frozen=True)
class ChapterCandidatePipelineProgress:
    """Metadata-only evidence retained when a candidate pipeline stops."""

    tokens: int = 0
    attempts: tuple[CandidateAttemptSummary, ...] = ()
    unattributed_usage: tuple[CandidateUnattributedUsageSummary, ...] = ()
    truncations: tuple[CandidateTruncationSummary, ...] = ()
    completed_steps: tuple[str, ...] = ()
    repair_cycles_used: int = 0
    repair_component_usage: tuple[RepairComponentUsageV1, ...] = ()
    repair_convergence: tuple[RepairConvergenceEvidenceV1, ...] = ()
    prose_run_id: str | None = None
    prose_run_revision: int | None = None
    prose_content_digest: str | None = None
    state_proposal_id: str | None = None


@dataclass(frozen=True)
class ChapterCandidatePipelineResume:
    """Owner-scoped live candidates reconstructed from persisted checkpoints."""

    progress: ChapterCandidatePipelineProgress
    checkpoints: tuple[CandidatePipelineCheckpointV1, ...]
    source: ProseCandidateSource
    adherence: ChapterGenerationResult | None = None
    state: ChapterGenerationResult | None = None


def _exception_usage_projection(
    progress: ChapterCandidatePipelineProgress,
) -> dict[str, int | str]:
    evidence_kind = _merge_usage_evidence_kind(
        *(
            item.evidence_kind
            for item in progress.unattributed_usage
        )
    )
    return {
        "total_tokens": progress.tokens,
        "usage_evidence_kind": evidence_kind.value,
    }


def _exception_unattributed_usage_projection(
    progress: ChapterCandidatePipelineProgress,
) -> list[dict[str, Any]]:
    return [
        item.model_dump(mode="json")
        for item in progress.unattributed_usage
    ]


class ChapterCandidatePipelineBlocked(ValueError):
    """A candidate gate failed before the formal chapter commit."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "candidate_gate_blocked",
        progress: ChapterCandidatePipelineProgress | None = None,
        gate: Literal["completion", "outline_adherence", "state"] | None = None,
        repair_limit: int | None = None,
        repair_component: RepairComponent | None = None,
        component_used: int | None = None,
        component_limit: int | None = None,
        next_step: str | None = None,
        consistency_issue_count: int | None = None,
        dropped_reference_count: int | None = None,
        affected_card_ids: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.progress = progress or ChapterCandidatePipelineProgress()
        self._progress_attached = progress is not None
        self.gate = gate
        self.repair_limit = repair_limit
        self.repair_component = repair_component
        self.component_used = component_used
        self.component_limit = component_limit
        self.next_step = next_step
        self.consistency_issue_count = consistency_issue_count
        self.dropped_reference_count = dropped_reference_count
        self.affected_card_ids = affected_card_ids

    def attach_progress(self, progress: ChapterCandidatePipelineProgress) -> None:
        self.progress = progress
        self._progress_attached = True

    @property
    def has_progress(self) -> bool:
        return self._progress_attached

    @property
    def attempts(self) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in self.progress.attempts]

    @property
    def usage(self) -> dict[str, int | str]:
        return _exception_usage_projection(self.progress)

    @property
    def unattributed_usage(self) -> list[dict[str, Any]]:
        return _exception_unattributed_usage_projection(self.progress)


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
    def usage(self) -> dict[str, int | str]:
        return _exception_usage_projection(self.progress)

    @property
    def unattributed_usage(self) -> list[dict[str, Any]]:
        return _exception_unattributed_usage_projection(self.progress)


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
        ...,
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
    persist_checkpoint: Callable[
        [CandidatePipelineCheckpointV1],
        Awaitable[None],
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
    repair_component_usage: tuple[RepairComponentUsageV1, ...]
    repair_convergence: tuple[RepairConvergenceEvidenceV1, ...]
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


@dataclass(frozen=True)
class _RecordedStepEvidence:
    attempt_ids: tuple[str, ...]
    truncation: CandidateTruncationProjectionV1


def _checkpoint_source(
    source: ProseCandidateSource,
) -> CandidateSourceIdentityV1:
    return CandidateSourceIdentityV1(
        schema_version="candidate_source_identity.v1",
        source_run_id=source.source_run_id,
        source_run_revision=source.source_run_revision,
        source_content_digest=source.source_content_digest,
    )


def _checkpoint_id(
    checkpoint: CandidatePipelineCheckpointV1,
) -> str:
    return candidate_pipeline_checkpoint_digest(
        checkpoint,
        include_checkpoint_id=False,
    )


def _seal_checkpoint(
    checkpoint: CandidatePipelineCheckpointV1,
) -> CandidatePipelineCheckpointV1:
    return parse_candidate_pipeline_checkpoint(checkpoint.model_copy(
        update={"checkpoint_id": _checkpoint_id(checkpoint)}
    ))


def _checkpoint_common(
    *,
    chapter_id: str,
    sequence: int,
    source: ProseCandidateSource,
    evidence: _RecordedStepEvidence,
) -> dict[str, Any]:
    return {
        "schema_version": "chapter_candidate_pipeline_checkpoint.v2",
        "checkpoint_id": "0" * 64,
        "sequence": sequence,
        "chapter_id": chapter_id,
        "source": _checkpoint_source(source),
        "attempt_ids": evidence.attempt_ids,
        "truncation": evidence.truncation,
    }


def _prose_checkpoint(
    *,
    chapter_id: str,
    sequence: int,
    source: ProseCandidateSource,
    evidence: _RecordedStepEvidence,
    cycle: int,
    origin: Literal["initial", "repair"],
) -> ProseCandidateCheckpointV1:
    completion = source.completion
    return _seal_checkpoint(ProseCandidateCheckpointV1(
        **_checkpoint_common(
            chapter_id=chapter_id,
            sequence=sequence,
            source=source,
            evidence=evidence,
        ),
        cycle=cycle,
        origin=origin,
        completion=CandidateCompletionProjectionV1(
            schema_version="candidate_completion_projection.v1",
            status=completion.get("status"),
            can_write_formal_prose=completion.get(
                "can_write_formal_prose"
            ),
            finish_reason=completion.get("finish_reason"),
        ),
    ))


def _adherence_checkpoint_projection(
    adherence: Mapping[str, Any],
) -> tuple[
    str,
    tuple[OutlineIssueCategory, ...],
    tuple[str, ...],
    tuple[CandidateSceneCoverageV1, ...],
]:
    uses_local_policy = adherence.get("evidence_schema_version") in (
        _LOCAL_POLICY_EVIDENCE_VERSIONS
    )
    policy_result = adherence.get(
        "decision" if uses_local_policy else "verdict"
    )
    valid_results = (
        {"pass", "repair", "manual_review"}
        if uses_local_policy
        else {"pass", "warn", "fail"}
    )
    if policy_result not in valid_results:
        raise ChapterCandidatePipelineBlocked(
            "章纲符合度本地裁决无效"
            if uses_local_policy
            else "章纲符合度 verdict 无效"
        )
    raw_issues = adherence.get(
        "local_issues" if uses_local_policy else "issues"
    )
    raw_coverage = adherence.get("scene_coverage")
    issue_limit = (
        MAX_V3_LOCAL_ADHERENCE_ISSUES
        if uses_local_policy
        else MAX_V2_ADHERENCE_ISSUES
    )
    if (
        not isinstance(raw_issues, list)
        or not isinstance(raw_coverage, list)
        or len(raw_issues) > issue_limit
        or len(raw_coverage) > _MAX_RESUMED_ADHERENCE_COVERAGE
    ):
        raise ChapterCandidatePipelineBlocked(
            "章纲符合度持久投影无效"
        )
    categories: list[OutlineIssueCategory] = []
    blocking_signatures: list[str] = []
    for item in raw_issues:
        if not isinstance(item, Mapping):
            raise ChapterCandidatePipelineBlocked(
                "章纲符合度问题投影无效"
            )
        severity = item.get("severity")
        if uses_local_policy and severity not in {"blocker", "major", "unknown"}:
            continue
        category = item.get("category")
        if category not in _OUTLINE_ISSUE_CATEGORIES:
            raise ChapterCandidatePipelineBlocked(
                "章纲符合度问题类别无效"
            )
        if category not in categories:
            categories.append(category)
        if uses_local_policy:
            signature = item.get("issue_signature")
            if not isinstance(signature, str):
                raise ChapterCandidatePipelineBlocked(
                    "章纲符合度问题签名无效"
                )
            blocking_signatures.append(signature)
    coverage: list[CandidateSceneCoverageV1] = []
    for item in raw_coverage:
        if not isinstance(item, Mapping):
            raise ChapterCandidatePipelineBlocked(
                "章纲符合度场景投影无效"
            )
        coverage.append(CandidateSceneCoverageV1(
            schema_version="candidate_scene_coverage.v1",
            scene_index=item.get("scene_index"),
            status=item.get("status"),
        ))
    return (
        str(policy_result),
        tuple(categories),
        tuple(blocking_signatures),
        tuple(coverage),
    )


def _adherence_checkpoint(
    *,
    chapter_id: str,
    sequence: int,
    source: ProseCandidateSource,
    evidence: _RecordedStepEvidence,
    cycle: int,
    adherence: Mapping[str, Any],
) -> AdherenceCandidateCheckpoint:
    policy_result, categories, blocking_signatures, coverage = (
        _adherence_checkpoint_projection(adherence)
    )
    evidence_version = adherence.get("evidence_schema_version")
    validated_evidence_v2 = None
    validated_evidence_v3 = None
    validated_evidence_v4 = None
    if evidence_version is not None:
        if evidence_version not in {
            LEGACY_OUTLINE_ADHERENCE_EVIDENCE_VERSION,
            LEGACY_LOCAL_OUTLINE_ADHERENCE_EVIDENCE_VERSION,
            OUTLINE_ADHERENCE_EVIDENCE_VERSION,
        }:
            raise ChapterCandidatePipelineBlocked(
                "章纲符合度检查点证据版本无效"
            )
        try:
            if evidence_version == OUTLINE_ADHERENCE_EVIDENCE_VERSION:
                validated_evidence_v4 = (
                    ValidatedChapterOutlineAdherenceEvidenceV4Schema.model_validate(
                        dict(adherence)
                    )
                )
            elif (
                evidence_version
                == LEGACY_LOCAL_OUTLINE_ADHERENCE_EVIDENCE_VERSION
            ):
                validated_evidence_v3 = (
                    ValidatedChapterOutlineAdherenceEvidenceV3Schema.model_validate(
                        dict(adherence)
                    )
                )
            else:
                validated_evidence_v2 = (
                    ValidatedChapterOutlineAdherenceEvidenceSchema.model_validate(
                        dict(adherence)
                    )
                )
        except ValueError as exc:
            raise ChapterCandidatePipelineBlocked(
                "章纲符合度检查点证据无效"
            ) from exc
    common = _checkpoint_common(
        chapter_id=chapter_id,
        sequence=sequence,
        source=source,
        evidence=evidence,
    )
    try:
        if validated_evidence_v4 is not None:
            checkpoint = AdherenceCandidateCheckpointV5(
                **{
                    **common,
                    "schema_version": "chapter_candidate_pipeline_checkpoint.v5",
                },
                cycle=cycle,
                decision=policy_result,
                issue_categories=categories,
                blocking_issue_signatures=blocking_signatures,
                scene_coverage=coverage,
                validated_evidence=validated_evidence_v4,
            )
        elif validated_evidence_v3 is not None:
            checkpoint = AdherenceCandidateCheckpointV4(
                **{
                    **common,
                    "schema_version": "chapter_candidate_pipeline_checkpoint.v4",
                },
                cycle=cycle,
                decision=policy_result,
                issue_categories=categories,
                blocking_issue_signatures=blocking_signatures,
                scene_coverage=coverage,
                validated_evidence=validated_evidence_v3,
            )
        elif validated_evidence_v2 is not None:
            checkpoint = AdherenceCandidateCheckpointV3(
                **{
                    **common,
                    "schema_version": "chapter_candidate_pipeline_checkpoint.v3",
                },
                cycle=cycle,
                verdict=policy_result,
                issue_categories=categories,
                scene_coverage=coverage,
                validated_evidence=validated_evidence_v2,
            )
        else:
            checkpoint = AdherenceCandidateCheckpointV1(
                **common,
                cycle=cycle,
                verdict=policy_result,
                issue_categories=categories,
                scene_coverage=coverage,
            )
    except ValueError as exc:
        raise ChapterCandidatePipelineBlocked(
            "章纲符合度检查点投影无效"
        ) from exc
    return _seal_checkpoint(checkpoint)


def _state_checkpoint(
    *,
    chapter_id: str,
    sequence: int,
    source: ProseCandidateSource,
    evidence: _RecordedStepEvidence,
    cycle: int,
    origin: Literal["initial", "repair"],
    request_id: str,
    proposal_id: str,
    consistency_issue_count: int,
    dropped_reference_count: int,
    fact_accounting: Mapping[str, Any],
) -> StateCandidateCheckpointV3:
    common = _checkpoint_common(
        chapter_id=chapter_id,
        sequence=sequence,
        source=source,
        evidence=evidence,
    )
    return _seal_checkpoint(StateCandidateCheckpointV3(
        **{
            **common,
            "schema_version": "chapter_candidate_pipeline_checkpoint.v3",
        },
        cycle=cycle,
        origin=origin,
        request_id=request_id,
        proposal_id=proposal_id,
        consistency_issue_count=consistency_issue_count,
        dropped_reference_count=dropped_reference_count,
        fact_accounting_digest=fact_accounting.get("accounting_digest"),
        unaccounted_canonical_fact_count=fact_accounting.get(
            "unaccounted_canonical_facts"
        ),
        invalid_internal_reference_count=fact_accounting.get(
            "invalid_internal_references"
        ),
        dangling_reference_count=fact_accounting.get(
            "dangling_references"
        ),
        extraction_failure_count=fact_accounting.get(
            "extraction_failure_count"
        ),
    ))


def _initial_state_request_id(
    *,
    chapter_id: str,
    source: ProseCandidateSource,
) -> str:
    identity = {
        "schema_version": "chapter_candidate_state_request.v1",
        "chapter_id": chapter_id,
        "source_run_id": source.source_run_id,
        "source_run_revision": source.source_run_revision,
        "source_content_digest": source.source_content_digest,
    }
    return hashlib.sha256(json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")).hexdigest()


@dataclass
class _PipelineTrace:
    tokens: int = 0
    attempts: list[CandidateAttemptSummary] = field(default_factory=list)
    unattributed_usage: list[CandidateUnattributedUsageSummary] = field(
        default_factory=list
    )
    truncations: list[CandidateTruncationSummary] = field(default_factory=list)
    completed_steps: list[str] = field(default_factory=list)
    repair_cycles_used: int = 0
    repair_component_usage: tuple[RepairComponentUsageV1, ...] = ()
    repair_convergence: tuple[RepairConvergenceEvidenceV1, ...] = ()
    source: ProseCandidateSource | None = None
    state_proposal_id: str | None = None

    def record(
        self,
        step: str,
        result: ChapterGenerationResult,
    ) -> _RecordedStepEvidence:
        attempt_start = len(self.attempts)
        truncation_start = len(self.truncations)
        try:
            usage, summaries = _project_result_evidence(result)
            conflict = self._cross_step_attempt_conflict(
                usage,
                summaries,
                evidence_kind=CandidateUsageEvidenceKind.EXACT,
            )
            if conflict is not None:
                raise conflict
            self._record_evidence(usage, summaries)
        except _EvidenceProjectionError as exc:
            if isinstance(exc, _UnattributedUsageProjectionError):
                conflict = self._cross_step_attempt_conflict(
                    exc.usage,
                    exc.attempts,
                    evidence_kind=exc.evidence_kind,
                )
                if conflict is not None:
                    exc = conflict
                self._record_unattributed_usage(exc)
            raise ChapterCandidatePipelineBlocked(
                f"候选管线调用证据无效：{exc}"
            ) from exc
        truncation = _truncation(step, result)
        if truncation is not None:
            if len(self.truncations) >= _MAX_PIPELINE_TRUNCATIONS:
                raise ChapterCandidatePipelineBlocked(
                    "候选管线截断证据超过 V1 上限"
                )
            self.truncations.append(truncation)
        self.completed_steps.append(step)
        recorded_truncation = (
            self.truncations[truncation_start]
            if len(self.truncations) > truncation_start
            else None
        )
        return _RecordedStepEvidence(
            attempt_ids=tuple(
                item.attempt_id
                for item in self.attempts[attempt_start:]
            ),
            truncation=CandidateTruncationProjectionV1(
                schema_version="candidate_truncation_projection.v1",
                truncated_section_count=(
                    recorded_truncation.truncated_section_count
                    if recorded_truncation is not None
                    else 0
                ),
                dropped_item_count=(
                    recorded_truncation.dropped_item_count
                    if recorded_truncation is not None
                    else 0
                ),
            ),
        )

    def record_failure(self, exc: Exception) -> None:
        outcome = getattr(exc, "outcome", None)
        try:
            raw_usage = getattr(exc, "usage", None)
            usage_mapping = _as_mapping(raw_usage)
            if not usage_mapping and outcome is not None:
                outcome_tokens = getattr(outcome, "tokens", None)
                if type(outcome_tokens) is int:
                    usage_mapping = {"total_tokens": outcome_tokens}
            aggregate, aggregate_error = _project_aggregate_usage(
                usage_mapping
            )
            aggregate_kind = _aggregate_usage_evidence_kind(
                aggregate_error
            )
            raw_attempts = getattr(exc, "attempts", None)
            if raw_attempts is None and outcome is not None:
                raw_attempts = getattr(outcome, "attempts", None)
            if raw_attempts is None:
                raw_attempt_values: tuple[Any, ...] = ()
            elif isinstance(raw_attempts, (list, tuple)):
                if len(raw_attempts) > _MAX_PIPELINE_ATTEMPTS:
                    raise _UnattributedUsageProjectionError(
                        "失败调用证据超过 V1 上限",
                        reason=(
                            CandidateUnattributedUsageReason.ATTEMPT_EVIDENCE_INVALID
                        ),
                        usage=aggregate,
                        attempts=(),
                        evidence_kind=_merge_usage_evidence_kind(
                            aggregate_kind,
                            CandidateUsageEvidenceKind.INCOMPLETE,
                        ),
                    )
                raw_attempt_values = tuple(raw_attempts)
            else:
                raise _UnattributedUsageProjectionError(
                    "失败调用证据格式无效",
                    reason=(
                        CandidateUnattributedUsageReason.ATTEMPT_EVIDENCE_INVALID
                    ),
                    usage=aggregate,
                    attempts=(),
                    evidence_kind=_merge_usage_evidence_kind(
                        aggregate_kind,
                        CandidateUsageEvidenceKind.INCOMPLETE,
                    ),
                )
            summaries = _project_attempt_batch_with_aggregate(
                raw_attempt_values,
                aggregate,
                aggregate_evidence_kind=aggregate_kind,
            )
            conflict = self._cross_step_attempt_conflict(
                _effective_usage(aggregate, summaries),
                summaries,
                evidence_kind=aggregate_kind,
            )
            if conflict is not None:
                raise conflict
            if aggregate_error is not None and (
                not summaries
                or isinstance(
                    aggregate_error,
                    _IncompleteAggregateUsageProjectionError,
                )
            ):
                raise _invalid_aggregate_error(
                    aggregate_error,
                    aggregate_floor=aggregate,
                    attempts=summaries,
                ) from aggregate_error
            summaries = _attribute_aggregate_usage(
                aggregate,
                summaries,
                aggregate_evidence_kind=aggregate_kind,
            )
            if aggregate_error is not None:
                raise _invalid_aggregate_error(
                    aggregate_error,
                    aggregate_floor=aggregate,
                    attempts=summaries,
                ) from aggregate_error
            usage = _effective_usage(aggregate, summaries)
            self._record_evidence(usage, summaries)
            raw_truncations = getattr(exc, "truncations", None)
            if raw_truncations is None and outcome is not None:
                raw_truncations = getattr(outcome, "truncations", None)
            if raw_truncations is None:
                raw_truncation_values: tuple[Any, ...] = ()
            elif isinstance(raw_truncations, (list, tuple)):
                if len(raw_truncations) > _MAX_PIPELINE_TRUNCATIONS:
                    raise _EvidenceProjectionError(
                        "失败截断证据超过 V1 上限"
                    )
                raw_truncation_values = tuple(raw_truncations)
            else:
                raise _EvidenceProjectionError("失败截断证据格式无效")
            projected_truncations: list[CandidateTruncationSummary] = []
            for raw in raw_truncation_values:
                truncated_count, dropped_count = _truncation_counts(raw)
                if not truncated_count and not dropped_count:
                    continue
                projected_truncations.append(
                    CandidateTruncationSummary(
                        step="dependency_failure",
                        truncated_section_count=truncated_count,
                        dropped_item_count=dropped_count,
                    )
                )
            if (
                len(self.truncations) + len(projected_truncations)
                > _MAX_PIPELINE_TRUNCATIONS
            ):
                raise _EvidenceProjectionError(
                    "候选管线累计截断证据超过 V1 上限"
                )
            self.truncations.extend(projected_truncations)
        except _UsageProjectionOverflow as overflow:
            projection_error = _usage_overflow_error(str(overflow))
            self._record_unattributed_usage(projection_error)
            raise ChapterCandidatePipelineBlocked(
                f"候选管线调用证据冲突或无效：{projection_error}"
            ) from overflow
        except _EvidenceProjectionError as projection_error:
            if isinstance(
                projection_error,
                _UnattributedUsageProjectionError,
            ):
                conflict = self._cross_step_attempt_conflict(
                    projection_error.usage,
                    projection_error.attempts,
                    evidence_kind=projection_error.evidence_kind,
                )
                if conflict is not None:
                    projection_error = conflict
                self._record_unattributed_usage(projection_error)
            raise ChapterCandidatePipelineBlocked(
                f"候选管线调用证据冲突或无效：{projection_error}"
            ) from projection_error

    def _record_unattributed_usage(
        self,
        error: _UnattributedUsageProjectionError,
    ) -> None:
        if (
            error.reason
            is CandidateUnattributedUsageReason.USAGE_PROJECTION_OVERFLOW
        ):
            self._record_usage_overflow()
            return
        known = {item.attempt_id: item for item in self.attempts}
        duplicates = tuple(
            item
            for item in error.attempts
            if known.get(item.attempt_id) == item
        )
        try:
            residual = _usage_residual(
                error.usage,
                _summed_usage(duplicates),
            )
        except _UsageProjectionOverflow:
            self._record_usage_overflow()
            return
        if (
            residual.total_tokens == 0
            and error.evidence_kind is CandidateUsageEvidenceKind.EXACT
        ):
            return
        if (
            len(self.unattributed_usage)
            >= _MAX_PIPELINE_UNATTRIBUTED_USAGE
        ):
            raise _EvidenceProjectionError(
                "候选管线未归属用量证据超过 V1 上限"
            )
        try:
            next_tokens = _checked_token_add(
                self.tokens,
                residual.total_tokens,
            )
        except _UsageProjectionOverflow:
            self._record_usage_overflow()
            return
        self.unattributed_usage.append(
            CandidateUnattributedUsageSummary(
                reason=error.reason,
                usage=residual,
                evidence_kind=error.evidence_kind,
            )
        )
        self.tokens = next_tokens

    def _record_usage_overflow(self) -> None:
        if any(
            item.reason
            is CandidateUnattributedUsageReason.USAGE_PROJECTION_OVERFLOW
            for item in self.unattributed_usage
        ):
            self.tokens = _MAX_TOKEN_COUNT
            return
        marker = CandidateUnattributedUsageSummary(
            reason=CandidateUnattributedUsageReason.USAGE_PROJECTION_OVERFLOW,
            usage=CandidateUsageSummary(total_tokens=_MAX_TOKEN_COUNT),
            evidence_kind=CandidateUsageEvidenceKind.LOWER_BOUND,
        )
        if len(self.unattributed_usage) >= _MAX_PIPELINE_UNATTRIBUTED_USAGE:
            self.unattributed_usage[-1] = marker
        else:
            self.unattributed_usage.append(marker)
        self.tokens = _MAX_TOKEN_COUNT

    def _cross_step_attempt_conflict(
        self,
        usage: CandidateUsageSummary,
        summaries: tuple[CandidateAttemptSummary, ...],
        *,
        evidence_kind: CandidateUsageEvidenceKind,
    ) -> _UnattributedUsageProjectionError | None:
        """Preserve a later paid step without reusing an earlier ledger ID."""
        known_ids = {item.attempt_id for item in self.attempts}
        reused = tuple(
            item for item in summaries if item.attempt_id in known_ids
        )
        if not reused:
            return None
        new = tuple(
            item for item in summaries if item.attempt_id not in known_ids
        )
        if len(self.attempts) + len(new) > _MAX_PIPELINE_ATTEMPTS:
            return _UnattributedUsageProjectionError(
                "付费步骤复用了先前步骤的 attempt_id，且新调用证据超过 V1 上限",
                reason=(
                    CandidateUnattributedUsageReason.ATTEMPT_LEDGER_CONFLICT
                ),
                usage=usage,
                attempts=(),
                evidence_kind=_merge_usage_evidence_kind(
                    evidence_kind,
                    CandidateUsageEvidenceKind.INCOMPLETE,
                ),
            )
        try:
            new_usage = _summed_usage(new)
            reused_usage = _summed_usage(reused)
            conflict_usage = _conservative_usage_max(
                reused_usage,
                _usage_residual(usage, new_usage),
            )
            next_tokens = _checked_token_add(
                self.tokens,
                new_usage.total_tokens,
            )
        except _UsageProjectionOverflow as overflow:
            return _usage_overflow_error(str(overflow))
        self.attempts.extend(new)
        self.tokens = next_tokens
        return _UnattributedUsageProjectionError(
            "付费步骤复用了先前步骤的 attempt_id",
            reason=CandidateUnattributedUsageReason.ATTEMPT_LEDGER_CONFLICT,
            usage=conflict_usage,
            attempts=(),
            evidence_kind=_merge_usage_evidence_kind(
                evidence_kind,
                CandidateUsageEvidenceKind.INCOMPLETE,
            ),
        )

    def _classify_attempts(
        self,
        summaries: tuple[CandidateAttemptSummary, ...],
    ) -> tuple[
        tuple[CandidateAttemptSummary, ...],
        tuple[CandidateAttemptSummary, ...],
    ]:
        known = {item.attempt_id: item for item in self.attempts}
        new: list[CandidateAttemptSummary] = []
        duplicates: list[CandidateAttemptSummary] = []
        conflicting: list[
            tuple[CandidateAttemptSummary, CandidateAttemptSummary]
        ] = []
        for summary in summaries:
            existing = known.get(summary.attempt_id)
            if existing is None:
                new.append(summary)
            elif existing == summary:
                duplicates.append(summary)
            else:
                conflicting.append((existing, summary))
        if conflicting:
            conflict_delta = CandidateUsageSummary()
            conflict_kind = CandidateUsageEvidenceKind.EXACT
            for old, current in conflicting:
                item_delta, item_kind = _usage_delta(
                    current.usage,
                    old.usage,
                )
                conflict_delta = _usage_add(conflict_delta, item_delta)
                conflict_kind = _merge_usage_evidence_kind(
                    conflict_kind,
                    item_kind,
                )
            unresolved_usage = _usage_add(
                _summed_usage(tuple(new)),
                conflict_delta,
            )
            raise _UnattributedUsageProjectionError(
                "同一 attempt_id 的持久调用证据冲突",
                reason=(
                    CandidateUnattributedUsageReason.ATTEMPT_LEDGER_CONFLICT
                ),
                usage=unresolved_usage,
                attempts=(),
                evidence_kind=conflict_kind,
            )
        return tuple(new), tuple(duplicates)

    def _record_evidence(
        self,
        usage: CandidateUsageSummary,
        summaries: tuple[CandidateAttemptSummary, ...],
    ) -> None:
        try:
            new, duplicates = self._classify_attempts(summaries)
            if len(self.attempts) + len(new) > _MAX_PIPELINE_ATTEMPTS:
                raise _UnattributedUsageProjectionError(
                    "候选管线调用证据超过 V1 上限",
                    reason=(
                        CandidateUnattributedUsageReason.ATTEMPT_LEDGER_CAPACITY_EXCEEDED
                    ),
                    usage=_summed_usage(new),
                    attempts=(),
                )
            additional = self._additional_tokens(usage, new, duplicates)
            next_tokens = _checked_token_add(self.tokens, additional)
        except _UsageProjectionOverflow as overflow:
            raise _usage_overflow_error(str(overflow)) from overflow
        self.attempts.extend(new)
        self.tokens = next_tokens

    @staticmethod
    def _additional_tokens(
        usage: CandidateUsageSummary,
        new: tuple[CandidateAttemptSummary, ...],
        duplicates: tuple[CandidateAttemptSummary, ...],
    ) -> int:
        new_tokens = _summed_usage(new).total_tokens
        duplicate_tokens = _summed_usage(duplicates).total_tokens
        accounted_tokens = _checked_token_add(
            new_tokens,
            duplicate_tokens,
        )
        if usage.total_tokens != accounted_tokens:
            raise _EvidenceProjectionError(
                "聚合 Token 用量无法归属到 attempt 账本"
            )
        return new_tokens

    def snapshot(self) -> ChapterCandidatePipelineProgress:
        source = self.source
        return ChapterCandidatePipelineProgress(
            tokens=self.tokens,
            attempts=tuple(self.attempts),
            unattributed_usage=tuple(self.unattributed_usage),
            truncations=tuple(self.truncations),
            completed_steps=tuple(self.completed_steps),
            repair_cycles_used=self.repair_cycles_used,
            repair_component_usage=self.repair_component_usage,
            repair_convergence=self.repair_convergence,
            prose_run_id=source.source_run_id if source is not None else None,
            prose_run_revision=(
                source.source_run_revision if source is not None else None
            ),
            prose_content_digest=(
                source.source_content_digest if source is not None else None
            ),
            state_proposal_id=self.state_proposal_id,
        )


@dataclass(frozen=True)
class _RestoredPipeline:
    trace: _PipelineTrace
    source: ProseCandidateSource
    adherence: ChapterGenerationResult | None
    state: ChapterGenerationResult | None
    review_count: int
    last_repair_kept_digest: bool
    repair_policy_replay: _RepairPolicyReplay


def _blocked_resume(
    trace: _PipelineTrace,
    message: str,
    *,
    code: str = "candidate_gate_blocked",
) -> ChapterCandidatePipelineBlocked:
    return ChapterCandidatePipelineBlocked(
        message,
        code=code,
        progress=trace.snapshot(),
    )


def _is_lower_hex(value: Any, *, length: int) -> bool:
    return bool(
        type(value) is str
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _checkpoint_source_key(
    checkpoint: CandidatePipelineCheckpointV1,
) -> tuple[str, int, str]:
    source = checkpoint.source
    return (
        source.source_run_id,
        source.source_run_revision,
        source.source_content_digest,
    )


def _runtime_source_key(
    source: ProseCandidateSource,
) -> tuple[str, int, str]:
    return (
        source.source_run_id,
        source.source_run_revision,
        source.source_content_digest,
    )


def _checkpoint_step_name(
    checkpoint: CandidatePipelineCheckpointV1,
    *,
    review_count: int,
) -> str:
    if isinstance(checkpoint, ProseCandidateCheckpointV1):
        return (
            "prose"
            if checkpoint.origin == "initial"
            else f"prose_repair_{checkpoint.cycle}"
        )
    if isinstance(
        checkpoint,
        (
            AdherenceCandidateCheckpointV1,
            AdherenceCandidateCheckpointV3,
            AdherenceCandidateCheckpointV4,
            AdherenceCandidateCheckpointV5,
        ),
    ):
        return (
            "outline_adherence"
            if review_count == 1
            else f"outline_adherence_recheck_{review_count}"
        )
    return (
        "state"
        if checkpoint.origin == "initial"
        else f"state_repair_{checkpoint.cycle}"
    )


def _validate_resumed_adherence_projection(
    reviewed: ChapterGenerationResult,
    *,
    checkpoint: AdherenceCandidateCheckpoint,
    source: ProseCandidateSource,
) -> None:
    if reviewed.stage is not ChapterGenerationStage.OUTLINE_ADHERENCE:
        raise ChapterCandidatePipelineBlocked("候选管线恢复复检结果无效")
    if not isinstance(reviewed.value, Mapping):
        raise ChapterCandidatePipelineBlocked("候选管线恢复复检结果无效")
    adherence = reviewed.value
    if not _adherence_matches_source(adherence, source):
        raise ChapterCandidatePipelineBlocked("候选管线恢复复检身份不一致")
    if isinstance(checkpoint, AdherenceCandidateCheckpointV5):
        try:
            restored_evidence = (
                ValidatedChapterOutlineAdherenceEvidenceV4Schema.model_validate(
                    dict(adherence)
                )
            )
        except ValueError as exc:
            raise ChapterCandidatePipelineBlocked(
                "候选管线恢复 V4 复检证据无效"
            ) from exc
        if restored_evidence != checkpoint.validated_evidence:
            raise ChapterCandidatePipelineBlocked(
                "候选管线恢复 V4 复检证据与检查点不一致"
            )
    elif isinstance(checkpoint, AdherenceCandidateCheckpointV4):
        try:
            restored_evidence = (
                ValidatedChapterOutlineAdherenceEvidenceV3Schema.model_validate(
                    dict(adherence)
                )
            )
        except ValueError as exc:
            raise ChapterCandidatePipelineBlocked(
                "候选管线恢复 V3 复检证据无效"
            ) from exc
        if restored_evidence != checkpoint.validated_evidence:
            raise ChapterCandidatePipelineBlocked(
                "候选管线恢复 V3 复检证据与检查点不一致"
            )
    elif isinstance(checkpoint, AdherenceCandidateCheckpointV3):
        try:
            restored_evidence = (
                ValidatedChapterOutlineAdherenceEvidenceSchema.model_validate(
                    dict(adherence)
                )
            )
        except ValueError as exc:
            raise ChapterCandidatePipelineBlocked(
                "候选管线恢复 V2 复检证据无效"
            ) from exc
        if restored_evidence != checkpoint.validated_evidence:
            raise ChapterCandidatePipelineBlocked(
                "候选管线恢复 V2 复检证据与检查点不一致"
            )
    try:
        policy_result, categories, signatures, coverage = (
            _adherence_checkpoint_projection(adherence)
        )
    except ChapterCandidatePipelineBlocked as exc:
        raise ChapterCandidatePipelineBlocked(
            "候选管线恢复复检结果无效"
        ) from exc
    expected_policy_result = (
        checkpoint.decision
        if isinstance(
            checkpoint,
            (AdherenceCandidateCheckpointV4, AdherenceCandidateCheckpointV5),
        )
        else checkpoint.verdict
    )
    expected_signatures = (
        checkpoint.blocking_issue_signatures
        if isinstance(
            checkpoint,
            (AdherenceCandidateCheckpointV4, AdherenceCandidateCheckpointV5),
        )
        else ()
    )
    if (
        policy_result != expected_policy_result
        or categories != checkpoint.issue_categories
        or signatures != expected_signatures
        or tuple((item.scene_index, item.status) for item in coverage)
        != tuple((item.scene_index, item.status) for item in checkpoint.scene_coverage)
    ):
        raise ChapterCandidatePipelineBlocked(
            "候选管线恢复复检投影与检查点不一致"
        )


def _validate_resumed_state_projection(
    state_result: ChapterGenerationResult,
    *,
    checkpoint: StateCandidateCheckpoint,
    source: ProseCandidateSource,
) -> None:
    _state, proposal_id, _acceptance_token, issues, accounting = (
        _validate_state_shape(
            state_result,
            source=source,
            chapter_id=checkpoint.chapter_id,
        )
    )
    if (
        proposal_id != checkpoint.proposal_id
        or len(issues) != checkpoint.consistency_issue_count
        or _dropped_reference_count(state_result.dropped)
        != checkpoint.dropped_reference_count
        or not isinstance(checkpoint, StateCandidateCheckpointV3)
            or accounting.get("accounting_digest")
            != checkpoint.fact_accounting_digest
        or accounting.get("unaccounted_canonical_facts")
        != checkpoint.unaccounted_canonical_fact_count
        or accounting.get("invalid_internal_references")
        != checkpoint.invalid_internal_reference_count
        or accounting.get("dangling_references")
        != checkpoint.dangling_reference_count
        or accounting.get("extraction_failure_count")
        != checkpoint.extraction_failure_count
    ):
        raise ChapterCandidatePipelineBlocked(
            "候选管线恢复状态投影与检查点不一致"
        )


def _refresh_restored_usage(trace: _PipelineTrace) -> None:
    usage = _summed_usage_values(tuple(
        item.usage
        for item in (*trace.attempts, *trace.unattributed_usage)
    ))
    trace.tokens = usage.total_tokens


def _resume_item_usage_floor(
    trace: _PipelineTrace,
    value: Any,
) -> CandidateUsageSummary:
    try:
        usage = (
            value.get("usage")
            if isinstance(value, Mapping)
            else getattr(value, "usage", None)
        )
        return _usage_component_floor(usage)
    except _UsageProjectionOverflow:
        trace._record_usage_overflow()
        return CandidateUsageSummary(total_tokens=_MAX_TOKEN_COUNT)


def _canonical_invalid_resume_attempt(
    value: Any,
    *,
    usage: CandidateUsageSummary,
) -> CandidateAttemptSummary | None:
    def field(name: str) -> Any:
        if isinstance(value, Mapping):
            return value.get(name)
        return getattr(value, name, None)

    attempt_id = field("attempt_id")
    if type(attempt_id) is not str or not 1 <= len(attempt_id) <= 128:
        return None
    provider_alias = field("provider_alias")
    if (
        type(provider_alias) is not str
        or not 1 <= len(provider_alias) <= 64
    ):
        provider_alias = "unreported"
    raw_phase = field("phase")
    try:
        phase = CandidateAttemptPhase(raw_phase)
    except (TypeError, ValueError):
        phase = CandidateAttemptPhase.UNKNOWN
    raw_state = field("state")
    try:
        state = CandidateAttemptState(raw_state)
    except (TypeError, ValueError):
        state = CandidateAttemptState.UNKNOWN
    return CandidateAttemptSummary(
        attempt_id=attempt_id,
        provider_alias=provider_alias,
        phase=phase,
        state=(
            CandidateAttemptState.UNCERTAIN
            if state is CandidateAttemptState.UNCERTAIN
            else CandidateAttemptState.UNKNOWN
        ),
        usage=usage,
    )


def _preserve_invalid_resume_attempt(
    trace: _PipelineTrace,
    groups: dict[str, list[CandidateAttemptSummary]],
    value: Any,
) -> None:
    usage = _resume_item_usage_floor(trace, value)
    representative = _canonical_invalid_resume_attempt(value, usage=usage)
    if representative is not None:
        groups.setdefault(representative.attempt_id, []).append(
            representative
        )
    _preserve_resume_aggregate_floor(
        trace,
        aggregate_tokens=None,
        reason=CandidateUnattributedUsageReason.ATTEMPT_EVIDENCE_INVALID,
        item_usage_floor=(
            CandidateUsageSummary()
            if representative is not None
            else usage
        ),
    )


def _append_resume_unattributed_usage(
    trace: _PipelineTrace,
    marker: CandidateUnattributedUsageSummary,
) -> None:
    if (
        marker.reason
        is CandidateUnattributedUsageReason.USAGE_PROJECTION_OVERFLOW
    ):
        trace._record_usage_overflow()
        return
    if (
        marker.usage == CandidateUsageSummary()
        and marker in trace.unattributed_usage
    ):
        return
    if len(trace.unattributed_usage) >= _MAX_PIPELINE_UNATTRIBUTED_USAGE:
        previous = trace.unattributed_usage[-1]
        try:
            trace.unattributed_usage[-1] = CandidateUnattributedUsageSummary(
                reason=marker.reason,
                usage=_usage_add(previous.usage, marker.usage),
                evidence_kind=_merge_usage_evidence_kind(
                    previous.evidence_kind,
                    marker.evidence_kind,
                ),
            )
        except _UsageProjectionOverflow:
            trace._record_usage_overflow()
            return
    else:
        trace.unattributed_usage.append(marker)
    try:
        _refresh_restored_usage(trace)
    except _UsageProjectionOverflow:
        trace._record_usage_overflow()


def _preserve_resume_aggregate_floor(
    trace: _PipelineTrace,
    *,
    aggregate_tokens: int | None,
    reason: CandidateUnattributedUsageReason,
    item_usage_floor: CandidateUsageSummary | None = None,
) -> None:
    if aggregate_tokens is None and item_usage_floor is None:
        return
    aggregate_residual = CandidateUsageSummary(total_tokens=max(
        0,
        (aggregate_tokens or 0) - trace.tokens,
    ))
    usage = _conservative_usage_max(
        aggregate_residual,
        item_usage_floor or CandidateUsageSummary(),
    )
    marker = CandidateUnattributedUsageSummary(
        reason=reason,
        usage=usage,
        evidence_kind=CandidateUsageEvidenceKind.INCOMPLETE,
    )
    _append_resume_unattributed_usage(trace, marker)


@dataclass(frozen=True)
class _CheckpointReplay:
    completed_steps: tuple[str, ...]
    attempt_ids: tuple[str, ...]
    truncations: tuple[CandidateTruncationSummary, ...]
    current_prose: ProseCandidateCheckpointV1
    previous_prose: ProseCandidateCheckpointV1 | None
    latest_adherence: AdherenceCandidateCheckpoint | None
    latest_state: StateCandidateCheckpointV1 | None
    phase: _ResumePhase
    repair_cycles_used: int
    review_count: int


def _prose_checkpoint_kept_digest(
    current: ProseCandidateCheckpointV1 | None,
    previous: ProseCandidateCheckpointV1 | None,
) -> bool:
    return bool(
        current is not None
        and current.origin == "repair"
        and previous is not None
        and current.source.source_content_digest
        == previous.source.source_content_digest
    )


def _project_replayed_checkpoint_prefix(
    trace: _PipelineTrace,
    checkpoints: tuple[CandidatePipelineCheckpointV1, ...],
) -> tuple[tuple[str, ...], tuple[CandidateTruncationSummary, ...]]:
    """Project a reducer-approved prefix without reinterpreting its gates."""

    completed_steps: list[str] = []
    truncations: list[CandidateTruncationSummary] = []
    review_count = 0
    repair_cycles_used = 0
    for checkpoint in checkpoints:
        if isinstance(
            checkpoint,
            (
                AdherenceCandidateCheckpointV1,
                AdherenceCandidateCheckpointV3,
                AdherenceCandidateCheckpointV4,
                AdherenceCandidateCheckpointV5,
            ),
        ):
            review_count += 1
        step = _checkpoint_step_name(
            checkpoint,
            review_count=review_count,
        )
        completed_steps.append(step)
        repair_cycles_used = max(repair_cycles_used, checkpoint.cycle)
        truncation = checkpoint.truncation
        if truncation.truncated_section_count or truncation.dropped_item_count:
            truncations.append(CandidateTruncationSummary(
                step=step,
                truncated_section_count=truncation.truncated_section_count,
                dropped_item_count=truncation.dropped_item_count,
            ))
    trace.completed_steps = list(completed_steps)
    trace.repair_cycles_used = repair_cycles_used
    return tuple(completed_steps), tuple(truncations)


def _replay_candidate_checkpoints(
    checkpoints: tuple[CandidatePipelineCheckpointV1, ...],
    *,
    trace: _PipelineTrace,
    repair_limit: int,
    chapter_id: str,
    chapter: Mapping[str, Any],
) -> _CheckpointReplay:
    outline = chapter.get("outline")
    scenes = outline.get("scenes") if isinstance(outline, Mapping) else None
    if not isinstance(scenes, list) or not scenes:
        raise _blocked_resume(trace, "候选管线恢复章节章纲无效")
    try:
        replay = replay_candidate_pipeline_checkpoints(
            checkpoints,
            chapter_id=chapter_id,
            expected_scene_count=len(scenes),
            max_repair_cycles=repair_limit,
        )
    except CandidatePipelineCheckpointConflict as exc:
        _project_replayed_checkpoint_prefix(
            trace,
            checkpoints[:exc.accepted_checkpoints],
        )
        raise _blocked_resume(
            trace,
            str(exc),
            code=exc.code,
        ) from exc

    completed_steps, expected_truncations = (
        _project_replayed_checkpoint_prefix(trace, checkpoints)
    )
    return _CheckpointReplay(
        completed_steps=completed_steps,
        attempt_ids=replay.attempt_ids,
        truncations=tuple(expected_truncations),
        current_prose=replay.current_prose,
        previous_prose=replay.previous_prose,
        latest_adherence=replay.latest_adherence,
        latest_state=replay.latest_state,
        phase=_ResumePhase(replay.phase),
        repair_cycles_used=replay.repair_cycles_used,
        review_count=replay.review_count,
    )


def _resume_trace(
    resume: ChapterCandidatePipelineResume,
    *,
    repair_limit: int,
    repair_budget_limits: RepairBudgetLimitsV1,
    tail_judge_retry_usage: int,
    tail_repair_attempt_ids: tuple[str, ...],
    chapter_id: str,
    chapter: Mapping[str, Any],
) -> _RestoredPipeline:
    if not isinstance(resume, ChapterCandidatePipelineResume):
        raise ChapterCandidatePipelineBlocked("候选管线恢复协议无效")
    progress = resume.progress
    if not isinstance(progress, ChapterCandidatePipelineProgress):
        raise ChapterCandidatePipelineBlocked("候选管线恢复进度无效")

    restored = _PipelineTrace()
    trusted_progress_tokens = (
        progress.tokens
        if (
            type(progress.tokens) is int
            and 0 <= progress.tokens <= _MAX_TOKEN_COUNT
        )
        else None
    )
    if (
        not isinstance(progress.attempts, tuple)
        or len(progress.attempts) > _MAX_PIPELINE_ATTEMPTS
    ):
        _preserve_resume_aggregate_floor(
            restored,
            aggregate_tokens=trusted_progress_tokens,
            reason=CandidateUnattributedUsageReason.ATTEMPT_EVIDENCE_INVALID,
        )
        raise _blocked_resume(restored, "候选管线恢复 attempt 无效")
    attempt_groups: dict[str, list[CandidateAttemptSummary]] = {}
    has_unresolved_uncertain_attempt = False
    attempt_error_message: str | None = None
    for item in progress.attempts:
        if not isinstance(item, CandidateAttemptSummary):
            _preserve_invalid_resume_attempt(
                restored,
                attempt_groups,
                item,
            )
            attempt_error_message = (
                attempt_error_message or "候选管线恢复 attempt 无效"
            )
            continue
        try:
            validated = CandidateAttemptSummary.model_validate(
                item.model_dump(mode="python")
            )
        except _UsageProjectionOverflow:
            _preserve_invalid_resume_attempt(
                restored,
                attempt_groups,
                item,
            )
            attempt_error_message = (
                attempt_error_message or "候选管线恢复 Token 超过 V1 上限"
            )
            continue
        except Exception:
            _preserve_invalid_resume_attempt(
                restored,
                attempt_groups,
                item,
            )
            attempt_error_message = (
                attempt_error_message or "候选管线恢复 attempt 无效"
            )
            continue
        attempt_groups.setdefault(validated.attempt_id, []).append(validated)

    for variants in attempt_groups.values():
        try:
            unique_variants: list[CandidateAttemptSummary] = []
            violations: list[
                tuple[str, CandidateUnattributedUsageReason]
            ] = []
            valid_variants: list[CandidateAttemptSummary] = []
            group_usage = CandidateUsageSummary()
            group_usage_overflow = False
            for variant in variants:
                if variant in unique_variants:
                    continue
                unique_variants.append(variant)
                if not group_usage_overflow:
                    try:
                        group_usage = _conservative_usage_max(
                            group_usage,
                            variant.usage,
                        )
                    except _UsageProjectionOverflow:
                        group_usage_overflow = True
                try:
                    violation = _attempt_usage_violation(variant)
                except _UsageProjectionOverflow:
                    group_usage_overflow = True
                    continue
                if violation is None:
                    valid_variants.append(variant)
                elif violation not in violations:
                    violations.append(violation)

            representative: CandidateAttemptSummary | None = None
            if group_usage_overflow:
                representative_template = next(
                    (
                        item
                        for item in unique_variants
                        if item.state is CandidateAttemptState.UNCERTAIN
                    ),
                    unique_variants[0],
                )
                representative = representative_template.model_copy(update={
                    "state": (
                        CandidateAttemptState.UNCERTAIN
                        if representative_template.state
                        is CandidateAttemptState.UNCERTAIN
                        else CandidateAttemptState.UNKNOWN
                    ),
                    "usage": CandidateUsageSummary(
                        total_tokens=_MAX_TOKEN_COUNT
                    ),
                })
                restored.attempts.append(representative)
                has_unresolved_uncertain_attempt = bool(
                    has_unresolved_uncertain_attempt
                    or representative.state is CandidateAttemptState.UNCERTAIN
                )
                restored._record_usage_overflow()
                for message, reason in violations:
                    _preserve_resume_aggregate_floor(
                        restored,
                        aggregate_tokens=None,
                        reason=reason,
                        item_usage_floor=CandidateUsageSummary(),
                    )
                    attempt_error_message = attempt_error_message or message
                if len(unique_variants) > 1:
                    _preserve_resume_aggregate_floor(
                        restored,
                        aggregate_tokens=None,
                        reason=(
                            CandidateUnattributedUsageReason.ATTEMPT_LEDGER_CONFLICT
                        ),
                        item_usage_floor=CandidateUsageSummary(),
                    )
                attempt_error_message = (
                    attempt_error_message
                    or "候选管线恢复 Token 超过 V1 上限"
                )
                continue
            representative_template = (
                valid_variants[0]
                if valid_variants
                else unique_variants[0]
            )
            has_uncertain_variant = any(
                item.state is CandidateAttemptState.UNCERTAIN
                for item in unique_variants
            )
            representative_state = representative_template.state
            if has_uncertain_variant:
                representative_state = CandidateAttemptState.UNCERTAIN
            representative = representative_template.model_copy(update={
                "state": representative_state,
                "usage": group_usage,
            })
            if (
                representative.state is not CandidateAttemptState.UNCERTAIN
                and _attempt_usage_violation(representative) is not None
            ):
                representative = representative.model_copy(
                    update={"state": CandidateAttemptState.UNKNOWN}
                )
            restored.attempts.append(representative)
            has_unresolved_uncertain_attempt = bool(
                has_unresolved_uncertain_attempt
                or representative.state is CandidateAttemptState.UNCERTAIN
            )

            for index, (message, reason) in enumerate(violations):
                _preserve_resume_aggregate_floor(
                    restored,
                    aggregate_tokens=None,
                    reason=reason,
                    item_usage_floor=(
                        group_usage
                        if representative is None and index == 0
                        else CandidateUsageSummary()
                    ),
                )
                attempt_error_message = attempt_error_message or message

            if len(unique_variants) > 1:
                conflict_usage = CandidateUsageSummary()
                _preserve_resume_aggregate_floor(
                    restored,
                    aggregate_tokens=None,
                    reason=(
                        CandidateUnattributedUsageReason.ATTEMPT_LEDGER_CONFLICT
                    ),
                    item_usage_floor=conflict_usage,
                )
                attempt_error_message = (
                    attempt_error_message or "候选管线恢复 attempt 重复"
                )

            _refresh_restored_usage(restored)
        except _UsageProjectionOverflow:
            restored._record_usage_overflow()
            attempt_error_message = (
                attempt_error_message or "候选管线恢复 Token 超过 V1 上限"
            )

    if (
        not isinstance(progress.unattributed_usage, tuple)
        or len(progress.unattributed_usage)
        > _MAX_PIPELINE_UNATTRIBUTED_USAGE
    ):
        _preserve_resume_aggregate_floor(
            restored,
            aggregate_tokens=trusted_progress_tokens,
            reason=CandidateUnattributedUsageReason.AGGREGATE_USAGE_INVALID,
        )
        raise _blocked_resume(restored, "候选管线恢复用量证据无效")
    unattributed_error_message: str | None = None
    for item in progress.unattributed_usage:
        if not isinstance(item, CandidateUnattributedUsageSummary):
            _preserve_resume_aggregate_floor(
                restored,
                aggregate_tokens=None,
                reason=(
                    CandidateUnattributedUsageReason.AGGREGATE_USAGE_INVALID
                ),
                item_usage_floor=_resume_item_usage_floor(restored, item),
            )
            unattributed_error_message = (
                unattributed_error_message or "候选管线恢复用量证据无效"
            )
            continue
        try:
            validated = CandidateUnattributedUsageSummary.model_validate(
                item.model_dump(mode="python")
            )
        except _UsageProjectionOverflow:
            restored._record_usage_overflow()
            unattributed_error_message = (
                unattributed_error_message
                or "候选管线恢复 Token 超过 V1 上限"
            )
            continue
        except Exception:
            _preserve_resume_aggregate_floor(
                restored,
                aggregate_tokens=None,
                reason=(
                    CandidateUnattributedUsageReason.AGGREGATE_USAGE_INVALID
                ),
                item_usage_floor=_resume_item_usage_floor(restored, item),
            )
            unattributed_error_message = (
                unattributed_error_message or "候选管线恢复用量证据无效"
            )
            continue
        if (
            validated.reason
            is CandidateUnattributedUsageReason.USAGE_PROJECTION_OVERFLOW
        ):
            if (
                validated.usage
                != CandidateUsageSummary(total_tokens=_MAX_TOKEN_COUNT)
                or validated.evidence_kind
                is not CandidateUsageEvidenceKind.LOWER_BOUND
            ):
                restored._record_usage_overflow()
                continue
        _append_resume_unattributed_usage(restored, validated)

    has_usage_overflow = any(
        item.reason
        is CandidateUnattributedUsageReason.USAGE_PROJECTION_OVERFLOW
        for item in restored.unattributed_usage
    )
    if trusted_progress_tokens is None:
        raise _blocked_resume(restored, "候选管线恢复 Token 无效")
    has_token_mismatch = bool(
        not has_usage_overflow
        and restored.tokens != trusted_progress_tokens
    )
    if has_token_mismatch and trusted_progress_tokens > restored.tokens:
        _preserve_resume_aggregate_floor(
            restored,
            aggregate_tokens=trusted_progress_tokens,
            reason=(
                CandidateUnattributedUsageReason.AGGREGATE_RESIDUAL_UNATTRIBUTED
            ),
        )
    if attempt_error_message is not None:
        raise _blocked_resume(restored, attempt_error_message)
    if unattributed_error_message is not None:
        raise _blocked_resume(restored, unattributed_error_message)
    if has_token_mismatch:
        raise _blocked_resume(
            restored,
            "候选管线恢复 Token 与调用证据不一致",
        )
    if (
        not isinstance(progress.truncations, tuple)
        or len(progress.truncations) > _MAX_PIPELINE_TRUNCATIONS
    ):
        raise _blocked_resume(restored, "候选管线恢复截断证据无效")
    for item in progress.truncations:
        if not isinstance(item, CandidateTruncationSummary):
            raise _blocked_resume(restored, "候选管线恢复截断证据无效")
        try:
            restored.truncations.append(
                CandidateTruncationSummary.model_validate(
                    item.model_dump(mode="python")
                )
            )
        except Exception as exc:
            raise _blocked_resume(
                restored,
                "候选管线恢复截断证据无效",
            ) from exc

    if (
        not _is_lower_hex(progress.prose_run_id, length=24)
        or type(progress.prose_run_revision) is not int
        or not 1 <= progress.prose_run_revision < 2**63
        or not _is_lower_hex(progress.prose_content_digest, length=64)
        or (
            progress.state_proposal_id is not None
            and not _is_lower_hex(progress.state_proposal_id, length=24)
        )
    ):
        raise _blocked_resume(restored, "候选管线恢复正文身份无效")

    raw_checkpoints = resume.checkpoints
    if (
        not isinstance(raw_checkpoints, tuple)
        or not raw_checkpoints
        or len(raw_checkpoints)
        > MAX_CHAPTER_CANDIDATE_PIPELINE_CHECKPOINTS
    ):
        raise _blocked_resume(restored, "候选管线恢复检查点无效")
    checkpoints: list[CandidatePipelineCheckpointV1] = []
    for raw_checkpoint in raw_checkpoints:
        try:
            checkpoints.append(
                parse_candidate_pipeline_checkpoint(raw_checkpoint)
            )
        except Exception as exc:
            raise _blocked_resume(
                restored,
                "候选管线恢复检查点无效",
            ) from exc

    try:
        source = _validate_prose_source(resume.source)
    except ChapterCandidatePipelineBlocked as exc:
        exc.attach_progress(restored.snapshot())
        raise
    if (
        progress.prose_run_id != source.source_run_id
        or progress.prose_run_revision != source.source_run_revision
        or progress.prose_content_digest != source.source_content_digest
    ):
        raise _blocked_resume(
            restored,
            "恢复检查点与正文候选身份不一致",
        )
    restored.source = source
    restored.state_proposal_id = progress.state_proposal_id

    replay = _replay_candidate_checkpoints(
        tuple(checkpoints),
        trace=restored,
        repair_limit=repair_limit,
        chapter_id=chapter_id,
        chapter=chapter,
    )
    current_prose = replay.current_prose
    previous_prose = replay.previous_prose
    latest_adherence = replay.latest_adherence
    latest_state = replay.latest_state
    phase = replay.phase
    repair_cycles_used = replay.repair_cycles_used
    review_count = replay.review_count
    repair_gate: Literal["outline_adherence", "state"] = (
        "state" if phase is _ResumePhase.STATE else "outline_adherence"
    )
    try:
        repair_policy_replay = _replay_repair_policy(
            tuple(checkpoints),
            limits=repair_budget_limits,
            attempts=tuple(restored.attempts),
            tail_judge_retry_usage=tail_judge_retry_usage,
        )
    except _RepairPolicyReplayBudgetExhausted as replay_exhausted:
        raise _repair_budget_blocked(
            replay_exhausted.exhausted,
            trace=restored,
            policy=replay_exhausted.policy,
            gate=repair_gate,
        ) from replay_exhausted
    _sync_repair_policy(restored, repair_policy_replay.policy)
    if replay.completed_steps != progress.completed_steps:
        raise _blocked_resume(restored, "候选管线恢复步骤与检查点不一致")
    restored_attempt_ids = tuple(
        item.attempt_id for item in restored.attempts
    )
    trailing_attempts = restored.attempts[len(replay.attempt_ids):]
    trailing_attempt_ids = tuple(
        item.attempt_id for item in trailing_attempts
    )
    if restored_attempt_ids[:len(replay.attempt_ids)] != replay.attempt_ids:
        raise _blocked_resume(restored, "候选管线恢复调用与检查点不一致")
    if tail_repair_attempt_ids:
        if trailing_attempt_ids != tail_repair_attempt_ids:
            raise _blocked_resume(
                restored,
                "候选管线恢复修复调用组与持久账本不一致",
            )
        observed_tail_judge_retries = sum(
            1
            for attempt in trailing_attempts
            if (
                attempt.phase in _JUDGE_OR_SCHEMA_RETRY_PHASES
                and attempt.state in _CHARGED_ATTEMPT_STATES
            )
        )
        if observed_tail_judge_retries != tail_judge_retry_usage:
            raise _blocked_resume(
                restored,
                "候选管线恢复修复调用组重试用量不一致",
            )
    elif tail_judge_retry_usage or len(trailing_attempts) > 1:
        raise _blocked_resume(restored, "候选管线恢复调用与检查点不一致")
    if (
        trailing_attempts
        and any(
            attempt.state is not CandidateAttemptState.UNCERTAIN
            for attempt in trailing_attempts
        )
    ):
        judge_component = RepairComponent.ADHERENCE_JUDGE_RETRY
        judge_used = repair_policy_replay.policy.used(judge_component)
        judge_limit = repair_budget_limits.limit_for(judge_component)
        if tail_judge_retry_usage and judge_used >= judge_limit:
            raise _repair_budget_blocked(
                RepairBudgetExhausted(
                    component=judge_component,
                    used=judge_used,
                    limit=judge_limit,
                ),
                trace=restored,
                policy=repair_policy_replay.policy,
                gate=repair_gate,
            )
        raise _blocked_resume(
            restored,
            "已结算候选调用缺少可恢复结果投影",
            code="candidate_result_projection_missing",
        )
    if replay.truncations != tuple(restored.truncations):
        raise _blocked_resume(restored, "候选管线恢复截断与检查点不一致")
    if (
        type(progress.repair_cycles_used) is not int
        or progress.repair_cycles_used != replay.repair_cycles_used
    ):
        raise _blocked_resume(restored, "候选管线恢复修复轮次与检查点不一致")

    if (
        _runtime_source_key(source) != _checkpoint_source_key(current_prose)
        or current_prose.completion.status
        != source.completion.get("status")
        or current_prose.completion.can_write_formal_prose
        is not source.completion.get("can_write_formal_prose")
        or current_prose.completion.finish_reason
        != source.completion.get("finish_reason")
    ):
        raise _blocked_resume(
            restored,
            "恢复检查点与正文候选身份不一致",
        )

    restored.completed_steps = list(replay.completed_steps)
    restored.repair_cycles_used = repair_cycles_used
    if has_usage_overflow:
        raise _blocked_resume(
            restored,
            "候选管线恢复 Token 超过 V1 上限",
        )
    if restored.unattributed_usage:
        raise _blocked_resume(
            restored,
            "候选管线恢复仍有未归属的 Token 证据",
        )
    if has_unresolved_uncertain_attempt:
        raise _blocked_resume(
            restored,
            "候选管线恢复仍有未决 uncertain attempt",
        )

    if (
        resume.adherence is not None
        and not isinstance(resume.adherence, ChapterGenerationResult)
    ):
        raise _blocked_resume(restored, "候选管线恢复复检结果无效")
    if resume.state is not None and not isinstance(
        resume.state,
        ChapterGenerationResult,
    ):
        raise _blocked_resume(restored, "候选管线恢复状态结果无效")

    if phase is _ResumePhase.PROSE:
        if (
            resume.adherence is not None
            or resume.state is not None
            or progress.state_proposal_id is not None
        ):
            raise _blocked_resume(restored, "候选管线恢复候选阶段无效")
    elif phase is _ResumePhase.ADHERENCE:
        if resume.adherence is None:
            raise _blocked_resume(restored, "候选管线恢复复检候选缺失")
        if (
            resume.state is not None
            or progress.state_proposal_id is not None
            or latest_adherence is None
        ):
            raise _blocked_resume(restored, "候选管线恢复候选阶段无效")
        try:
            _validate_resumed_adherence_projection(
                resume.adherence,
                checkpoint=latest_adherence,
                source=source,
            )
        except ChapterCandidatePipelineBlocked as exc:
            exc.attach_progress(restored.snapshot())
            raise
    else:
        if resume.state is None:
            raise _blocked_resume(restored, "候选管线恢复状态候选缺失")
        if resume.adherence is None or latest_adherence is None or latest_state is None:
            raise _blocked_resume(restored, "候选管线恢复状态缺少前置复检")
        try:
            _validate_resumed_adherence_projection(
                resume.adherence,
                checkpoint=latest_adherence,
                source=source,
            )
            _validate_resumed_state_projection(
                resume.state,
                checkpoint=latest_state,
                source=source,
            )
            _validate_resumed_state_prerequisite(
                resume.adherence,
                source=source,
                chapter=chapter,
            )
        except ChapterCandidatePipelineBlocked as exc:
            exc.attach_progress(restored.snapshot())
            raise
        if progress.state_proposal_id != latest_state.proposal_id:
            raise _blocked_resume(
                restored,
                "恢复检查点与状态候选身份不一致",
            )

    last_repair_kept_digest = _prose_checkpoint_kept_digest(
        current_prose,
        previous_prose,
    )
    return _RestoredPipeline(
        trace=restored,
        source=source,
        adherence=resume.adherence,
        state=resume.state,
        review_count=review_count,
        last_repair_kept_digest=last_repair_kept_digest,
        repair_policy_replay=repair_policy_replay,
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
    *,
    prose: str,
) -> dict[str, Any]:
    outline = chapter.get("outline")
    if not isinstance(outline, Mapping):
        raise ChapterCandidatePipelineBlocked("章节缺少有效章纲")
    try:
        return validate_complete_outline_adherence(
            adherence,
            outline=outline,
            prose=prose,
        )
    except OutlineAdherenceValidationError as exc:
        raise ChapterCandidatePipelineBlocked(str(exc)) from exc


def _validate_resumed_state_prerequisite(
    reviewed: ChapterGenerationResult,
    *,
    source: ProseCandidateSource,
    chapter: Mapping[str, Any],
) -> None:
    try:
        if reviewed.stage is not ChapterGenerationStage.OUTLINE_ADHERENCE:
            raise ChapterCandidatePipelineBlocked(
                "章纲符合度返回了错误阶段"
            )
        if not isinstance(reviewed.value, Mapping):
            raise ChapterCandidatePipelineBlocked(
                "章纲符合度不是有效映射"
            )
        adherence = reviewed.value
        if not _adherence_matches_source(adherence, source):
            raise ChapterCandidatePipelineBlocked(
                "章纲符合度没有绑定正文候选"
            )
        _validate_adherence_gate(
            adherence,
            chapter,
            prose=source.text,
        )
    except ChapterCandidatePipelineBlocked as exc:
        raise ChapterCandidatePipelineBlocked(
            "候选管线恢复状态的前置复检未通过"
        ) from exc


_SCENE_REPAIR_CATEGORIES = frozenset({
    OutlineIssueCategory.SCENE_COVERAGE.value,
    OutlineIssueCategory.SCENE_ORDER.value,
})


def _hard_repair_issues(
    adherence: Mapping[str, Any],
) -> tuple[RepairIssueV1, ...]:
    if adherence.get("evidence_schema_version") not in (
        _LOCAL_POLICY_EVIDENCE_VERSIONS
    ):
        return ()
    raw_issues = adherence.get("local_issues")
    if not isinstance(raw_issues, list):
        raise ChapterCandidatePipelineBlocked(
            "章纲符合度稳定问题集合无效"
        )
    issues: list[RepairIssueV1] = []
    for item in raw_issues:
        if not isinstance(item, Mapping):
            raise ChapterCandidatePipelineBlocked(
                "章纲符合度稳定问题集合无效"
            )
        severity = item.get("severity")
        if severity not in {"blocker", "major"}:
            continue
        try:
            issues.append(RepairIssueV1(
                issue_signature=item.get("issue_signature"),
                severity=severity,
            ))
        except ValueError as exc:
            raise ChapterCandidatePipelineBlocked(
                "章纲符合度稳定问题身份无效"
            ) from exc
    return tuple(issues)


def _checkpoint_hard_repair_issues(
    checkpoint: AdherenceCandidateCheckpointV4
    | AdherenceCandidateCheckpointV5,
) -> tuple[RepairIssueV1, ...]:
    return tuple(
        RepairIssueV1(
            issue_signature=item.issue_signature,
            severity=item.severity,
        )
        for item in checkpoint.validated_evidence.local_issues
        if item.severity in {"blocker", "major"}
    )


def _content_repair_component(
    adherence: Mapping[str, Any] | AdherenceCandidateCheckpoint | None,
) -> RepairComponent:
    if adherence is None:
        return RepairComponent.SCENE_REGENERATION
    if isinstance(
        adherence,
        (AdherenceCandidateCheckpointV4, AdherenceCandidateCheckpointV5),
    ):
        categories = set(adherence.issue_categories)
        missing_scene = any(
            item.status != "covered" for item in adherence.scene_coverage
        )
        legacy_unclassified_failure = False
    elif isinstance(
        adherence,
        (AdherenceCandidateCheckpointV1, AdherenceCandidateCheckpointV3),
    ):
        categories = set(adherence.issue_categories)
        missing_scene = any(
            item.status != "covered" for item in adherence.scene_coverage
        )
        legacy_unclassified_failure = bool(
            adherence.verdict != "pass" and not categories
        )
    else:
        raw_issues = adherence.get(
            "local_issues"
            if adherence.get("evidence_schema_version")
            in _LOCAL_POLICY_EVIDENCE_VERSIONS
            else "issues"
        )
        categories = {
            str(item.get("category") or "")
            for item in (raw_issues if isinstance(raw_issues, list) else [])
            if isinstance(item, Mapping)
            and item.get("severity") not in {"quality_debt", "info", "unknown"}
        }
        raw_coverage = adherence.get("scene_coverage")
        missing_scene = any(
            isinstance(item, Mapping) and item.get("status") != "covered"
            for item in (
                raw_coverage if isinstance(raw_coverage, list) else []
            )
        )
        legacy_unclassified_failure = bool(
            adherence.get("evidence_schema_version")
            not in _LOCAL_POLICY_EVIDENCE_VERSIONS
            and adherence.get("verdict") != "pass"
            and not categories
        )
    if (
        missing_scene
        or legacy_unclassified_failure
        or categories.intersection(_SCENE_REPAIR_CATEGORIES)
    ):
        return RepairComponent.SCENE_REGENERATION
    return RepairComponent.LOCAL_PROSE_REPAIR


@dataclass(frozen=True)
class _RepairPolicyReplay:
    policy: ChapterRepairPolicy
    pending_convergence_charge: RepairChargeV1 | None


@dataclass(frozen=True)
class ChapterRepairEvidenceReplay:
    """Durable metadata projection reconstructed from checkpoints and attempts."""

    component_usage: tuple[RepairComponentUsageV1, ...]
    convergence: tuple[RepairConvergenceEvidenceV1, ...]


class _RepairPolicyReplayBudgetExhausted(ValueError):
    def __init__(
        self,
        exhausted: RepairBudgetExhausted,
        policy: ChapterRepairPolicy,
    ) -> None:
        super().__init__(str(exhausted))
        self.exhausted = exhausted
        self.policy = policy


def _judge_or_schema_retry_count(
    attempt_ids: tuple[str, ...],
    attempts_by_id: Mapping[str, CandidateAttemptSummary],
) -> int:
    return sum(
        1
        for attempt_id in attempt_ids
        if (
            (attempt := attempts_by_id.get(attempt_id)) is not None
            and attempt.phase in _JUDGE_OR_SCHEMA_RETRY_PHASES
            and attempt.state in _CHARGED_ATTEMPT_STATES
        )
    )


def _replay_judge_or_schema_retries(
    policy: ChapterRepairPolicy,
    checkpoint: CandidatePipelineCheckpointV1,
    attempts_by_id: Mapping[str, CandidateAttemptSummary],
) -> None:
    for _index in range(_judge_or_schema_retry_count(
        checkpoint.attempt_ids,
        attempts_by_id,
    )):
        policy.authorize(RepairComponent.ADHERENCE_JUDGE_RETRY)


def _replay_repair_policy(
    checkpoints: list[CandidatePipelineCheckpointV1]
    | tuple[CandidatePipelineCheckpointV1, ...],
    *,
    limits: RepairBudgetLimitsV1,
    attempts: tuple[CandidateAttemptSummary, ...] = (),
    tail_judge_retry_usage: int = 0,
) -> _RepairPolicyReplay:
    if (
        type(tail_judge_retry_usage) is not int
        or tail_judge_retry_usage < 0
        or tail_judge_retry_usage > _MAX_PIPELINE_ATTEMPTS
    ):
        raise ValueError("tail Judge/schema retry usage is invalid")
    policy = ChapterRepairPolicy(limits)
    attempts_by_id = {attempt.attempt_id: attempt for attempt in attempts}
    phase: Literal["start", "prose", "adherence", "state"] = "start"
    latest_adherence: AdherenceCandidateCheckpoint | None = None
    pending: RepairChargeV1 | None = None
    try:
        for checkpoint in checkpoints:
            if isinstance(checkpoint, ProseCandidateCheckpointV1):
                if checkpoint.origin == "repair":
                    component = _content_repair_component(
                        latest_adherence if phase == "adherence" else None
                    )
                    charge = policy.authorize(component)
                    pending = (
                        charge
                        if phase == "adherence"
                        and isinstance(
                            latest_adherence,
                            (
                                AdherenceCandidateCheckpointV4,
                                AdherenceCandidateCheckpointV5,
                            ),
                        )
                        else None
                    )
                    _replay_judge_or_schema_retries(
                        policy,
                        checkpoint,
                        attempts_by_id,
                    )
                phase = "prose"
                continue
            if isinstance(
                checkpoint,
                (
                    AdherenceCandidateCheckpointV1,
                    AdherenceCandidateCheckpointV3,
                    AdherenceCandidateCheckpointV4,
                    AdherenceCandidateCheckpointV5,
                ),
            ):
                if checkpoint.cycle > 0:
                    _replay_judge_or_schema_retries(
                        policy,
                        checkpoint,
                        attempts_by_id,
                    )
                if isinstance(
                    checkpoint,
                    (
                        AdherenceCandidateCheckpointV4,
                        AdherenceCandidateCheckpointV5,
                    ),
                ):
                    issues = _checkpoint_hard_repair_issues(checkpoint)
                    if policy.observation_count == 0:
                        policy.observe_adherence(
                            issues,
                            prose_run_revision=(
                                checkpoint.source.source_run_revision
                            ),
                            content_digest=(
                                checkpoint.source.source_content_digest
                            ),
                        )
                    elif pending is not None:
                        policy.observe_adherence(
                            issues,
                            prose_run_revision=(
                                checkpoint.source.source_run_revision
                            ),
                            content_digest=(
                                checkpoint.source.source_content_digest
                            ),
                            charge=pending,
                        )
                    pending = None
                latest_adherence = checkpoint
                phase = "adherence"
                continue
            if checkpoint.origin == "repair":
                policy.authorize(RepairComponent.STATE_REEXTRACTION)
                _replay_judge_or_schema_retries(
                    policy,
                    checkpoint,
                    attempts_by_id,
                )
            phase = "state"
        for _index in range(tail_judge_retry_usage):
            policy.authorize(RepairComponent.ADHERENCE_JUDGE_RETRY)
    except RepairBudgetExhausted as exc:
        raise _RepairPolicyReplayBudgetExhausted(exc, policy) from exc
    return _RepairPolicyReplay(
        policy=policy,
        pending_convergence_charge=pending,
    )


def replay_chapter_repair_evidence(
    checkpoints: list[CandidatePipelineCheckpointV1]
    | tuple[CandidatePipelineCheckpointV1, ...],
    *,
    limits: RepairBudgetLimitsV1,
    attempts: tuple[CandidateAttemptSummary, ...] = (),
    tail_judge_retry_usage: int = 0,
) -> ChapterRepairEvidenceReplay:
    """Rebuild persisted component usage and convergence without prose content."""

    try:
        replay = _replay_repair_policy(
            checkpoints,
            limits=limits,
            attempts=attempts,
            tail_judge_retry_usage=tail_judge_retry_usage,
        )
    except _RepairPolicyReplayBudgetExhausted as exc:
        raise exc.exhausted from exc
    return ChapterRepairEvidenceReplay(
        component_usage=replay.policy.component_usage,
        convergence=replay.policy.transitions,
    )


def _sync_repair_policy(
    trace: _PipelineTrace,
    policy: ChapterRepairPolicy,
) -> None:
    trace.repair_component_usage = policy.component_usage
    trace.repair_convergence = policy.transitions


def _repair_budget_blocked(
    exhausted: RepairBudgetExhausted,
    *,
    trace: _PipelineTrace,
    policy: ChapterRepairPolicy,
    gate: Literal["completion", "outline_adherence", "state"],
    consistency_issue_count: int | None = None,
    dropped_reference_count: int | None = None,
    affected_card_ids: tuple[str, ...] = (),
) -> ChapterCandidatePipelineBlocked:
    _sync_repair_policy(trace, policy)
    return ChapterCandidatePipelineBlocked(
        "候选仍未通过闸门，对应组件的授权修复额度已耗尽",
        code="repair_budget_exhausted",
        progress=trace.snapshot(),
        gate=gate,
        repair_component=exhausted.component,
        component_used=exhausted.used,
        component_limit=exhausted.limit,
        next_step=exhausted.next_step,
        consistency_issue_count=consistency_issue_count,
        dropped_reference_count=dropped_reference_count,
        affected_card_ids=affected_card_ids,
    )


def _authorize_repair_component(
    policy: ChapterRepairPolicy,
    component: RepairComponent,
    *,
    trace: _PipelineTrace,
    gate: Literal["completion", "outline_adherence", "state"],
    consistency_issue_count: int | None = None,
    dropped_reference_count: int | None = None,
    affected_card_ids: tuple[str, ...] = (),
) -> RepairChargeV1:
    try:
        return policy.authorize(component)
    except RepairBudgetExhausted as exc:
        raise _repair_budget_blocked(
            exc,
            trace=trace,
            policy=policy,
            gate=gate,
            consistency_issue_count=consistency_issue_count,
            dropped_reference_count=dropped_reference_count,
            affected_card_ids=affected_card_ids,
        ) from exc


def _charge_judge_or_schema_retries(
    policy: ChapterRepairPolicy,
    evidence: _RecordedStepEvidence,
    *,
    trace: _PipelineTrace,
    gate: Literal["completion", "outline_adherence", "state"],
    consistency_issue_count: int | None = None,
    dropped_reference_count: int | None = None,
    affected_card_ids: tuple[str, ...] = (),
) -> None:
    attempts_by_id = {
        attempt.attempt_id: attempt
        for attempt in trace.attempts
    }
    for _index in range(_judge_or_schema_retry_count(
        evidence.attempt_ids,
        attempts_by_id,
    )):
        _authorize_repair_component(
            policy,
            RepairComponent.ADHERENCE_JUDGE_RETRY,
            trace=trace,
            gate=gate,
            consistency_issue_count=consistency_issue_count,
            dropped_reference_count=dropped_reference_count,
            affected_card_ids=affected_card_ids,
        )
    _sync_repair_policy(trace, policy)


def _strict_repair_limits(
    value: Any,
    supplied: RepairBudgetLimitsV1 | Mapping[str, Any] | None,
) -> tuple[RepairBudgetLimitsV1, int]:
    if (
        type(value) is not int
        or value < 0
        or value > MAX_CHAPTER_CANDIDATE_COMPONENT_REPAIRS
    ):
        raise ValueError(
            "max_repair_cycles must be an integer between 0 and "
            f"{MAX_CHAPTER_CANDIDATE_COMPONENT_REPAIRS}"
        )
    limits = (
        default_repair_budget_limits(value)
        if supplied is None
        else RepairBudgetLimitsV1.model_validate(
            supplied.model_dump(mode="python")
            if isinstance(supplied, RepairBudgetLimitsV1)
            else supplied
        )
    )
    for component in (
        RepairComponent.STATE_REEXTRACTION,
        RepairComponent.LOCAL_PROSE_REPAIR,
        RepairComponent.SCENE_REGENERATION,
        RepairComponent.OUTLINE_ROLLBACK,
    ):
        if limits.limit_for(component) > value:
            raise ValueError(
                "repair component limit exceeds max_repair_cycles"
            )
    event_limit = sum(
        limits.limit_for(component)
        for component in (
            RepairComponent.STATE_REEXTRACTION,
            RepairComponent.LOCAL_PROSE_REPAIR,
            RepairComponent.SCENE_REGENERATION,
            RepairComponent.OUTLINE_ROLLBACK,
        )
    )
    if event_limit > MAX_FINALIZATION_REPAIR_CYCLES:
        raise ValueError("repair event limit exceeds finalization authority")
    return limits, event_limit


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
    current_policy = adherence.get("evidence_schema_version") == (
        OUTLINE_ADHERENCE_EVIDENCE_VERSION
    )
    issues = adherence.get("local_issues" if current_policy else "issues")
    if isinstance(issues, list):
        for item in issues:
            if not isinstance(item, Mapping):
                continue
            if current_policy and item.get("severity") not in {
                "blocker",
                "major",
            }:
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
    stack: list[tuple[Any, int]] = [(value, 0)]
    visited_nodes = 0
    total = 0
    while stack:
        current, depth = stack.pop()
        visited_nodes += 1
        if (
            visited_nodes > _MAX_DROPPED_PROJECTION_NODES
            or depth > _MAX_DROPPED_PROJECTION_DEPTH
        ):
            raise ChapterCandidatePipelineBlocked(
                "状态丢弃引用投影超过 V1 有界深度或节点数"
            )
        if isinstance(current, Mapping):
            if len(current) > _MAX_DROPPED_PROJECTION_MAPPING_KEYS:
                raise ChapterCandidatePipelineBlocked(
                    "状态丢弃引用投影超过 V1 有界字段数"
                )
            if "dropped_reference_count" in current:
                exact_count = current.get("dropped_reference_count")
                if type(exact_count) is not int or exact_count < 0:
                    raise ChapterCandidatePipelineBlocked(
                        "状态丢弃引用投影无效"
                    )
                total += min(
                    MAX_STATE_REPAIR_DROPPED_REFERENCES,
                    exact_count,
                )
            else:
                stack.extend(
                    (item, depth + 1)
                    for item in current.values()
                )
        elif isinstance(current, (list, tuple, set, frozenset)):
            total += min(MAX_STATE_REPAIR_DROPPED_REFERENCES, len(current))
        else:
            total += int(bool(current))
        if total >= MAX_STATE_REPAIR_DROPPED_REFERENCES:
            return MAX_STATE_REPAIR_DROPPED_REFERENCES
    return total


def _state_repair_request(
    *,
    cycle: int,
    proposal_id: str,
    source: ProseCandidateSource,
    declared_card_ids: frozenset[str],
    state: Mapping[str, Any],
    dropped: Mapping[str, Any],
    fact_accounting: Mapping[str, Any],
) -> StateCandidateRepairRequest:
    raw_issues = state.get("consistency_issues")
    issues = raw_issues if isinstance(raw_issues, list) else []
    card_ids = _state_issue_card_ids(
        issues,
        declared_card_ids=declared_card_ids,
    )
    dropped_count = _dropped_reference_count(dropped)
    unaccounted_count = int(
        fact_accounting.get("unaccounted_canonical_facts") or 0
    )
    invalid_count = int(
        fact_accounting.get("invalid_internal_references") or 0
    )
    dangling_count = int(fact_accounting.get("dangling_references") or 0)
    extraction_failure_count = int(
        fact_accounting.get("extraction_failure_count") or 0
    )
    reason_codes: list[StateRepairReason] = []
    if issues:
        reason_codes.append("consistency_conflict")
    if dropped_count or invalid_count or dangling_count:
        reason_codes.append("invalid_internal_reference")
    if unaccounted_count:
        reason_codes.append("unaccounted_canonical_fact")
    if extraction_failure_count:
        reason_codes.append("state_extraction_unknown")
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
        unaccounted_canonical_fact_count=unaccounted_count,
        invalid_internal_reference_count=invalid_count,
        dangling_reference_count=dangling_count,
        extraction_failure_count=extraction_failure_count,
    )


def _state_issue_card_ids(
    issues: list[Any] | tuple[dict[str, Any], ...],
    *,
    declared_card_ids: frozenset[str],
) -> tuple[str, ...]:
    return tuple(sorted({
        card_id
        for item in issues[:MAX_STATE_REPAIR_CARD_IDS]
        if isinstance(item, Mapping)
        for card_id in (item.get("card_id"),)
        if (
            isinstance(card_id, str)
            and 0 < len(card_id) <= MAX_STATE_REPAIR_CARD_ID_LENGTH
            and card_id in declared_card_ids
        )
    }))


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
        for card_id in raw_ids[:MAX_STATE_REPAIR_CARD_IDS]
        if (
            isinstance(card_id, str)
            and 0 < len(card_id) <= MAX_STATE_REPAIR_CARD_ID_LENGTH
        )
    )


def _validate_prose_candidate(
    generated: GeneratedProseCandidate | ProseCandidateRepairReceipt,
) -> tuple[ChapterGenerationResult, ProseCandidateSource]:
    prose = generated.generation
    source = _validate_prose_source(generated.source)
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


def _validate_prose_source(
    source: ProseCandidateSource,
) -> ProseCandidateSource:
    if not isinstance(source, ProseCandidateSource):
        raise ChapterCandidatePipelineBlocked("正文候选来源投影无效")
    if (
        not isinstance(source.text, str)
        or not source.text
        or not isinstance(source.source_run_id, str)
        or not source.source_run_id
        or type(source.source_run_revision) is not int
        or source.source_run_revision < 0
        or not isinstance(source.source_content_digest, str)
        or len(source.source_content_digest) != 64
        or any(
            character not in "0123456789abcdef"
            for character in source.source_content_digest
        )
        or not isinstance(source.completion, Mapping)
    ):
        raise ChapterCandidatePipelineBlocked("正文候选来源投影无效")
    if chapter_content_digest(source.text) != source.source_content_digest:
        raise ChapterCandidatePipelineBlocked("正文候选摘要与正文不一致")
    completion = source.completion
    if (
        completion.get("source_run_id") != source.source_run_id
        or type(completion.get("source_run_revision")) is not int
        or completion.get("source_run_revision")
        != source.source_run_revision
        or completion.get("source_run_digest")
        != source.source_content_digest
    ):
        raise ChapterCandidatePipelineBlocked("正文候选来源身份不一致")
    return source


def _completion_passed(source: ProseCandidateSource) -> bool:
    completion = source.completion
    return completion_allows_formal_write(
        status=completion.get("status"),
        can_write_formal_prose=completion.get("can_write_formal_prose"),
        finish_reason=completion.get("finish_reason"),
    )


def _prose_repair_advanced_revision(
    previous: ProseCandidateSource,
    current: ProseCandidateSource,
) -> bool:
    return bool(
        current.source_run_id == previous.source_run_id
        and current.source_run_revision > previous.source_run_revision
    )


def _next_repair_cycle(
    used: int,
    limit: int,
    *,
    gate: Literal["completion", "outline_adherence", "state"],
    consistency_issue_count: int | None = None,
    dropped_reference_count: int | None = None,
    affected_card_ids: tuple[str, ...] = (),
) -> int:
    if used >= limit:
        raise ChapterCandidatePipelineBlocked(
            "候选仍未通过闸门，已达到授权的修复次数上限",
            code={
                "completion": "candidate_completion_repair_exhausted",
                "outline_adherence": "candidate_adherence_repair_exhausted",
                "state": "candidate_state_repair_exhausted",
            }[gate],
            gate=gate,
            repair_limit=limit,
            consistency_issue_count=consistency_issue_count,
            dropped_reference_count=dropped_reference_count,
            affected_card_ids=affected_card_ids,
        )
    return used + 1


def _validate_state_shape(
    state_result: ChapterGenerationResult,
    *,
    source: ProseCandidateSource,
    chapter_id: str,
) -> tuple[
    dict[str, Any],
    str,
    str,
    tuple[dict[str, Any], ...],
    dict[str, Any],
]:
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
    if len(raw_issues) > MAX_STATE_REPAIR_CARD_IDS:
        raise ChapterCandidatePipelineBlocked("状态候选冲突数量超过 V1 上限")
    issues = tuple(dict(item) for item in raw_issues)
    raw_fact_evidence = state.get("fact_evidence")
    if not isinstance(raw_fact_evidence, Mapping):
        raise ChapterCandidatePipelineBlocked(
            "状态候选缺少正式事实核算证据"
        )
    try:
        policy_decision = automatic_state_fact_decision(
            raw_fact_evidence,
            candidate=state,
        )
    except StateFactAccountingError as exc:
        raise ChapterCandidatePipelineBlocked(str(exc)) from exc
    accounting = dict(policy_decision["fact_accounting"])
    source_binding = accounting.get("source_binding")
    if (
        not isinstance(source_binding, Mapping)
        or source_binding.get("chapter_id") != chapter_id
        or source_binding.get("source_prose_run_id")
        != source.source_run_id
        or source_binding.get("source_prose_run_revision")
        != source.source_run_revision
        or source_binding.get("source_content_digest")
        != source.source_content_digest
    ):
        raise ChapterCandidatePipelineBlocked(
            "状态事实核算没有绑定当前正文候选"
        )
    return state, proposal_id, acceptance_token, issues, accounting


class ChapterCandidatePipeline:
    """Hide the ordered candidate gates behind one safe orchestration interface."""

    def __init__(self, deps: ChapterCandidatePipelineDeps) -> None:
        if not callable(deps.persist_checkpoint):
            raise ValueError(
                "candidate checkpoint persistence is required"
            )
        self._deps = deps

    async def _persist_checkpoint(
        self,
        checkpoint: CandidatePipelineCheckpointV1,
        *,
        ledger: list[CandidatePipelineCheckpointV1],
    ) -> None:
        persist = self._deps.persist_checkpoint
        validated = parse_candidate_pipeline_checkpoint(checkpoint)
        await persist(validated)
        if any(
            existing.checkpoint_id == validated.checkpoint_id
            for existing in ledger
        ):
            if not any(existing == validated for existing in ledger):
                raise ChapterCandidatePipelineBlocked(
                    "候选管线检查点幂等重放分歧"
                )
            return
        ledger.append(validated)

    async def _apply_prose_repair(
        self,
        *,
        owner_id: str | None,
        novel_id: str,
        chapter_id: str,
        source: ProseCandidateSource,
        request: ProseCandidateRepairRequest,
        trace: _PipelineTrace,
        repair_policy: ChapterRepairPolicy,
        checkpoint_ledger: list[CandidatePipelineCheckpointV1],
    ) -> tuple[ProseCandidateSource, bool]:
        repair = self._deps.repair_prose_candidate
        if repair is None:
            raise ChapterCandidatePipelineBlocked("正文候选没有授权修复入口")
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
        evidence = trace.record(
            f"prose_repair_{request.cycle}",
            receipt.generation,
        )
        _repaired_prose, repaired_source = _validate_prose_candidate(receipt)
        trace.source = repaired_source
        await self._persist_checkpoint(_prose_checkpoint(
            chapter_id=chapter_id,
            sequence=len(trace.completed_steps),
            source=repaired_source,
            evidence=evidence,
            cycle=request.cycle,
            origin="repair",
        ), ledger=checkpoint_ledger)
        trace.repair_cycles_used = request.cycle
        _charge_judge_or_schema_retries(
            repair_policy,
            evidence,
            trace=trace,
            gate=request.trigger,
        )
        if not _prose_repair_advanced_revision(source, repaired_source):
            raise ChapterCandidatePipelineBlocked(
                "正文修复没有产生新候选",
                code="repair_no_progress",
            )
        same_digest = (
            repaired_source.source_content_digest
            == source.source_content_digest
        )
        return repaired_source, same_digest

    async def run(
        self,
        *,
        owner_id: str | None = None,
        novel_id: str,
        chapter: dict[str, Any],
        max_repair_cycles: int = 0,
        repair_budget_limits: (
            RepairBudgetLimitsV1 | Mapping[str, Any] | None
        ) = None,
        tail_judge_retry_usage: int = 0,
        tail_repair_attempt_ids: tuple[str, ...] = (),
        resume: ChapterCandidatePipelineResume | None = None,
    ) -> ChapterCandidatePipelineResult:
        component_limits, repair_limit = _strict_repair_limits(
            max_repair_cycles,
            repair_budget_limits,
        )
        if (
            not isinstance(tail_repair_attempt_ids, tuple)
            or len(tail_repair_attempt_ids) > _MAX_PIPELINE_ATTEMPTS
            or len(tail_repair_attempt_ids)
            != len(set(tail_repair_attempt_ids))
            or any(
                not isinstance(attempt_id, str) or not attempt_id
                for attempt_id in tail_repair_attempt_ids
            )
            or (
                resume is None
                and (tail_judge_retry_usage or tail_repair_attempt_ids)
            )
        ):
            raise ValueError("tail repair attempt group is invalid")
        trace = _PipelineTrace()
        restored: _RestoredPipeline | None = None
        checkpoint_ledger: list[CandidatePipelineCheckpointV1] = []
        try:
            if resume is not None:
                restored = _resume_trace(
                    resume,
                    repair_limit=repair_limit,
                    repair_budget_limits=component_limits,
                    tail_judge_retry_usage=tail_judge_retry_usage,
                    tail_repair_attempt_ids=tail_repair_attempt_ids,
                    chapter_id=str(chapter.get("_id") or ""),
                    chapter=chapter,
                )
                trace = restored.trace
                checkpoint_ledger = [
                    parse_candidate_pipeline_checkpoint(checkpoint)
                    for checkpoint in resume.checkpoints
                ]
            if restored is not None:
                policy_replay = restored.repair_policy_replay
            else:
                try:
                    policy_replay = _replay_repair_policy(
                        checkpoint_ledger,
                        limits=component_limits,
                        attempts=tuple(trace.attempts),
                        tail_judge_retry_usage=tail_judge_retry_usage,
                    )
                except _RepairPolicyReplayBudgetExhausted as replay_exhausted:
                    raise _repair_budget_blocked(
                        replay_exhausted.exhausted,
                        trace=trace,
                        policy=replay_exhausted.policy,
                        gate="outline_adherence",
                    ) from replay_exhausted
            repair_policy = policy_replay.policy
            _sync_repair_policy(trace, repair_policy)
            if (
                repair_policy.transitions
                and repair_policy.transitions[-1].decision == "not_converged"
            ):
                latest = repair_policy.transitions[-1]
                raise ChapterCandidatePipelineBlocked(
                    "修复后的稳定问题集合没有收敛",
                    code="repair_not_converged",
                    progress=trace.snapshot(),
                    gate="outline_adherence",
                    repair_component=latest.component,
                    component_used=latest.component_attempt,
                    component_limit=latest.charge.authorized_limit,
                    next_step=repair_next_step(latest.component),
                )
            return await self._run(
                novel_id=novel_id,
                owner_id=owner_id,
                chapter=chapter,
                repair_limit=repair_limit,
                repair_policy=repair_policy,
                pending_convergence_charge=(
                    policy_replay.pending_convergence_charge
                ),
                trace=trace,
                resume=restored,
                checkpoint_ledger=checkpoint_ledger,
            )
        except ChapterCandidatePipelineBlocked as exc:
            if not exc.has_progress:
                exc.attach_progress(trace.snapshot())
            raise
        except (TokenBudgetExceeded, AttemptCapacityExceeded):
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
        repair_policy: ChapterRepairPolicy,
        pending_convergence_charge: RepairChargeV1 | None,
        trace: _PipelineTrace,
        resume: _RestoredPipeline | None,
        checkpoint_ledger: list[CandidatePipelineCheckpointV1],
    ) -> ChapterCandidatePipelineResult:
        chapter_id = str(chapter.get("_id") or "")
        if not chapter_id:
            raise ChapterCandidatePipelineBlocked("章节候选缺少内部章节 ID")
        if resume is None:
            generated = await self._deps.generate_prose_candidate(
                novel_id,
                chapter,
            )
            evidence = trace.record("prose", generated.generation)
            _prose, source = _validate_prose_candidate(generated)
            trace.source = source
            await self._persist_checkpoint(_prose_checkpoint(
                chapter_id=chapter_id,
                sequence=len(trace.completed_steps),
                source=source,
                evidence=evidence,
                cycle=0,
                origin="initial",
            ), ledger=checkpoint_ledger)
        else:
            source = resume.source
            if resume.state is not None:
                resumed_adherence = resume.adherence
                if resumed_adherence is None:
                    raise ChapterCandidatePipelineBlocked(
                        "候选管线恢复状态缺少前置复检"
                    )
                _validate_resumed_state_prerequisite(
                    resumed_adherence,
                    source=source,
                    chapter=chapter,
                )

        review_count = resume.review_count if resume is not None else 0
        pending_review = resume.adherence if resume is not None else None
        last_repair_kept_digest = bool(
            resume is not None and resume.last_repair_kept_digest
        )
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
                _authorize_repair_component(
                    repair_policy,
                    RepairComponent.SCENE_REGENERATION,
                    trace=trace,
                    gate="completion",
                )
                cycle = _next_repair_cycle(
                    trace.repair_cycles_used,
                    repair_limit,
                    gate="completion",
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
                        repair_policy=repair_policy,
                        checkpoint_ledger=checkpoint_ledger,
                    )
                )
                _sync_repair_policy(trace, repair_policy)
                continue

            if pending_review is None:
                reviewed = await self._deps.review_prose_candidate(
                    novel_id,
                    chapter,
                    source,
                )
                review_count += 1
                review_evidence = trace.record(
                    (
                        "outline_adherence"
                        if review_count == 1
                        else f"outline_adherence_recheck_{review_count}"
                    ),
                    reviewed,
                )
            else:
                reviewed = pending_review
                pending_review = None
                review_evidence = None
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
            if review_evidence is not None:
                await self._persist_checkpoint(_adherence_checkpoint(
                    chapter_id=chapter_id,
                    sequence=len(trace.completed_steps),
                    source=source,
                    evidence=review_evidence,
                    cycle=trace.repair_cycles_used,
                    adherence=adherence,
                ), ledger=checkpoint_ledger)
                if trace.repair_cycles_used > 0:
                    _charge_judge_or_schema_retries(
                        repair_policy,
                        review_evidence,
                        trace=trace,
                        gate="outline_adherence",
                    )
                if adherence.get("evidence_schema_version") == (
                    OUTLINE_ADHERENCE_EVIDENCE_VERSION
                ):
                    repair_issues = _hard_repair_issues(adherence)
                    if repair_policy.observation_count == 0:
                        convergence = repair_policy.observe_adherence(
                            repair_issues,
                            prose_run_revision=source.source_run_revision,
                            content_digest=source.source_content_digest,
                        )
                    elif pending_convergence_charge is not None:
                        convergence = repair_policy.observe_adherence(
                            repair_issues,
                            prose_run_revision=source.source_run_revision,
                            content_digest=source.source_content_digest,
                            charge=pending_convergence_charge,
                        )
                    else:
                        raise ChapterCandidatePipelineBlocked(
                            "章纲符合度复检缺少对应的内容修复授权"
                        )
                    pending_convergence_charge = None
                    _sync_repair_policy(trace, repair_policy)
                    if (
                        convergence is not None
                        and convergence.decision == "not_converged"
                    ):
                        raise ChapterCandidatePipelineBlocked(
                            "修复后的稳定问题集合没有收敛",
                            code="repair_not_converged",
                            progress=trace.snapshot(),
                            gate="outline_adherence",
                            repair_component=convergence.component,
                            component_used=convergence.component_attempt,
                            component_limit=(
                                convergence.charge.authorized_limit
                            ),
                            next_step=repair_next_step(
                                convergence.component
                            ),
                        )
            if adherence.get("decision") == "manual_review":
                raise ChapterCandidatePipelineBlocked(
                    "章纲符合度存在无法自动裁决的语义 unknown，必须转人工",
                    code="candidate_adherence_manual_review",
                    gate="outline_adherence",
                )
            try:
                adherence_metadata = _validate_adherence_gate(
                    adherence,
                    chapter,
                    prose=source.text,
                )
            except ChapterCandidatePipelineBlocked as gate_error:
                uses_stable_issue_policy = adherence.get(
                    "evidence_schema_version"
                ) == OUTLINE_ADHERENCE_EVIDENCE_VERSION
                if last_repair_kept_digest and not uses_stable_issue_policy:
                    raise ChapterCandidatePipelineBlocked(
                        "正文摘要未变化且章纲复检仍未通过",
                        code="repair_no_progress",
                    ) from gate_error
                if self._deps.repair_prose_candidate is None:
                    raise
                component = _content_repair_component(adherence)
                charge = _authorize_repair_component(
                    repair_policy,
                    component,
                    trace=trace,
                    gate="outline_adherence",
                )
                cycle = _next_repair_cycle(
                    trace.repair_cycles_used,
                    repair_limit,
                    gate="outline_adherence",
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
                        repair_policy=repair_policy,
                        checkpoint_ledger=checkpoint_ledger,
                    )
                )
                pending_convergence_charge = (
                    charge if uses_stable_issue_policy else None
                )
                _sync_repair_policy(trace, repair_policy)
                continue
            break

        if resume is not None and resume.state is not None:
            state_result = resume.state
            state_checkpoint_context = None
        else:
            state_request_id = _initial_state_request_id(
                chapter_id=chapter_id,
                source=source,
            )
            state_result = await self._deps.generate_state_candidate(
                novel_id,
                chapter,
                source,
                request_id=state_request_id,
            )
            state_evidence = trace.record("state", state_result)
            state_checkpoint_context = (
                state_evidence,
                "initial",
                0,
                state_request_id,
            )
        while True:
            (
                state,
                proposal_id,
                _acceptance_token,
                consistency_issues,
                fact_accounting,
            ) = (
                _validate_state_shape(
                    state_result,
                    source=source,
                    chapter_id=chapter_id,
                )
            )
            if (
                resume is not None
                and resume.state is state_result
                and trace.state_proposal_id != proposal_id
            ):
                raise ChapterCandidatePipelineBlocked(
                    "恢复检查点与状态候选身份不一致"
                )
            trace.state_proposal_id = proposal_id
            dropped = dict(state_result.dropped or {})
            if state_checkpoint_context is not None:
                (
                    state_evidence,
                    state_origin,
                    state_cycle,
                    state_request_id,
                ) = state_checkpoint_context
                await self._persist_checkpoint(_state_checkpoint(
                    chapter_id=chapter_id,
                    sequence=len(trace.completed_steps),
                    source=source,
                    evidence=state_evidence,
                    cycle=state_cycle,
                    origin=state_origin,
                    request_id=state_request_id,
                    proposal_id=proposal_id,
                    consistency_issue_count=len(consistency_issues),
                    dropped_reference_count=_dropped_reference_count(
                        dropped
                    ),
                    fact_accounting=fact_accounting,
                ), ledger=checkpoint_ledger)
            state_checkpoint_context = None
            if (
                not consistency_issues
                and not dropped
                and fact_accounting.get("gate_passed") is True
            ):
                break
            if self._deps.repair_state_candidate is None:
                if fact_accounting.get("unaccounted_canonical_facts"):
                    message = "状态候选仍有未核算正式事实"
                elif (
                    fact_accounting.get("invalid_internal_references")
                    or fact_accounting.get("dangling_references")
                ):
                    message = "状态候选仍有无效或悬空内部引用"
                elif fact_accounting.get("extraction_failure_count"):
                    message = "状态候选事实抽取仍不确定"
                else:
                    message = "状态候选仍有一致性冲突或无效引用"
                raise ChapterCandidatePipelineBlocked(
                    message
                )
            state_issue_card_ids = _state_issue_card_ids(
                consistency_issues,
                declared_card_ids=_declared_character_card_ids(chapter),
            )
            state_dropped_count = _dropped_reference_count(dropped)
            _authorize_repair_component(
                repair_policy,
                RepairComponent.STATE_REEXTRACTION,
                trace=trace,
                gate="state",
                consistency_issue_count=len(consistency_issues),
                dropped_reference_count=state_dropped_count,
                affected_card_ids=state_issue_card_ids,
            )
            cycle = _next_repair_cycle(
                trace.repair_cycles_used,
                repair_limit,
                gate="state",
                consistency_issue_count=len(consistency_issues),
                dropped_reference_count=state_dropped_count,
                affected_card_ids=state_issue_card_ids,
            )
            request = _state_repair_request(
                cycle=cycle,
                proposal_id=proposal_id,
                source=source,
                declared_card_ids=_declared_character_card_ids(chapter),
                state=state,
                dropped=dropped,
                fact_accounting=fact_accounting,
            )
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
            state_evidence = trace.record(
                f"state_repair_{cycle}",
                state_result,
            )
            (
                _next_state,
                next_proposal_id,
                _next_token,
                _next_issues,
                next_fact_accounting,
            ) = (
                _validate_state_shape(
                    state_result,
                    source=source,
                    chapter_id=chapter_id,
                )
            )
            await self._persist_checkpoint(_state_checkpoint(
                chapter_id=chapter_id,
                sequence=len(trace.completed_steps),
                source=source,
                evidence=state_evidence,
                cycle=cycle,
                origin="repair",
                request_id=receipt.request_id,
                proposal_id=next_proposal_id,
                consistency_issue_count=len(_next_issues),
                dropped_reference_count=_dropped_reference_count(
                    state_result.dropped
                ),
                fact_accounting=next_fact_accounting,
            ), ledger=checkpoint_ledger)
            trace.repair_cycles_used = cycle
            _charge_judge_or_schema_retries(
                repair_policy,
                state_evidence,
                trace=trace,
                gate="state",
                consistency_issue_count=len(_next_issues),
                dropped_reference_count=_dropped_reference_count(
                    state_result.dropped
                ),
                affected_card_ids=state_issue_card_ids,
            )
            _sync_repair_policy(trace, repair_policy)
            if next_proposal_id == proposal_id:
                raise ChapterCandidatePipelineBlocked(
                    "状态修复没有产生新候选",
                    code="repair_not_converged",
                    progress=trace.snapshot(),
                    gate="state",
                    repair_component=RepairComponent.STATE_REEXTRACTION,
                    component_used=repair_policy.used(
                        RepairComponent.STATE_REEXTRACTION
                    ),
                    component_limit=repair_policy.limits.limit_for(
                        RepairComponent.STATE_REEXTRACTION
                    ),
                    next_step=repair_next_step(
                        RepairComponent.STATE_REEXTRACTION
                    ),
                )
            trace.state_proposal_id = next_proposal_id

        outline = chapter.get("outline")
        scenes = outline.get("scenes") if isinstance(outline, Mapping) else None
        if not isinstance(scenes, list) or not scenes:
            raise ChapterCandidatePipelineBlocked("章节缺少有效章纲")
        try:
            replay_candidate_pipeline_checkpoints(
                tuple(checkpoint_ledger),
                chapter_id=chapter_id,
                expected_scene_count=len(scenes),
                max_repair_cycles=repair_limit,
                require_terminal=True,
            )
        except CandidatePipelineCheckpointConflict as exc:
            raise ChapterCandidatePipelineBlocked(
                str(exc),
                code=exc.code,
            ) from exc

        progress = trace.snapshot()
        repair_trace = (
            None
            if progress.repair_cycles_used == 0
            else {
                "schema_version": "chapter_repair_trace.v2",
                "repair_cycles_used": progress.repair_cycles_used,
                "component_usage": [
                    item.model_dump(mode="json")
                    for item in progress.repair_component_usage
                ],
                "convergence": [
                    item.model_dump(mode="json")
                    for item in progress.repair_convergence
                ],
                "final_issue_signatures": [],
                "converged": True,
            }
        )
        finalization = dict(
            await self._deps.finalize(
                novel_id,
                chapter,
                source,
                adherence,
                state,
                trace.repair_cycles_used,
                repair_trace=repair_trace,
            )
        )
        return ChapterCandidatePipelineResult(
            tokens=progress.tokens,
            attempts=progress.attempts,
            truncations=progress.truncations,
            outline_adherence=adherence_metadata,
            consistency_issues=consistency_issues,
            prose_run_id=source.source_run_id,
            prose_run_revision=source.source_run_revision,
            prose_content_digest=source.source_content_digest,
            state_proposal_id=proposal_id,
            repair_cycles_used=trace.repair_cycles_used,
            repair_component_usage=progress.repair_component_usage,
            repair_convergence=progress.repair_convergence,
            finalization=finalization,
        )
