"""批量作业运行循环 + 进程内任务注册表（设计 §4.1 / §7）。

主循环以作业记录为事实源：派生工作清单→跑一章→追加进度→判暂停/完成/失败。
repo 与每章管线以依赖注入，使循环可脱离 Mongo/LLM 用假件测。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from backend.db.utils import get_utc_now
from backend.db.repositories.generation_job_repository import (
    AttemptCapacityExceeded,
    TokenBudgetExceeded,
    generation_job_repo,
)
from backend.services.generation import job_planner
from backend.services.generation.chapter_pipeline import (
    ChapterOutcome,
    ChapterPipelineFailed,
    IncompleteProseGeneration,
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
class JobEngineDeps:
    list_worklist_chapters: Callable[[], Awaitable[List[Dict[str, Any]]]]
    run_chapter: Callable[[str, Dict[str, Any]], Awaitable[ChapterOutcome]]


def outcome_to_progress(outcome: ChapterOutcome) -> Dict[str, Any]:
    return {
        "chapter_id": outcome.chapter_id, "order_index": outcome.order_index,
        "steps_done": outcome.steps_done, "steps_skipped": outcome.steps_skipped,
        "agents_used": outcome.agents_used,
        "tokens": outcome.tokens, "consistency_issues": outcome.consistency_issues,
        "outline_adherence": outcome.outline_adherence,
        "facts_added": outcome.facts_added, "threads_advanced": outcome.threads_advanced,
        "summary_written": outcome.summary_written, "dropped_ids": outcome.dropped_ids,
        "truncations": outcome.truncations,
        "attempts": outcome.attempts,
        "step_outcomes": outcome.step_outcomes,
        "notices": outcome.notices,
        "completed_at": get_utc_now(),
    }


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
                "word_count": integer(raw.get("word_count")),
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

            # 成本上限：开下一章前的软天花板。
            committed_or_reserved = int(job.get("tokens_used", 0)) + int(
                job.get("tokens_reserved", 0) or 0
            )
            if job_planner.over_budget(committed_or_reserved, job.get("token_budget")):
                await _pause(repo, job_id, "cost_cap")
                return

            chapters = await deps.list_worklist_chapters()
            chapter = job_planner.first_needing_work(chapters)
            if chapter is None:
                await repo.update_job_fields(job_id, {
                    "status": "completed", "current_chapter_id": None, "active_slot": None,
                })
                return

            await repo.update_job_fields(job_id, {"current_chapter_id": str(chapter["_id"])})
            try:
                outcome = await deps.run_chapter(str(job["novel_id"]), chapter)
            except TokenBudgetExceeded as exc:
                diagnostic = build_failure_diagnostic(
                    exc,
                    step="run_chapter",
                    chapter_id=str(chapter["_id"]),
                    occurred_at=get_utc_now(),
                )
                await _persist_diagnostic(repo, job_id, diagnostic)
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
                await _pause(repo, job_id, "attempt_capacity")
                return
            except ChapterPipelineFailed as exc:
                incomplete = exc.__cause__
                if not isinstance(incomplete, IncompleteProseGeneration):
                    raise
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
                logger.exception("[job %s] chapter %s failed", job_id, chapter.get("_id"))
                failed_outcome = exc.outcome if isinstance(exc, ChapterPipelineFailed) else None
                attempts = list(getattr(exc, "attempts", []) or [])
                if failed_outcome is not None:
                    attempts = list(failed_outcome.attempts)
                await _persist_attempts(repo, job_id, attempts)
                diagnostic = build_failure_diagnostic(
                    exc,
                    step=getattr(exc, "step", "run_chapter"),
                    chapter_id=str(chapter["_id"]),
                    attempts=attempts,
                    occurred_at=get_utc_now(),
                )
                await _persist_diagnostic(repo, job_id, diagnostic)
                if diagnostic["category"] == "source_changed":
                    await repo.update_job_fields(job_id, {
                        "status": "paused",
                        "pause_reason": "source_changed",
                        "current_chapter_id": None,
                        "active_slot": None,
                        "error": {
                            "step": getattr(exc, "step", "run_chapter"),
                            "chapter_id": str(chapter["_id"]),
                            "message": "Source changed during generation",
                            "attempts": attempts,
                        },
                    })
                    return
                latest_job = await repo.get_job(job_id)
                has_uncertain = bool(latest_job.get("has_uncertain_attempts"))
                await repo.update_job_fields(job_id, {
                    "status": "interrupted" if has_uncertain else "failed",
                    "pause_reason": "uncertain_attempt" if has_uncertain else None,
                    "current_chapter_id": None,
                    "active_slot": None,
                    "error": {
                        "step": getattr(exc, "step", "run_chapter"),
                        "chapter_id": str(chapter["_id"]),
                        "message": str(exc),
                        "attempts": attempts,
                    },
                })
                return

            await _persist_attempts(repo, job_id, outcome.attempts)
            await repo.append_progress(
                job_id,
                outcome_to_progress(outcome),
                tokens_delta=0 if outcome.attempts else outcome.tokens,
            )

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
        _REGISTRY.pop(job_id, None)
