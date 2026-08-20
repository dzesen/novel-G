"""批量作业服务：CRUD、全局单作业守卫、拉起/控制进程内任务。"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Mapping, Optional

from pymongo.errors import DuplicateKeyError

from backend.db.repositories.generation_job_repository import generation_job_repo
from backend.db.narrative_revision import narrative_revision_store
from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.utils import to_object_id
from backend.services.generation import job_planner
from backend.services.generation.chapter_pipeline import ChapterPipelineFailed, run_chapter
from backend.services.generation.chapter_candidate_authorization import (
    authorized_candidate_repair_attempt_slots,
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
from backend.services.generation.chapter_candidate_repairs import (
    ChapterCandidateRepairApplication,
)
from backend.services.generation.chapter_finalization import (
    ChapterFinalizationAuthorization,
    ChapterFinalizationEvidence,
    chapter_finalization_service,
    parse_chapter_finalization_authorization,
)
from backend.services.generation.attempt_scope import JobAttemptScope
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
    JobControl, JobEngineDeps, run_job, _REGISTRY,
)
from backend.services.generation.outline_adherence import (
    PAUSE_FOR_REWRITE,
    validate_outline_deviation_policy,
)
from backend.services.novel.chapter_service import ChapterService
from backend.db.repositories.volume_repository import volume_repo
from backend.db.repositories.novel_repository import novel_repo
from backend.services.generation.book_worklist import get_book_worklist
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

logger = logging.getLogger(__name__)
_START_LOCK: asyncio.Lock | None = None
_START_LOCK_LOOP: asyncio.AbstractEventLoop | None = None


def _get_start_lock() -> asyncio.Lock:
    """Return one start/resume lock per event loop (tests use multiple loops)."""
    global _START_LOCK, _START_LOCK_LOOP
    loop = asyncio.get_running_loop()
    if _START_LOCK is None or _START_LOCK_LOOP is not loop:
        _START_LOCK = asyncio.Lock()
        _START_LOCK_LOOP = loop
    return _START_LOCK


class ConflictError(Exception):
    """已有在跑作业（全局单作业约束）。路由映射为 409。"""


async def _recover_job_mutation_revision(
    novel_id: str,
    idempotency_key: str,
    *,
    operation: str,
) -> int | None:
    # Lazy import keeps the recovery registry from forming a service import cycle.
    from backend.services.novel.mutation_recovery import (
        recover_bound_mutation_revision,
    )

    return await recover_bound_mutation_revision(
        novel_id,
        idempotency_key,
        operation=operation,
    )


class ResumeReadinessRequired(ValueError):
    """The paused job needs a fresh, user-confirmed authorization preview."""


_AUTHORIZATION_SCOPE_FIELDS = (
    "max_base_calls",
    "max_automatic_continuation_calls",
    "max_logical_prose_calls",
    "max_actual_provider_attempts",
    "conservative_base_token_bound",
    "conservative_continuation_token_bound",
    "conservative_token_bound",
    "conservative_total_token_bound",
)


def _safe_scope_value(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _authorization_scope(authorization: Mapping[str, Any] | None) -> dict[str, int]:
    values = dict(authorization or {})
    return {
        field: _safe_scope_value(values.get(field))
        for field in _AUTHORIZATION_SCOPE_FIELDS
    }


def _authorization_scope_increases(
    *,
    authorized: Mapping[str, Any] | None,
    candidate: Mapping[str, Any] | None,
) -> list[str]:
    authorized_scope = _authorization_scope(authorized)
    candidate_scope = _authorization_scope(candidate)
    return [
        field
        for field in _AUTHORIZATION_SCOPE_FIELDS
        if candidate_scope[field] > authorized_scope[field]
    ]


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
) -> Dict[str, Any]:
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
    document = {
        "novel_id": to_object_id(novel_id), "scope": scope,
        "volume_id": to_object_id(volume_id) if volume_id else None,
        "status": "running", "pause_reason": None,
        "checkpoint_interval": int(checkpoint_interval), "token_budget": token_budget,
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
        "generation_params": dict(generation_params or {}),
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
        authorization = dict(job.get("prose_continuation_authorization") or {})
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
        if not any(str(item.get("_id") or "") == str(chapter_id) for item in chapters):
            raise ValueError("accepted outline chapter is outside the generation job")

        policy = ProseContinuationPolicy.from_mapping(
            authorization.get("policy")
            or (job.get("generation_params") or {}).get(
                "prose_continuation_policy"
            )
        )
        generation_params = {
            **dict(job.get("generation_params") or {}),
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
            prose_continuation_policy=policy,
            token_budget=job.get("token_budget"),
            generation_params=generation_params,
            authorization_revision=max(1, current_revision),
        )
        candidate = dict(
            (report.get("planning") or {}).get(
                "prose_continuation_authorization"
            ) or {}
        )
        authorized_scope = _authorization_scope(authorization)
        candidate_scope = _authorization_scope(candidate)
        exceeded_fields = _authorization_scope_increases(
            authorized=authorization,
            candidate=candidate,
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
        recalculation = {
            "chapter_id": str(chapter_id),
            "authorization_revision": max(1, current_revision),
            "authorized_scope": authorized_scope,
            "candidate_scope": candidate_scope,
            "exceeded_fields": exceeded_fields,
            "new_acknowledgement_codes": new_acknowledgements,
            "blocked_issue_codes": blocked,
            "expected_narrative_revision": (
                (report.get("resources") or {}).get("narrative_revision")
            ),
        }
        expected_revision = recalculation["expected_narrative_revision"]
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("outline recalculation narrative revision is invalid")
        if not candidate or exceeded_fields or new_acknowledgements or blocked:
            result = {
                **recalculation,
                "status": "confirmation_required",
                "requires_confirmation": True,
            }
            await generation_job_repo.update_job_fields(job_id, {
                "readiness_recalculation": result,
                "authorization_confirmation_required": result,
            })
            return result

        await generation_job_repo.update_job_fields(job_id, {
            "prose_continuation_authorization": candidate,
            "readiness_recalculation": {
                **recalculation,
                "status": "narrowed_or_unchanged",
                "requires_confirmation": False,
            },
            "authorization_confirmation_required": None,
        })
        return {
            **recalculation,
            "status": "narrowed_or_unchanged",
            "requires_confirmation": False,
        }

    @staticmethod
    def _spawn(job_id: str, control: JobControl) -> None:
        """在当前事件循环拉起后台任务并记入注册表。测试用 monkeypatch 换成 no-op。

        工作清单提供者按作业 scope 装配：闭包每次读作业文档决定取整卷还是整本清单，
        引擎对 scope 无知（设计 §3.1/§3.2）。作业的 scope/目标从不变，重读代价可忽略。
        """
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
            generation_params = dict(
                current_job.get("generation_params") or {}
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
                await generation_job_repo.update_job_fields(
                    job_id,
                    {"confirm_uncertain_prose_retry": False},
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
            )

            cursor = current_job.get("expected_narrative_revision")
            if cursor is not None:
                if type(cursor) is not int or cursor < 0:
                    raise ValueError("Generation job narrative revision cursor is invalid")
                current_revision = await narrative_revision_store.current(novel_id)
                if current_revision != cursor:
                    raise StaleStatePreview(
                        "Generation job narrative revision changed before chapter execution"
                    )

            async def advance_cursor(outcome) -> None:
                nonlocal cursor
                if cursor is None:
                    return
                mutation_steps = [
                    step
                    for step in outcome.steps_done
                    if step in {"outline", "prose", "state"}
                ]
                next_revision = await narrative_revision_store.current(novel_id)
                if next_revision != cursor + len(mutation_steps):
                    raise StaleStatePreview(
                        "Generation job narrative revision changed during chapter execution"
                    )
                for _step in mutation_steps:
                    await generation_job_repo.advance_narrative_revision_cursor(
                        job_id,
                        chapter_id=chapter_id,
                        expected_revision=cursor,
                        next_revision=cursor + 1,
                    )
                    cursor += 1
            try:
                try:
                    outcome = await run_chapter(
                        novel_id,
                        chapter,
                        deps,
                        outline_deviation_policy=outline_deviation_policy,
                    )
                except ChapterPipelineFailed as exc:
                    await advance_cursor(exc.outcome)
                    raise
                await advance_cursor(outcome)
                return outcome
            finally:
                await generation_job_repo.finish_attempt_reservation(job_id, chapter_id)

        async def _run_candidate_chapter(
            novel_id: str,
            chapter: Dict[str, Any],
        ):
            chapter_id = str(chapter["_id"])
            current_job = await generation_job_repo.get_job(job_id)
            readiness = current_job.get("readiness")
            if not isinstance(readiness, Mapping):
                raise ValueError("candidate Job readiness is invalid")
            generation_params = dict(
                current_job.get("generation_params") or {}
            )
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
                live_plans = plan_candidate_job_generation(
                    needs_outline=snapshot.get("has_outline") is False,
                    active=True,
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
                    generate_prose_candidate=generate_prose_candidate,
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

        async def _inspect_reference_card_blockers():
            current_job = await generation_job_repo.get_job(job_id)
            return await emergent_reference_card_candidate_module.blocking_summary(
                str(current_job["novel_id"])
            )

        deps = JobEngineDeps(
            list_worklist_chapters=_list_worklist,
            run_chapter=_run_chapter,
            run_candidate_chapter=_run_candidate_chapter,
            inspect_reference_card_blockers=_inspect_reference_card_blockers,
        )
        task = asyncio.create_task(run_job(job_id, deps, control))
        _REGISTRY[job_id] = (task, control)

    @staticmethod
    async def resume_after_reference_card_review(
        novel_id: str,
    ) -> List[str]:
        """Resume the latest job paused only for a now-cleared card review."""
        if await emergent_reference_card_candidate_module.blocking_summary(
            novel_id
        ):
            return []
        job_id: str | None = None
        async with _get_start_lock():
            if await emergent_reference_card_candidate_module.blocking_summary(
                novel_id
            ):
                return []
            jobs = await generation_job_repo.list_jobs_by_novel(novel_id)
            target = next(
                (
                    item
                    for item in jobs
                    if item.get("status") == "paused"
                    and item.get("pause_reason") == "reference_card_review"
                ),
                None,
            )
            if target is None:
                return []
            await GenerationJobService._guard_no_running()
            job_id = str(target["_id"])
            await generation_job_repo.update_job_fields(job_id, {
                "status": "running",
                "pause_reason": None,
                "error": None,
                "active_slot": "global",
                "current_chapter_id": None,
            })
        control = JobControl()
        GenerationJobService._spawn(job_id, control)
        return [job_id]

    @staticmethod
    async def inspect_volume_readiness(
        volume_id: str,
        *,
        prose_continuation_policy: ProseContinuationPolicy | None = None,
        token_budget: int | None = None,
        generation_params: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
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
            prose_continuation_policy=prose_continuation_policy,
            token_budget=token_budget,
            generation_params=generation_params,
        )

    @staticmethod
    async def inspect_book_readiness(
        novel_id: str,
        *,
        prose_continuation_policy: ProseContinuationPolicy | None = None,
        token_budget: int | None = None,
        generation_params: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        await novel_repo.get_novel_by_id(novel_id)
        chapters = await get_book_worklist(novel_id, include_content=True)
        return await generation_readiness_module.inspect(
            novel_id=novel_id,
            scope="book",
            volume_id=None,
            chapters=chapters,
            prose_continuation_policy=prose_continuation_policy,
            token_budget=token_budget,
            generation_params=generation_params,
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
        if job.get("has_uncertain_attempts"):
            raise ValueError(
                "存在结果不确定的 Provider 请求，请先选择重试或跳过"
            )
        authorization = dict(job.get("prose_continuation_authorization") or {})
        stored_policy = ProseContinuationPolicy.from_mapping(
            authorization.get("policy")
            or (job.get("generation_params") or {}).get(
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
            **dict(job.get("generation_params") or {}),
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
            prose_continuation_policy=candidate_policy,
            token_budget=candidate_budget,
            generation_params=generation_params_snapshot,
            authorization_revision=max(1, current_revision + 1),
        )

    @staticmethod
    async def start_volume_job(volume_id: str, checkpoint_interval: int,
                               token_budget: Optional[int], *,
                               readiness_digest: str | None = None,
                               acknowledged_warning_codes: tuple[str, ...] | list[str] = (),
                               outline_deviation_policy: str = PAUSE_FOR_REWRITE,
                               generation_params: Mapping[str, Any] | None = None,
                               prose_continuation_policy: ProseContinuationPolicy | None = None,
                               ) -> Dict[str, Any]:
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
                **dict(generation_params or {}),
                "prose_continuation_policy": continuation_policy.to_dict(),
            }
            report = await generation_readiness_module.inspect(
                novel_id=novel_id,
                scope="volume",
                volume_id=volume_id,
                chapters=chapters,
                prose_continuation_policy=continuation_policy,
                token_budget=token_budget,
                generation_params=generation_params_snapshot,
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
                    )
                )
            except DuplicateKeyError as exc:
                raise ConflictError("已有正在运行的批量作业，请先暂停或等待其结束") from exc
        control = JobControl()
        GenerationJobService._spawn(job_id, control)
        return await generation_job_repo.get_job(job_id)

    @staticmethod
    async def start_book_job(novel_id: str, checkpoint_interval: int,
                             token_budget: Optional[int], *,
                             readiness_digest: str | None = None,
                             acknowledged_warning_codes: tuple[str, ...] | list[str] = (),
                             outline_deviation_policy: str = PAUSE_FOR_REWRITE,
                             generation_params: Mapping[str, Any] | None = None,
                             prose_continuation_policy: ProseContinuationPolicy | None = None,
                             ) -> Dict[str, Any]:
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
                **dict(generation_params or {}),
                "prose_continuation_policy": continuation_policy.to_dict(),
            }
            report = await generation_readiness_module.inspect(
                novel_id=novel_id,
                scope="book",
                volume_id=None,
                chapters=chapters,
                prose_continuation_policy=continuation_policy,
                token_budget=token_budget,
                generation_params=generation_params_snapshot,
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
                    )
                )
            except DuplicateKeyError as exc:
                raise ConflictError("已有正在运行的批量作业，请先暂停或等待其结束") from exc
        control = JobControl()
        GenerationJobService._spawn(job_id, control)
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
        async with _get_start_lock():
            job = await generation_job_repo.get_job(job_id)
            if not job_planner.can_resume(job["status"]):
                raise ValueError(f"作业当前状态 {job['status']} 不可恢复")
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
                (confirm_uncertain_retry or skip_uncertain)
                and not job.get("has_uncertain_attempts")
            ):
                raise ValueError(
                    "uncertain-attempt recovery requires an uncertain Provider attempt"
                )
            if job.get("has_uncertain_attempts") and not confirm_uncertain_retry:
                if skip_uncertain:
                    await generation_job_repo.acknowledge_uncertain_attempts(job_id, "skip")
                    await generation_job_repo.update_job_fields(job_id, {
                        "status": "failed",
                        "pause_reason": "uncertain_skipped",
                        "active_slot": None,
                        "current_chapter_id": None,
                        "error": {
                            "step": "uncertain_attempt",
                            "message": "用户选择跳过可能已发出的 Provider 请求；请人工检查章节后再恢复",
                        },
                    })
                    return await generation_job_repo.get_job(job_id)
                raise ValueError(
                    "存在请求已发出但未取得 usage 的 attempt，可能已计费；"
                    "请明确确认可能重复计费后再重试"
                )
            authorization_updates: Dict[str, Any] = {}
            reauthorized_revision: int | None = None
            authorization = dict(job.get("prose_continuation_authorization") or {})
            stored_policy = ProseContinuationPolicy.from_mapping(
                authorization.get("policy")
                or (job.get("generation_params") or {}).get(
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
            authorization_settings_changed = (
                prose_continuation_policy is not None
                or token_budget_provided
                or authorization_confirmation_required
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
                    **dict(job.get("generation_params") or {}),
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
                report = await generation_readiness_module.inspect(
                    novel_id=str(job["novel_id"]),
                    scope=str(job["scope"]),
                    volume_id=(
                        str(job["volume_id"])
                        if job.get("volume_id") is not None
                        else None
                    ),
                    chapters=chapters,
                    prose_continuation_policy=candidate_policy,
                    token_budget=candidate_budget,
                    generation_params=generation_params_snapshot,
                    authorization_revision=max(1, current_revision + 1),
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

            if job.get("has_uncertain_attempts") and confirm_uncertain_retry:
                await generation_job_repo.acknowledge_uncertain_attempts(job_id, "retry")
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
            if authorization_updates and reauthorized_revision is not None:
                previous_revision = job.get("expected_narrative_revision")
                if previous_revision is not None and type(previous_revision) is not int:
                    raise ValueError(
                        "Generation job narrative revision cursor is invalid"
                    )
                await generation_job_repo.update_job_authorization(
                    job_id,
                    resume_fields,
                    previous_revision=previous_revision,
                    next_revision=reauthorized_revision,
                )
            else:
                await generation_job_repo.update_job_fields(job_id, resume_fields)
        control = JobControl()
        GenerationJobService._spawn(job_id, control)
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
        await generation_job_repo.get_job(job_id)
        entry = _REGISTRY.get(job_id)
        if entry is not None:
            entry[1].abort_requested = True
            task = entry[0]
            if task is not None:
                task.cancel()
        # 无条件落终态：run_job 被 cancel 后其 finally 只弹注册表、不写状态，且
        # CancelledError（BaseException）绕过 except Exception，循环顶端的 abort 标记
        # 可能永不执行——若不在此处写 aborted，作业会永远卡在 running，全局单作业槽被
        # 死锁（list_running_jobs 计入它，挡住之后所有 start/resume）。cancel 后 run_job
        # 不会再写状态，故此处写入不会被覆盖。
        await generation_job_repo.update_job_fields(job_id, {
            "status": "aborted", "current_chapter_id": None, "active_slot": None,
        })
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
