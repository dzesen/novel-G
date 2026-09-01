"""Bounded successor Module from reviewed prose to a deferred state candidate.

This Module consumes ``required_reviewed_chapter_candidate.v1`` through a new
readiness and Job authority.  It performs one initial extraction plus at most
two state-only re-extractions, proves consistency and fact accounting, and
stops before every formal prose/state mutation.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import re
from typing import Any, Awaitable, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from backend.services.generation.attempt_scope import (
    JobAttemptScope,
    project_persisted_attempt_evidence,
)
from backend.services.generation.chapter_generation_application import (
    ChapterGenerationResult,
    ChapterGenerationStage,
    ProseCandidateSource,
    STATE_STEP,
    STATE_WORKFLOW,
    StateRepairGuidance,
)
from backend.services.generation.provider_budget import (
    scale_provider_bounds,
    structured_call_budget,
)
from backend.services.generation.required_chapter_review_job import (
    REQUIRED_REVIEWED_CANDIDATE_PAUSE_REASON,
    RequiredGenerationPlanSnapshot,
    RequiredProviderBound,
    RequiredReviewedChapterCandidate,
    parse_required_reviewed_candidate,
    validate_required_chapter_review_readiness,
    validate_required_reviewed_candidate_job,
)
from backend.services.generation.required_chapter_state_contracts import (
    RequiredStateGenerationBinding,
    required_state_digest,
)
from backend.services.generation.state_repair_contracts import (
    MAX_STATE_REPAIR_CARD_ID_LENGTH,
    MAX_STATE_REPAIR_CARD_IDS,
    MAX_STATE_REPAIR_DROPPED_REFERENCES,
    StateRepairDirective,
)
from backend.services.novel.state_completion import chapter_content_digest
from backend.services.novel.state_fact_accounting import (
    StateFactAccountingError,
    automatic_state_fact_decision,
)
from backend.services.llm.generation_runtime import GenerationPlan


REQUIRED_CHAPTER_STATE_PIPELINE_REVISION = "required-chapter-state-job-r1"
REQUIRED_CHAPTER_STATE_PLANNING_KEY = "required_chapter_state_authorization"
REQUIRED_CHAPTER_STATE_REVISION_KEY = "required_chapter_state_pipeline_revision"
REQUIRED_STATE_CANDIDATE_ACKNOWLEDGEMENT = (
    "successor_state_stops_before_formal_commit"
)
REQUIRED_STATE_CANDIDATE_PAUSE_REASON = "required_state_candidate_ready"
REQUIRED_STATE_STEP_PREFIX = "required-state-candidate:"
MAX_REQUIRED_STATE_REEXTRACTIONS = 2
MAX_REQUIRED_STATE_CALLS = 1 + MAX_REQUIRED_STATE_REEXTRACTIONS

_SHA256 = r"^[0-9a-f]{64}$"
_OBJECT_ID = r"^[0-9a-f]{24}$"
_SAFE_REASON = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_MAX = 2**63 - 1
_STATE_TARGET = (STATE_WORKFLOW, STATE_STEP)
_PLANNING_KEYS = frozenset({
    REQUIRED_CHAPTER_STATE_PLANNING_KEY,
    REQUIRED_CHAPTER_STATE_REVISION_KEY,
})
_FORBIDDEN_AUTHORITY_KEYS = frozenset({
    "chapter_finalization_authorization",
    "prose_continuation_authorization",
    "chapter_candidate_job_execution_authorization",
    "chapter_candidate_repair_authorization",
    "required_chapter_review_authorization",
})


class RequiredChapterStateJobConflict(ValueError):
    """A state successor authority, journal, result, or source diverged."""


class RequiredStateDispatchRejected(RequiredChapterStateJobConflict):
    provider_request_not_dispatched = True


class _Closed(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


class RequiredChapterStateAuthorization(_Closed):
    schema_version: Literal["required_chapter_state_job_authorization.v1"] = (
        "required_chapter_state_job_authorization.v1"
    )
    protocol_revision: Literal["required-chapter-state-job-r1"] = (
        REQUIRED_CHAPTER_STATE_PIPELINE_REVISION
    )
    contract_digest: str = Field(pattern=_SHA256)
    novel_id: str = Field(pattern=_OBJECT_ID)
    owner_id: str = Field(pattern=_OBJECT_ID)
    chapter_id: str = Field(pattern=_OBJECT_ID)
    volume_id: str = Field(pattern=_OBJECT_ID)
    authorization_revision: int = Field(ge=1, le=_MAX)
    narrative_revision: int = Field(ge=0, le=_MAX)
    outline_revision: str = Field(pattern=_SHA256)
    created_at: datetime
    deadline_at: datetime
    predecessor_candidate: RequiredReviewedChapterCandidate
    state_generation: RequiredGenerationPlanSnapshot
    maximum_provider_attempts_per_call: int = Field(ge=1, le=2)
    maximum_tokens_per_call: int = Field(ge=1, le=_MAX)
    maximum_provider_attempts_total: int = Field(ge=1, le=_MAX)
    maximum_tokens_total: int = Field(ge=1, le=_MAX)
    maximum_serial_seconds_total: int = Field(ge=1, le=_MAX)
    token_budget: int = Field(ge=1, le=_MAX)
    provider_bounds: list[RequiredProviderBound] = Field(min_length=1, max_length=8)
    max_state_reextractions: Literal[2] = MAX_REQUIRED_STATE_REEXTRACTIONS
    can_write_formal_prose: Literal[False] = False
    can_accept_formal_state: Literal[False] = False

    @model_validator(mode="after")
    def validate_authority(self) -> "RequiredChapterStateAuthorization":
        predecessor = self.predecessor_candidate
        plan = self.state_generation.thaw()
        budget = structured_call_budget(plan)
        expected_bounds = scale_provider_bounds(
            budget.provider_bounds,
            MAX_REQUIRED_STATE_CALLS,
        )
        expected_provider_bounds = [
            RequiredProviderBound(
                provider_alias=item.provider_alias,
                maximum_paid_attempts_total=item.paid_attempts,
                maximum_tokens_total=item.tokens,
            )
            for item in expected_bounds
        ]
        if (
            self.created_at.tzinfo is None
            or self.deadline_at.tzinfo is None
            or self.deadline_at
            < self.created_at + timedelta(seconds=self.maximum_serial_seconds_total)
            or predecessor.owner_id != self.owner_id
            or predecessor.novel_id != self.novel_id
            or predecessor.chapter_id != self.chapter_id
            or predecessor.narrative_revision != self.narrative_revision
            or predecessor.status != "reviewed"
            or predecessor.decision != "pass"
            or predecessor.next_step != "state_candidate"
            or predecessor.can_write_formal_prose is not False
            or predecessor.can_generate_state is not False
            or self.maximum_provider_attempts_per_call
            != budget.max_paid_attempts
            or self.maximum_tokens_per_call != budget.max_tokens_per_call
            or self.maximum_provider_attempts_total
            != budget.max_paid_attempts * MAX_REQUIRED_STATE_CALLS
            or self.maximum_tokens_total
            != budget.max_tokens_per_call * MAX_REQUIRED_STATE_CALLS
            or self.maximum_serial_seconds_total
            != plan.timeout_seconds * MAX_REQUIRED_STATE_CALLS
            or self.token_budget < self.maximum_tokens_total
            or self.provider_bounds != expected_provider_bounds
        ):
            raise ValueError("required_chapter_state_authorization_invalid")
        identity = self.model_dump(mode="python", exclude={"contract_digest"})
        if required_state_digest(identity) != self.contract_digest:
            raise ValueError("required_chapter_state_contract_digest_changed")
        return self

    def plan(self) -> GenerationPlan:
        return self.state_generation.thaw()


class RequiredStateCandidateRequest(_Closed):
    schema_version: Literal["required_state_candidate_request.v1"] = (
        "required_state_candidate_request.v1"
    )
    binding: RequiredStateGenerationBinding
    prior_proposal_id: str | None = Field(default=None, pattern=_OBJECT_ID)
    repair_directive: StateRepairDirective | None = None

    @model_validator(mode="after")
    def validate_request(self) -> "RequiredStateCandidateRequest":
        if (self.binding.ordinal == 0) != (
            self.prior_proposal_id is None and self.repair_directive is None
        ):
            raise ValueError("required_state_candidate_request_shape_invalid")
        if self.repair_directive is not None and (
            self.repair_directive.cycle != self.binding.ordinal
        ):
            raise ValueError("required_state_candidate_repair_cycle_changed")
        identity = required_state_request_identity(
            ordinal=self.binding.ordinal,
            source_run_id=self.binding.source_run_id,
            source_run_revision=self.binding.source_run_revision,
            source_content_digest=self.binding.source_content_digest,
            predecessor_result_digest=self.binding.predecessor_result_digest,
            prior_proposal_id=self.prior_proposal_id,
            repair_directive=self.repair_directive,
        )
        if required_state_digest(identity) != self.binding.request_digest:
            raise ValueError("required_state_candidate_request_digest_changed")
        return self

    @property
    def step_id(self) -> str:
        return (
            f"{REQUIRED_STATE_STEP_PREFIX}{self.binding.ordinal}:"
            f"{self.binding.request_digest}"
        )


class RequiredStateCandidateObservation(_Closed):
    schema_version: Literal["required_state_candidate_observation.v1"] = (
        "required_state_candidate_observation.v1"
    )
    proposal_id: str = Field(pattern=_OBJECT_ID)
    candidate_digest: str = Field(pattern=_SHA256)
    consistency_issue_count: int = Field(ge=0, le=MAX_STATE_REPAIR_CARD_IDS)
    affected_card_ids: tuple[str, ...] = Field(max_length=MAX_STATE_REPAIR_CARD_IDS)
    dropped_reference_count: int = Field(
        ge=0,
        le=MAX_STATE_REPAIR_DROPPED_REFERENCES,
    )
    fact_accounting_digest: str = Field(pattern=_SHA256)
    canonical_fact_count: int = Field(ge=0, le=MAX_STATE_REPAIR_DROPPED_REFERENCES)
    unaccounted_canonical_fact_count: int = Field(
        ge=0,
        le=MAX_STATE_REPAIR_DROPPED_REFERENCES,
    )
    invalid_internal_reference_count: int = Field(
        ge=0,
        le=MAX_STATE_REPAIR_DROPPED_REFERENCES,
    )
    dangling_reference_count: int = Field(
        ge=0,
        le=MAX_STATE_REPAIR_DROPPED_REFERENCES,
    )
    extraction_failure_count: int = Field(ge=0, le=1)
    truncated_section_count: int = Field(ge=0, le=100)
    dropped_item_count: int = Field(ge=0, le=10_000)
    attempt_ids: tuple[str, ...] = Field(min_length=1, max_length=2)
    gate_passed: bool

    @model_validator(mode="after")
    def validate_gate(self) -> "RequiredStateCandidateObservation":
        if len(set(self.affected_card_ids)) != len(self.affected_card_ids):
            raise ValueError("required_state_candidate_card_ids_duplicated")
        if any(
            not item or len(item) > MAX_STATE_REPAIR_CARD_ID_LENGTH
            for item in self.affected_card_ids
        ):
            raise ValueError("required_state_candidate_card_ids_invalid")
        expected = (
            self.consistency_issue_count == 0
            and self.dropped_reference_count == 0
            and self.unaccounted_canonical_fact_count == 0
            and self.invalid_internal_reference_count == 0
            and self.dangling_reference_count == 0
            and self.extraction_failure_count == 0
        )
        if self.gate_passed != expected:
            raise ValueError("required_state_candidate_gate_projection_changed")
        return self


class RequiredStateCandidate(_Closed):
    """Metadata-only handoff; the proposal and token stay in their Module."""

    schema_version: Literal["required_state_candidate.v1"] = (
        "required_state_candidate.v1"
    )
    status: Literal["consistent"] = "consistent"
    result_digest: str = Field(pattern=_SHA256)
    job_id: str = Field(pattern=_OBJECT_ID)
    owner_id: str = Field(pattern=_OBJECT_ID)
    novel_id: str = Field(pattern=_OBJECT_ID)
    chapter_id: str = Field(pattern=_OBJECT_ID)
    readiness_digest: str = Field(pattern=_SHA256)
    authorization_revision: int = Field(ge=1, le=_MAX)
    authorization_contract_digest: str = Field(pattern=_SHA256)
    narrative_revision: int = Field(ge=0, le=_MAX)
    predecessor_job_id: str = Field(pattern=_OBJECT_ID)
    predecessor_result_digest: str = Field(pattern=_SHA256)
    source_run_id: str = Field(pattern=_OBJECT_ID)
    source_run_revision: int = Field(ge=2, le=_MAX)
    source_content_digest: str = Field(pattern=_SHA256)
    state_proposal_id: str = Field(pattern=_OBJECT_ID)
    state_candidate_digest: str = Field(pattern=_SHA256)
    state_journal_digest: str = Field(pattern=_SHA256)
    fact_accounting_digest: str = Field(pattern=_SHA256)
    canonical_fact_count: int = Field(ge=0, le=MAX_STATE_REPAIR_DROPPED_REFERENCES)
    reextraction_count: int = Field(ge=0, le=2)
    gate_passed: Literal[True] = True
    next_step: Literal["chapter_finalization"] = "chapter_finalization"
    can_write_formal_prose: Literal[False] = False
    can_accept_formal_state: Literal[False] = False

    @model_validator(mode="after")
    def validate_result(self) -> "RequiredStateCandidate":
        identity = self.model_dump(mode="python", exclude={"result_digest"})
        if required_state_digest(identity) != self.result_digest:
            raise ValueError("required_state_candidate_result_digest_changed")
        return self


@dataclass(frozen=True)
class RequiredChapterStateJobOutcome:
    phase: Literal["ready", "blocked"]
    state_candidate: RequiredStateCandidate | None
    reextraction_count: int
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if (
            (self.phase == "ready") != (self.state_candidate is not None)
            or type(self.reextraction_count) is not int
            or not 0 <= self.reextraction_count <= 2
            or self.phase == "ready" and self.reason_code is not None
            or self.phase == "blocked"
            and (
                not isinstance(self.reason_code, str)
                or _SAFE_REASON.fullmatch(self.reason_code) is None
            )
        ):
            raise ValueError("required_chapter_state_job_outcome_invalid")


def required_state_request_identity(
    *,
    ordinal: int,
    source_run_id: str,
    source_run_revision: int,
    source_content_digest: str,
    predecessor_result_digest: str,
    prior_proposal_id: str | None,
    repair_directive: StateRepairDirective | None,
) -> dict[str, Any]:
    return {
        "schema_version": "required_state_candidate_request_identity.v1",
        "ordinal": ordinal,
        "source_run_id": source_run_id,
        "source_run_revision": source_run_revision,
        "source_content_digest": source_content_digest,
        "predecessor_result_digest": predecessor_result_digest,
        "prior_proposal_id": prior_proposal_id,
        "repair_directive": (
            repair_directive.model_dump(mode="json")
            if repair_directive is not None
            else None
        ),
    }


def _state_plan_snapshot(plan: GenerationPlan) -> RequiredGenerationPlanSnapshot:
    return RequiredGenerationPlanSnapshot.freeze(
        plan,
        call_kind="structured",
        expected_target=_STATE_TARGET,
    )


def build_required_chapter_state_authorization(
    *,
    predecessor_job: Mapping[str, Any],
    state_plan: GenerationPlan,
    token_budget: int,
    authorization_revision: int,
    created_at: datetime,
    deadline_at: datetime,
) -> RequiredChapterStateAuthorization:
    raw_predecessor = predecessor_job.get("required_reviewed_candidate")
    predecessor = parse_required_reviewed_candidate(raw_predecessor)
    validate_required_reviewed_candidate_job(predecessor_job, predecessor)
    review_authority = validate_required_chapter_review_readiness(
        predecessor_job.get("readiness")
    )
    chapter_authority = review_authority.chapter(predecessor.chapter_id)
    if (
        predecessor_job.get("status") != "paused"
        or predecessor_job.get("pause_reason")
        != REQUIRED_REVIEWED_CANDIDATE_PAUSE_REASON
        or predecessor_job.get("has_uncertain_attempts") is not False
        or predecessor_job.get("active_token_reservations") not in (None, [])
        or predecessor_job.get("tokens_reserved") not in (None, 0)
        or predecessor_job.get("attempt_reservation") is not None
    ):
        raise ValueError("required_state_predecessor_not_settled")
    snapshot = _state_plan_snapshot(state_plan)
    budget = structured_call_budget(state_plan)
    bounds = scale_provider_bounds(
        budget.provider_bounds,
        MAX_REQUIRED_STATE_CALLS,
    )
    identity = {
        "schema_version": "required_chapter_state_job_authorization.v1",
        "protocol_revision": REQUIRED_CHAPTER_STATE_PIPELINE_REVISION,
        "novel_id": predecessor.novel_id,
        "owner_id": predecessor.owner_id,
        "chapter_id": predecessor.chapter_id,
        "volume_id": chapter_authority.volume_id,
        "authorization_revision": authorization_revision,
        "narrative_revision": predecessor.narrative_revision,
        "outline_revision": chapter_authority.outline_revision,
        "created_at": created_at,
        "deadline_at": deadline_at,
        "predecessor_candidate": predecessor.model_dump(mode="json"),
        "state_generation": snapshot.model_dump(mode="json"),
        "maximum_provider_attempts_per_call": budget.max_paid_attempts,
        "maximum_tokens_per_call": budget.max_tokens_per_call,
        "maximum_provider_attempts_total": (
            budget.max_paid_attempts * MAX_REQUIRED_STATE_CALLS
        ),
        "maximum_tokens_total": budget.max_tokens_per_call * MAX_REQUIRED_STATE_CALLS,
        "maximum_serial_seconds_total": (
            state_plan.timeout_seconds * MAX_REQUIRED_STATE_CALLS
        ),
        "token_budget": token_budget,
        "provider_bounds": [
            RequiredProviderBound(
                provider_alias=item.provider_alias,
                maximum_paid_attempts_total=item.paid_attempts,
                maximum_tokens_total=item.tokens,
            ).model_dump(mode="json")
            for item in bounds
        ],
        "max_state_reextractions": MAX_REQUIRED_STATE_REEXTRACTIONS,
        "can_write_formal_prose": False,
        "can_accept_formal_state": False,
    }
    return RequiredChapterStateAuthorization(
        **identity,
        contract_digest=required_state_digest(identity),
    )


def prepare_required_chapter_state_readiness(
    predecessor_job: Mapping[str, Any],
    *,
    state_plan: GenerationPlan,
    token_budget: int,
    authorization_revision: int,
    created_at: datetime,
    deadline_at: datetime,
) -> dict[str, Any]:
    authorization = build_required_chapter_state_authorization(
        predecessor_job=predecessor_job,
        state_plan=state_plan,
        token_budget=token_budget,
        authorization_revision=authorization_revision,
        created_at=created_at,
        deadline_at=deadline_at,
    )
    predecessor = authorization.predecessor_candidate
    planning = {
        REQUIRED_CHAPTER_STATE_REVISION_KEY: REQUIRED_CHAPTER_STATE_PIPELINE_REVISION,
        # Keep native datetimes in the in-process readiness envelope.  This
        # mirrors the existing review successor contract and lets the strict
        # authorization model re-validate the exact value before persistence
        # performs its own BSON/JSON projection.
        REQUIRED_CHAPTER_STATE_PLANNING_KEY: authorization.model_dump(
            mode="python"
        ),
        "attempt_capacity": authorization.maximum_provider_attempts_total,
        "providers": [item.provider_alias for item in authorization.provider_bounds],
        "batch_generation_budget_coverage": {
            "schema_version": "required_chapter_state_budget_coverage.v1",
            "maximum_provider_attempts_total": (
                authorization.maximum_provider_attempts_total
            ),
            "maximum_tokens_total": authorization.maximum_tokens_total,
            "provider_bounds": [
                item.model_dump(mode="python")
                for item in authorization.provider_bounds
            ],
            "token_bound_known": True,
            "token_budget": authorization.token_budget,
            "covers_full_job_authority": True,
            "can_write_formal_prose": False,
            "can_accept_formal_state": False,
        },
    }
    issues = [{
        "code": REQUIRED_STATE_CANDIDATE_ACKNOWLEDGEMENT,
        "level": "warning_requires_ack",
        "details": {
            "predecessor_job_id": predecessor.job_id,
            "next_step": "chapter_finalization",
            "can_write_formal_prose": False,
            "can_accept_formal_state": False,
        },
        "action_codes": ["state_successor_boundary"],
    }]
    snapshot = {
        "version": 2,
        "novel_id": authorization.novel_id,
        "scope": "book",
        "volume_id": None,
        "outline_deviation_policy": "pause_for_rewrite",
        "work": {
            "chapters": [{
                "chapter_id": authorization.chapter_id,
                "volume_id": authorization.volume_id,
                "has_outline": True,
                "has_content": False,
            }]
        },
        "resources": {
            "owner_id": authorization.owner_id,
            "narrative_revision": authorization.narrative_revision,
        },
        "active_proposal": None,
        "planning": planning,
        "issues": issues,
    }
    report = {
        **snapshot,
        "status": "warning_requires_ack",
        "digest": required_state_digest(snapshot),
    }
    validate_required_chapter_state_readiness(report)
    return report


def required_chapter_state_planning_present(planning: Any) -> bool:
    return isinstance(planning, Mapping) and any(
        key in planning for key in _PLANNING_KEYS
    )


def readiness_uses_required_chapter_state(readiness: Any) -> bool:
    if not isinstance(readiness, Mapping):
        return False
    planning = readiness.get("planning")
    if not required_chapter_state_planning_present(planning):
        return False
    validate_required_chapter_state_readiness(readiness)
    return True


def parse_required_chapter_state_authorization(
    value: Any,
) -> RequiredChapterStateAuthorization:
    try:
        parsed = RequiredChapterStateAuthorization.model_validate(value)
    except (TypeError, ValueError, ValidationError) as exc:
        raise ValueError("required_chapter_state_authorization_invalid") from exc
    if required_state_digest(value) != required_state_digest(
        parsed.model_dump(mode="python")
    ):
        raise ValueError("required_chapter_state_authorization_not_canonical")
    return parsed


def validate_required_chapter_state_readiness(
    readiness: Mapping[str, Any],
) -> RequiredChapterStateAuthorization:
    planning = readiness.get("planning")
    if not isinstance(planning, Mapping):
        raise ValueError("required_chapter_state_readiness_invalid")
    if (
        planning.get(REQUIRED_CHAPTER_STATE_REVISION_KEY)
        != REQUIRED_CHAPTER_STATE_PIPELINE_REVISION
        or REQUIRED_CHAPTER_STATE_PLANNING_KEY not in planning
        or any(key in planning for key in _FORBIDDEN_AUTHORITY_KEYS)
    ):
        raise ValueError("required_chapter_state_mode_conflict")
    authorization = parse_required_chapter_state_authorization(
        planning[REQUIRED_CHAPTER_STATE_PLANNING_KEY]
    )
    work = readiness.get("work")
    chapters = work.get("chapters") if isinstance(work, Mapping) else None
    resources = readiness.get("resources")
    coverage = planning.get("batch_generation_budget_coverage")
    expected_work = [{
        "chapter_id": authorization.chapter_id,
        "volume_id": authorization.volume_id,
        "has_outline": True,
        "has_content": False,
    }]
    if (
        readiness.get("version") != 2
        or readiness.get("novel_id") != authorization.novel_id
        or readiness.get("scope") != "book"
        or readiness.get("volume_id") is not None
        or not isinstance(resources, Mapping)
        or resources.get("owner_id") != authorization.owner_id
        or resources.get("narrative_revision") != authorization.narrative_revision
        or chapters != expected_work
        or planning.get("attempt_capacity")
        != authorization.maximum_provider_attempts_total
        or planning.get("providers")
        != [item.provider_alias for item in authorization.provider_bounds]
        or not isinstance(coverage, Mapping)
        or coverage.get("schema_version")
        != "required_chapter_state_budget_coverage.v1"
        or coverage.get("maximum_provider_attempts_total")
        != authorization.maximum_provider_attempts_total
        or coverage.get("maximum_tokens_total")
        != authorization.maximum_tokens_total
        or coverage.get("token_budget") != authorization.token_budget
        or coverage.get("covers_full_job_authority") is not True
        or coverage.get("can_write_formal_prose") is not False
        or coverage.get("can_accept_formal_state") is not False
    ):
        raise ValueError("required_chapter_state_readiness_changed")
    digest_snapshot = {
        key: deepcopy(readiness.get(key))
        for key in (
            "version",
            "novel_id",
            "scope",
            "volume_id",
            "outline_deviation_policy",
            "work",
            "resources",
            "active_proposal",
            "planning",
            "issues",
        )
    }
    if readiness.get("digest") != required_state_digest(digest_snapshot):
        raise ValueError("required_chapter_state_readiness_digest_changed")
    return authorization


def readiness_chapter_uses_required_chapter_state(
    readiness: Any,
    *,
    chapter_id: str,
) -> bool:
    if not readiness_uses_required_chapter_state(readiness):
        return False
    return validate_required_chapter_state_readiness(readiness).chapter_id == str(
        chapter_id
    )


def build_required_state_request(
    *,
    job_id: str,
    authorization: RequiredChapterStateAuthorization,
    readiness_digest: str,
    ordinal: int,
    prior_observation: RequiredStateCandidateObservation | None,
) -> RequiredStateCandidateRequest:
    predecessor = authorization.predecessor_candidate
    prior_proposal_id = (
        prior_observation.proposal_id if prior_observation is not None else None
    )
    directive = (
        _repair_directive(ordinal, prior_observation)
        if prior_observation is not None
        else None
    )
    identity = required_state_request_identity(
        ordinal=ordinal,
        source_run_id=predecessor.source_run_id,
        source_run_revision=predecessor.source_run_revision,
        source_content_digest=predecessor.source_content_digest,
        predecessor_result_digest=predecessor.result_digest,
        prior_proposal_id=prior_proposal_id,
        repair_directive=directive,
    )
    request_digest = required_state_digest(identity)
    binding = RequiredStateGenerationBinding.create(
        job_id=job_id,
        owner_id=authorization.owner_id,
        novel_id=authorization.novel_id,
        chapter_id=authorization.chapter_id,
        readiness_digest=readiness_digest,
        authorization_revision=authorization.authorization_revision,
        expected_narrative_revision=authorization.narrative_revision,
        predecessor_job_id=predecessor.job_id,
        predecessor_result_digest=predecessor.result_digest,
        source_run_id=predecessor.source_run_id,
        source_run_revision=predecessor.source_run_revision,
        source_content_digest=predecessor.source_content_digest,
        ordinal=ordinal,
        request_digest=request_digest,
    )
    return RequiredStateCandidateRequest(
        binding=binding,
        prior_proposal_id=prior_proposal_id,
        repair_directive=directive,
    )


def _repair_directive(
    cycle: int,
    observation: RequiredStateCandidateObservation,
) -> StateRepairDirective:
    reasons = []
    if observation.consistency_issue_count:
        reasons.append("consistency_conflict")
    if (
        observation.dropped_reference_count
        or observation.invalid_internal_reference_count
        or observation.dangling_reference_count
    ):
        reasons.append("invalid_internal_reference")
    if observation.unaccounted_canonical_fact_count:
        reasons.append("unaccounted_canonical_fact")
    if observation.extraction_failure_count:
        reasons.append("state_extraction_unknown")
    if not reasons:
        reasons.append("state_extraction_unknown")
    return StateRepairDirective(
        cycle=cycle,
        reason_codes=tuple(reasons),
        consistency_issue_count=observation.consistency_issue_count,
        affected_card_ids=observation.affected_card_ids,
        dropped_reference_count=observation.dropped_reference_count,
        unaccounted_canonical_fact_count=(
            observation.unaccounted_canonical_fact_count
        ),
        invalid_internal_reference_count=(
            observation.invalid_internal_reference_count
        ),
        dangling_reference_count=observation.dangling_reference_count,
        extraction_failure_count=observation.extraction_failure_count,
    )


def _bounded_dropped_count(value: Any) -> int:
    if isinstance(value, Mapping):
        exact = value.get("dropped_reference_count")
        if type(exact) is int:
            return min(MAX_STATE_REPAIR_DROPPED_REFERENCES, max(0, exact))
        return min(
            MAX_STATE_REPAIR_DROPPED_REFERENCES,
            sum(_bounded_dropped_count(item) for item in value.values()),
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return min(MAX_STATE_REPAIR_DROPPED_REFERENCES, len(value))
    return int(bool(value))


def _truncation_counts(value: Any) -> tuple[int, int]:
    if not isinstance(value, Mapping):
        return 0, 0
    if "truncated_section_count" in value or "dropped_item_count" in value:
        raw_sections = value.get("truncated_section_count")
        raw_items = value.get("dropped_item_count")
        return (
            min(100, raw_sections) if type(raw_sections) is int and raw_sections >= 0 else 0,
            min(10_000, raw_items) if type(raw_items) is int and raw_items >= 0 else 0,
        )
    sections = value.get("truncated_sections")
    counts = value.get("dropped_item_counts")
    dropped_items = 0
    if isinstance(counts, Mapping):
        for item in list(counts.values())[:100]:
            if type(item) is int and item > 0:
                dropped_items = min(10_000, dropped_items + item)
    return (
        min(100, len(sections)) if isinstance(sections, list) else 0,
        dropped_items,
    )


def evaluate_required_state_candidate(
    generation: ChapterGenerationResult,
    *,
    source: ProseCandidateSource,
    chapter: Mapping[str, Any],
    attempt_ids: Sequence[str],
) -> RequiredStateCandidateObservation:
    if (
        generation.stage is not ChapterGenerationStage.STATE
        or generation.accepted
        or not isinstance(generation.value, Mapping)
    ):
        raise RequiredChapterStateJobConflict("required_state_result_invalid")
    state = dict(generation.value)
    proposal_id = state.get("proposal_id")
    acceptance_token = state.get("acceptance_token")
    raw_issues = state.get("consistency_issues")
    evidence = state.get("fact_evidence")
    if (
        not isinstance(proposal_id, str)
        or re.fullmatch(_OBJECT_ID, proposal_id) is None
        or not isinstance(acceptance_token, str)
        or not acceptance_token
        or not isinstance(raw_issues, list)
        or len(raw_issues) > MAX_STATE_REPAIR_CARD_IDS
        or any(not isinstance(item, Mapping) for item in raw_issues)
        or not isinstance(evidence, Mapping)
    ):
        raise RequiredChapterStateJobConflict("required_state_result_invalid")
    source_binding = evidence.get("source_binding")
    chapter_id = str(chapter.get("_id") or "")
    if (
        not isinstance(source_binding, Mapping)
        or source_binding.get("chapter_id") != chapter_id
        or source_binding.get("source_prose_run_id") != source.source_run_id
        or source_binding.get("source_prose_run_revision")
        != source.source_run_revision
        or source_binding.get("source_content_digest")
        != source.source_content_digest
    ):
        raise RequiredChapterStateJobConflict("required_state_source_stale")
    try:
        decision = automatic_state_fact_decision(evidence, candidate=state)
    except StateFactAccountingError as exc:
        raise RequiredChapterStateJobConflict(
            "required_state_fact_evidence_invalid"
        ) from exc
    accounting = decision.get("fact_accounting")
    if not isinstance(accounting, Mapping):
        raise RequiredChapterStateJobConflict("required_state_fact_evidence_invalid")
    accounting_binding = accounting.get("source_binding")
    if accounting_binding != source_binding:
        raise RequiredChapterStateJobConflict("required_state_source_stale")
    declared = _declared_character_card_ids(chapter)
    affected = tuple(sorted({
        card_id
        for issue in raw_issues
        for card_id in (issue.get("card_id"),)
        if isinstance(card_id, str) and card_id in declared
    }))
    dropped = _bounded_dropped_count(generation.dropped)
    truncated_sections, dropped_items = _truncation_counts(generation.truncation)
    persisted_candidate = {
        key: deepcopy(value)
        for key, value in state.items()
        if key not in {"proposal_id", "acceptance_token", "proposal_expires_at"}
    }
    normalized_attempt_ids = tuple(str(item) for item in attempt_ids)
    if (
        not 1 <= len(normalized_attempt_ids) <= 2
        or len(set(normalized_attempt_ids)) != len(normalized_attempt_ids)
        or any(not item or len(item) > 128 for item in normalized_attempt_ids)
    ):
        raise RequiredChapterStateJobConflict("required_state_attempts_invalid")
    unaccounted = int(accounting.get("unaccounted_canonical_facts") or 0)
    invalid = int(accounting.get("invalid_internal_references") or 0)
    dangling = int(accounting.get("dangling_references") or 0)
    extraction = int(accounting.get("extraction_failure_count") or 0)
    gate_passed = bool(
        not raw_issues
        and dropped == 0
        and accounting.get("gate_passed") is True
    )
    return RequiredStateCandidateObservation(
        proposal_id=proposal_id,
        candidate_digest=required_state_digest(persisted_candidate),
        consistency_issue_count=len(raw_issues),
        affected_card_ids=affected,
        dropped_reference_count=dropped,
        fact_accounting_digest=str(accounting.get("accounting_digest") or ""),
        canonical_fact_count=int(accounting.get("canonical_fact_count") or 0),
        unaccounted_canonical_fact_count=unaccounted,
        invalid_internal_reference_count=invalid,
        dangling_reference_count=dangling,
        extraction_failure_count=extraction,
        truncated_section_count=truncated_sections,
        dropped_item_count=dropped_items,
        attempt_ids=normalized_attempt_ids,
        gate_passed=gate_passed,
    )


def _declared_character_card_ids(chapter: Mapping[str, Any]) -> frozenset[str]:
    outline = chapter.get("outline")
    raw = (
        outline.get("present_character_card_ids")
        if isinstance(outline, Mapping)
        else None
    )
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(
        item
        for item in raw[:MAX_STATE_REPAIR_CARD_IDS]
        if isinstance(item, str)
        and 0 < len(item) <= MAX_STATE_REPAIR_CARD_ID_LENGTH
    )


def parse_required_state_candidate(value: Any) -> RequiredStateCandidate:
    try:
        return RequiredStateCandidate.model_validate(value)
    except (TypeError, ValueError, ValidationError) as exc:
        raise RequiredChapterStateJobConflict(
            "required_state_candidate_invalid"
        ) from exc


def _build_state_candidate_result(
    *,
    job_id: str,
    authorization: RequiredChapterStateAuthorization,
    readiness_digest: str,
    observation: RequiredStateCandidateObservation,
    journal_digest: str,
    reextraction_count: int,
) -> RequiredStateCandidate:
    predecessor = authorization.predecessor_candidate
    identity = {
        "schema_version": "required_state_candidate.v1",
        "status": "consistent",
        "job_id": job_id,
        "owner_id": authorization.owner_id,
        "novel_id": authorization.novel_id,
        "chapter_id": authorization.chapter_id,
        "readiness_digest": readiness_digest,
        "authorization_revision": authorization.authorization_revision,
        "authorization_contract_digest": authorization.contract_digest,
        "narrative_revision": authorization.narrative_revision,
        "predecessor_job_id": predecessor.job_id,
        "predecessor_result_digest": predecessor.result_digest,
        "source_run_id": predecessor.source_run_id,
        "source_run_revision": predecessor.source_run_revision,
        "source_content_digest": predecessor.source_content_digest,
        "state_proposal_id": observation.proposal_id,
        "state_candidate_digest": observation.candidate_digest,
        "state_journal_digest": journal_digest,
        "fact_accounting_digest": observation.fact_accounting_digest,
        "canonical_fact_count": observation.canonical_fact_count,
        "reextraction_count": reextraction_count,
        "gate_passed": True,
        "next_step": "chapter_finalization",
        "can_write_formal_prose": False,
        "can_accept_formal_state": False,
    }
    return RequiredStateCandidate(
        **identity,
        result_digest=required_state_digest(identity),
    )


def validate_required_state_candidate_job(
    job: Mapping[str, Any],
    candidate: RequiredStateCandidate,
) -> None:
    """Re-prove the result from its exact state Job journal and authority."""

    try:
        from backend.db.required_state_candidate_journal import (
            parse_required_state_candidate_journal,
            required_state_journal_digest,
            validate_required_state_attempt_accounting,
        )

        authorization = validate_required_chapter_state_readiness(
            job["readiness"]
        )
        journal = parse_required_state_candidate_journal(
            job.get("required_state_candidate_journal")
        )
        latest = journal.entries[-1]
        observation = latest.observation
        if observation is None:
            raise ValueError("state observation missing")
        for entry in journal.entries:
            validate_required_state_attempt_accounting(job, entry, authorization)
        if (
            str(job.get("_id")) != candidate.job_id
            or str(job.get("owner_id")) != candidate.owner_id
            or str(job.get("novel_id")) != candidate.novel_id
            or job.get("is_deleted") is not False
            or job.get("status") not in {"running", "paused"}
            or job.get("current_chapter_id") != candidate.chapter_id
            or job.get("authorization_revision")
            != candidate.authorization_revision
            or job.get("expected_narrative_revision")
            != candidate.narrative_revision
            or job["readiness"].get("digest") != candidate.readiness_digest
            or authorization.contract_digest
            != candidate.authorization_contract_digest
            or authorization.predecessor_candidate.job_id
            != candidate.predecessor_job_id
            or authorization.predecessor_candidate.result_digest
            != candidate.predecessor_result_digest
            or authorization.predecessor_candidate.source_run_id
            != candidate.source_run_id
            or authorization.predecessor_candidate.source_run_revision
            != candidate.source_run_revision
            or authorization.predecessor_candidate.source_content_digest
            != candidate.source_content_digest
            or len(journal.entries) - 1 != candidate.reextraction_count
            or latest.phase != "produced"
            or observation.gate_passed is not True
            or observation.proposal_id != candidate.state_proposal_id
            or observation.candidate_digest != candidate.state_candidate_digest
            or observation.fact_accounting_digest
            != candidate.fact_accounting_digest
            or observation.canonical_fact_count != candidate.canonical_fact_count
            or required_state_journal_digest(journal)
            != candidate.state_journal_digest
            or job.get("has_uncertain_attempts") is not False
            or job.get("active_token_reservations") not in (None, [])
            or job.get("tokens_reserved") not in (None, 0)
            or job.get("attempt_reservation") is not None
            or job.get("progress") not in (None, [])
        ):
            raise ValueError("required state candidate proof changed")
    except (IndexError, KeyError, TypeError, ValueError, ValidationError) as exc:
        raise RequiredChapterStateJobConflict(
            "required_state_candidate_proof_invalid"
        ) from exc


def required_state_source_from_run(
    run: Mapping[str, Any],
    authorization: RequiredChapterStateAuthorization,
) -> ProseCandidateSource:
    predecessor = authorization.predecessor_candidate
    text = run.get("assembled_text")
    if (
        str(run.get("_id") or "") != predecessor.source_run_id
        or str(run.get("owner_id") or "") != authorization.owner_id
        or str(run.get("novel_id") or "") != authorization.novel_id
        or str(run.get("chapter_id") or "") != authorization.chapter_id
        or run.get("is_deleted") is not False
        or run.get("status") != "complete"
        or type(run.get("revision")) is not int
        or run.get("revision") != predecessor.source_run_revision
        or not isinstance(text, str)
        or chapter_content_digest(text) != predecessor.source_content_digest
    ):
        raise RequiredChapterStateJobConflict("required_state_source_stale")
    completion = dict(run.get("completion") or {})
    if (
        completion.get("status") != "complete"
        or completion.get("finish_reason") != "stop"
        or completion.get("can_write_formal_prose") is not False
    ):
        raise RequiredChapterStateJobConflict(
            "required_state_source_completion_invalid"
        )
    completion.update({
        "source_run_id": predecessor.source_run_id,
        "source_run_revision": predecessor.source_run_revision,
        "source_run_digest": predecessor.source_content_digest,
    })
    return ProseCandidateSource(
        text=text,
        source_run_id=predecessor.source_run_id,
        source_run_revision=predecessor.source_run_revision,
        source_content_digest=predecessor.source_content_digest,
        completion=completion,
    )


class RequiredChapterStateJobRunner:
    """Production Adapter hiding recovery, re-extraction, and local gates."""

    def __init__(
        self,
        job_id: str,
        *,
        repository=None,
        get_prose_run: Callable[..., Awaitable[Mapping[str, Any]]] | None = None,
        generate_state: Callable[..., Awaitable[ChapterGenerationResult]] | None = None,
        recover_state: Callable[..., Awaitable[Any]] | None = None,
        attempt_scope_factory: Callable[..., Any] | None = None,
    ) -> None:
        if repository is None:
            from backend.db.repositories.generation_job_repository import (
                generation_job_repo,
            )

            repository = generation_job_repo
        if get_prose_run is None:
            from backend.db.repositories.prose_run_repository import prose_run_repo

            get_prose_run = prose_run_repo.get_run
        if generate_state is None:
            from backend.services.generation.headless_generation import (
                generate_state_candidate,
            )

            generate_state = generate_state_candidate
        if recover_state is None:
            from backend.services.novel.state_proposal import state_proposal_module

            recover_state = state_proposal_module.recover_required_state_generation
        self._job_id = str(job_id)
        self._repository = repository
        self._get_prose_run = get_prose_run
        self._generate_state = generate_state
        self._recover_state = recover_state
        self._attempt_scope_factory = attempt_scope_factory

    async def run(
        self,
        novel_id: str,
        chapter: Mapping[str, Any],
    ) -> RequiredChapterStateJobOutcome:
        from backend.db.required_state_candidate_journal import (
            parse_required_state_candidate_journal,
            required_state_journal_digest,
        )

        job = await self._repository.get_job(self._job_id)
        authorization = validate_required_chapter_state_readiness(
            job.get("readiness")
        )
        chapter_id = str(chapter.get("_id") or "")
        existing = job.get("required_state_candidate")
        if (
            authorization.novel_id != str(novel_id)
            or authorization.chapter_id != chapter_id
            or str(job.get("novel_id") or "") != authorization.novel_id
            or str(job.get("owner_id") or "") != authorization.owner_id
            or job.get("current_chapter_id") != chapter_id
            or job.get("authorization_revision")
            != authorization.authorization_revision
            or job.get("expected_narrative_revision")
            != authorization.narrative_revision
            or (existing is None and job.get("status") != "running")
            or (existing is not None and job.get("status") not in {"running", "paused"})
            or bool(str(chapter.get("content") or "").strip())
        ):
            raise RequiredChapterStateJobConflict(
                "required_chapter_state_job_binding_stale"
            )
        if existing is not None:
            candidate = parse_required_state_candidate(existing)
            validate_required_state_candidate_job(job, candidate)
            return RequiredChapterStateJobOutcome(
                phase="ready",
                state_candidate=candidate,
                reextraction_count=candidate.reextraction_count,
            )
        predecessor_job = await self._repository.read_required_state_predecessor_job(
            self._job_id,
            authorization.predecessor_candidate.job_id,
        )
        predecessor = parse_required_reviewed_candidate(
            predecessor_job.get("required_reviewed_candidate")
        )
        validate_required_reviewed_candidate_job(predecessor_job, predecessor)
        if predecessor != authorization.predecessor_candidate:
            raise RequiredChapterStateJobConflict(
                "required_state_predecessor_changed"
            )
        run = await self._get_prose_run(
            predecessor.source_run_id,
            authorization.owner_id,
        )
        source = required_state_source_from_run(run, authorization)
        used_slots = await self._repository.list_attempt_slots(
            self._job_id,
            chapter_id=chapter_id,
            step_prefix=REQUIRED_STATE_STEP_PREFIX,
        )
        remaining = authorization.maximum_provider_attempts_total - len(used_slots)
        if remaining < 0:
            raise RequiredChapterStateJobConflict(
                "required_state_attempt_capacity_changed"
            )
        await self._repository.reserve_attempts(
            self._job_id,
            chapter_id,
            remaining,
        )
        try:
            prior: RequiredStateCandidateObservation | None = None
            raw_journal = job.get("required_state_candidate_journal")
            if raw_journal is not None:
                journal = parse_required_state_candidate_journal(raw_journal)
                produced = [
                    entry.observation
                    for entry in journal.entries
                    if entry.phase == "produced" and entry.observation is not None
                ]
                prior = produced[-1] if produced else None
                if prior is not None and prior.gate_passed:
                    candidate = _build_state_candidate_result(
                        job_id=self._job_id,
                        authorization=authorization,
                        readiness_digest=str(job["readiness"]["digest"]),
                        observation=prior,
                        journal_digest=required_state_journal_digest(journal),
                        reextraction_count=len(journal.entries) - 1,
                    )
                    return RequiredChapterStateJobOutcome(
                        phase="ready",
                        state_candidate=candidate,
                        reextraction_count=len(journal.entries) - 1,
                    )
            start_ordinal = 0 if prior is None else len(produced)
            for ordinal in range(start_ordinal, MAX_REQUIRED_STATE_CALLS):
                request = build_required_state_request(
                    job_id=self._job_id,
                    authorization=authorization,
                    readiness_digest=str(job["readiness"]["digest"]),
                    ordinal=ordinal,
                    prior_observation=prior,
                )
                await self._repository.begin_required_state_candidate(
                    self._job_id,
                    request,
                )
                generation = await self._recover_generation(
                    request=request,
                    source=source,
                    chapter=chapter,
                    authorization=authorization,
                )
                slots = await self._repository.list_attempt_slots(
                    self._job_id,
                    chapter_id=chapter_id,
                    step_prefix=request.step_id,
                )
                attempts, _ = project_persisted_attempt_evidence(
                    slots,
                    maximum_entries=authorization.maximum_provider_attempts_per_call,
                )
                observation = evaluate_required_state_candidate(
                    generation,
                    source=source,
                    chapter=chapter,
                    attempt_ids=[
                        str(item["attempt_id"])
                        for item in attempts
                        if item.get("state") == "accounted"
                    ],
                )
                await self._repository.publish_required_state_observation(
                    self._job_id,
                    request,
                    observation,
                )
                if observation.gate_passed:
                    current = await self._repository.get_job(self._job_id)
                    journal = parse_required_state_candidate_journal(
                        current.get("required_state_candidate_journal")
                    )
                    candidate = _build_state_candidate_result(
                        job_id=self._job_id,
                        authorization=authorization,
                        readiness_digest=str(job["readiness"]["digest"]),
                        observation=observation,
                        journal_digest=required_state_journal_digest(journal),
                        reextraction_count=ordinal,
                    )
                    return RequiredChapterStateJobOutcome(
                        phase="ready",
                        state_candidate=candidate,
                        reextraction_count=ordinal,
                    )
                prior = observation
            return RequiredChapterStateJobOutcome(
                phase="blocked",
                state_candidate=None,
                reextraction_count=MAX_REQUIRED_STATE_REEXTRACTIONS,
                reason_code="required_state_reextraction_exhausted",
            )
        finally:
            await self._repository.finish_attempt_reservation(
                self._job_id,
                chapter_id,
            )

    async def _recover_generation(
        self,
        *,
        request: RequiredStateCandidateRequest,
        source: ProseCandidateSource,
        chapter: Mapping[str, Any],
        authorization: RequiredChapterStateAuthorization,
    ) -> ChapterGenerationResult:
        recovered = await self._recover_state(request.binding)
        if recovered is not None:
            value = dict(getattr(recovered, "value", recovered) or {})
            return ChapterGenerationResult(
                stage=ChapterGenerationStage.STATE,
                value=value,
                usage={},
                attempts=[],
                truncation={
                    "truncated_section_count": int(
                        getattr(recovered, "truncated_section_count", 0) or 0
                    ),
                    "dropped_item_count": int(
                        getattr(recovered, "dropped_item_count", 0) or 0
                    ),
                },
                dropped=(
                    {
                        "dropped_reference_count": int(
                            getattr(recovered, "dropped_reference_count", 0) or 0
                        )
                    }
                    if int(
                        getattr(recovered, "dropped_reference_count", 0) or 0
                    )
                    else {}
                ),
                accepted=False,
            )
        slots = await self._repository.list_attempt_slots(
            self._job_id,
            chapter_id=authorization.chapter_id,
            step_prefix=request.step_id,
        )
        factory = self._attempt_scope_factory
        attempt_scope = (
            factory(
                self._job_id,
                authorization.chapter_id,
                request.step_id,
                slots,
            )
            if factory is not None
            else JobAttemptScope(
                self._job_id,
                authorization.chapter_id,
                request.step_id,
                repo=self._repository,
                existing_attempt_slots=slots,
            )
        )
        guidance = (
            StateRepairGuidance(
                **request.repair_directive.model_dump(mode="python"),
                prior_proposal_id=str(request.prior_proposal_id),
            )
            if request.repair_directive is not None
            else None
        )
        return await self._generate_state(
            authorization.novel_id,
            dict(chapter),
            source,
            attempt_scope=attempt_scope,
            generation_plan=authorization.plan(),
            repair_guidance=guidance,
            request_id=request.binding.recovery_key,
            required_state_generation_binding=request.binding,
        )
