"""Small shared contracts for the bounded chapter-candidate repair tail."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from backend.llm.schemas.novel_pydantic import MAX_CHAPTER_OUTLINE_SCENES
from backend.llm.schemas.scene_contract_pydantic import (
    ValidatedChapterOutlineAdherenceEvidenceSchema,
    ValidatedChapterOutlineAdherenceEvidenceV3Schema,
    ValidatedChapterOutlineAdherenceEvidenceV4Schema,
    ValidatedChapterOutlineAdherenceEvidenceV5Schema,
)
from backend.llm.stream_terminal import FinishReason
from backend.scene_contract_versions import (
    LEGACY_OUTLINE_ADHERENCE_EVIDENCE_VERSION,
    LEGACY_LOCAL_OUTLINE_ADHERENCE_EVIDENCE_VERSION,
    LEGACY_OUTLINE_ADHERENCE_ISSUE_POLICY_VERSION,
    CURRENT_OUTLINE_ADHERENCE_POLICIES,
)
from backend.services.generation.outline_adherence import (
    OutlineIssueCategoryValue,
)
from backend.services.generation.prose_completion_contract import (
    completion_allows_formal_write,
)
from backend.services.generation.chapter_repair_policy import (
    RepairComponent,
    RepairComponentUsageV1,
    RepairConvergenceEvidenceV1,
)
from backend.services.generation.chapter_review_policy import ChapterNotReviewedReceipt


MAX_CHAPTER_CANDIDATE_COMPONENT_REPAIRS = 8
MAX_CHAPTER_CANDIDATE_REPAIR_EVENTS = (
    MAX_CHAPTER_CANDIDATE_COMPONENT_REPAIRS * 3
)
# Historical name retained for persisted checkpoint validators. A "cycle" in
# that ledger is now one charged repair event, while each component remains
# independently capped by MAX_CHAPTER_CANDIDATE_COMPONENT_REPAIRS.
MAX_CHAPTER_CANDIDATE_REPAIR_CYCLES = MAX_CHAPTER_CANDIDATE_REPAIR_EVENTS
MAX_CHAPTER_CANDIDATE_PIPELINE_CHECKPOINTS = 64
MAX_CANDIDATE_CHECKPOINT_ATTEMPTS = 512
MAX_CANDIDATE_PIPELINE_PROGRESS_ENTRIES = 10_000
MAX_CANDIDATE_OUTLINE_SCENES = MAX_CHAPTER_OUTLINE_SCENES
MAX_BSON_INT64 = 2**63 - 1
_SAFE_CANDIDATE_IDENTIFIER_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
)
_CANDIDATE_CHECKPOINT_LIST_LIMITS = {
    "attempt_ids": MAX_CANDIDATE_CHECKPOINT_ATTEMPTS,
    "issue_categories": 20,
    "blocking_issue_signatures": 80,
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

    def __init__(
        self,
        message: str,
        *,
        code: str = "candidate_gate_blocked",
        accepted_checkpoints: int = 0,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.accepted_checkpoints = accepted_checkpoints


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


class JobMutationRecoveryBindingV1(BaseModel):
    """Closed Job authority carried into one recoverable formal mutation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["job_mutation_recovery_binding.v1"] = (
        "job_mutation_recovery_binding.v1"
    )
    novel_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    job_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    chapter_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    readiness_digest: str = Field(min_length=1, max_length=128)
    authorization_revision: int = Field(ge=1, le=MAX_BSON_INT64)
    expected_narrative_revision: int = Field(ge=0, le=MAX_BSON_INT64 - 1)
    operation: Literal[
        "accept_chapter_outline",
        "accept_chapter_state",
        "finalize_chapter_generation",
    ]
    idempotency_key: str = Field(min_length=1, max_length=240)


StateDispatchResolutionAction = Literal["retry", "skip", "abort"]
StateDispatchResolutionPhase = Literal[
    "intent",
    "proposal_acknowledged",
    "attempts_acknowledged",
    "proposal_released",
    "job_transitioned",
    "terminal",
]
STATE_DISPATCH_RESOLUTION_PHASES = get_args(StateDispatchResolutionPhase)
STATE_DISPATCH_RESOLUTION_ACTIONS = get_args(StateDispatchResolutionAction)


class StateDispatchResolutionV3(BaseModel):
    """Durable phase receipt for one explicit Job-bound dispatch decision."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["state_dispatch_resolution.v3"]
    binding: JobMutationRecoveryBindingV1
    action: StateDispatchResolutionAction
    phase: StateDispatchResolutionPhase

    @model_validator(mode="after")
    def validate_action_phase(self) -> "StateDispatchResolutionV3":
        if self.phase == "job_transitioned" and self.action != "retry":
            raise ValueError("Only retry may enter the worker launch phase")
        if self.phase == "terminal" and self.action not in {"skip", "abort"}:
            raise ValueError("Only skip or abort may enter the terminal phase")
        return self


class JobMutationReceiptV1(BaseModel):
    """Exact revision receipt used by the Job cursor's atomic rollover."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["job_mutation_receipt.v1"] = (
        "job_mutation_receipt.v1"
    )
    binding: JobMutationRecoveryBindingV1
    next_narrative_revision: int = Field(ge=1, le=MAX_BSON_INT64)

    @model_validator(mode="after")
    def validate_revision_transition(self) -> "JobMutationReceiptV1":
        if (
            self.next_narrative_revision
            != self.binding.expected_narrative_revision + 1
        ):
            raise ValueError("Job mutation receipt revision is invalid")
        return self


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
    scene_coverage_count: int = Field(
        ge=0,
        le=MAX_CANDIDATE_OUTLINE_SCENES,
    )
    consistency_issue_count: int = Field(ge=0, le=20)
    repair_component_usage: tuple[RepairComponentUsageV1, ...] = Field(
        default=(),
        max_length=6,
    )
    repair_convergence: tuple[RepairConvergenceEvidenceV1, ...] = Field(
        default=(),
        max_length=MAX_CHAPTER_CANDIDATE_REPAIR_EVENTS,
    )


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
    schema_version: Literal["chapter_candidate_pipeline_checkpoint.v2"]
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


class _CandidatePipelineCheckpointV3(_CandidatePipelineCheckpointV1):
    schema_version: Literal["chapter_candidate_pipeline_checkpoint.v3"]


class AdherenceCandidateCheckpointV3(_CandidatePipelineCheckpointV3):
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
    validated_evidence: ValidatedChapterOutlineAdherenceEvidenceSchema

    @model_validator(mode="after")
    def validate_evidence_projection(self) -> "AdherenceCandidateCheckpointV3":
        evidence = self.validated_evidence
        if (
            evidence.evidence_schema_version
            != LEGACY_OUTLINE_ADHERENCE_EVIDENCE_VERSION
        ):
            raise ValueError("V2 adherence checkpoint evidence version is invalid")
        categories = tuple(dict.fromkeys(item.category for item in evidence.issues))
        coverage = tuple(
            (item.scene_index, item.status) for item in evidence.scene_coverage
        )
        projected_coverage = tuple(
            (item.scene_index, item.status) for item in self.scene_coverage
        )
        if (
            self.verdict != evidence.verdict
            or self.issue_categories != categories
            or projected_coverage != coverage
            or self.source.source_run_id != evidence.source_prose_run_id
            or self.source.source_run_revision
            != evidence.source_prose_run_revision
            or self.source.source_content_digest != evidence.source_content_digest
        ):
            raise ValueError("V2 adherence checkpoint projection diverged")
        return self


class _CandidatePipelineCheckpointV4(_CandidatePipelineCheckpointV1):
    schema_version: Literal["chapter_candidate_pipeline_checkpoint.v4"]


class AdherenceCandidateCheckpointV4(_CandidatePipelineCheckpointV4):
    """Read-only V3/V1 local-policy checkpoint."""

    kind: Literal["outline_adherence"] = "outline_adherence"
    decision: Literal["pass", "repair", "manual_review"]
    issue_categories: tuple[CandidateOutlineIssueCategory, ...] = Field(
        default=(),
        max_length=20,
    )
    blocking_issue_signatures: tuple[str, ...] = Field(
        default=(),
        max_length=80,
    )
    scene_coverage: tuple[CandidateSceneCoverageV1, ...] = Field(
        default=(),
        max_length=20,
    )
    validated_evidence: ValidatedChapterOutlineAdherenceEvidenceV3Schema

    @model_validator(mode="after")
    def validate_evidence_projection(self) -> "AdherenceCandidateCheckpointV4":
        evidence = self.validated_evidence
        if (
            evidence.evidence_schema_version
            != LEGACY_LOCAL_OUTLINE_ADHERENCE_EVIDENCE_VERSION
        ):
            raise ValueError("V3 adherence checkpoint evidence version is invalid")
        if (
            evidence.issue_policy_version
            != LEGACY_OUTLINE_ADHERENCE_ISSUE_POLICY_VERSION
        ):
            raise ValueError("V3 adherence checkpoint issue policy is invalid")
        blocking_issues = [
            item
            for item in evidence.local_issues
            if item.severity in {"blocker", "major", "unknown"}
        ]
        categories = tuple(
            dict.fromkeys(item.category for item in blocking_issues)
        )
        signatures = tuple(item.issue_signature for item in blocking_issues)
        if any(
            len(signature) != 64
            or any(character not in "0123456789abcdef" for character in signature)
            for signature in self.blocking_issue_signatures
        ):
            raise ValueError("V3 adherence checkpoint issue signature is invalid")
        coverage = tuple(
            (item.scene_index, item.status) for item in evidence.scene_coverage
        )
        projected_coverage = tuple(
            (item.scene_index, item.status) for item in self.scene_coverage
        )
        if (
            self.decision != evidence.decision
            or self.issue_categories != categories
            or self.blocking_issue_signatures != signatures
            or projected_coverage != coverage
            or self.source.source_run_id != evidence.source_prose_run_id
            or self.source.source_run_revision
            != evidence.source_prose_run_revision
            or self.source.source_content_digest != evidence.source_content_digest
        ):
            raise ValueError("V3 adherence checkpoint projection diverged")
        return self


class _CandidatePipelineCheckpointV5(_CandidatePipelineCheckpointV1):
    schema_version: Literal["chapter_candidate_pipeline_checkpoint.v5"]


class AdherenceCandidateCheckpointV5(_CandidatePipelineCheckpointV5):
    """Checkpoint envelope with explicitly versioned V4 or V5 evidence."""

    kind: Literal["outline_adherence"] = "outline_adherence"
    decision: Literal["pass", "repair", "manual_review"]
    issue_categories: tuple[CandidateOutlineIssueCategory, ...] = Field(
        default=(),
        max_length=20,
    )
    blocking_issue_signatures: tuple[str, ...] = Field(
        default=(),
        max_length=80,
    )
    scene_coverage: tuple[CandidateSceneCoverageV1, ...] = Field(
        default=(),
        max_length=20,
    )
    validated_evidence: (
        ValidatedChapterOutlineAdherenceEvidenceV4Schema
        | ValidatedChapterOutlineAdherenceEvidenceV5Schema
    ) = Field(discriminator="evidence_schema_version")

    @model_validator(mode="after")
    def validate_evidence_projection(self) -> "AdherenceCandidateCheckpointV5":
        evidence = self.validated_evidence
        if evidence.evidence_schema_version not in CURRENT_OUTLINE_ADHERENCE_POLICIES:
            raise ValueError("current adherence checkpoint evidence version is invalid")
        if evidence.issue_policy_version != CURRENT_OUTLINE_ADHERENCE_POLICIES[
            evidence.evidence_schema_version
        ]:
            raise ValueError("current adherence checkpoint issue policy is invalid")
        blocking_issues = [
            item
            for item in evidence.local_issues
            if item.severity in {"blocker", "major", "unknown"}
        ]
        categories = tuple(
            dict.fromkeys(item.category for item in blocking_issues)
        )
        signatures = tuple(item.issue_signature for item in blocking_issues)
        if any(
            len(signature) != 64
            or any(character not in "0123456789abcdef" for character in signature)
            for signature in self.blocking_issue_signatures
        ):
            raise ValueError("V4 adherence checkpoint issue signature is invalid")
        coverage = tuple(
            (item.scene_index, item.status) for item in evidence.scene_coverage
        )
        projected_coverage = tuple(
            (item.scene_index, item.status) for item in self.scene_coverage
        )
        if (
            self.decision != evidence.decision
            or self.issue_categories != categories
            or self.blocking_issue_signatures != signatures
            or projected_coverage != coverage
            or self.source.source_run_id != evidence.source_prose_run_id
            or self.source.source_run_revision
            != evidence.source_prose_run_revision
            or self.source.source_content_digest != evidence.source_content_digest
        ):
            raise ValueError("V4 adherence checkpoint projection diverged")
        return self


class AdherenceNotReviewedCheckpointV6(_CandidatePipelineCheckpointV1):
    """A zero-call policy receipt, distinct from a passing semantic review."""

    schema_version: Literal["chapter_candidate_pipeline_checkpoint.v6"]
    kind: Literal["outline_adherence"] = "outline_adherence"
    validated_evidence: ChapterNotReviewedReceipt

    @model_validator(mode="after")
    def validate_receipt(self) -> "AdherenceNotReviewedCheckpointV6":
        receipt = self.validated_evidence
        if (
            self.attempt_ids or self.truncation.truncated_section_count or self.truncation.dropped_item_count
            or receipt.chapter_id != self.chapter_id
            or receipt.source_prose_run_id != self.source.source_run_id
            or receipt.source_prose_run_revision != self.source.source_run_revision
            or receipt.source_content_digest != self.source.source_content_digest
        ):
            raise ValueError("not-reviewed checkpoint differs from its zero-call source receipt")
        return self


class StateCandidateCheckpointV1(_CandidatePipelineCheckpointV1):
    kind: Literal["state_candidate"] = "state_candidate"
    origin: Literal["initial", "repair"]
    proposal_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    request_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    consistency_issue_count: int = Field(ge=0, le=20)
    dropped_reference_count: int = Field(default=0, ge=0, le=1_000)

    @model_validator(mode="after")
    def validate_origin_cycle(self) -> "StateCandidateCheckpointV1":
        if (self.origin == "initial") != (self.cycle == 0):
            raise ValueError("state checkpoint origin and cycle diverged")
        return self


class StateCandidateCheckpointV3(_CandidatePipelineCheckpointV3):
    """Metadata-only projection of one locally computed fact accounting."""

    kind: Literal["state_candidate"] = "state_candidate"
    origin: Literal["initial", "repair"]
    proposal_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    request_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    consistency_issue_count: int = Field(ge=0, le=20)
    dropped_reference_count: int = Field(default=0, ge=0, le=1_000)
    fact_accounting_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    unaccounted_canonical_fact_count: int = Field(ge=0, le=1_000)
    invalid_internal_reference_count: int = Field(ge=0, le=1_000)
    dangling_reference_count: int = Field(ge=0, le=1_000)
    extraction_failure_count: int = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_origin_cycle(self) -> "StateCandidateCheckpointV3":
        if (self.origin == "initial") != (self.cycle == 0):
            raise ValueError("state checkpoint origin and cycle diverged")
        return self


AdherenceCandidateCheckpoint = (
    AdherenceCandidateCheckpointV1
    | AdherenceCandidateCheckpointV3
    | AdherenceCandidateCheckpointV4
    | AdherenceCandidateCheckpointV5
    | AdherenceNotReviewedCheckpointV6
)
StateCandidateCheckpoint = StateCandidateCheckpointV1 | StateCandidateCheckpointV3


CandidatePipelineCheckpointV1 = (
    ProseCandidateCheckpointV1
    | AdherenceCandidateCheckpointV1
    | AdherenceCandidateCheckpointV3
    | AdherenceCandidateCheckpointV4
    | AdherenceCandidateCheckpointV5
    | AdherenceNotReviewedCheckpointV6
    | StateCandidateCheckpointV1
    | StateCandidateCheckpointV3
)


@dataclass(frozen=True)
class CandidatePipelineCompletionEvidenceV1:
    """Canonical terminal projection derived from one checkpoint ledger."""

    prose: ProseCandidateCheckpointV1
    adherence: AdherenceCandidateCheckpoint
    state: StateCandidateCheckpoint
    repair_cycles_used: int
    attempt_count: int
    truncation_count: int


@dataclass(frozen=True)
class CandidatePipelineReplayV1:
    """Canonical prefix state shared by live recovery and terminal publish."""

    checkpoints: tuple[CandidatePipelineCheckpointV1, ...]
    current_prose: ProseCandidateCheckpointV1
    previous_prose: ProseCandidateCheckpointV1 | None
    latest_adherence: AdherenceCandidateCheckpoint | None
    latest_state: StateCandidateCheckpoint | None
    phase: Literal["prose", "adherence", "state"]
    repair_cycles_used: int
    review_count: int
    attempt_ids: tuple[str, ...]
    truncation_count: int
    completed_steps: tuple[str, ...]
    truncations: tuple[tuple[str, int, int], ...]


def candidate_checkpoint_completion_passed(
    checkpoint: ProseCandidateCheckpointV1,
) -> bool:
    return completion_allows_formal_write(
        status=checkpoint.completion.status,
        can_write_formal_prose=checkpoint.completion.can_write_formal_prose,
        finish_reason=checkpoint.completion.finish_reason,
    )


def candidate_checkpoint_adherence_passed(
    checkpoint: AdherenceCandidateCheckpoint,
    *,
    expected_scene_count: int,
) -> bool:
    if isinstance(checkpoint, AdherenceNotReviewedCheckpointV6):
        return False
    indexes = tuple(item.scene_index for item in checkpoint.scene_coverage)
    policy_passed = (
        checkpoint.decision == "pass"
        and not checkpoint.blocking_issue_signatures
        if isinstance(
            checkpoint,
            (AdherenceCandidateCheckpointV4, AdherenceCandidateCheckpointV5),
        )
        else checkpoint.verdict == "pass"
    )
    return bool(
        type(expected_scene_count) is int
        and 1 <= expected_scene_count <= MAX_CANDIDATE_OUTLINE_SCENES
        and policy_passed
        and not checkpoint.issue_categories
        and len(indexes) == expected_scene_count
        and indexes == tuple(range(1, expected_scene_count + 1))
        and all(item.status == "covered" for item in checkpoint.scene_coverage)
    )


def candidate_checkpoint_review_requirement_satisfied(
    checkpoint: AdherenceCandidateCheckpoint, *, expected_scene_count: int,
) -> bool:
    # Live execution and finalization separately rebind this receipt to the
    # frozen server authorization; the reducer only validates ledger order.
    if isinstance(checkpoint, AdherenceNotReviewedCheckpointV6):
        return type(expected_scene_count) is int and 1 <= expected_scene_count <= MAX_CANDIDATE_OUTLINE_SCENES
    return candidate_checkpoint_adherence_passed(checkpoint, expected_scene_count=expected_scene_count)


def candidate_checkpoint_state_passed(
    checkpoint: StateCandidateCheckpoint,
) -> bool:
    if isinstance(checkpoint, StateCandidateCheckpointV3):
        return bool(
            checkpoint.consistency_issue_count == 0
            and checkpoint.dropped_reference_count == 0
            and checkpoint.unaccounted_canonical_fact_count == 0
            and checkpoint.invalid_internal_reference_count == 0
            and checkpoint.dangling_reference_count == 0
            and checkpoint.extraction_failure_count == 0
        )
    return bool(
        checkpoint.consistency_issue_count == 0
        and checkpoint.dropped_reference_count == 0
    )


def replay_candidate_pipeline_checkpoints(
    values: Sequence[Any],
    *,
    chapter_id: str,
    expected_scene_count: int,
    max_repair_cycles: int,
    require_terminal: bool = False,
) -> CandidatePipelineReplayV1:
    """Replay one bounded checkpoint prefix with the canonical gate reducer."""

    if (
        isinstance(values, (str, bytes))
        or not isinstance(values, Sequence)
        or not values
        or (require_terminal and len(values) < 3)
    ):
        raise CandidatePipelineCheckpointConflict(
            "Candidate pipeline completion ledger is incomplete"
        )
    if (
        len(values) > MAX_CHAPTER_CANDIDATE_PIPELINE_CHECKPOINTS
        or not is_safe_candidate_identifier(chapter_id, maximum=24)
        or type(max_repair_cycles) is not int
        or not 0
        <= max_repair_cycles
        <= MAX_CHAPTER_CANDIDATE_REPAIR_CYCLES
    ):
        raise CandidatePipelineCheckpointConflict(
            "Candidate pipeline completion ledger is invalid"
        )
    try:
        checkpoints = tuple(
            parse_candidate_pipeline_checkpoint(value) for value in values
        )
    except Exception as exc:
        raise CandidatePipelineCheckpointConflict(
            "Candidate pipeline completion ledger is invalid"
        ) from exc

    current_prose: ProseCandidateCheckpointV1 | None = None
    previous_prose: ProseCandidateCheckpointV1 | None = None
    latest_adherence: AdherenceCandidateCheckpoint | None = None
    latest_state: StateCandidateCheckpoint | None = None
    phase: Literal["start", "prose", "adherence", "state"] = "start"
    repair_cycles_used = 0
    review_count = 0
    seen_checkpoint_ids: set[str] = set()
    seen_attempt_ids: set[str] = set()
    ordered_attempt_ids: list[str] = []
    seen_state_request_ids: set[str] = set()
    seen_state_proposal_ids: set[str] = set()
    truncation_count = 0
    completed_steps: list[str] = []
    truncations: list[tuple[str, int, int]] = []
    accepted_checkpoints = 0

    def record_step(
        step: str,
        checkpoint: CandidatePipelineCheckpointV1,
    ) -> None:
        nonlocal truncation_count
        completed_steps.append(step)
        truncated = checkpoint.truncation.truncated_section_count
        dropped = checkpoint.truncation.dropped_item_count
        if truncated or dropped:
            truncation_count += 1
            truncations.append((step, truncated, dropped))

    def diverged(
        message: str = "Candidate pipeline completion gates diverged",
        *,
        code: str = "candidate_gate_blocked",
        include_current: bool = False,
    ) -> CandidatePipelineCheckpointConflict:
        return CandidatePipelineCheckpointConflict(
            message,
            code=code,
            accepted_checkpoints=(
                accepted_checkpoints + 1
                if include_current
                else accepted_checkpoints
            ),
        )

    for sequence, checkpoint in enumerate(checkpoints, start=1):
        if (
            checkpoint.chapter_id != chapter_id
            or checkpoint.sequence != sequence
            or checkpoint.checkpoint_id in seen_checkpoint_ids
        ):
            raise diverged(
                "候选管线恢复检查点顺序或章节身份无效"
            )
        if any(
            attempt_id in seen_attempt_ids
            for attempt_id in checkpoint.attempt_ids
        ):
            raise diverged("候选管线恢复调用与检查点不一致")
        seen_checkpoint_ids.add(checkpoint.checkpoint_id)
        seen_attempt_ids.update(checkpoint.attempt_ids)
        ordered_attempt_ids.extend(checkpoint.attempt_ids)
        if isinstance(checkpoint, ProseCandidateCheckpointV1):
            if checkpoint.origin == "initial":
                if phase != "start":
                    raise diverged()
            else:
                kept_digest = bool(
                    current_prose is not None
                    and current_prose.origin == "repair"
                    and previous_prose is not None
                    and current_prose.source.source_content_digest
                    == previous_prose.source.source_content_digest
                )
                prior_gate_passed = bool(
                    phase == "prose"
                    and current_prose is not None
                    and candidate_checkpoint_completion_passed(current_prose)
                ) or bool(
                    phase == "adherence"
                    and latest_adherence is not None
                    and candidate_checkpoint_review_requirement_satisfied(
                        latest_adherence,
                        expected_scene_count=expected_scene_count,
                    )
                )
                if (
                    phase not in {"prose", "adherence"}
                    or current_prose is None
                    or prior_gate_passed
                    or kept_digest
                    or checkpoint.cycle != repair_cycles_used + 1
                    or checkpoint.source.source_run_id
                    != current_prose.source.source_run_id
                ):
                    raise diverged(
                        message=(
                            "正文摘要未变化且章纲复检仍未通过"
                            if kept_digest and phase == "adherence"
                            else (
                                "正文摘要未变化且完成闸门复检仍未通过"
                                if kept_digest
                                else "Candidate pipeline completion gates diverged"
                            )
                        ),
                        code=(
                            "repair_no_progress"
                            if kept_digest
                            else "candidate_gate_blocked"
                        )
                    )
                if (
                    checkpoint.source.source_run_revision
                    <= current_prose.source.source_run_revision
                ):
                    raise diverged(
                        "正文修复没有产生新候选",
                        code="repair_no_progress",
                        include_current=True,
                    )
                repair_cycles_used = checkpoint.cycle
                if repair_cycles_used > max_repair_cycles:
                    raise diverged()
            previous_prose = current_prose
            current_prose = checkpoint
            latest_adherence = None
            latest_state = None
            phase = "prose"
            record_step(
                "prose"
                if checkpoint.origin == "initial"
                else f"prose_repair_{checkpoint.cycle}",
                checkpoint,
            )
            accepted_checkpoints = sequence
            continue

        if isinstance(
            checkpoint,
            (
                AdherenceCandidateCheckpointV1,
                AdherenceCandidateCheckpointV3,
                AdherenceCandidateCheckpointV4,
                AdherenceCandidateCheckpointV5,
                AdherenceNotReviewedCheckpointV6,
            ),
        ):
            if (
                phase != "prose"
                or current_prose is None
                or not candidate_checkpoint_completion_passed(current_prose)
                or checkpoint.source != current_prose.source
                or checkpoint.cycle != current_prose.cycle
            ):
                raise diverged()
            review_count += 1
            latest_adherence = checkpoint
            latest_state = None
            phase = "adherence"
            record_step(
                "review_not_requested" if isinstance(checkpoint, AdherenceNotReviewedCheckpointV6) else "outline_adherence"
                if review_count == 1
                else f"outline_adherence_recheck_{review_count}",
                checkpoint,
            )
            accepted_checkpoints = sequence
            continue

        if (
            current_prose is None
            or checkpoint.source != current_prose.source
        ):
            raise diverged("候选管线恢复状态正文身份无效")
        if checkpoint.request_id in seen_state_request_ids:
            raise diverged("候选管线恢复状态修复身份重复")
        if checkpoint.origin == "initial":
            if (
                phase != "adherence"
                or latest_adherence is None
                or checkpoint.proposal_id in seen_state_proposal_ids
                or not candidate_checkpoint_review_requirement_satisfied(
                    latest_adherence,
                    expected_scene_count=expected_scene_count,
                )
            ):
                raise diverged("候选管线恢复状态前置步骤无效")
        elif (
            phase != "state"
            or latest_state is None
            or (
                candidate_checkpoint_state_passed(latest_state)
            )
            or checkpoint.cycle != repair_cycles_used + 1
            or checkpoint.proposal_id == latest_state.proposal_id
        ):
            raise diverged(
                message=(
                    "状态修复没有产生新候选"
                    if (
                        latest_state is not None
                        and checkpoint.proposal_id
                        == latest_state.proposal_id
                    )
                    else "Candidate pipeline completion gates diverged"
                ),
                code=(
                    "repair_no_progress"
                    if (
                        latest_state is not None
                        and checkpoint.proposal_id
                        == latest_state.proposal_id
                    )
                    else "candidate_gate_blocked"
                ),
                include_current=(
                    latest_state is not None
                    and checkpoint.proposal_id == latest_state.proposal_id
                ),
            )
        else:
            if checkpoint.proposal_id in seen_state_proposal_ids:
                raise diverged("候选管线恢复状态修复身份重复")
            repair_cycles_used = checkpoint.cycle
            if repair_cycles_used > max_repair_cycles:
                raise diverged()
        seen_state_request_ids.add(checkpoint.request_id)
        seen_state_proposal_ids.add(checkpoint.proposal_id)
        latest_state = checkpoint
        phase = "state"
        record_step(
            "state"
            if checkpoint.origin == "initial"
            else f"state_repair_{checkpoint.cycle}",
            checkpoint,
        )
        accepted_checkpoints = sequence

    if require_terminal and (
        phase != "state"
        or current_prose is None
        or latest_adherence is None
        or latest_state is None
        or not isinstance(latest_state, StateCandidateCheckpointV3)
        or not candidate_checkpoint_completion_passed(current_prose)
        or not candidate_checkpoint_review_requirement_satisfied(
            latest_adherence,
            expected_scene_count=expected_scene_count,
        )
        or not candidate_checkpoint_state_passed(latest_state)
        or repair_cycles_used > max_repair_cycles
    ):
        raise diverged()
    if current_prose is None or phase == "start":
        raise CandidatePipelineCheckpointConflict(
            "Candidate pipeline completion ledger is incomplete",
            accepted_checkpoints=accepted_checkpoints,
        )
    return CandidatePipelineReplayV1(
        checkpoints=checkpoints,
        current_prose=current_prose,
        previous_prose=previous_prose,
        latest_adherence=latest_adherence,
        latest_state=latest_state,
        phase=phase,
        repair_cycles_used=repair_cycles_used,
        review_count=review_count,
        attempt_ids=tuple(ordered_attempt_ids),
        truncation_count=truncation_count,
        completed_steps=tuple(completed_steps),
        truncations=tuple(truncations),
    )


def validate_candidate_pipeline_completion_chain(
    values: Sequence[Any],
    *,
    chapter_id: str,
    expected_scene_count: int,
    max_repair_cycles: int,
) -> CandidatePipelineCompletionEvidenceV1:
    """Replay the bounded terminal chain before publishing committed progress."""

    try:
        replay = replay_candidate_pipeline_checkpoints(
            values,
            chapter_id=chapter_id,
            expected_scene_count=expected_scene_count,
            max_repair_cycles=max_repair_cycles,
            require_terminal=True,
        )
    except CandidatePipelineCheckpointConflict as exc:
        if str(exc).startswith("Candidate pipeline completion ledger"):
            raise
        raise CandidatePipelineCheckpointConflict(
            "Candidate pipeline completion gates diverged",
            code=exc.code,
            accepted_checkpoints=exc.accepted_checkpoints,
        ) from exc
    assert replay.latest_adherence is not None
    assert replay.latest_state is not None
    return CandidatePipelineCompletionEvidenceV1(
        prose=replay.current_prose,
        adherence=replay.latest_adherence,
        state=replay.latest_state,
        repair_cycles_used=replay.repair_cycles_used,
        attempt_count=len(replay.attempt_ids),
        truncation_count=replay.truncation_count,
    )


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
    checkpoint_version = value.get("schema_version")
    if checkpoint_version not in {
        "chapter_candidate_pipeline_checkpoint.v2",
        "chapter_candidate_pipeline_checkpoint.v3",
        "chapter_candidate_pipeline_checkpoint.v4",
        "chapter_candidate_pipeline_checkpoint.v5",
        "chapter_candidate_pipeline_checkpoint.v6",
    }:
        raise ValueError("candidate checkpoint schema_version is invalid")
    if (
        checkpoint_version == "chapter_candidate_pipeline_checkpoint.v3"
        and value.get("kind") not in {"outline_adherence", "state_candidate"}
    ):
        raise ValueError("candidate checkpoint v3 kind is invalid")
    if (
        checkpoint_version == "chapter_candidate_pipeline_checkpoint.v4"
        and value.get("kind") != "outline_adherence"
    ):
        raise ValueError("candidate checkpoint v4 kind is invalid")
    if (
        checkpoint_version in {"chapter_candidate_pipeline_checkpoint.v5", "chapter_candidate_pipeline_checkpoint.v6"}
        and value.get("kind") != "outline_adherence"
    ):
        raise ValueError("candidate checkpoint v5 kind is invalid")
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
    raw_usage = value.get("repair_component_usage")
    if raw_usage is not None:
        if not isinstance(raw_usage, (list, tuple)) or len(raw_usage) > 6:
            raise ValueError("candidate repair component usage is invalid")
        normalized_usage: list[RepairComponentUsageV1] = []
        for raw_item in raw_usage:
            if isinstance(raw_item, BaseModel):
                raw_item = raw_item.model_dump(mode="python")
            if not isinstance(raw_item, Mapping):
                raise ValueError(
                    "candidate repair component usage item is invalid"
                )
            item = dict(raw_item)
            item["component"] = RepairComponent(item.get("component"))
            normalized_usage.append(
                RepairComponentUsageV1.model_validate(item)
            )
        value["repair_component_usage"] = tuple(normalized_usage)
    raw_convergence = value.get("repair_convergence")
    if raw_convergence is not None:
        if (
            not isinstance(raw_convergence, (list, tuple))
            or len(raw_convergence) > MAX_CHAPTER_CANDIDATE_REPAIR_EVENTS
        ):
            raise ValueError("candidate repair convergence is invalid")
        normalized_convergence: list[RepairConvergenceEvidenceV1] = []
        tuple_fields = (
            "target_issue_signatures",
            "remaining_target_issue_signatures",
            "resolved_issue_signatures",
            "introduced_issue_signatures",
            "regressed_issue_signatures",
            "introduced_blocker_signatures",
            "reason_codes",
            "prose_run_revision_sequence",
            "content_digest_sequence",
        )
        for raw_item in raw_convergence:
            if isinstance(raw_item, BaseModel):
                raw_item = raw_item.model_dump(mode="python")
            if not isinstance(raw_item, Mapping):
                raise ValueError("candidate repair convergence item is invalid")
            item = dict(raw_item)
            raw_charge = item.get("charge")
            if isinstance(raw_charge, Mapping):
                charge = dict(raw_charge)
                charge["component"] = RepairComponent(
                    charge.get("component")
                )
                item["charge"] = charge
            for field in tuple_fields:
                field_value = item.get(field)
                if isinstance(field_value, list):
                    item[field] = tuple(field_value)
            normalized_convergence.append(
                RepairConvergenceEvidenceV1.model_validate(item)
            )
        value["repair_convergence"] = tuple(normalized_convergence)
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
