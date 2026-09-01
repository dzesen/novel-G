"""批量作业运行循环 + 进程内任务注册表（设计 §4.1 / §7）。

主循环以作业记录为事实源：派生工作清单→跑一章→追加进度→判暂停/完成/失败。
repo 与每章管线以依赖注入，使循环可脱离 Mongo/LLM 用假件测。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol
from uuid import uuid4

from backend.db.narrative_revision import NarrativeRevisionConflict
from backend.db.repositories.generation_job_repository import (
    AttemptCapacityExceeded,
    TokenBudgetExceeded,
    generation_job_repo,
)
from backend.db.utils import get_utc_now
from backend.services.generation import job_planner
from backend.services.generation.candidate_repair_contracts import (
    CandidatePipelineCheckpointConflict,
    CandidatePipelineCheckpointV1,
    CandidatePipelineProgressV1,
)
from backend.services.generation.candidate_manual_takeover import (
    project_candidate_manual_takeover,
)
from backend.services.generation.chapter_pipeline import (
    ChapterOutcome,
    ChapterPipelineFailed,
    IncompleteProseGeneration,
)
from backend.services.generation.chapter_candidate_authorization import (
    readiness_chapter_uses_candidate_pipeline,
    readiness_uses_candidate_pipeline,
)
from backend.services.generation.required_chapter_review_job import (
    RequiredChapterReviewJobOutcome,
    readiness_chapter_uses_required_chapter_review,
    readiness_uses_required_chapter_review,
    required_review_repair_count,
)
from backend.services.generation.required_chapter_state_job import (
    RequiredChapterStateJobOutcome,
    readiness_chapter_uses_required_chapter_state,
    readiness_uses_required_chapter_state,
)
from backend.services.generation.required_chapter_finalization_job import (
    RequiredChapterFinalizationJobOutcome,
    readiness_chapter_uses_required_chapter_finalization,
    readiness_uses_required_chapter_finalization,
)
from backend.services.generation.required_book_successor import (
    readiness_uses_required_book_successor,
)
from backend.services.generation.failure_diagnostics import (
    build_failure_diagnostic,
    candidate_repair_stop_projection,
    incomplete_prose_pre_dispatch_boundary_code,
)
from backend.services.generation.job_execution import JobExecutionLeaseLost
from backend.services.novel.book_completion import BookCompletionReport

logger = logging.getLogger(__name__)

# 进程内任务注册表：job_id → (asyncio.Task, JobControl)。易失，重启即丢（设计 §4.1/§4.3）。
_REGISTRY: Dict[str, "tuple[asyncio.Task, JobControl]"] = {}

_PRE_DISPATCH_PAUSE_REASONS = {
    "token_budget_exceeded_before_dispatch": "cost_cap",
    "attempt_capacity_exhausted": "attempt_capacity",
}


def _pre_dispatch_pause_reason(code: object) -> str | None:
    return _PRE_DISPATCH_PAUSE_REASONS.get(str(code or ""))


@dataclass
class JobControl:
    pause_requested: bool = False
    abort_requested: bool = False


@dataclass(frozen=True)
class CandidateChapterOutcome:
    """One fully finalized candidate chapter awaiting atomic Job progress."""

    progress: CandidatePipelineProgressV1
    checkpoints: tuple[CandidatePipelineCheckpointV1, ...]
    expected_narrative_revision: int | None = None
    next_narrative_revision: int | None = None


class BookCompletionPublicationFenceLike(Protocol):
    narrative_revision: int
    fence_token: str


@dataclass(frozen=True)
class JobEngineDeps:
    list_worklist_chapters: Callable[[], Awaitable[List[Dict[str, Any]]]]
    run_chapter: Callable[[str, Dict[str, Any]], Awaitable[ChapterOutcome]]

    run_candidate_chapter: Optional[
        Callable[
            [str, Dict[str, Any]],
            Awaitable[CandidateChapterOutcome],
        ]
    ] = None

    run_required_review_chapter: Optional[
        Callable[
            [str, Dict[str, Any]],
            Awaitable[RequiredChapterReviewJobOutcome],
        ]
    ] = None

    run_required_state_chapter: Optional[
        Callable[
            [str, Dict[str, Any]],
            Awaitable[RequiredChapterStateJobOutcome],
        ]
    ] = None

    run_required_finalization_chapter: Optional[
        Callable[
            [str, Dict[str, Any]],
            Awaitable[RequiredChapterFinalizationJobOutcome],
        ]
    ] = None

    run_required_book_successor: Optional[
        Callable[[], Awaitable[None]]
    ] = None

    inspect_reference_card_blockers: Optional[
        Callable[[], Awaitable[Dict[str, Any] | None]]
    ] = None

    resolve_reference_card_blockers: Optional[
        Callable[[], Awaitable[Dict[str, Any] | None]]
    ] = None

    inspect_book_completion: Optional[
        Callable[
            [BookCompletionPublicationFenceLike],
            Awaitable[BookCompletionReport | Mapping[str, Any]],
        ]
    ] = None

    guard_book_completion_publication: Optional[
        Callable[
            [int | None, str],
            AbstractAsyncContextManager[BookCompletionPublicationFenceLike],
        ]
    ] = None


async def finalize_book_job(
    repo: Any,
    job_id: str,
    job: Mapping[str, Any],
    inspect_book_completion: Optional[
        Callable[
            [BookCompletionPublicationFenceLike],
            Awaitable[BookCompletionReport | Mapping[str, Any]],
        ]
    ],
    guard_book_completion_publication: Optional[
        Callable[
            [int | None, str],
            AbstractAsyncContextManager[BookCompletionPublicationFenceLike],
        ]
    ],
) -> None:
    """Publish a terminal Job status only behind the canonical book audit."""

    previous_epoch = job.get("execution_epoch", 0)
    if type(previous_epoch) is not int or previous_epoch < 0:
        raise ValueError("Generation Job execution epoch is invalid")
    previous_revision = job.get("expected_narrative_revision")
    if previous_revision is not None and (
        type(previous_revision) is not int or previous_revision < 0
    ):
        raise ValueError("Generation Job narrative revision is invalid")
    previous_status = str(job.get("status") or "")
    previous_pause_reason = (
        str(job.get("pause_reason"))
        if job.get("pause_reason") is not None
        else None
    )
    novel_id = str(job.get("novel_id") or "")
    fence_token = uuid4().hex
    snapshot = {
        "previous_status": previous_status,
        "previous_pause_reason": previous_pause_reason,
        "previous_execution_epoch": previous_epoch,
        "previous_expected_narrative_revision": previous_revision,
        "novel_id": novel_id,
    }
    await repo.reserve_book_completion_audit_publication(
        job_id,
        fence_token=fence_token,
        **snapshot,
    )

    try:
        if inspect_book_completion is None:
            raise RuntimeError("Book completion audit dependency is unavailable")
        if guard_book_completion_publication is None:
            raise RuntimeError(
                "Book completion audit publication fence is unavailable"
            )
        async with guard_book_completion_publication(
            previous_revision,
            fence_token,
        ) as fence:
            if fence.fence_token != fence_token:
                raise ValueError(
                    "Book completion audit fence token does not match its Job reservation"
                )
            report = BookCompletionReport.model_validate(
                await inspect_book_completion(fence)
            )
            if report.novel_id != str(job.get("novel_id") or ""):
                raise ValueError(
                    "Book completion audit novel does not match the Job"
                )
            if report.blueprint.frozen_job_id != str(job_id):
                raise ValueError(
                    "Book completion audit is not bound to the Job"
                )
            if report.narrative_revision != fence.narrative_revision:
                raise NarrativeRevisionConflict(
                    "Book completion audit revision does not match its fence"
                )
            await repo.renew_book_completion_audit_publication(
                job_id,
                fence_token=fence.fence_token,
                **snapshot,
            )
            await repo.publish_book_completion_audit(
                job_id,
                report.model_dump(mode="json"),
                fence_token=fence.fence_token,
                previous_status=previous_status,
                previous_pause_reason=previous_pause_reason,
                previous_execution_epoch=previous_epoch,
                previous_expected_narrative_revision=previous_revision,
            )
            return
    except (
        CandidatePipelineCheckpointConflict,
        JobExecutionLeaseLost,
    ):
        raise
    except Exception as exc:  # noqa: BLE001 - publish one fail-closed fact
        await _persist_diagnostic(
            repo,
            job_id,
            build_failure_diagnostic(
                exc,
                step="final_audit",
                chapter_id="",
                occurred_at=get_utc_now(),
            ),
        )
        await repo.publish_book_completion_audit_failure(
            job_id,
            novel_id=novel_id,
            fence_token=fence_token,
            message=str(exc),
            source_changed=isinstance(exc, NarrativeRevisionConflict),
            previous_status=previous_status,
            previous_pause_reason=previous_pause_reason,
            previous_execution_epoch=previous_epoch,
            previous_expected_narrative_revision=previous_revision,
        )


def outcome_to_progress(outcome: ChapterOutcome) -> Dict[str, Any]:
    progress = {
        "chapter_id": outcome.chapter_id, "order_index": outcome.order_index,
        "steps_done": outcome.steps_done, "steps_skipped": outcome.steps_skipped,
        "agents_used": outcome.agents_used,
        "tokens": outcome.tokens, "consistency_issues": outcome.consistency_issues,
        "facts_added": outcome.facts_added, "threads_advanced": outcome.threads_advanced,
        "summary_written": outcome.summary_written, "dropped_ids": outcome.dropped_ids,
        "truncations": outcome.truncations,
        "attempts": outcome.attempts,
        "step_outcomes": outcome.step_outcomes,
        "notices": outcome.notices,
        "prose_completion": dict(outcome.prose_completion),
        "authorization_recalculation": dict(outcome.authorization_recalculation),
        "completed_at": get_utc_now(),
    }
    # An incomplete prose run stops before the adherence review.  Do not persist
    # its default empty object as if it were a completed review.
    if outcome.outline_adherence:
        progress["outline_adherence"] = outcome.outline_adherence
    if outcome.mutation_receipts:
        if len(outcome.mutation_receipts) != 1:
            raise ValueError("Legacy chapter has an invalid mutation receipt count")
        progress["job_mutation_receipt"] = outcome.mutation_receipts[0].model_dump(
            mode="json"
        )
    return progress


def _incomplete_prose_checkpoint(
    completion: Dict[str, Any],
    *,
    chapter_id: str,
) -> Dict[str, Any]:
    """Persist a recovery pointer and counters, never draft prose or prompts."""
    def integer(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    scenes: list[dict[str, Any]] = []
    for raw in list(completion.get("scene_progress") or [])[:20]:
        if not isinstance(raw, dict):
            continue
        word_count = integer(raw.get("word_count"))
        raw_word_count = (
            integer(raw.get("raw_word_count"))
            if raw.get("raw_word_count") is not None
            else word_count
        )
        effective_word_count = min(
            raw_word_count,
            (
                integer(raw.get("effective_word_count"))
                if raw.get("effective_word_count") is not None
                else raw_word_count
            ),
        )
        scenes.append(
            {
                "scene_index": integer(raw.get("scene_index")),
                "status": str(raw.get("status") or "incomplete")[:40],
                "base_calls_used": integer(raw.get("base_calls_used")),
                "automatic_continuations_used": integer(
                    raw.get("automatic_continuations_used")
                ),
                "manual_continuations_used": integer(
                    raw.get("manual_continuations_used")
                ),
                "word_count": word_count,
                "raw_word_count": raw_word_count,
                "effective_word_count": effective_word_count,
                "replayed_characters_total": integer(
                    raw.get("replayed_characters_total")
                ),
                "scene_target_words": integer(raw.get("scene_target_words")),
                "converge_attempts": integer(raw.get("converge_attempts")),
                "converge_attempts_without_stop": integer(
                    raw.get("converge_attempts_without_stop")
                ),
                "continues_truncated_output_count": integer(
                    raw.get("continues_truncated_output_count")
                ),
                "max_cross_call_repeat_characters": integer(
                    raw.get("max_cross_call_repeat_characters")
                ),
                "last_prompt_mode": str(
                    raw.get("last_prompt_mode") or ""
                )[:60],
                "last_finish_reason": str(
                    raw.get("last_finish_reason") or "unreported"
                )[:60],
                "pause_reason": str(raw.get("pause_reason") or "")[:100],
            }
        )
    return {
        "chapter_id": str(chapter_id),
        "source_run_id": str(completion.get("source_run_id") or ""),
        "source_run_revision": integer(completion.get("source_run_revision")),
        "status": str(completion.get("status") or "incomplete")[:40],
        "pause_reason": str(
            completion.get("pause_reason") or "incomplete_scene"
        )[:100],
        "reason_codes": [
            str(code)[:100]
            for code in list(completion.get("reason_codes") or [])[:20]
            if str(code).strip()
        ],
        "scene_count": integer(completion.get("scene_count")),
        "completed_scene_count": integer(
            completion.get("completed_scene_count")
        ),
        "scene_progress": scenes,
    }


async def _pause(repo, job_id: str, reason: str) -> None:
    await repo.update_job_fields(job_id, {
        "status": "paused",
        "pause_reason": reason,
        "current_chapter_id": None,
        "active_slot": None,
    })


async def _pause_candidate_execution(
    repo,
    job_id: str,
    *,
    chapter_id: str,
    reason: str,
) -> None:
    latest = await repo.get_job(job_id)
    checkpoints = latest.get("candidate_pipeline_checkpoints")
    preserve = isinstance(checkpoints, list) and bool(checkpoints)
    await repo.update_job_fields(job_id, {
        "status": "paused",
        "pause_reason": reason,
        "current_chapter_id": chapter_id if preserve else None,
        "active_slot": None,
    })


async def _pause_required_state_execution(
    repo,
    job_id: str,
    *,
    chapter_id: str,
    reason: str,
) -> None:
    latest = await repo.get_job(job_id)
    preserve = isinstance(
        latest.get("required_state_candidate_journal"),
        Mapping,
    )
    await repo.update_job_fields(job_id, {
        "status": "paused",
        "pause_reason": reason,
        "current_chapter_id": chapter_id if preserve else None,
        "active_slot": None,
    })


async def _persist_attempts(repo, job_id: str, attempts: list[dict]) -> None:
    account = getattr(repo, "account_attempt", None)
    if account is None:
        return
    from backend.llm.models import TokenUsage

    for attempt in attempts:
        attempt_id = str(attempt.get("attempt_id") or "")
        if not attempt_id:
            continue
        await account(job_id, attempt_id, TokenUsage.model_validate(attempt.get("usage") or {}))


async def _persist_diagnostic(repo, job_id: str, event: dict[str, Any]) -> None:
    append = getattr(repo, "append_diagnostic", None)
    if append is None:
        return
    await append(job_id, event)


async def _handle_chapter_failure(
    repo,
    job_id: str,
    chapter: Dict[str, Any],
    exc: Exception,
) -> None:
    """Persist every recoverable chapter failure before the background task exits."""
    chapter_id = str(chapter["_id"])
    step = str(getattr(exc, "step", "run_chapter"))
    failed_outcome = exc.outcome if isinstance(exc, ChapterPipelineFailed) else None
    attempts = list(getattr(exc, "attempts", []) or [])
    if failed_outcome is not None:
        attempts = list(failed_outcome.attempts)

    logger.error(
        "[job %s] chapter %s failed at %s",
        job_id,
        chapter_id,
        step,
        exc_info=exc,
    )
    await _persist_attempts(repo, job_id, attempts)
    diagnostic = build_failure_diagnostic(
        exc,
        step=step,
        chapter_id=chapter_id,
        attempts=attempts,
        occurred_at=get_utc_now(),
    )
    await _persist_diagnostic(repo, job_id, diagnostic)
    latest_job = await repo.get_job(job_id)
    preserve_job_mutation = isinstance(
        latest_job.get("job_mutation_recovery"),
        Mapping,
    )

    pause_reason = _pre_dispatch_pause_reason(diagnostic["code"])
    if pause_reason is not None:
        has_partial_checkpoint = bool(
            failed_outcome is not None
            and (
                failed_outcome.steps_done
                or failed_outcome.steps_skipped
                or failed_outcome.step_outcomes
                or failed_outcome.tokens
                or failed_outcome.attempts
            )
        )
        if has_partial_checkpoint:
            await repo.append_progress(
                job_id,
                outcome_to_progress(failed_outcome),
                tokens_delta=0 if attempts else failed_outcome.tokens,
            )
        await repo.update_job_fields(job_id, {
            "status": "paused",
            "pause_reason": pause_reason,
            "current_chapter_id": chapter_id if preserve_job_mutation else None,
            "active_slot": None,
            "error": {
                "step": step,
                "chapter_id": chapter_id,
                "message": str(exc),
                "attempts": attempts,
            },
        })
        return

    if diagnostic["category"] == "source_changed":
        await repo.update_job_fields(job_id, {
            "status": "paused",
            "pause_reason": "source_changed",
            "current_chapter_id": chapter_id if preserve_job_mutation else None,
            "active_slot": None,
            "error": {
                "step": step,
                "chapter_id": chapter_id,
                "message": "Source changed during generation",
                "attempts": attempts,
                "reason_codes": [
                    "narrative_revision_changed",
                    "reauthorization_required",
                ],
            },
            "authorization_confirmation_required": {
                "status": "source_changed",
                "requires_confirmation": True,
                "chapter_id": chapter_id,
                "code": "chapter_or_narrative_changed",
            },
        })
        return

    has_uncertain = bool(latest_job.get("has_uncertain_attempts"))
    await repo.update_job_fields(job_id, {
        "status": "interrupted" if has_uncertain else "failed",
        "pause_reason": "uncertain_attempt" if has_uncertain else None,
        "current_chapter_id": chapter_id if preserve_job_mutation else None,
        "active_slot": None,
        "error": {
            "step": step,
            "chapter_id": chapter_id,
            "message": str(exc),
            "attempts": attempts,
        },
    })


async def _handle_candidate_chapter_failure(
    repo,
    job_id: str,
    chapter: Dict[str, Any],
    exc: Exception,
) -> None:
    """Stop one candidate execution without discarding its durable prefix."""

    chapter_id = str(chapter["_id"])
    attempts = list(getattr(exc, "attempts", []) or [])
    diagnostic = build_failure_diagnostic(
        exc,
        step="candidate_pipeline",
        chapter_id=chapter_id,
        attempts=attempts,
        occurred_at=get_utc_now(),
    )
    await _persist_diagnostic(repo, job_id, diagnostic)
    budget_pause_reason = _pre_dispatch_pause_reason(diagnostic.get("code"))
    if budget_pause_reason is not None:
        await _pause_candidate_execution(
            repo,
            job_id,
            chapter_id=chapter_id,
            reason=budget_pause_reason,
        )
        return
    latest = await repo.get_job(job_id)
    checkpoints = latest.get("candidate_pipeline_checkpoints")
    preserve = isinstance(checkpoints, list) and bool(checkpoints)
    has_uncertain = bool(latest.get("has_uncertain_attempts"))
    candidate_code = str(getattr(exc, "code", "") or "")
    repair_stop = candidate_repair_stop_projection(exc)
    repair_pause_reason = (
        repair_stop.pause_reason if repair_stop is not None else None
    )
    authorization_scope_increased = (
        candidate_code == "authorization_scope_increased"
    )
    adherence_manual_review = (
        candidate_code == "candidate_adherence_manual_review"
    )
    source_changed = diagnostic["category"] == "source_changed"
    source_changed = source_changed or (
        candidate_code == "candidate_narrative_revision_changed"
    )
    readiness = latest.get("readiness")
    readiness_digest = (
        readiness.get("digest") if isinstance(readiness, Mapping) else None
    )
    manual_takeover = None
    if (
        not has_uncertain
        and not source_changed
        and not authorization_scope_increased
        and not adherence_manual_review
        and isinstance(checkpoints, list)
        and isinstance(readiness_digest, str)
    ):
        manual_takeover = project_candidate_manual_takeover(
            exc,
            novel_id=str(latest.get("novel_id") or ""),
            job_id=str(latest.get("_id") or job_id),
            chapter_id=chapter_id,
            readiness_digest=readiness_digest,
            authorization_revision=latest.get("authorization_revision"),
            expected_narrative_revision=latest.get(
                "expected_narrative_revision"
            ),
            failure_event_id=str(diagnostic.get("event_id") or ""),
            checkpoints=checkpoints,
        )
    if manual_takeover is not None:
        await repo.pause_candidate_pipeline_for_manual_takeover(
            job_id,
            takeover=manual_takeover,
            expected_checkpoints=checkpoints,
        )
        return
    fields = {
        "status": (
            "interrupted"
            if has_uncertain
            else "paused"
            if (
                source_changed
                or authorization_scope_increased
                or adherence_manual_review
                or repair_pause_reason is not None
            )
            else "failed"
        ),
        "pause_reason": (
            "uncertain_attempt"
            if has_uncertain
            else "authorization_scope_increased"
            if authorization_scope_increased
            else "source_changed"
            if source_changed
            else "outline_adherence_manual_review"
            if adherence_manual_review
            else repair_pause_reason
            if repair_pause_reason is not None
            else None
        ),
        "current_chapter_id": (
            chapter_id
            if (
                preserve
                or authorization_scope_increased
                or adherence_manual_review
                or repair_pause_reason is not None
            )
            else None
        ),
        "active_slot": None,
        "error": {
            "step": "candidate_pipeline",
            "chapter_id": chapter_id,
            "message": str(exc),
            "attempts": attempts,
        },
    }
    diagnostic_details = diagnostic.get("details")
    diagnostic_reason_codes = (
        list(diagnostic_details.get("reason_codes") or [])
        if isinstance(diagnostic_details, Mapping)
        else []
    )
    if diagnostic_reason_codes:
        fields["error"]["reason_codes"] = diagnostic_reason_codes
    if repair_stop is not None:
        fields["error"]["reason_codes"] = list(repair_stop.reason_codes)
        if repair_stop.next_step is not None:
            fields["error"]["next_step"] = repair_stop.next_step
        if repair_stop.component_used is not None:
            fields["error"]["component_used"] = repair_stop.component_used
        if repair_stop.component_limit is not None:
            fields["error"]["component_limit"] = repair_stop.component_limit
    if source_changed:
        fields["error"]["reason_codes"] = [
            "narrative_revision_changed",
            "successor_required" if preserve else "reauthorization_required",
        ]
        fields["authorization_confirmation_required"] = {
            "status": "source_changed",
            "requires_confirmation": True,
            "chapter_id": chapter_id,
            "code": candidate_code or "chapter_or_narrative_changed",
        }
    await repo.update_job_fields(job_id, fields)


async def run_job(job_id: str, deps: JobEngineDeps, control: JobControl, *, repo=generation_job_repo) -> None:
    """主循环。任何返回前都已把终态/暂停态持久化。"""
    try:
        while True:
            if control.abort_requested:
                await repo.update_job_fields(job_id, {
                    "status": "aborted", "current_chapter_id": None,
                    "current_failure_event_id": None, "active_slot": None,
                })
                return

            job = await repo.get_job(job_id)

            try:
                required_book_successor = (
                    readiness_uses_required_book_successor(
                        job.get("readiness")
                    )
                )
            except ValueError as exc:
                await repo.update_job_fields(job_id, {
                    "status": "failed",
                    "pause_reason": None,
                    "active_slot": None,
                    "error": {
                        "step": "required_book_successor_authorization",
                        "message": str(exc),
                    },
                })
                return
            if required_book_successor:
                if deps.run_required_book_successor is None:
                    await repo.update_job_fields(job_id, {
                        "status": "failed",
                        "pause_reason": None,
                        "active_slot": None,
                        "error": {
                            "step": "required_book_successor",
                            "message": (
                                "Required book successor runner is unavailable"
                            ),
                        },
                    })
                    return
                await deps.run_required_book_successor()
                return

            try:
                required_finalization_authorized = (
                    readiness_uses_required_chapter_finalization(
                        job.get("readiness")
                    )
                )
                required_state_authorized = (
                    False
                    if required_finalization_authorized
                    else readiness_uses_required_chapter_state(
                        job.get("readiness")
                    )
                )
                required_review_authorized = (
                    False
                    if required_finalization_authorized
                    or required_state_authorized
                    else readiness_uses_required_chapter_review(
                        job.get("readiness")
                    )
                )
                candidate_authorized = (
                    False
                    if required_finalization_authorized
                    or required_review_authorized
                    or required_state_authorized
                    else readiness_uses_candidate_pipeline(
                        job.get("readiness")
                    )
                )
            except ValueError as exc:
                await repo.update_job_fields(job_id, {
                    "status": "failed",
                    "pause_reason": None,
                    "active_slot": None,
                    "error": {
                        "step": "chapter_execution_authorization",
                        "message": str(exc),
                    },
                })
                return

            raw_candidate_checkpoints = job.get(
                "candidate_pipeline_checkpoints",
                [],
            )
            if not isinstance(raw_candidate_checkpoints, list):
                await repo.update_job_fields(job_id, {
                    "status": "failed",
                    "pause_reason": None,
                    "active_slot": None,
                    "error": {
                        "step": "candidate_pipeline_recovery",
                        "message": "Candidate checkpoint ledger is invalid",
                    },
                })
                return
            candidate_recovery = bool(raw_candidate_checkpoints)
            required_journal_names = (
                "required_initial_prose_journal",
                "required_prose_rewrite_journal",
                "required_adherence_journal",
                "required_reviewed_candidate",
            )
            invalid_required_journal = next(
                (
                    name
                    for name in required_journal_names
                    if job.get(name) is not None
                    and not isinstance(job.get(name), Mapping)
                ),
                None,
            )
            if invalid_required_journal is not None:
                await repo.update_job_fields(job_id, {
                    "status": "failed",
                    "pause_reason": None,
                    "active_slot": None,
                    "error": {
                        "step": "required_chapter_review_recovery",
                        "message": (
                            "Required chapter review journal is invalid"
                        ),
                    },
                })
                return
            required_review_recovery = any(
                job.get(name) is not None
                for name in required_journal_names
            )
            required_state_names = (
                "required_state_candidate_journal",
                "required_state_candidate",
            )
            invalid_required_state = next(
                (
                    name
                    for name in required_state_names
                    if job.get(name) is not None
                    and not isinstance(job.get(name), Mapping)
                ),
                None,
            )
            if invalid_required_state is not None:
                await repo.update_job_fields(job_id, {
                    "status": "failed",
                    "pause_reason": None,
                    "active_slot": None,
                    "error": {
                        "step": "required_chapter_state_recovery",
                        "message": "Required chapter state journal is invalid",
                    },
                })
                return
            required_state_recovery = any(
                job.get(name) is not None for name in required_state_names
            )
            raw_job_mutation_recovery = job.get("job_mutation_recovery")
            if raw_job_mutation_recovery is not None and not isinstance(
                raw_job_mutation_recovery,
                Mapping,
            ):
                await repo.update_job_fields(job_id, {
                    "status": "failed",
                    "pause_reason": None,
                    "active_slot": None,
                    "error": {
                        "step": "job_mutation_recovery",
                        "message": "Job mutation recovery binding is invalid",
                    },
                })
                return
            job_mutation_recovery = raw_job_mutation_recovery is not None
            recovery_modes = sum((
                bool(candidate_recovery),
                bool(required_review_recovery),
                bool(required_state_recovery),
                bool(job_mutation_recovery),
            ))
            if recovery_modes > 1:
                await repo.update_job_fields(job_id, {
                    "status": "failed",
                    "pause_reason": None,
                    "active_slot": None,
                    "error": {
                        "step": "job_mutation_recovery",
                        "message": "Job has conflicting recovery checkpoints",
                    },
                })
                return
            if candidate_recovery and not candidate_authorized:
                await repo.update_job_fields(job_id, {
                    "status": "failed",
                    "pause_reason": None,
                    "active_slot": None,
                    "error": {
                        "step": "candidate_pipeline_recovery",
                        "message": (
                            "Candidate checkpoints have no bound authorization"
                        ),
                    },
                })
                return
            if required_review_recovery and not required_review_authorized:
                await repo.update_job_fields(job_id, {
                    "status": "failed",
                    "pause_reason": None,
                    "active_slot": None,
                    "error": {
                        "step": "required_chapter_review_recovery",
                        "message": (
                            "Required review journals have no bound authorization"
                        ),
                    },
                })
                return
            if required_state_recovery and not required_state_authorized:
                await repo.update_job_fields(job_id, {
                    "status": "failed",
                    "pause_reason": None,
                    "active_slot": None,
                    "error": {
                        "step": "required_chapter_state_recovery",
                        "message": (
                            "Required state journal has no bound authorization"
                        ),
                    },
                })
                return
            if (
                job_mutation_recovery
                and not candidate_authorized
                and not required_finalization_authorized
            ):
                await repo.update_job_fields(job_id, {
                    "status": "failed",
                    "pause_reason": None,
                    "active_slot": None,
                    "error": {
                        "step": "job_mutation_recovery",
                        "message": "Job mutation has no bound formal authorization",
                    },
                })
                return
            # 成本上限：开下一章前的软天花板。
            committed_or_reserved = int(job.get("tokens_used", 0)) + int(
                job.get("tokens_reserved", 0) or 0
            )
            if (
                not candidate_recovery
                and not required_review_recovery
                and not required_state_recovery
                and job_planner.over_budget(
                    committed_or_reserved,
                    job.get("token_budget"),
                )
            ):
                await _persist_diagnostic(
                    repo,
                    job_id,
                    build_failure_diagnostic(
                        TokenBudgetExceeded(
                            "Job token budget is exhausted before dispatch"
                        ),
                        step="job_budget",
                        chapter_id="",
                        occurred_at=get_utc_now(),
                    ),
                )
                await _pause(repo, job_id, "cost_cap")
                return

            chapters = await deps.list_worklist_chapters()
            if (
                candidate_recovery
                or required_review_recovery
                or required_state_recovery
                or job_mutation_recovery
            ):
                current_chapter_id = str(
                    job.get("current_chapter_id") or ""
                )
                chapter = next(
                    (
                        item
                        for item in chapters
                        if str(item.get("_id") or "")
                        == current_chapter_id
                    ),
                    None,
                )
                if chapter is None:
                    await repo.update_job_fields(job_id, {
                        "status": "failed",
                        "pause_reason": None,
                        "active_slot": None,
                        "error": {
                            "step": (
                                "required_chapter_state_recovery"
                                if required_state_recovery
                                else "required_chapter_review_recovery"
                                if required_review_recovery
                                else "candidate_pipeline_recovery"
                            ),
                            "message": (
                                "Bound recovery chapter is unavailable"
                            ),
                        },
                    })
                    return
            else:
                if (
                    required_finalization_authorized
                    or required_review_authorized
                    or required_state_authorized
                ):
                    readiness = job.get("readiness")
                    work = (
                        readiness.get("work")
                        if isinstance(readiness, Mapping)
                        else None
                    )
                    raw_work = (
                        work.get("chapters")
                        if isinstance(work, Mapping)
                        else None
                    )
                    ordered_ids = [
                        str(item.get("chapter_id") or "")
                        for item in (raw_work or [])
                        if isinstance(item, Mapping)
                        and item.get("has_content") is False
                    ]
                    live_by_id = {
                        str(item.get("_id") or ""): item
                        for item in chapters
                    }
                    chapter = next(
                        (
                            live_by_id[chapter_id]
                            for chapter_id in ordered_ids
                            if chapter_id in live_by_id
                        ),
                        None,
                    )
                else:
                    chapter = job_planner.first_needing_work(chapters)
            if chapter is not None:
                await repo.update_job_fields(
                    job_id,
                    {"current_chapter_id": str(chapter["_id"])},
                )
            reference_card_blocker_check = (
                deps.resolve_reference_card_blockers
                or deps.inspect_reference_card_blockers
            )
            if (
                not candidate_recovery
                and not required_review_recovery
                and not required_state_recovery
                and not job_mutation_recovery
                and not required_finalization_authorized
                and chapter is not None
                and reference_card_blocker_check is not None
            ):
                try:
                    blockers = await reference_card_blocker_check()
                except Exception as exc:  # noqa: BLE001 - preserve exact recovery
                    await _persist_diagnostic(
                        repo,
                        job_id,
                        build_failure_diagnostic(
                            exc,
                            step="reference_card_auto_creation_recovery",
                            chapter_id=str(chapter.get("_id") or ""),
                            occurred_at=get_utc_now(),
                        ),
                    )
                    await repo.update_job_fields(job_id, {
                        "status": "paused",
                        "pause_reason": (
                            "reference_card_auto_creation_recovery"
                        ),
                        # Keep the source chapter cursor so the same immutable
                        # mutation can be resumed after a standalone crash.
                        "active_slot": None,
                            "error": {
                                "step": (
                                    "reference_card_auto_creation_recovery"
                                ),
                                "chapter_id": str(chapter.get("_id") or ""),
                                "message": str(exc),
                            },
                    })
                    return
                if blockers:
                    auto_creation = blockers.get("auto_creation")
                    requested_pause = (
                        str(auto_creation.get("pause_reason") or "")
                        if isinstance(auto_creation, Mapping)
                        else ""
                    )
                    pause_reason = (
                        requested_pause
                        if requested_pause
                        in {
                            "reference_card_repair_exhausted",
                            "cost_cap",
                            "attempt_capacity",
                            "uncertain_attempt",
                            "source_changed",
                        }
                        else "reference_card_review"
                    )
                    await repo.update_job_fields(job_id, {
                        "status": "paused",
                        "pause_reason": pause_reason,
                        "current_failure_event_id": (
                            str(auto_creation.get("failure_event_id"))
                            if isinstance(auto_creation, Mapping)
                            and str(auto_creation.get("failure_event_id") or "")
                            else None
                        ),
                        "current_chapter_id": (
                            str((blockers.get("chapter_ids") or [""])[0])
                            if pause_reason
                            in {
                                "cost_cap",
                                "attempt_capacity",
                                "uncertain_attempt",
                                "source_changed",
                            }
                            else None
                        ),
                        "active_slot": None,
                        "error": {
                            "step": (
                                "reference_card_review"
                                if pause_reason == "reference_card_review"
                                else pause_reason
                            ),
                            "message": (
                                "New reference-card candidates must be reviewed "
                                "before the next chapter"
                            ),
                            "candidate_ids": list(
                                blockers.get("candidate_ids") or []
                            ),
                            "candidate_names": list(
                                blockers.get("names") or []
                            ),
                            **(
                                {"auto_creation": dict(auto_creation)}
                                if isinstance(auto_creation, Mapping)
                                else {}
                            ),
                        },
                    })
                    return
            if chapter is None:
                if required_finalization_authorized:
                    await repo.update_job_fields(job_id, {
                        "status": "failed",
                        "pause_reason": None,
                        "active_slot": None,
                        "error": {
                            "step": "required_chapter_finalization_recovery",
                            "message": (
                                "Required finalization worklist changed before execution"
                            ),
                        },
                    })
                    return
                if required_state_authorized:
                    await repo.update_job_fields(job_id, {
                        "status": "failed",
                        "pause_reason": None,
                        "active_slot": None,
                        "error": {
                            "step": "required_chapter_state_recovery",
                            "message": (
                                "Required state worklist changed before execution"
                            ),
                        },
                    })
                    return
                if required_review_authorized:
                    await repo.update_job_fields(job_id, {
                        "status": "failed",
                        "pause_reason": None,
                        "active_slot": None,
                        "error": {
                            "step": "required_chapter_review_recovery",
                            "message": (
                                "Required review worklist changed before execution"
                            ),
                        },
                    })
                    return
                if job.get("scope") == "book":
                    await finalize_book_job(
                        repo,
                        job_id,
                        job,
                        deps.inspect_book_completion,
                        deps.guard_book_completion_publication,
                    )
                    return
                await repo.update_job_fields(job_id, {
                    "status": "completed", "current_chapter_id": None,
                    "current_failure_event_id": None, "active_slot": None,
                })
                return

            try:
                required_finalization_execution = (
                    readiness_chapter_uses_required_chapter_finalization(
                        job.get("readiness"),
                        chapter_id=str(chapter.get("_id") or ""),
                    )
                    if required_finalization_authorized
                    else False
                )
                required_state_execution = (
                    readiness_chapter_uses_required_chapter_state(
                        job.get("readiness"),
                        chapter_id=str(chapter.get("_id") or ""),
                    )
                    if required_state_authorized
                    else False
                )
                required_review_execution = (
                    readiness_chapter_uses_required_chapter_review(
                        job.get("readiness"),
                        chapter_id=str(chapter.get("_id") or ""),
                    )
                    if required_review_authorized
                    else False
                )
                candidate_execution = (
                    False
                    if required_finalization_execution
                    or required_review_execution
                    or required_state_execution
                    else readiness_chapter_uses_candidate_pipeline(
                        job.get("readiness"),
                        chapter_id=str(chapter.get("_id") or ""),
                    )
                )
                if required_review_recovery and not required_review_execution:
                    raise ValueError(
                        "Required review journals do not match the frozen chapter"
                    )
                if required_state_recovery and not required_state_execution:
                    raise ValueError(
                        "Required state journal does not match the frozen chapter"
                    )
                if candidate_recovery and not candidate_execution:
                    raise ValueError(
                        "Candidate checkpoints do not match the frozen chapter mode"
                    )
                if job_mutation_recovery and candidate_execution:
                    raise ValueError(
                        "State-only mutation recovery entered candidate generation"
                    )
            except ValueError as exc:
                await repo.update_job_fields(job_id, {
                    "status": "failed",
                    "pause_reason": None,
                    "active_slot": None,
                    "error": {
                        "step": (
                            "required_chapter_finalization_recovery"
                            if required_finalization_authorized
                            else "required_chapter_state_recovery"
                            if required_state_authorized
                            else "required_chapter_review_recovery"
                            if required_review_authorized
                            else "candidate_pipeline_recovery"
                        ),
                        "message": str(exc),
                    },
                })
                return
            if (
                required_finalization_execution
                and deps.run_required_finalization_chapter is None
            ):
                await repo.update_job_fields(job_id, {
                    "status": "failed",
                    "pause_reason": None,
                    "active_slot": None,
                    "error": {
                        "step": "required_chapter_finalization_recovery",
                        "message": (
                            "Required chapter finalization runner is unavailable"
                        ),
                    },
                })
                return
            if (
                required_state_execution
                and deps.run_required_state_chapter is None
            ):
                await repo.update_job_fields(job_id, {
                    "status": "failed",
                    "pause_reason": None,
                    "active_slot": None,
                    "error": {
                        "step": "required_chapter_state_recovery",
                        "message": "Required chapter state runner is unavailable",
                    },
                })
                return
            if (
                required_review_execution
                and deps.run_required_review_chapter is None
            ):
                await repo.update_job_fields(job_id, {
                    "status": "failed",
                    "pause_reason": None,
                    "active_slot": None,
                    "error": {
                        "step": "required_chapter_review_recovery",
                        "message": "Required chapter review runner is unavailable",
                    },
                })
                return
            if candidate_execution and deps.run_candidate_chapter is None:
                await repo.update_job_fields(job_id, {
                    "status": "failed",
                    "pause_reason": None,
                    "active_slot": None,
                    "error": {
                        "step": "candidate_pipeline_recovery",
                        "message": "Candidate checkpoint runner is unavailable",
                    },
                })
                return

            try:
                if required_finalization_execution:
                    assert deps.run_required_finalization_chapter is not None
                    finalization_outcome = (
                        await deps.run_required_finalization_chapter(
                            str(job["novel_id"]),
                            chapter,
                        )
                    )
                    await repo.complete_required_chapter_finalization(
                        job_id,
                        finalization_outcome,
                    )
                    return
                if required_state_execution:
                    assert deps.run_required_state_chapter is not None
                    state_outcome = await deps.run_required_state_chapter(
                        str(job["novel_id"]),
                        chapter,
                    )
                    if state_outcome.phase == "ready":
                        assert state_outcome.state_candidate is not None
                        await repo.publish_required_state_candidate(
                            job_id,
                            state_outcome.state_candidate,
                        )
                    else:
                        assert state_outcome.reason_code is not None
                        await repo.pause_required_chapter_state(
                            job_id,
                            chapter_id=str(chapter["_id"]),
                            reason_code=state_outcome.reason_code,
                            reextraction_count=(
                                state_outcome.reextraction_count
                            ),
                        )
                    return
                if required_review_execution:
                    assert deps.run_required_review_chapter is not None
                    required_outcome = await deps.run_required_review_chapter(
                        str(job["novel_id"]),
                        chapter,
                    )
                    if required_outcome.phase == "reviewed":
                        assert required_outcome.reviewed_candidate is not None
                        await repo.publish_required_reviewed_candidate(
                            job_id,
                            required_outcome.reviewed_candidate,
                        )
                    else:
                        assert required_outcome.reason_code is not None
                        await repo.pause_required_chapter_review(
                            job_id,
                            chapter_id=str(chapter["_id"]),
                            phase=required_outcome.phase,
                            reason_code=required_outcome.reason_code,
                            repair_count=required_outcome.repair_count,
                        )
                    return
                if candidate_execution:
                    assert deps.run_candidate_chapter is not None
                    candidate_outcome = await deps.run_candidate_chapter(
                        str(job["novel_id"]),
                        chapter,
                    )
                    await repo.complete_candidate_pipeline_chapter(
                        job_id,
                        chapter_id=str(chapter["_id"]),
                        expected_checkpoints=candidate_outcome.checkpoints,
                        entry=candidate_outcome.progress,
                        tokens_delta=0,
                        expected_narrative_revision=(
                            candidate_outcome.expected_narrative_revision
                        ),
                        next_narrative_revision=(
                            candidate_outcome.next_narrative_revision
                        ),
                    )
                else:
                    outcome = await deps.run_chapter(
                        str(job["novel_id"]),
                        chapter,
                    )
            except TokenBudgetExceeded as exc:
                diagnostic = build_failure_diagnostic(
                    exc,
                    step="run_chapter",
                    chapter_id=str(chapter["_id"]),
                    occurred_at=get_utc_now(),
                )
                await _persist_diagnostic(repo, job_id, diagnostic)
                if required_state_execution:
                    await _pause_required_state_execution(
                        repo,
                        job_id,
                        chapter_id=str(chapter["_id"]),
                        reason="cost_cap",
                    )
                elif required_review_execution:
                    latest = await repo.get_job(job_id)
                    await repo.pause_required_chapter_review(
                        job_id,
                        chapter_id=str(chapter["_id"]),
                        phase="blocked",
                        reason_code="cost_cap",
                        repair_count=required_review_repair_count(latest),
                    )
                elif candidate_execution:
                    await _pause_candidate_execution(
                        repo,
                        job_id,
                        chapter_id=str(chapter["_id"]),
                        reason="cost_cap",
                    )
                else:
                    await _pause(repo, job_id, "cost_cap")
                return

            except AttemptCapacityExceeded as exc:
                diagnostic = build_failure_diagnostic(
                    exc,
                    step="run_chapter",
                    chapter_id=str(chapter["_id"]),
                    occurred_at=get_utc_now(),
                )
                await _persist_diagnostic(repo, job_id, diagnostic)
                if required_state_execution:
                    await _pause_required_state_execution(
                        repo,
                        job_id,
                        chapter_id=str(chapter["_id"]),
                        reason="attempt_capacity",
                    )
                elif required_review_execution:
                    latest = await repo.get_job(job_id)
                    await repo.pause_required_chapter_review(
                        job_id,
                        chapter_id=str(chapter["_id"]),
                        phase="blocked",
                        reason_code="attempt_capacity",
                        repair_count=required_review_repair_count(latest),
                    )
                elif candidate_execution:
                    await _pause_candidate_execution(
                        repo,
                        job_id,
                        chapter_id=str(chapter["_id"]),
                        reason="attempt_capacity",
                    )
                else:
                    await _pause(repo, job_id, "attempt_capacity")
                return
            except ChapterPipelineFailed as exc:
                if required_state_execution:
                    await _handle_chapter_failure(repo, job_id, chapter, exc)
                    return
                if required_review_execution:
                    diagnostic = build_failure_diagnostic(
                        exc,
                        step="required_chapter_review",
                        chapter_id=str(chapter["_id"]),
                        occurred_at=get_utc_now(),
                    )
                    await _persist_diagnostic(repo, job_id, diagnostic)
                    latest = await repo.get_job(job_id)
                    await repo.pause_required_chapter_review(
                        job_id,
                        chapter_id=str(chapter["_id"]),
                        phase="blocked",
                        reason_code="required_review_execution_failed",
                        repair_count=required_review_repair_count(latest),
                    )
                    return
                if candidate_execution:
                    await _handle_candidate_chapter_failure(
                        repo,
                        job_id,
                        chapter,
                        exc,
                    )
                    return
                incomplete = exc.__cause__
                if not isinstance(incomplete, IncompleteProseGeneration):
                    await _handle_chapter_failure(repo, job_id, chapter, exc)
                    return
                outcome = exc.outcome
                attempts = list(outcome.attempts)
                await _persist_attempts(repo, job_id, attempts)
                diagnostic = build_failure_diagnostic(
                    exc,
                    step="prose",
                    chapter_id=str(chapter["_id"]),
                    attempts=attempts,
                    occurred_at=get_utc_now(),
                )
                await _persist_diagnostic(repo, job_id, diagnostic)
                checkpoint = _incomplete_prose_checkpoint(
                    incomplete.completion,
                    chapter_id=str(chapter["_id"]),
                )
                progress = outcome_to_progress(outcome)
                progress["incomplete_prose"] = checkpoint
                await repo.append_progress(
                    job_id,
                    progress,
                    tokens_delta=0 if attempts else outcome.tokens,
                )
                boundary_pause_reason = _pre_dispatch_pause_reason(
                    incomplete_prose_pre_dispatch_boundary_code(
                        incomplete.completion
                    )
                )
                if boundary_pause_reason is not None:
                    await _pause(repo, job_id, boundary_pause_reason)
                    return
                await repo.update_job_fields(job_id, {
                    "status": "paused",
                    "pause_reason": "incomplete_scene",
                    "current_chapter_id": None,
                    "active_slot": None,
                    "incomplete_prose": checkpoint,
                    "error": {
                        "step": "prose",
                        "chapter_id": str(chapter["_id"]),
                        "message": "An incomplete prose scene requires manual continuation",
                        "reason_codes": checkpoint["reason_codes"],
                    },
                })
                return

            except Exception as exc:  # noqa: BLE001 — fail-fast，人工 resume 即重试
                if required_state_execution:
                    diagnostic = build_failure_diagnostic(
                        exc,
                        step="required_chapter_state",
                        chapter_id=str(chapter["_id"]),
                        occurred_at=get_utc_now(),
                    )
                    await _persist_diagnostic(repo, job_id, diagnostic)
                    await repo.update_job_fields(job_id, {
                        "status": "paused",
                        "pause_reason": "required_state_execution_failed",
                        "current_chapter_id": str(chapter["_id"]),
                        "active_slot": None,
                        "error": {
                            "step": "required_chapter_state",
                            "chapter_id": str(chapter["_id"]),
                            "message": str(exc),
                            "reason_codes": [
                                "required_state_execution_failed"
                            ],
                        },
                    })
                elif required_review_execution:
                    diagnostic = build_failure_diagnostic(
                        exc,
                        step="required_chapter_review",
                        chapter_id=str(chapter["_id"]),
                        occurred_at=get_utc_now(),
                    )
                    await _persist_diagnostic(repo, job_id, diagnostic)
                    latest = await repo.get_job(job_id)
                    await repo.pause_required_chapter_review(
                        job_id,
                        chapter_id=str(chapter["_id"]),
                        phase="blocked",
                        reason_code="required_review_execution_failed",
                        repair_count=required_review_repair_count(latest),
                    )
                elif candidate_execution:
                    await _handle_candidate_chapter_failure(
                        repo,
                        job_id,
                        chapter,
                        exc,
                    )
                else:
                    await _handle_chapter_failure(repo, job_id, chapter, exc)
                return

            if candidate_execution:
                if control.pause_requested:
                    await _pause(repo, job_id, "manual")
                    return
                job = await repo.get_job(job_id)
                if job_planner.should_checkpoint(
                    len(job["progress"]),
                    int(job.get("last_checkpoint_index", 0)),
                    (
                        int(job["checkpoint_interval"])
                        if job.get("checkpoint_interval") is not None
                        else None
                    ),
                ):
                    await _pause(repo, job_id, "checkpoint")
                    return
                continue

            await _persist_attempts(repo, job_id, outcome.attempts)
            progress = outcome_to_progress(outcome)
            if outcome.mutation_receipts:
                await repo.complete_job_mutation_chapter(
                    job_id,
                    receipt=outcome.mutation_receipts[0],
                    entry=progress,
                    tokens_delta=0 if outcome.attempts else outcome.tokens,
                )
            else:
                await repo.append_progress(
                    job_id,
                    progress,
                    tokens_delta=0 if outcome.attempts else outcome.tokens,
                    resolves_current_failure=True,
                )

            if outcome.requires_authorization_confirmation:
                await repo.update_job_fields(job_id, {
                    "status": "paused",
                    "pause_reason": "authorization_scope_increased",
                    "current_chapter_id": None,
                    "active_slot": None,
                    "authorization_confirmation_required": dict(
                        outcome.authorization_recalculation
                    ),
                    "error": {
                        "step": "prose",
                        "chapter_id": outcome.chapter_id,
                        "message": (
                            "Accepted outline changed the remaining authorization "
                            "scope; a fresh readiness confirmation is required"
                        ),
                    },
                })
                return

            # 暂停判定（顺序：细纲偏离 > 事实冲突 > 提醒 > 手动 > 计划检查点）。
            if outcome.requires_outline_pause:
                await _pause(repo, job_id, "outline_deviation")
                return
            if outcome.consistency_issues:
                await _pause(repo, job_id, "conflict")
                return
            if any(
                bool(notice.get("requires_pause"))
                for notice in outcome.notices
            ):
                await _pause(repo, job_id, "requires_attention")
                return
            if control.pause_requested:
                await _pause(repo, job_id, "manual")
                return

            job = await repo.get_job(job_id)
            if job_planner.should_checkpoint(
                len(job["progress"]),
                int(job.get("last_checkpoint_index", 0)),
                (
                    int(job["checkpoint_interval"])
                    if job.get("checkpoint_interval") is not None
                    else None
                ),
            ):
                await _pause(repo, job_id, "checkpoint")
                return
    finally:
        # The launcher owns lease release and registry removal; the engine only
        # records that its fenced loop has stopped.
        logger.debug("[job %s] engine loop exited", job_id)
