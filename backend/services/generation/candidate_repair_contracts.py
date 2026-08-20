"""Small shared contracts for the bounded chapter-candidate repair tail."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from backend.llm.stream_terminal import FinishReason
from backend.services.generation.outline_adherence import (
    OutlineIssueCategoryValue,
)


MAX_CHAPTER_CANDIDATE_REPAIR_CYCLES = 8
MAX_CHAPTER_CANDIDATE_PIPELINE_CHECKPOINTS = 32
MAX_CANDIDATE_CHECKPOINT_ATTEMPTS = 512
MAX_CANDIDATE_PIPELINE_PROGRESS_ENTRIES = 10_000
MAX_BSON_INT64 = 2**63 - 1
_SAFE_CANDIDATE_IDENTIFIER_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
)
_CANDIDATE_CHECKPOINT_LIST_LIMITS = {
    "attempt_ids": MAX_CANDIDATE_CHECKPOINT_ATTEMPTS,
    "issue_categories": 20,
    "scene_coverage": 20,
}


def is_safe_candidate_identifier(value: Any, *, maximum: int) -> bool:
    """Validate one bounded identifier shared by live and persisted evidence."""
    return (
        isinstance(value, str)
        and 0 < len(value) <= maximum
        and all(
            character in _SAFE_CANDIDATE_IDENTIFIER_CHARACTERS
            for character in value
        )
    )


class CandidatePipelineCheckpointConflict(ValueError):
    """The append-only candidate cursor no longer matches this execution."""


class PreDispatchFenceV1(BaseModel):
    """One bounded receipt lease mirrored into the GenerationJob ledger."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["state_repair_pre_dispatch_fence.v1"] = (
        "state_repair_pre_dispatch_fence.v1"
    )
    receipt_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    claim_token: str = Field(min_length=1, max_length=128)
    claim_epoch: int = Field(ge=1, le=1_000_000)
    cycle: int = Field(ge=1, le=MAX_CHAPTER_CANDIDATE_REPAIR_CYCLES)

    @property
    def step_id(self) -> str:
        return f"candidate-state-repair:{self.cycle}"


class StateContextProjection(BaseModel):
    """Metadata-only context truncation evidence persisted before dispatch."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["state_context_projection.v1"] = (
        "state_context_projection.v1"
    )
    truncated_section_count: int = Field(ge=0, le=100)
    dropped_item_count: int = Field(ge=0, le=10_000)


class _CandidateCheckpointContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class CandidateSourceIdentityV1(_CandidateCheckpointContract):
    schema_version: Literal["candidate_source_identity.v1"]
    source_run_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    source_run_revision: int = Field(ge=1, le=MAX_BSON_INT64)
    source_content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class CandidatePipelineCompletionV1(_CandidateCheckpointContract):
    """Stable receipt for atomically rolling one active ledger into progress."""

    schema_version: Literal["candidate_pipeline_completion.v1"]
    checkpoint_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    checkpoint_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    ledger_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    sequence: int = Field(
        ge=1,
        le=MAX_CHAPTER_CANDIDATE_PIPELINE_CHECKPOINTS,
    )
    chapter_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    source: CandidateSourceIdentityV1
    state_proposal_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    tokens_delta: int = Field(ge=0, le=MAX_BSON_INT64)


class CandidatePipelineProgressV1(_CandidateCheckpointContract):
    """Bounded metadata-only chapter result published by the atomic rollover."""

    schema_version: Literal["candidate_pipeline_progress.v1"]
    status: Literal["completed"]
    finalization_status: Literal["committed"]
    chapter_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    order_index: int = Field(ge=0, le=1_000_000)
    tokens: int = Field(ge=0, le=MAX_BSON_INT64)
    source: CandidateSourceIdentityV1
    state_proposal_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    repair_cycles_used: int = Field(
        ge=0,
        le=MAX_CHAPTER_CANDIDATE_REPAIR_CYCLES,
    )
    attempt_count: int = Field(ge=0, le=MAX_CANDIDATE_CHECKPOINT_ATTEMPTS)
    truncation_count: int = Field(
        ge=0,
        le=MAX_CHAPTER_CANDIDATE_PIPELINE_CHECKPOINTS,
    )
    outline_issue_categories: tuple[OutlineIssueCategoryValue, ...] = Field(
        default=(),
        max_length=20,
    )
    scene_coverage_count: int = Field(ge=0, le=20)
    consistency_issue_count: int = Field(ge=0, le=20)


class CandidateCompletionProjectionV1(_CandidateCheckpointContract):
    """Only completion fields consumed by the deterministic candidate gates."""

    schema_version: Literal["candidate_completion_projection.v1"]
    status: Literal["complete", "degraded", "incomplete", "stale"]
    can_write_formal_prose: bool
    finish_reason: FinishReason


class CandidateTruncationProjectionV1(_CandidateCheckpointContract):
    schema_version: Literal["candidate_truncation_projection.v1"]
    truncated_section_count: int = Field(default=0, ge=0, le=100)
    dropped_item_count: int = Field(default=0, ge=0, le=10_000)


class CandidateSceneCoverageV1(_CandidateCheckpointContract):
    schema_version: Literal["candidate_scene_coverage.v1"]
    scene_index: int = Field(ge=1, le=100)
    status: Literal["covered", "partial", "missing"]


CandidateOutlineIssueCategory = OutlineIssueCategoryValue


class _CandidatePipelineCheckpointV1(_CandidateCheckpointContract):
    schema_version: Literal["chapter_candidate_pipeline_checkpoint.v1"]
    checkpoint_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    sequence: int = Field(
        ge=1,
        le=MAX_CHAPTER_CANDIDATE_PIPELINE_CHECKPOINTS,
    )
    chapter_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    cycle: int = Field(ge=0, le=MAX_CHAPTER_CANDIDATE_REPAIR_CYCLES)
    source: CandidateSourceIdentityV1
    attempt_ids: tuple[str, ...] = Field(
        default=(),
        max_length=MAX_CANDIDATE_CHECKPOINT_ATTEMPTS,
    )
    truncation: CandidateTruncationProjectionV1

    @model_validator(mode="after")
    def validate_attempt_identities(self) -> "_CandidatePipelineCheckpointV1":
        if any(
            not is_safe_candidate_identifier(attempt_id, maximum=128)
            for attempt_id in self.attempt_ids
        ):
            raise ValueError("candidate checkpoint attempt identity is invalid")
        if len(set(self.attempt_ids)) != len(self.attempt_ids):
            raise ValueError("candidate checkpoint attempt identity is duplicated")
        return self


class ProseCandidateCheckpointV1(_CandidatePipelineCheckpointV1):
    kind: Literal["prose_candidate"] = "prose_candidate"
    origin: Literal["initial", "repair"]
    completion: CandidateCompletionProjectionV1

    @model_validator(mode="after")
    def validate_origin_cycle(self) -> "ProseCandidateCheckpointV1":
        if (self.origin == "initial") != (self.cycle == 0):
            raise ValueError("prose checkpoint origin and cycle diverged")
        return self


class AdherenceCandidateCheckpointV1(_CandidatePipelineCheckpointV1):
    kind: Literal["outline_adherence"] = "outline_adherence"
    verdict: Literal["pass", "warn", "fail"]
    issue_categories: tuple[CandidateOutlineIssueCategory, ...] = Field(
        default=(),
        max_length=20,
    )
    scene_coverage: tuple[CandidateSceneCoverageV1, ...] = Field(
        default=(),
        max_length=20,
    )


class StateCandidateCheckpointV1(_CandidatePipelineCheckpointV1):
    kind: Literal["state_candidate"] = "state_candidate"
    origin: Literal["initial", "repair"]
    proposal_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    request_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    dropped_reference_count: int = Field(default=0, ge=0, le=1_000)

    @model_validator(mode="after")
    def validate_origin_cycle(self) -> "StateCandidateCheckpointV1":
        if (self.origin == "initial") != (self.cycle == 0):
            raise ValueError("state checkpoint origin and cycle diverged")
        return self


CandidatePipelineCheckpointV1 = Annotated[
    ProseCandidateCheckpointV1
    | AdherenceCandidateCheckpointV1
    | StateCandidateCheckpointV1,
    Field(discriminator="kind"),
]


_CANDIDATE_PIPELINE_CHECKPOINT_ADAPTER = TypeAdapter(
    CandidatePipelineCheckpointV1
)


def parse_candidate_pipeline_checkpoint(
    value: Any,
) -> CandidatePipelineCheckpointV1:
    """Revalidate even already-built models to reject forged model_copy values."""

    bounded_list_fields = _validate_checkpoint_list_bounds(value)
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python")
    if not isinstance(value, Mapping):
        return _CANDIDATE_PIPELINE_CHECKPOINT_ADAPTER.validate_python(value)

    value = dict(value)
    _require_contract_version(
        value,
        expected="chapter_candidate_pipeline_checkpoint.v1",
        subject="candidate checkpoint",
    )
    _require_nested_contract_version(
        value,
        field="source",
        expected="candidate_source_identity.v1",
    )
    _require_nested_contract_version(
        value,
        field="truncation",
        expected="candidate_truncation_projection.v1",
    )
    if value.get("kind") == "prose_candidate":
        _require_nested_contract_version(
            value,
            field="completion",
            expected="candidate_completion_projection.v1",
        )

    for field in bounded_list_fields:
        stored = value.get(field)
        if field == "scene_coverage":
            for item in stored:
                if not isinstance(item, Mapping):
                    raise ValueError(
                        "candidate checkpoint scene coverage is invalid"
                    )
                _require_contract_version(
                    item,
                    expected="candidate_scene_coverage.v1",
                    subject="candidate scene coverage",
                )
        if isinstance(stored, list):
            value[field] = tuple(stored)
    return _CANDIDATE_PIPELINE_CHECKPOINT_ADAPTER.validate_python(value)


def candidate_pipeline_checkpoint_digest(
    value: Any,
    *,
    include_checkpoint_id: bool = True,
) -> str:
    """Hash one canonical checkpoint, optionally excluding its claimed ID."""

    checkpoint = parse_candidate_pipeline_checkpoint(value)
    encoded = json.dumps(
        checkpoint.model_dump(
            mode="json",
            exclude=None if include_checkpoint_id else {"checkpoint_id"},
        ),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def candidate_pipeline_checkpoint_ledger_digest(
    values: Sequence[Any],
) -> str:
    """Hash the complete ordered checkpoint ledger used by one finalization."""

    if (
        isinstance(values, (str, bytes))
        or not isinstance(values, Sequence)
        or not 1 <= len(values) <= MAX_CHAPTER_CANDIDATE_PIPELINE_CHECKPOINTS
    ):
        raise ValueError("candidate checkpoint ledger is invalid")
    checkpoints = [
        parse_candidate_pipeline_checkpoint(value).model_dump(mode="json")
        for value in values
    ]
    encoded = json.dumps(
        checkpoints,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def parse_candidate_pipeline_progress(value: Any) -> CandidatePipelineProgressV1:
    """Revalidate the bounded public progress projection without coercion."""

    raw_categories = (
        getattr(value, "outline_issue_categories", None)
        if isinstance(value, BaseModel)
        else value.get("outline_issue_categories")
        if isinstance(value, Mapping)
        else None
    )
    if raw_categories is not None and (
        not isinstance(raw_categories, (list, tuple))
        or len(raw_categories) > 20
    ):
        raise ValueError("candidate progress issue categories are invalid")
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python")
    if not isinstance(value, Mapping):
        return CandidatePipelineProgressV1.model_validate(value)
    value = dict(value)
    _require_contract_version(
        value,
        expected="candidate_pipeline_progress.v1",
        subject="candidate pipeline progress",
    )
    _require_nested_contract_version(
        value,
        field="source",
        expected="candidate_source_identity.v1",
    )
    if isinstance(value.get("outline_issue_categories"), list):
        value["outline_issue_categories"] = tuple(
            value["outline_issue_categories"]
        )
    return CandidatePipelineProgressV1.model_validate(value)


def _validate_checkpoint_list_bounds(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (BaseModel, Mapping)):
        return ()
    bounded_fields: list[str] = []
    for field, limit in _CANDIDATE_CHECKPOINT_LIST_LIMITS.items():
        stored = (
            getattr(value, field, None)
            if isinstance(value, BaseModel)
            else value.get(field)
        )
        if stored is None:
            continue
        if not isinstance(stored, (list, tuple)):
            raise ValueError(f"candidate checkpoint {field} is not a list")
        if len(stored) > limit:
            raise ValueError(
                f"candidate checkpoint {field} exceeds its bounded length"
            )
        bounded_fields.append(field)
    return tuple(bounded_fields)


def _require_contract_version(
    value: Mapping[str, Any],
    *,
    expected: str,
    subject: str,
) -> None:
    version = value.get("schema_version")
    if type(version) is not str or version != expected:
        raise ValueError(f"{subject} schema version is invalid")


def _require_nested_contract_version(
    value: Mapping[str, Any],
    *,
    field: str,
    expected: str,
) -> None:
    nested = value.get(field)
    if not isinstance(nested, Mapping):
        raise ValueError(f"candidate checkpoint {field} is invalid")
    _require_contract_version(
        nested,
        expected=expected,
        subject=f"candidate checkpoint {field}",
    )


def project_state_context(
    *,
    truncated_sections: Sequence[Any],
    dropped_item_counts: Mapping[str, Any],
) -> StateContextProjection:
    """Project typed context metadata without retaining names or prose."""
    dropped = 0
    for value in list(dropped_item_counts.values())[:100]:
        if type(value) is not int or value < 0:
            raise ValueError("state context dropped-item evidence is invalid")
        dropped = min(10_000, dropped + value)
    return StateContextProjection(
        truncated_section_count=min(100, len(truncated_sections)),
        dropped_item_count=dropped,
    )
