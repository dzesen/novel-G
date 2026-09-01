"""Explicit successor authority from deferred candidates to one formal chapter.

The review and state successor Jobs deliberately stop before formal writes.
This Module is the narrow, separately-authorized seam that consumes both exact
metadata handoffs, re-proves their durable journals, and delegates the one
atomic prose + state mutation to ``ChapterFinalizationService``.  It performs
no Provider calls and never stores prose in the finalization Job.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from backend.llm.schemas.scene_contract_pydantic import (
    ValidatedChapterOutlineAdherenceEvidenceV4Schema,
)
from backend.services.generation.candidate_repair_contracts import (
    JobMutationReceiptV1,
    JobMutationRecoveryBindingV1,
)
from backend.services.generation.attempt_ledger_contracts import (
    validate_launchable_attempt_ledgers,
)
from backend.services.generation.chapter_finalization import (
    ChapterFinalizationAuthorization,
    ChapterFinalizationEvidence,
    build_chapter_finalization_authorization,
    chapter_finalization_idempotency_key,
    chapter_finalization_service,
    parse_chapter_finalization_authorization,
)
from backend.services.generation.required_chapter_review_job import (
    REQUIRED_REVIEWED_CANDIDATE_PAUSE_REASON,
    RequiredReviewedChapterCandidate,
    parse_required_reviewed_candidate,
    required_review_finalization_evidence,
    validate_required_chapter_review_readiness,
    validate_required_reviewed_candidate_job,
)
from backend.services.generation.required_chapter_state_contracts import (
    RequiredStateGenerationBinding,
    required_state_digest,
)
from backend.services.generation.required_chapter_state_job import (
    REQUIRED_STATE_CANDIDATE_PAUSE_REASON,
    RequiredStateCandidate,
    parse_required_state_candidate,
    validate_required_chapter_state_readiness,
    validate_required_state_candidate_job,
)


REQUIRED_CHAPTER_FINALIZATION_PIPELINE_REVISION = (
    "required-chapter-finalization-job-r1"
)
REQUIRED_CHAPTER_FINALIZATION_PLANNING_KEY = (
    "required_chapter_finalization_authorization"
)
REQUIRED_CHAPTER_FINALIZATION_REVISION_KEY = (
    "required_chapter_finalization_pipeline_revision"
)
REQUIRED_CHAPTER_FINALIZATION_ACKNOWLEDGEMENT = (
    "successor_finalization_writes_formal_prose_and_state"
)
REQUIRED_CHAPTER_FINALIZATION_JOB_TOKEN_BUDGET = 1
REQUIRED_CHAPTER_FINALIZATION_RESULT_STEP = "chapter_successor_rollover"

_SHA256 = r"^[0-9a-f]{64}$"
_OBJECT_ID = r"^[0-9a-f]{24}$"
_MAX = 2**63 - 1
_PLANNING_KEYS = frozenset({
    REQUIRED_CHAPTER_FINALIZATION_PLANNING_KEY,
    REQUIRED_CHAPTER_FINALIZATION_REVISION_KEY,
})
_FORBIDDEN_AUTHORITY_KEYS = frozenset({
    "prose_continuation_authorization",
    "chapter_candidate_job_execution_authorization",
    "chapter_candidate_repair_authorization",
    "required_chapter_review_authorization",
    "required_chapter_state_authorization",
})


class RequiredChapterFinalizationJobConflict(ValueError):
    """The formal successor authority or one of its source proofs diverged."""


class _Closed(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


class RequiredFinalizationAttemptUsage(_Closed):
    input_tokens: int = Field(ge=0, le=_MAX)
    output_tokens: int = Field(ge=0, le=_MAX)
    total_tokens: int = Field(ge=0, le=_MAX)

    @model_validator(mode="after")
    def validate_total(self) -> "RequiredFinalizationAttemptUsage":
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("required_finalization_attempt_usage_invalid")
        return self


class RequiredFinalizationAttempt(_Closed):
    source_job_id: str = Field(pattern=_OBJECT_ID)
    attempt_id: str = Field(min_length=1, max_length=128)
    chapter_id: str = Field(pattern=_OBJECT_ID)
    step_id: str = Field(min_length=1, max_length=240)
    phase: str = Field(min_length=1, max_length=64)
    provider_alias: str = Field(min_length=1, max_length=64)
    state: Literal[
        "accounted",
        "released_pre_dispatch",
        "uncertain_retry_acknowledged",
        "uncertain_skip_acknowledged",
        "uncertain_abort_acknowledged",
    ]
    conservative_tokens: int = Field(ge=1, le=_MAX)
    charged_tokens: int | None = Field(default=None, ge=0, le=_MAX)
    usage: RequiredFinalizationAttemptUsage | None = None

    @model_validator(mode="after")
    def validate_settlement(self) -> "RequiredFinalizationAttempt":
        if self.state in {"claimed", "uncertain"}:
            raise ValueError("required_finalization_attempt_unsettled")
        if self.state == "accounted" and (
            self.usage is None
            or self.charged_tokens != self.usage.total_tokens
        ):
            raise ValueError("required_finalization_attempt_accounting_invalid")
        if self.state != "accounted" and (
            self.usage is not None or self.charged_tokens is not None
        ):
            raise ValueError("required_finalization_attempt_accounting_invalid")
        return self


class RequiredChapterFinalizationAuthorization(_Closed):
    schema_version: Literal[
        "required_chapter_finalization_job_authorization.v1"
    ] = "required_chapter_finalization_job_authorization.v1"
    protocol_revision: Literal[
        "required-chapter-finalization-job-r1"
    ] = REQUIRED_CHAPTER_FINALIZATION_PIPELINE_REVISION
    contract_digest: str = Field(pattern=_SHA256)
    novel_id: str = Field(pattern=_OBJECT_ID)
    owner_id: str = Field(pattern=_OBJECT_ID)
    chapter_id: str = Field(pattern=_OBJECT_ID)
    volume_id: str = Field(pattern=_OBJECT_ID)
    authorization_revision: int = Field(ge=1, le=_MAX)
    narrative_revision: int = Field(ge=0, le=_MAX - 1)
    outline_revision: str = Field(pattern=_SHA256)
    created_at: datetime
    deadline_at: datetime
    reviewed_candidate: RequiredReviewedChapterCandidate
    state_candidate: RequiredStateCandidate
    state_generation_binding: RequiredStateGenerationBinding
    outline_adherence: dict[str, Any]
    repair_cycles_used: int = Field(ge=0, le=2)
    repair_trace: dict[str, Any] | None = None
    attempt_ledger: tuple[RequiredFinalizationAttempt, ...] = Field(
        max_length=128
    )
    attempt_ledger_digest: str = Field(pattern=_SHA256)
    formal_authorization: dict[str, Any]
    token_budget: Literal[1] = REQUIRED_CHAPTER_FINALIZATION_JOB_TOKEN_BUDGET
    maximum_provider_attempts_total: Literal[0] = 0
    maximum_tokens_total: Literal[0] = 0
    can_write_formal_prose: Literal[True] = True
    can_accept_formal_state: Literal[True] = True

    @model_validator(mode="after")
    def validate_authority(self) -> "RequiredChapterFinalizationAuthorization":
        reviewed = self.reviewed_candidate
        state = self.state_candidate
        try:
            adherence = (
                ValidatedChapterOutlineAdherenceEvidenceV4Schema.model_validate(
                    self.outline_adherence
                )
            )
            formal = parse_chapter_finalization_authorization(
                self.formal_authorization
            )
        except ValueError as exc:
            raise ValueError(
                "required_chapter_finalization_evidence_invalid"
            ) from exc
        allowed_jobs = {reviewed.job_id, state.job_id}
        attempt_keys = {
            (attempt.source_job_id, attempt.attempt_id)
            for attempt in self.attempt_ledger
        }
        trace_valid = (
            self.repair_trace is None
            if self.repair_cycles_used == 0
            else isinstance(self.repair_trace, dict)
            and set(self.repair_trace) == {
                "schema_version",
                "repair_cycles_used",
                "component_usage",
                "convergence",
                "final_issue_signatures",
                "converged",
            }
            and self.repair_trace.get("schema_version")
            == "chapter_repair_trace.v2"
            and self.repair_trace.get("repair_cycles_used")
            == self.repair_cycles_used
            and self.repair_trace.get("final_issue_signatures") == []
            and self.repair_trace.get("converged") is True
        )
        if (
            self.created_at.tzinfo is None
            or self.deadline_at.tzinfo is None
            or self.deadline_at.astimezone(timezone.utc)
            <= self.created_at.astimezone(timezone.utc)
            or reviewed.owner_id != self.owner_id
            or reviewed.novel_id != self.novel_id
            or reviewed.chapter_id != self.chapter_id
            or reviewed.narrative_revision != self.narrative_revision
            or state.owner_id != self.owner_id
            or state.novel_id != self.novel_id
            or state.chapter_id != self.chapter_id
            or state.narrative_revision != self.narrative_revision
            or state.predecessor_job_id != reviewed.job_id
            or state.predecessor_result_digest != reviewed.result_digest
            or state.source_run_id != reviewed.source_run_id
            or state.source_run_revision != reviewed.source_run_revision
            or state.source_content_digest != reviewed.source_content_digest
            or self.state_generation_binding.job_id != state.job_id
            or self.state_generation_binding.ordinal
            != state.reextraction_count
            or self.state_generation_binding.source_run_id
            != reviewed.source_run_id
            or self.state_generation_binding.source_run_revision
            != reviewed.source_run_revision
            or self.state_generation_binding.source_content_digest
            != reviewed.source_content_digest
            or self.state_generation_binding.expected_narrative_revision
            != self.narrative_revision
            or adherence.source_prose_run_id != reviewed.source_run_id
            or adherence.source_prose_run_revision
            != reviewed.source_run_revision
            or adherence.source_content_digest
            != reviewed.source_content_digest
            or adherence.decision != "pass"
            or self.repair_cycles_used != reviewed.repair_count
            or not trace_valid
            or formal["authorization_revision"]
            != self.authorization_revision
            or formal["max_repair_cycles"] < self.repair_cycles_used
            or any(
                attempt.source_job_id not in allowed_jobs
                or attempt.chapter_id != self.chapter_id
                for attempt in self.attempt_ledger
            )
            or len(attempt_keys) != len(self.attempt_ledger)
            or required_state_digest([
                item.model_dump(mode="json") for item in self.attempt_ledger
            ])
            != self.attempt_ledger_digest
        ):
            raise ValueError("required_chapter_finalization_authorization_invalid")
        identity = self.model_dump(mode="python", exclude={"contract_digest"})
        if required_state_digest(identity) != self.contract_digest:
            raise ValueError(
                "required_chapter_finalization_contract_digest_changed"
            )
        return self


class RequiredChapterFinalizationResult(_Closed):
    schema_version: Literal[
        "required_chapter_finalization_result.v1"
    ] = "required_chapter_finalization_result.v1"
    status: Literal["formalized"] = "formalized"
    result_digest: str = Field(pattern=_SHA256)
    job_id: str = Field(pattern=_OBJECT_ID)
    owner_id: str = Field(pattern=_OBJECT_ID)
    novel_id: str = Field(pattern=_OBJECT_ID)
    chapter_id: str = Field(pattern=_OBJECT_ID)
    readiness_digest: str = Field(pattern=_SHA256)
    authorization_revision: int = Field(ge=1, le=_MAX)
    authorization_contract_digest: str = Field(pattern=_SHA256)
    reviewed_result_digest: str = Field(pattern=_SHA256)
    state_result_digest: str = Field(pattern=_SHA256)
    source_run_id: str = Field(pattern=_OBJECT_ID)
    source_run_revision: int = Field(ge=2, le=_MAX)
    source_content_digest: str = Field(pattern=_SHA256)
    state_proposal_id: str = Field(pattern=_OBJECT_ID)
    certificate_digest: str = Field(pattern=_SHA256)
    completion_receipt_digest: str = Field(pattern=_SHA256)
    narrative_revision_before: int = Field(ge=0, le=_MAX - 1)
    narrative_revision_after: int = Field(ge=1, le=_MAX)
    formal_prose_state: Literal["ai_complete"] = "ai_complete"
    formal_state_accepted: Literal[True] = True
    next_step: Literal["chapter_successor_rollover"] = (
        REQUIRED_CHAPTER_FINALIZATION_RESULT_STEP
    )

    @model_validator(mode="after")
    def validate_result(self) -> "RequiredChapterFinalizationResult":
        if self.narrative_revision_after != self.narrative_revision_before + 1:
            raise ValueError("required_finalization_revision_receipt_invalid")
        identity = self.model_dump(mode="python", exclude={"result_digest"})
        if required_state_digest(identity) != self.result_digest:
            raise ValueError("required_finalization_result_digest_changed")
        return self


@dataclass(frozen=True)
class RequiredChapterFinalizationJobOutcome:
    result: RequiredChapterFinalizationResult
    mutation_receipt: JobMutationReceiptV1


def _project_attempts(
    job: Mapping[str, Any],
    *,
    source_job_id: str,
    chapter_id: str,
) -> list[RequiredFinalizationAttempt]:
    projected: list[RequiredFinalizationAttempt] = []
    for raw in list(job.get("attempt_slots") or []):
        if not isinstance(raw, Mapping):
            raise ValueError("required_finalization_attempt_ledger_invalid")
        if str(raw.get("chapter_id") or "") != chapter_id:
            continue
        usage = raw.get("usage")
        parsed_usage = (
            None
            if usage is None
            else RequiredFinalizationAttemptUsage.model_validate(usage)
        )
        projected.append(RequiredFinalizationAttempt(
            source_job_id=source_job_id,
            attempt_id=str(raw.get("attempt_id") or ""),
            chapter_id=chapter_id,
            step_id=str(raw.get("step_id") or ""),
            phase=str(raw.get("phase") or ""),
            provider_alias=str(raw.get("provider_alias") or ""),
            state=str(raw.get("state") or ""),
            conservative_tokens=raw.get("conservative_tokens"),
            charged_tokens=raw.get("charged_tokens"),
            usage=parsed_usage,
        ))
    return projected


def _validate_predecessor_attempt_ledger(job: Mapping[str, Any]) -> None:
    """Re-prove one predecessor's closed, fully settled Provider ledger."""

    try:
        validate_launchable_attempt_ledgers(
            attempt_slots=job.get("attempt_slots", []),
            active_token_reservations=job.get(
                "active_token_reservations",
                [],
            ),
            usage_attempt_ids=job.get("usage_attempt_ids", []),
            attempt_capacity=job.get("usage_attempt_capacity", 0),
            attempts_claimed=job.get("usage_attempt_claimed", 0),
            tokens_used=job.get("tokens_used", 0),
            tokens_reserved=job.get("tokens_reserved", 0),
            token_budget=job.get("token_budget"),
            maximum_active_reservations=0,
        )
    except ValueError as exc:
        raise ValueError(
            "required_finalization_predecessor_attempt_invalid"
        ) from exc


def _latest_state_binding(
    state_job: Mapping[str, Any],
) -> RequiredStateGenerationBinding:
    from backend.db.required_state_candidate_journal import (
        parse_required_state_candidate_journal,
    )

    journal = parse_required_state_candidate_journal(
        state_job.get("required_state_candidate_journal")
    )
    latest = journal.entries[-1]
    if latest.phase != "produced" or latest.observation is None:
        raise ValueError("required_finalization_state_journal_incomplete")
    return latest.request.binding


def build_required_chapter_finalization_authorization(
    *,
    state_job: Mapping[str, Any],
    reviewed_job: Mapping[str, Any],
    authorization_revision: int,
    created_at: datetime,
    deadline_at: datetime,
) -> RequiredChapterFinalizationAuthorization:
    state = parse_required_state_candidate(
        state_job.get("required_state_candidate")
    )
    reviewed = parse_required_reviewed_candidate(
        reviewed_job.get("required_reviewed_candidate")
    )
    validate_required_state_candidate_job(state_job, state)
    validate_required_reviewed_candidate_job(reviewed_job, reviewed)
    state_authority = validate_required_chapter_state_readiness(
        state_job.get("readiness")
    )
    review_authority = validate_required_chapter_review_readiness(
        reviewed_job.get("readiness")
    )
    chapter_authority = review_authority.chapter(reviewed.chapter_id)
    if (
        state_job.get("status") != "paused"
        or state_job.get("pause_reason")
        != REQUIRED_STATE_CANDIDATE_PAUSE_REASON
        or reviewed_job.get("status") != "paused"
        or reviewed_job.get("pause_reason")
        != REQUIRED_REVIEWED_CANDIDATE_PAUSE_REASON
        or state_authority.predecessor_candidate != reviewed
        or state.predecessor_job_id != reviewed.job_id
        or any(
            job.get("has_uncertain_attempts") is not False
            or job.get("active_token_reservations") not in (None, [])
            or job.get("tokens_reserved") not in (None, 0)
            or job.get("attempt_reservation") is not None
            for job in (state_job, reviewed_job)
        )
    ):
        raise ValueError("required_finalization_predecessor_not_settled")
    _validate_predecessor_attempt_ledger(reviewed_job)
    _validate_predecessor_attempt_ledger(state_job)
    evidence = required_review_finalization_evidence(reviewed_job, reviewed)
    attempts = sorted(
        [
            *_project_attempts(
                reviewed_job,
                source_job_id=reviewed.job_id,
                chapter_id=reviewed.chapter_id,
            ),
            *_project_attempts(
                state_job,
                source_job_id=state.job_id,
                chapter_id=state.chapter_id,
            ),
        ],
        key=lambda item: (item.source_job_id, item.attempt_id),
    )
    formal = build_chapter_finalization_authorization(
        authorization_revision=authorization_revision,
        max_repair_cycles=max(2, reviewed.repair_count),
    )
    identity = {
        "schema_version": "required_chapter_finalization_job_authorization.v1",
        "protocol_revision": REQUIRED_CHAPTER_FINALIZATION_PIPELINE_REVISION,
        "novel_id": reviewed.novel_id,
        "owner_id": reviewed.owner_id,
        "chapter_id": reviewed.chapter_id,
        "volume_id": chapter_authority.volume_id,
        "authorization_revision": authorization_revision,
        "narrative_revision": reviewed.narrative_revision,
        "outline_revision": chapter_authority.outline_revision,
        "created_at": created_at,
        "deadline_at": deadline_at,
        "reviewed_candidate": reviewed.model_dump(mode="json"),
        "state_candidate": state.model_dump(mode="json"),
        "state_generation_binding": _latest_state_binding(state_job).model_dump(
            mode="json"
        ),
        "outline_adherence": deepcopy(evidence["outline_adherence"]),
        "repair_cycles_used": int(evidence["repair_cycles_used"]),
        "repair_trace": deepcopy(evidence["repair_trace"]),
        "attempt_ledger": tuple(
            item.model_dump(mode="json") for item in attempts
        ),
        "attempt_ledger_digest": required_state_digest([
            item.model_dump(mode="json") for item in attempts
        ]),
        "formal_authorization": formal,
        "token_budget": REQUIRED_CHAPTER_FINALIZATION_JOB_TOKEN_BUDGET,
        "maximum_provider_attempts_total": 0,
        "maximum_tokens_total": 0,
        "can_write_formal_prose": True,
        "can_accept_formal_state": True,
    }
    return RequiredChapterFinalizationAuthorization(
        **identity,
        contract_digest=required_state_digest(identity),
    )


def prepare_required_chapter_finalization_readiness(
    state_job: Mapping[str, Any],
    reviewed_job: Mapping[str, Any],
    *,
    authorization_revision: int,
    created_at: datetime,
    deadline_at: datetime,
) -> dict[str, Any]:
    authorization = build_required_chapter_finalization_authorization(
        state_job=state_job,
        reviewed_job=reviewed_job,
        authorization_revision=authorization_revision,
        created_at=created_at,
        deadline_at=deadline_at,
    )
    planning = {
        REQUIRED_CHAPTER_FINALIZATION_REVISION_KEY: (
            REQUIRED_CHAPTER_FINALIZATION_PIPELINE_REVISION
        ),
        REQUIRED_CHAPTER_FINALIZATION_PLANNING_KEY: authorization.model_dump(
            mode="python"
        ),
        "chapter_finalization_authorization": deepcopy(
            authorization.formal_authorization
        ),
        "attempt_capacity": 0,
        "providers": [],
        "batch_generation_budget_coverage": {
            "schema_version": (
                "required_chapter_finalization_budget_coverage.v1"
            ),
            "maximum_provider_attempts_total": 0,
            "maximum_tokens_total": 0,
            "token_budget": REQUIRED_CHAPTER_FINALIZATION_JOB_TOKEN_BUDGET,
            "covers_full_job_authority": True,
            "provider_dispatch_allowed": False,
            "can_write_formal_prose": True,
            "can_accept_formal_state": True,
        },
    }
    source_binding = {
        "owner_id": authorization.owner_id,
        "novel_id": authorization.novel_id,
        "chapter_id": authorization.chapter_id,
        "prose_run_id": authorization.reviewed_candidate.source_run_id,
        "prose_run_revision": (
            authorization.reviewed_candidate.source_run_revision
        ),
        "content_digest": (
            authorization.reviewed_candidate.source_content_digest
        ),
        "expected_narrative_revision": authorization.narrative_revision,
    }
    issues = [{
        "code": REQUIRED_CHAPTER_FINALIZATION_ACKNOWLEDGEMENT,
        "level": "warning_requires_ack",
        "details": {
            "chapter_id": authorization.chapter_id,
            "reviewed_result_digest": (
                authorization.reviewed_candidate.result_digest
            ),
            "state_result_digest": authorization.state_candidate.result_digest,
            "writes_formal_prose": True,
            "accepts_formal_state": True,
            "provider_dispatch_allowed": False,
        },
        "action_codes": ["formalize_reviewed_chapter_successor"],
    }]
    snapshot = {
        "version": 2,
        "novel_id": authorization.novel_id,
        "scope": "book",
        "volume_id": None,
        "outline_deviation_policy": "pause_for_rewrite",
        "source_binding": source_binding,
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
    validate_required_chapter_finalization_readiness(report)
    return report


def required_chapter_finalization_planning_present(planning: Any) -> bool:
    return isinstance(planning, Mapping) and any(
        key in planning for key in _PLANNING_KEYS
    )


def parse_required_chapter_finalization_authorization(
    value: Any,
) -> RequiredChapterFinalizationAuthorization:
    try:
        parsed = RequiredChapterFinalizationAuthorization.model_validate(value)
    except (TypeError, ValueError, ValidationError) as exc:
        raise ValueError(
            "required_chapter_finalization_authorization_invalid"
        ) from exc
    if required_state_digest(value) != required_state_digest(
        parsed.model_dump(mode="python")
    ):
        raise ValueError(
            "required_chapter_finalization_authorization_not_canonical"
        )
    return parsed


def validate_required_chapter_finalization_readiness(
    readiness: Mapping[str, Any],
) -> RequiredChapterFinalizationAuthorization:
    planning = readiness.get("planning")
    if not isinstance(planning, Mapping):
        raise ValueError("required_chapter_finalization_readiness_invalid")
    if (
        planning.get(REQUIRED_CHAPTER_FINALIZATION_REVISION_KEY)
        != REQUIRED_CHAPTER_FINALIZATION_PIPELINE_REVISION
        or REQUIRED_CHAPTER_FINALIZATION_PLANNING_KEY not in planning
        or any(key in planning for key in _FORBIDDEN_AUTHORITY_KEYS)
    ):
        raise ValueError("required_chapter_finalization_mode_conflict")
    authorization = parse_required_chapter_finalization_authorization(
        planning[REQUIRED_CHAPTER_FINALIZATION_PLANNING_KEY]
    )
    resources = readiness.get("resources")
    work = readiness.get("work")
    chapters = work.get("chapters") if isinstance(work, Mapping) else None
    coverage = planning.get("batch_generation_budget_coverage")
    source = readiness.get("source_binding")
    if (
        readiness.get("version") != 2
        or readiness.get("novel_id") != authorization.novel_id
        or readiness.get("scope") != "book"
        or readiness.get("volume_id") is not None
        or not isinstance(resources, Mapping)
        or resources.get("owner_id") != authorization.owner_id
        or resources.get("narrative_revision")
        != authorization.narrative_revision
        or chapters != [{
            "chapter_id": authorization.chapter_id,
            "volume_id": authorization.volume_id,
            "has_outline": True,
            "has_content": False,
        }]
        or not isinstance(source, Mapping)
        or source.get("owner_id") != authorization.owner_id
        or source.get("novel_id") != authorization.novel_id
        or source.get("chapter_id") != authorization.chapter_id
        or source.get("prose_run_id")
        != authorization.reviewed_candidate.source_run_id
        or source.get("prose_run_revision")
        != authorization.reviewed_candidate.source_run_revision
        or source.get("content_digest")
        != authorization.reviewed_candidate.source_content_digest
        or source.get("expected_narrative_revision")
        != authorization.narrative_revision
        or planning.get("chapter_finalization_authorization")
        != authorization.formal_authorization
        or planning.get("attempt_capacity") != 0
        or planning.get("providers") != []
        or not isinstance(coverage, Mapping)
        or coverage.get("schema_version")
        != "required_chapter_finalization_budget_coverage.v1"
        or coverage.get("maximum_provider_attempts_total") != 0
        or coverage.get("maximum_tokens_total") != 0
        or coverage.get("token_budget")
        != REQUIRED_CHAPTER_FINALIZATION_JOB_TOKEN_BUDGET
        or coverage.get("covers_full_job_authority") is not True
        or coverage.get("provider_dispatch_allowed") is not False
        or coverage.get("can_write_formal_prose") is not True
        or coverage.get("can_accept_formal_state") is not True
    ):
        raise ValueError("required_chapter_finalization_readiness_changed")
    digest_snapshot = {
        key: deepcopy(readiness.get(key))
        for key in (
            "version",
            "novel_id",
            "scope",
            "volume_id",
            "outline_deviation_policy",
            "source_binding",
            "work",
            "resources",
            "active_proposal",
            "planning",
            "issues",
        )
    }
    if readiness.get("digest") != required_state_digest(digest_snapshot):
        raise ValueError(
            "required_chapter_finalization_readiness_digest_changed"
        )
    return authorization


def readiness_uses_required_chapter_finalization(readiness: Any) -> bool:
    if not isinstance(readiness, Mapping):
        return False
    planning = readiness.get("planning")
    if not required_chapter_finalization_planning_present(planning):
        return False
    validate_required_chapter_finalization_readiness(readiness)
    return True


def readiness_chapter_uses_required_chapter_finalization(
    readiness: Any,
    *,
    chapter_id: str,
) -> bool:
    if not readiness_uses_required_chapter_finalization(readiness):
        return False
    return validate_required_chapter_finalization_readiness(
        readiness
    ).chapter_id == str(chapter_id)


def parse_required_chapter_finalization_result(
    value: Any,
) -> RequiredChapterFinalizationResult:
    try:
        return RequiredChapterFinalizationResult.model_validate(value)
    except (TypeError, ValueError, ValidationError) as exc:
        raise RequiredChapterFinalizationJobConflict(
            "required_chapter_finalization_result_invalid"
        ) from exc


def validate_required_chapter_finalization_result_job(
    job: Mapping[str, Any],
    result: RequiredChapterFinalizationResult,
) -> None:
    try:
        authorization = validate_required_chapter_finalization_readiness(
            job["readiness"]
        )
        if (
            str(job.get("_id") or "") != result.job_id
            or str(job.get("owner_id") or "") != result.owner_id
            or str(job.get("novel_id") or "") != result.novel_id
            or job.get("is_deleted") is not False
            or job.get("status") not in {"running", "completed"}
            or job.get("authorization_revision")
            != result.authorization_revision
            or job["readiness"].get("digest") != result.readiness_digest
            or authorization.contract_digest
            != result.authorization_contract_digest
            or authorization.reviewed_candidate.result_digest
            != result.reviewed_result_digest
            or authorization.state_candidate.result_digest
            != result.state_result_digest
            or authorization.reviewed_candidate.source_run_id
            != result.source_run_id
            or authorization.reviewed_candidate.source_run_revision
            != result.source_run_revision
            or authorization.reviewed_candidate.source_content_digest
            != result.source_content_digest
            or authorization.state_candidate.state_proposal_id
            != result.state_proposal_id
            or authorization.narrative_revision
            != result.narrative_revision_before
        ):
            raise ValueError("required finalization result proof changed")
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise RequiredChapterFinalizationJobConflict(
            "required_chapter_finalization_result_proof_invalid"
        ) from exc


def build_required_completion_promotion(
    *,
    finalization_job_id: str,
    readiness_digest: str,
    authorization: RequiredChapterFinalizationAuthorization,
    stored_completion: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    stored = deepcopy(dict(stored_completion))
    if (
        stored.get("status") != "complete"
        or stored.get("finish_reason") != "stop"
        or stored.get("can_write_formal_prose") is not False
    ):
        raise RequiredChapterFinalizationJobConflict(
            "required_finalization_source_completion_invalid"
        )
    effective = {**stored, "can_write_formal_prose": True}
    proof_identity = {
        "schema_version": "required_reviewed_completion_promotion.v1",
        "finalization_job_id": finalization_job_id,
        "readiness_digest": readiness_digest,
        "authorization_contract_digest": authorization.contract_digest,
        "reviewed_job_id": authorization.reviewed_candidate.job_id,
        "reviewed_result_digest": (
            authorization.reviewed_candidate.result_digest
        ),
        "state_job_id": authorization.state_candidate.job_id,
        "state_result_digest": authorization.state_candidate.result_digest,
        "source_run_id": authorization.reviewed_candidate.source_run_id,
        "source_run_revision": (
            authorization.reviewed_candidate.source_run_revision
        ),
        "source_content_digest": (
            authorization.reviewed_candidate.source_content_digest
        ),
        "stored_completion_digest": required_state_digest(stored),
        "effective_completion_digest": required_state_digest(effective),
    }
    return effective, {
        **proof_identity,
        "promotion_digest": required_state_digest(proof_identity),
    }


def _build_result(
    *,
    job_id: str,
    readiness_digest: str,
    authorization: RequiredChapterFinalizationAuthorization,
    finalization: Mapping[str, Any],
) -> RequiredChapterFinalizationResult:
    certificate = finalization.get("certificate")
    receipt = finalization.get("completion_receipt")
    prose = finalization.get("prose")
    state = finalization.get("state")
    if (
        not isinstance(certificate, Mapping)
        or not isinstance(receipt, Mapping)
        or not isinstance(prose, Mapping)
        or not isinstance(state, Mapping)
        or prose.get("acceptance_state") != "ai_complete"
    ):
        raise RequiredChapterFinalizationJobConflict(
            "required_finalization_receipt_invalid"
        )
    after = receipt.get("narrative_revision_after")
    if type(after) is not int:
        raise RequiredChapterFinalizationJobConflict(
            "required_finalization_revision_receipt_invalid"
        )
    identity = {
        "schema_version": "required_chapter_finalization_result.v1",
        "status": "formalized",
        "job_id": job_id,
        "owner_id": authorization.owner_id,
        "novel_id": authorization.novel_id,
        "chapter_id": authorization.chapter_id,
        "readiness_digest": readiness_digest,
        "authorization_revision": authorization.authorization_revision,
        "authorization_contract_digest": authorization.contract_digest,
        "reviewed_result_digest": (
            authorization.reviewed_candidate.result_digest
        ),
        "state_result_digest": authorization.state_candidate.result_digest,
        "source_run_id": authorization.reviewed_candidate.source_run_id,
        "source_run_revision": (
            authorization.reviewed_candidate.source_run_revision
        ),
        "source_content_digest": (
            authorization.reviewed_candidate.source_content_digest
        ),
        "state_proposal_id": authorization.state_candidate.state_proposal_id,
        "certificate_digest": required_state_digest(dict(certificate)),
        "completion_receipt_digest": required_state_digest(dict(receipt)),
        "narrative_revision_before": authorization.narrative_revision,
        "narrative_revision_after": after,
        "formal_prose_state": "ai_complete",
        "formal_state_accepted": True,
        "next_step": REQUIRED_CHAPTER_FINALIZATION_RESULT_STEP,
    }
    return RequiredChapterFinalizationResult(
        **identity,
        result_digest=required_state_digest(identity),
    )


class RequiredChapterFinalizationJobRunner:
    """Adapter from exact predecessor Jobs to the existing atomic finalizer."""

    def __init__(
        self,
        job_id: str,
        *,
        repository=None,
        recover_state: Callable[..., Awaitable[Any]] | None = None,
        finalizer: Any = None,
    ) -> None:
        if repository is None:
            from backend.db.repositories.generation_job_repository import (
                generation_job_repo,
            )

            repository = generation_job_repo
        if recover_state is None:
            from backend.services.novel.state_proposal import state_proposal_module

            recover_state = state_proposal_module.recover_required_state_generation
        self._job_id = str(job_id)
        self._repository = repository
        self._recover_state = recover_state
        self._finalizer = finalizer or chapter_finalization_service

    async def run(
        self,
        novel_id: str,
        chapter: Mapping[str, Any],
    ) -> RequiredChapterFinalizationJobOutcome:
        job = await self._repository.get_job(self._job_id)
        authorization = validate_required_chapter_finalization_readiness(
            job.get("readiness")
        )
        chapter_id = str(chapter.get("_id") or "")
        existing = job.get("required_chapter_finalization_result")
        parsed_existing = (
            parse_required_chapter_finalization_result(existing)
            if existing is not None
            else None
        )
        if parsed_existing is not None:
            validate_required_chapter_finalization_result_job(
                job,
                parsed_existing,
            )
        expected_job_revision = (
            parsed_existing.narrative_revision_after
            if parsed_existing is not None
            else authorization.narrative_revision
        )
        binding = JobMutationRecoveryBindingV1(
            novel_id=authorization.novel_id,
            job_id=self._job_id,
            chapter_id=authorization.chapter_id,
            readiness_digest=str(job["readiness"]["digest"]),
            authorization_revision=authorization.authorization_revision,
            expected_narrative_revision=authorization.narrative_revision,
            operation="finalize_chapter_generation",
            idempotency_key=chapter_finalization_idempotency_key(
                prose_run_id=authorization.reviewed_candidate.source_run_id,
                prose_run_revision=(
                    authorization.reviewed_candidate.source_run_revision
                ),
                state_proposal_id=(
                    authorization.state_candidate.state_proposal_id
                ),
            ),
        )
        chapter_has_content = bool(str(chapter.get("content") or "").strip())
        persisted_recovery = job.get("job_mutation_recovery")
        if (
            authorization.novel_id != str(novel_id)
            or authorization.chapter_id != chapter_id
            or str(job.get("owner_id") or "") != authorization.owner_id
            or str(job.get("novel_id") or "") != authorization.novel_id
            or job.get("authorization_revision")
            != authorization.authorization_revision
            or job.get("expected_narrative_revision")
            != expected_job_revision
            or existing is None and job.get("status") != "running"
            or existing is not None and job.get("status") != "completed"
            or (
                existing is None
                and chapter_has_content
                and persisted_recovery != binding.model_dump(mode="json")
            )
        ):
            raise RequiredChapterFinalizationJobConflict(
                "required_chapter_finalization_job_binding_stale"
            )
        if parsed_existing is not None:
            result = parsed_existing
            return RequiredChapterFinalizationJobOutcome(
                result=result,
                mutation_receipt=JobMutationReceiptV1(
                    binding=JobMutationRecoveryBindingV1(
                        novel_id=authorization.novel_id,
                        job_id=self._job_id,
                        chapter_id=authorization.chapter_id,
                        readiness_digest=str(job["readiness"]["digest"]),
                        authorization_revision=(
                            authorization.authorization_revision
                        ),
                        expected_narrative_revision=(
                            authorization.narrative_revision
                        ),
                        operation="finalize_chapter_generation",
                        idempotency_key=chapter_finalization_idempotency_key(
                            prose_run_id=result.source_run_id,
                            prose_run_revision=result.source_run_revision,
                            state_proposal_id=result.state_proposal_id,
                        ),
                    ),
                    next_narrative_revision=result.narrative_revision_after,
                ),
            )
        state_job, reviewed_job = (
            await self._repository.read_required_finalization_predecessor_jobs(
                self._job_id,
                authorization.state_candidate.job_id,
                authorization.reviewed_candidate.job_id,
            )
        )
        rebuilt = build_required_chapter_finalization_authorization(
            state_job=state_job,
            reviewed_job=reviewed_job,
            authorization_revision=authorization.authorization_revision,
            created_at=authorization.created_at,
            deadline_at=authorization.deadline_at,
        )
        if rebuilt != authorization:
            raise RequiredChapterFinalizationJobConflict(
                "required_finalization_predecessor_changed"
            )
        await self._repository.bind_job_mutation_recovery(
            self._job_id,
            binding,
        )
        finalization_authorization = ChapterFinalizationAuthorization(
            kind="job_readiness",
            job_id=self._job_id,
            readiness_digest=binding.readiness_digest,
            authorization_revision=binding.authorization_revision,
        )
        finalized = await self._finalizer.recover_completed(
            owner_id=authorization.owner_id,
            novel_id=authorization.novel_id,
            chapter_id=authorization.chapter_id,
            prose_run_id=authorization.reviewed_candidate.source_run_id,
            prose_run_revision=(
                authorization.reviewed_candidate.source_run_revision
            ),
            state_proposal_id=authorization.state_candidate.state_proposal_id,
            authorization=finalization_authorization,
        )
        if finalized is None:
            recovered = await self._recover_state(
                authorization.state_generation_binding
            )
            value = dict(getattr(recovered, "value", recovered) or {})
            if (
                str(value.get("proposal_id") or "")
                != authorization.state_candidate.state_proposal_id
                or not isinstance(value.get("acceptance_token"), str)
                or not value["acceptance_token"]
            ):
                raise RequiredChapterFinalizationJobConflict(
                    "required_finalization_state_proposal_unavailable"
                )
            finalized = await self._finalizer.commit(
                owner_id=authorization.owner_id,
                chapter_id=authorization.chapter_id,
                prose_run_id=authorization.reviewed_candidate.source_run_id,
                prose_run_revision=(
                    authorization.reviewed_candidate.source_run_revision
                ),
                state_proposal_id=authorization.state_candidate.state_proposal_id,
                state_acceptance_token=value["acceptance_token"],
                authorization=finalization_authorization,
                evidence=ChapterFinalizationEvidence(
                    outline_adherence=authorization.outline_adherence,
                    repair_cycles_used=authorization.repair_cycles_used,
                    repair_trace=authorization.repair_trace,
                ),
            )
        result = _build_result(
            job_id=self._job_id,
            readiness_digest=binding.readiness_digest,
            authorization=authorization,
            finalization=finalized,
        )
        return RequiredChapterFinalizationJobOutcome(
            result=result,
            mutation_receipt=JobMutationReceiptV1(
                binding=binding,
                next_narrative_revision=result.narrative_revision_after,
            ),
        )
