"""批量作业服务：CRUD、全局单作业守卫、拉起/控制进程内任务。"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
import time
from dataclasses import replace
from datetime import timedelta
from typing import Any, Dict, List, Literal, Mapping, Optional, TypedDict

from pymongo.errors import DuplicateKeyError

from backend.db.mutation import MutationConflictError
from backend.db.repositories.generation_job_repository import (
    AttemptCapacityExceeded,
    TokenBudgetExceeded,
    generation_job_repo,
)
from backend.db.narrative_revision import narrative_revision_store
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
from backend.services.generation.chapter_candidate_job import (
    CandidateJobExecution,
    ChapterCandidateJobRunner,
    ChapterCandidateJobRunnerDeps,
)
from backend.services.generation.candidate_repair_contracts import (
    JobMutationRecoveryBindingV1,
    JobMutationReceiptV1,
    StateDispatchResolutionV3,
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
from backend.services.generation.failure_diagnostics import summarize_jobs
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
from backend.services.novel.book_completion import book_completion_audit
from backend.db.repositories.volume_repository import volume_repo
from backend.db.repositories.novel_repository import novel_repo
from backend.services.generation.book_worklist import get_book_worklist
from backend.services.generation.book_structure_initialization import (
    initialize_book_structure as execute_book_structure_initialization,
    inspect_book_structure_initialization,
)
from backend.services.generation.readiness import generation_readiness_module
from backend.services.novel.state_completion import state_completion_module
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
        if not readiness_uses_candidate_pipeline(readiness):
            raise ValueError("legacy execution protocol")
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


async def _recover_job_mutation_revision(
    binding: JobMutationRecoveryBindingV1,
) -> int | None:
    # Lazy import keeps the recovery registry from forming a service import cycle.
    from backend.services.novel.mutation_recovery import (
        recover_bound_mutation_revision,
    )

    return await recover_bound_mutation_revision(binding)


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
    candidate_readiness = (
        isinstance(planning, Mapping)
        and "chapter_candidate_pipeline_revision" in planning
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
        "candidate_pipeline_checkpoints": [],
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
        "authorization_revision": int(((readiness.get("planning") or {}).get(
            "prose_continuation_authorization"
        ) or {}).get("authorization_revision") or 0),
        "readiness": readiness,
    }
    if type(expected_revision) is int and expected_revision >= 0:
        document["expected_narrative_revision"] = expected_revision
    return document


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

        recorded_repair_cycles = {
            int(event.get("cycle"))
            for event in list(job.get("reference_card_repair_events") or [])
            if isinstance(event, Mapping)
            and type(event.get("cycle")) is int
            and str(event.get("chapter_id") or "") == current_chapter_id
            and str(event.get("authorization_digest") or "")
            == str((authorization or {}).get("authorization_digest") or "")
        }
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
        last_repair: dict[str, Any] | None = None
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
            except TokenBudgetExceeded:
                return {
                    **live_blockers,
                    "auto_creation": {
                        "outcome": "repair_exhausted",
                        "pause_reason": "cost_cap",
                        "created_count": 0,
                        "deny_reasons": list(event["deny_reasons"]),
                        "denials": latest_denials,
                    },
                }
            except AttemptCapacityExceeded:
                return {
                    **live_blockers,
                    "auto_creation": {
                        "outcome": "repair_exhausted",
                        "pause_reason": "attempt_capacity",
                        "created_count": 0,
                        "deny_reasons": list(event["deny_reasons"]),
                        "denials": latest_denials,
                    },
                }
            except MutationConflictError:
                return {
                    **live_blockers,
                    "auto_creation": {
                        "outcome": "repair_exhausted",
                        "pause_reason": "source_changed",
                        "created_count": 0,
                        "deny_reasons": list(event["deny_reasons"]),
                        "denials": latest_denials,
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
    def _pending_scene_can_use_new_policy(
        pending: Mapping[str, Any],
        policy: ProseContinuationPolicy,
        *,
        allow_divergence_stop: bool = False,
    ) -> bool:
        pause_reason = str(pending.get("pause_reason") or "")
        if pause_reason != "automatic_continuations_exhausted" and not (
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
                cycles, adherence_plan, state_plan = (
                    repairs.execution_snapshot(chapter_id=chapter_id)
                )
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
                    max_repair_cycles=cycles,
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

            async def finalize_candidate(
                *,
                owner_id: str,
                novel_id: str,
                chapter: Mapping[str, Any],
                source,
                adherence: Mapping[str, Any],
                state: Mapping[str, Any],
                repair_cycles_used: int,
            ) -> Mapping[str, Any]:
                del novel_id
                planning = readiness.get("planning")
                if not isinstance(planning, Mapping):
                    raise ValueError("candidate finalization planning is invalid")
                frozen = parse_chapter_finalization_authorization(
                    planning.get("chapter_finalization_authorization")
                )
                proposal_id = state.get("proposal_id")
                acceptance_token = state.get("acceptance_token")
                if not isinstance(proposal_id, str) or not isinstance(
                    acceptance_token, str
                ):
                    raise ValueError("candidate state receipt is invalid")
                readiness_digest = readiness.get("digest")
                if not isinstance(readiness_digest, str) or not readiness_digest:
                    raise ValueError("candidate readiness digest is invalid")
                return await chapter_finalization_service.commit(
                    owner_id=owner_id,
                    chapter_id=str(chapter.get("_id") or ""),
                    prose_run_id=source.source_run_id,
                    prose_run_revision=source.source_run_revision,
                    state_proposal_id=proposal_id,
                    state_acceptance_token=acceptance_token,
                    authorization=ChapterFinalizationAuthorization(
                        job_id=job_id,
                        readiness_digest=readiness_digest,
                        authorization_revision=frozen[
                            "authorization_revision"
                        ],
                    ),
                    evidence=ChapterFinalizationEvidence(
                        outline_adherence=dict(adherence),
                        repair_cycles_used=repair_cycles_used,
                    ),
                )

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

        deps = JobEngineDeps(
            list_worklist_chapters=_list_worklist,
            run_chapter=_run_chapter,
            run_candidate_chapter=_run_candidate_chapter,
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
                await generation_job_repo.update_job_fields(
                    job_id,
                    {
                        "status": "paused",
                        "pause_reason": "source_changed",
                        "active_slot": None,
                        "error": {
                            "step": "source_changed",
                            "reason_codes": [
                                "narrative_revision_changed",
                                "successor_required",
                            ],
                        },
                    },
                )
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
                await generation_job_repo.update_job_fields(
                    job_id,
                    {
                        "status": "paused",
                        "pause_reason": "source_changed",
                        "active_slot": None,
                        "error": {
                            "step": "source_changed",
                            "reason_codes": [
                                "authorization_invalid_or_missing",
                                "successor_required",
                            ],
                        },
                    },
                )
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
        if job.get("has_uncertain_attempts"):
            raise ValueError(
                "存在结果不确定的 Provider 请求，请先选择重试或跳过"
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
            if (
                job.get("pause_reason") == "source_changed"
                and isinstance(candidate_prefix, list)
                and candidate_prefix
            ):
                raise ValueError(
                    "候选检查点绑定的 narrative revision 已失效；"
                    "请终止该作业并以新 readiness 启动 successor 作业"
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
            )
            source_change_requires_reauthorization = (
                job.get("pause_reason") == "source_changed"
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
    async def pause_job(job_id: str) -> Dict[str, Any]:
        await generation_job_repo.get_job(job_id)
        entry = _REGISTRY.get(job_id)
        if entry is not None:
            entry[1].pause_requested = True
        return await generation_job_repo.get_job(job_id)

    @staticmethod
    async def abort_job(job_id: str) -> Dict[str, Any]:
        job = await generation_job_repo.get_job(job_id)
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
        if (
            pending_resolution is not None
            and pending_resolution.phase == "terminal"
        ):
            return job
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
            if task is not None:
                task.cancel()
        return await generation_job_repo.get_job(job_id)

    @staticmethod
    async def get_job(job_id: str) -> Dict[str, Any]:
        return await generation_job_repo.get_job(job_id)

    @staticmethod
    async def list_jobs(novel_id: str) -> List[Dict[str, Any]]:
        return await generation_job_repo.list_jobs_by_novel(novel_id)

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
