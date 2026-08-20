"""Deferred chapter tail: candidates first, one deterministic formal commit last."""

from __future__ import annotations

import hashlib
import json
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
from backend.services.generation.candidate_repair_contracts import (
    MAX_CHAPTER_CANDIDATE_PIPELINE_CHECKPOINTS,
    AdherenceCandidateCheckpointV1,
    CandidateCompletionProjectionV1,
    CandidatePipelineCheckpointV1,
    CandidateSceneCoverageV1,
    CandidateSourceIdentityV1,
    CandidateTruncationProjectionV1,
    ProseCandidateCheckpointV1,
    StateCandidateCheckpointV1,
    is_safe_candidate_identifier,
    parse_candidate_pipeline_checkpoint,
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
from backend.services.generation.prose_completion import (
    completion_allows_formal_write,
)
from backend.services.generation.state_repair_contracts import (
    MAX_STATE_REPAIR_CARD_ID_LENGTH,
    MAX_STATE_REPAIR_CARD_IDS,
    MAX_STATE_REPAIR_DROPPED_REFERENCES,
    StateRepairDirective,
    StateRepairReason,
)


_MAX_REPAIR_SCENE_INDEXES = 20
_MAX_PIPELINE_ATTEMPTS = 512
_MAX_PIPELINE_TRUNCATIONS = 32
_MAX_PIPELINE_UNATTRIBUTED_USAGE = 32
_MAX_TOKEN_COUNT = 1_000_000_000
_MAX_RESUMED_ADHERENCE_ITEMS = 20
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
    ) -> None:
        super().__init__(message)
        self.code = code
        self.progress = progress or ChapterCandidatePipelineProgress()
        self._progress_attached = progress is not None

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
    payload = checkpoint.model_dump(
        mode="json",
        exclude={"checkpoint_id"},
    )
    return hashlib.sha256(json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


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
        "schema_version": "chapter_candidate_pipeline_checkpoint.v1",
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
    Literal["pass", "warn", "fail"],
    tuple[OutlineIssueCategory, ...],
    tuple[CandidateSceneCoverageV1, ...],
]:
    verdict = adherence.get("verdict")
    if verdict not in {"pass", "warn", "fail"}:
        raise ChapterCandidatePipelineBlocked(
            "章纲符合度 verdict 无效"
        )
    raw_issues = adherence.get("issues")
    raw_coverage = adherence.get("scene_coverage")
    if (
        not isinstance(raw_issues, list)
        or not isinstance(raw_coverage, list)
        or len(raw_issues) > _MAX_RESUMED_ADHERENCE_ITEMS
        or len(raw_coverage) > _MAX_RESUMED_ADHERENCE_ITEMS
    ):
        raise ChapterCandidatePipelineBlocked(
            "章纲符合度持久投影无效"
        )
    categories: list[OutlineIssueCategory] = []
    for item in raw_issues:
        if not isinstance(item, Mapping):
            raise ChapterCandidatePipelineBlocked(
                "章纲符合度问题投影无效"
            )
        category = item.get("category")
        if category not in _OUTLINE_ISSUE_CATEGORIES:
            raise ChapterCandidatePipelineBlocked(
                "章纲符合度问题类别无效"
            )
        if category not in categories:
            categories.append(category)
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
    return verdict, tuple(categories), tuple(coverage)


def _adherence_checkpoint(
    *,
    chapter_id: str,
    sequence: int,
    source: ProseCandidateSource,
    evidence: _RecordedStepEvidence,
    cycle: int,
    adherence: Mapping[str, Any],
) -> AdherenceCandidateCheckpointV1:
    verdict, categories, coverage = _adherence_checkpoint_projection(
        adherence
    )
    return _seal_checkpoint(AdherenceCandidateCheckpointV1(
        **_checkpoint_common(
            chapter_id=chapter_id,
            sequence=sequence,
            source=source,
            evidence=evidence,
        ),
        cycle=cycle,
        verdict=verdict,
        issue_categories=categories,
        scene_coverage=coverage,
    ))


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
    dropped_reference_count: int,
) -> StateCandidateCheckpointV1:
    return _seal_checkpoint(StateCandidateCheckpointV1(
        **_checkpoint_common(
            chapter_id=chapter_id,
            sequence=sequence,
            source=source,
            evidence=evidence,
        ),
        cycle=cycle,
        origin=origin,
        request_id=request_id,
        proposal_id=proposal_id,
        dropped_reference_count=dropped_reference_count,
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
            self._record_cross_step_attempt_conflict(usage, summaries)
            self._record_evidence(usage, summaries)
        except _EvidenceProjectionError as exc:
            if isinstance(exc, _UnattributedUsageProjectionError):
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
            self._record_cross_step_attempt_conflict(usage, summaries)
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

    def _record_cross_step_attempt_conflict(
        self,
        usage: CandidateUsageSummary,
        summaries: tuple[CandidateAttemptSummary, ...],
    ) -> None:
        """Preserve a later paid step without reusing an earlier ledger ID."""
        known_ids = {item.attempt_id for item in self.attempts}
        reused = tuple(
            item for item in summaries if item.attempt_id in known_ids
        )
        if not reused:
            return
        new = tuple(
            item for item in summaries if item.attempt_id not in known_ids
        )
        if len(self.attempts) + len(new) > _MAX_PIPELINE_ATTEMPTS:
            raise _UnattributedUsageProjectionError(
                "付费步骤复用了先前步骤的 attempt_id，且新调用证据超过 V1 上限",
                reason=(
                    CandidateUnattributedUsageReason.ATTEMPT_LEDGER_CONFLICT
                ),
                usage=usage,
                attempts=(),
                evidence_kind=CandidateUsageEvidenceKind.INCOMPLETE,
            )
        try:
            new_usage = _summed_usage(new)
            reused_usage = _summed_usage(reused)
            next_tokens = _checked_token_add(
                self.tokens,
                new_usage.total_tokens,
            )
        except _UsageProjectionOverflow as overflow:
            raise _usage_overflow_error(str(overflow)) from overflow
        self.attempts.extend(new)
        self.tokens = next_tokens
        raise _UnattributedUsageProjectionError(
            "付费步骤复用了先前步骤的 attempt_id",
            reason=CandidateUnattributedUsageReason.ATTEMPT_LEDGER_CONFLICT,
            usage=reused_usage,
            attempts=(),
            evidence_kind=CandidateUsageEvidenceKind.INCOMPLETE,
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
    if isinstance(checkpoint, AdherenceCandidateCheckpointV1):
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


def _checkpoint_completion_passed(
    checkpoint: ProseCandidateCheckpointV1,
) -> bool:
    return completion_allows_formal_write(
        status=checkpoint.completion.status,
        can_write_formal_prose=(
            checkpoint.completion.can_write_formal_prose
        ),
        finish_reason=checkpoint.completion.finish_reason,
    )
def _checkpoint_adherence_passed(
    checkpoint: AdherenceCandidateCheckpointV1,
    *,
    chapter: Mapping[str, Any],
) -> bool:
    outline = chapter.get("outline")
    scenes = outline.get("scenes") if isinstance(outline, Mapping) else None
    if not isinstance(scenes, list) or not scenes:
        return False
    indexes = [item.scene_index for item in checkpoint.scene_coverage]
    return bool(
        checkpoint.verdict == "pass"
        and not checkpoint.issue_categories
        and len(indexes) == len(scenes)
        and len(set(indexes)) == len(indexes)
        and set(indexes) == set(range(1, len(scenes) + 1))
        and all(
            item.status == "covered"
            for item in checkpoint.scene_coverage
        )
    )


def _validate_resumed_adherence_projection(
    reviewed: ChapterGenerationResult,
    *,
    checkpoint: AdherenceCandidateCheckpointV1,
    source: ProseCandidateSource,
) -> None:
    if reviewed.stage is not ChapterGenerationStage.OUTLINE_ADHERENCE:
        raise ChapterCandidatePipelineBlocked("候选管线恢复复检结果无效")
    if not isinstance(reviewed.value, Mapping):
        raise ChapterCandidatePipelineBlocked("候选管线恢复复检结果无效")
    adherence = reviewed.value
    if not _adherence_matches_source(adherence, source):
        raise ChapterCandidatePipelineBlocked("候选管线恢复复检身份不一致")
    issues = adherence.get("issues")
    coverage = adherence.get("scene_coverage")
    if (
        not isinstance(issues, list)
        or not isinstance(coverage, list)
        or len(issues) > _MAX_RESUMED_ADHERENCE_ITEMS
        or len(coverage) > _MAX_RESUMED_ADHERENCE_ITEMS
    ):
        raise ChapterCandidatePipelineBlocked("候选管线恢复复检结果无效")
    issue_categories: list[str] = []
    for item in issues:
        if not isinstance(item, Mapping):
            raise ChapterCandidatePipelineBlocked(
                "候选管线恢复复检结果无效"
            )
        category = item.get("category")
        if category not in _OUTLINE_ISSUE_CATEGORIES:
            raise ChapterCandidatePipelineBlocked(
                "候选管线恢复复检结果无效"
            )
        if category not in issue_categories:
            issue_categories.append(category)
    projected_coverage: list[tuple[int, str]] = []
    for item in coverage:
        if not isinstance(item, Mapping):
            raise ChapterCandidatePipelineBlocked(
                "候选管线恢复复检结果无效"
            )
        index = item.get("scene_index")
        status = item.get("status")
        if type(index) is not int or status not in {
            "covered",
            "partial",
            "missing",
        }:
            raise ChapterCandidatePipelineBlocked(
                "候选管线恢复复检结果无效"
            )
        projected_coverage.append((index, status))
    if (
        adherence.get("verdict") != checkpoint.verdict
        or tuple(issue_categories) != tuple(checkpoint.issue_categories)
        or tuple(projected_coverage)
        != tuple(
            (item.scene_index, item.status)
            for item in checkpoint.scene_coverage
        )
    ):
        raise ChapterCandidatePipelineBlocked(
            "候选管线恢复复检投影与检查点不一致"
        )


def _validate_resumed_state_projection(
    state_result: ChapterGenerationResult,
    *,
    checkpoint: StateCandidateCheckpointV1,
) -> None:
    _state, proposal_id, _acceptance_token, _issues = _validate_state_shape(
        state_result
    )
    if (
        proposal_id != checkpoint.proposal_id
        or _dropped_reference_count(state_result.dropped)
        != checkpoint.dropped_reference_count
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
    latest_adherence: AdherenceCandidateCheckpointV1 | None
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


def _replay_candidate_checkpoints(
    checkpoints: tuple[CandidatePipelineCheckpointV1, ...],
    *,
    trace: _PipelineTrace,
    repair_limit: int,
    chapter_id: str,
    chapter: Mapping[str, Any],
) -> _CheckpointReplay:
    completed_steps: list[str] = []
    checkpoint_attempt_ids: list[str] = []
    expected_truncations: list[CandidateTruncationSummary] = []
    current_prose: ProseCandidateCheckpointV1 | None = None
    previous_prose: ProseCandidateCheckpointV1 | None = None
    latest_adherence: AdherenceCandidateCheckpointV1 | None = None
    latest_state: StateCandidateCheckpointV1 | None = None
    phase = _ResumePhase.START
    repair_cycles_used = 0
    review_count = 0
    seen_checkpoint_ids: set[str] = set()
    seen_state_request_ids: set[str] = set()
    seen_state_proposal_ids: set[str] = set()

    for expected_sequence, checkpoint in enumerate(checkpoints, start=1):
        if (
            checkpoint.sequence != expected_sequence
            or checkpoint.chapter_id != chapter_id
            or checkpoint.checkpoint_id in seen_checkpoint_ids
        ):
            raise _blocked_resume(
                trace,
                "候选管线恢复检查点顺序或章节身份无效",
            )
        seen_checkpoint_ids.add(checkpoint.checkpoint_id)

        if isinstance(checkpoint, ProseCandidateCheckpointV1):
            if checkpoint.origin == "initial":
                if phase is not _ResumePhase.START:
                    raise _blocked_resume(
                        trace,
                        "候选管线恢复步骤顺序无效",
                    )
            else:
                if phase not in {
                    _ResumePhase.PROSE,
                    _ResumePhase.ADHERENCE,
                }:
                    raise _blocked_resume(
                        trace,
                        "候选管线恢复步骤顺序无效",
                    )
                kept_digest = _prose_checkpoint_kept_digest(
                    current_prose,
                    previous_prose,
                )
                if phase is _ResumePhase.PROSE and current_prose is not None:
                    if _checkpoint_completion_passed(current_prose):
                        raise _blocked_resume(
                            trace,
                            "候选管线恢复步骤顺序无效",
                        )
                    if kept_digest:
                        raise _blocked_resume(
                            trace,
                            "正文摘要未变化且完成闸门复检仍未通过",
                            code="repair_no_progress",
                        )
                if (
                    phase is _ResumePhase.ADHERENCE
                    and latest_adherence is not None
                ):
                    if _checkpoint_adherence_passed(
                        latest_adherence,
                        chapter=chapter,
                    ):
                        raise _blocked_resume(
                            trace,
                            "候选管线恢复步骤顺序无效",
                        )
                    if kept_digest:
                        raise _blocked_resume(
                            trace,
                            "正文摘要未变化且章纲复检仍未通过",
                            code="repair_no_progress",
                        )
                if checkpoint.cycle != repair_cycles_used + 1:
                    raise _blocked_resume(
                        trace,
                        "候选管线恢复修复轮次无效",
                    )
                if (
                    current_prose is None
                    or checkpoint.source.source_run_id
                    != current_prose.source.source_run_id
                    or checkpoint.source.source_run_revision
                    <= current_prose.source.source_run_revision
                ):
                    raise _blocked_resume(
                        trace,
                        "候选管线恢复正文修复谱系无效",
                    )
                repair_cycles_used = checkpoint.cycle
            previous_prose = current_prose
            current_prose = checkpoint
            latest_adherence = None
            latest_state = None
            phase = _ResumePhase.PROSE
        elif isinstance(checkpoint, AdherenceCandidateCheckpointV1):
            if phase is not _ResumePhase.PROSE or current_prose is None:
                raise _blocked_resume(
                    trace,
                    "候选管线恢复步骤顺序无效",
                )
            if not _checkpoint_completion_passed(current_prose):
                raise _blocked_resume(
                    trace,
                    "候选管线恢复复检早于正文完成闸门",
                )
            if (
                _checkpoint_source_key(checkpoint)
                != _checkpoint_source_key(current_prose)
                or checkpoint.cycle != current_prose.cycle
            ):
                raise _blocked_resume(
                    trace,
                    "候选管线恢复复检身份无效",
                )
            review_count += 1
            latest_adherence = checkpoint
            latest_state = None
            phase = _ResumePhase.ADHERENCE
        else:
            if checkpoint.request_id in seen_state_request_ids:
                raise _blocked_resume(
                    trace,
                    "候选管线恢复状态修复身份重复",
                )
            if checkpoint.origin == "initial":
                if (
                    phase is not _ResumePhase.ADHERENCE
                    or latest_adherence is None
                    or not _checkpoint_adherence_passed(
                        latest_adherence,
                        chapter=chapter,
                    )
                ):
                    raise _blocked_resume(
                        trace,
                        "候选管线恢复状态前置步骤无效",
                    )
            else:
                if (
                    phase is not _ResumePhase.STATE
                    or latest_state is None
                    or checkpoint.cycle != repair_cycles_used + 1
                ):
                    raise _blocked_resume(
                        trace,
                        "候选管线恢复状态修复轮次无效",
                    )
                if checkpoint.proposal_id == latest_state.proposal_id:
                    trace.completed_steps.append(_checkpoint_step_name(
                        checkpoint,
                        review_count=review_count,
                    ))
                    trace.repair_cycles_used = checkpoint.cycle
                    raise _blocked_resume(
                        trace,
                        "状态修复没有产生新候选",
                        code="repair_no_progress",
                    )
                if checkpoint.proposal_id in seen_state_proposal_ids:
                    raise _blocked_resume(
                        trace,
                        "候选管线恢复状态修复身份重复",
                    )
                repair_cycles_used = checkpoint.cycle
            if (
                current_prose is None
                or _checkpoint_source_key(checkpoint)
                != _checkpoint_source_key(current_prose)
            ):
                raise _blocked_resume(
                    trace,
                    "候选管线恢复状态正文身份无效",
                )
            seen_state_request_ids.add(checkpoint.request_id)
            seen_state_proposal_ids.add(checkpoint.proposal_id)
            latest_state = checkpoint
            phase = _ResumePhase.STATE

        step = _checkpoint_step_name(
            checkpoint,
            review_count=review_count,
        )
        completed_steps.append(step)
        trace.completed_steps.append(step)
        trace.repair_cycles_used = repair_cycles_used
        checkpoint_attempt_ids.extend(checkpoint.attempt_ids)
        truncation = checkpoint.truncation
        if truncation.truncated_section_count or truncation.dropped_item_count:
            expected_truncations.append(CandidateTruncationSummary(
                step=step,
                truncated_section_count=truncation.truncated_section_count,
                dropped_item_count=truncation.dropped_item_count,
            ))

    if current_prose is None:
        raise _blocked_resume(trace, "候选管线恢复正文步骤缺失")
    if repair_cycles_used > repair_limit:
        raise _blocked_resume(trace, "候选管线恢复超出修复授权")
    return _CheckpointReplay(
        completed_steps=tuple(completed_steps),
        attempt_ids=tuple(checkpoint_attempt_ids),
        truncations=tuple(expected_truncations),
        current_prose=current_prose,
        previous_prose=previous_prose,
        latest_adherence=latest_adherence,
        latest_state=latest_state,
        phase=phase,
        repair_cycles_used=repair_cycles_used,
        review_count=review_count,
    )


def _resume_trace(
    resume: ChapterCandidatePipelineResume,
    *,
    repair_limit: int,
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
    if replay.completed_steps != progress.completed_steps:
        raise _blocked_resume(restored, "候选管线恢复步骤与检查点不一致")
    restored_attempt_ids = tuple(
        item.attempt_id for item in restored.attempts
    )
    trailing_attempts = restored.attempts[len(replay.attempt_ids):]
    if (
        restored_attempt_ids[:len(replay.attempt_ids)]
        != replay.attempt_ids
        or len(trailing_attempts) > 1
    ):
        raise _blocked_resume(restored, "候选管线恢复调用与检查点不一致")
    if (
        trailing_attempts
        and trailing_attempts[0].state is not CandidateAttemptState.UNCERTAIN
    ):
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
) -> dict[str, Any]:
    outline = chapter.get("outline")
    if not isinstance(outline, Mapping):
        raise ChapterCandidatePipelineBlocked("章节缺少有效章纲")
    try:
        return validate_complete_outline_adherence(
            adherence,
            outline=outline,
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
        _validate_adherence_gate(adherence, chapter)
    except ChapterCandidatePipelineBlocked as exc:
        raise ChapterCandidatePipelineBlocked(
            "候选管线恢复状态的前置复检未通过"
        ) from exc


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
) -> StateCandidateRepairRequest:
    raw_issues = state.get("consistency_issues")
    issues = raw_issues if isinstance(raw_issues, list) else []
    card_ids = tuple(sorted({
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
    if len(raw_issues) > MAX_STATE_REPAIR_CARD_IDS:
        raise ChapterCandidatePipelineBlocked("状态候选冲突数量超过 V1 上限")
    issues = tuple(dict(item) for item in raw_issues)
    return state, proposal_id, acceptance_token, issues


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
    ) -> None:
        persist = self._deps.persist_checkpoint
        await persist(parse_candidate_pipeline_checkpoint(checkpoint))

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
        ))
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
        resume: ChapterCandidatePipelineResume | None = None,
    ) -> ChapterCandidatePipelineResult:
        repair_limit = _strict_repair_limit(max_repair_cycles)
        trace = _PipelineTrace()
        restored: _RestoredPipeline | None = None
        try:
            if resume is not None:
                restored = _resume_trace(
                    resume,
                    repair_limit=repair_limit,
                    chapter_id=str(chapter.get("_id") or ""),
                    chapter=chapter,
                )
                trace = restored.trace
            return await self._run(
                novel_id=novel_id,
                owner_id=owner_id,
                chapter=chapter,
                repair_limit=repair_limit,
                trace=trace,
                resume=restored,
            )
        except ChapterCandidatePipelineBlocked as exc:
            if not exc.has_progress:
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
        resume: _RestoredPipeline | None,
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
            ))
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
                ))
            try:
                adherence_metadata = _validate_adherence_gate(
                    adherence,
                    chapter,
                )
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
            state, proposal_id, _acceptance_token, consistency_issues = (
                _validate_state_shape(state_result)
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
                    dropped_reference_count=_dropped_reference_count(
                        dropped
                    ),
                ))
            state_checkpoint_context = None
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
            state_evidence = trace.record(
                f"state_repair_{cycle}",
                state_result,
            )
            _next_state, next_proposal_id, _next_token, _next_issues = (
                _validate_state_shape(state_result)
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
                dropped_reference_count=_dropped_reference_count(
                    state_result.dropped
                ),
            ))
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
            outline_adherence=adherence_metadata,
            consistency_issues=consistency_issues,
            prose_run_id=source.source_run_id,
            prose_run_revision=source.source_run_revision,
            prose_content_digest=source.source_content_digest,
            state_proposal_id=proposal_id,
            repair_cycles_used=trace.repair_cycles_used,
            finalization=finalization,
        )
