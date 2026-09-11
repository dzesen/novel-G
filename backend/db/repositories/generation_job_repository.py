"""generation_jobs 仓储：批量作业记录的 CRUD 与进度追加。"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, TYPE_CHECKING
from uuid import uuid4

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo.results import BulkWriteResult
from pydantic import ValidationError

from backend.db import collections
from backend.db.restored_authorization import (
    RESTORED_AUTHORITY_FIELD, require_current_authorization,
)
from backend.db.base import BaseRepository
from backend.db.errors import NotFoundError
from backend.db.mongo import get_database
from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.utils import get_utc_now, to_object_id
from backend.llm.models import TokenUsage
from backend.services.llm.pre_dispatch_boundaries import (
    AttemptCapacityExceeded,
    TokenBudgetExceeded,
)
from backend.services.generation.attempt_ledger_contracts import (
    MAX_PERSISTED_ATTEMPT_TOKENS,
    validate_launchable_attempt_ledgers,
)
from backend.services.generation.candidate_repair_contracts import (
    MAX_CANDIDATE_PIPELINE_PROGRESS_ENTRIES,
    MAX_CANDIDATE_OUTLINE_SCENES,
    MAX_CHAPTER_CANDIDATE_PIPELINE_CHECKPOINTS,
    AdherenceNotReviewedCheckpointV6,
    CandidatePipelineCheckpointConflict,
    CandidatePipelineCompletionEvidenceV1,
    CandidatePipelineCompletionV1,
    CandidatePipelineCheckpointV1,
    CandidatePipelineProgressV1,
    JobMutationRecoveryBindingV1,
    JobMutationReceiptV1,
    PreDispatchFenceV1,
    STATE_DISPATCH_RESOLUTION_ACTIONS,
    STATE_DISPATCH_RESOLUTION_PHASES,
    StateDispatchResolutionV3,
    candidate_pipeline_checkpoint_digest,
    candidate_pipeline_checkpoint_ledger_digest,
    parse_candidate_pipeline_checkpoint,
    parse_candidate_pipeline_progress,
    validate_candidate_pipeline_completion_chain,
)
from backend.services.generation.candidate_manual_takeover import (
    MAX_CANDIDATE_MANUAL_TAKEOVER_EVENTS,
    CandidateManualTakeoverResolutionV1,
    CandidateManualTakeoverV1,
    parse_candidate_manual_takeover,
    parse_candidate_manual_takeover_resolution,
    validate_candidate_manual_takeover_binding,
)
from backend.services.generation.job_execution import (
    JOB_EXECUTION_LEASE_SECONDS,
    JobExecutionLeaseLost,
    JobExecutionLeaseUnavailable,
    JobExecutionLeaseV1,
    current_job_execution,
)
from backend.services.generation.job_authorization_contracts import (
    OutlineAuthorizationRecalculationCommandV1,
    evaluate_outline_authorization_recalculation,
    parse_prose_authorization,
)

if TYPE_CHECKING:
    from backend.db.required_adherence_journal import JobRequiredReviewJournal, RequiredReviewJobBinding
    from backend.services.generation.independent_outline_review import IndependentReviewPlan
    from backend.services.generation.required_adherence_handoff import RequiredReviewCandidate


USAGE_SUMMARY_LIMIT = 100


MAX_ACTIVE_TOKEN_RESERVATIONS = 32
BOOK_COMPLETION_PUBLICATION_LEASE_SECONDS = 30
BOOK_COMPLETION_PUBLICATION_SCHEMA = "book_completion_audit_publication.v1"
_ATOMIC_JOB_FIELDS = frozenset({
    "candidate_manual_takeover",
    "candidate_manual_takeover_events",
    "candidate_pipeline_checkpoints",
    "chapter_completion_decisions",
    "completion_audit",
    "completion_audit_publication",
    "expected_narrative_revision",
    "execution_epoch",
    "execution_lease",
    "job_mutation_recovery",
    "progress",
    "required_adherence_journal",
    "required_initial_prose_journal",
    "required_prose_rewrite_journal",
    "required_reviewed_candidate",
    "required_state_candidate_journal",
    "required_state_candidate",
    "required_chapter_finalization_result",
    "required_book_successor_journal",
    "required_book_successor_recovery_checkpoint",
    "required_book_successor_action",
    "required_book_successor_parent_job_id",
    "successor_acceptance_outline_journal",
    "state_dispatch_resolution",
})
_LEASED_RUNTIME_PATCH_FIELDS = frozenset({
    "active_slot",
    "authorization_confirmation_required",
    "current_chapter_id",
    "error",
    "incomplete_prose",
    "pause_reason",
    "status",
})
_MAX_NARRATIVE_REVISION = 2**63 - 1


@dataclass(frozen=True)
class ChapterCompletionDecisionFence:
    """Exact authority/ledger snapshot for one completion-decision append."""

    novel_id: Any
    owner_id: Any
    scope: Any
    job_kind: Any
    status: Any
    current_chapter_id: Any
    readiness_digest: Any
    finalization_authorization: Any
    authorization_revision: Any
    execution_epoch: Any
    has_uncertain_attempts: Any
    attempt_slots: Any
    interactive_execution_claim: Any

    @classmethod
    def capture(
        cls,
        job: Mapping[str, Any],
    ) -> "ChapterCompletionDecisionFence":
        readiness = job.get("readiness")
        planning = (
            readiness.get("planning")
            if isinstance(readiness, Mapping)
            else None
        )
        return cls(
            novel_id=deepcopy(job.get("novel_id")),
            owner_id=deepcopy(job.get("owner_id")),
            scope=deepcopy(job.get("scope")),
            job_kind=deepcopy(job.get("job_kind")),
            status=deepcopy(job.get("status")),
            current_chapter_id=deepcopy(job.get("current_chapter_id")),
            readiness_digest=deepcopy(
                readiness.get("digest")
                if isinstance(readiness, Mapping)
                else None
            ),
            finalization_authorization=deepcopy(
                planning.get("chapter_finalization_authorization")
                if isinstance(planning, Mapping)
                else None
            ),
            authorization_revision=deepcopy(
                job.get("authorization_revision")
            ),
            execution_epoch=deepcopy(job.get("execution_epoch")),
            has_uncertain_attempts=deepcopy(
                job.get("has_uncertain_attempts")
            ),
            attempt_slots=deepcopy(job.get("attempt_slots")),
            interactive_execution_claim=deepcopy(
                job.get("interactive_execution_claim")
            ),
        )

    def query(self) -> dict[str, Any]:
        return {
            "novel_id": deepcopy(self.novel_id),
            "owner_id": deepcopy(self.owner_id),
            "scope": deepcopy(self.scope),
            "job_kind": deepcopy(self.job_kind),
            "status": deepcopy(self.status),
            "current_chapter_id": deepcopy(self.current_chapter_id),
            "readiness.digest": deepcopy(self.readiness_digest),
            "readiness.planning.chapter_finalization_authorization": (
                deepcopy(self.finalization_authorization)
            ),
            "authorization_revision": deepcopy(self.authorization_revision),
            "execution_epoch": deepcopy(self.execution_epoch),
            "has_uncertain_attempts": deepcopy(
                self.has_uncertain_attempts
            ),
            "attempt_slots": deepcopy(self.attempt_slots),
            "interactive_execution_claim": deepcopy(
                self.interactive_execution_claim
            ),
        }


def _reject_atomic_field_updates(fields: Dict[str, Any]) -> None:
    if any(
        key == protected
        or (
            isinstance(key, str)
            and key.startswith(f"{protected}.")
        )
        for key in fields
        for protected in _ATOMIC_JOB_FIELDS
    ):
        raise ValueError(
            "Candidate pipeline checkpoints require an atomic repository command"
        )


def _validate_leased_runtime_patch(fields: Mapping[str, Any]) -> None:
    """Keep a worker's generic patch surface away from authority and ledgers."""

    invalid = [
        key
        for key in fields
        if not isinstance(key, str)
        or "." in key
        or key not in _LEASED_RUNTIME_PATCH_FIELDS
    ]
    if invalid:
        raise ValueError(
            "Generation Job worker generic patch contains protected fields"
        )
    confirmation = fields.get("authorization_confirmation_required")
    if "authorization_confirmation_required" in fields and (
        not isinstance(confirmation, Mapping)
        or confirmation.get("requires_confirmation") is not True
    ):
        raise ValueError(
            "Generation Job worker cannot clear authorization confirmation"
        )


def _validated_pre_dispatch_fence(
    fence: Any,
) -> PreDispatchFenceV1:
    if not isinstance(fence, PreDispatchFenceV1):
        raise ValueError("pre-dispatch fence contract is required")
    return PreDispatchFenceV1.model_validate(
        fence.model_dump(mode="python")
    )


def _validate_initial_candidate_ledgers(document: Mapping[str, Any]) -> None:
    if document.get("required_initial_prose_journal") is not None:
        raise ValueError("Required initial prose journal must start empty")
    if document.get("required_prose_rewrite_journal") is not None:
        raise ValueError("Required rewrite journal must start empty")
    if document.get("required_adherence_journal") is not None:
        raise ValueError("Required review journal must start empty")
    if document.get("required_reviewed_candidate") is not None:
        raise ValueError("Required reviewed candidate must start empty")
    if document.get("required_state_candidate_journal") is not None:
        raise ValueError("Required state candidate journal must start empty")
    if document.get("required_state_candidate") is not None:
        raise ValueError("Required state candidate must start empty")
    if document.get("required_chapter_finalization_result") is not None:
        raise ValueError("Required chapter finalization result must start empty")
    if document.get("required_book_successor_journal") is not None:
        raise ValueError("Required book successor journal must start empty")
    if document.get("required_book_successor_recovery_checkpoint") is not None:
        raise ValueError(
            "Required book successor recovery checkpoint must start empty"
        )
    if document.get("required_book_successor_action") is not None:
        raise ValueError("Required book successor action must start empty")
    if document.get("required_book_successor_parent_job_id") is not None:
        raise ValueError("Required book successor parent must start empty")
    if document.get("successor_acceptance_outline_journal") is not None:
        raise ValueError(
            "Successor acceptance outline journal must start empty"
        )
    checkpoints = document.get("candidate_pipeline_checkpoints", [])
    if not isinstance(checkpoints, list) or checkpoints:
        raise ValueError("Candidate checkpoint ledger must start empty")
    progress = document.get("progress", [])
    if (
        not isinstance(progress, list)
        or len(progress) > MAX_CANDIDATE_PIPELINE_PROGRESS_ENTRIES
    ):
        raise ValueError("Generation job progress ledger is invalid")
    if any(
        isinstance(item, Mapping)
        and (
            "candidate_pipeline_completion" in item
            or "job_mutation_receipt" in item
        )
        for item in progress
    ):
        raise ValueError(
            "Candidate completion receipts require an atomic repository command"
        )
    if document.get("job_mutation_recovery") is not None:
        raise ValueError("Job mutation recovery binding must start empty")
    if document.get("candidate_manual_takeover") is not None:
        raise ValueError("Candidate manual takeover binding must start empty")
    takeover_events = document.get("candidate_manual_takeover_events", [])
    if not isinstance(takeover_events, list) or takeover_events:
        raise ValueError("Candidate manual takeover ledger must start empty")
    decisions = document.get("chapter_completion_decisions", [])
    if not isinstance(decisions, list) or decisions:
        raise ValueError("Chapter completion decision ledger must start empty")
    if document.get("state_dispatch_resolution") is not None:
        raise ValueError("State dispatch resolution must start empty")
    if document.get("execution_lease") is not None:
        raise ValueError("Generation job execution lease must start empty")
    execution_epoch = document.get("execution_epoch", 0)
    if type(execution_epoch) is not int or execution_epoch != 0:
        raise ValueError("Generation job execution epoch must start at zero")
    revision = document.get("expected_narrative_revision")
    if revision is not None and (
        type(revision) is not int
        or revision < 0
        or revision > _MAX_NARRATIVE_REVISION
    ):
        raise ValueError("Generation job narrative revision cursor is invalid")


class TokenBudgetUnbounded(TokenBudgetExceeded):
    """A finite budget cannot authorize a call without a conservative bound."""


def _trusted_usage_tokens(usage: TokenUsage) -> int:
    reported_total = max(0, int(usage.total_tokens or 0))
    component_total = max(0, int(usage.input_tokens or 0)) + max(
        0, int(usage.output_tokens or 0)
    )
    return max(reported_total, component_total)


def _live_attempt_transition_fields(
    *,
    source_states: Sequence[str],
    target_state: str,
    now: datetime,
    reason: str | None = None,
) -> dict[str, Any]:
    """Build one atomic, metadata-only transition for both attempt ledgers."""

    source = list(source_states)
    replacement: dict[str, Any] = {
        "state": target_state,
        "updated_at": now,
    }
    if reason is not None:
        replacement["uncertain_reason"] = reason

    def mapped(field: str, alias: str) -> dict[str, Any]:
        return {
            "$map": {
                "input": {"$ifNull": [f"${field}", []]},
                "as": alias,
                "in": {
                    "$cond": [
                        {"$in": [f"$${alias}.state", source]},
                        {
                            "$mergeObjects": [
                                f"$${alias}",
                                replacement,
                            ]
                        },
                        f"$${alias}",
                    ]
                },
            }
        }

    return {
        "attempt_slots": mapped("attempt_slots", "slot"),
        "active_token_reservations": mapped(
            "active_token_reservations",
            "reservation",
        ),
        "uncertain_attempt_ids": {
            "$setUnion": [
                {"$ifNull": ["$uncertain_attempt_ids", []]},
                {
                    "$map": {
                        "input": {
                            "$filter": {
                                "input": {
                                    "$ifNull": ["$attempt_slots", []]
                                },
                                "as": "slot",
                                "cond": {
                                    "$in": ["$$slot.state", source]
                                },
                            }
                        },
                        "as": "slot",
                        "in": "$$slot.attempt_id",
                    }
                },
            ]
        },
    }


def _execution_interruption_pipeline(
    *,
    previous_epoch: int,
    now: datetime,
    reason: str,
) -> list[dict[str, Any]]:
    """Publish one complete execution takeover without an unfenced tail write."""

    interruption_event_id = f"execution-interruption:{previous_epoch + 1}"
    pending_or_uncertain = {
        "$or": [
            {"$eq": ["$has_uncertain_attempts", True]},
            {
                "$gt": [
                    {
                        "$size": {
                            "$filter": {
                                "input": {"$ifNull": ["$attempt_slots", []]},
                                "as": "slot",
                                "cond": {
                                    "$in": [
                                        "$$slot.state",
                                        ["claimed", "uncertain"],
                                    ]
                                },
                            }
                        }
                    },
                    0,
                ]
            },
        ]
    }
    interruption_diagnostic = {
        "schema_version": 1,
        "event_id": interruption_event_id,
        "category": {
            "$cond": [
                pending_or_uncertain,
                "provider_or_transport",
                "unknown_system",
            ]
        },
        "code": {
            "$cond": [
                pending_or_uncertain,
                "provider_attempt_uncertain",
                "process_restart",
            ]
        },
        "evidence": "confirmed",
        "source": "runtime",
        "step": "execution_recovery",
        "chapter_id": {"$ifNull": ["$current_chapter_id", None]},
        "occurred_at": now,
        "impact": {
            "$cond": [
                pending_or_uncertain,
                "provider_attempt_outcome_unknown",
                "execution_interrupted_before_stable_state",
            ]
        },
        "action_codes": {
            "$cond": [
                pending_or_uncertain,
                ["review_provider_attempt", "resume_generation_job"],
                ["resume_generation_job"],
            ]
        },
        "details": {
            "execution_epoch": previous_epoch + 1,
            "has_uncertain_attempts": pending_or_uncertain,
            "reason": reason,
        },
    }
    return [
        {
            "$set": {
                **_live_attempt_transition_fields(
                    source_states=("claimed",),
                    target_state="uncertain",
                    now=now,
                    reason=reason,
                ),
            }
        },
        {
            "$set": {
                "execution_epoch": previous_epoch + 1,
                "execution_lease": "$$REMOVE",
                "status": "interrupted",
                "pause_reason": {
                    "$cond": [
                        pending_or_uncertain,
                        "uncertain_attempt",
                        "process_restart",
                    ]
                },
                "current_chapter_id": {
                    "$cond": [
                        {
                            "$or": [
                                {
                                    "$gt": [
                                        {
                                            "$size": {
                                                "$ifNull": [
                                                    "$candidate_pipeline_checkpoints",
                                                    [],
                                                ]
                                            }
                                        },
                                        0,
                                    ]
                                },
                                {
                                    "$eq": [
                                        {"$type": "$job_mutation_recovery"},
                                        "object",
                                    ]
                                },
                                {
                                    "$eq": [
                                        {"$type": "$required_initial_prose_journal"},
                                        "object",
                                    ]
                                },
                                {
                                    "$eq": [
                                        {"$type": "$required_prose_rewrite_journal"},
                                        "object",
                                    ]
                                },
                                {
                                    "$eq": [
                                        {"$type": "$required_adherence_journal"},
                                        "object",
                                    ]
                                },
                                {
                                    "$eq": [
                                        {"$type": "$required_reviewed_candidate"},
                                        "object",
                                    ]
                                },
                                {
                                    "$eq": [
                                        {
                                            "$type": (
                                                "$required_state_candidate_journal"
                                            )
                                        },
                                        "object",
                                    ]
                                },
                                {
                                    "$eq": [
                                        {"$type": "$required_state_candidate"},
                                        "object",
                                    ]
                                },
                            ]
                        },
                        "$current_chapter_id",
                        None,
                    ]
                },
                "active_slot": None,
                "has_uncertain_attempts": pending_or_uncertain,
                "diagnostics": {
                    "$slice": [
                        {
                            "$concatArrays": [
                                {
                                    "$cond": [
                                        {"$isArray": "$diagnostics"},
                                        "$diagnostics",
                                        [],
                                    ]
                                },
                                [interruption_diagnostic],
                            ]
                        },
                        -200,
                    ]
                },
                "diagnostic_schema_version": 1,
                "current_failure_event_id": interruption_event_id,
                "updated_at": now,
            }
        },
    ]


class AttemptFenceExpired(AttemptCapacityExceeded):
    """A stale worker tried to claim against a replaced dispatch fence."""


class GenerationJobRepository:
    """Generation Job persistence behind one lease-aware surface.

    This repository deliberately uses ``BaseRepository`` by composition.  A
    leased worker must not inherit a new generic CRUD method that bypasses the
    execution authority checks below.
    """

    def __init__(self) -> None:
        self._base = BaseRepository(collections.GENERATION_JOBS)

    def required_review_journal(
        self, binding: RequiredReviewJobBinding, *, candidate: RequiredReviewCandidate,
        plan: IndependentReviewPlan,
    ) -> JobRequiredReviewJournal:
        """Open the opt-in review journal behind this repository's lease fence."""
        from backend.db.required_adherence_journal import JobRequiredReviewJournal

        return JobRequiredReviewJournal(
            binding, candidate=candidate, plan=plan,
            read_job=self.get_job, write_job=self._collection_update_one,
        )

    def required_initial_prose_journal(
        self,
        binding,
        *,
        origin,
        authorization,
    ):
        """Open the single initial-source journal behind the Job lease."""
        from backend.db.required_initial_prose_journal import (
            JobRequiredInitialProseJournal,
        )

        return JobRequiredInitialProseJournal(
            binding,
            origin=origin,
            authorization=authorization,
            read_job=self.get_job,
            write_job=self._collection_update_one,
        )

    def required_rewrite_journal(self, binding, *, authorization, review_plan):
        """Open the candidate-only writer journal with the same Job lease gate."""
        from backend.db.required_prose_rewrite_journal import JobRequiredProseRewriteJournal

        return JobRequiredProseRewriteJournal(
            binding, authorization=authorization, review_plan=review_plan,
            read_job=self.get_job, write_job=self._collection_update_one,
        )

    @staticmethod
    def _assert_unowned_creation() -> None:
        if current_job_execution() is not None:
            raise JobExecutionLeaseLost(
                "Generation Job workers cannot create Generation Jobs"
            )

    @staticmethod
    def _launchable_attempt_filter(
        current: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Freeze a live-attempt-free ledger into the execution-acquire CAS."""

        has_uncertain = current.get("has_uncertain_attempts", False)
        if type(has_uncertain) is not bool:
            raise JobExecutionLeaseUnavailable(
                "Generation Job uncertainty marker is invalid"
            )
        if has_uncertain:
            raise JobExecutionLeaseUnavailable(
                "Generation Job has uncertain Provider attempts"
            )
        require_current_authorization(current)
        exact_fields: list[dict[str, Any]] = [{RESTORED_AUTHORITY_FIELD: None}]
        if "has_uncertain_attempts" in current:
            exact_fields.append({"has_uncertain_attempts": False})
        else:
            exact_fields.append({
                "has_uncertain_attempts": {"$exists": False}
            })
        capacity = current.get("usage_attempt_capacity", 0)
        claimed = current.get("usage_attempt_claimed", 0)
        raw_attempts = current.get("attempt_slots", [])
        raw_reservations = current.get("active_token_reservations", [])
        raw_accounted_ids = current.get("usage_attempt_ids", [])
        tokens_reserved = current.get("tokens_reserved", 0)
        tokens_used = current.get("tokens_used", 0)
        token_budget = current.get("token_budget")
        try:
            validate_launchable_attempt_ledgers(
                attempt_slots=raw_attempts,
                active_token_reservations=raw_reservations,
                usage_attempt_ids=raw_accounted_ids,
                attempt_capacity=capacity,
                attempts_claimed=claimed,
                tokens_used=tokens_used,
                tokens_reserved=tokens_reserved,
                token_budget=token_budget,
                maximum_active_reservations=MAX_ACTIVE_TOKEN_RESERVATIONS,
            )
        except ValueError as exc:
            raise JobExecutionLeaseUnavailable(
                "Generation Job attempt or usage ledger is invalid"
            ) from exc
        for field, raw_items in (
            ("attempt_slots", raw_attempts),
            ("active_token_reservations", raw_reservations),
            ("usage_attempt_ids", raw_accounted_ids),
        ):
            if field in current:
                exact_fields.append({field: raw_items})
            else:
                exact_fields.append({field: {"$exists": False}})
        exact_fields.extend([
            {"usage_attempt_capacity": capacity},
            {"usage_attempt_claimed": claimed},
            {"tokens_reserved": tokens_reserved},
            {"tokens_used": tokens_used},
            {"token_budget": token_budget},
        ])
        return {"$and": exact_fields}

    @staticmethod
    def _execution_authority_filter(
        lease: JobExecutionLeaseV1,
        *,
        now: datetime,
        require_live: bool = True,
    ) -> dict[str, Any]:
        """Match one strict V1 lease without freezing heartbeat timestamps."""

        expires_at: dict[str, Any] = {"$type": "date"}
        if require_live:
            expires_at["$gt"] = now
        return {
            "$and": [
                {
                    "_id": to_object_id(lease.job_id),
                    RESTORED_AUTHORITY_FIELD: None,
                    "execution_epoch": lease.epoch,
                    "execution_lease.schema_version": (
                        "job_execution_lease.v1"
                    ),
                    "execution_lease.job_id": lease.job_id,
                    "execution_lease.worker_id": lease.worker_id,
                    "execution_lease.epoch": lease.epoch,
                    "execution_lease.heartbeat_at": {"$type": "date"},
                    "execution_lease.expires_at": expires_at,
                },
                {
                    "$expr": {
                        "$and": [
                            {
                                "$in": [
                                    {"$type": "$execution_epoch"},
                                    ["int", "long"],
                                ]
                            },
                            {
                                "$in": [
                                    {"$type": "$execution_lease.epoch"},
                                    ["int", "long"],
                                ]
                            },
                            {
                                "$eq": [
                                    {
                                        "$size": {
                                            "$objectToArray": {
                                                "$ifNull": [
                                                    "$execution_lease",
                                                    {},
                                                ]
                                            }
                                        }
                                    },
                                    6,
                                ]
                            },
                            {
                                "$gt": [
                                    "$execution_lease.expires_at",
                                    "$execution_lease.heartbeat_at",
                                ]
                            },
                        ]
                    }
                },
            ]
        }

    @staticmethod
    def _execution_filter(query: Mapping[str, Any]) -> dict[str, Any]:
        """Inject the current worker lease into every owned Job operation."""

        base = dict(query)
        lease = current_job_execution()
        if lease is None:
            return base
        expected_id = to_object_id(lease.job_id)
        supplied_id = base.get("_id")
        if supplied_id is not None and supplied_id != expected_id:
            raise JobExecutionLeaseLost(
                "Generation Job worker attempted to access another Job"
            )
        return {
            "$and": [
                base,
                GenerationJobRepository._execution_authority_filter(
                    lease,
                    now=get_utc_now(),
                ),
            ]
        }

    async def _collection_update_one(
        self,
        query: Mapping[str, Any],
        update: Mapping[str, Any] | Sequence[Mapping[str, Any]],
        **kwargs: Any,
    ) -> Any:
        update_document: Any = (
            [dict(stage) for stage in update]
            if isinstance(update, Sequence) and not isinstance(update, Mapping)
            else dict(update)
        )
        result = await self._base.collection.update_one(
            self._execution_filter(query),
            update_document,
            **kwargs,
        )
        if result.matched_count == 0:
            await self._assert_execution_current()
        return result

    async def _collection_find_one_and_update(
        self,
        query: Mapping[str, Any],
        update: Mapping[str, Any] | Sequence[Mapping[str, Any]],
        **kwargs: Any,
    ) -> Any:
        update_document: Any = (
            [dict(stage) for stage in update]
            if isinstance(update, Sequence) and not isinstance(update, Mapping)
            else dict(update)
        )
        document = await self._base.collection.find_one_and_update(
            self._execution_filter(query),
            update_document,
            **kwargs,
        )
        if document is None:
            await self._assert_execution_current()
        return document

    async def _assert_execution_current(self) -> None:
        """Raise only when the task-local lease itself is no longer valid."""

        lease = current_job_execution()
        if lease is None:
            return
        current = await self._base.collection.find_one({
            "$and": [
                {"is_deleted": False},
                self._execution_authority_filter(
                    lease,
                    now=get_utc_now(),
                ),
            ]
        })
        if current is None:
            raise JobExecutionLeaseLost(
                "Generation Job execution lease is no longer current"
            )

    async def _assert_worker_generic_patch_allowed(
        self,
        fields: Mapping[str, Any],
    ) -> None:
        lease = current_job_execution()
        if lease is None:
            return
        await self._assert_execution_current()
        _validate_leased_runtime_patch(fields)
        current: Mapping[str, Any] | None = None
        if fields.get("status") == "completed":
            current = await self.get_job(lease.job_id)
        if (
            fields.get("status") == "completed"
            and current is not None
            and current.get("scope") == "book"
        ):
            raise ValueError(
                "Book Job completion audit requires its atomic publish command"
            )

    async def _reject_worker_generic_mutation(self, operation: str) -> None:
        if current_job_execution() is None:
            return
        await self._assert_execution_current()
        raise ValueError(
            f"Generation Job worker cannot use generic {operation}"
        )

    async def acquire_execution_lease(
        self,
        job_id: str,
        worker_id: str,
        *,
        now: datetime,
        expires_at: datetime,
    ) -> JobExecutionLeaseV1:
        """Atomically consume an optional retry receipt and own Job execution."""

        if expires_at <= now:
            raise ValueError("Generation job execution lease expiry is invalid")
        current = await self.get_job(job_id)
        require_current_authorization(current)
        current_status = str(current.get("status") or "")
        raw_resolution = current.get("state_dispatch_resolution")
        resolution_query: dict[str, Any]
        if raw_resolution is None:
            if current_status != "running":
                raise JobExecutionLeaseUnavailable(
                    "Generation Job is not ready to acquire an execution lease"
                )
            resolution_query = {"state_dispatch_resolution": None}
        else:
            try:
                resolution = StateDispatchResolutionV3.model_validate(
                    raw_resolution
                )
            except (TypeError, ValueError) as exc:
                raise JobExecutionLeaseUnavailable(
                    "Generation Job retry launch receipt is invalid"
                ) from exc
            if (
                resolution.action != "retry"
                or resolution.phase != "job_transitioned"
                or current_status not in {"running", "interrupted"}
            ):
                raise JobExecutionLeaseUnavailable(
                    "Generation Job action is not ready to launch"
                )
            resolution_query = {
                "state_dispatch_resolution": resolution.model_dump(mode="json")
            }
        launchable_attempts = self._launchable_attempt_filter(current)
        previous_epoch = current.get("execution_epoch", 0)
        if (
            type(previous_epoch) is not int
            or previous_epoch < 0
            or previous_epoch >= _MAX_NARRATIVE_REVISION
        ):
            raise JobExecutionLeaseUnavailable(
                "Generation Job execution epoch is invalid"
            )
        raw_lease = current.get("execution_lease")
        existing: JobExecutionLeaseV1 | None = None
        if raw_lease is not None:
            if current_status != "running":
                raise JobExecutionLeaseUnavailable(
                    "Interrupted Generation Job retained an execution lease"
                )
            try:
                existing = JobExecutionLeaseV1.model_validate(raw_lease)
            except (TypeError, ValueError) as exc:
                raise JobExecutionLeaseUnavailable(
                    "Persisted Generation Job execution lease is invalid"
                ) from exc
            if existing.job_id != str(job_id):
                raise JobExecutionLeaseUnavailable(
                    "Persisted Generation Job execution lease changed scope"
                )
            if existing.epoch != previous_epoch:
                raise JobExecutionLeaseUnavailable(
                    "Persisted Generation Job execution lease epoch diverged"
                )
            if existing.expires_at > now:
                if existing.worker_id == str(worker_id):
                    return existing
                raise JobExecutionLeaseUnavailable(
                    "Generation Job already has a live execution worker"
                )
        next_epoch = previous_epoch + 1
        try:
            lease = JobExecutionLeaseV1(
                schema_version="job_execution_lease.v1",
                job_id=str(job_id),
                worker_id=str(worker_id),
                epoch=next_epoch,
                heartbeat_at=now,
                expires_at=expires_at,
            )
        except (TypeError, ValueError) as exc:
            raise JobExecutionLeaseUnavailable(
                "Generation Job execution lease command is invalid"
            ) from exc

        epoch_query: dict[str, Any] = {"execution_epoch": previous_epoch}
        if previous_epoch == 0:
            epoch_query = {
                "$or": [
                    {"execution_epoch": 0},
                    {"execution_epoch": {"$exists": False}},
                ]
            }
        query: dict[str, Any] = {
            "$and": [
                {
                    "_id": to_object_id(job_id),
                    "is_deleted": False,
                    "status": current_status,
                    **resolution_query,
                },
                epoch_query,
                launchable_attempts,
                (
                    {"execution_lease": existing.model_dump(mode="python")}
                    if existing is not None
                    else {
                        "$or": [
                            {"execution_lease": {"$exists": False}},
                            {"execution_lease": None},
                        ]
                    }
                ),
            ]
        }
        update: dict[str, Any] = {
            "$set": {
                "status": "running",
                "pause_reason": None,
                "error": None,
                "active_slot": "global",
                "execution_epoch": next_epoch,
                "execution_lease": lease.model_dump(mode="python"),
                "updated_at": now,
            }
        }
        if raw_resolution is not None:
            update["$unset"] = {"state_dispatch_resolution": ""}
        document = await self._base.collection.find_one_and_update(
            query,
            update,
            return_document=ReturnDocument.AFTER,
        )
        if document is not None:
            return lease
        latest = await self.get_job(job_id)
        try:
            existing = JobExecutionLeaseV1.model_validate(
                latest.get("execution_lease")
            )
        except (TypeError, ValueError) as exc:
            raise JobExecutionLeaseUnavailable(
                "Generation Job execution lease raced"
            ) from exc
        latest_epoch = latest.get("execution_epoch")
        if (
            existing.worker_id == worker_id
            and existing.job_id == str(job_id)
            and type(latest_epoch) is int
            and existing.epoch == latest_epoch
            and existing.expires_at > now
        ):
            return existing
        raise JobExecutionLeaseUnavailable(
            "Generation Job execution lease is owned by another worker"
        )

    async def heartbeat_execution_lease(
        self,
        lease: JobExecutionLeaseV1,
        *,
        now: datetime,
        expires_at: datetime,
    ) -> JobExecutionLeaseV1 | None:
        """Extend one live lease; return ``None`` after ownership is lost."""

        frozen = JobExecutionLeaseV1.model_validate(
            lease.model_dump(mode="python")
        )
        if expires_at <= now:
            raise ValueError("Generation job execution heartbeat is invalid")
        renewed = frozen.model_copy(
            update={"heartbeat_at": now, "expires_at": expires_at}
        )
        result = await self._base.collection.update_one(
            {
                "$and": [
                    {"is_deleted": False, "status": "running"},
                    self._execution_authority_filter(
                        frozen,
                        now=now,
                    ),
                ]
            },
            {
                "$set": {
                    "execution_lease": renewed.model_dump(mode="python"),
                    "updated_at": now,
                }
            },
        )
        return renewed if result.modified_count == 1 else None

    async def release_execution_lease(
        self,
        lease: JobExecutionLeaseV1,
    ) -> bool:
        """Release terminal work or atomically interrupt unfinished work."""

        frozen = JobExecutionLeaseV1.model_validate(
            lease.model_dump(mode="python")
        )
        now = get_utc_now()
        interrupted = await self._base.collection.update_one(
            {
                "$and": [
                    {"is_deleted": False, "status": "running"},
                    self._execution_authority_filter(
                        frozen,
                        now=now,
                        require_live=False,
                    ),
                ]
            },
            _execution_interruption_pipeline(
                previous_epoch=frozen.epoch,
                now=now,
                reason="execution worker stopped before reaching a stable state",
            ),
        )
        if interrupted.modified_count == 1:
            return True
        result = await self._base.collection.update_one(
            {
                "$and": [
                    {
                        "is_deleted": False,
                        "status": {"$ne": "running"},
                    },
                    self._execution_authority_filter(
                        frozen,
                        now=now,
                        require_live=False,
                    ),
                ]
            },
            {
                "$unset": {"execution_lease": ""},
                "$set": {"updated_at": now},
            },
        )
        if result.modified_count == 1:
            return True
        latest = await self._base.collection.find_one({
            "_id": to_object_id(frozen.job_id),
            "is_deleted": False,
        })
        if latest is None:
            return False
        if (
            latest.get("status") == "running"
            and latest.get("execution_lease") is None
            and latest.get("execution_epoch") == frozen.epoch
        ):
            return await self.interrupt_stale_execution(
                frozen.job_id,
                now=now,
            )
        return latest.get("execution_lease") is None

    async def interrupt_stale_execution(
        self,
        job_id: str,
        *,
        now: datetime,
    ) -> bool:
        """Fence only a missing/expired worker before startup recovery writes."""

        current = await self.get_job(job_id)
        if str(current.get("status") or "") != "running":
            return False
        previous_epoch = current.get("execution_epoch", 0)
        if (
            type(previous_epoch) is not int
            or previous_epoch < 0
            or previous_epoch >= _MAX_NARRATIVE_REVISION
        ):
            return False
        raw_lease = current.get("execution_lease")
        if raw_lease is None:
            lease_query: dict[str, Any] = {
                "$or": [
                    {"execution_lease": {"$exists": False}},
                    {"execution_lease": None},
                ]
            }
        else:
            try:
                lease = JobExecutionLeaseV1.model_validate(raw_lease)
            except (TypeError, ValueError):
                return False
            if (
                lease.job_id != str(job_id)
                or lease.epoch != previous_epoch
                or lease.expires_at > now
            ):
                return False
            lease_query = {
                "execution_lease": lease.model_dump(mode="python")
            }
        epoch_query: dict[str, Any] = {"execution_epoch": previous_epoch}
        if previous_epoch == 0:
            epoch_query = {
                "$or": [
                    {"execution_epoch": 0},
                    {"execution_epoch": {"$exists": False}},
                ]
            }
        result = await self._base.collection.update_one(
            {
                "$and": [
                    {
                        "_id": to_object_id(job_id),
                        "is_deleted": False,
                        "status": "running",
                    },
                    epoch_query,
                    lease_query,
                ]
            },
            _execution_interruption_pipeline(
                previous_epoch=previous_epoch,
                now=now,
                reason="backend process interrupted before usage was recorded",
            ),
        )
        return result.modified_count == 1

    async def create_job(self, data: Dict[str, Any]) -> str:
        self._assert_unowned_creation()
        return await self.insert_one(dict(data))

    async def ensure_successor_acceptance_outline_control_job(
        self,
        document: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Create or recover the deterministic outline control Job."""

        from backend.evaluation.required_book_successor_acceptance_outline import (
            SUCCESSOR_ACCEPTANCE_OUTLINE_JOB_KIND,
            parse_successor_acceptance_outline_journal,
        )

        self._assert_unowned_creation()
        candidate = deepcopy(dict(document))
        for identity_field in ("_id", "novel_id", "owner_id"):
            identity_value = candidate.get(identity_field)
            if identity_value is None:
                raise ValueError(
                    "Successor acceptance outline control Job identity is invalid"
                )
            candidate[identity_field] = to_object_id(identity_value)
        _validate_initial_candidate_ledgers(candidate)
        if (
            candidate.get("job_kind")
            != SUCCESSOR_ACCEPTANCE_OUTLINE_JOB_KIND
            or candidate.get("successor_acceptance_outline_journal")
            is not None
            or candidate.get("is_deleted") is not False
        ):
            raise ValueError(
                "Successor acceptance outline control Job is invalid"
            )
        existing = await self._base.collection.find_one({
            "_id": candidate["_id"],
        })
        if existing is None:
            try:
                await self._base.insert_one(candidate)
            except DuplicateKeyError:
                pass
            existing = await self._base.collection.find_one({
                "_id": candidate["_id"],
            })
        if existing is None:
            raise ValueError(
                "Successor acceptance outline control Job was not durable"
            )
        immutable_fields = (
            "_id",
            "novel_id",
            "owner_id",
            "scope",
            "volume_id",
            "job_kind",
            "token_budget",
            "usage_attempt_capacity",
            "authorization_revision",
            "generation_params",
            "readiness",
            "successor_acceptance_claim",
            "progress",
            "is_deleted",
        )
        if (
            any(
                existing.get(field) != candidate.get(field)
                for field in immutable_fields
            )
            or existing.get("status")
            not in {"running", "paused", "interrupted"}
            or type(existing.get("expected_narrative_revision")) is not int
            or not 0 <= existing["expected_narrative_revision"] <= 3
        ):
            raise ValueError(
                "Successor acceptance outline control Job changed"
            )
        raw_journal = existing.get("successor_acceptance_outline_journal")
        if raw_journal is not None:
            parse_successor_acceptance_outline_journal(raw_journal)
        return existing

    async def begin_successor_acceptance_outline(
        self,
        job_id: str,
        request,
    ) -> bool:
        """Persist one ordered outline request before any paid dispatch."""

        from backend.evaluation.required_book_successor_acceptance_outline import (
            begin_successor_acceptance_outline_value,
            parse_successor_acceptance_outline_journal,
        )

        current = await self.get_job(job_id)
        next_journal, changed = begin_successor_acceptance_outline_value(
            current,
            request,
        )
        if not changed:
            return False
        raw = current.get("successor_acceptance_outline_journal")
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "status": "running",
                "expected_narrative_revision": (
                    request.expected_narrative_revision
                ),
                "successor_acceptance_outline_journal": raw,
            },
            {
                "$set": {
                    "successor_acceptance_outline_journal": (
                        next_journal.model_dump(mode="json")
                    ),
                    "current_chapter_id": request.chapter_id,
                    "updated_at": get_utc_now(),
                }
            },
        )
        if result.modified_count == 1:
            return True
        latest = await self.get_job(job_id)
        stored = parse_successor_acceptance_outline_journal(
            latest.get("successor_acceptance_outline_journal")
        )
        if stored == next_journal:
            return False
        raise ValueError(
            "Successor acceptance outline request CAS was lost"
        )

    async def publish_successor_acceptance_outline_candidate(
        self,
        job_id: str,
        request,
        provider_outline: Mapping[str, Any],
        attempt_ids: Sequence[str],
    ) -> bool:
        """Persist the paid Provider candidate before formal acceptance."""

        from backend.evaluation.required_book_successor_acceptance_outline import (
            parse_successor_acceptance_outline_journal,
            publish_successor_acceptance_outline_candidate_value,
        )

        current = await self.get_job(job_id)
        previous, updated = (
            publish_successor_acceptance_outline_candidate_value(
                current,
                request,
                provider_outline,
                attempt_ids,
            )
        )
        if previous == updated:
            return False
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "status": "running",
                "expected_narrative_revision": (
                    request.expected_narrative_revision
                ),
                "successor_acceptance_outline_journal": (
                    previous.model_dump(mode="json")
                ),
                "attempt_slots": deepcopy(current.get("attempt_slots")),
                "attempt_reservation": None,
            },
            {
                "$set": {
                    "successor_acceptance_outline_journal": (
                        updated.model_dump(mode="json")
                    ),
                    "updated_at": get_utc_now(),
                }
            },
        )
        if result.modified_count == 1:
            return True
        latest = await self.get_job(job_id)
        stored = parse_successor_acceptance_outline_journal(
            latest.get("successor_acceptance_outline_journal")
        )
        if stored == updated:
            return False
        raise ValueError(
            "Successor acceptance outline candidate CAS was lost"
        )

    async def publish_successor_acceptance_outline_accepted(
        self,
        job_id: str,
        request,
        formal_outline_revision: str,
    ) -> bool:
        """Publish formal acceptance and its revision cursor atomically."""

        from backend.evaluation.required_book_successor_acceptance_outline import (
            SUCCESSOR_ACCEPTANCE_OUTLINES_READY,
            parse_successor_acceptance_outline_journal,
            publish_successor_acceptance_outline_accepted_value,
        )

        current = await self.get_job(job_id)
        previous, updated = (
            publish_successor_acceptance_outline_accepted_value(
                current,
                request,
                formal_outline_revision,
            )
        )
        if previous == updated:
            return False
        pause_reason = (
            SUCCESSOR_ACCEPTANCE_OUTLINES_READY
            if request.chapter_order == 3
            else "successor_acceptance_outline_chapter_ready"
        )
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "status": "running",
                "expected_narrative_revision": (
                    request.expected_narrative_revision
                ),
                "successor_acceptance_outline_journal": (
                    previous.model_dump(mode="json")
                ),
                "attempt_reservation": None,
            },
            {
                "$set": {
                    "successor_acceptance_outline_journal": (
                        updated.model_dump(mode="json")
                    ),
                    "expected_narrative_revision": (
                        request.expected_narrative_revision + 1
                    ),
                    "status": "paused",
                    "pause_reason": pause_reason,
                    "active_slot": None,
                    "current_chapter_id": None,
                    "error": None,
                    "updated_at": get_utc_now(),
                }
            },
        )
        if result.modified_count == 1:
            return True
        latest = await self.get_job(job_id)
        stored = parse_successor_acceptance_outline_journal(
            latest.get("successor_acceptance_outline_journal")
        )
        if (
            stored == updated
            and latest.get("expected_narrative_revision")
            == request.expected_narrative_revision + 1
            and latest.get("status") == "paused"
            and latest.get("pause_reason") == pause_reason
        ):
            return False
        raise ValueError(
            "Successor acceptance formal outline CAS was lost"
        )

    async def block_successor_acceptance_outline(
        self,
        job_id: str,
        request,
        failure_code: str,
    ) -> bool:
        """Stop this outline stage without erasing its paid evidence."""

        from backend.evaluation.required_book_successor_acceptance_outline import (
            block_successor_acceptance_outline_value,
            parse_successor_acceptance_outline_journal,
            validate_successor_acceptance_outline_attempts,
        )

        current = await self.get_job(job_id)
        attempts = validate_successor_acceptance_outline_attempts(
            current,
            request,
        )
        if any(item.get("state") == "claimed" for item in attempts):
            raise ValueError(
                "Successor acceptance outline attempt is still in flight"
            )
        previous, updated = block_successor_acceptance_outline_value(
            current,
            request,
            failure_code,
        )
        if previous == updated:
            return False
        current_status = str(current.get("status") or "")
        if current_status not in {"running", "interrupted"}:
            raise ValueError(
                "Successor acceptance outline block state changed"
            )
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "status": current_status,
                "expected_narrative_revision": (
                    request.expected_narrative_revision
                ),
                "successor_acceptance_outline_journal": (
                    previous.model_dump(mode="json")
                ),
                "attempt_slots": deepcopy(current.get("attempt_slots")),
            },
            {
                "$set": {
                    "successor_acceptance_outline_journal": (
                        updated.model_dump(mode="json")
                    ),
                    "status": "paused",
                    "pause_reason": "successor_acceptance_outline_blocked",
                    "active_slot": None,
                    "current_chapter_id": None,
                    "attempt_reservation": None,
                    "error": {
                        "step": "successor_acceptance_outline",
                        "code": str(failure_code),
                        "reason_codes": [str(failure_code)],
                    },
                    "updated_at": get_utc_now(),
                }
            },
        )
        if result.modified_count == 1:
            return True
        latest = await self.get_job(job_id)
        stored = parse_successor_acceptance_outline_journal(
            latest.get("successor_acceptance_outline_journal")
        )
        if (
            stored == updated
            and latest.get("status") == "paused"
            and latest.get("pause_reason")
            == "successor_acceptance_outline_blocked"
        ):
            return False
        raise ValueError(
            "Successor acceptance outline block CAS was lost"
        )

    async def resume_successor_acceptance_outline(
        self,
        job_id: str,
        request,
    ) -> bool:
        """Resume only a safe ordered outline prefix or interrupted replay."""

        from backend.evaluation.required_book_successor_acceptance_outline import (
            parse_successor_acceptance_outline_journal,
            validate_successor_acceptance_outline_attempts,
            validate_successor_acceptance_outline_control_job,
        )

        current = await self.get_job(job_id)
        validate_successor_acceptance_outline_control_job(current, request)
        status = str(current.get("status") or "")
        raw_journal = current.get("successor_acceptance_outline_journal")
        journal = (
            parse_successor_acceptance_outline_journal(raw_journal)
            if raw_journal is not None
            else None
        )
        if status == "paused":
            if (
                current.get("pause_reason")
                != "successor_acceptance_outline_chapter_ready"
                or journal is None
                or request.chapter_order <= 1
                or len(journal.entries) != request.chapter_order - 1
                or any(entry.phase != "accepted" for entry in journal.entries)
            ):
                raise ValueError(
                    "Successor acceptance outline resume prefix changed"
                )
        elif status == "interrupted":
            if raw_journal is None:
                if request.chapter_order != 1:
                    raise ValueError(
                        "Successor acceptance outline interrupted prefix changed"
                    )
            else:
                assert journal is not None
                index = request.chapter_order - 1
                request_not_started = (
                    index == len(journal.entries)
                    and request.chapter_order == len(journal.entries) + 1
                    and all(
                        entry.phase == "accepted"
                        for entry in journal.entries
                    )
                )
                if not request_not_started and (
                    index >= len(journal.entries)
                    or journal.entries[index].request != request
                    or journal.entries[index].phase
                    not in {"reserved", "produced"}
                ):
                    raise ValueError(
                        "Successor acceptance outline interrupted prefix changed"
                    )
                if request_not_started:
                    attempts = ()
                    entry_phase = None
                else:
                    attempts = validate_successor_acceptance_outline_attempts(
                        current,
                        request,
                    )
                    entry_phase = journal.entries[index].phase
                if any(
                    item.get("state") in {"claimed", "uncertain"}
                    for item in attempts
                ):
                    raise ValueError(
                        "Successor acceptance outline interrupted attempt is unsettled"
                    )
                if (
                    entry_phase == "reserved"
                    and any(
                        item.get("state") == "accounted"
                        for item in attempts
                    )
                ):
                    raise ValueError(
                        "Successor acceptance outline result confirmation was lost"
                    )
        else:
            raise ValueError(
                "Successor acceptance outline Job is not resumable"
            )
        if (
            current.get("has_uncertain_attempts") is not False
            or current.get("active_token_reservations") != []
            or any(
                item.get("state") in {"claimed", "uncertain"}
                for item in list(current.get("attempt_slots") or [])
            )
        ):
            raise ValueError(
                "Successor acceptance outline Job has unsettled attempts"
            )
        previous_epoch = current.get("execution_epoch")
        if type(previous_epoch) is not int or not 0 <= previous_epoch < (
            _MAX_NARRATIVE_REVISION
        ):
            raise ValueError(
                "Successor acceptance outline execution epoch is invalid"
            )
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "status": status,
                "execution_epoch": previous_epoch,
                "successor_acceptance_outline_journal": raw_journal,
                "attempt_slots": deepcopy(current.get("attempt_slots")),
                "active_token_reservations": [],
                "has_uncertain_attempts": False,
                "expected_narrative_revision": (
                    request.expected_narrative_revision
                ),
            },
            {
                "$inc": {"execution_epoch": 1},
                "$unset": {"execution_lease": ""},
                "$set": {
                    "status": "running",
                    "pause_reason": None,
                    "active_slot": "global",
                    "current_chapter_id": None,
                    "error": None,
                    "updated_at": get_utc_now(),
                },
            },
        )
        if result.modified_count == 1:
            return True
        latest = await self.get_job(job_id)
        if (
            latest.get("status") == "running"
            and latest.get("successor_acceptance_outline_journal")
            == raw_journal
            and latest.get("expected_narrative_revision")
            == request.expected_narrative_revision
            and latest.get("execution_epoch") == previous_epoch + 1
            and latest.get("execution_lease") is None
        ):
            return False
        raise ValueError(
            "Successor acceptance outline resume CAS was lost"
        )

    async def initialize_required_book_successor(
        self,
        job_id: str,
    ):
        """Create the root journal once under the root Job execution lease."""

        from backend.services.generation.required_book_successor import (
            RequiredBookSuccessorCoordinator,
            parse_required_book_successor_journal,
            readiness_uses_required_book_successor,
        )

        current = await self.get_job(job_id)
        if not readiness_uses_required_book_successor(current.get("readiness")):
            raise ValueError("Required book successor authority is unavailable")
        coordinator = RequiredBookSuccessorCoordinator(
            coordinator_job_id=str(job_id),
            readiness=current["readiness"],
        )
        initial = coordinator.initial_journal()
        raw = current.get("required_book_successor_journal")
        if raw is not None:
            stored = parse_required_book_successor_journal(raw)
            coordinator.next_action(stored)
            return stored
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "status": "running",
                "required_book_successor_journal": None,
            },
            {
                "$set": {
                    "required_book_successor_journal": initial.model_dump(
                        mode="json"
                    ),
                    "job_kind": "required_book_successor",
                }
            },
        )
        if result.modified_count != 1:
            latest = await self.get_job(job_id)
            stored = parse_required_book_successor_journal(
                latest.get("required_book_successor_journal")
            )
            coordinator.next_action(stored)
            return stored
        return initial

    async def create_required_book_successor_child(
        self,
        parent_job_id: str,
        *,
        action,
        document: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Idempotently insert the one child authorized by the root journal."""

        from backend.services.generation.required_book_successor import (
            RequiredBookSuccessorCoordinator,
            parse_required_book_successor_action,
            parse_required_book_successor_journal,
        )

        parent = await self.get_job(parent_job_id)
        coordinator = RequiredBookSuccessorCoordinator(
            coordinator_job_id=str(parent_job_id),
            readiness=parent.get("readiness") or {},
        )
        journal = parse_required_book_successor_journal(
            parent.get("required_book_successor_journal")
        )
        parsed_action = parse_required_book_successor_action(action)
        candidate = deepcopy(dict(document))
        raw_child_id = candidate.pop("_id", None)
        child_object_id = (
            to_object_id(raw_child_id)
            if raw_child_id is not None
            else ObjectId()
        )
        child_id = str(child_object_id)
        _validate_initial_candidate_ledgers(candidate)
        candidate.update({
            "_id": child_object_id,
            "required_book_successor_action": parsed_action.model_dump(
                mode="json"
            ),
            "required_book_successor_parent_job_id": to_object_id(
                parent_job_id
            ),
            "active_slot": f"required_book_successor_child:{parent_job_id}",
            "job_kind": f"required_book_successor_{parsed_action.stage}",
        })
        coordinator.validate_child_authority(
            journal,
            parsed_action,
            candidate,
        )
        await self._assert_execution_current()
        existing = await self._base.collection.find_one({
            "is_deleted": False,
            "required_book_successor_parent_job_id": to_object_id(
                parent_job_id
            ),
            "required_book_successor_action.action_digest": (
                parsed_action.action_digest
            ),
        })
        if existing is None:
            try:
                await self._base.insert_one(candidate)
            except DuplicateKeyError:
                pass
            existing = await self._base.collection.find_one({
                "is_deleted": False,
                "required_book_successor_parent_job_id": to_object_id(
                    parent_job_id
                ),
                "required_book_successor_action.action_digest": (
                    parsed_action.action_digest
                ),
            })
        if existing is None:
            raise JobExecutionLeaseLost(
                "Required book successor child creation was not durable"
            )
        if (
            existing.get("required_book_successor_parent_job_id")
            != to_object_id(parent_job_id)
            or existing.get("readiness") != candidate.get("readiness")
            or existing.get("required_book_successor_action")
            != candidate.get("required_book_successor_action")
        ):
            raise ValueError("Required book successor child identity changed")
        coordinator.validate_child_authority(
            journal,
            parsed_action,
            existing,
        )
        return existing

    async def read_required_book_successor_child(
        self,
        parent_job_id: str,
        child_job_id: str,
    ) -> Dict[str, Any]:
        """Read only a current or previously accepted child of this root."""

        from backend.services.generation.required_book_successor import (
            RequiredBookSuccessorCoordinator,
            parse_required_book_successor_action,
            parse_required_book_successor_journal,
        )

        parent = await self.get_job(parent_job_id)
        coordinator = RequiredBookSuccessorCoordinator(
            coordinator_job_id=str(parent_job_id),
            readiness=parent.get("readiness") or {},
        )
        journal = parse_required_book_successor_journal(
            parent.get("required_book_successor_journal")
        )
        allowed = {item.child_job_id for item in journal.stages}
        next_action = coordinator.next_action(journal)
        if (
            str(child_job_id) not in allowed
            and (next_action is None or next_action.stage == "book_audit")
        ):
            raise ValueError("Required book successor child read is stale")
        child = await self._base.collection.find_one({
            "_id": to_object_id(child_job_id),
            "is_deleted": False,
            "required_book_successor_parent_job_id": to_object_id(
                parent_job_id
            ),
        })
        if child is None:
            raise NotFoundError(
                f"Required book successor child not found: {child_job_id}"
            )
        stored_action = parse_required_book_successor_action(
            child.get("required_book_successor_action")
        )
        matching_record = next(
            (
                item
                for item in journal.stages
                if item.child_job_id == str(child_job_id)
            ),
            None,
        )
        if matching_record is not None:
            if matching_record.action_digest != stored_action.action_digest:
                raise ValueError("Required book successor child action changed")
        elif next_action != stored_action:
            raise ValueError("Required book successor current child changed")
        return child

    async def advance_required_book_successor_child(
        self,
        parent_job_id: str,
        child_job_id: str,
    ):
        """Atomically append one fully validated child result to the root."""

        from backend.services.generation.required_book_successor import (
            RequiredBookSuccessorCoordinator,
            parse_required_book_successor_journal,
        )
        parent = await self.get_job(parent_job_id)
        raw_journal = parent.get("required_book_successor_journal")
        journal = parse_required_book_successor_journal(raw_journal)
        child = await self.read_required_book_successor_child(
            parent_job_id,
            child_job_id,
        )
        coordinator = RequiredBookSuccessorCoordinator(
            coordinator_job_id=str(parent_job_id),
            readiness=parent.get("readiness") or {},
        )
        advanced = coordinator.accept_child_job(journal, child)
        result = await self._collection_update_one(
            {
                "_id": to_object_id(parent_job_id),
                "is_deleted": False,
                "status": "running",
                "required_book_successor_journal": raw_journal,
            },
            {
                "$set": {
                    "required_book_successor_journal": advanced.model_dump(
                        mode="json"
                    ),
                    "expected_narrative_revision": (
                        advanced.expected_narrative_revision
                    ),
                    "current_chapter_id": None,
                    "last_checkpoint_index": len(advanced.stages) // 3,
                }
            },
        )
        if result.modified_count == 1:
            return advanced
        latest = await self.get_job(parent_job_id)
        stored = parse_required_book_successor_journal(
            latest.get("required_book_successor_journal")
        )
        if stored == advanced:
            return stored
        raise JobExecutionLeaseLost(
            "Required book successor journal advance fence was lost"
        )

    async def complete_required_book_successor(
        self,
        parent_job_id: str,
        report,
        *,
        fence_token: str,
        previous_status: str,
        previous_pause_reason: str | None,
        previous_execution_epoch: int,
        previous_expected_narrative_revision: int | None,
    ):
        """Publish the exact final audit and root terminal state together."""

        from backend.services.generation.required_book_successor import (
            RequiredBookSuccessorCoordinator,
            parse_required_book_successor_journal,
        )
        from backend.services.novel.book_completion import BookCompletionReport

        parent = await self.get_job(parent_job_id)
        raw_journal = parent.get("required_book_successor_journal")
        journal = parse_required_book_successor_journal(raw_journal)
        coordinator = RequiredBookSuccessorCoordinator(
            coordinator_job_id=str(parent_job_id),
            readiness=parent.get("readiness") or {},
        )
        completed = coordinator.accept_book_audit(journal, report)
        canonical_report = (
            report.model_dump(mode="json")
            if hasattr(report, "model_dump")
            else deepcopy(dict(report))
        )
        parsed_report = BookCompletionReport.model_validate(canonical_report)
        now = get_utc_now()
        fenced_novel = await get_database()[collections.NOVELS].find_one(
            {
                "_id": to_object_id(parsed_report.novel_id),
                "$expr": {
                    "$eq": [
                        {"$ifNull": ["$narrative_revision", 0]},
                        parsed_report.narrative_revision,
                    ]
                },
                "narrative_write_fence.token": str(fence_token),
                "narrative_write_fence.resource_kind": (
                    "book_completion_audit"
                ),
                "narrative_write_fence.resource_id": str(parent_job_id),
                "narrative_write_fence.expires_at": {"$exists": False},
            },
            projection={"_id": 1},
        )
        if fenced_novel is None:
            raise CandidatePipelineCheckpointConflict(
                "Required book successor lost its narrative revision fence"
            )
        query = self._book_completion_snapshot_query(
            parent_job_id,
            previous_status=previous_status,
            previous_pause_reason=previous_pause_reason,
            previous_execution_epoch=previous_execution_epoch,
            previous_expected_narrative_revision=(
                previous_expected_narrative_revision
            ),
            novel_id=parsed_report.novel_id,
        )
        query["$and"].extend([
            {"required_book_successor_journal": raw_journal},
            self._book_completion_publication_query(
                parent_job_id,
                fence_token,
                live_after=now,
            ),
        ])
        result = await self._collection_update_one(
            query,
            {
                "$set": {
                    "required_book_successor_journal": completed.model_dump(
                        mode="json"
                    ),
                    "completion_audit": canonical_report,
                    "status": "completed",
                    "pause_reason": None,
                    "current_chapter_id": None,
                    "active_slot": None,
                    "error": None,
                    "current_failure_event_id": None,
                    "last_checkpoint_index": completed.chapter_count,
                    "expected_narrative_revision": (
                        completed.expected_narrative_revision
                    ),
                    "updated_at": now,
                },
                "$unset": {"completion_audit_publication": ""},
            },
        )
        if result.modified_count != 1:
            raise CandidatePipelineCheckpointConflict(
                "Required book successor completion fence was lost"
            )
        return completed

    async def block_required_book_successor(
        self,
        parent_job_id: str,
        reason: str,
    ):
        """Persist one stable root stop without discarding child evidence."""

        from backend.services.generation.required_book_successor import (
            RequiredBookSuccessorCoordinator,
            parse_required_book_successor_journal,
        )

        parent = await self.get_job(parent_job_id)
        raw_journal = parent.get("required_book_successor_journal")
        journal = parse_required_book_successor_journal(raw_journal)
        coordinator = RequiredBookSuccessorCoordinator(
            coordinator_job_id=str(parent_job_id),
            readiness=parent.get("readiness") or {},
        )
        blocked = coordinator.block(journal, reason)
        result = await self._collection_update_one(
            {
                "_id": to_object_id(parent_job_id),
                "is_deleted": False,
                "status": "running",
                "required_book_successor_journal": raw_journal,
            },
            {
                "$set": {
                    "required_book_successor_journal": blocked.model_dump(
                        mode="json"
                    ),
                    "status": "paused",
                    "pause_reason": "required_book_successor_blocked",
                    "active_slot": None,
                    "current_chapter_id": None,
                    "error": {
                        "step": "required_book_successor",
                        "code": reason,
                        "reason_codes": [reason],
                    },
                }
            },
        )
        if result.modified_count == 1:
            return blocked
        latest = await self.get_job(parent_job_id)
        stored = parse_required_book_successor_journal(
            latest.get("required_book_successor_journal")
        )
        if stored == blocked:
            return stored
        raise JobExecutionLeaseLost(
            "Required book successor block fence was lost"
        )

    async def pause_required_book_successor_recovery_checkpoint(
        self,
        parent_job_id: str,
        journal_digest: str,
    ) -> bool:
        """Pause the acceptance root once before any child Provider work."""

        from backend.services.generation.required_book_successor import (
            REQUIRED_BOOK_SUCCESSOR_RECOVERY_CHECKPOINT_BEFORE_FIRST_CHILD,
            RequiredBookSuccessorRecoveryCheckpoint,
            parse_required_book_successor_journal,
            parse_required_book_successor_recovery_checkpoint,
            validate_required_book_successor_readiness,
        )

        parent = await self.get_job(parent_job_id)
        authority = validate_required_book_successor_readiness(
            parent.get("readiness") or {}
        )
        journal = parse_required_book_successor_journal(
            parent.get("required_book_successor_journal")
        )
        execution_epoch = parent.get("execution_epoch")
        if (
            authority.recovery_checkpoint
            != REQUIRED_BOOK_SUCCESSOR_RECOVERY_CHECKPOINT_BEFORE_FIRST_CHILD
            or journal.journal_digest != str(journal_digest)
            or journal.stages
            or journal.phase != "review"
            or type(execution_epoch) is not int
            or execution_epoch < 1
            or list(parent.get("attempt_slots") or [])
            or parent.get("has_uncertain_attempts") is True
            or parent.get("current_chapter_id") is not None
            or parent.get("required_book_successor_action") is not None
        ):
            raise JobExecutionLeaseLost(
                "Required book successor recovery checkpoint changed"
            )
        raw_journal = parent.get("required_book_successor_journal")
        raw_checkpoint = parent.get(
            "required_book_successor_recovery_checkpoint"
        )
        if raw_checkpoint is not None:
            checkpoint = parse_required_book_successor_recovery_checkpoint(
                raw_checkpoint
            )
            if (
                checkpoint.coordinator_job_id != str(parent_job_id)
                or checkpoint.coordinator_readiness_digest
                != journal.coordinator_readiness_digest
                or checkpoint.journal_digest != journal.journal_digest
                or str(parent.get("status") or "") != "paused"
                or str(parent.get("pause_reason") or "")
                != "required_book_successor_recovery_checkpoint"
            ):
                raise JobExecutionLeaseLost(
                    "Required book successor recovery checkpoint changed"
                )
            return True
        checkpoint = RequiredBookSuccessorRecoveryCheckpoint.create(
            coordinator_job_id=str(parent_job_id),
            coordinator_readiness_digest=(
                journal.coordinator_readiness_digest
            ),
            journal_digest=journal.journal_digest,
            execution_epoch=execution_epoch,
        )
        result = await self._collection_update_one(
            {
                "_id": to_object_id(parent_job_id),
                "is_deleted": False,
                "status": "running",
                "execution_epoch": execution_epoch,
                "required_book_successor_journal": raw_journal,
                "required_book_successor_recovery_checkpoint": None,
                "required_book_successor_action": None,
                "current_chapter_id": None,
                "attempt_slots": [],
                "has_uncertain_attempts": False,
            },
            {
                "$set": {
                    "required_book_successor_recovery_checkpoint": (
                        checkpoint.model_dump(mode="json")
                    ),
                    "status": "paused",
                    "pause_reason": (
                        "required_book_successor_recovery_checkpoint"
                    ),
                    "active_slot": None,
                    "current_chapter_id": None,
                    "error": None,
                    "updated_at": get_utc_now(),
                }
            },
        )
        if result.modified_count == 1:
            return True
        latest = await self.get_job(parent_job_id)
        try:
            stored_checkpoint = (
                parse_required_book_successor_recovery_checkpoint(
                    latest.get(
                        "required_book_successor_recovery_checkpoint"
                    )
                )
            )
        except (TypeError, ValueError):
            return False
        return bool(
            str(latest.get("status") or "") == "paused"
            and str(latest.get("pause_reason") or "")
            == "required_book_successor_recovery_checkpoint"
            and latest.get("required_book_successor_journal") == raw_journal
            and stored_checkpoint == checkpoint
            and not list(latest.get("attempt_slots") or [])
            and latest.get("has_uncertain_attempts") is False
        )

    async def insert_one(
        self,
        document: Dict[str, Any],
        session: AsyncClientSession | None = None,
    ) -> str:
        self._assert_unowned_creation()
        _validate_initial_candidate_ledgers(document)
        return await self._base.insert_one(dict(document), session=session)

    async def insert_many(
        self,
        documents: List[Dict[str, Any]],
        session: AsyncClientSession | None = None,
    ) -> List[str]:
        self._assert_unowned_creation()
        for document in documents:
            _validate_initial_candidate_ledgers(document)
        return await self._base.insert_many(
            [dict(document) for document in documents],
            session=session,
        )

    async def find_one(
        self,
        query: Dict[str, Any],
        include_deleted: bool = False,
        session: AsyncClientSession | None = None,
    ) -> Dict[str, Any] | None:
        document = await self._base.find_one(
            self._execution_filter(query),
            include_deleted=include_deleted,
            session=session,
        )
        if document is None:
            await self._assert_execution_current()
        return document

    async def find_many(
        self,
        query: Dict[str, Any],
        include_deleted: bool = False,
        limit: int = 0,
        skip: int = 0,
        sort: Any = None,
        session: AsyncClientSession | None = None,
    ) -> List[Dict[str, Any]]:
        documents = await self._base.find_many(
            self._execution_filter(query),
            include_deleted=include_deleted,
            limit=limit,
            skip=skip,
            sort=sort,
            session=session,
        )
        if not documents:
            await self._assert_execution_current()
        return documents

    async def update_one(
        self,
        query: Dict[str, Any],
        update_data: Dict[str, Any],
        include_deleted: bool = False,
        session: AsyncClientSession | None = None,
    ) -> bool:
        _reject_atomic_field_updates(update_data)
        await self._assert_worker_generic_patch_allowed(update_data)
        updated = await self._base.update_one(
            self._execution_filter(query),
            update_data,
            include_deleted=include_deleted,
            session=session,
        )
        if not updated:
            await self._assert_execution_current()
        return updated

    async def update_many(
        self,
        query: Dict[str, Any],
        update_data: Dict[str, Any],
        include_deleted: bool = False,
        session: AsyncClientSession | None = None,
    ) -> int:
        _reject_atomic_field_updates(update_data)
        await self._reject_worker_generic_mutation("update_many")
        updated = await self._base.update_many(
            self._execution_filter(query),
            update_data,
            include_deleted=include_deleted,
            session=session,
        )
        if updated == 0:
            await self._assert_execution_current()
        return updated

    async def increment_one(
        self,
        query: Dict[str, Any],
        increments: Dict[str, int],
        include_deleted: bool = False,
        session: AsyncClientSession | None = None,
    ) -> bool:
        _reject_atomic_field_updates(increments)
        await self._reject_worker_generic_mutation("increment_one")
        updated = await self._base.increment_one(
            self._execution_filter(query),
            increments,
            include_deleted=include_deleted,
            session=session,
        )
        if not updated:
            await self._assert_execution_current()
        return updated

    async def bulk_write(
        self,
        operations: Sequence[Any],
        ordered: bool = True,
        session: AsyncClientSession | None = None,
    ) -> BulkWriteResult | None:
        raise ValueError(
            "Generation job bulk writes require an atomic repository command"
        )

    async def bulk_update_one_set(
        self,
        updates: Iterable[tuple[Dict[str, Any], Dict[str, Any]]],
        include_deleted: bool = False,
        ordered: bool = True,
        session: AsyncClientSession | None = None,
    ) -> int:
        raise ValueError(
            "Generation job bulk writes require an atomic repository command"
        )

    async def soft_delete_one(
        self,
        query: Dict[str, Any],
        session: AsyncClientSession | None = None,
    ) -> bool:
        await self._reject_worker_generic_mutation("delete")
        changed = await self._base.soft_delete_one(
            self._execution_filter(query),
            session=session,
        )
        if not changed:
            await self._assert_execution_current()
        return changed

    async def restore_one(
        self,
        query: Dict[str, Any],
        session: AsyncClientSession | None = None,
    ) -> bool:
        await self._reject_worker_generic_mutation("restore")
        changed = await self._base.restore_one(
            self._execution_filter(query),
            session=session,
        )
        if not changed:
            await self._assert_execution_current()
        return changed

    async def hard_delete_one(
        self,
        query: Dict[str, Any],
        session: AsyncClientSession | None = None,
    ) -> bool:
        await self._reject_worker_generic_mutation("delete")
        changed = await self._base.hard_delete_one(
            self._execution_filter(query),
            session=session,
        )
        if not changed:
            await self._assert_execution_current()
        return changed

    async def hard_delete_many(
        self,
        query: Dict[str, Any],
        session: AsyncClientSession | None = None,
    ) -> int:
        await self._reject_worker_generic_mutation("delete")
        changed = await self._base.hard_delete_many(
            self._execution_filter(query),
            session=session,
        )
        if changed == 0:
            await self._assert_execution_current()
        return changed

    async def count_documents(
        self,
        query: Dict[str, Any],
        include_deleted: bool = False,
        session: AsyncClientSession | None = None,
    ) -> int:
        count = await self._base.count_documents(
            self._execution_filter(query),
            include_deleted=include_deleted,
            session=session,
        )
        if count == 0:
            await self._assert_execution_current()
        return count

    async def exists(
        self,
        query: Dict[str, Any],
        include_deleted: bool = False,
        session: AsyncClientSession | None = None,
    ) -> bool:
        return (
            await self.count_documents(
                query,
                include_deleted=include_deleted,
                session=session,
            )
        ) > 0

    async def paginate(
        self,
        query: Dict[str, Any],
        page: int = 1,
        page_size: int = 10,
        include_deleted: bool = False,
        sort: Any = None,
        session: AsyncClientSession | None = None,
    ) -> Dict[str, Any]:
        skip = (page - 1) * page_size
        items = await self.find_many(
            query,
            include_deleted=include_deleted,
            limit=page_size,
            skip=skip,
            sort=sort,
            session=session,
        )
        total = await self.count_documents(
            query,
            include_deleted=include_deleted,
            session=session,
        )
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (
                (total + page_size - 1) // page_size
                if page_size > 0
                else 0
            ),
        }

    async def get_job(self, job_id: str) -> Dict[str, Any]:
        doc = await self._base.find_one(
            self._execution_filter({"_id": to_object_id(job_id)})
        )
        if doc is None:
            if current_job_execution() is not None:
                raise JobExecutionLeaseLost(
                    "Generation Job execution lease is no longer current"
                )
            raise NotFoundError(f"Generation job not found: {job_id}")
        return doc

    async def list_jobs_by_novel(
        self,
        novel_id: str,
        *,
        limit: int = 0,
    ) -> List[Dict[str, Any]]:
        return await self.find_many(
            {"novel_id": to_object_id(novel_id)},
            sort=[("created_at", -1)],
            limit=max(0, int(limit)),
        )

    async def list_running_jobs(self) -> List[Dict[str, Any]]:
        return await self.find_many({"status": "running"})

    async def list_attempt_slots(
        self,
        job_id: str,
        *,
        chapter_id: str,
        step_prefix: str,
    ) -> List[Dict[str, Any]]:
        """Read one execution's persistent attempt ledger in claim order."""
        job = await self.get_job(job_id)
        return [
            dict(slot)
            for slot in list(job.get("attempt_slots") or [])
            if str(slot.get("chapter_id") or "") == str(chapter_id)
            and str(slot.get("step_id") or "").startswith(step_prefix)
        ]

    @staticmethod
    def _candidate_pipeline_checkpoints(
        job: Dict[str, Any],
        *,
        chapter_id: str,
    ) -> list[CandidatePipelineCheckpointV1]:
        raw = job.get("candidate_pipeline_checkpoints")
        if raw is None:
            raw = []
        if (
            not isinstance(raw, list)
            or len(raw) > MAX_CHAPTER_CANDIDATE_PIPELINE_CHECKPOINTS
        ):
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline checkpoint ledger is invalid"
            )
        parsed: list[CandidatePipelineCheckpointV1] = []
        checkpoint_ids: set[str] = set()
        for sequence, item in enumerate(raw, start=1):
            try:
                checkpoint = parse_candidate_pipeline_checkpoint(item)
            except Exception as exc:
                raise CandidatePipelineCheckpointConflict(
                    "Candidate pipeline checkpoint contract is invalid"
                ) from exc
            if (
                checkpoint.chapter_id != str(chapter_id)
                or checkpoint.sequence != sequence
                or checkpoint.checkpoint_id in checkpoint_ids
            ):
                raise CandidatePipelineCheckpointConflict(
                    "Candidate pipeline checkpoint order or scope changed"
                )
            checkpoint_ids.add(checkpoint.checkpoint_id)
            parsed.append(checkpoint)
        return parsed

    @staticmethod
    def _validate_candidate_completion_chain(
        checkpoints: Sequence[CandidatePipelineCheckpointV1],
        *,
        entry: CandidatePipelineProgressV1,
        expected_scene_count: int,
        max_repair_cycles: int,
    ) -> CandidatePipelineCompletionEvidenceV1:
        evidence = validate_candidate_pipeline_completion_chain(
            checkpoints,
            chapter_id=entry.chapter_id,
            expected_scene_count=expected_scene_count,
            max_repair_cycles=max_repair_cycles,
        )
        # A not-reviewed receipt carries no semantic findings or coverage.
        # Match the runner's honest zero projection without inventing a review;
        # reviewed checkpoints retain the full-scene requirement below.
        not_reviewed = isinstance(evidence.adherence, AdherenceNotReviewedCheckpointV6)
        expected_issue_categories = () if not_reviewed else evidence.adherence.issue_categories
        expected_review_coverage_count = 0 if not_reviewed else expected_scene_count
        if (
            entry.source != evidence.prose.source
            or entry.state_proposal_id != evidence.state.proposal_id
            or entry.attempt_count != evidence.attempt_count
            or entry.truncation_count != evidence.truncation_count
            or entry.outline_issue_categories
            != expected_issue_categories
            or entry.consistency_issue_count
            != evidence.state.consistency_issue_count
            or entry.scene_coverage_count != expected_review_coverage_count
            or entry.repair_cycles_used != evidence.repair_cycles_used
        ):
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline progress does not match its checkpoint"
            )
        return evidence

    @staticmethod
    async def _candidate_completion_authority(
        job: Mapping[str, Any],
        *,
        chapter_id: str,
    ) -> tuple[int, int]:
        """Re-read the two authorities that terminal progress cannot self-report."""

        try:
            from backend.services.generation.chapter_candidate_authorization import (
                CANDIDATE_PIPELINE_REVISION,
            )
            from backend.services.generation.chapter_finalization import (
                parse_chapter_finalization_authorization,
            )

            readiness = job.get("readiness")
            if (
                not isinstance(readiness, Mapping)
                or type(readiness.get("version")) is not int
                or readiness.get("version") != 2
            ):
                raise ValueError("candidate readiness is missing")
            planning = readiness.get("planning")
            if not isinstance(planning, Mapping):
                raise ValueError("candidate readiness planning is missing")
            revision = planning.get("chapter_candidate_pipeline_revision")
            if type(revision) is not int or revision != CANDIDATE_PIPELINE_REVISION:
                raise ValueError("candidate pipeline revision changed")
            finalization = parse_chapter_finalization_authorization(
                planning.get("chapter_finalization_authorization")
            )
            max_repair_cycles = finalization["max_repair_cycles"]
            work = readiness.get("work")
            raw_snapshots = (
                work.get("chapters") if isinstance(work, Mapping) else None
            )
            if not isinstance(raw_snapshots, list):
                raise ValueError("candidate worklist is missing")
            matching_snapshots = [
                snapshot
                for snapshot in raw_snapshots
                if isinstance(snapshot, Mapping)
                and str(snapshot.get("chapter_id") or "") == chapter_id
            ]
            if (
                len(matching_snapshots) != 1
                or matching_snapshots[0].get("has_content") is not False
            ):
                raise ValueError("candidate chapter is outside the worklist")
            chapter = await chapter_repo.get_chapter_by_id(chapter_id)
            if str(chapter.get("novel_id") or "") != str(
                job.get("novel_id") or ""
            ):
                raise ValueError("candidate chapter scope changed")
            outline = chapter.get("outline")
            scenes = outline.get("scenes") if isinstance(outline, Mapping) else None
            if (
                not isinstance(scenes, list)
                or not 1 <= len(scenes) <= MAX_CANDIDATE_OUTLINE_SCENES
                or any(not isinstance(scene, Mapping) for scene in scenes)
            ):
                raise ValueError("candidate chapter outline is invalid")
        except Exception as exc:
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline completion authority diverged"
            ) from exc
        return len(scenes), max_repair_cycles

    async def list_candidate_pipeline_checkpoints(
        self,
        job_id: str,
        *,
        chapter_id: str,
    ) -> List[Dict[str, Any]]:
        normalized_chapter_id = str(chapter_id or "")
        if not normalized_chapter_id:
            raise ValueError("Candidate pipeline chapter id is required")
        job = await self.get_job(job_id)
        if str(job.get("current_chapter_id") or "") != normalized_chapter_id:
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline chapter is no longer current"
            )
        return [
            checkpoint.model_dump(mode="json")
            for checkpoint in self._candidate_pipeline_checkpoints(
                job,
                chapter_id=normalized_chapter_id,
            )
        ]

    async def append_candidate_pipeline_checkpoint(
        self,
        job_id: str,
        checkpoint: CandidatePipelineCheckpointV1,
    ) -> bool:
        try:
            validated = parse_candidate_pipeline_checkpoint(checkpoint)
        except Exception as exc:
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline checkpoint contract is invalid"
            ) from exc
        value = validated.model_dump(mode="json")
        job = await self.get_job(job_id)
        if (
            str(job.get("status") or "") != "running"
            or str(job.get("current_chapter_id") or "")
            != validated.chapter_id
        ):
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline execution is no longer current"
            )
        existing = self._candidate_pipeline_checkpoints(
            job,
            chapter_id=validated.chapter_id,
        )
        matched = next(
            (
                item
                for item in existing
                if item.checkpoint_id == validated.checkpoint_id
            ),
            None,
        )
        if matched is not None:
            if matched == validated:
                return True
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline checkpoint replay diverged"
            )
        if validated.sequence != len(existing) + 1:
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline checkpoint sequence diverged"
            )
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "status": "running",
                "current_chapter_id": validated.chapter_id,
                "candidate_pipeline_checkpoints.checkpoint_id": {
                    "$ne": validated.checkpoint_id
                },
                "$expr": {
                    "$eq": [
                        {
                            "$size": {
                                "$ifNull": [
                                    "$candidate_pipeline_checkpoints",
                                    [],
                                ]
                            }
                        },
                        len(existing),
                    ]
                },
            },
            {
                "$push": {"candidate_pipeline_checkpoints": value},
                "$set": {"updated_at": get_utc_now()},
            },
        )
        if result.modified_count == 1:
            return True
        current = await self.get_job(job_id)
        if (
            str(current.get("current_chapter_id") or "")
            == validated.chapter_id
        ):
            for item in self._candidate_pipeline_checkpoints(
                current,
                chapter_id=validated.chapter_id,
            ):
                if item.checkpoint_id == validated.checkpoint_id:
                    if item == validated:
                        return True
                    break
        raise CandidatePipelineCheckpointConflict(
            "Candidate pipeline checkpoint append lost its execution fence"
        )

    @staticmethod
    def _completed_candidate_progress(
        job: Mapping[str, Any],
        *,
        expected_receipt: CandidatePipelineCompletionV1,
        entry: CandidatePipelineProgressV1,
    ) -> bool:
        raw_progress = job.get("progress")
        if raw_progress is None:
            raw_progress = []
        if (
            not isinstance(raw_progress, list)
            or len(raw_progress) > MAX_CANDIDATE_PIPELINE_PROGRESS_ENTRIES
        ):
            raise CandidatePipelineCheckpointConflict(
                "Generation job progress ledger is invalid"
            )
        matched = False
        expected_fields = set(CandidatePipelineProgressV1.model_fields)
        persisted_fields = expected_fields | {
            "candidate_pipeline_completion",
            "completed_at",
        }
        for raw_entry in raw_progress:
            if not isinstance(raw_entry, Mapping):
                continue
            raw_receipt = raw_entry.get("candidate_pipeline_completion")
            if raw_receipt is None:
                continue
            try:
                receipt = CandidatePipelineCompletionV1.model_validate(
                    raw_receipt
                )
            except Exception as exc:
                raise CandidatePipelineCheckpointConflict(
                    "Candidate pipeline completion receipt is invalid"
                ) from exc
            if receipt.chapter_id != expected_receipt.chapter_id:
                continue
            if receipt != expected_receipt:
                raise CandidatePipelineCheckpointConflict(
                    "Candidate pipeline completion receipt diverged"
                )
            if set(raw_entry) != persisted_fields:
                raise CandidatePipelineCheckpointConflict(
                    "Candidate pipeline completion progress is invalid"
                )
            try:
                candidate = parse_candidate_pipeline_progress({
                    field: raw_entry[field]
                    for field in expected_fields
                })
            except Exception as exc:
                raise CandidatePipelineCheckpointConflict(
                    "Candidate pipeline completion progress is invalid"
                ) from exc
            if candidate != entry or matched:
                raise CandidatePipelineCheckpointConflict(
                    "Candidate pipeline completion replay diverged"
                )
            matched = True
        return matched

    async def advance_narrative_revision_cursor(
        self,
        job_id: str,
        *,
        chapter_id: str,
        expected_revision: int,
        next_revision: int,
    ) -> bool:
        """Advance one Job-owned mutation cursor under the active chapter fence."""

        normalized_chapter_id = str(chapter_id or "")
        if (
            not normalized_chapter_id
            or type(expected_revision) is not int
            or type(next_revision) is not int
            or expected_revision < 0
            or next_revision != expected_revision + 1
            or next_revision > _MAX_NARRATIVE_REVISION
        ):
            raise CandidatePipelineCheckpointConflict(
                "Generation job narrative revision transition is invalid"
            )
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "status": "running",
                "current_chapter_id": normalized_chapter_id,
                "expected_narrative_revision": expected_revision,
            },
            {
                "$set": {
                    "expected_narrative_revision": next_revision,
                    "updated_at": get_utc_now(),
                }
            },
        )
        if result.modified_count == 1:
            return True
        current = await self.get_job(job_id)
        if (
            str(current.get("status") or "") == "running"
            and str(current.get("current_chapter_id") or "")
            == normalized_chapter_id
            and type(current.get("expected_narrative_revision")) is int
            and current["expected_narrative_revision"] == next_revision
        ):
            return True
        raise CandidatePipelineCheckpointConflict(
            "Generation job narrative revision cursor changed"
        )

    async def record_reference_card_auto_creation(
        self,
        job_id: str,
        *,
        chapter_id: str,
        expected_revision: int,
        next_revision: int,
        event: Mapping[str, Any],
    ) -> bool:
        """Persist one auto-create outcome with its Job revision transition."""

        normalized_chapter_id = str(chapter_id or "")
        normalized_event = dict(event)
        event_id = str(normalized_event.get("event_id") or "")
        outcome = str(normalized_event.get("outcome") or "")
        created_count = normalized_event.get("created_count")
        authorization_digest = str(
            normalized_event.get("authorization_digest") or ""
        )
        readiness_digest = str(normalized_event.get("readiness_digest") or "")
        authorization_revision = normalized_event.get("authorization_revision")
        if (
            not normalized_chapter_id
            or normalized_event.get("schema_version")
            != "reference_card_auto_creation_event.v1"
            or str(normalized_event.get("chapter_id") or "")
            != normalized_chapter_id
            or not event_id
            or len(event_id) > 200
            or outcome
            not in {"auto_created", "manual_review_required", "not_applicable"}
            or type(created_count) is not int
            or created_count < 0
            or len(authorization_digest) != 64
            or len(readiness_digest) != 64
            or any(
                character not in "0123456789abcdef"
                for character in authorization_digest + readiness_digest
            )
            or type(authorization_revision) is not int
            or authorization_revision < 1
            or normalized_event.get("policy_revision")
            != 1
            or type(expected_revision) is not int
            or type(next_revision) is not int
            or expected_revision < 0
            or next_revision not in {expected_revision, expected_revision + 1}
        ):
            raise CandidatePipelineCheckpointConflict(
                "Reference-card auto-creation event is invalid"
            )
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "status": "running",
                "current_chapter_id": normalized_chapter_id,
                "expected_narrative_revision": expected_revision,
                "reference_card_auto_creation_events.event_id": {"$ne": event_id},
            },
            {
                "$set": {
                    "expected_narrative_revision": next_revision,
                    "updated_at": get_utc_now(),
                },
                "$push": {
                    "reference_card_auto_creation_events": {
                        "$each": [normalized_event],
                        "$slice": -200,
                    }
                },
            },
        )
        if result.modified_count == 1:
            return True
        current = await self.get_job(job_id)
        matching_events = [
            item
            for item in list(
                current.get("reference_card_auto_creation_events") or []
            )
            if isinstance(item, Mapping)
            and str(item.get("event_id") or "") == event_id
        ]
        if (
            str(current.get("status") or "") == "running"
            and str(current.get("current_chapter_id") or "")
            == normalized_chapter_id
            and current.get("expected_narrative_revision") == next_revision
            and matching_events == [normalized_event]
        ):
            return True
        raise CandidatePipelineCheckpointConflict(
            "Reference-card auto-creation cursor changed"
        )

    async def record_reference_card_repair(
        self,
        job_id: str,
        *,
        chapter_id: str,
        expected_revision: int,
        next_revision: int,
        event: Mapping[str, Any],
    ) -> bool:
        """Persist one bounded dependency-repair outcome and cursor change."""

        normalized_chapter_id = str(chapter_id or "")
        normalized_event = dict(event)
        event_id = str(normalized_event.get("event_id") or "")
        outcome = str(normalized_event.get("outcome") or "")
        resolution = normalized_event.get("resolution")
        cycle = normalized_event.get("cycle")
        created_candidate_ids = list(
            normalized_event.get("created_reference_card_candidate_ids") or []
        )
        authorization_digest = str(
            normalized_event.get("authorization_digest") or ""
        )
        readiness_digest = str(normalized_event.get("readiness_digest") or "")
        authorization_revision = normalized_event.get("authorization_revision")
        valid_transition = (
            next_revision == expected_revision + 1
            if outcome == "applied"
            else next_revision == expected_revision
        )
        if (
            not normalized_chapter_id
            or normalized_event.get("schema_version")
            != "reference_card_repair_event.v1"
            or str(normalized_event.get("chapter_id") or "")
            != normalized_chapter_id
            or not event_id
            or len(event_id) > 200
            or outcome not in {"applied", "exhausted", "uncertain"}
            or (
                resolution not in {
                    None,
                    "rewritten_unique_new",
                    "dependency_removed",
                }
                if outcome == "applied"
                else resolution is not None
            )
            or type(cycle) is not int
            or cycle < 1
            or cycle > 2
            or len(authorization_digest) != 64
            or len(readiness_digest) != 64
            or any(
                character not in "0123456789abcdef"
                for character in authorization_digest + readiness_digest
            )
            or type(authorization_revision) is not int
            or authorization_revision < 1
            or normalized_event.get("policy_revision")
            != 1
            or type(expected_revision) is not int
            or type(next_revision) is not int
            or expected_revision < 0
            or not valid_transition
            or len(created_candidate_ids) != len(set(created_candidate_ids))
            or any(
                not isinstance(candidate_id, str)
                or not ObjectId.is_valid(candidate_id)
                for candidate_id in created_candidate_ids
            )
            or (
                outcome == "applied"
                and resolution in {None, "rewritten_unique_new"}
                and not created_candidate_ids
            )
            or (
                outcome == "applied"
                and resolution == "dependency_removed"
                and bool(created_candidate_ids)
            )
            or (outcome != "applied" and bool(created_candidate_ids))
        ):
            raise CandidatePipelineCheckpointConflict(
                "Reference-card repair event is invalid"
            )
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "status": "running",
                "current_chapter_id": normalized_chapter_id,
                "expected_narrative_revision": expected_revision,
                "reference_card_repair_events.event_id": {"$ne": event_id},
            },
            {
                "$set": {
                    "expected_narrative_revision": next_revision,
                    "updated_at": get_utc_now(),
                },
                "$push": {
                    "reference_card_repair_events": {
                        "$each": [normalized_event],
                        "$slice": -200,
                    }
                },
            },
        )
        if result.modified_count == 1:
            return True
        current = await self.get_job(job_id)
        matching_events = [
            item
            for item in list(current.get("reference_card_repair_events") or [])
            if isinstance(item, Mapping)
            and str(item.get("event_id") or "") == event_id
        ]
        if (
            str(current.get("status") or "") == "running"
            and str(current.get("current_chapter_id") or "")
            == normalized_chapter_id
            and current.get("expected_narrative_revision") == next_revision
            and matching_events == [normalized_event]
        ):
            return True
        raise CandidatePipelineCheckpointConflict(
            "Reference-card repair cursor changed"
        )

    async def finalize_reference_card_repair_resolution(
        self,
        job_id: str,
        *,
        event_id: str,
        resolution: str,
    ) -> bool:
        """Publish a repaired candidate as unique only after its full Gate."""

        normalized_event_id = str(event_id or "")
        if (
            not normalized_event_id
            or len(normalized_event_id) > 200
            or resolution != "rewritten_unique_new"
        ):
            raise CandidatePipelineCheckpointConflict(
                "Reference-card repair resolution is invalid"
            )
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "reference_card_repair_events": {
                    "$elemMatch": {
                        "event_id": normalized_event_id,
                        "outcome": "applied",
                        "resolution": None,
                    }
                },
            },
            {
                "$set": {
                    "reference_card_repair_events.$.resolution": resolution,
                    "updated_at": get_utc_now(),
                }
            },
        )
        if result.modified_count == 1:
            return True
        current = await self.get_job(job_id)
        matching_events = [
            item
            for item in list(current.get("reference_card_repair_events") or [])
            if isinstance(item, Mapping)
            and str(item.get("event_id") or "") == normalized_event_id
        ]
        if (
            len(matching_events) == 1
            and matching_events[0].get("outcome") == "applied"
            and matching_events[0].get("resolution") == resolution
            and bool(
                matching_events[0].get(
                    "created_reference_card_candidate_ids"
                )
            )
        ):
            return True
        raise CandidatePipelineCheckpointConflict(
            "Reference-card repair resolution changed"
        )

    async def bind_job_mutation_recovery(
        self,
        job_id: str,
        binding: JobMutationRecoveryBindingV1,
    ) -> bool:
        """Persist one exact state-only mutation identity before Provider work."""

        try:
            frozen = JobMutationRecoveryBindingV1.model_validate(
                binding.model_dump(mode="python")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise CandidatePipelineCheckpointConflict(
                "Job mutation recovery binding is invalid"
            ) from exc
        if frozen.job_id != str(job_id):
            raise CandidatePipelineCheckpointConflict(
                "Job mutation recovery binding belongs to another Job"
            )
        canonical = frozen.model_dump(mode="json")
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "status": "running",
                "current_chapter_id": frozen.chapter_id,
                "expected_narrative_revision": (
                    frozen.expected_narrative_revision
                ),
                "$or": [
                    {"job_mutation_recovery": {"$exists": False}},
                    {"job_mutation_recovery": None},
                    {"job_mutation_recovery": canonical},
                ],
            },
            {
                "$set": {
                    "job_mutation_recovery": canonical,
                    "updated_at": get_utc_now(),
                }
            },
        )
        if result.matched_count == 1:
            return True
        raise CandidatePipelineCheckpointConflict(
            "Job mutation recovery binding changed"
        )

    async def clear_job_mutation_recovery(
        self,
        job_id: str,
        binding: JobMutationRecoveryBindingV1,
        *,
        terminal_status: str,
    ) -> bool:
        """Clear one exact terminal state-only recovery marker."""

        try:
            frozen = JobMutationRecoveryBindingV1.model_validate(
                binding.model_dump(mode="python")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise CandidatePipelineCheckpointConflict(
                "Job mutation recovery binding is invalid"
            ) from exc
        if frozen.job_id != str(job_id):
            raise CandidatePipelineCheckpointConflict(
                "Job mutation recovery binding belongs to another Job"
            )
        if terminal_status not in {"failed", "aborted"}:
            raise ValueError("Job mutation recovery terminal status is invalid")
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "status": terminal_status,
                "job_mutation_recovery": frozen.model_dump(mode="json"),
            },
            {
                "$unset": {"job_mutation_recovery": ""},
                "$set": {"updated_at": get_utc_now()},
            },
        )
        if result.modified_count == 1:
            return True
        current = await self.get_job(job_id)
        if (
            str(current.get("status") or "") == terminal_status
            and current.get("job_mutation_recovery") is None
        ):
            return True
        raise CandidatePipelineCheckpointConflict(
            "Job mutation recovery release lost its terminal fence"
        )

    @staticmethod
    def _validated_state_dispatch_resolution(
        resolution: StateDispatchResolutionV3,
    ) -> StateDispatchResolutionV3:
        try:
            return StateDispatchResolutionV3.model_validate(
                resolution.model_dump(mode="python")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise CandidatePipelineCheckpointConflict(
                "State dispatch resolution is invalid"
            ) from exc

    async def begin_state_dispatch_resolution(
        self,
        job_id: str,
        binding: JobMutationRecoveryBindingV1,
        action: str,
    ) -> StateDispatchResolutionV3:
        """Persist one immutable explicit action before touching either ledger."""

        try:
            frozen_binding = JobMutationRecoveryBindingV1.model_validate(
                binding.model_dump(mode="python")
            )
            intent = StateDispatchResolutionV3(
                schema_version="state_dispatch_resolution.v3",
                binding=frozen_binding,
                action=action,
                phase="intent",
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise CandidatePipelineCheckpointConflict(
                "State dispatch resolution intent is invalid"
            ) from exc
        if frozen_binding.job_id != str(job_id):
            raise CandidatePipelineCheckpointConflict(
                "State dispatch resolution belongs to another Job"
            )
        canonical_binding = frozen_binding.model_dump(mode="json")
        canonical_intent = intent.model_dump(mode="json")
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "job_mutation_recovery": canonical_binding,
                "$or": [
                    {"state_dispatch_resolution": {"$exists": False}},
                    {"state_dispatch_resolution": None},
                ],
            },
            {
                "$inc": {"execution_epoch": 1},
                "$set": {
                    "state_dispatch_resolution": canonical_intent,
                    "updated_at": get_utc_now(),
                },
                "$unset": {"execution_lease": ""},
            },
        )
        if result.modified_count == 1:
            return intent
        current = await self.get_job(job_id)
        raw = current.get("state_dispatch_resolution")
        try:
            existing = StateDispatchResolutionV3.model_validate(raw)
        except (TypeError, ValueError) as exc:
            raise CandidatePipelineCheckpointConflict(
                "Persisted state dispatch resolution is invalid"
            ) from exc
        if existing.binding == frozen_binding and existing.action == intent.action:
            return existing
        raise CandidatePipelineCheckpointConflict(
            "State dispatch resolution intent diverged"
        )

    async def advance_state_dispatch_resolution(
        self,
        job_id: str,
        resolution: StateDispatchResolutionV3,
        next_phase: str,
    ) -> StateDispatchResolutionV3:
        """Advance exactly one durable action phase with the Job marker fenced."""

        current = self._validated_state_dispatch_resolution(resolution)
        preparation_phases = STATE_DISPATCH_RESOLUTION_PHASES[:4]
        try:
            current_index = preparation_phases.index(current.phase)
            next_index = preparation_phases.index(next_phase)
        except ValueError as exc:
            raise CandidatePipelineCheckpointConflict(
                "State dispatch resolution phase is invalid"
            ) from exc
        if next_index != current_index + 1:
            raise CandidatePipelineCheckpointConflict(
                "State dispatch resolution phase is not monotonic"
            )
        advanced = StateDispatchResolutionV3.model_validate({
            **current.model_dump(mode="python"),
            "phase": next_phase,
        })
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "job_mutation_recovery": current.binding.model_dump(mode="json"),
                "state_dispatch_resolution": current.model_dump(mode="json"),
            },
            {
                "$set": {
                    "state_dispatch_resolution": advanced.model_dump(mode="json"),
                    "updated_at": get_utc_now(),
                }
            },
        )
        if result.modified_count == 1:
            return advanced
        latest = await self.get_job(job_id)
        try:
            existing = StateDispatchResolutionV3.model_validate(
                latest.get("state_dispatch_resolution")
            )
        except (TypeError, ValueError) as exc:
            raise CandidatePipelineCheckpointConflict(
                "Persisted state dispatch resolution disappeared"
            ) from exc
        if existing == advanced:
            return existing
        raise CandidatePipelineCheckpointConflict(
            "State dispatch resolution phase raced"
        )

    async def acknowledge_state_dispatch_attempts(
        self,
        job_id: str,
        resolution: StateDispatchResolutionV3,
    ) -> StateDispatchResolutionV3:
        """Resolve every live outer attempt under the durable action fence."""

        current = self._validated_state_dispatch_resolution(resolution)
        if current.phase != "proposal_acknowledged":
            raise CandidatePipelineCheckpointConflict(
                "State dispatch attempts are not ready for resolution"
            )
        advanced = StateDispatchResolutionV3.model_validate({
            **current.model_dump(mode="python"),
            "phase": "attempts_acknowledged",
        })
        acknowledged_state = f"uncertain_{current.action}_acknowledged"
        now = get_utc_now()
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "job_mutation_recovery": current.binding.model_dump(mode="json"),
                "state_dispatch_resolution": current.model_dump(mode="json"),
            },
            {
                "$set": {
                    "attempt_slots.$[slot].state": acknowledged_state,
                    "attempt_slots.$[slot].updated_at": now,
                    "active_token_reservations.$[reservation].state": (
                        acknowledged_state
                    ),
                    "active_token_reservations.$[reservation].updated_at": now,
                    "has_uncertain_attempts": False,
                    "attempt_reservation": None,
                    "state_dispatch_resolution": advanced.model_dump(mode="json"),
                    "updated_at": now,
                },
            },
            array_filters=[
                {"slot.state": {"$in": ["claimed", "uncertain"]}},
                {"reservation.state": {"$in": ["claimed", "uncertain"]}},
            ],
        )
        if result.modified_count == 1:
            return advanced
        latest = await self.get_job(job_id)
        try:
            existing = StateDispatchResolutionV3.model_validate(
                latest.get("state_dispatch_resolution")
            )
        except (TypeError, ValueError) as exc:
            raise CandidatePipelineCheckpointConflict(
                "State dispatch attempt resolution disappeared"
            ) from exc
        if existing == advanced and not latest.get("has_uncertain_attempts"):
            return existing
        raise CandidatePipelineCheckpointConflict(
            "State dispatch attempt resolution raced"
        )

    async def transition_state_dispatch_retry(
        self,
        job_id: str,
        resolution: StateDispatchResolutionV3,
        *,
        last_checkpoint_index: int,
    ) -> StateDispatchResolutionV3:
        """Set Job running only after both ledgers and the receipt key are resolved."""

        current = self._validated_state_dispatch_resolution(resolution)
        if current.action != "retry" or current.phase != "proposal_released":
            raise CandidatePipelineCheckpointConflict(
                "State dispatch retry is not ready to transition"
            )
        if (
            type(last_checkpoint_index) is not int
            or last_checkpoint_index < 0
            or last_checkpoint_index > MAX_CANDIDATE_PIPELINE_PROGRESS_ENTRIES
        ):
            raise CandidatePipelineCheckpointConflict(
                "State dispatch retry checkpoint cursor is invalid"
            )
        transitioned = StateDispatchResolutionV3.model_validate({
            **current.model_dump(mode="python"),
            "phase": "job_transitioned",
        })
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "job_mutation_recovery": current.binding.model_dump(mode="json"),
                "state_dispatch_resolution": current.model_dump(mode="json"),
            },
            {
                "$set": {
                    "status": "running",
                    "pause_reason": None,
                    "error": None,
                    "active_slot": "global",
                    "has_uncertain_attempts": False,
                    "confirm_uncertain_prose_retry": True,
                    "last_checkpoint_index": last_checkpoint_index,
                    "state_dispatch_resolution": transitioned.model_dump(
                        mode="json"
                    ),
                    "updated_at": get_utc_now(),
                }
            },
        )
        if result.modified_count == 1:
            return transitioned
        latest = await self.get_job(job_id)
        try:
            existing = StateDispatchResolutionV3.model_validate(
                latest.get("state_dispatch_resolution")
            )
        except (TypeError, ValueError) as exc:
            raise CandidatePipelineCheckpointConflict(
                "State dispatch retry transition disappeared"
            ) from exc
        if existing == transitioned and latest.get("status") == "running":
            return existing
        raise CandidatePipelineCheckpointConflict(
            "State dispatch retry transition raced"
        )

    async def complete_state_dispatch_terminal(
        self,
        job_id: str,
        resolution: StateDispatchResolutionV3,
    ) -> StateDispatchResolutionV3:
        """Publish a replayable skip/abort receipt with the terminal Job state."""

        current = self._validated_state_dispatch_resolution(resolution)
        expected_status = {"skip": "failed", "abort": "aborted"}.get(
            current.action
        )
        if current.phase != "proposal_released" or expected_status is None:
            raise CandidatePipelineCheckpointConflict(
                "State dispatch terminal resolution is not ready"
            )
        terminal = StateDispatchResolutionV3.model_validate({
            **current.model_dump(mode="python"),
            "phase": "terminal",
        })
        terminal_fields: dict[str, Any] = {
            "status": expected_status,
            "pause_reason": (
                "uncertain_skipped" if current.action == "skip" else None
            ),
            "active_slot": None,
            "current_chapter_id": None,
            "has_uncertain_attempts": False,
            "attempt_reservation": None,
            "error": (
                {
                    "step": "uncertain_attempt",
                    "message": (
                        "用户选择跳过可能已发出的 Provider 请求；"
                        "请人工检查章节后再恢复"
                    ),
                }
                if current.action == "skip"
                else None
            ),
            "state_dispatch_resolution": terminal.model_dump(mode="json"),
            "updated_at": get_utc_now(),
        }
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "job_mutation_recovery": current.binding.model_dump(mode="json"),
                "state_dispatch_resolution": current.model_dump(mode="json"),
            },
            {
                "$set": terminal_fields,
                "$unset": {"job_mutation_recovery": ""},
            },
        )
        if result.modified_count == 1:
            return terminal
        latest = await self.get_job(job_id)
        try:
            existing = StateDispatchResolutionV3.model_validate(
                latest.get("state_dispatch_resolution")
            )
        except (TypeError, ValueError) as exc:
            raise CandidatePipelineCheckpointConflict(
                "State dispatch terminal receipt disappeared"
            ) from exc
        if existing == terminal and latest.get("status") == expected_status:
            return existing
        raise CandidatePipelineCheckpointConflict(
            "State dispatch terminal transition raced"
        )

    async def complete_job_mutation_chapter(
        self,
        job_id: str,
        *,
        receipt: JobMutationReceiptV1,
        entry: Mapping[str, Any],
        tokens_delta: int,
    ) -> bool:
        """Publish state-only progress and advance its exact receipt atomically."""

        try:
            frozen = JobMutationReceiptV1.model_validate(
                receipt.model_dump(mode="python")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise CandidatePipelineCheckpointConflict(
                "Job mutation receipt is invalid"
            ) from exc
        binding = frozen.binding
        if binding.job_id != str(job_id):
            raise CandidatePipelineCheckpointConflict(
                "Job mutation receipt belongs to another Job"
            )
        if (
            type(tokens_delta) is not int
            or tokens_delta < 0
            or tokens_delta > _MAX_NARRATIVE_REVISION
        ):
            raise CandidatePipelineCheckpointConflict(
                "Job mutation progress token delta is invalid"
            )
        progress = dict(entry)
        canonical_receipt = frozen.model_dump(mode="json")
        if progress.get("job_mutation_receipt") != canonical_receipt:
            raise CandidatePipelineCheckpointConflict(
                "Job mutation progress receipt diverged"
            )
        if str(progress.get("chapter_id") or "") != binding.chapter_id:
            raise CandidatePipelineCheckpointConflict(
                "Job mutation progress chapter diverged"
            )
        supplied_tokens_delta = progress.get("job_mutation_tokens_delta")
        if supplied_tokens_delta is not None and (
            type(supplied_tokens_delta) is not int
            or supplied_tokens_delta != tokens_delta
        ):
            raise CandidatePipelineCheckpointConflict(
                "Job mutation progress token delta diverged"
            )
        progress["job_mutation_tokens_delta"] = tokens_delta
        canonical_binding = binding.model_dump(mode="json")
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "status": "running",
                "current_chapter_id": binding.chapter_id,
                "expected_narrative_revision": (
                    binding.expected_narrative_revision
                ),
                "job_mutation_recovery": canonical_binding,
                "progress": {
                    "$not": {
                        "$elemMatch": {
                            "job_mutation_receipt": canonical_receipt,
                        }
                    }
                },
                "$expr": {
                    "$lt": [
                        {"$size": {"$ifNull": ["$progress", []]}},
                        MAX_CANDIDATE_PIPELINE_PROGRESS_ENTRIES,
                    ]
                },
            },
            {
                "$push": {"progress": progress},
                "$inc": {"tokens_used": tokens_delta},
                "$set": {
                    "expected_narrative_revision": (
                        frozen.next_narrative_revision
                    ),
                    "current_chapter_id": None,
                    "current_failure_event_id": None,
                    "updated_at": get_utc_now(),
                },
                "$unset": {"job_mutation_recovery": ""},
            },
        )
        if result.modified_count == 1:
            return True
        current = await self.get_job(job_id)
        matching_progress = [
            dict(item)
            for item in current.get("progress") or []
            if isinstance(item, Mapping)
            and item.get("job_mutation_receipt") == canonical_receipt
        ]
        if matching_progress:
            def replay_projection(value: Mapping[str, Any]) -> dict[str, Any]:
                projected = dict(value)
                # completed_at is display metadata, not part of the stable
                # mutation/result identity.
                projected.pop("completed_at", None)
                return projected

            if (
                len(matching_progress) == 1
                and current.get("expected_narrative_revision")
                == frozen.next_narrative_revision
                and current.get("job_mutation_recovery") is None
                and replay_projection(matching_progress[0])
                == replay_projection(progress)
            ):
                return True
            raise CandidatePipelineCheckpointConflict(
                "Job mutation completion replay diverged"
            )
        raise CandidatePipelineCheckpointConflict(
            "Job mutation completion lost its revision fence"
        )

    async def pause_candidate_pipeline_for_manual_takeover(
        self,
        job_id: str,
        *,
        takeover: CandidateManualTakeoverV1,
        expected_checkpoints: Sequence[Any],
    ) -> bool:
        """Atomically bind one incomplete candidate to an explicit author handoff."""

        try:
            binding = parse_candidate_manual_takeover(takeover)
            checkpoints = validate_candidate_manual_takeover_binding(
                binding,
                expected_checkpoints,
            )
        except (TypeError, ValueError) as exc:
            raise CandidatePipelineCheckpointConflict(
                "Candidate manual takeover evidence is invalid"
            ) from exc
        if binding.job_id != str(job_id):
            raise CandidatePipelineCheckpointConflict(
                "Candidate manual takeover Job identity is invalid"
            )
        checkpoint_values = [
            checkpoint.model_dump(mode="json") for checkpoint in checkpoints
        ]
        binding_value = binding.model_dump(mode="json")
        confirmation = {
            "status": "candidate_manual_takeover_required",
            "requires_confirmation": True,
            "chapter_id": binding.chapter_id,
            "code": binding.termination_reason_code,
            "reason_codes": list(binding.reason_codes),
            "next_step": binding.next_step,
        }
        error = {
            "step": "candidate_pipeline",
            "chapter_id": binding.chapter_id,
            "message": "Candidate prose requires bounded manual completion",
            "reason_codes": list(binding.reason_codes),
            "next_step": binding.next_step,
        }
        result = await self._collection_update_one(
            {
                "$and": [
                    {
                        "_id": to_object_id(job_id),
                        "novel_id": to_object_id(binding.novel_id),
                        "is_deleted": False,
                        "status": "running",
                        "active_slot": "global",
                        "current_chapter_id": binding.chapter_id,
                        "current_failure_event_id": binding.failure_event_id,
                        "authorization_revision": (
                            binding.authorization_revision
                        ),
                        "expected_narrative_revision": (
                            binding.expected_narrative_revision
                        ),
                        "readiness.digest": binding.readiness_digest,
                        "candidate_pipeline_checkpoints": checkpoint_values,
                        "state_dispatch_resolution": None,
                    },
                    {
                        "$or": [
                            {"candidate_manual_takeover": {"$exists": False}},
                            {"candidate_manual_takeover": None},
                        ]
                    },
                ]
            },
            {
                "$set": {
                    "status": "paused",
                    "pause_reason": "incomplete_scene",
                    "current_chapter_id": binding.chapter_id,
                    "active_slot": None,
                    "error": error,
                    "authorization_confirmation_required": confirmation,
                    "candidate_manual_takeover": binding_value,
                    "updated_at": get_utc_now(),
                }
            },
        )
        if result.modified_count == 1:
            return True
        current = await self.get_job(job_id)
        current_readiness = current.get("readiness")
        if (
            current.get("status") == "paused"
            and current.get("pause_reason") == "incomplete_scene"
            and current.get("active_slot") is None
            and str(current.get("novel_id") or "") == binding.novel_id
            and current.get("current_chapter_id") == binding.chapter_id
            and current.get("current_failure_event_id")
            == binding.failure_event_id
            and current.get("authorization_revision")
            == binding.authorization_revision
            and current.get("expected_narrative_revision")
            == binding.expected_narrative_revision
            and isinstance(current_readiness, Mapping)
            and current_readiness.get("digest") == binding.readiness_digest
            and current.get("state_dispatch_resolution") is None
            and current.get("candidate_manual_takeover") == binding_value
            and current.get("candidate_pipeline_checkpoints")
            == checkpoint_values
        ):
            return True
        raise CandidatePipelineCheckpointConflict(
            "Candidate manual takeover lost its checkpoint fence"
        )

    async def update_job_authorization(
        self,
        job_id: str,
        fields: Dict[str, Any],
        *,
        previous_revision: int | None,
        next_revision: int,
        previous_status: str,
        previous_authorization_revision: int | None,
        previous_readiness_digest: str | None,
        previous_active_slot: str | None,
        previous_execution_epoch: int,
        resolved_job_mutation_recovery: (
            JobMutationRecoveryBindingV1 | None
        ) = None,
        resolved_candidate_manual_takeover: (
            CandidateManualTakeoverResolutionV1 | None
        ) = None,
    ) -> bool:
        """Rebind readiness and its revision cursor in one fenced update."""

        _reject_atomic_field_updates(fields)
        resolved_binding: JobMutationRecoveryBindingV1 | None = None
        if resolved_job_mutation_recovery is not None:
            try:
                resolved_binding = JobMutationRecoveryBindingV1.model_validate(
                    resolved_job_mutation_recovery.model_dump(mode="python")
                )
            except (AttributeError, TypeError, ValueError) as exc:
                raise ValueError(
                    "Resolved generation job mutation recovery is invalid"
                ) from exc
        resolved_takeover: CandidateManualTakeoverResolutionV1 | None = None
        if resolved_candidate_manual_takeover is not None:
            try:
                resolved_takeover = parse_candidate_manual_takeover_resolution(
                    resolved_candidate_manual_takeover
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "Resolved candidate manual takeover is invalid"
                ) from exc
        if resolved_binding is not None and resolved_takeover is not None:
            raise ValueError(
                "Generation job cannot resolve two recovery bindings at once"
            )
        if (
            (previous_revision is not None and type(previous_revision) is not int)
            or type(next_revision) is not int
            or next_revision < 0
            or next_revision > _MAX_NARRATIVE_REVISION
            or previous_status not in {"paused", "interrupted", "failed"}
            or type(previous_execution_epoch) is not int
            or previous_execution_epoch < 0
            or (
                previous_authorization_revision is not None
                and type(previous_authorization_revision) is not int
            )
            or (
                previous_readiness_digest is not None
                and not isinstance(previous_readiness_digest, str)
            )
            or (
                previous_active_slot is not None
                and not isinstance(previous_active_slot, str)
            )
        ):
            raise ValueError("Generation job reauthorization revision is invalid")
        if resolved_binding is not None and (
            resolved_binding.job_id != str(job_id)
            or resolved_binding.expected_narrative_revision != previous_revision
            or resolved_binding.authorization_revision
            != previous_authorization_revision
            or resolved_binding.readiness_digest != previous_readiness_digest
        ):
            raise ValueError(
                "Resolved generation job mutation recovery does not match "
                "the previous authorization"
            )
        if resolved_takeover is not None:
            takeover = resolved_takeover.takeover
            if (
                takeover.job_id != str(job_id)
                or takeover.expected_narrative_revision != previous_revision
                or takeover.authorization_revision
                != previous_authorization_revision
                or takeover.readiness_digest != previous_readiness_digest
                or resolved_takeover.narrative_revision != next_revision
            ):
                raise ValueError(
                    "Resolved candidate manual takeover does not match "
                    "the previous authorization"
                )
        query: dict[str, Any] = {
            "_id": to_object_id(job_id),
            "is_deleted": False,
            "status": previous_status,
            "active_slot": previous_active_slot,
        }
        resolved_checkpoint_values: list[dict[str, Any]] | None = None
        if resolved_takeover is not None:
            current = await self.get_job(job_id)
            try:
                current_takeover = parse_candidate_manual_takeover(
                    current.get("candidate_manual_takeover")
                )
                resolved_checkpoints = validate_candidate_manual_takeover_binding(
                    current_takeover,
                    current.get("candidate_pipeline_checkpoints"),
                )
            except (TypeError, ValueError) as exc:
                raise CandidatePipelineCheckpointConflict(
                    "Candidate manual takeover recovery binding is invalid"
                ) from exc
            if current_takeover != resolved_takeover.takeover:
                raise CandidatePipelineCheckpointConflict(
                    "Candidate manual takeover recovery binding changed"
                )
            resolved_checkpoint_values = [
                checkpoint.model_dump(mode="json")
                for checkpoint in resolved_checkpoints
            ]
            query.update({
                "novel_id": to_object_id(current_takeover.novel_id),
                "pause_reason": "incomplete_scene",
                "current_chapter_id": current_takeover.chapter_id,
                "current_failure_event_id": current_takeover.failure_event_id,
                "candidate_manual_takeover": current_takeover.model_dump(
                    mode="json"
                ),
                "candidate_pipeline_checkpoints": resolved_checkpoint_values,
                "state_dispatch_resolution": None,
            })
        else:
            query["candidate_pipeline_checkpoints"] = []
        if resolved_binding is None:
            query["$or"] = [
                {"job_mutation_recovery": {"$exists": False}},
                {"job_mutation_recovery": None},
            ]
        else:
            query.update({
                "novel_id": to_object_id(resolved_binding.novel_id),
                "current_chapter_id": resolved_binding.chapter_id,
                "job_mutation_recovery": resolved_binding.model_dump(mode="json"),
                "state_dispatch_resolution": None,
            })
        if previous_execution_epoch == 0:
            query["$and"] = [{
                "$or": [
                    {"execution_epoch": 0},
                    {"execution_epoch": {"$exists": False}},
                ]
            }]
        else:
            query["execution_epoch"] = previous_execution_epoch
        if previous_revision is None:
            query["expected_narrative_revision"] = {"$exists": False}
        else:
            query["expected_narrative_revision"] = previous_revision
        if previous_authorization_revision is None:
            query["authorization_revision"] = {"$exists": False}
        else:
            query["authorization_revision"] = previous_authorization_revision
        if previous_readiness_digest is None:
            query["readiness.digest"] = {"$exists": False}
        else:
            query["readiness.digest"] = previous_readiness_digest
        unset_fields = {"execution_lease": ""}
        if resolved_binding is not None:
            unset_fields["job_mutation_recovery"] = ""
        if resolved_takeover is not None:
            unset_fields["candidate_manual_takeover"] = ""
        set_fields = {
            **dict(fields),
            "expected_narrative_revision": next_revision,
            "updated_at": get_utc_now(),
        }
        update: dict[str, Any] = {
            "$inc": {"execution_epoch": 1},
            "$unset": unset_fields,
            "$set": set_fields,
        }
        if resolved_takeover is not None:
            set_fields.update({
                "candidate_pipeline_checkpoints": [],
                "current_chapter_id": None,
                "current_failure_event_id": None,
            })
            takeover_event = {
                **resolved_takeover.model_dump(mode="json"),
                "resolved_at": get_utc_now(),
            }
            update["$push"] = {
                "candidate_manual_takeover_events": {
                    "$each": [takeover_event],
                    "$slice": -MAX_CANDIDATE_MANUAL_TAKEOVER_EVENTS,
                }
            }
        result = await self._collection_update_one(
            query,
            update,
        )
        if result.modified_count == 1:
            return True
        raise CandidatePipelineCheckpointConflict(
            "Generation job reauthorization lost its revision fence"
        )

    @staticmethod
    def _book_completion_snapshot_query(
        job_id: str,
        *,
        previous_status: str,
        previous_pause_reason: str | None,
        previous_execution_epoch: int,
        previous_expected_narrative_revision: int | None,
        novel_id: str,
    ) -> dict[str, Any]:
        lease = current_job_execution()
        worker_publish = lease is not None
        if (
            type(previous_execution_epoch) is not int
            or previous_execution_epoch < 0
            or previous_execution_epoch >= _MAX_NARRATIVE_REVISION
            or (
                previous_expected_narrative_revision is not None
                and (
                    type(previous_expected_narrative_revision) is not int
                    or previous_expected_narrative_revision < 0
                )
            )
            or (worker_publish and previous_status != "running")
            or (
                not worker_publish
                and (
                    previous_status not in {"paused", "failed"}
                    or previous_pause_reason != "final_audit"
                )
            )
        ):
            raise CandidatePipelineCheckpointConflict(
                "Book completion audit publication snapshot is invalid"
            )
        epoch_query: dict[str, Any] = {
            "execution_epoch": previous_execution_epoch
        }
        if previous_execution_epoch == 0:
            epoch_query = {
                "$or": [
                    {"execution_epoch": 0},
                    {"execution_epoch": {"$exists": False}},
                ]
            }
        revision_query: dict[str, Any] = {
            "expected_narrative_revision": (
                previous_expected_narrative_revision
            )
        }
        if previous_expected_narrative_revision is None:
            revision_query = {
                "$or": [
                    {"expected_narrative_revision": {"$exists": False}},
                    {"expected_narrative_revision": None},
                ]
            }
        clauses: list[dict[str, Any]] = [
            {
                "_id": to_object_id(job_id),
                "novel_id": to_object_id(novel_id),
                "scope": "book",
                "is_deleted": False,
                "status": previous_status,
                "pause_reason": previous_pause_reason,
                "state_dispatch_resolution": None,
            },
            epoch_query,
            revision_query,
        ]
        if not worker_publish:
            clauses.append({
                "$or": [
                    {"execution_lease": {"$exists": False}},
                    {"execution_lease": None},
                ]
            })
        return {"$and": clauses}

    @staticmethod
    def _book_completion_publication_query(
        job_id: str,
        fence_token: str,
        *,
        live_after: datetime | None = None,
    ) -> dict[str, Any]:
        if not str(fence_token or ""):
            raise CandidatePipelineCheckpointConflict(
                "Book completion audit publication token is required"
            )
        query: dict[str, Any] = {
            "completion_audit_publication.schema_version": (
                BOOK_COMPLETION_PUBLICATION_SCHEMA
            ),
            "completion_audit_publication.token": str(fence_token),
            "completion_audit_publication.job_id": str(job_id),
            "completion_audit_publication.expires_at": {"$type": "date"},
        }
        if live_after is not None:
            query["completion_audit_publication.expires_at"] = {
                "$type": "date",
                "$gt": live_after,
            }
        return query

    async def reserve_book_completion_audit_publication(
        self,
        job_id: str,
        *,
        fence_token: str,
        previous_status: str,
        previous_pause_reason: str | None,
        previous_execution_epoch: int,
        previous_expected_narrative_revision: int | None,
        novel_id: str,
    ) -> bool:
        """Reserve the Job-local token that owns one persistent audit fence."""

        now = get_utc_now()
        query = self._book_completion_snapshot_query(
            job_id,
            previous_status=previous_status,
            previous_pause_reason=previous_pause_reason,
            previous_execution_epoch=previous_execution_epoch,
            previous_expected_narrative_revision=(
                previous_expected_narrative_revision
            ),
            novel_id=novel_id,
        )
        query["$and"].append({
            "$or": [
                {"completion_audit_publication": {"$exists": False}},
                {"completion_audit_publication": None},
                {
                    "completion_audit_publication.expires_at": {
                        "$lte": now,
                    }
                },
                self._book_completion_publication_query(
                    job_id,
                    fence_token,
                ),
            ]
        })
        result = await self._collection_update_one(
            query,
            {
                "$set": {
                    "completion_audit_publication": {
                        "schema_version": BOOK_COMPLETION_PUBLICATION_SCHEMA,
                        "token": str(fence_token),
                        "job_id": str(job_id),
                        "expires_at": now + timedelta(
                            seconds=BOOK_COMPLETION_PUBLICATION_LEASE_SECONDS
                        ),
                    },
                    "updated_at": now,
                }
            },
        )
        if result.modified_count == 1:
            return True
        raise CandidatePipelineCheckpointConflict(
            "Book completion audit publication reservation raced"
        )

    async def renew_book_completion_audit_publication(
        self,
        job_id: str,
        *,
        fence_token: str,
        previous_status: str,
        previous_pause_reason: str | None,
        previous_execution_epoch: int,
        previous_expected_narrative_revision: int | None,
        novel_id: str,
    ) -> bool:
        """Renew only an unreplaced token; stale-fence recovery may revoke it."""

        now = get_utc_now()
        query = self._book_completion_snapshot_query(
            job_id,
            previous_status=previous_status,
            previous_pause_reason=previous_pause_reason,
            previous_execution_epoch=previous_execution_epoch,
            previous_expected_narrative_revision=(
                previous_expected_narrative_revision
            ),
            novel_id=novel_id,
        )
        query["$and"].append(
            self._book_completion_publication_query(job_id, fence_token)
        )
        result = await self._collection_update_one(
            query,
            {
                "$set": {
                    "completion_audit_publication.expires_at": (
                        now + timedelta(
                            seconds=BOOK_COMPLETION_PUBLICATION_LEASE_SECONDS
                        )
                    ),
                    "updated_at": now,
                }
            },
        )
        if result.modified_count == 1:
            return True
        raise CandidatePipelineCheckpointConflict(
            "Book completion audit publication token was revoked"
        )

    async def publish_book_completion_audit(
        self,
        job_id: str,
        report_value: Mapping[str, Any],
        *,
        fence_token: str,
        previous_status: str,
        previous_pause_reason: str | None,
        previous_execution_epoch: int,
        previous_expected_narrative_revision: int | None,
    ) -> bool:
        """Atomically publish one fenced audit against an exact Job snapshot."""

        from backend.services.novel.book_completion import BookCompletionReport

        try:
            report = BookCompletionReport.model_validate(report_value)
        except ValueError as exc:
            raise CandidatePipelineCheckpointConflict(
                "Book completion audit report is invalid"
            ) from exc
        if (
            not fence_token
            or report.blueprint.frozen_job_id != str(job_id)
        ):
            raise CandidatePipelineCheckpointConflict(
                "Book completion audit is not bound to its publication fence"
            )
        now = get_utc_now()
        fenced_novel = await get_database()[collections.NOVELS].find_one(
            {
                "_id": to_object_id(report.novel_id),
                "$expr": {
                    "$eq": [
                        {"$ifNull": ["$narrative_revision", 0]},
                        report.narrative_revision,
                    ]
                },
                "narrative_write_fence.token": str(fence_token),
                "narrative_write_fence.resource_kind": (
                    "book_completion_audit"
                ),
                "narrative_write_fence.resource_id": str(job_id),
                "narrative_write_fence.expires_at": {"$exists": False},
            },
            projection={"_id": 1},
        )
        if fenced_novel is None:
            raise CandidatePipelineCheckpointConflict(
                "Book completion audit lost its narrative revision fence"
            )
        if report.complete:
            status = "completed"
            pause_reason = None
            error = None
        else:
            status = "paused"
            pause_reason = "final_audit"
            error = {
                "step": "final_audit",
                "message": "Book completion audit has blocking issues",
                "audit_digest": report.audit_digest,
                "blocking_issue_codes": sorted({
                    issue.code
                    for issue in report.issues
                    if issue.level == "blocking"
                }),
            }
        update: dict[str, Any] = {
            "$set": {
                "status": status,
                "pause_reason": pause_reason,
                "current_chapter_id": None,
                "active_slot": None,
                "error": error,
                "current_failure_event_id": None,
                "completion_audit": report.model_dump(mode="json"),
                "updated_at": now,
            },
            "$unset": {"completion_audit_publication": ""},
        }
        if current_job_execution() is None:
            update["$inc"] = {"execution_epoch": 1}
            update["$unset"]["execution_lease"] = ""
        query = self._book_completion_snapshot_query(
            job_id,
            previous_status=previous_status,
            previous_pause_reason=previous_pause_reason,
            previous_execution_epoch=previous_execution_epoch,
            previous_expected_narrative_revision=(
                previous_expected_narrative_revision
            ),
            novel_id=report.novel_id,
        )
        query["$and"].append(
            self._book_completion_publication_query(
                job_id,
                fence_token,
                live_after=now,
            )
        )
        result = await self._collection_update_one(
            query,
            update,
        )
        if result.modified_count == 1:
            return True
        raise CandidatePipelineCheckpointConflict(
            "Book completion audit publication lost its execution fence"
        )

    async def publish_book_completion_audit_failure(
        self,
        job_id: str,
        *,
        novel_id: str,
        fence_token: str,
        message: str,
        source_changed: bool,
        previous_status: str,
        previous_pause_reason: str | None,
        previous_execution_epoch: int,
        previous_expected_narrative_revision: int | None,
    ) -> bool:
        """Fail closed without allowing an old audit to overwrite Job control."""

        pause_reason = "source_changed" if source_changed else "final_audit"
        update: dict[str, Any] = {
            "$set": {
                "status": "paused" if source_changed else "failed",
                "pause_reason": pause_reason,
                "current_chapter_id": None,
                "active_slot": None,
                "error": {
                    "step": pause_reason,
                    "message": str(message),
                },
                "updated_at": get_utc_now(),
            },
            "$unset": {"completion_audit_publication": ""},
        }
        if current_job_execution() is None:
            update["$inc"] = {"execution_epoch": 1}
            update["$unset"]["execution_lease"] = ""
        query = self._book_completion_snapshot_query(
            job_id,
            previous_status=previous_status,
            previous_pause_reason=previous_pause_reason,
            previous_execution_epoch=previous_execution_epoch,
            previous_expected_narrative_revision=(
                previous_expected_narrative_revision
            ),
            novel_id=novel_id,
        )
        query["$and"].append(
            self._book_completion_publication_query(job_id, fence_token)
        )
        result = await self._collection_update_one(
            query,
            update,
        )
        if result.modified_count == 1:
            return True
        raise CandidatePipelineCheckpointConflict(
            "Book completion audit failure lost its execution fence"
        )

    async def transition_job_resume(
        self,
        job_id: str,
        fields: Mapping[str, Any],
        *,
        previous_status: str,
        previous_execution_epoch: int,
    ) -> bool:
        """Publish a manual resume while revoking any finishing old worker."""

        updates = dict(fields)
        _reject_atomic_field_updates(updates)
        if (
            previous_status not in {"paused", "interrupted", "failed"}
            or type(previous_execution_epoch) is not int
            or previous_execution_epoch < 0
            or updates.get("status") != "running"
        ):
            raise CandidatePipelineCheckpointConflict(
                "Generation job resume command is invalid"
            )
        epoch_query: dict[str, Any] = {
            "execution_epoch": previous_execution_epoch
        }
        if previous_execution_epoch == 0:
            epoch_query = {
                "$or": [
                    {"execution_epoch": 0},
                    {"execution_epoch": {"$exists": False}},
                ]
            }
        result = await self._collection_update_one(
            {
                "$and": [
                    {
                        "_id": to_object_id(job_id),
                        "is_deleted": False,
                        "status": previous_status,
                        "state_dispatch_resolution": None,
                    },
                    epoch_query,
                    {
                        "$or": [
                            {"candidate_manual_takeover": {"$exists": False}},
                            {"candidate_manual_takeover": None},
                        ]
                    },
                ]
            },
            {
                "$inc": {"execution_epoch": 1},
                "$unset": {"execution_lease": ""},
                "$set": {**updates, "updated_at": get_utc_now()},
            },
        )
        if result.modified_count == 1:
            return True
        raise CandidatePipelineCheckpointConflict(
            "Generation job resume lost its execution fence"
        )

    async def complete_job_abort(
        self,
        job_id: str,
    ) -> bool:
        """Publish abort while atomically freezing every live paid attempt."""

        current = await self.get_job(job_id)
        previous_epoch = current.get("execution_epoch", 0)
        if (
            type(previous_epoch) is not int
            or previous_epoch < 0
            or previous_epoch >= _MAX_NARRATIVE_REVISION
        ):
            raise CandidatePipelineCheckpointConflict(
                "Generation job abort execution epoch is invalid"
            )
        epoch_query: dict[str, Any] = {
            "execution_epoch": previous_epoch
        }
        if previous_epoch == 0:
            epoch_query = {
                "$or": [
                    {"execution_epoch": 0},
                    {"execution_epoch": {"$exists": False}},
                ]
            }
        now = get_utc_now()
        acknowledged_state = "uncertain_abort_acknowledged"
        result = await self._collection_update_one(
            {
                "$and": [
                    {
                        "_id": to_object_id(job_id),
                        "is_deleted": False,
                        "status": {"$nin": ["completed", "aborted"]},
                        "state_dispatch_resolution": None,
                    },
                    epoch_query,
                ]
            },
            [
                {
                    "$set": {
                        **_live_attempt_transition_fields(
                            source_states=("claimed", "uncertain"),
                            target_state=acknowledged_state,
                            now=now,
                        ),
                        "execution_epoch": previous_epoch + 1,
                        "execution_lease": "$$REMOVE",
                        "completion_audit_publication": "$$REMOVE",
                        "attempt_reservation": None,
                        "has_uncertain_attempts": False,
                        "status": "aborted",
                        "current_chapter_id": None,
                        "active_slot": None,
                        "updated_at": now,
                    }
                }
            ],
        )
        if result.modified_count == 1:
            return True
        current = await self.get_job(job_id)
        if current.get("status") == "aborted":
            return True
        raise CandidatePipelineCheckpointConflict(
            "Generation job abort lost its execution fence"
        )

    async def complete_candidate_pipeline_chapter(
        self,
        job_id: str,
        *,
        chapter_id: str,
        expected_checkpoints: Sequence[CandidatePipelineCheckpointV1],
        entry: CandidatePipelineProgressV1,
        tokens_delta: int,
        expected_narrative_revision: int | None = None,
        next_narrative_revision: int | None = None,
    ) -> bool:
        """Atomically publish progress and release exactly one checkpoint tail."""
        normalized_chapter_id = str(chapter_id or "")
        try:
            validated_entry = parse_candidate_pipeline_progress(entry)
        except Exception as exc:
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline progress identity is invalid"
            ) from exc
        if validated_entry.chapter_id != normalized_chapter_id:
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline progress identity is invalid"
            )
        if (
            type(tokens_delta) is not int
            or tokens_delta < 0
            or tokens_delta > 2**63 - 1
        ):
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline progress token delta is invalid"
            )
        revision_transition_supplied = (
            expected_narrative_revision is not None
            or next_narrative_revision is not None
        )
        if revision_transition_supplied and (
            type(expected_narrative_revision) is not int
            or type(next_narrative_revision) is not int
            or expected_narrative_revision < 0
            or next_narrative_revision != expected_narrative_revision + 1
            or next_narrative_revision > _MAX_NARRATIVE_REVISION
        ):
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline narrative revision transition is invalid"
            )
        if (
            isinstance(expected_checkpoints, (str, bytes))
            or not isinstance(expected_checkpoints, Sequence)
            or not 1
            <= len(expected_checkpoints)
            <= MAX_CHAPTER_CANDIDATE_PIPELINE_CHECKPOINTS
        ):
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline completion ledger is invalid"
            )
        try:
            validated_checkpoints = tuple(
                parse_candidate_pipeline_checkpoint(checkpoint)
                for checkpoint in expected_checkpoints
            )
        except Exception as exc:
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline completion ledger is invalid"
            ) from exc
        if any(
            checkpoint.chapter_id != normalized_chapter_id
            or checkpoint.sequence != sequence
            for sequence, checkpoint in enumerate(
                validated_checkpoints,
                start=1,
            )
        ) or len({
            checkpoint.checkpoint_id for checkpoint in validated_checkpoints
        }) != len(validated_checkpoints):
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline completion ledger is invalid"
            )
        validated_checkpoint = validated_checkpoints[-1]
        if validated_checkpoint.kind != "state_candidate":
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline completion ledger is incomplete"
            )
        receipt = CandidatePipelineCompletionV1(
            schema_version="candidate_pipeline_completion.v1",
            checkpoint_id=validated_checkpoint.checkpoint_id,
            checkpoint_digest=candidate_pipeline_checkpoint_digest(
                validated_checkpoint
            ),
            ledger_digest=candidate_pipeline_checkpoint_ledger_digest(
                validated_checkpoints
            ),
            sequence=validated_checkpoint.sequence,
            chapter_id=validated_checkpoint.chapter_id,
            source=validated_checkpoint.source,
            state_proposal_id=validated_checkpoint.proposal_id,
            tokens_delta=tokens_delta,
        )
        job = await self.get_job(job_id)
        current_revision = job.get("expected_narrative_revision")
        if current_revision is not None and type(current_revision) is not int:
            raise CandidatePipelineCheckpointConflict(
                "Generation job narrative revision cursor is invalid"
            )
        if (current_revision is not None) != revision_transition_supplied:
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline narrative revision transition is required"
            )
        if self._completed_candidate_progress(
            job,
            expected_receipt=receipt,
            entry=validated_entry,
        ):
            if (
                revision_transition_supplied
                and current_revision != next_narrative_revision
            ):
                raise CandidatePipelineCheckpointConflict(
                    "Candidate pipeline completion revision diverged"
                )
            return True
        if (
            revision_transition_supplied
            and current_revision != expected_narrative_revision
        ):
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline narrative revision cursor changed"
            )
        expected_scene_count, max_repair_cycles = (
            await self._candidate_completion_authority(
                job,
                chapter_id=normalized_chapter_id,
            )
        )
        evidence = self._validate_candidate_completion_chain(
            validated_checkpoints,
            entry=validated_entry,
            expected_scene_count=expected_scene_count,
            max_repair_cycles=max_repair_cycles,
        )
        if evidence.state != validated_checkpoint:
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline completion ledger is invalid"
            )
        raw_progress = job.get("progress")
        if raw_progress is None:
            raw_progress = []
        if (
            not isinstance(raw_progress, list)
            or len(raw_progress) >= MAX_CANDIDATE_PIPELINE_PROGRESS_ENTRIES
        ):
            raise CandidatePipelineCheckpointConflict(
                "Generation job progress ledger capacity is exhausted"
            )
        if (
            str(job.get("status") or "") != "running"
            or str(job.get("current_chapter_id") or "")
            != normalized_chapter_id
        ):
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline execution is no longer current"
            )
        checkpoints = self._candidate_pipeline_checkpoints(
            dict(job),
            chapter_id=normalized_chapter_id,
        )
        self._validate_candidate_completion_chain(
            checkpoints,
            entry=validated_entry,
            expected_scene_count=expected_scene_count,
            max_repair_cycles=max_repair_cycles,
        )
        if tuple(checkpoints) != validated_checkpoints:
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline completion ledger changed"
            )
        tail = checkpoints[-1]
        value = validated_entry.model_dump(mode="json")
        value["candidate_pipeline_completion"] = receipt.model_dump(
            mode="json"
        )
        value["completed_at"] = get_utc_now()
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "status": "running",
                "current_chapter_id": normalized_chapter_id,
                "candidate_pipeline_checkpoints": [
                    checkpoint.model_dump(mode="json")
                    for checkpoint in checkpoints
                ],
                "progress.candidate_pipeline_completion.ledger_digest": {
                    "$ne": receipt.ledger_digest
                },
                **(
                    {
                        "expected_narrative_revision": (
                            expected_narrative_revision
                        )
                    }
                    if revision_transition_supplied
                    else {}
                ),
                "$expr": {
                    "$lt": [
                        {
                            "$size": {
                                "$ifNull": ["$progress", []]
                            }
                        },
                        MAX_CANDIDATE_PIPELINE_PROGRESS_ENTRIES,
                    ]
                },
            },
            {
                "$push": {"progress": value},
                "$inc": {"tokens_used": tokens_delta},
                "$set": {
                    "candidate_pipeline_checkpoints": [],
                    "current_chapter_id": None,
                    "current_failure_event_id": None,
                    **(
                        {
                            "expected_narrative_revision": (
                                next_narrative_revision
                            )
                        }
                        if revision_transition_supplied
                        else {}
                    ),
                    "updated_at": get_utc_now(),
                },
            },
        )
        if result.modified_count == 1:
            return True
        current = await self.get_job(job_id)
        if self._completed_candidate_progress(
            current,
            expected_receipt=receipt,
            entry=validated_entry,
        ):
            if (
                revision_transition_supplied
                and current.get("expected_narrative_revision")
                != next_narrative_revision
            ):
                raise CandidatePipelineCheckpointConflict(
                    "Candidate pipeline completion revision diverged"
                )
            return True
        raise CandidatePipelineCheckpointConflict(
            "Candidate pipeline completion lost its checkpoint fence"
        )

    async def update_job_fields(self, job_id: str, fields: Dict[str, Any]) -> bool:
        return await self.update_one({"_id": to_object_id(job_id)}, dict(fields))

    async def publish_required_reviewed_candidate(
        self,
        job_id: str,
        candidate: Any,
    ) -> bool:
        """Atomically publish one non-formal reviewed-candidate handoff.

        The command deliberately leaves ``progress`` and the narrative revision
        unchanged. Its only terminal effect is to pause the Job on the exact
        current chapter so a later, separately authorized Module can consume
        the metadata-only result.
        """

        from backend.services.generation.required_chapter_review_job import (
            RequiredChapterReviewJobConflict,
            parse_required_reviewed_candidate,
            validate_required_chapter_review_readiness,
        )
        from backend.db.required_adherence_journal import (
            RequiredReviewJobBinding,
            _read_owned_complete_source,
            candidate_write_fences,
        )
        from backend.services.generation.prose_runs import prose_revision
        from backend.services.novel.state_completion import (
            chapter_content_digest,
        )

        parsed = parse_required_reviewed_candidate(candidate)
        binding = RequiredReviewJobBinding(
            job_id=parsed.job_id,
            owner_id=parsed.owner_id,
            novel_id=parsed.novel_id,
            chapter_id=parsed.chapter_id,
            readiness_digest=parsed.readiness_digest,
            authorization_revision=parsed.authorization_revision,
            narrative_revision=parsed.narrative_revision,
        )
        try:
            async with candidate_write_fences(
                binding,
                run_id=parsed.source_run_id,
                run_revision=parsed.source_run_revision,
            ):
                run, chapter = await _read_owned_complete_source(
                    binding,
                    run_id=parsed.source_run_id,
                    run_revision=parsed.source_run_revision,
                )
                current = await self.get_job(job_id)
                authorization = validate_required_chapter_review_readiness(
                    current.get("readiness")
                )
                chapter_authorization = authorization.chapter(
                    parsed.chapter_id
                )
                text = run.get("assembled_text")
                outline = chapter.get("outline")
                if (
                    not isinstance(text, str)
                    or not isinstance(outline, Mapping)
                    or chapter_content_digest(text)
                    != parsed.source_content_digest
                    or run.get("outline_revision")
                    != chapter_authorization.outline_revision
                    or prose_revision(outline)
                    != chapter_authorization.outline_revision
                    or bool(str(chapter.get("content") or "").strip())
                ):
                    raise RequiredChapterReviewJobConflict(
                        "required_reviewed_candidate_source_stale"
                    )
                return await self._publish_required_reviewed_candidate_snapshot(
                    job_id,
                    parsed=parsed,
                    current=current,
                )
        except RequiredChapterReviewJobConflict:
            raise
        except (KeyError, TypeError, ValueError, NotFoundError) as exc:
            raise RequiredChapterReviewJobConflict(
                "required_reviewed_candidate_source_stale"
            ) from exc

    async def _publish_required_reviewed_candidate_snapshot(
        self,
        job_id: str,
        *,
        parsed: Any,
        current: Mapping[str, Any],
    ) -> bool:
        """Commit a source-fenced reviewed result against one exact Job view."""

        from backend.services.generation.required_chapter_review_job import (
            REQUIRED_REVIEWED_CANDIDATE_PAUSE_REASON,
            RequiredChapterReviewJobConflict,
            parse_required_reviewed_candidate,
            validate_required_reviewed_candidate_job,
        )

        value = parsed.model_dump(mode="json")
        existing = current.get("required_reviewed_candidate")
        if existing is not None:
            stored = parse_required_reviewed_candidate(existing)
            validate_required_reviewed_candidate_job(current, stored)
            if (
                stored == parsed
                and current.get("status") == "paused"
                and current.get("pause_reason")
                == REQUIRED_REVIEWED_CANDIDATE_PAUSE_REASON
                and current.get("active_slot") is None
                and current.get("current_chapter_id") == parsed.chapter_id
            ):
                return False
            raise RequiredChapterReviewJobConflict(
                "required_reviewed_candidate_replay_diverged"
            )
        validate_required_reviewed_candidate_job(current, parsed)
        journal_fields = {
            name: deepcopy(current.get(name))
            for name in (
                "required_initial_prose_journal",
                "required_prose_rewrite_journal",
                "required_adherence_journal",
            )
        }
        accounting_fields = {
            name: deepcopy(current.get(name))
            for name in (
                "attempt_slots",
                "usage_attempt_capacity",
                "usage_attempt_claimed",
                "usage_attempt_ids",
                "token_budget",
                "tokens_used",
            )
        }
        error = {
            "step": "required_chapter_review",
            "chapter_id": parsed.chapter_id,
            "message": (
                "Reviewed prose candidate is ready for the next authorized stage"
            ),
            "reason_codes": [REQUIRED_REVIEWED_CANDIDATE_PAUSE_REASON],
            "next_step": parsed.next_step,
            "result_digest": parsed.result_digest,
            "can_write_formal_prose": False,
            "can_generate_state": False,
        }
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "status": "running",
                "current_chapter_id": parsed.chapter_id,
                "readiness.digest": parsed.readiness_digest,
                "authorization_revision": parsed.authorization_revision,
                "expected_narrative_revision": parsed.narrative_revision,
                "has_uncertain_attempts": False,
                "active_token_reservations": [],
                "tokens_reserved": 0,
                "attempt_reservation": None,
                "state_dispatch_resolution": None,
                "job_mutation_recovery": None,
                "candidate_pipeline_checkpoints": [],
                "required_reviewed_candidate": None,
                "progress": deepcopy(current.get("progress")),
                **journal_fields,
                **accounting_fields,
            },
            {
                "$set": {
                    "required_reviewed_candidate": value,
                    "status": "paused",
                    "pause_reason": REQUIRED_REVIEWED_CANDIDATE_PAUSE_REASON,
                    "active_slot": None,
                    "error": error,
                    "current_failure_event_id": None,
                    "updated_at": get_utc_now(),
                }
            },
        )
        if result.modified_count == 1:
            return True
        latest = await self.get_job(job_id)
        replay = latest.get("required_reviewed_candidate")
        if replay is not None:
            stored = parse_required_reviewed_candidate(replay)
            validate_required_reviewed_candidate_job(latest, stored)
            if (
                stored == parsed
                and latest.get("status") == "paused"
                and latest.get("pause_reason")
                == REQUIRED_REVIEWED_CANDIDATE_PAUSE_REASON
                and latest.get("current_chapter_id") == parsed.chapter_id
            ):
                return False
        raise RequiredChapterReviewJobConflict(
            "required_reviewed_candidate_publish_fence_lost"
        )

    async def pause_required_chapter_review(
        self,
        job_id: str,
        *,
        chapter_id: str,
        phase: str,
        reason_code: str,
        repair_count: int,
    ) -> bool:
        """Pause a bounded review failure without advancing formal progress."""

        from backend.services.generation.required_chapter_review_job import (
            RequiredChapterReviewJobConflict,
            RequiredChapterReviewJobOutcome,
            required_review_repair_count,
            validate_required_chapter_review_readiness,
        )
        from backend.db.required_adherence_journal import _checked_bookkeeping

        outcome = RequiredChapterReviewJobOutcome(
            phase=phase,
            reviewed_candidate=None,
            repair_count=repair_count,
            reason_code=reason_code,
        )
        current = await self.get_job(job_id)
        authorization = validate_required_chapter_review_readiness(
            current.get("readiness")
        )
        authorization.chapter(str(chapter_id))
        _checked_bookkeeping(current)
        durable_repair_count = required_review_repair_count(current)
        if (
            str(current.get("novel_id") or "") != authorization.novel_id
            or str(current.get("owner_id") or "") != authorization.owner_id
            or current.get("is_deleted") is not False
            or current.get("status") != "running"
            or current.get("authorization_revision")
            != authorization.authorization_revision
            or current.get("expected_narrative_revision")
            != authorization.narrative_revision
            or current.get("required_reviewed_candidate") is not None
            or current.get("candidate_pipeline_checkpoints") not in (None, [])
            or current.get("job_mutation_recovery") is not None
            or current.get("state_dispatch_resolution") is not None
            or current.get("has_uncertain_attempts") is not False
            or current.get("active_token_reservations") not in (None, [])
            or current.get("tokens_reserved") not in (None, 0)
            or current.get("attempt_reservation") is not None
            or repair_count != durable_repair_count
        ):
            raise RequiredChapterReviewJobConflict(
                "required_chapter_review_pause_proof_invalid"
            )
        pause_reason = (
            reason_code
            if reason_code in {"cost_cap", "attempt_capacity"}
            else f"required_review_{outcome.phase}"
        )
        error = {
            "step": "required_chapter_review",
            "chapter_id": str(chapter_id),
            "message": "Required chapter review stopped before formal completion",
            "reason_codes": [reason_code],
            "next_step": (
                "resume_required_review"
                if outcome.phase == "incomplete"
                else "manual_review"
            ),
            "repair_count": repair_count,
            "can_write_formal_prose": False,
            "can_generate_state": False,
        }
        journals = {
            name: deepcopy(current.get(name))
            for name in (
                "required_initial_prose_journal",
                "required_prose_rewrite_journal",
                "required_adherence_journal",
            )
        }
        accounting = {
            name: deepcopy(current.get(name))
            for name in (
                "attempt_slots",
                "usage_attempt_capacity",
                "usage_attempt_claimed",
                "usage_attempt_ids",
                "token_budget",
                "tokens_used",
                "has_uncertain_attempts",
                "active_token_reservations",
                "tokens_reserved",
                "attempt_reservation",
            )
        }
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "status": "running",
                "current_chapter_id": str(chapter_id),
                "readiness.digest": current["readiness"]["digest"],
                "authorization_revision": authorization.authorization_revision,
                "expected_narrative_revision": authorization.narrative_revision,
                "required_reviewed_candidate": None,
                "progress": deepcopy(current.get("progress")),
                **journals,
                **accounting,
            },
            {
                "$set": {
                    "status": "paused",
                    "pause_reason": pause_reason,
                    "current_chapter_id": str(chapter_id),
                    "active_slot": None,
                    "error": error,
                    "updated_at": get_utc_now(),
                }
            },
        )
        if result.modified_count == 1:
            return True
        latest = await self.get_job(job_id)
        if (
            latest.get("status") == "paused"
            and latest.get("pause_reason") == pause_reason
            and latest.get("current_chapter_id") == str(chapter_id)
            and latest.get("required_reviewed_candidate") is None
            and latest.get("error") == error
        ):
            return False
        raise RequiredChapterReviewJobConflict(
            "required_chapter_review_pause_fence_lost"
        )

    async def begin_required_state_candidate(
        self,
        job_id: str,
        request: Any,
    ) -> bool:
        """Append one exact state request before its first paid claim."""

        from backend.db.required_state_candidate_journal import (
            begin_required_state_entry_value,
        )
        from backend.services.generation.required_chapter_state_job import (
            RequiredChapterStateJobConflict,
            RequiredStateCandidateRequest,
        )

        try:
            parsed = RequiredStateCandidateRequest.model_validate(request)
            current = await self.get_job(job_id)
            before, changed = begin_required_state_entry_value(current, parsed)
        except (TypeError, ValueError, ValidationError) as exc:
            raise RequiredChapterStateJobConflict(
                "required_state_candidate_begin_invalid"
            ) from exc
        if not changed:
            return False
        raw_before = current.get("required_state_candidate_journal")
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "status": "running",
                "current_chapter_id": parsed.binding.chapter_id,
                "readiness.digest": parsed.binding.readiness_digest,
                "authorization_revision": parsed.binding.authorization_revision,
                "expected_narrative_revision": (
                    parsed.binding.expected_narrative_revision
                ),
                "required_state_candidate_journal": deepcopy(raw_before),
                "required_state_candidate": None,
                "progress": deepcopy(current.get("progress")),
            },
            {
                "$set": {
                    "required_state_candidate_journal": before.model_dump(
                        mode="json"
                    ),
                    "updated_at": get_utc_now(),
                }
            },
        )
        if result.modified_count == 1:
            return True
        latest = await self.get_job(job_id)
        replay, replay_changed = begin_required_state_entry_value(latest, parsed)
        if not replay_changed and replay == before:
            return False
        raise RequiredChapterStateJobConflict(
            "required_state_candidate_begin_fence_lost"
        )

    async def read_required_state_predecessor_job(
        self,
        state_job_id: str,
        predecessor_job_id: str,
    ) -> Dict[str, Any]:
        """Read only the reviewed Job frozen into the leased state Job.

        A worker lease normally (and deliberately) forbids reading any other
        Generation Job.  The state successor needs one cross-Job read, so this
        Adapter re-proves the exact predecessor from the current readiness
        before using the underlying read-only collection Interface.  It does
        not permit writes or arbitrary cross-Job lookup.
        """

        from backend.services.generation.required_chapter_review_job import (
            parse_required_reviewed_candidate,
            validate_required_reviewed_candidate_job,
        )
        from backend.services.generation.required_chapter_state_job import (
            RequiredChapterStateJobConflict,
            validate_required_chapter_state_readiness,
        )

        lease = current_job_execution()
        if lease is None or lease.job_id != str(state_job_id):
            raise RequiredChapterStateJobConflict(
                "required_state_predecessor_read_unowned"
            )
        current = await self.get_job(state_job_id)
        authorization = validate_required_chapter_state_readiness(
            current.get("readiness")
        )
        frozen = authorization.predecessor_candidate
        if (
            current.get("status") != "running"
            or str(current.get("owner_id") or "") != authorization.owner_id
            or str(current.get("novel_id") or "") != authorization.novel_id
            or current.get("authorization_revision")
            != authorization.authorization_revision
            or current.get("expected_narrative_revision")
            != authorization.narrative_revision
            or frozen.job_id != str(predecessor_job_id)
        ):
            raise RequiredChapterStateJobConflict(
                "required_state_predecessor_read_stale"
            )
        predecessor = await self._base.find_one({
            "_id": to_object_id(frozen.job_id),
            "owner_id": to_object_id(frozen.owner_id),
            "novel_id": to_object_id(frozen.novel_id),
            "is_deleted": False,
            "status": "paused",
            "pause_reason": "required_reviewed_candidate_ready",
            "current_chapter_id": frozen.chapter_id,
        })
        if predecessor is None:
            raise RequiredChapterStateJobConflict(
                "required_state_predecessor_unavailable"
            )
        parsed = parse_required_reviewed_candidate(
            predecessor.get("required_reviewed_candidate")
        )
        validate_required_reviewed_candidate_job(predecessor, parsed)
        if parsed != frozen:
            raise RequiredChapterStateJobConflict(
                "required_state_predecessor_changed"
            )
        return predecessor

    async def publish_required_state_observation(
        self,
        job_id: str,
        request: Any,
        observation: Any,
    ) -> bool:
        """Settle one state observation against its exact paid attempts."""

        from backend.db.required_state_candidate_journal import (
            observation_entry_value,
        )
        from backend.services.generation.required_chapter_state_job import (
            RequiredChapterStateJobConflict,
            RequiredStateCandidateObservation,
            RequiredStateCandidateRequest,
        )

        try:
            parsed_request = RequiredStateCandidateRequest.model_validate(request)
            parsed_observation = RequiredStateCandidateObservation.model_validate(
                observation
            )
            current = await self.get_job(job_id)
            before, after = observation_entry_value(
                current,
                parsed_request,
                parsed_observation,
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise RequiredChapterStateJobConflict(
                "required_state_observation_invalid"
            ) from exc
        if before == after:
            return False
        accounting = {
            name: deepcopy(current.get(name))
            for name in (
                "attempt_slots",
                "usage_attempt_capacity",
                "usage_attempt_claimed",
                "usage_attempt_ids",
                "token_budget",
                "tokens_used",
                "tokens_reserved",
                "active_token_reservations",
                "attempt_reservation",
                "has_uncertain_attempts",
            )
        }
        if (
            accounting["tokens_reserved"] not in (None, 0)
            or accounting["active_token_reservations"] not in (None, [])
            or accounting["has_uncertain_attempts"] is not False
        ):
            raise RequiredChapterStateJobConflict(
                "required_state_observation_accounting_unsettled"
            )
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "status": "running",
                "current_chapter_id": parsed_request.binding.chapter_id,
                "readiness.digest": parsed_request.binding.readiness_digest,
                "authorization_revision": (
                    parsed_request.binding.authorization_revision
                ),
                "expected_narrative_revision": (
                    parsed_request.binding.expected_narrative_revision
                ),
                "required_state_candidate_journal": before.model_dump(
                    mode="json"
                ),
                "required_state_candidate": None,
                "progress": deepcopy(current.get("progress")),
                **accounting,
            },
            {
                "$set": {
                    "required_state_candidate_journal": after.model_dump(
                        mode="json"
                    ),
                    "updated_at": get_utc_now(),
                }
            },
        )
        if result.modified_count == 1:
            return True
        latest = await self.get_job(job_id)
        replay_before, replay_after = observation_entry_value(
            latest,
            parsed_request,
            parsed_observation,
        )
        if replay_before == replay_after and replay_before == after:
            return False
        raise RequiredChapterStateJobConflict(
            "required_state_observation_fence_lost"
        )

    async def publish_required_state_candidate(
        self,
        job_id: str,
        candidate: Any,
    ) -> bool:
        """Publish a consistent, recoverable state handoff without mutation."""

        from backend.db.required_adherence_journal import (
            RequiredReviewJobBinding,
            _read_owned_complete_source,
            candidate_write_fences,
        )
        from backend.db.required_state_candidate_journal import (
            parse_required_state_candidate_journal,
        )
        from backend.services.generation.chapter_generation_application import (
            ChapterGenerationResult,
            ChapterGenerationStage,
        )
        from backend.services.generation.required_chapter_review_job import (
            parse_required_reviewed_candidate,
            validate_required_reviewed_candidate_job,
        )
        from backend.services.generation.required_chapter_state_job import (
            REQUIRED_STATE_CANDIDATE_PAUSE_REASON,
            RequiredChapterStateJobConflict,
            evaluate_required_state_candidate,
            parse_required_state_candidate,
            required_state_source_from_run,
            validate_required_chapter_state_readiness,
            validate_required_state_candidate_job,
        )
        from backend.services.novel.state_proposal import state_proposal_module

        parsed = parse_required_state_candidate(candidate)
        current = await self.get_job(job_id)
        existing = current.get("required_state_candidate")
        if existing is not None:
            stored = parse_required_state_candidate(existing)
            validate_required_state_candidate_job(current, stored)
            if (
                stored == parsed
                and current.get("status") == "paused"
                and current.get("pause_reason")
                == REQUIRED_STATE_CANDIDATE_PAUSE_REASON
            ):
                return False
            raise RequiredChapterStateJobConflict(
                "required_state_candidate_replay_diverged"
            )
        validate_required_state_candidate_job(current, parsed)
        authorization = validate_required_chapter_state_readiness(
            current.get("readiness")
        )
        predecessor_job = await self.read_required_state_predecessor_job(
            job_id,
            parsed.predecessor_job_id,
        )
        predecessor = parse_required_reviewed_candidate(
            predecessor_job.get("required_reviewed_candidate")
        )
        validate_required_reviewed_candidate_job(predecessor_job, predecessor)
        if predecessor != authorization.predecessor_candidate:
            raise RequiredChapterStateJobConflict(
                "required_state_predecessor_changed"
            )
        predecessor_binding = RequiredReviewJobBinding(
            job_id=predecessor.job_id,
            owner_id=predecessor.owner_id,
            novel_id=predecessor.novel_id,
            chapter_id=predecessor.chapter_id,
            readiness_digest=predecessor.readiness_digest,
            authorization_revision=predecessor.authorization_revision,
            narrative_revision=predecessor.narrative_revision,
        )
        async with candidate_write_fences(
            predecessor_binding,
            run_id=predecessor.source_run_id,
            run_revision=predecessor.source_run_revision,
        ):
            run, chapter = await _read_owned_complete_source(
                predecessor_binding,
                run_id=predecessor.source_run_id,
                run_revision=predecessor.source_run_revision,
            )
            if bool(str(chapter.get("content") or "").strip()):
                raise RequiredChapterStateJobConflict(
                    "required_state_source_stale"
                )
            source = required_state_source_from_run(run, authorization)
            journal = parse_required_state_candidate_journal(
                current.get("required_state_candidate_journal")
            )
            latest = journal.entries[-1]
            if latest.observation is None:
                raise RequiredChapterStateJobConflict(
                    "required_state_observation_missing"
                )
            recovered = await state_proposal_module.recover_required_state_generation(
                latest.request.binding
            )
            if recovered is None:
                raise RequiredChapterStateJobConflict(
                    "required_state_proposal_missing"
                )
            generation = ChapterGenerationResult(
                stage=ChapterGenerationStage.STATE,
                value=recovered.value,
                usage={},
                attempts=[],
                truncation={
                    "truncated_section_count": recovered.truncated_section_count,
                    "dropped_item_count": recovered.dropped_item_count,
                },
                dropped=(
                    {
                        "dropped_reference_count": (
                            recovered.dropped_reference_count
                        )
                    }
                    if recovered.dropped_reference_count
                    else {}
                ),
                accepted=False,
            )
            reprojection = evaluate_required_state_candidate(
                generation,
                source=source,
                chapter=chapter,
                attempt_ids=latest.observation.attempt_ids,
            )
            if reprojection != latest.observation:
                raise RequiredChapterStateJobConflict(
                    "required_state_proposal_projection_changed"
                )
            current = await self.get_job(job_id)
            validate_required_state_candidate_job(current, parsed)
            value = parsed.model_dump(mode="json")
            protected = {
                name: deepcopy(current.get(name))
                for name in (
                    "required_state_candidate_journal",
                    "attempt_slots",
                    "usage_attempt_capacity",
                    "usage_attempt_claimed",
                    "usage_attempt_ids",
                    "token_budget",
                    "tokens_used",
                    "progress",
                )
            }
            result = await self._collection_update_one(
                {
                    "_id": to_object_id(job_id),
                    "is_deleted": False,
                    "status": "running",
                    "current_chapter_id": parsed.chapter_id,
                    "readiness.digest": parsed.readiness_digest,
                    "authorization_revision": parsed.authorization_revision,
                    "expected_narrative_revision": parsed.narrative_revision,
                    "required_state_candidate": None,
                    "has_uncertain_attempts": False,
                    "active_token_reservations": [],
                    "tokens_reserved": 0,
                    "attempt_reservation": None,
                    **protected,
                },
                {
                    "$set": {
                        "required_state_candidate": value,
                        "status": "paused",
                        "pause_reason": REQUIRED_STATE_CANDIDATE_PAUSE_REASON,
                        "active_slot": None,
                        "error": {
                            "step": "required_chapter_state",
                            "chapter_id": parsed.chapter_id,
                            "message": (
                                "Consistent deferred state candidate is ready"
                            ),
                            "reason_codes": [
                                REQUIRED_STATE_CANDIDATE_PAUSE_REASON
                            ],
                            "next_step": parsed.next_step,
                            "result_digest": parsed.result_digest,
                            "can_write_formal_prose": False,
                            "can_accept_formal_state": False,
                        },
                        "current_failure_event_id": None,
                        "updated_at": get_utc_now(),
                    }
                },
            )
            if result.modified_count == 1:
                return True
        latest_job = await self.get_job(job_id)
        replay = latest_job.get("required_state_candidate")
        if replay is not None:
            stored = parse_required_state_candidate(replay)
            validate_required_state_candidate_job(latest_job, stored)
            if stored == parsed:
                return False
        raise RequiredChapterStateJobConflict(
            "required_state_candidate_publish_fence_lost"
        )

    async def read_required_finalization_predecessor_jobs(
        self,
        finalization_job_id: str,
        state_job_id: str,
        reviewed_job_id: str,
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        """Read only the two Jobs frozen into a leased finalization Job."""

        from backend.services.generation.required_chapter_finalization_job import (
            RequiredChapterFinalizationJobConflict,
            build_required_chapter_finalization_authorization,
            validate_required_chapter_finalization_readiness,
        )
        from backend.services.generation.required_chapter_review_job import (
            REQUIRED_REVIEWED_CANDIDATE_PAUSE_REASON,
            parse_required_reviewed_candidate,
            validate_required_reviewed_candidate_job,
        )
        from backend.services.generation.required_chapter_state_job import (
            REQUIRED_STATE_CANDIDATE_PAUSE_REASON,
            parse_required_state_candidate,
            validate_required_state_candidate_job,
        )

        lease = current_job_execution()
        if lease is None or lease.job_id != str(finalization_job_id):
            raise RequiredChapterFinalizationJobConflict(
                "required_finalization_predecessor_read_unowned"
            )
        current = await self.get_job(finalization_job_id)
        authorization = validate_required_chapter_finalization_readiness(
            current.get("readiness")
        )
        if (
            current.get("status") != "running"
            or current.get("authorization_revision")
            != authorization.authorization_revision
            or current.get("expected_narrative_revision")
            != authorization.narrative_revision
            or authorization.state_candidate.job_id != str(state_job_id)
            or authorization.reviewed_candidate.job_id != str(reviewed_job_id)
        ):
            raise RequiredChapterFinalizationJobConflict(
                "required_finalization_predecessor_read_stale"
            )
        state_job = await self._base.find_one({
            "_id": to_object_id(authorization.state_candidate.job_id),
            "owner_id": to_object_id(authorization.owner_id),
            "novel_id": to_object_id(authorization.novel_id),
            "is_deleted": False,
            "status": "paused",
            "pause_reason": REQUIRED_STATE_CANDIDATE_PAUSE_REASON,
            "current_chapter_id": authorization.chapter_id,
        })
        reviewed_job = await self._base.find_one({
            "_id": to_object_id(authorization.reviewed_candidate.job_id),
            "owner_id": to_object_id(authorization.owner_id),
            "novel_id": to_object_id(authorization.novel_id),
            "is_deleted": False,
            "status": "paused",
            "pause_reason": REQUIRED_REVIEWED_CANDIDATE_PAUSE_REASON,
            "current_chapter_id": authorization.chapter_id,
        })
        if state_job is None or reviewed_job is None:
            raise RequiredChapterFinalizationJobConflict(
                "required_finalization_predecessor_unavailable"
            )
        state = parse_required_state_candidate(
            state_job.get("required_state_candidate")
        )
        reviewed = parse_required_reviewed_candidate(
            reviewed_job.get("required_reviewed_candidate")
        )
        validate_required_state_candidate_job(state_job, state)
        validate_required_reviewed_candidate_job(reviewed_job, reviewed)
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
        return state_job, reviewed_job

    async def complete_required_chapter_finalization(
        self,
        job_id: str,
        outcome: Any,
    ) -> bool:
        """Publish one recovered formal mutation receipt and terminal result."""

        from backend.services.generation.required_chapter_finalization_job import (
            RequiredChapterFinalizationJobConflict,
            RequiredChapterFinalizationJobOutcome,
            parse_required_chapter_finalization_result,
            validate_required_chapter_finalization_readiness,
            validate_required_chapter_finalization_result_job,
        )

        if not isinstance(outcome, RequiredChapterFinalizationJobOutcome):
            raise RequiredChapterFinalizationJobConflict(
                "required_finalization_outcome_invalid"
            )
        result = parse_required_chapter_finalization_result(outcome.result)
        receipt = JobMutationReceiptV1.model_validate(
            outcome.mutation_receipt
        )
        binding = receipt.binding
        current = await self.get_job(job_id)
        authorization = validate_required_chapter_finalization_readiness(
            current.get("readiness")
        )
        validate_required_chapter_finalization_result_job(current, result)
        expected_binding = JobMutationRecoveryBindingV1(
            novel_id=authorization.novel_id,
            job_id=str(job_id),
            chapter_id=authorization.chapter_id,
            readiness_digest=str(current["readiness"]["digest"]),
            authorization_revision=authorization.authorization_revision,
            expected_narrative_revision=authorization.narrative_revision,
            operation="finalize_chapter_generation",
            idempotency_key=(
                f"finalize-chapter-generation:{result.source_run_id}:"
                f"{result.source_run_revision}:{result.state_proposal_id}"
            ),
        )
        if (
            binding != expected_binding
            or receipt.next_narrative_revision
            != result.narrative_revision_after
        ):
            raise RequiredChapterFinalizationJobConflict(
                "required_finalization_mutation_receipt_changed"
            )
        canonical_result = result.model_dump(mode="json")
        canonical_receipt = receipt.model_dump(mode="json")
        progress = {
            "schema_version": "required_chapter_finalization_progress.v1",
            "chapter_id": result.chapter_id,
            "steps_done": ["chapter_finalization"],
            "steps_skipped": [],
            "tokens": 0,
            "attempts": [],
            "summary_written": True,
            "result_digest": result.result_digest,
            "certificate_digest": result.certificate_digest,
            "completion_receipt_digest": result.completion_receipt_digest,
            "job_mutation_receipt": canonical_receipt,
            "job_mutation_tokens_delta": 0,
        }
        result_update = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "status": "running",
                "current_chapter_id": result.chapter_id,
                "authorization_revision": result.authorization_revision,
                "expected_narrative_revision": result.narrative_revision_before,
                "readiness.digest": result.readiness_digest,
                "job_mutation_recovery": binding.model_dump(mode="json"),
                "required_chapter_finalization_result": None,
                "progress": deepcopy(current.get("progress")),
                "attempt_slots": [],
                "usage_attempt_claimed": 0,
                "tokens_used": 0,
                "tokens_reserved": 0,
                "active_token_reservations": [],
                "attempt_reservation": None,
                "has_uncertain_attempts": False,
            },
            {
                "$set": {
                    "required_chapter_finalization_result": canonical_result,
                    "progress": [progress],
                    "status": "completed",
                    "pause_reason": None,
                    "active_slot": None,
                    "current_chapter_id": None,
                    "current_failure_event_id": None,
                    "expected_narrative_revision": (
                        result.narrative_revision_after
                    ),
                    "error": None,
                    "updated_at": get_utc_now(),
                },
                "$unset": {"job_mutation_recovery": ""},
            },
        )
        if result_update.modified_count == 1:
            return True
        latest = await self.get_job(job_id)
        stored = latest.get("required_chapter_finalization_result")
        if stored is not None:
            parsed = parse_required_chapter_finalization_result(stored)
            validate_required_chapter_finalization_result_job(latest, parsed)
            if (
                parsed == result
                and latest.get("status") == "completed"
                and latest.get("expected_narrative_revision")
                == result.narrative_revision_after
                and latest.get("job_mutation_recovery") is None
                and latest.get("progress") == [progress]
            ):
                return False
        raise RequiredChapterFinalizationJobConflict(
            "required_finalization_publish_fence_lost"
        )

    async def pause_required_chapter_state(
        self,
        job_id: str,
        *,
        chapter_id: str,
        reason_code: str,
        reextraction_count: int,
    ) -> bool:
        """Pause a locally failed state gate without formal progress."""

        from backend.db.required_state_candidate_journal import (
            parse_required_state_candidate_journal,
            validate_required_state_attempt_accounting,
        )
        from backend.services.generation.required_chapter_state_job import (
            RequiredChapterStateJobConflict,
            RequiredChapterStateJobOutcome,
            validate_required_chapter_state_readiness,
        )

        RequiredChapterStateJobOutcome(
            phase="blocked",
            state_candidate=None,
            reextraction_count=reextraction_count,
            reason_code=reason_code,
        )
        current = await self.get_job(job_id)
        authorization = validate_required_chapter_state_readiness(
            current.get("readiness")
        )
        journal = parse_required_state_candidate_journal(
            current.get("required_state_candidate_journal")
        )
        for entry in journal.entries:
            validate_required_state_attempt_accounting(
                current,
                entry,
                authorization,
            )
        latest = journal.entries[-1]
        if (
            str(chapter_id) != authorization.chapter_id
            or current.get("status") != "running"
            or current.get("current_chapter_id") != str(chapter_id)
            or current.get("required_state_candidate") is not None
            or len(journal.entries) != 3
            or reextraction_count != 2
            or latest.phase != "produced"
            or latest.observation is None
            or latest.observation.gate_passed
            or current.get("has_uncertain_attempts") is not False
            or current.get("active_token_reservations") not in (None, [])
            or current.get("tokens_reserved") not in (None, 0)
            or current.get("attempt_reservation") is not None
            or current.get("progress") not in (None, [])
        ):
            raise RequiredChapterStateJobConflict(
                "required_state_pause_proof_invalid"
            )
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "status": "running",
                "current_chapter_id": str(chapter_id),
                "readiness.digest": current["readiness"]["digest"],
                "authorization_revision": authorization.authorization_revision,
                "expected_narrative_revision": authorization.narrative_revision,
                "required_state_candidate_journal": journal.model_dump(
                    mode="json"
                ),
                "required_state_candidate": None,
                "attempt_slots": deepcopy(current.get("attempt_slots")),
                "tokens_used": current.get("tokens_used"),
                "progress": deepcopy(current.get("progress")),
            },
            {
                "$set": {
                    "status": "paused",
                    "pause_reason": "required_state_blocked",
                    "active_slot": None,
                    "error": {
                        "step": "required_chapter_state",
                        "chapter_id": str(chapter_id),
                        "message": (
                            "Required state gate exhausted bounded re-extraction"
                        ),
                        "reason_codes": [reason_code],
                        "next_step": "manual_state_review",
                        "reextraction_count": reextraction_count,
                        "can_write_formal_prose": False,
                        "can_accept_formal_state": False,
                    },
                    "updated_at": get_utc_now(),
                }
            },
        )
        if result.modified_count == 1:
            return True
        latest_job = await self.get_job(job_id)
        if (
            latest_job.get("status") == "paused"
            and latest_job.get("pause_reason") == "required_state_blocked"
            and latest_job.get("current_chapter_id") == str(chapter_id)
        ):
            return False
        raise RequiredChapterStateJobConflict(
            "required_state_pause_fence_lost"
        )

    async def publish_outline_authorization_recalculation(
        self,
        job_id: str,
        *,
        command: OutlineAuthorizationRecalculationCommandV1,
    ) -> dict[str, Any]:
        """Prove and publish one post-outline authorization narrowing."""

        if not isinstance(command, OutlineAuthorizationRecalculationCommandV1):
            raise ValueError(
                "Outline authorization recalculation command is required"
            )
        frozen = OutlineAuthorizationRecalculationCommandV1.model_validate(
            command.model_dump(mode="python")
        )
        current = await self.get_job(job_id)
        if str(current.get("status") or "") != "running":
            raise CandidatePipelineCheckpointConflict(
                "Outline authorization Job is no longer running"
            )
        readiness = current.get("readiness")
        if not isinstance(readiness, Mapping):
            raise ValueError("Generation Job readiness is invalid")
        readiness_digest = readiness.get("digest")
        if (
            not isinstance(readiness_digest, str)
            or readiness_digest != frozen.readiness_digest
            or current.get("current_chapter_id") != frozen.chapter_id
            or current.get("expected_narrative_revision")
            != frozen.expected_narrative_revision
            or current.get("authorization_revision")
            != frozen.authorization_revision
        ):
            raise CandidatePipelineCheckpointConflict(
                "Outline authorization snapshot changed"
            )
        raw_current_authorization = current.get(
            "prose_continuation_authorization"
        )
        current_authorization = parse_prose_authorization(
            raw_current_authorization
        )
        decision = evaluate_outline_authorization_recalculation(
            command=frozen,
            current_authorization=current_authorization,
            job_token_budget=current.get("token_budget"),
        )
        payload = decision.model_dump(mode="json")
        fields: dict[str, Any] = {
            "readiness_recalculation": payload,
            "authorization_confirmation_required": (
                payload if decision.requires_confirmation else None
            ),
        }
        assert isinstance(raw_current_authorization, Mapping)
        current_payload = dict(raw_current_authorization)
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "status": "running",
                "state_dispatch_resolution": None,
                "current_chapter_id": frozen.chapter_id,
                "expected_narrative_revision": (
                    frozen.expected_narrative_revision
                ),
                "authorization_revision": frozen.authorization_revision,
                "readiness.digest": frozen.readiness_digest,
                "prose_continuation_authorization": current_payload,
            },
            {"$set": {**fields, "updated_at": get_utc_now()}},
        )
        if result.matched_count == 1:
            return payload
        raise CandidatePipelineCheckpointConflict(
            "Outline authorization decision lost its execution fence"
        )

    async def consume_uncertain_prose_retry(self, job_id: str) -> bool:
        """Consume the one-shot retry grant without a generic authority patch."""

        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "state_dispatch_resolution": None,
                "confirm_uncertain_prose_retry": True,
            },
            {
                "$set": {
                    "confirm_uncertain_prose_retry": False,
                    "updated_at": get_utc_now(),
                }
            },
        )
        return result.modified_count == 1

    async def append_diagnostic(
        self,
        job_id: str,
        event: Dict[str, Any],
    ) -> bool:
        event_id = self._validated_diagnostic_event_id(event)
        result = await self._collection_update_one(
            {"_id": to_object_id(job_id), "is_deleted": False},
            {
                "$push": {"diagnostics": {"$each": [dict(event)], "$slice": -200}},
                "$set": {
                    "diagnostic_schema_version": 1,
                    "current_failure_event_id": event_id,
                    "updated_at": get_utc_now(),
                },
            },
        )
        return result.matched_count > 0

    async def append_chapter_completion_decision(
        self,
        job_id: str,
        *,
        chapter_id: str,
        prose_run_id: str,
        prose_run_revision: int,
        decision: Mapping[str, Any],
        fence: ChapterCompletionDecisionFence,
    ) -> bool:
        """Append one bounded, canonical completion decision exactly once."""

        from backend.services.generation.chapter_completion_certificate import (
            ChapterCompletionDecision,
        )

        if (
            not isinstance(fence, ChapterCompletionDecisionFence)
            or not ObjectId.is_valid(str(chapter_id))
            or not ObjectId.is_valid(str(prose_run_id))
            or type(prose_run_revision) is not int
            or prose_run_revision < 1
        ):
            raise ValueError("Chapter completion decision binding is invalid")
        parsed = ChapterCompletionDecision.model_validate(decision)
        canonical = parsed.model_dump(mode="json")
        entry = {
            "chapter_id": str(chapter_id),
            "prose_run_id": str(prose_run_id),
            "prose_run_revision": prose_run_revision,
            "decision": canonical,
            "recorded_at": get_utc_now(),
        }
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                **fence.query(),
                "chapter_completion_decisions.decision.decision_id": {
                    "$ne": parsed.decision_id
                },
                "$expr": {
                    "$lt": [
                        {
                            "$size": {
                                "$ifNull": [
                                    "$chapter_completion_decisions",
                                    [],
                                ]
                            }
                        },
                        200,
                    ]
                },
            },
            {
                "$push": {"chapter_completion_decisions": entry},
                "$set": {"updated_at": get_utc_now()},
            },
        )
        if result.matched_count == 1:
            return True
        current = await self.get_job(job_id)
        matches = [
            item
            for item in current.get("chapter_completion_decisions") or []
            if isinstance(item, Mapping)
            and isinstance(item.get("decision"), Mapping)
            and item["decision"].get("decision_id") == parsed.decision_id
        ]
        if len(matches) == 1 and all(
            (
                str(matches[0].get("chapter_id") or "") == str(chapter_id),
                str(matches[0].get("prose_run_id") or "")
                == str(prose_run_id),
                matches[0].get("prose_run_revision")
                == prose_run_revision,
                dict(matches[0]["decision"]) == canonical,
            )
        ):
            return False
        if len(current.get("chapter_completion_decisions") or []) >= 200:
            raise ValueError("Chapter completion decision ledger is full")
        raise ValueError("Chapter completion decision append lost its fence")

    @staticmethod
    def _validated_diagnostic_event_id(event: Mapping[str, Any]) -> str:
        event_id = event.get("event_id")
        if (
            not isinstance(event_id, str)
            or not event_id
            or event_id != event_id.strip()
            or len(event_id) > 240
        ):
            raise ValueError("Generation diagnostic event id is invalid")
        return event_id

    async def pause_for_source_change(
        self,
        job_id: str,
        *,
        diagnostic: Mapping[str, Any],
        error: Mapping[str, Any],
        expected_status: str,
        expected_pause_reason: str | None,
        expected_execution_epoch: int,
        expected_failure_event_id: str | None,
    ) -> bool:
        """Atomically bind a source-change pause to its owning diagnostic."""

        event_id = self._validated_diagnostic_event_id(diagnostic)
        if (
            diagnostic.get("schema_version") != 1
            or str(diagnostic.get("category") or "") != "source_changed"
            or str(error.get("step") or "") != "source_changed"
            or expected_status != "paused"
            or not isinstance(expected_pause_reason, str)
            or not expected_pause_reason
            or type(expected_execution_epoch) is not int
            or expected_execution_epoch < 0
            or (
                expected_failure_event_id is not None
                and (
                    not isinstance(expected_failure_event_id, str)
                    or not expected_failure_event_id
                    or expected_failure_event_id
                    != expected_failure_event_id.strip()
                    or len(expected_failure_event_id) > 240
                )
            )
        ):
            raise ValueError("Source-change diagnostic contract is invalid")
        query: dict[str, Any] = {
            "_id": to_object_id(job_id),
            "is_deleted": False,
            "status": expected_status,
            "pause_reason": expected_pause_reason,
            "current_failure_event_id": expected_failure_event_id,
        }
        if expected_execution_epoch == 0:
            query["$or"] = [
                {"execution_epoch": 0},
                {"execution_epoch": {"$exists": False}},
            ]
        else:
            query["execution_epoch"] = expected_execution_epoch
        result = await self._collection_update_one(
            query,
            {
                "$push": {
                    "diagnostics": {
                        "$each": [dict(diagnostic)],
                        "$slice": -200,
                    }
                },
                "$set": {
                    "status": "paused",
                    "pause_reason": "source_changed",
                    "active_slot": None,
                    "error": dict(error),
                    "diagnostic_schema_version": 1,
                    "current_failure_event_id": event_id,
                    "updated_at": get_utc_now(),
                },
            },
        )
        return result.matched_count > 0

    async def append_progress(
        self,
        job_id: str,
        entry: Dict[str, Any],
        tokens_delta: int,
        *,
        resolves_current_failure: bool = False,
    ) -> bool:
        # $push progress + $inc tokens_used 在一次原子 update 内完成。
        if (
            "candidate_pipeline_completion" in entry
            or "job_mutation_receipt" in entry
        ):
            raise ValueError(
                "Mutation completion requires an atomic repository command"
            )
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "$expr": {
                    "$lt": [
                        {
                            "$size": {
                                "$ifNull": ["$progress", []]
                            }
                        },
                        MAX_CANDIDATE_PIPELINE_PROGRESS_ENTRIES,
                    ]
                },
            },
            {
                "$push": {"progress": entry},
                "$inc": {"tokens_used": int(tokens_delta)},
                "$set": {
                    "updated_at": get_utc_now(),
                    **(
                        {"current_failure_event_id": None}
                        if resolves_current_failure
                        else {}
                    ),
                },
            },
        )
        if result.matched_count > 0:
            return True
        current = await self.get_job(job_id)
        raw_progress = current.get("progress")
        if (
            isinstance(raw_progress, list)
            and len(raw_progress) >= MAX_CANDIDATE_PIPELINE_PROGRESS_ENTRIES
        ):
            raise ValueError(
                "Generation job progress ledger capacity is exhausted"
            )
        return False

    async def reserve_attempts(self, job_id: str, chapter_id: str, slots: int) -> Dict[str, Any]:
        """为下一章保留固定数量的 Provider 调用槽，不扩大作业总容量。"""
        requested = int(slots)
        if requested < 0:
            raise ValueError("Attempt reservation cannot be negative")
        job = await self.get_job(job_id)
        require_current_authorization(job)
        capacity = int(job.get("usage_attempt_capacity") or 0)
        claimed = int(job.get("usage_attempt_claimed") or 0)
        reservation = job.get("attempt_reservation") or {}
        if (
            str(reservation.get("chapter_id") or "") == str(chapter_id)
            and int(reservation.get("reserved_slots") or 0) >= requested
        ):
            return reservation
        if claimed + requested > capacity:
            raise AttemptCapacityExceeded(
                f"Attempt capacity exhausted: need {requested}, "
                f"remaining {max(0, capacity - claimed)}"
            )
        prepared = {
            "chapter_id": str(chapter_id),
            "reserved_slots": requested,
            "claimed_slots": 0,
            "created_at": get_utc_now(),
        }
        result = await self._collection_find_one_and_update(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "state_dispatch_resolution": None,
                "$expr": {
                    "$lte": [
                        {"$add": [{"$ifNull": ["$usage_attempt_claimed", 0]}, requested]},
                        {"$ifNull": ["$usage_attempt_capacity", 0]},
                    ]
                },
            },
            {"$set": {"attempt_reservation": prepared, "updated_at": get_utc_now()}},
            return_document=ReturnDocument.AFTER,
        )
        if result is None:
            raise AttemptCapacityExceeded("Attempt capacity changed while reserving chapter")
        return dict(result["attempt_reservation"])

    async def claim_attempt(
        self,
        job_id: str,
        chapter_id: str,
        step_id: str,
        phase: str,
        provider_alias: str,
    ) -> str:
        """Reject the removed pre-budget claim protocol before it can persist."""
        del job_id, chapter_id, step_id, phase, provider_alias
        raise TokenBudgetUnbounded(
            "Every Provider attempt requires a conservative token bound"
        )

    async def account_attempt(
        self,
        job_id: str,
        attempt_id: str,
        usage: TokenUsage,
    ) -> bool:
        """Reject settlement that has no matching conservative reservation."""
        del job_id, attempt_id, usage
        raise TokenBudgetUnbounded(
            "Every Provider attempt requires a conservative token bound"
        )

    async def mark_attempt_uncertain(self, job_id: str, attempt_id: str, reason: str) -> bool:
        now = get_utc_now()
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "state_dispatch_resolution": None,
                "attempt_slots": {"$elemMatch": {
                    "attempt_id": str(attempt_id),
                    "state": "claimed",
                }},
            },
            {
                "$set": {
                    "attempt_slots.$[slot].state": "uncertain",
                    "attempt_slots.$[slot].uncertain_reason": str(reason),
                    "attempt_slots.$[slot].updated_at": now,
                    "has_uncertain_attempts": True,
                    "updated_at": now,
                },
                "$addToSet": {"uncertain_attempt_ids": str(attempt_id)},
            },
            array_filters=[{"slot.attempt_id": str(attempt_id)}],
        )
        return result.modified_count == 1

    async def finish_attempt_reservation(self, job_id: str, chapter_id: str) -> bool:
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "state_dispatch_resolution": None,
                "attempt_reservation.chapter_id": str(chapter_id),
            },
            {"$set": {"attempt_reservation": None, "updated_at": get_utc_now()}},
        )
        return result.matched_count == 1

    async def mark_claimed_attempts_uncertain(self, job_id: str, reason: str) -> int:
        job = await self.get_job(job_id)
        pending = [
            (str(slot["attempt_id"]), slot.get("conservative_tokens"))
            for slot in job.get("attempt_slots") or []
            if slot.get("state") == "claimed"
        ]
        changed = 0
        for attempt_id, conservative_tokens in pending:
            if conservative_tokens is None:
                changed += int(await self.mark_attempt_uncertain(job_id, attempt_id, reason))
            else:
                changed += int(await self.mark_attempt_uncertain_with_budget(
                    job_id, attempt_id, reason
                ))
        return changed

    async def acknowledge_uncertain_attempts(self, job_id: str, action: str) -> bool:
        if action not in STATE_DISPATCH_RESOLUTION_ACTIONS:
            raise ValueError("Unknown uncertain-attempt action")
        now = get_utc_now()
        acknowledged_state = f"uncertain_{action}_acknowledged"
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "state_dispatch_resolution": None,
            },
            [
                {
                    "$set": {
                        **_live_attempt_transition_fields(
                            source_states=("uncertain",),
                            target_state=acknowledged_state,
                            now=now,
                        ),
                        "has_uncertain_attempts": False,
                        "attempt_reservation": None,
                        "updated_at": now,
                    }
                }
            ],
        )
        return result.matched_count == 1

    async def bind_pre_dispatch_fence(
        self,
        job_id: str,
        chapter_id: str,
        fence: PreDispatchFenceV1,
    ) -> None:
        """Publish the receipt lease into the same document used for claims.

        A receipt takeover is cross-collection. Publishing its token here first
        makes every later Job attempt claim a local, atomic fencing check.
        """
        validated_fence = _validated_pre_dispatch_fence(fence)
        value = validated_fence.model_dump(mode="json")
        fence_path = "attempt_reservation.pre_dispatch_fence"
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "state_dispatch_resolution": None,
                "attempt_reservation.chapter_id": str(chapter_id),
                "$or": [
                    {fence_path: {"$exists": False}},
                    {fence_path: value},
                    {
                        f"{fence_path}.schema_version": value["schema_version"],
                        f"{fence_path}.receipt_id": value["receipt_id"],
                        f"{fence_path}.cycle": value["cycle"],
                        f"{fence_path}.claim_epoch": {
                            "$lt": value["claim_epoch"]
                        }
                    },
                    {
                        f"{fence_path}.schema_version": value["schema_version"],
                        f"{fence_path}.cycle": {"$lt": value["cycle"]},
                        "attempt_slots": {
                            "$not": {
                                "$elemMatch": {
                                    "state": {"$in": ["claimed", "uncertain"]}
                                }
                            }
                        },
                    },
                ],
            },
            {
                "$set": {
                    fence_path: value,
                    "updated_at": get_utc_now(),
                }
            },
        )
        if result.matched_count != 1:
            raise AttemptFenceExpired(
                "Chapter attempt reservation cannot publish the dispatch fence"
            )

    async def claim_attempt_with_budget(
        self,
        job_id: str,
        chapter_id: str,
        step_id: str,
        phase: str,
        provider_alias: str,
        conservative_tokens: int | None,
        *,
        pre_dispatch_fence: PreDispatchFenceV1 | None = None,
        interactive_execution_token: str | None = None,
    ) -> str:
        """Atomically claim an attempt slot and reserve its worst-case tokens."""
        if (
            type(conservative_tokens) is not int
            or conservative_tokens <= 0
            or conservative_tokens > MAX_PERSISTED_ATTEMPT_TOKENS
        ):
            raise TokenBudgetUnbounded(
                "Every Provider attempt requires a conservative token bound"
            )
        reserved = conservative_tokens
        if interactive_execution_token is not None and (
            not isinstance(interactive_execution_token, str)
            or len(interactive_execution_token) != 64
            or any(
                character not in "0123456789abcdef"
                for character in interactive_execution_token
            )
        ):
            raise ValueError("Interactive execution token is invalid")
        attempt_id = uuid4().hex
        now = get_utc_now()
        slot = {
            "attempt_id": attempt_id,
            "chapter_id": str(chapter_id),
            "step_id": str(step_id),
            "phase": str(phase),
            "provider_alias": str(provider_alias),
            "state": "claimed",
            "claimed_at": now,
            "conservative_tokens": reserved,
        }
        fence: dict[str, Any] | None = None
        if pre_dispatch_fence is not None:
            validated_fence = _validated_pre_dispatch_fence(
                pre_dispatch_fence
            )
            if validated_fence.step_id != str(step_id):
                raise ValueError("pre-dispatch fence step does not match the claim")
            fence = validated_fence.model_dump(mode="json")
            slot["pre_dispatch_fence"] = fence
        query: dict[str, Any] = {
            "_id": to_object_id(job_id),
            RESTORED_AUTHORITY_FIELD: None,
            "is_deleted": False,
            "state_dispatch_resolution": None,
            "attempt_reservation.chapter_id": str(chapter_id),
        }
        if interactive_execution_token is not None:
            query.update({
                "status": "completion_running",
                "interactive_execution_claim.token": (
                    interactive_execution_token
                ),
            })
        if fence is not None:
            query["attempt_reservation.pre_dispatch_fence"] = fence
        query["$expr"] = {
            "$and": [
                    {
                        "$lt": [
                            {"$ifNull": ["$usage_attempt_claimed", 0]},
                            {"$ifNull": ["$usage_attempt_capacity", 0]},
                        ]
                    },
                    {
                        "$lt": [
                            {"$ifNull": ["$attempt_reservation.claimed_slots", 0]},
                            {"$ifNull": ["$attempt_reservation.reserved_slots", 0]},
                        ]
                    },
                    {
                        "$lt": [
                            {"$size": {"$ifNull": ["$active_token_reservations", []]}},
                            MAX_ACTIVE_TOKEN_RESERVATIONS,
                        ]
                    },
                    {
                        "$or": [
                            {"$eq": [{"$ifNull": ["$token_budget", None]}, None]},
                            {
                                "$lte": [
                                    {
                                        "$add": [
                                            {"$ifNull": ["$tokens_used", 0]},
                                            {"$ifNull": ["$tokens_reserved", 0]},
                                            reserved,
                                        ]
                                    },
                                    {"$ifNull": ["$token_budget", 0]},
                                ]
                            },
                        ]
                    },
            ]
        }
        update: dict[str, Any] = {
            "$inc": {
                "usage_attempt_claimed": 1,
                "attempt_reservation.claimed_slots": 1,
            },
            "$push": {"attempt_slots": slot},
            "$set": {"updated_at": now},
        }
        update["$inc"]["tokens_reserved"] = reserved
        update["$push"]["active_token_reservations"] = {
            "attempt_id": attempt_id,
            "chapter_id": str(chapter_id),
            "step_id": str(step_id),
            "phase": str(phase),
            "provider_alias": str(provider_alias),
            "conservative_tokens": reserved,
            "state": "claimed",
            "reserved_at": now,
        }
        required_protocol: str | None = None
        if str(step_id).startswith("successor-acceptance-outline:"):
            required_protocol = "successor_acceptance_outline"
            from backend.evaluation.required_book_successor_acceptance_outline import (
                write_successor_acceptance_outline_claim,
            )

            result = await write_successor_acceptance_outline_claim(
                await self.get_job(job_id),
                chapter_id=str(chapter_id),
                step_id=str(step_id),
                phase=phase,
                provider_alias=provider_alias,
                conservative_tokens=reserved,
                query=query,
                update=update,
                write_job=self._collection_update_one,
            )
        elif str(step_id).startswith("required-initial-prose:"):
            required_protocol = "initial"
            from backend.db.required_initial_prose_journal import (
                write_required_initial_prose_claim,
            )

            result = await write_required_initial_prose_claim(
                await self.get_job(job_id),
                chapter_id=str(chapter_id),
                step_id=str(step_id),
                phase=phase,
                provider_alias=provider_alias,
                conservative_tokens=reserved,
                query=query,
                update=update,
                write_job=self._collection_update_one,
            )
        elif str(step_id).startswith("required-adherence:"):
            required_protocol = "review"
            from backend.db.required_adherence_journal import write_required_review_claim

            result = await write_required_review_claim(
                await self.get_job(job_id), chapter_id=str(chapter_id), step_id=str(step_id),
                phase=phase, provider_alias=provider_alias, conservative_tokens=reserved,
                query=query, update=update, write_job=self._collection_update_one,
            )
        elif str(step_id).startswith("required-prose-rewrite:"):
            required_protocol = "rewrite"
            from backend.db.required_prose_rewrite_journal import write_required_rewrite_claim

            result = await write_required_rewrite_claim(
                await self.get_job(job_id), chapter_id=str(chapter_id), step_id=str(step_id),
                phase=phase, provider_alias=provider_alias, conservative_tokens=reserved,
                query=query, update=update, write_job=self._collection_update_one,
            )
        elif str(step_id).startswith("required-state-candidate:"):
            required_protocol = "state_candidate"
            from backend.db.required_state_candidate_journal import (
                write_required_state_claim,
            )

            result = await write_required_state_claim(
                await self.get_job(job_id),
                chapter_id=str(chapter_id),
                step_id=str(step_id),
                phase=phase,
                provider_alias=provider_alias,
                conservative_tokens=reserved,
                query=query,
                update=update,
                write_job=self._collection_update_one,
            )
        else:
            # An opt-in successor Job cannot borrow the legacy generic pool.
            # Other successor phases have not been activated by this adapter.
            query["required_adherence_journal"] = None
            query["required_initial_prose_journal"] = None
            query["required_prose_rewrite_journal"] = None
            query["required_reviewed_candidate"] = None
            query["readiness.planning.required_initial_prose"] = {"$exists": False}
            query["readiness.planning.required_adherence_review"] = {"$exists": False}
            query["readiness.planning.required_prose_rewrite"] = {"$exists": False}
            query[
                "readiness.planning.required_chapter_review_authorization"
            ] = {"$exists": False}
            query[
                "readiness.planning.required_chapter_review_pipeline_revision"
            ] = {"$exists": False}
            query["required_state_candidate_journal"] = None
            query["required_state_candidate"] = None
            query[
                "readiness.planning.required_chapter_state_authorization"
            ] = {"$exists": False}
            query[
                "readiness.planning.required_chapter_state_pipeline_revision"
            ] = {"$exists": False}
            result = await self._collection_update_one(query, update)
        if result.modified_count == 1:
            return attempt_id

        job = await self.get_job(job_id)
        require_current_authorization(job)
        if required_protocol == "successor_acceptance_outline":
            from backend.evaluation.required_book_successor_acceptance_outline import (
                SuccessorAcceptanceOutlineDispatchRejected,
            )

            raise SuccessorAcceptanceOutlineDispatchRejected(
                "successor_acceptance_outline_dispatch_rejected"
            )
        if required_protocol == "initial":
            from backend.db.required_initial_prose_journal import (
                RequiredInitialProseDispatchRejected,
            )

            raise RequiredInitialProseDispatchRejected(
                "initial_prose_dispatch_rejected"
            )
        if required_protocol == "review":
            from backend.db.required_adherence_journal import (
                RequiredReviewDispatchRejected,
            )

            raise RequiredReviewDispatchRejected(
                "review_dispatch_rejected"
            )
        if required_protocol == "rewrite":
            from backend.db.required_prose_rewrite_journal import (
                RequiredRewriteDispatchRejected,
            )

            raise RequiredRewriteDispatchRejected(
                "rewrite_dispatch_rejected"
            )
        if required_protocol == "state_candidate":
            from backend.services.generation.required_chapter_state_job import (
                RequiredStateDispatchRejected,
            )

            raise RequiredStateDispatchRejected(
                "required_state_dispatch_rejected"
            )
        readiness = job.get("readiness")
        planning = readiness.get("planning") if isinstance(readiness, Mapping) else None
        if isinstance(planning, Mapping):
            from backend.services.generation.required_chapter_review_job import (
                RequiredChapterReviewJobConflict,
                required_chapter_review_planning_present,
            )

            if (
                required_chapter_review_planning_present(planning)
                or job.get("required_reviewed_candidate") is not None
            ):
                raise RequiredChapterReviewJobConflict(
                    "required_chapter_review_generic_dispatch_rejected"
                )
            from backend.services.generation.required_chapter_state_job import (
                RequiredChapterStateJobConflict,
                required_chapter_state_planning_present,
            )

            if (
                required_chapter_state_planning_present(planning)
                or job.get("required_state_candidate_journal") is not None
                or job.get("required_state_candidate") is not None
            ):
                raise RequiredChapterStateJobConflict(
                    "required_chapter_state_generic_dispatch_rejected"
                )
        if job.get("required_initial_prose_journal") is not None or (
            isinstance(planning, Mapping) and "required_initial_prose" in planning
        ):
            from backend.db.required_initial_prose_journal import (
                RequiredInitialProseDispatchRejected,
            )

            raise RequiredInitialProseDispatchRejected(
                "initial_prose_dispatch_rejected"
            )
        if job.get("required_adherence_journal") is not None or (
            isinstance(planning, Mapping) and "required_adherence_review" in planning
        ):
            from backend.db.required_adherence_journal import RequiredReviewDispatchRejected

            raise RequiredReviewDispatchRejected("review_dispatch_rejected")
        if interactive_execution_token is not None:
            execution_claim = job.get("interactive_execution_claim")
            active_token = (
                str(execution_claim.get("token") or "")
                if isinstance(execution_claim, Mapping)
                else ""
            )
            if (
                active_token != interactive_execution_token
                or str(job.get("status") or "") != "completion_running"
            ):
                raise AttemptFenceExpired(
                    "Interactive completion execution authority was replaced"
                )
        if fence is not None:
            active_fence = (job.get("attempt_reservation") or {}).get(
                "pre_dispatch_fence"
            )
            if active_fence != fence:
                raise AttemptFenceExpired(
                    "Provider attempt dispatch fence was replaced"
                )
        budget = job.get("token_budget")
        if budget is not None:
            used = int(job.get("tokens_used") or 0)
            already_reserved = int(job.get("tokens_reserved") or 0)
            if used + already_reserved + reserved > int(budget):
                raise TokenBudgetExceeded(
                    "Token budget would be exceeded before Provider dispatch"
                )
        if len(job.get("active_token_reservations") or []) >= (
            MAX_ACTIVE_TOKEN_RESERVATIONS
        ):
            raise AttemptCapacityExceeded("Active token reservation limit reached")
        raise AttemptCapacityExceeded(
            "Attempt capacity or chapter reservation is exhausted"
        )

    async def settle_attempt_budget(
        self,
        job_id: str,
        attempt_id: str,
        usage: TokenUsage,
        *,
        conservative_tokens: int | None,
    ) -> bool:
        """Release a reservation once; missing usage is charged conservatively."""
        if (
            type(conservative_tokens) is not int
            or conservative_tokens <= 0
            or conservative_tokens > MAX_PERSISTED_ATTEMPT_TOKENS
        ):
            raise TokenBudgetUnbounded(
                "Every Provider attempt requires a conservative token bound"
            )
        reserved = conservative_tokens
        observed = _trusted_usage_tokens(usage)
        charged = observed if observed > 0 else reserved
        accounted_usage = usage.model_copy(update={"total_tokens": charged})
        now = get_utc_now()
        summary = {
            "attempt_id": str(attempt_id),
            "usage": accounted_usage.model_dump(),
            "accounted_at": now,
            "charged_tokens": charged,
        }
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "state_dispatch_resolution": None,
                "usage_attempt_ids": {"$ne": str(attempt_id)},
                "attempt_slots": {
                    "$elemMatch": {
                        "attempt_id": str(attempt_id),
                        "state": {"$in": ["claimed", "uncertain"]},
                    }
                },
                "active_token_reservations": {
                    "$elemMatch": {
                        "attempt_id": str(attempt_id),
                        "conservative_tokens": reserved,
                    }
                },
            },
            {
                "$addToSet": {"usage_attempt_ids": str(attempt_id)},
                "$push": {
                    "usage_attempt_summaries": {
                        "$each": [summary],
                        "$slice": -USAGE_SUMMARY_LIMIT,
                    }
                },
                "$pull": {"active_token_reservations": {"attempt_id": str(attempt_id)}},
                "$inc": {
                    "tokens_used": charged,
                    "tokens_reserved": -reserved,
                },
                "$set": {
                    "attempt_slots.$[slot].state": "accounted",
                    "attempt_slots.$[slot].usage": accounted_usage.model_dump(),
                    "attempt_slots.$[slot].charged_tokens": charged,
                    "attempt_slots.$[slot].accounted_at": now,
                    "updated_at": now,
                },
            },
            array_filters=[{"slot.attempt_id": str(attempt_id)}],
        )
        return result.modified_count == 1

    async def mark_attempt_uncertain_with_budget(
        self,
        job_id: str,
        attempt_id: str,
        reason: str,
    ) -> bool:
        """Freeze the reservation when a dispatched Provider result is unknown."""
        now = get_utc_now()
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "state_dispatch_resolution": None,
                "attempt_slots": {
                    "$elemMatch": {
                        "attempt_id": str(attempt_id),
                        "state": "claimed",
                    }
                },
                "active_token_reservations": {
                    "$elemMatch": {"attempt_id": str(attempt_id)}
                },
            },
            {
                "$set": {
                    "attempt_slots.$[slot].state": "uncertain",
                    "attempt_slots.$[slot].uncertain_reason": str(reason),
                    "attempt_slots.$[slot].updated_at": now,
                    "active_token_reservations.$[reservation].state": "uncertain",
                    "active_token_reservations.$[reservation].uncertain_reason": str(reason),
                    "active_token_reservations.$[reservation].updated_at": now,
                    "has_uncertain_attempts": True,
                    "updated_at": now,
                },
                "$addToSet": {"uncertain_attempt_ids": str(attempt_id)},
            },
            array_filters=[
                {"slot.attempt_id": str(attempt_id)},
                {"reservation.attempt_id": str(attempt_id)},
            ],
        )
        return result.modified_count == 1

    async def release_attempt_budget(
        self,
        job_id: str,
        attempt_id: str,
        *,
        conservative_tokens: int | None,
        reason: str,
    ) -> bool:
        """Release only a proven pre-dispatch failure; uncertain calls stay frozen."""
        if conservative_tokens is None:
            return False
        reserved = max(1, int(conservative_tokens))
        now = get_utc_now()
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "state_dispatch_resolution": None,
                "attempt_slots": {
                    "$elemMatch": {
                        "attempt_id": str(attempt_id),
                        "state": "claimed",
                    }
                },
                "active_token_reservations": {
                    "$elemMatch": {
                        "attempt_id": str(attempt_id),
                        "conservative_tokens": reserved,
                    }
                },
            },
            {
                "$inc": {"tokens_reserved": -reserved},
                "$pull": {"active_token_reservations": {"attempt_id": str(attempt_id)}},
                "$set": {
                    "attempt_slots.$[slot].state": "released_pre_dispatch",
                    "attempt_slots.$[slot].release_reason": str(reason),
                    "attempt_slots.$[slot].updated_at": now,
                    "updated_at": now,
                },
            },
            array_filters=[{"slot.attempt_id": str(attempt_id)}],
        )
        return result.modified_count == 1

    async def discard_proven_pre_dispatch_attempt(
        self,
        job_id: str,
        chapter_id: str,
        step_id: str,
        attempt_id: str,
        *,
        current_pre_dispatch_fence: PreDispatchFenceV1 | None = None,
    ) -> bool:
        """Remove a cross-ledger orphan only after receipt fencing proves no dispatch."""
        current_fence: dict[str, Any] | None = None
        if current_pre_dispatch_fence is not None:
            validated_fence = _validated_pre_dispatch_fence(
                current_pre_dispatch_fence
            )
            if validated_fence.step_id != str(step_id):
                raise ValueError("pre-dispatch fence step does not match cleanup")
            current_fence = validated_fence.model_dump(mode="json")
        job = await self.get_job(job_id)
        slot = next(
            (
                item
                for item in list(job.get("attempt_slots") or [])
                if str(item.get("attempt_id") or "") == str(attempt_id)
                and str(item.get("chapter_id") or "") == str(chapter_id)
                and str(item.get("step_id") or "") == str(step_id)
                and item.get("state") in {
                    "claimed",
                    "released_pre_dispatch",
                }
            ),
            None,
        )
        if slot is None:
            return False
        if current_fence is not None:
            active_fence = (job.get("attempt_reservation") or {}).get(
                "pre_dispatch_fence"
            )
            if active_fence != current_fence:
                raise AttemptFenceExpired(
                    "Pre-dispatch cleanup fence was replaced"
                )
            slot_fence = slot.get("pre_dispatch_fence")
            if (
                slot.get("state") == "claimed"
                and slot_fence == current_fence
            ):
                return False
        state = str(slot["state"])
        bound = slot.get("conservative_tokens")
        reserved = (
            int(bound)
            if state == "claimed" and type(bound) is int and bound > 0
            else 0
        )
        query: dict[str, Any] = {
            "_id": to_object_id(job_id),
            "is_deleted": False,
            "usage_attempt_claimed": {"$gte": 1},
            "attempt_slots": {"$elemMatch": {
                "attempt_id": str(attempt_id),
                "chapter_id": str(chapter_id),
                "step_id": str(step_id),
                "state": state,
            }},
        }
        if current_fence is not None:
            query["attempt_reservation.pre_dispatch_fence"] = current_fence
        update: dict[str, Any] = {
            "$pull": {"attempt_slots": {"attempt_id": str(attempt_id)}},
            "$inc": {"usage_attempt_claimed": -1},
            "$set": {"updated_at": get_utc_now()},
        }
        reservation = job.get("attempt_reservation")
        if (
            isinstance(reservation, dict)
            and str(reservation.get("chapter_id") or "") == str(chapter_id)
            and int(reservation.get("claimed_slots") or 0) > 0
        ):
            query["attempt_reservation.chapter_id"] = str(chapter_id)
            query["attempt_reservation.claimed_slots"] = {"$gte": 1}
            update["$inc"]["attempt_reservation.claimed_slots"] = -1
        if reserved:
            query["tokens_reserved"] = {"$gte": reserved}
            query["active_token_reservations"] = {"$elemMatch": {
                "attempt_id": str(attempt_id),
                "conservative_tokens": reserved,
            }}
            update["$inc"]["tokens_reserved"] = -reserved
            update["$pull"]["active_token_reservations"] = {
                "attempt_id": str(attempt_id)
            }
        result = await self._collection_update_one(query, update)
        return result.modified_count == 1
generation_job_repo = GenerationJobRepository()
