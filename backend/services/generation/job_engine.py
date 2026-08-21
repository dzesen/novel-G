"""批量作业运行循环 + 进程内任务注册表（设计 §4.1 / §7）。

主循环以作业记录为事实源：派生工作清单→跑一章→追加进度→判暂停/完成/失败。
repo 与每章管线以依赖注入，使循环可脱离 Mongo/LLM 用假件测。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from backend.db.utils import get_utc_now
from backend.db.repositories.generation_job_repository import (
    AttemptCapacityExceeded,
    TokenBudgetExceeded,
    generation_job_repo,
)
from backend.services.generation import job_planner
from backend.services.generation.candidate_repair_contracts import (
    CandidatePipelineCheckpointV1,
    CandidatePipelineProgressV1,
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
from backend.services.generation.failure_diagnostics import (
    build_failure_diagnostic,
)

logger = logging.getLogger(__name__)

# 进程内任务注册表：job_id → (asyncio.Task, JobControl)。易失，重启即丢（设计 §4.1/§4.3）。
_REGISTRY: Dict[str, "tuple[asyncio.Task, JobControl]"] = {}


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

    inspect_reference_card_blockers: Optional[
        Callable[[], Awaitable[Dict[str, Any] | None]]
    ] = None

    resolve_reference_card_blockers: Optional[
        Callable[[], Awaitable[Dict[str, Any] | None]]
    ] = None


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

    pause_reason = {
        "token_budget_exceeded_before_dispatch": "cost_cap",
        "attempt_capacity_exhausted": "attempt_capacity",
    }.get(diagnostic["code"])
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
    latest = await repo.get_job(job_id)
    checkpoints = latest.get("candidate_pipeline_checkpoints")
    preserve = isinstance(checkpoints, list) and bool(checkpoints)
    has_uncertain = bool(latest.get("has_uncertain_attempts"))
    candidate_code = str(getattr(exc, "code", "") or "")
    authorization_scope_increased = (
        candidate_code == "authorization_scope_increased"
    )
    source_changed = diagnostic["category"] == "source_changed"
    source_changed = source_changed or (
        candidate_code == "candidate_narrative_revision_changed"
    )
    fields = {
        "status": (
            "interrupted"
            if has_uncertain
            else "paused"
            if source_changed or authorization_scope_increased
            else "failed"
        ),
        "pause_reason": (
            "uncertain_attempt"
            if has_uncertain
            else "authorization_scope_increased"
            if authorization_scope_increased
            else "source_changed"
            if source_changed
            else None
        ),
        "current_chapter_id": (
            chapter_id
            if preserve or authorization_scope_increased
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
    if source_changed:
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
                    "status": "aborted", "current_chapter_id": None, "active_slot": None,
                })
                return

            job = await repo.get_job(job_id)

            try:
                candidate_authorized = readiness_uses_candidate_pipeline(
                    job.get("readiness")
                )
            except ValueError as exc:
                await repo.update_job_fields(job_id, {
                    "status": "failed",
                    "pause_reason": None,
                    "active_slot": None,
                    "error": {
                        "step": "candidate_pipeline_recovery",
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
            if candidate_recovery and job_mutation_recovery:
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
            if job_mutation_recovery and not candidate_authorized:
                await repo.update_job_fields(job_id, {
                    "status": "failed",
                    "pause_reason": None,
                    "active_slot": None,
                    "error": {
                        "step": "job_mutation_recovery",
                        "message": "Job mutation has no bound candidate authorization",
                    },
                })
                return
            # 成本上限：开下一章前的软天花板。
            committed_or_reserved = int(job.get("tokens_used", 0)) + int(
                job.get("tokens_reserved", 0) or 0
            )
            if (
                not candidate_recovery
                and job_planner.over_budget(
                    committed_or_reserved,
                    job.get("token_budget"),
                )
            ):
                await _pause(repo, job_id, "cost_cap")
                return

            chapters = await deps.list_worklist_chapters()
            if candidate_recovery or job_mutation_recovery:
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
                            "step": "candidate_pipeline_recovery",
                            "message": (
                                "Candidate checkpoint chapter is unavailable"
                            ),
                        },
                    })
                    return
            else:
                chapter = job_planner.first_needing_work(chapters)
            reference_card_blocker_check = (
                deps.resolve_reference_card_blockers
                or deps.inspect_reference_card_blockers
            )
            if (
                not candidate_recovery
                and not job_mutation_recovery
                and chapter is not None
                and reference_card_blocker_check is not None
            ):
                try:
                    blockers = await reference_card_blocker_check()
                except Exception as exc:  # noqa: BLE001 - preserve exact recovery
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
                await repo.update_job_fields(job_id, {
                    "status": "completed", "current_chapter_id": None, "active_slot": None,
                })
                return

            try:
                candidate_execution = readiness_chapter_uses_candidate_pipeline(
                    job.get("readiness"),
                    chapter_id=str(chapter.get("_id") or ""),
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
                        "step": "candidate_pipeline_recovery",
                        "message": str(exc),
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

            await repo.update_job_fields(job_id, {"current_chapter_id": str(chapter["_id"])})
            try:
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
                if candidate_execution:
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
                if candidate_execution:
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
                budget_blocked = (
                    checkpoint["pause_reason"]
                    == "token_budget_exceeded_before_dispatch"
                    or "token_budget_exceeded_before_dispatch"
                    in checkpoint["reason_codes"]
                )
                if budget_blocked:
                    await _pause(repo, job_id, "cost_cap")
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
                if candidate_execution:
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
                    int(job["checkpoint_interval"]),
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
            if job_planner.should_checkpoint(len(job["progress"]), int(job.get("last_checkpoint_index", 0)),
                                             int(job["checkpoint_interval"])):
                await _pause(repo, job_id, "checkpoint")
                return
    finally:
        # The launcher owns lease release and registry removal; the engine only
        # records that its fenced loop has stopped.
        logger.debug("[job %s] engine loop exited", job_id)
