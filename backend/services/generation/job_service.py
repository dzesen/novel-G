"""批量作业服务：CRUD、全局单作业守卫、拉起/控制进程内任务。"""
from __future__ import annotations

import asyncio
from contextvars import Context
import hashlib
import logging
import re
import secrets
import time
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Dict, List, Literal, Mapping, Optional, TypedDict

from pymongo.errors import DuplicateKeyError

from backend.db.mutation import MutationConflictError
from backend.db.repositories.generation_job_repository import (
    AttemptCapacityExceeded,
    TokenBudgetExceeded,
    generation_job_repo,
)
from backend.db.narrative_revision import (
    NarrativeRevisionConflict,
    narrative_revision_store,
)
from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.utils import get_utc_now, to_object_id
from backend.services.generation import job_planner
from backend.services.generation.chapter_pipeline import (
    ChapterOutcome,
    ChapterPipelineFailed,
    run_chapter,
)
from backend.services.generation.chapter_candidate_authorization import (
    authorized_candidate_repair_attempt_slots,
    candidate_job_generation_requirements,
    generation_plan_from_candidate_snapshot,
    plan_candidate_job_generation,
    readiness_uses_candidate_pipeline,
    validate_candidate_job_execution_authorization,
)
from backend.services.generation.required_chapter_review import (
    RequiredChapterReviewPlan,
)
from backend.services.generation.required_chapter_review_job import (
    REQUIRED_REVIEWED_CANDIDATE_PAUSE_REASON,
    RequiredChapterReviewJobRunner,
    prepare_required_chapter_review_readiness,
    readiness_uses_required_chapter_review,
    validate_required_chapter_review_readiness,
)
from backend.services.generation.required_chapter_state_job import (
    REQUIRED_STATE_CANDIDATE_PAUSE_REASON,
    RequiredChapterStateJobRunner,
    prepare_required_chapter_state_readiness,
    readiness_uses_required_chapter_state,
    validate_required_chapter_state_readiness,
)
from backend.services.generation.required_chapter_finalization_job import (
    REQUIRED_CHAPTER_FINALIZATION_JOB_TOKEN_BUDGET,
    RequiredChapterFinalizationJobRunner,
    prepare_required_chapter_finalization_readiness,
    readiness_uses_required_chapter_finalization,
    validate_required_chapter_finalization_readiness,
)
from backend.services.generation.required_book_successor import (
    REQUIRED_BOOK_SUCCESSOR_ACKNOWLEDGEMENT,
    REQUIRED_BOOK_SUCCESSOR_RECOVERY_CHECKPOINT_BEFORE_FIRST_CHILD,
    REQUIRED_BOOK_SUCCESSOR_RECOVERY_CHECKPOINT_NONE,
    RequiredBookSuccessorCoordinator,
    parse_required_book_successor_journal,
    parse_required_book_successor_recovery_checkpoint,
    prepare_required_book_successor_readiness,
    readiness_uses_required_book_successor,
    validate_required_book_successor_readiness,
)
from backend.services.generation.required_book_successor_job import (
    RequiredBookSuccessorJobDeps,
    RequiredBookSuccessorJobRunner,
)
from backend.services.generation.chapter_candidate_job import (
    CandidateJobExecution,
    ChapterCandidateCompletionFailureRequest,
    ChapterCandidateJobRunner,
    ChapterCandidateJobRunnerDeps,
)
from backend.services.generation.candidate_repair_contracts import (
    JobMutationRecoveryBindingV1,
    JobMutationReceiptV1,
    StateDispatchResolutionV3,
)
from backend.services.generation.candidate_manual_takeover import (
    CandidateManualTakeoverResolutionV1,
    parse_candidate_manual_takeover,
    validate_candidate_manual_takeover_binding,
)
from backend.services.generation.job_execution import (
    JOB_EXECUTION_LEASE_SECONDS,
    JobExecutionLeaseLost,
    JobExecutionLeaseUnavailable,
    JobExecutionLeaseV1,
    bind_job_execution,
)
from backend.services.generation.job_authorization_contracts import (
    OutlineAuthorizationRecalculationCommandV1,
    build_batch_generation_authorization_contract,
    parse_batch_generation_authorization_contract,
    parse_prose_authorization,
    prose_authorization_digest,
    prose_authorization_scope,
)
from backend.services.generation.chapter_candidate_repairs import (
    ChapterCandidateRepairApplication,
)
from backend.services.generation.chapter_finalization import (
    ChapterFinalizationAuthorization,
    ChapterFinalizationEvidence,
    chapter_finalization_service,
    parse_chapter_finalization_authorization,
)
from backend.services.generation.discarded_candidate_recovery import (
    resolve_discarded_candidate_attempt_ids,
)
from backend.services.generation.attempt_scope import (
    JobAttemptScope,
    project_persisted_attempt_evidence,
)
from backend.services.generation.headless_generation import (
    build_chapter_pipeline_deps,
    estimate_chapter_attempt_slots,
    estimate_worklist_attempt_capacity,
    generate_outline,
    generate_prose_candidate,
    generate_state_candidate,
    review_prose_candidate,
)
from backend.services.llm.generation_runtime import GenerationPlan
from backend.services.generation.failure_diagnostics import (
    build_failure_diagnostic,
    summarize_jobs,
)
from backend.services.generation.job_engine import (
    JobControl,
    JobEngineDeps,
    _REGISTRY,
    finalize_book_job,
    run_job,
)
from backend.services.generation.outline_adherence import (
    PAUSE_FOR_REWRITE,
    validate_outline_deviation_policy,
)
from backend.services.novel.chapter_service import ChapterService
from backend.services.novel.book_completion import (
    BookCompletionReport,
    book_completion_audit,
)
from backend.db.repositories.volume_repository import volume_repo
from backend.db.repositories.novel_repository import novel_repo
from backend.services.generation.book_worklist import get_book_worklist
from backend.services.generation.book_structure_initialization import (
    initialize_book_structure as execute_book_structure_initialization,
    inspect_book_structure_initialization,
)
from backend.services.generation.readiness import generation_readiness_module
from backend.services.novel.state_completion import (
    chapter_content_digest,
    state_completion_module,
)
from backend.services.novel.state_proposal import StaleStatePreview, state_proposal_module
from backend.services.novel.emergent_reference_card_candidates import (
    emergent_reference_card_candidate_module,
)
from backend.services.generation.prose_continuation import (
    ProseContinuationPolicy,
    authorization_ruleset_requires_refresh,
)
from backend.services.generation.protected_generation_params import (
    validate_protected_generation_params,
)
from backend.services.generation.reference_card_auto_creation import (
    ReferenceCardAutoCreationPolicy,
    auto_reference_card_creation_service,
)
from backend.services.generation.reference_card_dependency_repair import (
    parse_reference_card_repair_plan_authorization,
    reference_card_denials_are_repairable,
    reference_card_dependency_repair_service,
)

logger = logging.getLogger(__name__)
_START_LOCK: asyncio.Lock | None = None
_START_LOCK_LOOP: asyncio.AbstractEventLoop | None = None
_READINESS_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SUCCESSOR_REQUIRED_MESSAGE = (
    "该历史作业缺少当前受保护的预算或 readiness 授权；"
    "请终止该作业并以新 readiness 启动 successor"
)


def _get_start_lock() -> asyncio.Lock:
    """Return one start/resume lock per event loop (tests use multiple loops)."""
    global _START_LOCK, _START_LOCK_LOOP
    loop = asyncio.get_running_loop()
    if _START_LOCK is None or _START_LOCK_LOOP is not loop:
        _START_LOCK = asyncio.Lock()
        _START_LOCK_LOOP = loop
    return _START_LOCK


def _persisted_reference_card_auto_creation_policy(
    job: Mapping[str, Any],
) -> ReferenceCardAutoCreationPolicy:
    readiness = job.get("readiness")
    planning = readiness.get("planning") if isinstance(readiness, Mapping) else None
    raw_policy = (
        planning.get("reference_card_auto_creation_policy")
        if isinstance(planning, Mapping)
        else None
    )
    return ReferenceCardAutoCreationPolicy.from_mapping(raw_policy)


class ConflictError(Exception):
    """已有在跑作业（全局单作业约束）。路由映射为 409。"""


class ReferenceCardReviewResumeOutcome(TypedDict):
    """Safe post-commit projection for automatic batch resume."""

    resumed_job_ids: List[str]
    status: Literal["resumed", "not_resumed", "deferred"]
    reason_codes: List[str]


class _ReferenceCardReviewSourceChanged(ValueError):
    diagnostic_category = "source_changed"
    diagnostic_evidence = "confirmed"

    def __init__(self, reason_code: str) -> None:
        if reason_code not in {
            "narrative_revision_changed",
            "authorization_invalid_or_missing",
        }:
            raise ValueError("Unsupported reference-card review source change")
        self.diagnostic_code = reason_code
        super().__init__("Reference-card review requires a successor Job")


async def _pause_reference_card_review_for_source_change(
    job_id: str,
    target: Mapping[str, Any],
    *,
    reason_codes: list[str],
) -> bool:
    execution_epoch = target.get("execution_epoch", 0)
    failure_event_id = target.get("current_failure_event_id")
    if type(execution_epoch) is not int or execution_epoch < 0:
        raise ValueError("Generation job execution epoch is invalid")
    if failure_event_id is not None and not isinstance(failure_event_id, str):
        raise ValueError("Generation job failure event pointer is invalid")
    diagnostic = build_failure_diagnostic(
        _ReferenceCardReviewSourceChanged(reason_codes[0]),
        step="source_changed",
        chapter_id=str(target.get("current_chapter_id") or ""),
        occurred_at=get_utc_now(),
    )
    return await generation_job_repo.pause_for_source_change(
        job_id,
        diagnostic=diagnostic,
        error={
            "step": "source_changed",
            "reason_codes": list(reason_codes),
        },
        expected_status=str(target.get("status") or ""),
        expected_pause_reason=(
            str(target.get("pause_reason"))
            if target.get("pause_reason") is not None
            else None
        ),
        expected_execution_epoch=execution_epoch,
        expected_failure_event_id=failure_event_id,
    )


def _validate_start_authorization(
    *,
    token_budget: int | None,
    readiness_digest: str | None,
    generation_params: Mapping[str, Any] | None,
) -> dict[str, Any]:
    params = validate_protected_generation_params(generation_params)
    if (
        isinstance(token_budget, bool)
        or not isinstance(token_budget, int)
        or token_budget <= 0
        or not isinstance(readiness_digest, str)
        or _READINESS_DIGEST_PATTERN.fullmatch(readiness_digest) is None
    ):
        raise ValueError(
            "批量生成必须先确认正整数 token 预算与当前 64 位 readiness digest"
        )
    return params


def _validate_resumable_job_authorization(
    job: Mapping[str, Any],
) -> dict[str, Any]:
    params = validate_protected_generation_params(
        job.get("generation_params")
    )
    token_budget = job.get("token_budget")
    readiness = job.get("readiness")
    if (
        isinstance(token_budget, bool)
        or not isinstance(token_budget, int)
        or token_budget <= 0
        or not isinstance(readiness, Mapping)
    ):
        raise ValueError(_SUCCESSOR_REQUIRED_MESSAGE)
    digest = readiness.get("digest")
    if (
        not isinstance(digest, str)
        or _READINESS_DIGEST_PATTERN.fullmatch(digest) is None
    ):
        raise ValueError(_SUCCESSOR_REQUIRED_MESSAGE)
    try:
        contract = parse_batch_generation_authorization_contract(
            job.get("batch_authorization_contract")
        )
        if (
            contract.token_budget != token_budget
            or contract.readiness_digest != digest
        ):
            raise ValueError("batch authorization contract changed")
        book_successor_readiness = readiness_uses_required_book_successor(
            readiness
        )
        finalization_successor_readiness = (
            False
            if book_successor_readiness
            else readiness_uses_required_chapter_finalization(readiness)
        )
        state_successor_readiness = (
            False
            if book_successor_readiness
            or finalization_successor_readiness
            else readiness_uses_required_chapter_state(readiness)
        )
        successor_readiness = (
            False
            if book_successor_readiness
            or finalization_successor_readiness
            or state_successor_readiness
            else readiness_uses_required_chapter_review(readiness)
        )
        candidate_readiness = (
            False
            if book_successor_readiness
            or finalization_successor_readiness
            or successor_readiness
            or state_successor_readiness
            else readiness_uses_candidate_pipeline(readiness)
        )
        if not (
            book_successor_readiness
            or
            candidate_readiness
            or finalization_successor_readiness
            or successor_readiness
            or state_successor_readiness
        ):
            raise ValueError("legacy execution protocol")
        if book_successor_readiness:
            book_successor = validate_required_book_successor_readiness(
                readiness
            )
            raw_book_journal = job.get("required_book_successor_journal")
            expected_root_revision = (
                book_successor.base_narrative_revision
            )
            if raw_book_journal is not None:
                book_journal = parse_required_book_successor_journal(
                    raw_book_journal
                )
                coordinator = RequiredBookSuccessorCoordinator(
                    coordinator_job_id=str(job.get("_id") or ""),
                    readiness=readiness,
                )
                coordinator.next_action(book_journal)
                expected_root_revision = (
                    book_journal.expected_narrative_revision
                )
            if (
                job.get("authorization_revision")
                != book_successor.authorization_revision
                or job.get("expected_narrative_revision")
                != expected_root_revision
                or str(job.get("owner_id") or "")
                != book_successor.owner_id
            ):
                raise ValueError("required book successor Job authority changed")
        if finalization_successor_readiness:
            finalization_successor = (
                validate_required_chapter_finalization_readiness(readiness)
            )
            if (
                job.get("authorization_revision")
                != finalization_successor.authorization_revision
                or job.get("expected_narrative_revision")
                != finalization_successor.narrative_revision
                or str(job.get("owner_id") or "")
                != finalization_successor.owner_id
            ):
                raise ValueError(
                    "required finalization Job authority changed"
                )
        if state_successor_readiness:
            state_successor = validate_required_chapter_state_readiness(
                readiness
            )
            if (
                job.get("authorization_revision")
                != state_successor.authorization_revision
                or job.get("expected_narrative_revision")
                != state_successor.narrative_revision
                or str(job.get("owner_id") or "")
                != state_successor.owner_id
            ):
                raise ValueError("required state Job authority changed")
        if successor_readiness:
            successor = validate_required_chapter_review_readiness(
                readiness
            )
            if (
                job.get("authorization_revision")
                != successor.authorization_revision
                or job.get("expected_narrative_revision")
                != successor.narrative_revision
                or str(job.get("owner_id") or "") != successor.owner_id
            ):
                raise ValueError("required review Job authority changed")
        expected_volume_id = (
            str(job.get("volume_id"))
            if job.get("volume_id") is not None
            else None
        )
        if (
            readiness.get("novel_id") != str(job.get("novel_id") or "")
            or readiness.get("scope") != str(job.get("scope") or "")
            or readiness.get("volume_id") != expected_volume_id
        ):
            raise ValueError("readiness scope changed")
        acknowledged = readiness.get("acknowledged_warning_codes") or []
        if not isinstance(acknowledged, list):
            raise ValueError("readiness acknowledgements are invalid")
        generation_readiness_module.authorize(
            dict(readiness),
            supplied_digest=digest,
            acknowledged_warning_codes=acknowledged,
        )
    except ValueError as exc:
        raise ValueError(_SUCCESSOR_REQUIRED_MESSAGE) from exc
    return params


def _reject_external_required_book_child_control(
    job: Mapping[str, Any],
) -> None:
    if job.get("required_book_successor_parent_job_id") is not None:
        raise ValueError(
            "整本 successor 的内部子作业只能由其根作业恢复或控制"
        )


async def _recover_job_mutation_revision(
    binding: JobMutationRecoveryBindingV1,
) -> int | None:
    # Lazy import keeps the recovery registry from forming a service import cycle.
    from backend.services.novel.mutation_recovery import (
        recover_bound_mutation_revision,
    )

    return await recover_bound_mutation_revision(binding)


async def _candidate_finalization_recovery_available(
    job_id: str,
    job: Mapping[str, Any],
) -> bool:
    """Read-only proof that the frozen candidate finalization can resume."""

    chapter_id = job.get("current_chapter_id")
    expected_revision = job.get("expected_narrative_revision")
    readiness = job.get("readiness")
    planning = readiness.get("planning") if isinstance(readiness, Mapping) else None
    readiness_digest = (
        readiness.get("digest") if isinstance(readiness, Mapping) else None
    )
    if (
        not isinstance(chapter_id, str)
        or not chapter_id
        or type(expected_revision) is not int
        or expected_revision < 0
        or not isinstance(readiness_digest, str)
        or not readiness_digest
    ):
        return False
    try:
        finalization = parse_chapter_finalization_authorization(
            planning.get("chapter_finalization_authorization")
            if isinstance(planning, Mapping)
            else None
        )
    except ValueError:
        return False

    # Discovery stays read-only until transition_job_resume atomically reacquires
    # the global execution slot.  The candidate runner performs the actual
    # frozen-journal recovery after that transition.
    from backend.services.novel.mutation_recovery import (
        find_job_bound_finalization_recovery_binding,
    )

    binding = await find_job_bound_finalization_recovery_binding(
        novel_id=str(job.get("novel_id") or ""),
        job_id=str(job_id),
        chapter_id=chapter_id,
        readiness_digest=readiness_digest,
        authorization_revision=int(finalization["authorization_revision"]),
        expected_narrative_revision=expected_revision,
    )
    return binding is not None


async def _with_recovery_capabilities(
    job_id: str,
    job: Mapping[str, Any],
) -> Dict[str, Any]:
    """Project read-only recovery actions without mutating the stored Job."""

    presented = dict(job)
    available = False
    if (
        job.get("status") in {"paused", "interrupted", "failed"}
        and job.get("pause_reason") == "source_changed"
        and bool(job.get("candidate_pipeline_checkpoints"))
    ):
        try:
            available = await _candidate_finalization_recovery_available(
                job_id,
                job,
            )
        except (MutationConflictError, ValueError):
            available = False
    presented["resume_original_writeback_available"] = available
    return presented


def _state_only_job_mutation_binding(
    *,
    job_id: str,
    job: Mapping[str, Any],
    chapter_id: str,
    expected_revision: int,
) -> JobMutationRecoveryBindingV1:
    readiness = job.get("readiness")
    if not isinstance(readiness, Mapping):
        raise ValueError("state-only Job readiness is invalid")
    planning = readiness.get("planning")
    finalization = parse_chapter_finalization_authorization(
        planning.get("chapter_finalization_authorization")
        if isinstance(planning, Mapping)
        else None
    )
    novel_id = str(job.get("novel_id") or "")
    digest = readiness.get("digest")
    if not isinstance(digest, str) or not digest:
        raise ValueError("state-only Job readiness digest is invalid")
    return JobMutationRecoveryBindingV1(
        schema_version="job_mutation_recovery_binding.v1",
        novel_id=novel_id,
        job_id=str(job_id),
        chapter_id=str(chapter_id),
        readiness_digest=digest,
        authorization_revision=int(finalization["authorization_revision"]),
        expected_narrative_revision=expected_revision,
        operation="accept_chapter_state",
        idempotency_key=f"candidate-job-state:{job_id}:{chapter_id}",
    )


def _validated_state_dispatch_binding(
    *,
    job_id: str,
    job: Mapping[str, Any],
    value: Any,
    require_current_chapter: bool = True,
) -> JobMutationRecoveryBindingV1:
    """Rebuild the exact current Job authority before touching a proposal receipt."""

    if (
        require_current_chapter
        and not readiness_uses_candidate_pipeline(job.get("readiness"))
    ):
        raise ValueError(
            "Generation job state dispatch requires current candidate authority"
        )

    try:
        binding = JobMutationRecoveryBindingV1.model_validate(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "Generation job mutation recovery binding is invalid"
        ) from exc
    if binding.job_id != str(job_id):
        raise ValueError(
            "Generation job mutation recovery binding belongs to another Job"
        )
    chapter_id = job.get("current_chapter_id")
    if require_current_chapter and str(chapter_id or "") != binding.chapter_id:
        raise ValueError(
            "Generation job mutation recovery chapter diverged"
        )
    expected_revision = job.get("expected_narrative_revision")
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError(
            "Generation job narrative revision cursor is invalid"
        )
    expected = _state_only_job_mutation_binding(
        job_id=str(job_id),
        job=job,
        chapter_id=binding.chapter_id,
        expected_revision=expected_revision,
    )
    if binding != expected:
        raise ValueError(
            "Generation job mutation recovery binding diverged from current authority"
        )
    return binding


def _persisted_state_dispatch_resolution(
    *,
    job_id: str,
    job: Mapping[str, Any],
) -> StateDispatchResolutionV3 | None:
    raw = job.get("state_dispatch_resolution")
    if raw is None:
        return None
    try:
        resolution = StateDispatchResolutionV3.model_validate(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "Generation job state dispatch resolution is invalid"
        ) from exc
    terminal = resolution.phase == "terminal"
    binding = _validated_state_dispatch_binding(
        job_id=job_id,
        job=job,
        value=resolution.binding,
        require_current_chapter=not terminal,
    )
    if resolution.binding != binding:
        raise ValueError(
            "Generation job state dispatch resolution binding diverged"
        )
    if terminal:
        expected_status = {"skip": "failed", "abort": "aborted"}.get(
            resolution.action
        )
        if (
            expected_status is None
            or job.get("status") != expected_status
            or job.get("current_chapter_id") is not None
            or job.get("job_mutation_recovery") is not None
            or job.get("has_uncertain_attempts") is not False
        ):
            raise ValueError(
                "Generation job terminal state dispatch receipt diverged"
            )
    return resolution


async def _advance_state_dispatch_resolution(
    *,
    job_id: str,
    binding: JobMutationRecoveryBindingV1,
    action: str,
    last_checkpoint_index: int | None = None,
) -> StateDispatchResolutionV3:
    """Drive one explicit action through both ledgers using a durable Job phase."""

    job = await generation_job_repo.get_job(job_id)
    action_frozen_on_proposal = job.get("state_dispatch_resolution") is None
    if action_frozen_on_proposal:
        acknowledged = await state_proposal_module.acknowledge_job_bound_dispatch(
            binding,
            action,
        )
        if not acknowledged and action != "abort":
            raise ValueError(
                "Generation job state dispatch receipt disappeared before resolution"
            )
    resolution = await generation_job_repo.begin_state_dispatch_resolution(
        job_id,
        binding,
        action,
    )
    if resolution.phase == "intent":
        if not action_frozen_on_proposal:
            acknowledged = (
                await state_proposal_module.acknowledge_job_bound_dispatch(
                    binding,
                    action,
                )
            )
            if not acknowledged and action != "abort":
                raise ValueError(
                    "Generation job state dispatch receipt disappeared before resolution"
                )
        resolution = await generation_job_repo.advance_state_dispatch_resolution(
            job_id,
            resolution,
            "proposal_acknowledged",
        )
    if resolution.phase == "proposal_acknowledged":
        resolution = await generation_job_repo.acknowledge_state_dispatch_attempts(
            job_id,
            resolution,
        )
    if resolution.phase == "attempts_acknowledged":
        # False is the idempotent crash-after-release case: no worker can have
        # reused the key while the durable Job action is still non-terminal.
        await state_proposal_module.release_job_bound_dispatch(binding, action)
        resolution = await generation_job_repo.advance_state_dispatch_resolution(
            job_id,
            resolution,
            "proposal_released",
        )
    if action == "retry":
        if resolution.phase == "proposal_released":
            if last_checkpoint_index is None:
                raise ValueError(
                    "Generation job retry checkpoint cursor is missing"
                )
            resolution = await generation_job_repo.transition_state_dispatch_retry(
                job_id,
                resolution,
                last_checkpoint_index=last_checkpoint_index,
            )
        if resolution.phase != "job_transitioned":
            raise ValueError("Generation job retry resolution is incomplete")
        return resolution
    if resolution.phase == "proposal_released":
        return await generation_job_repo.complete_state_dispatch_terminal(
            job_id,
            resolution,
        )
    if resolution.phase == "terminal":
        return resolution
    raise ValueError("Generation job terminal resolution is incomplete")


def _recovered_state_only_outcome(
    chapter: Mapping[str, Any],
    receipt: JobMutationReceiptV1,
    attempts: List[Mapping[str, Any]],
) -> ChapterOutcome:
    try:
        projected_attempts, tokens = project_persisted_attempt_evidence(
            attempts,
            maximum_entries=2_040,
        )
    except ValueError as exc:
        raise StaleStatePreview(
            "Generation job state attempt ledger is invalid"
        ) from exc
    return ChapterOutcome(
        chapter_id=str(chapter.get("_id") or ""),
        order_index=int(chapter.get("order_index") or 0),
        steps_done=["state"],
        steps_skipped=["outline", "prose", "outline_adherence"],
        tokens=tokens,
        attempts=projected_attempts,
        summary_written=True,
        mutation_receipts=[receipt],
    )


class ResumeReadinessRequired(ValueError):
    """The paused job needs a fresh, user-confirmed authorization preview."""


def _estimate_authorized_chapter_attempt_slots(
    job: Mapping[str, Any],
    chapter: Mapping[str, Any],
    generation_params: Mapping[str, Any] | None,
) -> int:
    """Size one chapter reservation from the already-confirmed readiness."""
    base_slots = estimate_chapter_attempt_slots(
        dict(chapter),
        generation_params,
    )
    readiness = job.get("readiness")
    if not readiness_uses_candidate_pipeline(readiness):
        return base_slots
    assert isinstance(readiness, Mapping)
    repair_slots = authorized_candidate_repair_attempt_slots(
        readiness,
        chapter_id=str(chapter.get("_id") or ""),
        generation_params=generation_params,
    )
    return base_slots + repair_slots


def _new_job_doc(
    novel_id, scope, volume_id, checkpoint_interval, token_budget, attempt_capacity,
    readiness, outline_deviation_policy, generation_params,
    confirmed_readiness_digest,
) -> Dict[str, Any]:
    protected_generation_params = validate_protected_generation_params(
        generation_params
    )
    planning = readiness.get("planning")
    book_successor_readiness = readiness_uses_required_book_successor(readiness)
    finalization_successor_readiness = (
        False
        if book_successor_readiness
        else readiness_uses_required_chapter_finalization(readiness)
    )
    state_successor_readiness = (
        False
        if book_successor_readiness
        or finalization_successor_readiness
        else readiness_uses_required_chapter_state(readiness)
    )
    review_successor_readiness = (
        False
        if book_successor_readiness
        or finalization_successor_readiness
        or state_successor_readiness
        else readiness_uses_required_chapter_review(readiness)
    )
    finalization_successor_authorization = (
        validate_required_chapter_finalization_readiness(readiness)
        if finalization_successor_readiness
        else None
    )
    book_successor_authorization = (
        validate_required_book_successor_readiness(readiness)
        if book_successor_readiness
        else None
    )
    review_successor_authorization = (
        validate_required_chapter_review_readiness(readiness)
        if review_successor_readiness
        else None
    )
    state_successor_authorization = (
        validate_required_chapter_state_readiness(readiness)
        if state_successor_readiness
        else None
    )
    successor_authorization = (
        book_successor_authorization
        or finalization_successor_authorization
        or state_successor_authorization
        or review_successor_authorization
    )
    candidate_readiness = (
        book_successor_readiness
        or finalization_successor_readiness
        or state_successor_readiness
        or review_successor_readiness
        or (
            isinstance(planning, Mapping)
            and "chapter_candidate_pipeline_revision" in planning
        )
    )
    resources = readiness.get("resources")
    expected_revision = (
        resources.get("narrative_revision")
        if isinstance(resources, Mapping)
        else None
    )
    if (
        candidate_readiness
        and (type(expected_revision) is not int or expected_revision < 0)
    ):
        raise ValueError("Job readiness narrative revision is invalid")
    if successor_authorization is not None and (
        token_budget != successor_authorization.token_budget
        or int(attempt_capacity)
        != successor_authorization.maximum_provider_attempts_total
    ):
        raise ValueError("Required successor Job budget diverged from readiness")
    readiness_digest = readiness.get("digest")
    if readiness_digest != confirmed_readiness_digest:
        raise ValueError("Job readiness digest diverged from explicit confirmation")
    authorization_contract = build_batch_generation_authorization_contract(
        readiness_digest=confirmed_readiness_digest,
        token_budget=token_budget,
    )
    document = {
        "novel_id": to_object_id(novel_id), "scope": scope,
        "volume_id": to_object_id(volume_id) if volume_id else None,
        "status": "running", "pause_reason": None,
        "checkpoint_interval": (
            int(checkpoint_interval)
            if checkpoint_interval is not None
            else None
        ),
        "token_budget": token_budget,
        "tokens_used": 0, "current_chapter_id": None, "progress": [],
        "tokens_reserved": 0,
        "active_token_reservations": [],
        "last_checkpoint_index": 0, "error": None,
        "active_slot": "global",
        "usage_attempt_capacity": int(attempt_capacity),
        "usage_attempt_claimed": 0,
        "usage_attempt_ids": [],
        "usage_attempt_summaries": [],
        "attempt_slots": [],
        "attempt_reservation": None,
        "candidate_manual_takeover": None,
        "candidate_manual_takeover_events": [],
        "candidate_pipeline_checkpoints": [],
        "required_initial_prose_journal": None,
        "required_prose_rewrite_journal": None,
        "required_adherence_journal": None,
        "required_reviewed_candidate": None,
        "required_state_candidate_journal": None,
        "required_state_candidate": None,
        "required_chapter_finalization_result": None,
        "required_book_successor_journal": None,
        "required_book_successor_recovery_checkpoint": None,
        "required_book_successor_action": None,
        "required_book_successor_parent_job_id": None,
        "chapter_completion_decisions": [],
        "reference_card_auto_creation_events": [],
        "reference_card_repair_events": [],
        "state_dispatch_resolution": None,
        "execution_epoch": 0,
        "execution_lease": None,
        "authorization_confirmation_required": None,
        "uncertain_attempt_ids": [],
        "has_uncertain_attempts": False,
        "confirm_uncertain_prose_retry": False,
        "diagnostic_schema_version": 1,
        "diagnostics": [],
        "current_failure_event_id": None,
        "outline_deviation_policy": validate_outline_deviation_policy(
            outline_deviation_policy
        ),
        # 请求级参数是作业快照的一部分。暂停/恢复只重读这份快照，不会被
        # 后续页面操作覆盖；未设置的键仍由每次调用时选中的 Provider 默认值兜底。
        "generation_params": protected_generation_params,
        "batch_authorization_contract": authorization_contract,
        "prose_continuation_authorization": dict(
            ((readiness.get("planning") or {}).get(
                "prose_continuation_authorization"
            ) or {})
        ),
        "authorization_revision": (
            successor_authorization.authorization_revision
            if successor_authorization is not None
            else int(((readiness.get("planning") or {}).get(
                "prose_continuation_authorization"
            ) or {}).get("authorization_revision") or 0)
        ),
        "readiness": readiness,
    }
    if successor_authorization is not None:
        document["owner_id"] = to_object_id(successor_authorization.owner_id)
    if type(expected_revision) is int and expected_revision >= 0:
        document["expected_narrative_revision"] = expected_revision
    return document


def _stable_required_book_successor_replay_state(
    job: Mapping[str, Any],
    *,
    expected_job: Mapping[str, Any],
) -> Literal["checkpoint_reached", "startable"]:
    """Validate one deterministic acceptance root after confirmation loss.

    A persisted checkpoint is the durable completion receipt for ``start``.
    Without it, the root may be restarted only while no child or paid evidence
    exists.  This keeps an idempotent host retry from creating or advancing a
    second root execution.
    """

    stable_job_id = str(expected_job.get("_id") or "")
    if re.fullmatch(r"[0-9a-f]{24}", stable_job_id) is None:
        raise ValueError("required book successor stable Job id is invalid")
    try:
        _validate_resumable_job_authorization(job)
        authority = validate_required_book_successor_readiness(
            job.get("readiness") or {}
        )
        expected_authority = validate_required_book_successor_readiness(
            expected_job.get("readiness") or {}
        )
    except ValueError as exc:
        raise ValueError(
            "required book successor stable Job authority changed"
        ) from exc
    immutable_fields = (
        "scope",
        "volume_id",
        "token_budget",
        "usage_attempt_capacity",
        "generation_params",
        "batch_authorization_contract",
        "authorization_revision",
        "outline_deviation_policy",
        "required_book_successor_parent_job_id",
    )
    if (
        str(job.get("_id") or "") != stable_job_id
        or str(job.get("novel_id") or "")
        != str(expected_job.get("novel_id") or "")
        or str(job.get("owner_id") or "")
        != str(expected_job.get("owner_id") or "")
        or job.get("is_deleted") is not False
        or any(job.get(field) != expected_job.get(field) for field in immutable_fields)
        or authority != expected_authority
        or (job.get("readiness") or {}).get("digest")
        != (expected_job.get("readiness") or {}).get("digest")
        or list((job.get("readiness") or {}).get(
            "acknowledged_warning_codes"
        ) or [])
        != list((expected_job.get("readiness") or {}).get(
            "acknowledged_warning_codes"
        ) or [])
    ):
        raise ValueError(
            "required book successor stable Job authority changed"
        )
    coordinator = RequiredBookSuccessorCoordinator(
        coordinator_job_id=stable_job_id,
        readiness=job["readiness"],
    )
    initial = coordinator.initial_journal()
    raw_checkpoint = job.get(
        "required_book_successor_recovery_checkpoint"
    )
    if raw_checkpoint is not None:
        checkpoint = parse_required_book_successor_recovery_checkpoint(
            raw_checkpoint
        )
        if (
            checkpoint.coordinator_job_id != stable_job_id
            or checkpoint.coordinator_readiness_digest
            != initial.coordinator_readiness_digest
            or checkpoint.journal_digest != initial.journal_digest
        ):
            raise ValueError(
                "required book successor stable checkpoint changed"
            )
        return "checkpoint_reached"

    raw_journal = job.get("required_book_successor_journal")
    if raw_journal is not None:
        journal = parse_required_book_successor_journal(raw_journal)
        if journal != initial:
            raise ValueError(
                "required book successor advanced without its checkpoint"
            )
    execution_epoch = job.get("execution_epoch")
    if (
        str(job.get("status") or "")
        not in {"running", "interrupted", "failed"}
        or type(execution_epoch) is not int
        or execution_epoch < 0
        or job.get("current_chapter_id") is not None
        or job.get("required_book_successor_action") is not None
        or list(job.get("progress") or [])
        or list(job.get("attempt_slots") or [])
        or int(job.get("usage_attempt_claimed") or 0) != 0
        or int(job.get("tokens_used") or 0) != 0
        or job.get("has_uncertain_attempts") is not False
    ):
        raise ValueError(
            "required book successor advanced without its checkpoint"
        )
    return "startable"


class GenerationJobService:
    @staticmethod
    async def _guard_no_running() -> None:
        running = await generation_job_repo.list_running_jobs()
        if not job_planner.can_start_new(len(running)):
            raise ConflictError("已有正在运行的批量作业，请先暂停或等待其结束")

    @staticmethod
    async def _resolve_reference_card_blockers(
        job_id: str,
    ) -> dict[str, Any] | None:
        """Apply only readiness-bound unique creates, then return live blockers.

        A disabled or legacy job follows the original manual-review path.  An
        enabled job may consume one standalone narrative revision even when the
        mutation Gate denies the batch, so the Job cursor and audit event are
        persisted before the engine is allowed to pause.
        """

        job = await generation_job_repo.get_job(job_id)
        protected_generation_params = _validate_resumable_job_authorization(job)
        novel_id = str(job.get("novel_id") or "")
        policy = _persisted_reference_card_auto_creation_policy(job)
        if not policy.enabled:
            return await (
                emergent_reference_card_candidate_module.blocking_summary(
                    novel_id
                )
            )

        readiness = job.get("readiness")
        planning = readiness.get("planning") if isinstance(readiness, Mapping) else None
        resources = (
            readiness.get("resources") if isinstance(readiness, Mapping) else None
        )
        authorization = (
            planning.get("reference_card_creation_authorization")
            if isinstance(planning, Mapping)
            else None
        )
        current_chapter_id = str(job.get("current_chapter_id") or "")
        readiness_digest = (
            str(readiness.get("digest") or "")
            if isinstance(readiness, Mapping)
            else ""
        )
        owner_id = (
            str(resources.get("owner_id") or "")
            if isinstance(resources, Mapping)
            else ""
        )
        authorization_revision = job.get("authorization_revision")
        expected_revision = job.get("expected_narrative_revision")
        authority_is_valid = not (
            not isinstance(authorization, Mapping)
            or not current_chapter_id
            or len(readiness_digest) != 64
            or not owner_id
            or type(authorization_revision) is not int
            or authorization_revision < 1
            or type(expected_revision) is not int
            or expected_revision < 0
        )

        def build_repair_event(
            repair: Mapping[str, Any],
            cycle: int,
        ) -> dict[str, Any]:
            repair_status = str(repair.get("status") or "")
            candidate_ids = [
                str(candidate_id)
                for candidate_id in list(
                    repair.get("created_reference_card_candidate_ids") or []
                )
            ]
            return {
                "schema_version": "reference_card_repair_event.v1",
                "event_id": (
                    f"{str((authorization or {}).get('authorization_digest') or '')}:"
                    f"{current_chapter_id}:{cycle}"
                ),
                "chapter_id": current_chapter_id,
                "actor_owner_id": owner_id,
                "authorization_digest": str(
                    (authorization or {}).get("authorization_digest") or ""
                ),
                "readiness_digest": readiness_digest,
                "authorization_revision": authorization_revision,
                "policy_revision": (authorization or {}).get(
                    "policy_revision"
                ),
                "cycle": cycle,
                "outcome": repair_status,
                "resolution": (
                    "dependency_removed"
                    if repair_status == "applied" and not candidate_ids
                    else None
                ),
                "created_reference_card_candidate_ids": candidate_ids,
                "reason": str(repair.get("reason") or ""),
                "proposal_digest": str(repair.get("proposal_digest") or ""),
                "source_mutation_id": str(
                    repair.get("source_mutation_id") or ""
                ),
                "occurred_at": repair.get("occurred_at") or get_utc_now(),
            }

        async def record_repair_failure(
            failure: BaseException,
            *,
            cycle: int,
        ) -> str:
            diagnostic = build_failure_diagnostic(
                failure,
                step=f"reference-card-repair:{cycle}",
                chapter_id=current_chapter_id,
                occurred_at=get_utc_now(),
            )
            await generation_job_repo.append_diagnostic(job_id, diagnostic)
            return str(diagnostic["event_id"])

        recorded_repair_events = [
            dict(event)
            for event in list(job.get("reference_card_repair_events") or [])
            if isinstance(event, Mapping)
            and type(event.get("cycle")) is int
            and str(event.get("chapter_id") or "") == current_chapter_id
            and str(event.get("authorization_digest") or "")
            == str((authorization or {}).get("authorization_digest") or "")
        ]
        recorded_repair_cycles = {
            int(event.get("cycle"))
            for event in recorded_repair_events
        }
        latest_recorded_repair_event = max(
            recorded_repair_events,
            key=lambda item: int(item["cycle"]),
            default=None,
        )
        last_repair: dict[str, Any] | None = None
        last_repair_event_id = str(
            (latest_recorded_repair_event or {}).get("event_id") or ""
        )
        pending_repair_event = next(
            (
                dict(event)
                for event in reversed(
                    list(job.get("reference_card_repair_events") or [])
                )
                if isinstance(event, Mapping)
                and str(event.get("chapter_id") or "") == current_chapter_id
                and str(event.get("authorization_digest") or "")
                == str((authorization or {}).get("authorization_digest") or "")
                and event.get("outcome") == "applied"
                and event.get("resolution") is None
            ),
            None,
        )
        pending_repair_event_id = str(
            (pending_repair_event or {}).get("event_id") or ""
        )
        pending_repair_candidate_ids = {
            str(candidate_id)
            for candidate_id in list(
                (pending_repair_event or {}).get(
                    "created_reference_card_candidate_ids"
                )
                or []
            )
        }
        pending_repair_source_mutation_id = str(
            (pending_repair_event or {}).get("source_mutation_id") or ""
        )
        current_revision = expected_revision if authority_is_valid else None

        def build_auto_creation_event(
            result: Mapping[str, Any],
        ) -> dict[str, Any]:
            status = str(result.get("status") or "")
            deny_reasons = sorted({
                str(reason) for reason in list(result.get("deny_reasons") or [])
            })
            outcome = (
                "auto_created"
                if status == "created"
                else (
                    "manual_review_required"
                    if status == "denied"
                    else "not_applicable"
                )
            )
            source_mutation_id = str(result.get("source_mutation_id") or "")
            source_tag = hashlib.sha256(
                source_mutation_id.encode("utf-8")
            ).hexdigest()[:16]
            return {
                "schema_version": "reference_card_auto_creation_event.v1",
                "event_id": (
                    f"{str((authorization or {}).get('authorization_digest') or '')}:"
                    f"{current_chapter_id}:{source_tag}"
                ),
                "chapter_id": current_chapter_id,
                "actor_owner_id": owner_id,
                "authorization_digest": str(
                    (authorization or {}).get("authorization_digest") or ""
                ),
                "readiness_digest": readiness_digest,
                "authorization_revision": authorization_revision,
                "policy_revision": (authorization or {}).get(
                    "policy_revision"
                ),
                "outcome": outcome,
                "created_count": int(result.get("created_count") or 0),
                "mappings": list(result.get("mappings") or []),
                "deny_reasons": deny_reasons,
                "denials": list(result.get("denials") or []),
                "limit_usage": dict(result.get("limit_usage") or {}),
                "source_mutation_id": source_mutation_id,
                "mutation_receipt_id": str(
                    result.get("mutation_idempotency_key") or ""
                ),
                "occurred_at": result.get("occurred_at") or get_utc_now(),
            }

        async def record_auto_result(
            result: Mapping[str, Any],
            revision: int,
        ) -> tuple[dict[str, Any], dict[str, Any], int]:
            next_revision = result.get("next_narrative_revision")
            if (
                type(next_revision) is not int
                or next_revision not in {revision, revision + 1}
            ):
                raise ValueError(
                    "Reference-card auto-creation narrative revision is invalid"
                )
            status = str(result.get("status") or "")
            if status not in {"created", "denied", "not_applicable"}:
                raise ValueError("Reference-card auto-creation result is invalid")
            event = build_auto_creation_event(result)
            await generation_job_repo.record_reference_card_auto_creation(
                job_id,
                chapter_id=current_chapter_id,
                expected_revision=revision,
                next_revision=next_revision,
                event=event,
            )
            return dict(result), event, next_revision

        async def apply_and_record_auto(
            revision: int,
        ) -> tuple[dict[str, Any], dict[str, Any], int]:
            result = await auto_reference_card_creation_service.apply_chapter(
                owner_id=owner_id,
                novel_id=novel_id,
                job_id=str(job_id),
                chapter_id=current_chapter_id,
                readiness_digest=readiness_digest,
                authorization_revision=authorization_revision,
                expected_narrative_revision=revision,
                authorization=authorization,
            )
            return await record_auto_result(result, revision)

        def auto_event_resolves_pending_repair(
            event: Mapping[str, Any],
        ) -> bool:
            mapped_candidate_ids = {
                str(mapping.get("candidate_id") or "")
                for mapping in list(event.get("mappings") or [])
                if isinstance(mapping, Mapping)
            }
            return bool(
                pending_repair_event_id
                and pending_repair_candidate_ids
                and str(event.get("chapter_id") or "") == current_chapter_id
                and str(event.get("authorization_digest") or "")
                == str((authorization or {}).get("authorization_digest") or "")
                and str(event.get("outcome") or "") == "auto_created"
                and str(event.get("source_mutation_id") or "")
                == pending_repair_source_mutation_id
                and mapped_candidate_ids == pending_repair_candidate_ids
            )

        async def finalize_pending_repair_from_event(
            event: Mapping[str, Any],
        ) -> None:
            nonlocal pending_repair_event_id
            if not auto_event_resolves_pending_repair(event):
                return
            await generation_job_repo.finalize_reference_card_repair_resolution(
                job_id,
                event_id=pending_repair_event_id,
                resolution="rewritten_unique_new",
            )
            pending_repair_event_id = ""
        if (
            authority_is_valid
            and policy.max_candidate_repair_cycles_per_chapter > 0
        ):
            for cycle in range(
                1,
                policy.max_candidate_repair_cycles_per_chapter + 1,
            ):
                if cycle in recorded_repair_cycles:
                    continue
                recovered = await (
                    reference_card_dependency_repair_service.recover_applied_cycle(
                        owner_id=owner_id,
                        novel_id=novel_id,
                        job_id=str(job_id),
                        chapter_id=current_chapter_id,
                        readiness=readiness,
                        expected_narrative_revision=current_revision,
                        cycle=cycle,
                    )
                )
                if recovered is None:
                    break
                repair_next_revision = recovered.get(
                    "next_narrative_revision"
                )
                if (
                    str(recovered.get("status") or "") != "applied"
                    or type(repair_next_revision) is not int
                    or repair_next_revision != current_revision + 1
                ):
                    raise ValueError(
                        "Recovered reference-card repair result is invalid"
                    )
                repair_event = build_repair_event(recovered, cycle)
                await generation_job_repo.record_reference_card_repair(
                    job_id,
                    chapter_id=current_chapter_id,
                    expected_revision=current_revision,
                    next_revision=repair_next_revision,
                    event=repair_event,
                )
                current_revision = repair_next_revision
                recorded_repair_cycles.add(cycle)
                last_repair = dict(recovered)
                last_repair_event_id = str(repair_event["event_id"])
                if repair_event["resolution"] is None:
                    pending_repair_event_id = repair_event["event_id"]
                    pending_repair_candidate_ids = set(
                        repair_event[
                            "created_reference_card_candidate_ids"
                        ]
                    )
                    pending_repair_source_mutation_id = str(
                        repair_event.get("source_mutation_id") or ""
                    )

        persisted_auto_created_events = [
            dict(event)
            for event in list(
                job.get("reference_card_auto_creation_events") or []
            )
            if isinstance(event, Mapping)
            and str(event.get("chapter_id") or "") == current_chapter_id
            and str(event.get("authorization_digest") or "")
            == str((authorization or {}).get("authorization_digest") or "")
            and str(event.get("outcome") or "") == "auto_created"
        ]
        for persisted_event in persisted_auto_created_events:
            await finalize_pending_repair_from_event(persisted_event)

        if authority_is_valid and not persisted_auto_created_events:
            recovered_auto_creation = await (
                auto_reference_card_creation_service.recover_applied_chapter(
                    owner_id=owner_id,
                    novel_id=novel_id,
                    job_id=str(job_id),
                    chapter_id=current_chapter_id,
                    readiness_digest=readiness_digest,
                    authorization_revision=authorization_revision,
                    expected_narrative_revision=current_revision,
                    authorization=authorization,
                )
            )
            if recovered_auto_creation is not None:
                (
                    _recovered_result,
                    recovered_event,
                    current_revision,
                ) = await record_auto_result(
                    recovered_auto_creation,
                    current_revision,
                )
                await finalize_pending_repair_from_event(recovered_event)

        blockers = await emergent_reference_card_candidate_module.blocking_summary(
            novel_id
        )
        if blockers is None:
            return None
        blocker_chapter_ids = [
            str(chapter_id)
            for chapter_id in list(blockers.get("chapter_ids") or [])
        ]
        if not authority_is_valid or blocker_chapter_ids != [current_chapter_id]:
            return {
                **blockers,
                "auto_creation": {
                    "outcome": "manual_review_required",
                    "created_count": 0,
                    "deny_reasons": ["authorization_invalid"],
                    "denials": [],
                },
            }
        if type(current_revision) is not int:
            raise ValueError("Reference-card Job revision is invalid")

        result, event, current_revision = await apply_and_record_auto(
            current_revision
        )
        await finalize_pending_repair_from_event(event)
        live_blockers = (
            await emergent_reference_card_candidate_module.blocking_summary(
                novel_id
            )
        )
        if live_blockers is None:
            return None
        if (
            str(result.get("status") or "") != "denied"
            or policy.max_candidate_repair_cycles_per_chapter == 0
            or not reference_card_denials_are_repairable(
                list(result.get("denials") or [])
            )
        ):
            return {
                **live_blockers,
                "auto_creation": {
                    "outcome": "manual_review_required",
                    "created_count": event["created_count"],
                    "deny_reasons": event["deny_reasons"],
                    "denials": event["denials"],
                },
            }

        generation_params = protected_generation_params
        plan_authorization = parse_reference_card_repair_plan_authorization(
            planning.get("reference_card_repair_plan_authorization")
        )
        latest_denials = list(result.get("denials") or [])
        for cycle in range(
            1,
            policy.max_candidate_repair_cycles_per_chapter + 1,
        ):
            if cycle in recorded_repair_cycles:
                continue
            async def reserve_attempt_scope(slots: int):
                if slots != plan_authorization.generation_plan.max_semantic_attempts:
                    raise ValueError(
                        "Reference-card repair attempt reservation changed"
                    )
                await generation_job_repo.reserve_attempts(
                    job_id,
                    current_chapter_id,
                    slots,
                )
                existing = await generation_job_repo.list_attempt_slots(
                    job_id,
                    chapter_id=current_chapter_id,
                    step_prefix=f"reference-card-repair:{cycle}",
                )
                return JobAttemptScope(
                    job_id,
                    current_chapter_id,
                    f"reference-card-repair:{cycle}",
                    existing_attempt_slots=existing,
                )

            async def finish_attempt_reservation() -> None:
                await generation_job_repo.finish_attempt_reservation(
                    job_id,
                    current_chapter_id,
                )

            try:
                repair = await reference_card_dependency_repair_service.repair_cycle(
                    owner_id=owner_id,
                    novel_id=novel_id,
                    job_id=str(job_id),
                    chapter_id=current_chapter_id,
                    readiness=readiness,
                    generation_params=generation_params,
                    expected_narrative_revision=current_revision,
                    cycle=cycle,
                    denials=latest_denials,
                    attempt_scope_factory=reserve_attempt_scope,
                    finish_attempt_reservation=finish_attempt_reservation,
                )
            except TokenBudgetExceeded as exc:
                failure_event_id = await record_repair_failure(exc, cycle=cycle)
                return {
                    **live_blockers,
                    "auto_creation": {
                        "outcome": "repair_exhausted",
                        "pause_reason": "cost_cap",
                        "created_count": 0,
                        "deny_reasons": list(event["deny_reasons"]),
                        "denials": latest_denials,
                        "failure_event_id": failure_event_id,
                    },
                }
            except AttemptCapacityExceeded as exc:
                failure_event_id = await record_repair_failure(exc, cycle=cycle)
                return {
                    **live_blockers,
                    "auto_creation": {
                        "outcome": "repair_exhausted",
                        "pause_reason": "attempt_capacity",
                        "created_count": 0,
                        "deny_reasons": list(event["deny_reasons"]),
                        "denials": latest_denials,
                        "failure_event_id": failure_event_id,
                    },
                }
            except MutationConflictError as exc:
                failure_event_id = await record_repair_failure(exc, cycle=cycle)
                return {
                    **live_blockers,
                    "auto_creation": {
                        "outcome": "repair_exhausted",
                        "pause_reason": "source_changed",
                        "created_count": 0,
                        "deny_reasons": list(event["deny_reasons"]),
                        "denials": latest_denials,
                        "failure_event_id": failure_event_id,
                    },
                }
            repair_status = str(repair.get("status") or "")
            repair_next_revision = repair.get("next_narrative_revision")
            if (
                repair_status not in {"applied", "exhausted", "uncertain"}
                or type(repair_next_revision) is not int
                or repair_next_revision
                not in {current_revision, current_revision + 1}
            ):
                raise ValueError("Reference-card repair result is invalid")
            repair_event = build_repair_event(repair, cycle)
            await generation_job_repo.record_reference_card_repair(
                job_id,
                chapter_id=current_chapter_id,
                expected_revision=current_revision,
                next_revision=repair_next_revision,
                event=repair_event,
            )
            current_revision = repair_next_revision
            recorded_repair_cycles.add(cycle)
            last_repair = repair
            last_repair_event_id = str(repair_event["event_id"])
            pending_repair_event_id = (
                repair_event["event_id"]
                if repair_event["resolution"] is None
                else ""
            )
            pending_repair_candidate_ids = (
                set(repair_event["created_reference_card_candidate_ids"])
                if pending_repair_event_id
                else set()
            )
            pending_repair_source_mutation_id = (
                str(repair_event.get("source_mutation_id") or "")
                if pending_repair_event_id
                else ""
            )
            if repair_status == "uncertain":
                return {
                    **live_blockers,
                    "auto_creation": {
                        "outcome": "repair_exhausted",
                        "pause_reason": "uncertain_attempt",
                        "created_count": 0,
                        "deny_reasons": list(event["deny_reasons"]),
                        "denials": latest_denials,
                        "failure_event_id": last_repair_event_id,
                    },
                }
            if repair_status != "applied":
                continue
            live_blockers = (
                await emergent_reference_card_candidate_module.blocking_summary(
                    novel_id
                )
            )
            if live_blockers is None:
                return None
            result, event, current_revision = await apply_and_record_auto(
                current_revision
            )
            await finalize_pending_repair_from_event(event)
            live_blockers = (
                await emergent_reference_card_candidate_module.blocking_summary(
                    novel_id
                )
            )
            if live_blockers is None:
                return None
            latest_denials = list(result.get("denials") or [])
            if not reference_card_denials_are_repairable(latest_denials):
                return {
                    **live_blockers,
                    "auto_creation": {
                        "outcome": "manual_review_required",
                        "created_count": int(event.get("created_count") or 0),
                        "deny_reasons": list(event.get("deny_reasons") or []),
                        "denials": latest_denials,
                        "repair": dict(last_repair or {}),
                    },
                }

        return {
            **live_blockers,
            "auto_creation": {
                "outcome": "repair_exhausted",
                "pause_reason": "reference_card_repair_exhausted",
                "created_count": int(event.get("created_count") or 0),
                "deny_reasons": list(event.get("deny_reasons") or []),
                "denials": latest_denials,
                "repair": dict(last_repair or {}),
                "failure_event_id": last_repair_event_id,
            },
        }

    @staticmethod
    async def _incomplete_prose_is_resolved(job: Mapping[str, Any]) -> bool:
        pending = dict(job.get("incomplete_prose") or {})
        chapter_id = str(pending.get("chapter_id") or "")
        if not chapter_id:
            return False
        chapter = await chapter_repo.get_chapter_by_id(chapter_id)
        if str(chapter.get("novel_id")) != str(job.get("novel_id")):
            raise ValueError("Incomplete prose checkpoint belongs to another novel")
        if not str(chapter.get("content") or "").strip():
            return False
        prose_state = str(
            (chapter.get("prose_acceptance") or {}).get("state")
            or (chapter.get("state_completion") or {}).get(
                "prose_acceptance_state"
            )
            or ""
        )
        return prose_state != "partial_manual_required"

    @staticmethod
    async def _candidate_manual_takeover_resolution(
        job: Mapping[str, Any],
    ) -> CandidateManualTakeoverResolutionV1 | None:
        """Return the exact manual completion proof, or None while unfinished."""

        raw_takeover = job.get("candidate_manual_takeover")
        if raw_takeover is None:
            return None
        try:
            takeover = parse_candidate_manual_takeover(raw_takeover)
            validate_candidate_manual_takeover_binding(
                takeover,
                job.get("candidate_pipeline_checkpoints"),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Generation job candidate manual takeover binding is invalid"
            ) from exc
        readiness = job.get("readiness")
        readiness_digest = (
            readiness.get("digest")
            if isinstance(readiness, Mapping)
            else None
        )
        if (
            takeover.job_id != str(job.get("_id") or "")
            or takeover.novel_id != str(job.get("novel_id") or "")
            or takeover.chapter_id
            != str(job.get("current_chapter_id") or "")
            or takeover.readiness_digest
            != str(readiness_digest or "")
            or takeover.authorization_revision
            != job.get("authorization_revision")
            or takeover.expected_narrative_revision
            != job.get("expected_narrative_revision")
            or takeover.failure_event_id
            != str(job.get("current_failure_event_id") or "")
            or job.get("status") != "paused"
            or job.get("pause_reason") != "incomplete_scene"
        ):
            raise ValueError(
                "Generation job candidate manual takeover snapshot changed"
            )
        chapter = await chapter_repo.get_chapter_by_id(takeover.chapter_id)
        if str(chapter.get("novel_id") or "") != takeover.novel_id:
            raise ValueError(
                "Candidate manual takeover chapter belongs to another novel"
            )
        content = str(chapter.get("content") or "")
        acceptance = chapter.get("prose_acceptance")
        if (
            not content.strip()
            or chapter.get("status") != "completed"
            or not isinstance(acceptance, Mapping)
            or acceptance.get("state") != "manual_complete"
            or acceptance.get("content_origin") not in {None, "manual"}
        ):
            return None
        digest = chapter_content_digest(content)
        if acceptance.get("content_digest") != digest:
            return None
        current_revision = await narrative_revision_store.current(
            takeover.novel_id
        )
        try:
            return CandidateManualTakeoverResolutionV1(
                schema_version="candidate_manual_takeover_resolution.v1",
                takeover=takeover,
                manual_content_digest=digest,
                narrative_revision=current_revision,
            )
        except ValueError:
            return None

    @staticmethod
    def _pending_scene_can_use_new_policy(
        pending: Mapping[str, Any],
        policy: ProseContinuationPolicy,
        *,
        allow_divergence_stop: bool = False,
    ) -> bool:
        pause_reason = str(pending.get("pause_reason") or "")
        if pause_reason not in {
            "automatic_continuations_exhausted",
            "provider_length_continuation_capacity_exhausted",
        } and not (
            allow_divergence_stop
            and pause_reason == "prose_scene_divergence_stopped"
        ):
            return False
        for scene in list(pending.get("scene_progress") or []):
            if not isinstance(scene, Mapping):
                continue
            if str(scene.get("status") or "") == "complete":
                continue
            try:
                used = max(
                    0,
                    int(scene.get("automatic_continuations_used") or 0),
                )
            except (TypeError, ValueError):
                return False
            return policy.automatic_continuations_per_scene > used
        # No paused scene means no evidence for a safe quota extension.
        return False

    @staticmethod
    async def _recalculate_after_outline_acceptance(
        job_id: str,
        chapter_id: str,
    ) -> dict[str, Any]:
        """Narrow a running job after its real scene count becomes known.

        An outline acceptance is already an authorized normal write.  The
        follow-up calculation is read-only until it proves every relevant call
        and token ceiling is no larger than the existing authorization.  A
        larger or newly-unacknowledged scope is returned to the engine for a
        pause; it is never persisted as an implicit expansion.
        """
        job = await generation_job_repo.get_job(job_id)
        protected_generation_params = _validate_resumable_job_authorization(job)
        authorization = dict(job.get("prose_continuation_authorization") or {})
        current_authorization = parse_prose_authorization(authorization)
        current_revision = job.get("authorization_revision")
        if (
            type(current_revision) is not int
            or current_revision != current_authorization.authorization_revision
        ):
            raise ValueError("outline authorization revision is invalid")
        readiness = job.get("readiness")
        readiness_digest = (
            readiness.get("digest")
            if isinstance(readiness, Mapping)
            else None
        )
        if not isinstance(readiness_digest, str) or not readiness_digest:
            raise ValueError("outline authorization readiness digest is invalid")
        if job.get("scope") == "book":
            chapters = await get_book_worklist(
                str(job["novel_id"]),
                include_content=True,
            )
        else:
            chapters = await ChapterService.get_chapters_by_volume(
                str(job["volume_id"]),
                include_content=True,
            )
            chapters = await state_completion_module.attach_many(chapters)
        if not any(str(item.get("_id") or "") == str(chapter_id) for item in chapters):
            raise ValueError("accepted outline chapter is outside the generation job")

        policy = ProseContinuationPolicy.from_mapping(
            authorization.get("policy")
            or protected_generation_params.get(
                "prose_continuation_policy"
            )
        )
        generation_params = {
            **protected_generation_params,
            "prose_continuation_policy": policy.to_dict(),
        }
        report = await generation_readiness_module.inspect(
            novel_id=str(job["novel_id"]),
            scope=str(job["scope"]),
            volume_id=(
                str(job["volume_id"])
                if job.get("volume_id") is not None
                else None
            ),
            chapters=chapters,
            outline_deviation_policy=str(
                job.get("outline_deviation_policy") or PAUSE_FOR_REWRITE
            ),
            prose_continuation_policy=policy,
            token_budget=job.get("token_budget"),
            generation_params=generation_params,
            authorization_revision=max(1, current_revision),
            reference_card_auto_creation_policy=(
                _persisted_reference_card_auto_creation_policy(job)
            ),
        )
        candidate = dict(
            (report.get("planning") or {}).get(
                "prose_continuation_authorization"
            ) or {}
        )
        candidate_authorization = (
            parse_prose_authorization(candidate) if candidate else None
        )
        previously_acknowledged = set(
            str(code)
            for code in list((job.get("readiness") or {}).get(
                "acknowledged_warning_codes"
            ) or [])
        )
        new_acknowledgements = sorted(
            str(issue.get("code") or "")
            for issue in list(report.get("issues") or [])
            if issue.get("level") == "warning_requires_ack"
            and str(issue.get("code") or "") not in previously_acknowledged
        )
        blocked = [
            str(issue.get("code") or "")
            for issue in list(report.get("issues") or [])
            if issue.get("level") == "blocked"
        ]
        expected_revision = (report.get("resources") or {}).get(
            "narrative_revision"
        )
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("outline recalculation narrative revision is invalid")
        command = OutlineAuthorizationRecalculationCommandV1(
            schema_version="outline_authorization_recalculation_command.v1",
            chapter_id=str(chapter_id),
            authorization_revision=current_revision,
            expected_narrative_revision=expected_revision,
            readiness_digest=readiness_digest,
            expected_authorization_digest=prose_authorization_digest(
                current_authorization
            ),
            candidate_scope=(
                prose_authorization_scope(candidate_authorization)
                if candidate_authorization is not None
                else None
            ),
            new_acknowledgement_codes=tuple(new_acknowledgements),
            blocked_issue_codes=tuple(blocked),
        )
        return await generation_job_repo.publish_outline_authorization_recalculation(
            job_id,
            command=command,
        )

    @staticmethod
    async def _run_required_book_successor_child(
        parent_job_id: str,
        child_job_id: str,
    ) -> Dict[str, Any]:
        """Run one internally-authorized child in a fresh lease context."""

        async def run_clean() -> Dict[str, Any]:
            child = await generation_job_repo.get_job(child_job_id)
            if str(child.get("required_book_successor_parent_job_id") or "") != (
                str(parent_job_id)
            ):
                raise ValueError("Required book successor child parent changed")
            if child.get("has_uncertain_attempts"):
                return child
            status = str(child.get("status") or "")
            if status == "interrupted":
                previous_epoch = child.get("execution_epoch", 0)
                if type(previous_epoch) is not int or previous_epoch < 0:
                    raise ValueError(
                        "Required book successor child epoch is invalid"
                    )
                await generation_job_repo.transition_job_resume(
                    child_job_id,
                    {
                        "status": "running",
                        "pause_reason": None,
                        "error": None,
                        "active_slot": (
                            f"required_book_successor_child:{parent_job_id}"
                        ),
                        "has_uncertain_attempts": False,
                        "confirm_uncertain_prose_retry": False,
                    },
                    previous_status=status,
                    previous_execution_epoch=previous_epoch,
                )
                child = await generation_job_repo.get_job(child_job_id)
                status = str(child.get("status") or "")
            if status != "running":
                return child
            control = JobControl()
            spawned = await GenerationJobService._spawn(
                child_job_id,
                control,
            )
            if not spawned:
                return await generation_job_repo.get_job(child_job_id)
            entry = _REGISTRY.get(child_job_id)
            if entry is None:
                return await generation_job_repo.get_job(child_job_id)
            await entry[0]
            return await generation_job_repo.get_job(child_job_id)

        task = asyncio.create_task(run_clean(), context=Context())
        return await task

    @staticmethod
    async def _spawn(job_id: str, control: JobControl) -> bool:
        """Acquire the durable execution lease before scheduling JobEngine."""

        job = await generation_job_repo.get_job(job_id)
        _validate_resumable_job_authorization(job)
        previous_entry = _REGISTRY.get(job_id)
        worker_id = secrets.token_hex(32)
        now = get_utc_now()
        try:
            lease = await generation_job_repo.acquire_execution_lease(
                job_id,
                worker_id,
                now=now,
                expires_at=now + timedelta(
                    seconds=JOB_EXECUTION_LEASE_SECONDS
                ),
            )
        except JobExecutionLeaseUnavailable:
            await generation_job_repo.interrupt_stale_execution(
                job_id,
                now=now,
            )
            return False

        previous_task = (
            previous_entry[0] if previous_entry is not None else None
        )
        if previous_task is not None and not previous_task.done():
            previous_entry[1].abort_requested = True
            previous_task.cancel()
            try:
                await previous_task
            except (asyncio.CancelledError, JobExecutionLeaseLost):
                pass
            except Exception:  # noqa: BLE001 - the lease already fenced it
                logger.exception(
                    "[job %s] prior worker failed during fenced handoff",
                    job_id,
                )
        # 工作清单提供者按作业 scope 装配；引擎本身对 scope 无知。
        async def _list_worklist():
            started_at = time.perf_counter()
            job = await generation_job_repo.get_job(job_id)
            if job.get("scope") == "book":
                chapters = await get_book_worklist(str(job["novel_id"]), include_content=True)
            else:
                chapters = await ChapterService.get_chapters_by_volume(
                    str(job["volume_id"]), include_content=True
                )
                chapters = await state_completion_module.attach_many(chapters)
            if readiness_uses_required_chapter_finalization(
                job.get("readiness")
            ):
                authority = validate_required_chapter_finalization_readiness(
                    job["readiness"]
                )
                chapters = [
                    chapter
                    for chapter in chapters
                    if str(chapter.get("_id") or "") == authority.chapter_id
                ]
            elif readiness_uses_required_chapter_state(job.get("readiness")):
                authority = validate_required_chapter_state_readiness(
                    job["readiness"]
                )
                chapters = [
                    chapter
                    for chapter in chapters
                    if str(chapter.get("_id") or "") == authority.chapter_id
                ]
            logger.info(
                "[job %s] worklist chapters=%d content_chars=%d elapsed_ms=%d",
                job_id,
                len(chapters),
                sum(len(str(chapter.get("content") or "")) for chapter in chapters),
                int((time.perf_counter() - started_at) * 1000),
            )
            return chapters

        async def _run_chapter(novel_id: str, chapter: Dict[str, Any]):
            chapter_id = str(chapter["_id"])
            current_job = await generation_job_repo.get_job(job_id)
            generation_params = _validate_resumable_job_authorization(
                current_job
            )
            cursor = current_job.get("expected_narrative_revision")
            state_binding: JobMutationRecoveryBindingV1 | None = None
            recovered_receipt: JobMutationReceiptV1 | None = None
            if cursor is not None:
                if type(cursor) is not int or cursor < 0:
                    raise ValueError(
                        "Generation job narrative revision cursor is invalid"
                    )
                if readiness_uses_candidate_pipeline(current_job.get("readiness")):
                    state_binding = _state_only_job_mutation_binding(
                        job_id=job_id,
                        job=current_job,
                        chapter_id=chapter_id,
                        expected_revision=cursor,
                    )
                    raw_binding = current_job.get("job_mutation_recovery")
                    if raw_binding is not None:
                        stored_binding = JobMutationRecoveryBindingV1.model_validate(
                            raw_binding
                        )
                        if stored_binding != state_binding:
                            raise StaleStatePreview(
                                "Generation job state mutation binding changed"
                            )
                        await generation_job_repo.bind_job_mutation_recovery(
                            job_id,
                            state_binding,
                        )
                        recovered_revision = await _recover_job_mutation_revision(
                            state_binding
                        )
                        if recovered_revision is not None:
                            recovered_receipt = JobMutationReceiptV1(
                                schema_version="job_mutation_receipt.v1",
                                binding=state_binding,
                                next_narrative_revision=recovered_revision,
                            )
                current_revision = await narrative_revision_store.current(novel_id)
                expected_live_revision = (
                    recovered_receipt.next_narrative_revision
                    if recovered_receipt is not None
                    else cursor
                )
                if current_revision != expected_live_revision:
                    raise StaleStatePreview(
                        "Generation job narrative revision changed before chapter execution"
                    )
            if recovered_receipt is not None:
                attempts = await generation_job_repo.list_attempt_slots(
                    job_id,
                    chapter_id=chapter_id,
                    step_prefix="",
                )
                try:
                    return _recovered_state_only_outcome(
                        chapter,
                        recovered_receipt,
                        list(attempts),
                    )
                finally:
                    await generation_job_repo.finish_attempt_reservation(
                        job_id,
                        chapter_id,
                    )
            slots = _estimate_authorized_chapter_attempt_slots(
                current_job,
                chapter,
                generation_params,
            )
            await generation_job_repo.reserve_attempts(job_id, chapter_id, slots)
            outline_deviation_policy = validate_outline_deviation_policy(
                current_job.get("outline_deviation_policy")
                or PAUSE_FOR_REWRITE
            )
            confirm_prose_retry = bool(
                current_job.get("confirm_uncertain_prose_retry")
            )
            if confirm_prose_retry:
                # Consume before any Provider work. A second crash requires a new
                # user confirmation instead of inheriting a stale blanket grant.
                if not await generation_job_repo.consume_uncertain_prose_retry(
                    job_id
                ):
                    raise ValueError(
                        "Generation job prose retry grant changed before use"
                    )
            deps = build_chapter_pipeline_deps(
                lambda step: JobAttemptScope(
                    job_id,
                    chapter_id,
                    step,
                    confirm_uncertain_retry=(
                        confirm_prose_retry and step == "prose"
                    ),
                ),
                generation_params=generation_params,
                recalculate_prose_authorization=(
                    lambda accepted_chapter_id, _outline:
                    GenerationJobService._recalculate_after_outline_acceptance(
                        job_id,
                        accepted_chapter_id,
                    )
                ),
                state_job_mutation_binding=state_binding,
            )
            if state_binding is not None:
                generate_state = deps.generate_state

                async def generate_bound_state(
                    bound_novel_id: str,
                    bound_chapter: dict[str, Any],
                ):
                    # The marker becomes durable only when the state step is
                    # actually about to run.  An earlier adherence pause must
                    # not leave a phantom mutation recovery checkpoint.
                    await generation_job_repo.bind_job_mutation_recovery(
                        job_id,
                        state_binding,
                    )
                    return await generate_state(
                        bound_novel_id,
                        bound_chapter,
                    )

                deps = replace(deps, generate_state=generate_bound_state)

            async def attach_state_receipt(
                outcome: ChapterOutcome,
            ) -> ChapterOutcome:
                if state_binding is None:
                    return outcome
                revision = await _recover_job_mutation_revision(state_binding)
                if revision is None:
                    if "state" in outcome.steps_done:
                        raise StaleStatePreview(
                            "Generation job state mutation receipt is missing"
                        )
                    return outcome
                if await narrative_revision_store.current(novel_id) != revision:
                    raise StaleStatePreview(
                        "Generation job state mutation revision diverged"
                    )
                outcome.mutation_receipts.append(JobMutationReceiptV1(
                    schema_version="job_mutation_receipt.v1",
                    binding=state_binding,
                    next_narrative_revision=revision,
                ))
                if "state" not in outcome.steps_done:
                    outcome.steps_done.append("state")
                outcome.steps_skipped = [
                    step for step in outcome.steps_skipped if step != "state"
                ]
                outcome.summary_written = True
                return outcome
            try:
                try:
                    outcome = await run_chapter(
                        novel_id,
                        chapter,
                        deps,
                        outline_deviation_policy=outline_deviation_policy,
                    )
                except ChapterPipelineFailed as exc:
                    if state_binding is not None:
                        recovered = await attach_state_receipt(exc.outcome)
                        if recovered.mutation_receipts:
                            return recovered
                    raise
                return await attach_state_receipt(outcome)
            finally:
                await generation_job_repo.finish_attempt_reservation(job_id, chapter_id)

        async def _run_required_review_chapter(
            novel_id: str,
            chapter: Dict[str, Any],
        ):
            current_job = await generation_job_repo.get_job(job_id)
            _validate_resumable_job_authorization(current_job)
            if not readiness_uses_required_chapter_review(
                current_job.get("readiness")
            ):
                raise ValueError(
                    "Required chapter review Job authorization is unavailable"
                )
            return await RequiredChapterReviewJobRunner(
                job_id,
                repository=generation_job_repo,
            ).run(novel_id, chapter)

        async def _run_required_state_chapter(
            novel_id: str,
            chapter: Dict[str, Any],
        ):
            current_job = await generation_job_repo.get_job(job_id)
            _validate_resumable_job_authorization(current_job)
            if not readiness_uses_required_chapter_state(
                current_job.get("readiness")
            ):
                raise ValueError(
                    "Required chapter state Job authorization is unavailable"
                )
            return await RequiredChapterStateJobRunner(
                job_id,
                repository=generation_job_repo,
            ).run(novel_id, chapter)

        async def _run_required_finalization_chapter(
            novel_id: str,
            chapter: Dict[str, Any],
        ):
            current_job = await generation_job_repo.get_job(job_id)
            _validate_resumable_job_authorization(current_job)
            if not readiness_uses_required_chapter_finalization(
                current_job.get("readiness")
            ):
                raise ValueError(
                    "Required chapter finalization Job authorization is unavailable"
                )
            return await RequiredChapterFinalizationJobRunner(
                job_id,
                repository=generation_job_repo,
            ).run(novel_id, chapter)

        async def _run_candidate_chapter(
            novel_id: str,
            chapter: Dict[str, Any],
        ):
            chapter_id = str(chapter["_id"])
            current_job = await generation_job_repo.get_job(job_id)
            generation_params = _validate_resumable_job_authorization(
                current_job
            )
            readiness = current_job.get("readiness")
            if not isinstance(readiness, Mapping):
                raise ValueError("candidate Job readiness is invalid")
            expected_revision = current_job.get("expected_narrative_revision")
            if type(expected_revision) is not int or expected_revision < 0:
                raise ValueError("candidate Job narrative revision cursor is invalid")
            authorized_slots = _estimate_authorized_chapter_attempt_slots(
                current_job,
                chapter,
                generation_params,
            )
            existing_slots = await generation_job_repo.list_attempt_slots(
                job_id,
                chapter_id=chapter_id,
                step_prefix="",
            )
            def build_execution(
                *,
                chapter_id: str,
                attempt_scope_factory,
            ) -> CandidateJobExecution:
                repairs = ChapterCandidateRepairApplication(
                    execution_id=job_id,
                    readiness=readiness,
                    generation_params=generation_params,
                    attempt_scope_factory=attempt_scope_factory,
                )
                repair_authorization, adherence_plan, state_plan = (
                    repairs.execution_snapshot(chapter_id=chapter_id)
                )
                cycles = repair_authorization.max_repair_cycles_per_chapter
                work = readiness.get("work")
                raw_chapters = (
                    work.get("chapters")
                    if isinstance(work, Mapping)
                    else None
                )
                snapshot = next(
                    (
                        item
                        for item in (raw_chapters or [])
                        if isinstance(item, Mapping)
                        and item.get("chapter_id") == chapter_id
                    ),
                    None,
                )
                if not isinstance(snapshot, Mapping):
                    raise ValueError("candidate chapter snapshot is missing")
                requirements = candidate_job_generation_requirements(readiness)
                live_plans = plan_candidate_job_generation(
                    needs_outline=requirements.needs_outline,
                    active=requirements.active,
                )
                execution_authorization = (
                    validate_candidate_job_execution_authorization(
                        readiness,
                        chapter_id=chapter_id,
                        generation_params=generation_params,
                        live_plans=live_plans,
                    )
                )
                if (
                    adherence_plan != live_plans.adherence
                    or state_plan != live_plans.state
                    or execution_authorization.prose is None
                    or execution_authorization.adherence is None
                    or execution_authorization.state is None
                ):
                    raise ValueError(
                        "candidate repair and initial Provider plans diverged"
                    )
                return CandidateJobExecution(
                    repair_authorization=repair_authorization,
                    outline_plan=(
                        generation_plan_from_candidate_snapshot(
                            execution_authorization.outline
                        )
                        if execution_authorization.outline is not None
                        else None
                    ),
                    prose_plan=generation_plan_from_candidate_snapshot(
                        execution_authorization.prose
                    ),
                    adherence_plan=generation_plan_from_candidate_snapshot(
                        execution_authorization.adherence
                    ),
                    state_plan=generation_plan_from_candidate_snapshot(
                        execution_authorization.state
                    ),
                    repair_prose_candidate=(
                        repairs.repair_prose_candidate if cycles else None
                    ),
                    repair_state_candidate=(
                        repairs.repair_state_candidate if cycles else None
                    ),
                    recover_source=repairs.recover_source,
                )

            def candidate_finalization_authorization(
            ) -> ChapterFinalizationAuthorization:
                planning = readiness.get("planning")
                if not isinstance(planning, Mapping):
                    raise ValueError("candidate finalization planning is invalid")
                frozen = parse_chapter_finalization_authorization(
                    planning.get("chapter_finalization_authorization")
                )
                readiness_digest = readiness.get("digest")
                if not isinstance(readiness_digest, str) or not readiness_digest:
                    raise ValueError("candidate readiness digest is invalid")
                return ChapterFinalizationAuthorization(
                    job_id=job_id,
                    readiness_digest=readiness_digest,
                    authorization_revision=frozen[
                        "authorization_revision"
                    ],
                )

            async def finalize_candidate(
                *,
                owner_id: str,
                novel_id: str,
                chapter: Mapping[str, Any],
                source,
                adherence: Mapping[str, Any] | None,
                state: Mapping[str, Any],
                repair_cycles_used: int,
                repair_trace: Mapping[str, Any] | None,
            ) -> Mapping[str, Any]:
                del novel_id
                proposal_id = state.get("proposal_id")
                acceptance_token = state.get("acceptance_token")
                if not isinstance(proposal_id, str) or not isinstance(
                    acceptance_token, str
                ):
                    raise ValueError("candidate state receipt is invalid")
                return await chapter_finalization_service.commit(
                    owner_id=owner_id,
                    chapter_id=str(chapter.get("_id") or ""),
                    prose_run_id=source.source_run_id,
                    prose_run_revision=source.source_run_revision,
                    state_proposal_id=proposal_id,
                    state_acceptance_token=acceptance_token,
                    authorization=candidate_finalization_authorization(),
                    evidence=ChapterFinalizationEvidence(
                        outline_adherence=dict(adherence),
                        repair_cycles_used=repair_cycles_used,
                        repair_trace=(
                            dict(repair_trace)
                            if repair_trace is not None
                            else None
                        ),
                    ),
                )

            async def record_completion_failure(
                *,
                owner_id: str,
                novel_id: str,
                chapter: Mapping[str, Any],
                failure: ChapterCandidateCompletionFailureRequest,
            ) -> Mapping[str, Any]:
                del novel_id
                decision = await chapter_finalization_service.record_failure(
                    owner_id=owner_id,
                    chapter_id=str(chapter.get("_id") or ""),
                    prose_run_id=failure.source.source_run_id,
                    prose_run_revision=failure.source.source_run_revision,
                    authorization=candidate_finalization_authorization(),
                    adherence=(
                        dict(failure.adherence)
                        if isinstance(failure.adherence, Mapping)
                        else None
                    ),
                    failure_fact=failure.failure_fact,
                    state_proposal_id=failure.state_proposal_id,
                    state_fact_accounting=failure.state_fact_accounting,
                )
                return decision.model_dump(mode="json")

            async def generate_job_prose_candidate(
                target_novel_id,
                target_chapter,
                attempt_scope=None,
                generation_params=None,
                *,
                generation_plan=None,
            ):
                return await generate_prose_candidate(
                    target_novel_id,
                    target_chapter,
                    attempt_scope,
                    generation_params,
                    generation_plan=generation_plan,
                    generation_job_id=job_id,
                )

            runner = ChapterCandidateJobRunner(
                execution_id=job_id,
                readiness=readiness,
                expected_narrative_revision=expected_revision,
                authorized_attempt_slots=authorized_slots,
                generation_params=generation_params,
                recalculate_after_outline=(
                    lambda accepted_chapter_id, _outline:
                    GenerationJobService._recalculate_after_outline_acceptance(
                        job_id,
                        accepted_chapter_id,
                    )
                ),
                deps=ChapterCandidateJobRunnerDeps(
                    get_novel=novel_repo.get_novel_by_id,
                    get_volume=volume_repo.get_volume_by_id,
                    get_chapter=chapter_repo.get_chapter_by_id,
                    list_checkpoints=(
                        generation_job_repo.list_candidate_pipeline_checkpoints
                    ),
                    append_checkpoint=(
                        generation_job_repo.append_candidate_pipeline_checkpoint
                    ),
                    list_attempts=generation_job_repo.list_attempt_slots,
                    resolve_discarded_prose_attempt_ids=(
                        resolve_discarded_candidate_attempt_ids
                    ),
                    reserve_attempts=generation_job_repo.reserve_attempts,
                    attempt_scope_factory=(
                        lambda target_job_id, target_chapter_id, step, slots:
                        JobAttemptScope(
                            target_job_id,
                            target_chapter_id,
                            step,
                            existing_attempt_slots=slots,
                        )
                    ),
                    build_execution=build_execution,
                    current_narrative_revision=(
                        narrative_revision_store.current
                    ),
                    recover_mutation_revision=(
                        _recover_job_mutation_revision
                    ),
                    advance_narrative_revision_cursor=(
                        generation_job_repo.advance_narrative_revision_cursor
                    ),
                    generate_outline=generate_outline,
                    generate_prose_candidate=generate_job_prose_candidate,
                    review_prose_candidate=review_prose_candidate,
                    generate_state_candidate=generate_state_candidate,
                    recover_state_candidate=(
                        state_proposal_module.recover_owned_repair_result
                    ),
                    record_completion_failure=record_completion_failure,
                    finalize=finalize_candidate,
                ),
            )
            try:
                return await runner.run(novel_id, chapter)
            finally:
                await generation_job_repo.finish_attempt_reservation(
                    job_id,
                    chapter_id,
                )

        async def _resolve_reference_card_blockers():
            return await GenerationJobService._resolve_reference_card_blockers(
                job_id
            )

        async def _inspect_book_completion(fence):
            current_job = await generation_job_repo.get_job(job_id)
            if current_job.get("scope") != "book":
                raise ValueError("Book completion audit requires a book Job")
            return await book_completion_audit.inspect(
                str(current_job["novel_id"]),
                job_id=str(job_id),
                expected_narrative_revision=fence.narrative_revision,
                publication_fence_token=fence.fence_token,
            )

        def _guard_book_completion_publication(
            expected_narrative_revision: int | None,
            fence_token: str,
        ):
            return book_completion_audit.publication_fence(
                str(job["novel_id"]),
                str(job_id),
                expected_narrative_revision=expected_narrative_revision,
                fence_token=fence_token,
            )

        def _required_book_child_document(
            readiness: Mapping[str, Any],
        ) -> Dict[str, Any]:
            if readiness_uses_required_chapter_finalization(readiness):
                authority = validate_required_chapter_finalization_readiness(
                    readiness
                )
            elif readiness_uses_required_chapter_state(readiness):
                authority = validate_required_chapter_state_readiness(readiness)
            else:
                authority = validate_required_chapter_review_readiness(readiness)
            return _new_job_doc(
                str(authority.novel_id),
                "book",
                None,
                None,
                int(authority.token_budget),
                int(authority.maximum_provider_attempts_total),
                readiness,
                PAUSE_FOR_REWRITE,
                dict(job.get("generation_params") or {}),
                str(readiness.get("digest") or ""),
            )

        async def _run_required_book_successor() -> None:
            async def create_child(action, readiness):
                return await (
                    generation_job_repo.create_required_book_successor_child(
                        job_id,
                        action=action,
                        document=_required_book_child_document(readiness),
                    )
                )

            async def finalize_required_book_audit() -> bool:
                parent = await generation_job_repo.get_job(job_id)
                previous_epoch = parent.get("execution_epoch", 0)
                previous_revision = parent.get("expected_narrative_revision")
                if (
                    type(previous_epoch) is not int
                    or previous_epoch < 0
                    or type(previous_revision) is not int
                    or previous_revision < 0
                ):
                    raise ValueError(
                        "Required book successor audit cursor is invalid"
                    )
                previous_status = str(parent.get("status") or "")
                previous_pause_reason = (
                    str(parent.get("pause_reason"))
                    if parent.get("pause_reason") is not None
                    else None
                )
                fence_token = secrets.token_hex(16)
                snapshot = {
                    "previous_status": previous_status,
                    "previous_pause_reason": previous_pause_reason,
                    "previous_execution_epoch": previous_epoch,
                    "previous_expected_narrative_revision": previous_revision,
                    "novel_id": str(parent.get("novel_id") or ""),
                }
                await generation_job_repo.reserve_book_completion_audit_publication(
                    job_id,
                    fence_token=fence_token,
                    **snapshot,
                )
                try:
                    async with _guard_book_completion_publication(
                        previous_revision,
                        fence_token,
                    ) as fence:
                        report = BookCompletionReport.model_validate(
                            await _inspect_book_completion(fence)
                        )
                        await generation_job_repo.renew_book_completion_audit_publication(
                            job_id,
                            fence_token=fence_token,
                            **snapshot,
                        )
                        if report.complete:
                            await generation_job_repo.complete_required_book_successor(
                                job_id,
                                report,
                                fence_token=fence_token,
                                previous_status=previous_status,
                                previous_pause_reason=previous_pause_reason,
                                previous_execution_epoch=previous_epoch,
                                previous_expected_narrative_revision=(
                                    previous_revision
                                ),
                            )
                            return True
                        await generation_job_repo.publish_book_completion_audit(
                            job_id,
                            report.model_dump(mode="json"),
                            fence_token=fence_token,
                            previous_status=previous_status,
                            previous_pause_reason=previous_pause_reason,
                            previous_execution_epoch=previous_epoch,
                            previous_expected_narrative_revision=(
                                previous_revision
                            ),
                        )
                        return False
                except (
                    CandidatePipelineCheckpointConflict,
                    JobExecutionLeaseLost,
                ):
                    raise
                except Exception as exc:  # noqa: BLE001 - stable fail-close
                    await generation_job_repo.publish_book_completion_audit_failure(
                        job_id,
                        novel_id=str(parent.get("novel_id") or ""),
                        fence_token=fence_token,
                        message=str(exc),
                        source_changed=isinstance(
                            exc,
                            NarrativeRevisionConflict,
                        ),
                        previous_status=previous_status,
                        previous_pause_reason=previous_pause_reason,
                        previous_execution_epoch=previous_epoch,
                        previous_expected_narrative_revision=(
                            previous_revision
                        ),
                    )
                    return False

            async def pause_parent() -> None:
                await generation_job_repo.update_job_fields(job_id, {
                    "status": "paused",
                    "pause_reason": "manual",
                    "current_chapter_id": None,
                    "active_slot": None,
                })

            async def abort_parent() -> None:
                await generation_job_repo.update_job_fields(job_id, {
                    "status": "aborted",
                    "pause_reason": None,
                    "current_chapter_id": None,
                    "active_slot": None,
                    "error": None,
                })

            await RequiredBookSuccessorJobRunner(
                job_id,
                deps=RequiredBookSuccessorJobDeps(
                    get_parent=lambda: generation_job_repo.get_job(job_id),
                    initialize=(
                        lambda: generation_job_repo
                        .initialize_required_book_successor(job_id)
                    ),
                    get_chapter=chapter_repo.get_chapter_by_id,
                    read_child=(
                        lambda child_id: generation_job_repo
                        .read_required_book_successor_child(job_id, child_id)
                    ),
                    create_child=create_child,
                    run_child=(
                        lambda child_id: GenerationJobService
                        ._run_required_book_successor_child(job_id, child_id)
                    ),
                    advance_child=(
                        lambda child_id: generation_job_repo
                        .advance_required_book_successor_child(job_id, child_id)
                    ),
                    block=(
                        lambda reason: generation_job_repo
                        .block_required_book_successor(job_id, reason)
                    ),
                    finalize_audit=finalize_required_book_audit,
                    pause_parent=pause_parent,
                    abort_parent=abort_parent,
                    pause_requested=lambda: control.pause_requested,
                    abort_requested=lambda: control.abort_requested,
                    pause_for_recovery_checkpoint=(
                        lambda journal_digest: generation_job_repo
                        .pause_required_book_successor_recovery_checkpoint(
                            job_id,
                            journal_digest,
                        )
                    ),
                ),
            ).run()

        deps = JobEngineDeps(
            list_worklist_chapters=_list_worklist,
            run_chapter=_run_chapter,
            run_candidate_chapter=_run_candidate_chapter,
            run_required_review_chapter=(
                _run_required_review_chapter
            ),
            run_required_state_chapter=(
                _run_required_state_chapter
            ),
            run_required_finalization_chapter=(
                _run_required_finalization_chapter
            ),
            run_required_book_successor=_run_required_book_successor,
            resolve_reference_card_blockers=_resolve_reference_card_blockers,
            inspect_book_completion=_inspect_book_completion,
            guard_book_completion_publication=(
                _guard_book_completion_publication
            ),
        )
        async def _heartbeat_execution(
            owner_task: asyncio.Task[Any],
            initial: JobExecutionLeaseV1,
        ) -> None:
            lease = initial
            interval = max(1, JOB_EXECUTION_LEASE_SECONDS // 3)
            while True:
                await asyncio.sleep(interval)
                now = get_utc_now()
                renewed = await generation_job_repo.heartbeat_execution_lease(
                    lease,
                    now=now,
                    expires_at=now + timedelta(
                        seconds=JOB_EXECUTION_LEASE_SECONDS
                    ),
                )
                if renewed is None:
                    owner_task.cancel()
                    return
                lease = renewed

        async def _run_owned_job() -> None:
            owner_task = asyncio.current_task()
            assert owner_task is not None
            heartbeat_task: asyncio.Task[None] | None = None
            try:
                with bind_job_execution(lease):
                    heartbeat_task = asyncio.create_task(
                        _heartbeat_execution(owner_task, lease)
                    )
                    await run_job(job_id, deps, control)
            except JobExecutionLeaseLost:
                logger.info("[job %s] execution lease was replaced", job_id)
            finally:
                if heartbeat_task is not None:
                    heartbeat_task.cancel()
                    try:
                        await heartbeat_task
                    except asyncio.CancelledError:
                        pass
                await generation_job_repo.release_execution_lease(lease)
                current = _REGISTRY.get(job_id)
                if current is not None and current[0] is owner_task:
                    _REGISTRY.pop(job_id, None)

        try:
            task = asyncio.create_task(_run_owned_job())
        except Exception:
            await generation_job_repo.release_execution_lease(lease)
            raise
        _REGISTRY[job_id] = (task, control)
        return True

    @staticmethod
    async def resume_after_reference_card_review(
        novel_id: str,
    ) -> ReferenceCardReviewResumeOutcome:
        """Resume the latest job paused only for a now-cleared card review."""
        if await emergent_reference_card_candidate_module.blocking_summary(
            novel_id
        ):
            return {
                "resumed_job_ids": [],
                "status": "not_resumed",
                "reason_codes": ["blocking_candidates_remaining"],
            }
        job_id: str | None = None
        async with _get_start_lock():
            if await emergent_reference_card_candidate_module.blocking_summary(
                novel_id
            ):
                return {
                    "resumed_job_ids": [],
                    "status": "not_resumed",
                    "reason_codes": ["blocking_candidates_remaining"],
                }
            jobs = await generation_job_repo.list_jobs_by_novel(novel_id)
            target = next(
                (
                    item
                    for item in jobs
                    if item.get("status") == "paused"
                    and (
                        item.get("pause_reason")
                        in {
                            "reference_card_review",
                            "reference_card_repair_exhausted",
                        }
                        or (
                            item.get("pause_reason") == "uncertain_attempt"
                            and isinstance(item.get("error"), Mapping)
                            and isinstance(
                                item["error"].get("auto_creation"),
                                Mapping,
                            )
                        )
                    )
                ),
                None,
            )
            if target is None:
                return {
                    "resumed_job_ids": [],
                    "status": "not_resumed",
                    "reason_codes": ["no_eligible_paused_job"],
                }
            job_id = str(target["_id"])
            expected_revision = target.get("expected_narrative_revision")
            current_revision = await narrative_revision_store.current(novel_id)
            if (
                type(expected_revision) is not int
                or expected_revision < 0
                or current_revision != expected_revision
            ):
                changed = await _pause_reference_card_review_for_source_change(
                    job_id,
                    target,
                    reason_codes=[
                        "narrative_revision_changed",
                        "successor_required",
                    ],
                )
                if not changed:
                    return {
                        "resumed_job_ids": [],
                        "status": "deferred",
                        "reason_codes": ["job_state_changed"],
                    }
                return {
                    "resumed_job_ids": [],
                    "status": "deferred",
                    "reason_codes": [
                        "narrative_revision_changed",
                        "successor_required",
                    ],
                }
            try:
                _validate_resumable_job_authorization(target)
            except ValueError:
                # The human card decision was already committed by the caller.
                # A legacy Job must not turn that successful mutation into an
                # HTTP 500; keep it paused and require a newly authorized Job.
                changed = await _pause_reference_card_review_for_source_change(
                    job_id,
                    target,
                    reason_codes=[
                        "authorization_invalid_or_missing",
                        "successor_required",
                    ],
                )
                if not changed:
                    return {
                        "resumed_job_ids": [],
                        "status": "deferred",
                        "reason_codes": ["job_state_changed"],
                    }
                return {
                    "resumed_job_ids": [],
                    "status": "deferred",
                    "reason_codes": [
                        "authorization_invalid_or_missing",
                        "successor_required",
                    ],
                }
            await GenerationJobService._guard_no_running()
            current_chapter_id = None
            raw_recovery = target.get("job_mutation_recovery")
            if raw_recovery is not None:
                try:
                    recovery = JobMutationRecoveryBindingV1.model_validate(
                        raw_recovery
                    )
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        "Generation job mutation recovery binding is invalid"
                    ) from exc
                if (
                    recovery.job_id != job_id
                    or recovery.novel_id != str(target.get("novel_id") or "")
                ):
                    raise ValueError(
                        "Generation job mutation recovery binding diverged"
                    )
                current_chapter_id = recovery.chapter_id
            previous_execution_epoch = target.get("execution_epoch", 0)
            if (
                type(previous_execution_epoch) is not int
                or previous_execution_epoch < 0
            ):
                raise ValueError("Generation job execution epoch is invalid")
            await generation_job_repo.transition_job_resume(
                job_id,
                {
                    "status": "running",
                    "pause_reason": None,
                    "error": None,
                    "active_slot": "global",
                    "current_chapter_id": current_chapter_id,
                },
                previous_status="paused",
                previous_execution_epoch=previous_execution_epoch,
            )
        control = JobControl()
        await GenerationJobService._spawn(job_id, control)
        return {
            "resumed_job_ids": [job_id],
            "status": "resumed",
            "reason_codes": [],
        }

    @staticmethod
    async def inspect_volume_readiness(
        volume_id: str,
        *,
        outline_deviation_policy: str = PAUSE_FOR_REWRITE,
        prose_continuation_policy: ProseContinuationPolicy | None = None,
        token_budget: int | None = None,
        generation_params: Mapping[str, Any] | None = None,
        reference_card_auto_creation_policy: (
            ReferenceCardAutoCreationPolicy | None
        ) = None,
    ) -> Dict[str, Any]:
        protected_generation_params = validate_protected_generation_params(
            generation_params
        )
        volume = await volume_repo.get_volume_by_id(volume_id)
        novel_id = str(volume["novel_id"])
        chapters = await ChapterService.get_chapters_by_volume(
            volume_id, include_content=True
        )
        chapters = await state_completion_module.attach_many(chapters)
        return await generation_readiness_module.inspect(
            novel_id=novel_id,
            scope="volume",
            volume_id=volume_id,
            chapters=chapters,
            outline_deviation_policy=outline_deviation_policy,
            prose_continuation_policy=prose_continuation_policy,
            token_budget=token_budget,
            generation_params=protected_generation_params,
            reference_card_auto_creation_policy=(
                reference_card_auto_creation_policy
            ),
        )

    @staticmethod
    async def inspect_book_readiness(
        novel_id: str,
        *,
        outline_deviation_policy: str = PAUSE_FOR_REWRITE,
        prose_continuation_policy: ProseContinuationPolicy | None = None,
        token_budget: int | None = None,
        generation_params: Mapping[str, Any] | None = None,
        reference_card_auto_creation_policy: (
            ReferenceCardAutoCreationPolicy | None
        ) = None,
    ) -> Dict[str, Any]:
        protected_generation_params = validate_protected_generation_params(
            generation_params
        )
        await novel_repo.get_novel_by_id(novel_id)
        chapters = await get_book_worklist(novel_id, include_content=True)
        structure_initialization = await inspect_book_structure_initialization(
            novel_id,
            generation_params=protected_generation_params,
        )
        return await generation_readiness_module.inspect(
            novel_id=novel_id,
            scope="book",
            volume_id=None,
            chapters=chapters,
            book_structure_initialization=structure_initialization,
            outline_deviation_policy=outline_deviation_policy,
            prose_continuation_policy=prose_continuation_policy,
            token_budget=token_budget,
            generation_params=protected_generation_params,
            reference_card_auto_creation_policy=(
                reference_card_auto_creation_policy
            ),
        )

    @staticmethod
    async def inspect_required_chapter_finalization_readiness(
        state_job_id: str,
        *,
        authorization_revision: int,
        created_at: datetime,
        deadline_at: datetime,
        generation_params: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Freeze a zero-Provider formal-write successor over both candidates."""

        validate_protected_generation_params(generation_params)
        state_job = await generation_job_repo.get_job(state_job_id)
        state_authority = validate_required_chapter_state_readiness(
            state_job.get("readiness")
        )
        reviewed_job = await generation_job_repo.get_job(
            state_authority.predecessor_candidate.job_id
        )
        return prepare_required_chapter_finalization_readiness(
            state_job,
            reviewed_job,
            authorization_revision=authorization_revision,
            created_at=created_at,
            deadline_at=deadline_at,
        )

    @staticmethod
    async def start_required_chapter_finalization_job(
        state_job_id: str,
        *,
        authorization_revision: int,
        created_at: datetime,
        deadline_at: datetime,
        readiness_digest: str,
        acknowledged_warning_codes: tuple[str, ...] | list[str],
        generation_params: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Create the formal successor only after its new digest is confirmed."""

        protected = _validate_start_authorization(
            token_budget=REQUIRED_CHAPTER_FINALIZATION_JOB_TOKEN_BUDGET,
            readiness_digest=readiness_digest,
            generation_params=generation_params,
        )
        async with _get_start_lock():
            await GenerationJobService._guard_no_running()
            report = await (
                GenerationJobService.inspect_required_chapter_finalization_readiness(
                    state_job_id,
                    authorization_revision=authorization_revision,
                    created_at=created_at,
                    deadline_at=deadline_at,
                    generation_params=protected,
                )
            )
            accepted = generation_readiness_module.authorize(
                report,
                supplied_digest=readiness_digest,
                acknowledged_warning_codes=acknowledged_warning_codes,
            )
            authority = validate_required_chapter_finalization_readiness(
                accepted
            )
            try:
                job_id = await generation_job_repo.create_job(
                    _new_job_doc(
                        authority.novel_id,
                        "book",
                        None,
                        None,
                        REQUIRED_CHAPTER_FINALIZATION_JOB_TOKEN_BUDGET,
                        0,
                        accepted,
                        PAUSE_FOR_REWRITE,
                        protected,
                        readiness_digest,
                    )
                )
            except DuplicateKeyError as exc:
                raise ConflictError(
                    "已有正在运行的批量作业，请先暂停或等待其结束"
                ) from exc
        control = JobControl()
        await GenerationJobService._spawn(job_id, control)
        return await generation_job_repo.get_job(job_id)

    @staticmethod
    async def resume_required_chapter_finalization_job(
        job_id: str,
    ) -> Dict[str, Any]:
        """Resume only the same frozen mutation; never rebuild its evidence."""

        async with _get_start_lock():
            job = await generation_job_repo.get_job(job_id)
            _reject_external_required_book_child_control(job)
            _validate_resumable_job_authorization(job)
            if not readiness_uses_required_chapter_finalization(
                job.get("readiness")
            ):
                raise ValueError("当前作业不是章节正式提交 successor")
            if job.get("required_chapter_finalization_result") is not None:
                if job.get("status") != "completed":
                    raise ValueError("章节正式提交结果与作业状态不一致")
                return job
            if job.get("has_uncertain_attempts"):
                raise ValueError("零 Provider 正式提交作业不得出现 uncertain 请求")
            if not job_planner.can_resume(str(job.get("status") or "")):
                raise ValueError(f"作业当前状态 {job.get('status')} 不可恢复")
            if job.get("job_mutation_recovery") is None:
                raise ValueError("章节正式提交没有可恢复的冻结 mutation")
            await GenerationJobService._guard_no_running()
            previous_epoch = job.get("execution_epoch", 0)
            if type(previous_epoch) is not int or previous_epoch < 0:
                raise ValueError("Generation job execution epoch is invalid")
            await generation_job_repo.transition_job_resume(
                job_id,
                {
                    "status": "running",
                    "pause_reason": None,
                    "error": None,
                    "active_slot": "global",
                    "has_uncertain_attempts": False,
                    "confirm_uncertain_prose_retry": False,
                },
                previous_status=str(job.get("status") or ""),
                previous_execution_epoch=previous_epoch,
            )
        control = JobControl()
        await GenerationJobService._spawn(job_id, control)
        return await generation_job_repo.get_job(job_id)

    @staticmethod
    async def inspect_required_chapter_state_readiness(
        predecessor_job_id: str,
        *,
        state_plan: GenerationPlan,
        token_budget: int,
        authorization_revision: int,
        created_at: datetime,
        deadline_at: datetime,
        generation_params: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Freeze the separate state-only successor over one reviewed result."""

        validate_protected_generation_params(generation_params)
        predecessor = await generation_job_repo.get_job(predecessor_job_id)
        return prepare_required_chapter_state_readiness(
            predecessor,
            state_plan=state_plan,
            token_budget=token_budget,
            authorization_revision=authorization_revision,
            created_at=created_at,
            deadline_at=deadline_at,
        )

    @staticmethod
    async def start_required_chapter_state_job(
        predecessor_job_id: str,
        *,
        state_plan: GenerationPlan,
        token_budget: int,
        authorization_revision: int,
        created_at: datetime,
        deadline_at: datetime,
        readiness_digest: str,
        acknowledged_warning_codes: tuple[str, ...] | list[str],
        generation_params: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Create and launch the explicitly authorized state-only Job."""

        protected = _validate_start_authorization(
            token_budget=token_budget,
            readiness_digest=readiness_digest,
            generation_params=generation_params,
        )
        async with _get_start_lock():
            await GenerationJobService._guard_no_running()
            report = await GenerationJobService.inspect_required_chapter_state_readiness(
                predecessor_job_id,
                state_plan=state_plan,
                token_budget=token_budget,
                authorization_revision=authorization_revision,
                created_at=created_at,
                deadline_at=deadline_at,
                generation_params=protected,
            )
            accepted = generation_readiness_module.authorize(
                report,
                supplied_digest=readiness_digest,
                acknowledged_warning_codes=acknowledged_warning_codes,
            )
            authority = validate_required_chapter_state_readiness(accepted)
            try:
                job_id = await generation_job_repo.create_job(
                    _new_job_doc(
                        authority.novel_id,
                        "book",
                        None,
                        None,
                        token_budget,
                        authority.maximum_provider_attempts_total,
                        accepted,
                        PAUSE_FOR_REWRITE,
                        protected,
                        readiness_digest,
                    )
                )
            except DuplicateKeyError as exc:
                raise ConflictError(
                    "已有正在运行的批量作业，请先暂停或等待其结束"
                ) from exc
        control = JobControl()
        await GenerationJobService._spawn(job_id, control)
        return await generation_job_repo.get_job(job_id)

    @staticmethod
    async def resume_required_chapter_state_job(
        job_id: str,
    ) -> Dict[str, Any]:
        """Resume only a non-terminal state journal under unchanged authority."""

        async with _get_start_lock():
            job = await generation_job_repo.get_job(job_id)
            _reject_external_required_book_child_control(job)
            _validate_resumable_job_authorization(job)
            if not readiness_uses_required_chapter_state(job.get("readiness")):
                raise ValueError("当前作业不是必需状态候选 successor")
            if job.get("required_state_candidate") is not None:
                if (
                    job.get("status") != "paused"
                    or job.get("pause_reason")
                    != REQUIRED_STATE_CANDIDATE_PAUSE_REASON
                ):
                    raise ValueError("状态候选结果与作业状态不一致")
                return job
            if job.get("has_uncertain_attempts"):
                raise ValueError(
                    "状态候选存在 uncertain 请求，原授权不允许自动重派发"
                )
            if not job_planner.can_resume(str(job.get("status") or "")):
                raise ValueError(f"作业当前状态 {job.get('status')} 不可恢复")
            if job.get("pause_reason") in {
                "required_state_blocked",
                "cost_cap",
                "attempt_capacity",
            }:
                raise ValueError(
                    "当前状态候选已在原授权边界内收敛，不能重复运行"
                )
            await GenerationJobService._guard_no_running()
            previous_epoch = job.get("execution_epoch", 0)
            if type(previous_epoch) is not int or previous_epoch < 0:
                raise ValueError("Generation job execution epoch is invalid")
            await generation_job_repo.transition_job_resume(
                job_id,
                {
                    "status": "running",
                    "pause_reason": None,
                    "error": None,
                    "active_slot": "global",
                    "last_checkpoint_index": 0,
                    "has_uncertain_attempts": False,
                    "confirm_uncertain_prose_retry": False,
                },
                previous_status=str(job.get("status") or ""),
                previous_execution_epoch=previous_epoch,
            )
        control = JobControl()
        await GenerationJobService._spawn(job_id, control)
        return await generation_job_repo.get_job(job_id)

    @staticmethod
    async def inspect_required_book_successor_readiness(
        novel_id: str,
        *,
        review_plan: RequiredChapterReviewPlan,
        state_plan: GenerationPlan,
        token_budget: int,
        authorization_revision: int,
        created_at: datetime,
        deadline_at: datetime,
        generation_params: Mapping[str, Any] | None = None,
        recovery_checkpoint: Literal[
            "none",
            "before_first_child",
        ] = REQUIRED_BOOK_SUCCESSOR_RECOVERY_CHECKPOINT_NONE,
    ) -> Dict[str, Any]:
        """Freeze the full review/state/formalization/audit book envelope."""

        review = await (
            GenerationJobService.inspect_required_chapter_review_book_readiness(
                novel_id,
                plan=review_plan,
                token_budget=token_budget,
                authorization_revision=authorization_revision,
                created_at=created_at,
                deadline_at=deadline_at,
                generation_params=generation_params,
            )
        )
        return prepare_required_book_successor_readiness(
            review,
            state_plan=state_plan,
            token_budget=token_budget,
            recovery_checkpoint=recovery_checkpoint,
        )

    @staticmethod
    async def start_required_book_successor_job(
        novel_id: str,
        *,
        review_plan: RequiredChapterReviewPlan,
        state_plan: GenerationPlan,
        token_budget: int,
        authorization_revision: int,
        created_at: datetime,
        deadline_at: datetime,
        readiness_digest: str,
        acknowledged_warning_codes: tuple[str, ...] | list[str],
        generation_params: Mapping[str, Any] | None = None,
        recovery_checkpoint: Literal[
            "none",
            "before_first_child",
        ] = REQUIRED_BOOK_SUCCESSOR_RECOVERY_CHECKPOINT_NONE,
        stable_job_id: str | None = None,
    ) -> Dict[str, Any]:
        """Start one explicitly-authorized root; children need no new grant.

        ``stable_job_id`` is reserved for the host acceptance parent.  It is
        legal only with the durable pre-child checkpoint and turns a lost host
        receipt into recovery of the same root instead of a second execution.
        """

        protected = _validate_start_authorization(
            token_budget=token_budget,
            readiness_digest=readiness_digest,
            generation_params=generation_params,
        )
        stable = str(stable_job_id or "") or None
        if stable is not None and (
            re.fullmatch(r"[0-9a-f]{24}", stable) is None
            or recovery_checkpoint
            != REQUIRED_BOOK_SUCCESSOR_RECOVERY_CHECKPOINT_BEFORE_FIRST_CHILD
        ):
            raise ValueError(
                "稳定根作业 ID 只允许用于首个子作业前恢复检查点"
            )
        should_spawn = False
        async with _get_start_lock():
            report = await (
                GenerationJobService.inspect_required_book_successor_readiness(
                    novel_id,
                    review_plan=review_plan,
                    state_plan=state_plan,
                    token_budget=token_budget,
                    authorization_revision=authorization_revision,
                    created_at=created_at,
                    deadline_at=deadline_at,
                    generation_params=protected,
                    recovery_checkpoint=recovery_checkpoint,
                )
            )
            accepted = generation_readiness_module.authorize(
                report,
                supplied_digest=readiness_digest,
                acknowledged_warning_codes=acknowledged_warning_codes,
            )
            authority = validate_required_book_successor_readiness(accepted)
            if REQUIRED_BOOK_SUCCESSOR_ACKNOWLEDGEMENT not in set(
                accepted.get("acknowledged_warning_codes") or []
            ):
                raise ValueError(
                    "整本 successor 必须显式确认正式正文与状态写入"
                )
            document = _new_job_doc(
                novel_id,
                "book",
                None,
                None,
                token_budget,
                authority.maximum_provider_attempts_total,
                accepted,
                PAUSE_FOR_REWRITE,
                protected,
                readiness_digest,
            )
            if stable is not None:
                document["_id"] = to_object_id(stable)

            async def recover_existing(
                existing: Mapping[str, Any],
            ) -> tuple[str, bool]:
                existing_id = str(existing.get("_id") or "")
                if stable is None or existing_id != stable:
                    raise ValueError(
                        "required book successor stable Job identity changed"
                    )
                replay = _stable_required_book_successor_replay_state(
                    existing,
                    expected_job=document,
                )
                if replay == "checkpoint_reached":
                    return existing_id, False
                status = str(existing.get("status") or "")
                if status in {"interrupted", "failed"}:
                    await GenerationJobService._guard_no_running()
                    previous_epoch = existing.get("execution_epoch")
                    assert type(previous_epoch) is int
                    await generation_job_repo.transition_job_resume(
                        existing_id,
                        {
                            "status": "running",
                            "pause_reason": None,
                            "error": None,
                            "active_slot": "global",
                            "has_uncertain_attempts": False,
                            "confirm_uncertain_prose_retry": False,
                        },
                        previous_status=status,
                        previous_execution_epoch=previous_epoch,
                    )
                return existing_id, True

            if stable is not None:
                existing = await generation_job_repo.find_one({
                    "_id": to_object_id(stable),
                })
                if existing is not None:
                    job_id, should_spawn = await recover_existing(existing)
                else:
                    await GenerationJobService._guard_no_running()
                    try:
                        job_id = await generation_job_repo.create_job(document)
                        should_spawn = True
                    except DuplicateKeyError as exc:
                        raced = await generation_job_repo.find_one({
                            "_id": to_object_id(stable),
                        })
                        if raced is None:
                            raise ConflictError(
                                "已有正在运行的批量作业，请先暂停或等待其结束"
                            ) from exc
                        job_id, should_spawn = await recover_existing(raced)
            else:
                await GenerationJobService._guard_no_running()
                try:
                    job_id = await generation_job_repo.create_job(document)
                    should_spawn = True
                except DuplicateKeyError as exc:
                    raise ConflictError(
                        "已有正在运行的批量作业，请先暂停或等待其结束"
                    ) from exc
        if should_spawn:
            control = JobControl()
            await GenerationJobService._spawn(job_id, control)
        return await generation_job_repo.get_job(job_id)

    @staticmethod
    async def resume_required_book_successor_job(
        job_id: str,
    ) -> Dict[str, Any]:
        """Resume the same root and the exact next child without reauthorization."""

        async with _get_start_lock():
            job = await generation_job_repo.get_job(job_id)
            _reject_external_required_book_child_control(job)
            _validate_resumable_job_authorization(job)
            if not readiness_uses_required_book_successor(job.get("readiness")):
                raise ValueError("当前作业不是整本 successor")
            authority = validate_required_book_successor_readiness(
                job["readiness"]
            )
            if get_utc_now() >= authority.deadline_at:
                raise ValueError("整本 successor 原授权已过期，必须重新检查")
            raw_journal = job.get("required_book_successor_journal")
            if raw_journal is not None:
                journal = parse_required_book_successor_journal(raw_journal)
                if journal.phase == "completed":
                    if job.get("status") != "completed":
                        raise ValueError("整本 successor 完成状态不一致")
                    return job
                if journal.phase == "blocked":
                    raise ValueError("整本 successor 已在原授权内失败关闭")
            if job.get("has_uncertain_attempts"):
                raise ValueError("整本 successor 存在未结算请求")
            if str(job.get("status") or "") not in {
                "paused",
                "interrupted",
                "failed",
            }:
                if job.get("status") == "running":
                    control = JobControl()
                    await GenerationJobService._spawn(job_id, control)
                    return await generation_job_repo.get_job(job_id)
                raise ValueError(f"作业当前状态 {job.get('status')} 不可恢复")
            await GenerationJobService._guard_no_running()
            previous_epoch = job.get("execution_epoch", 0)
            if type(previous_epoch) is not int or previous_epoch < 0:
                raise ValueError("Generation job execution epoch is invalid")
            await generation_job_repo.transition_job_resume(
                job_id,
                {
                    "status": "running",
                    "pause_reason": None,
                    "error": None,
                    "active_slot": "global",
                    "has_uncertain_attempts": False,
                    "confirm_uncertain_prose_retry": False,
                },
                previous_status=str(job.get("status") or ""),
                previous_execution_epoch=previous_epoch,
            )
        control = JobControl()
        await GenerationJobService._spawn(job_id, control)
        return await generation_job_repo.get_job(job_id)

    @staticmethod
    async def inspect_required_chapter_review_book_readiness(
        novel_id: str,
        *,
        plan: RequiredChapterReviewPlan,
        token_budget: int,
        authorization_revision: int,
        created_at: datetime,
        deadline_at: datetime,
        generation_params: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Build the opt-in, non-formal successor readiness for one book.

        The caller must provide every Provider plan and both timestamps. This
        Interface never chooses a reviewer model and can be recomputed at start
        against the same frozen inputs without changing the digest.
        """

        protected_generation_params = validate_protected_generation_params(
            generation_params
        )
        await novel_repo.get_novel_by_id(novel_id)
        chapters = await get_book_worklist(novel_id, include_content=True)
        target_chapters = [
            chapter
            for chapter in chapters
            if not str(chapter.get("content") or "").strip()
        ]
        if not target_chapters:
            raise ValueError(
                "本书没有可进入必需章节审查 successor 的空白正文章节"
            )
        base_report = await generation_readiness_module.inspect(
            novel_id=novel_id,
            scope="book",
            volume_id=None,
            chapters=chapters,
            book_structure_initialization=(
                await inspect_book_structure_initialization(
                    novel_id,
                    generation_params=protected_generation_params,
                )
            ),
            outline_deviation_policy=PAUSE_FOR_REWRITE,
            prose_continuation_policy=ProseContinuationPolicy(
                automatic_continuations_per_scene=0,
            ),
            token_budget=token_budget,
            generation_params=protected_generation_params,
            authorization_revision=authorization_revision,
            reference_card_auto_creation_policy=None,
        )
        return prepare_required_chapter_review_readiness(
            base_report,
            chapters=target_chapters,
            plan=plan,
            token_budget=token_budget,
            authorization_revision=authorization_revision,
            created_at=created_at,
            deadline_at=deadline_at,
        )

    @staticmethod
    async def start_required_chapter_review_book_job(
        novel_id: str,
        *,
        plan: RequiredChapterReviewPlan,
        token_budget: int,
        authorization_revision: int,
        created_at: datetime,
        deadline_at: datetime,
        readiness_digest: str,
        acknowledged_warning_codes: tuple[str, ...] | list[str],
        generation_params: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Create and launch the explicitly authorized successor Job."""

        protected_generation_params = _validate_start_authorization(
            token_budget=token_budget,
            readiness_digest=readiness_digest,
            generation_params=generation_params,
        )
        async with _get_start_lock():
            await GenerationJobService._guard_no_running()
            report = await (
                GenerationJobService.inspect_required_chapter_review_book_readiness(
                    novel_id,
                    plan=plan,
                    token_budget=token_budget,
                    authorization_revision=authorization_revision,
                    created_at=created_at,
                    deadline_at=deadline_at,
                    generation_params=protected_generation_params,
                )
            )
            accepted = generation_readiness_module.authorize(
                report,
                supplied_digest=readiness_digest,
                acknowledged_warning_codes=acknowledged_warning_codes,
            )
            authority = validate_required_chapter_review_readiness(accepted)
            try:
                job_id = await generation_job_repo.create_job(
                    _new_job_doc(
                        novel_id,
                        "book",
                        None,
                        None,
                        token_budget,
                        authority.maximum_provider_attempts_total,
                        accepted,
                        PAUSE_FOR_REWRITE,
                        protected_generation_params,
                        readiness_digest,
                    )
                )
            except DuplicateKeyError as exc:
                raise ConflictError(
                    "已有正在运行的批量作业，请先暂停或等待其结束"
                ) from exc
        control = JobControl()
        await GenerationJobService._spawn(job_id, control)
        return await generation_job_repo.get_job(job_id)

    @staticmethod
    async def resume_required_chapter_review_job(
        job_id: str,
    ) -> Dict[str, Any]:
        """Resume only a recoverable successor journal under its old authority."""

        async with _get_start_lock():
            job = await generation_job_repo.get_job(job_id)
            _reject_external_required_book_child_control(job)
            _validate_resumable_job_authorization(job)
            if not readiness_uses_required_chapter_review(
                job.get("readiness")
            ):
                raise ValueError("当前作业不是必需章节审查 successor")
            if job.get("required_reviewed_candidate") is not None:
                if (
                    job.get("status") != "paused"
                    or job.get("pause_reason")
                    != REQUIRED_REVIEWED_CANDIDATE_PAUSE_REASON
                ):
                    raise ValueError("已审查候选的作业状态不一致")
                return job
            if job.get("has_uncertain_attempts"):
                raise ValueError(
                    "必需审查存在 uncertain 请求，原授权不允许自动重派发"
                )
            if not job_planner.can_resume(str(job.get("status") or "")):
                raise ValueError(f"作业当前状态 {job.get('status')} 不可恢复")
            if job.get("pause_reason") in {
                "required_review_blocked",
                "cost_cap",
                "attempt_capacity",
            }:
                raise ValueError(
                    "当前必需审查已在原授权边界内收敛，不能重复运行"
                )
            await GenerationJobService._guard_no_running()
            previous_epoch = job.get("execution_epoch", 0)
            if type(previous_epoch) is not int or previous_epoch < 0:
                raise ValueError("Generation job execution epoch is invalid")
            await generation_job_repo.transition_job_resume(
                job_id,
                {
                    "status": "running",
                    "pause_reason": None,
                    "error": None,
                    "active_slot": "global",
                    "last_checkpoint_index": len(job.get("progress") or []),
                    "has_uncertain_attempts": False,
                    "confirm_uncertain_prose_retry": False,
                },
                previous_status=str(job.get("status") or ""),
                previous_execution_epoch=previous_epoch,
            )
        control = JobControl()
        await GenerationJobService._spawn(job_id, control)
        return await generation_job_repo.get_job(job_id)

    @staticmethod
    async def initialize_book_structure(
        novel_id: str,
        *,
        token_budget: int | None,
        readiness_digest: str | None,
        acknowledged_warning_codes: tuple[str, ...] | list[str] = (),
        outline_deviation_policy: str = PAUSE_FOR_REWRITE,
        generation_params: Mapping[str, Any] | None = None,
        prose_continuation_policy: ProseContinuationPolicy | None = None,
        reference_card_auto_creation_policy: (
            ReferenceCardAutoCreationPolicy | None
        ) = None,
    ) -> Dict[str, Any]:
        """Create initial volume/chapter stubs under the reviewed book digest."""

        protected_generation_params = _validate_start_authorization(
            token_budget=token_budget,
            readiness_digest=readiness_digest,
            generation_params=generation_params,
        )
        continuation_policy = (
            prose_continuation_policy or ProseContinuationPolicy()
        )
        generation_params_snapshot = {
            **protected_generation_params,
            "prose_continuation_policy": continuation_policy.to_dict(),
        }
        async with _get_start_lock():
            await GenerationJobService._guard_no_running()
            report = await GenerationJobService.inspect_book_readiness(
                novel_id,
                outline_deviation_policy=outline_deviation_policy,
                prose_continuation_policy=continuation_policy,
                token_budget=token_budget,
                generation_params=generation_params_snapshot,
                reference_card_auto_creation_policy=(
                    reference_card_auto_creation_policy
                ),
            )
            authorization = generation_readiness_module.authorize(
                report,
                supplied_digest=readiness_digest,
                acknowledged_warning_codes=acknowledged_warning_codes,
            )
            planning = authorization.get("planning")
            structure_authorization = (
                planning.get("book_structure_initialization")
                if isinstance(planning, Mapping)
                else None
            )
            if not isinstance(structure_authorization, Mapping):
                raise ValueError("当前整书预检不包含待生成的卷章结构")
            return await execute_book_structure_initialization(
                novel_id,
                authorization=structure_authorization,
                token_budget=token_budget,
                generation_params=generation_params_snapshot,
            )

    @staticmethod
    async def inspect_resume_readiness(
        job_id: str,
        *,
        prose_continuation_policy: (
            ProseContinuationPolicy | Mapping[str, Any] | None
        ) = None,
        token_budget: int | None = None,
        token_budget_provided: bool = False,
    ) -> Dict[str, Any]:
        """Preview a paused job's *next* authorization revision without writes.

        The revision is derived from the persisted job rather than supplied by
        the client.  That makes the returned digest usable only for the exact
        resume it previews and prevents a stale browser tab from authorizing a
        higher budget under an older revision.
        """
        job = await generation_job_repo.get_job(job_id)
        if not job_planner.can_resume(job["status"]):
            raise ValueError(f"作业当前状态 {job['status']} 不可恢复")
        protected_generation_params = _validate_resumable_job_authorization(
            job
        )
        if readiness_uses_required_chapter_review(job.get("readiness")):
            raise ValueError(
                "必需章节审查 successor 不接受旧式 reauthorization 预检"
            )
        if job.get("has_uncertain_attempts"):
            raise ValueError(
                "存在结果不确定的 Provider 请求，请先选择重试或跳过"
            )
        if job.get("candidate_manual_takeover") is not None:
            resolution = await (
                GenerationJobService._candidate_manual_takeover_resolution(job)
            )
            if resolution is None:
                raise ValueError(
                    "当前候选已转人工接管；请先在章节编辑器中补完并标记完成，"
                    "再重新预检"
                )
        authorization = dict(job.get("prose_continuation_authorization") or {})
        stored_policy = ProseContinuationPolicy.from_mapping(
            authorization.get("policy")
            or protected_generation_params.get(
                "prose_continuation_policy"
            )
        )
        requested_policy = prose_continuation_policy
        if requested_policy is not None and not isinstance(
            requested_policy,
            ProseContinuationPolicy,
        ):
            requested_policy = ProseContinuationPolicy.from_mapping(
                requested_policy
            )
        candidate_policy = requested_policy or stored_policy
        candidate_budget = (
            token_budget if token_budget_provided else job.get("token_budget")
        )
        generation_params_snapshot = {
            **protected_generation_params,
            "prose_continuation_policy": candidate_policy.to_dict(),
        }
        current_revision = max(
            int(job.get("authorization_revision") or 0),
            int(authorization.get("authorization_revision") or 0),
        )
        if job.get("scope") == "book":
            chapters = await get_book_worklist(
                str(job["novel_id"]),
                include_content=True,
            )
        else:
            chapters = await ChapterService.get_chapters_by_volume(
                str(job["volume_id"]),
                include_content=True,
            )
            chapters = await state_completion_module.attach_many(chapters)
        return await generation_readiness_module.inspect(
            novel_id=str(job["novel_id"]),
            scope=str(job["scope"]),
            volume_id=(
                str(job["volume_id"])
                if job.get("volume_id") is not None
                else None
            ),
            chapters=chapters,
            outline_deviation_policy=str(
                job.get("outline_deviation_policy") or PAUSE_FOR_REWRITE
            ),
            prose_continuation_policy=candidate_policy,
            token_budget=candidate_budget,
            generation_params=generation_params_snapshot,
            authorization_revision=max(1, current_revision + 1),
            reference_card_auto_creation_policy=(
                _persisted_reference_card_auto_creation_policy(job)
            ),
        )

    @staticmethod
    async def start_volume_job(volume_id: str, checkpoint_interval: Optional[int],
                               token_budget: Optional[int], *,
                               readiness_digest: str | None = None,
                               acknowledged_warning_codes: tuple[str, ...] | list[str] = (),
                               outline_deviation_policy: str = PAUSE_FOR_REWRITE,
                               generation_params: Mapping[str, Any] | None = None,
                                prose_continuation_policy: ProseContinuationPolicy | None = None,
                                reference_card_auto_creation_policy: (
                                    ReferenceCardAutoCreationPolicy | None
                                ) = None,
                                ) -> Dict[str, Any]:
        protected_generation_params = _validate_start_authorization(
            token_budget=token_budget,
            readiness_digest=readiness_digest,
            generation_params=generation_params,
        )
        volume = await volume_repo.get_volume_by_id(volume_id)  # 不存在抛 NotFoundError
        novel_id = str(volume["novel_id"])
        chapters = await ChapterService.get_chapters_by_volume(volume_id, include_content=True)
        chapters = await state_completion_module.attach_many(chapters)
        if job_planner.first_needing_work(chapters) is None:
            raise ValueError("本卷没有需要生成的章节（都已有正文与状态回填，或还没有章节存根）")
        async with _get_start_lock():
            await GenerationJobService._guard_no_running()
            # 在真正占用全局槽前重读全部输入并复算 digest，不能信任弹窗打开时的旧报告。
            chapters = await ChapterService.get_chapters_by_volume(
                volume_id, include_content=True
            )
            chapters = await state_completion_module.attach_many(chapters)
            continuation_policy = (
                prose_continuation_policy or ProseContinuationPolicy()
            )
            generation_params_snapshot = {
                **protected_generation_params,
                "prose_continuation_policy": continuation_policy.to_dict(),
            }
            report = await generation_readiness_module.inspect(
                novel_id=novel_id,
                scope="volume",
                volume_id=volume_id,
                chapters=chapters,
                outline_deviation_policy=outline_deviation_policy,
                prose_continuation_policy=continuation_policy,
                token_budget=token_budget,
                generation_params=generation_params_snapshot,
                reference_card_auto_creation_policy=(
                    reference_card_auto_creation_policy
                ),
            )
            authorization = generation_readiness_module.authorize(
                report,
                supplied_digest=readiness_digest,
                acknowledged_warning_codes=acknowledged_warning_codes,
            )
            capacity = max(
                int(
                    (authorization.get("planning") or {}).get(
                        "attempt_capacity"
                    )
                    or 0
                ),
                estimate_worklist_attempt_capacity(
                    chapters,
                    generation_params_snapshot,
                ),
            )
            try:
                job_id = await generation_job_repo.create_job(
                    _new_job_doc(
                        novel_id, "volume", volume_id, checkpoint_interval,
                        token_budget, capacity, authorization,
                        outline_deviation_policy,
                        generation_params_snapshot,
                        readiness_digest,
                    )
                )
            except DuplicateKeyError as exc:
                raise ConflictError("已有正在运行的批量作业，请先暂停或等待其结束") from exc
        control = JobControl()
        await GenerationJobService._spawn(job_id, control)
        return await generation_job_repo.get_job(job_id)

    @staticmethod
    async def start_book_job(novel_id: str, checkpoint_interval: Optional[int],
                             token_budget: Optional[int], *,
                             readiness_digest: str | None = None,
                             acknowledged_warning_codes: tuple[str, ...] | list[str] = (),
                             outline_deviation_policy: str = PAUSE_FOR_REWRITE,
                             generation_params: Mapping[str, Any] | None = None,
                             prose_continuation_policy: ProseContinuationPolicy | None = None,
                             reference_card_auto_creation_policy: (
                                 ReferenceCardAutoCreationPolicy | None
                             ) = None,
                             ) -> Dict[str, Any]:
        protected_generation_params = _validate_start_authorization(
            token_budget=token_budget,
            readiness_digest=readiness_digest,
            generation_params=generation_params,
        )
        await novel_repo.get_novel_by_id(novel_id)  # 不存在抛 NotFoundError → 404
        chapters = await get_book_worklist(novel_id, include_content=True)
        if job_planner.first_needing_work(chapters) is None:
            raise ValueError("本书没有需要生成的章节（所有卷的章节都已有正文与状态回填，或还没有章节存根）")
        async with _get_start_lock():
            await GenerationJobService._guard_no_running()
            chapters = await get_book_worklist(novel_id, include_content=True)
            continuation_policy = (
                prose_continuation_policy or ProseContinuationPolicy()
            )
            generation_params_snapshot = {
                **protected_generation_params,
                "prose_continuation_policy": continuation_policy.to_dict(),
            }
            structure_initialization = await inspect_book_structure_initialization(
                novel_id,
                generation_params=generation_params_snapshot,
            )
            report = await generation_readiness_module.inspect(
                novel_id=novel_id,
                scope="book",
                volume_id=None,
                chapters=chapters,
                book_structure_initialization=structure_initialization,
                outline_deviation_policy=outline_deviation_policy,
                prose_continuation_policy=continuation_policy,
                token_budget=token_budget,
                generation_params=generation_params_snapshot,
                reference_card_auto_creation_policy=(
                    reference_card_auto_creation_policy
                ),
            )
            authorization = generation_readiness_module.authorize(
                report,
                supplied_digest=readiness_digest,
                acknowledged_warning_codes=acknowledged_warning_codes,
            )
            capacity = max(
                int(
                    (authorization.get("planning") or {}).get(
                        "attempt_capacity"
                    )
                    or 0
                ),
                estimate_worklist_attempt_capacity(
                    chapters,
                    generation_params_snapshot,
                ),
            )
            try:
                job_id = await generation_job_repo.create_job(
                    _new_job_doc(
                        novel_id, "book", None, checkpoint_interval,
                        token_budget, capacity, authorization,
                        outline_deviation_policy,
                        generation_params_snapshot,
                        readiness_digest,
                    )
                )
            except DuplicateKeyError as exc:
                raise ConflictError("已有正在运行的批量作业，请先暂停或等待其结束") from exc
        control = JobControl()
        await GenerationJobService._spawn(job_id, control)
        return await generation_job_repo.get_job(job_id)

    @staticmethod
    async def resume_job(
        job_id: str,
        *,
        confirm_uncertain_retry: bool = False,
        skip_uncertain: bool = False,
        prose_continuation_policy: ProseContinuationPolicy | None = None,
        token_budget: int | None = None,
        token_budget_provided: bool = False,
        readiness_digest: str | None = None,
        acknowledged_warning_codes: tuple[str, ...] | list[str] | None = None,
    ) -> Dict[str, Any]:
        retry_resolution_to_launch: StateDispatchResolutionV3 | None = None
        async with _get_start_lock():
            job = await generation_job_repo.get_job(job_id)
            _reject_external_required_book_child_control(job)
            job_mutation_recovery_binding: JobMutationRecoveryBindingV1 | None = None
            state_dispatch_binding: JobMutationRecoveryBindingV1 | None = None
            raw_job_mutation_recovery = job.get("job_mutation_recovery")
            if raw_job_mutation_recovery is not None:
                try:
                    candidate_binding = JobMutationRecoveryBindingV1.model_validate(
                        raw_job_mutation_recovery
                    )
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        "Generation job mutation recovery binding is invalid"
                    ) from exc
                job_mutation_recovery_binding = candidate_binding
                if candidate_binding.operation == "accept_chapter_state":
                    state_dispatch_binding = _validated_state_dispatch_binding(
                        job_id=job_id,
                        job=job,
                        value=candidate_binding,
                    )
            pending_state_resolution = _persisted_state_dispatch_resolution(
                job_id=job_id,
                job=job,
            )
            pending_proposal_action = (
                await state_proposal_module.pending_job_bound_dispatch_action(
                    state_dispatch_binding
                )
                if state_dispatch_binding is not None
                else None
            )
            if (
                pending_state_resolution is not None
                and pending_state_resolution.phase != "terminal"
                and state_dispatch_binding != pending_state_resolution.binding
            ):
                raise ValueError(
                    "Generation job state dispatch resolution lost its binding"
                )
            if (
                not job_planner.can_resume(job["status"])
                and pending_state_resolution is None
            ):
                raise ValueError(f"作业当前状态 {job['status']} 不可恢复")
            protected_generation_params = _validate_resumable_job_authorization(
                job
            )
            readiness = job.get("readiness")
            if readiness_uses_required_book_successor(readiness):
                raise ValueError(
                    "整本 successor 必须使用其专用恢复接口"
                )
            if readiness_uses_required_chapter_finalization(readiness):
                raise ValueError(
                    "章节正式提交 successor 必须使用其专用恢复接口"
                )
            if readiness_uses_required_chapter_state(readiness):
                raise ValueError(
                    "必需状态候选 successor 必须使用其专用恢复接口"
                )
            if readiness_uses_required_chapter_review(readiness):
                raise ValueError(
                    "必需章节审查 successor 必须使用其专用恢复接口"
                )
            if job.get("pause_reason") == "final_audit":
                if (
                    confirm_uncertain_retry
                    or skip_uncertain
                    or prose_continuation_policy is not None
                    or token_budget_provided
                    or readiness_digest is not None
                    or acknowledged_warning_codes is not None
                ):
                    raise ValueError(
                        "final audit rerun does not accept Provider authorization"
                    )

                async def inspect_completion(fence):
                    return await book_completion_audit.inspect(
                        str(job["novel_id"]),
                        job_id=str(job_id),
                        expected_narrative_revision=fence.narrative_revision,
                        publication_fence_token=fence.fence_token,
                    )

                await finalize_book_job(
                    generation_job_repo,
                    job_id,
                    job,
                    inspect_completion,
                    lambda expected, token: book_completion_audit.publication_fence(
                        str(job["novel_id"]),
                        str(job_id),
                        expected_narrative_revision=expected,
                        fence_token=token,
                    ),
                )
                return await generation_job_repo.get_job(job_id)
            unresolved_state_dispatch = bool(
                pending_state_resolution is not None
                or pending_proposal_action is not None
                or (
                    state_dispatch_binding is not None
                    and await state_proposal_module.has_unresolved_job_dispatch(
                        state_dispatch_binding
                    )
                )
            )
            candidate_prefix = job.get("candidate_pipeline_checkpoints")
            candidate_finalization_recovery_available = False
            if (
                job.get("pause_reason") == "source_changed"
                and isinstance(candidate_prefix, list)
                and candidate_prefix
            ):
                candidate_finalization_recovery_available = await (
                    _candidate_finalization_recovery_available(job_id, job)
                )
                if not candidate_finalization_recovery_available:
                    raise ValueError(
                        "候选检查点绑定的 narrative revision 已失效；"
                        "请终止该作业并以新 readiness 启动 successor 作业"
                    )
            resolved_candidate_manual_takeover: (
                CandidateManualTakeoverResolutionV1 | None
            ) = None
            if job.get("candidate_manual_takeover") is not None:
                resolved_candidate_manual_takeover = await (
                    GenerationJobService._candidate_manual_takeover_resolution(
                        job
                    )
                )
                if resolved_candidate_manual_takeover is None:
                    raise ValueError(
                        "当前候选已转人工接管；请先在章节编辑器中补完并标记完成，"
                        "再重新预检并继续"
                    )
            if confirm_uncertain_retry and skip_uncertain:
                raise ValueError(
                    "confirm_uncertain_retry and skip_uncertain are mutually exclusive"
                )
            persisted_state_action = (
                pending_state_resolution.action
                if pending_state_resolution is not None
                else pending_proposal_action
            )
            if persisted_state_action is not None:
                supplied_action = (
                    "retry"
                    if confirm_uncertain_retry
                    else "skip"
                    if skip_uncertain
                    else None
                )
                if (
                    supplied_action is not None
                    and supplied_action != persisted_state_action
                ):
                    raise ValueError(
                        "Generation job state dispatch resolution action diverged"
                    )
                confirm_uncertain_retry = (
                    persisted_state_action == "retry"
                )
                skip_uncertain = persisted_state_action == "skip"
            reauthorization_payload_supplied = (
                prose_continuation_policy is not None
                or token_budget_provided
                or readiness_digest is not None
                or acknowledged_warning_codes is not None
            )
            if (
                (confirm_uncertain_retry or skip_uncertain)
                and reauthorization_payload_supplied
            ):
                raise ValueError(
                    "uncertain-attempt recovery and re-authorized resume are mutually exclusive"
                )
            if (
                pending_state_resolution is not None
                and pending_state_resolution.phase == "terminal"
            ):
                if reauthorization_payload_supplied:
                    raise ValueError(
                        "terminal state dispatch resolution cannot be re-authorized"
                    )
                return job
            if (
                persisted_state_action == "abort"
            ):
                if reauthorization_payload_supplied:
                    raise ValueError(
                        "aborted state dispatch resolution cannot be re-authorized"
                    )
                if state_dispatch_binding is None:
                    raise ValueError(
                        "Generation job state dispatch binding disappeared"
                    )
                await _advance_state_dispatch_resolution(
                    job_id=job_id,
                    binding=state_dispatch_binding,
                    action="abort",
                )
                return await generation_job_repo.get_job(job_id)
            if (
                (confirm_uncertain_retry or skip_uncertain)
                and not (
                    job.get("has_uncertain_attempts")
                    or unresolved_state_dispatch
                )
            ):
                raise ValueError(
                    "uncertain-attempt recovery requires an uncertain Provider attempt"
                )
            if (
                job.get("has_uncertain_attempts") or unresolved_state_dispatch
            ) and not confirm_uncertain_retry:
                if skip_uncertain:
                    if state_dispatch_binding is not None:
                        await _advance_state_dispatch_resolution(
                            job_id=job_id,
                            binding=state_dispatch_binding,
                            action="skip",
                        )
                    else:
                        await generation_job_repo.acknowledge_uncertain_attempts(
                            job_id,
                            "skip",
                        )
                        await generation_job_repo.update_job_fields(job_id, {
                            "status": "failed",
                            "pause_reason": "uncertain_skipped",
                            "active_slot": None,
                            "current_chapter_id": None,
                            "error": {
                                "step": "uncertain_attempt",
                                "message": (
                                    "用户选择跳过可能已发出的 Provider 请求；"
                                    "请人工检查章节后再恢复"
                                ),
                            },
                        })
                    return await generation_job_repo.get_job(job_id)
                raise ValueError(
                    "存在请求已发出但未取得 usage 的 attempt，可能已计费；"
                    "请明确确认可能重复计费后再重试"
                )
            authorization_updates: Dict[str, Any] = {}
            reauthorized_revision: int | None = None
            resolved_job_mutation_recovery: (
                JobMutationRecoveryBindingV1 | None
            ) = None
            authorization = dict(job.get("prose_continuation_authorization") or {})
            stored_policy = ProseContinuationPolicy.from_mapping(
                authorization.get("policy")
                or protected_generation_params.get(
                    "prose_continuation_policy"
                )
            )
            candidate_policy = prose_continuation_policy or stored_policy
            authorization_ruleset_changed = authorization_ruleset_requires_refresh(
                authorization,
                policy=candidate_policy,
            )
            authorization_confirmation_required = bool(
                job.get("authorization_confirmation_required")
            ) and not candidate_finalization_recovery_available
            source_change_requires_reauthorization = (
                job.get("pause_reason") == "source_changed"
                and not candidate_finalization_recovery_available
            )
            authorization_settings_changed = (
                prose_continuation_policy is not None
                or token_budget_provided
                or authorization_confirmation_required
                or source_change_requires_reauthorization
                or (
                    authorization_ruleset_changed
                    and not (confirm_uncertain_retry or skip_uncertain)
                )
            )
            if (
                not authorization_settings_changed
                and (readiness_digest is not None or acknowledged_warning_codes is not None)
            ):
                raise ValueError(
                    "readiness confirmation is only valid for a re-authorized resume"
                )
            if authorization_settings_changed:
                if readiness_digest is None:
                    raise ResumeReadinessRequired(
                        "继续前需要查看并确认当前自动续写调用容量与 token 预算"
                    )
                candidate_budget = (
                    token_budget if token_budget_provided else job.get("token_budget")
                )
                generation_params_snapshot = {
                    **protected_generation_params,
                    "prose_continuation_policy": candidate_policy.to_dict(),
                }
                generation_params_snapshot = _validate_start_authorization(
                    token_budget=candidate_budget,
                    readiness_digest=readiness_digest,
                    generation_params=generation_params_snapshot,
                )
                if job_mutation_recovery_binding is not None:
                    recovered_revision = await _recover_job_mutation_revision(
                        job_mutation_recovery_binding
                    )
                    current_narrative_revision = await (
                        narrative_revision_store.current(
                            str(job["novel_id"])
                        )
                    )
                    if recovered_revision is None:
                        raise ValueError(
                            "当前作业仍有未完成的章节正式写回，不能替换旧快照；"
                            "请先处理当前章节，或终止旧作业后重新预检"
                        )
                    if (
                        recovered_revision
                        <= job_mutation_recovery_binding.expected_narrative_revision
                        or current_narrative_revision < recovered_revision
                    ):
                        raise ValueError(
                            "Generation job mutation recovery revision is invalid"
                        )
                    resolved_job_mutation_recovery = (
                        job_mutation_recovery_binding
                    )
                current_revision = max(
                    int(job.get("authorization_revision") or 0),
                    int(authorization.get("authorization_revision") or 0),
                )
                if job.get("scope") == "book":
                    chapters = await get_book_worklist(
                        str(job["novel_id"]),
                        include_content=True,
                    )
                else:
                    chapters = await ChapterService.get_chapters_by_volume(
                        str(job["volume_id"]),
                        include_content=True,
                    )
                    chapters = await state_completion_module.attach_many(chapters)
                report = await generation_readiness_module.inspect(
                    novel_id=str(job["novel_id"]),
                    scope=str(job["scope"]),
                    volume_id=(
                        str(job["volume_id"])
                        if job.get("volume_id") is not None
                        else None
                    ),
                    chapters=chapters,
                    outline_deviation_policy=str(
                        job.get("outline_deviation_policy") or PAUSE_FOR_REWRITE
                    ),
                    prose_continuation_policy=candidate_policy,
                    token_budget=candidate_budget,
                    generation_params=generation_params_snapshot,
                    authorization_revision=max(1, current_revision + 1),
                    reference_card_auto_creation_policy=(
                        _persisted_reference_card_auto_creation_policy(job)
                    ),
                )
                accepted_readiness = generation_readiness_module.authorize(
                    report,
                    supplied_digest=readiness_digest,
                    acknowledged_warning_codes=acknowledged_warning_codes or (),
                )
                accepted_resources = accepted_readiness.get("resources")
                reauthorized_revision = (
                    accepted_resources.get("narrative_revision")
                    if isinstance(accepted_resources, Mapping)
                    else None
                )
                if (
                    type(reauthorized_revision) is not int
                    or reauthorized_revision < 0
                ):
                    accepted_planning = accepted_readiness.get("planning")
                    if (
                        isinstance(accepted_planning, Mapping)
                        and "chapter_candidate_pipeline_revision"
                        in accepted_planning
                    ):
                        raise ValueError(
                            "re-authorized readiness narrative revision is invalid"
                        )
                    stored_revision = job.get("expected_narrative_revision")
                    reauthorized_revision = (
                        stored_revision
                        if type(stored_revision) is int and stored_revision >= 0
                        else None
                    )
                if (
                    resolved_candidate_manual_takeover is not None
                    and reauthorized_revision
                    != resolved_candidate_manual_takeover.narrative_revision
                ):
                    raise ValueError(
                        "Candidate manual takeover resolution is stale against "
                        "the accepted readiness"
                    )
                remaining_capacity = max(
                    int(
                        (accepted_readiness.get("planning") or {}).get(
                            "attempt_capacity"
                        )
                        or 0
                    ),
                    estimate_worklist_attempt_capacity(
                        chapters,
                        generation_params_snapshot,
                    ),
                )
                authorization_updates = {
                    "token_budget": candidate_budget,
                    "generation_params": generation_params_snapshot,
                    "batch_authorization_contract": (
                        build_batch_generation_authorization_contract(
                            readiness_digest=readiness_digest,
                            token_budget=candidate_budget,
                        )
                    ),
                    "prose_continuation_authorization": dict(
                        (accepted_readiness.get("planning") or {}).get(
                            "prose_continuation_authorization"
                        )
                        or {}
                    ),
                    "authorization_revision": max(1, current_revision + 1),
                    "readiness": accepted_readiness,
                    "authorization_confirmation_required": None,
                    # Historical claims are never rolled back. The new policy
                    # governs only the work still visible in the current list.
                    "usage_attempt_capacity": max(
                        int(job.get("usage_attempt_claimed") or 0),
                        int(job.get("usage_attempt_claimed") or 0)
                        + remaining_capacity,
                    ),
                    # A prior reservation may have been sized for the old policy.
                    # Clearing it forces the runner to reserve against the new cap.
                    "attempt_reservation": None,
                }

            incomplete_prose_updates: Dict[str, Any] = {}
            pending_incomplete_prose = dict(job.get("incomplete_prose") or {})
            if pending_incomplete_prose:
                manually_resolved = await GenerationJobService._incomplete_prose_is_resolved(
                    job
                )
                can_use_new_automatic_policy = bool(
                    authorization_settings_changed
                    and candidate_policy is not None
                    and (
                        prose_continuation_policy is not None
                        or authorization_ruleset_changed
                    )
                    and GenerationJobService._pending_scene_can_use_new_policy(
                        pending_incomplete_prose,
                        candidate_policy,
                        allow_divergence_stop=authorization_ruleset_changed,
                    )
                )
                if not manually_resolved and not can_use_new_automatic_policy:
                    raise ValueError(
                        "The incomplete prose scene must be manually continued and accepted "
                        "before this batch job can resume"
                    )
                incomplete_prose_updates["incomplete_prose"] = None

            retry_already_transitioned = bool(
                pending_state_resolution is not None
                and pending_state_resolution.action == "retry"
                and pending_state_resolution.phase == "job_transitioned"
                and job.get("status") == "running"
            )
            if not retry_already_transitioned:
                await GenerationJobService._guard_no_running()
            # 任何 resume 把检查点窗口推进到当前 progress 长度（设计 §7）。
            resume_fields = {
                **authorization_updates,
                **incomplete_prose_updates,
                "status": "running", "pause_reason": None, "error": None,
                **(
                    {"authorization_confirmation_required": None}
                    if candidate_finalization_recovery_available
                    else {}
                ),
                "active_slot": "global",
                "has_uncertain_attempts": False if confirm_uncertain_retry else bool(
                    job.get("has_uncertain_attempts")
                ),
                "confirm_uncertain_prose_retry": bool(confirm_uncertain_retry),
                "last_checkpoint_index": len(job.get("progress", [])),
            }
            if confirm_uncertain_retry and state_dispatch_binding is not None:
                if authorization_updates:
                    raise ValueError(
                        "state dispatch retry cannot replace its frozen authorization"
                    )
                retry_resolution_to_launch = await (
                    _advance_state_dispatch_resolution(
                        job_id=job_id,
                        binding=state_dispatch_binding,
                        action="retry",
                        last_checkpoint_index=resume_fields[
                            "last_checkpoint_index"
                        ],
                    )
                )
            elif authorization_updates and reauthorized_revision is not None:
                previous_revision = job.get("expected_narrative_revision")
                if previous_revision is not None and type(previous_revision) is not int:
                    raise ValueError(
                        "Generation job narrative revision cursor is invalid"
                    )
                previous_authorization_revision = job.get(
                    "authorization_revision"
                )
                if (
                    previous_authorization_revision is not None
                    and type(previous_authorization_revision) is not int
                ):
                    raise ValueError(
                        "Generation job authorization revision is invalid"
                    )
                previous_readiness = job.get("readiness")
                previous_readiness_digest = (
                    previous_readiness.get("digest")
                    if isinstance(previous_readiness, Mapping)
                    else None
                )
                if (
                    previous_readiness_digest is not None
                    and not isinstance(previous_readiness_digest, str)
                ):
                    raise ValueError("Generation job readiness digest is invalid")
                previous_active_slot = job.get("active_slot")
                if (
                    previous_active_slot is not None
                    and not isinstance(previous_active_slot, str)
                ):
                    raise ValueError("Generation job active slot is invalid")
                previous_execution_epoch = job.get("execution_epoch", 0)
                if (
                    type(previous_execution_epoch) is not int
                    or previous_execution_epoch < 0
                ):
                    raise ValueError(
                        "Generation job execution epoch is invalid"
                    )
                update_authorization_kwargs: dict[str, Any] = {
                    "previous_revision": previous_revision,
                    "next_revision": reauthorized_revision,
                    "previous_status": str(job.get("status") or ""),
                    "previous_authorization_revision": (
                        previous_authorization_revision
                    ),
                    "previous_readiness_digest": previous_readiness_digest,
                    "previous_active_slot": previous_active_slot,
                    "previous_execution_epoch": previous_execution_epoch,
                }
                if resolved_job_mutation_recovery is not None:
                    update_authorization_kwargs[
                        "resolved_job_mutation_recovery"
                    ] = resolved_job_mutation_recovery
                if resolved_candidate_manual_takeover is not None:
                    update_authorization_kwargs[
                        "resolved_candidate_manual_takeover"
                    ] = resolved_candidate_manual_takeover
                await generation_job_repo.update_job_authorization(
                    job_id,
                    resume_fields,
                    **update_authorization_kwargs,
                )
            else:
                if job.get("has_uncertain_attempts") and confirm_uncertain_retry:
                    await generation_job_repo.acknowledge_uncertain_attempts(
                        job_id,
                        "retry",
                    )
                previous_execution_epoch = job.get("execution_epoch", 0)
                if (
                    type(previous_execution_epoch) is not int
                    or previous_execution_epoch < 0
                ):
                    raise ValueError(
                        "Generation job execution epoch is invalid"
                    )
                await generation_job_repo.transition_job_resume(
                    job_id,
                    resume_fields,
                    previous_status=str(job.get("status") or ""),
                    previous_execution_epoch=previous_execution_epoch,
                )
        if retry_resolution_to_launch is not None:
            control = JobControl()
            await GenerationJobService._spawn(job_id, control)
            return await generation_job_repo.get_job(job_id)

        control = JobControl()
        await GenerationJobService._spawn(job_id, control)
        return await generation_job_repo.get_job(job_id)

    @staticmethod
    async def wait_for_worker_shutdown(job_id: str) -> None:
        """Wait until the registered worker can no longer mutate Job state."""
        entry = _REGISTRY.get(job_id)
        if entry is None:
            return
        task = entry[0]
        if task is None or task is asyncio.current_task():
            return
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.cancelled():
                raise
        except JobExecutionLeaseLost:
            pass
        except Exception:  # noqa: BLE001 - completion still proves quiescence
            logger.exception(
                "[job %s] worker failed before reaching shutdown",
                job_id,
            )

    @staticmethod
    async def pause_job(job_id: str) -> Dict[str, Any]:
        job = await generation_job_repo.get_job(job_id)
        _reject_external_required_book_child_control(job)
        entry = _REGISTRY.get(job_id)
        if entry is not None:
            entry[1].pause_requested = True
        return await generation_job_repo.get_job(job_id)

    @staticmethod
    async def abort_job(job_id: str) -> Dict[str, Any]:
        job = await generation_job_repo.get_job(job_id)
        _reject_external_required_book_child_control(job)
        state_dispatch_binding: JobMutationRecoveryBindingV1 | None = None
        raw_job_mutation_recovery = job.get("job_mutation_recovery")
        if raw_job_mutation_recovery is not None:
            try:
                candidate_binding = JobMutationRecoveryBindingV1.model_validate(
                    raw_job_mutation_recovery
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "Generation job mutation recovery binding is invalid"
                ) from exc
            if candidate_binding.operation == "accept_chapter_state":
                state_dispatch_binding = _validated_state_dispatch_binding(
                    job_id=job_id,
                    job=job,
                    value=candidate_binding,
                )
        pending_resolution = _persisted_state_dispatch_resolution(
            job_id=job_id,
            job=job,
        )
        if pending_resolution is not None and pending_resolution.action != "abort":
            raise ValueError(
                "Generation job state dispatch resolution action diverged"
            )
        resolution_is_terminal = (
            pending_resolution is not None
            and pending_resolution.phase == "terminal"
        )
        if not resolution_is_terminal:
            if state_dispatch_binding is not None:
                await _advance_state_dispatch_resolution(
                    job_id=job_id,
                    binding=state_dispatch_binding,
                    action="abort",
                )
            else:
                await generation_job_repo.complete_job_abort(job_id)
        entry = _REGISTRY.get(job_id)
        if entry is not None:
            entry[1].abort_requested = True
            task = entry[0]
            if (
                task is not None
                and task is not asyncio.current_task()
                and not task.done()
            ):
                task.cancel()
            await GenerationJobService.wait_for_worker_shutdown(job_id)
        return await generation_job_repo.get_job(job_id)

    @staticmethod
    async def get_job(job_id: str) -> Dict[str, Any]:
        job = await generation_job_repo.get_job(job_id)
        return await _with_recovery_capabilities(str(job_id), job)

    @staticmethod
    async def list_jobs(novel_id: str) -> List[Dict[str, Any]]:
        jobs = await generation_job_repo.list_jobs_by_novel(novel_id)
        return list(await asyncio.gather(*(
            _with_recovery_capabilities(str(job["_id"]), job)
            for job in jobs
        )))

    @staticmethod
    async def summarize_diagnostics(
        novel_id: str,
        *,
        limit: int = 30,
    ) -> Dict[str, Any]:
        jobs = await generation_job_repo.list_jobs_by_novel(
            novel_id,
            limit=limit,
        )
        return summarize_jobs(jobs, limit=limit)
