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
    generation_job_repo,
)
from backend.services.generation import job_planner
from backend.services.generation.chapter_pipeline import ChapterOutcome, ChapterPipelineFailed

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
        "tokens": outcome.tokens, "consistency_issues": outcome.consistency_issues,
        "facts_added": outcome.facts_added, "threads_advanced": outcome.threads_advanced,
        "summary_written": outcome.summary_written, "dropped_ids": outcome.dropped_ids,
        "truncations": outcome.truncations,
        "attempts": outcome.attempts,
        "completed_at": get_utc_now(),
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
            if job_planner.over_budget(int(job.get("tokens_used", 0)), job.get("token_budget")):
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
            except AttemptCapacityExceeded:
                await _pause(repo, job_id, "attempt_capacity")
                return
            except Exception as exc:  # noqa: BLE001 — fail-fast，人工 resume 即重试
                logger.exception("[job %s] chapter %s failed", job_id, chapter.get("_id"))
                failed_outcome = exc.outcome if isinstance(exc, ChapterPipelineFailed) else None
                attempts = list(getattr(exc, "attempts", []) or [])
                if failed_outcome is not None:
                    attempts = list(failed_outcome.attempts)
                await _persist_attempts(repo, job_id, attempts)
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

            # 暂停判定（顺序：冲突 > 手动 > 计划检查点）。
            if outcome.consistency_issues:
                await _pause(repo, job_id, "conflict")
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
