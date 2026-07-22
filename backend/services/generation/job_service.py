"""批量作业服务：CRUD、全局单作业守卫、拉起/控制进程内任务。"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from pymongo.errors import DuplicateKeyError

from backend.db.repositories.generation_job_repository import generation_job_repo
from backend.db.utils import to_object_id
from backend.services.generation import job_planner
from backend.services.generation.chapter_pipeline import run_chapter
from backend.services.generation.attempt_scope import JobAttemptScope
from backend.services.generation.headless_generation import (
    build_chapter_pipeline_deps,
    estimate_chapter_attempt_slots,
    estimate_worklist_attempt_capacity,
)
from backend.services.generation.job_engine import (
    JobControl, JobEngineDeps, run_job, _REGISTRY,
)
from backend.services.novel.chapter_service import ChapterService
from backend.db.repositories.volume_repository import volume_repo
from backend.db.repositories.novel_repository import novel_repo
from backend.services.generation.book_worklist import get_book_worklist

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


def _new_job_doc(
    novel_id, scope, volume_id, checkpoint_interval, token_budget, attempt_capacity
) -> Dict[str, Any]:
    return {
        "novel_id": to_object_id(novel_id), "scope": scope,
        "volume_id": to_object_id(volume_id) if volume_id else None,
        "status": "running", "pause_reason": None,
        "checkpoint_interval": int(checkpoint_interval), "token_budget": token_budget,
        "tokens_used": 0, "current_chapter_id": None, "progress": [],
        "last_checkpoint_index": 0, "error": None,
        "active_slot": "global",
        "usage_attempt_capacity": int(attempt_capacity),
        "usage_attempt_claimed": 0,
        "usage_attempt_ids": [],
        "usage_attempt_summaries": [],
        "attempt_slots": [],
        "attempt_reservation": None,
        "uncertain_attempt_ids": [],
        "has_uncertain_attempts": False,
    }


class GenerationJobService:
    @staticmethod
    async def _guard_no_running() -> None:
        running = await generation_job_repo.list_running_jobs()
        if not job_planner.can_start_new(len(running)):
            raise ConflictError("已有正在运行的批量作业，请先暂停或等待其结束")

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
            slots = estimate_chapter_attempt_slots(chapter)
            await generation_job_repo.reserve_attempts(job_id, chapter_id, slots)
            deps = build_chapter_pipeline_deps(
                lambda step: JobAttemptScope(job_id, chapter_id, step)
            )
            try:
                return await run_chapter(novel_id, chapter, deps)
            finally:
                await generation_job_repo.finish_attempt_reservation(job_id, chapter_id)

        deps = JobEngineDeps(
            list_worklist_chapters=_list_worklist,
            run_chapter=_run_chapter,
        )
        task = asyncio.create_task(run_job(job_id, deps, control))
        _REGISTRY[job_id] = (task, control)

    @staticmethod
    async def start_volume_job(volume_id: str, checkpoint_interval: int,
                               token_budget: Optional[int]) -> Dict[str, Any]:
        volume = await volume_repo.get_volume_by_id(volume_id)  # 不存在抛 NotFoundError
        novel_id = str(volume["novel_id"])
        chapters = await ChapterService.get_chapters_by_volume(volume_id, include_content=True)
        if job_planner.first_needing_work(chapters) is None:
            raise ValueError("本卷没有需要生成的章节（都已有正文与状态回填，或还没有章节存根）")
        capacity = estimate_worklist_attempt_capacity(chapters)
        async with _get_start_lock():
            await GenerationJobService._guard_no_running()
            try:
                job_id = await generation_job_repo.create_job(
                    _new_job_doc(
                        novel_id, "volume", volume_id, checkpoint_interval,
                        token_budget, capacity,
                    )
                )
            except DuplicateKeyError as exc:
                raise ConflictError("已有正在运行的批量作业，请先暂停或等待其结束") from exc
        control = JobControl()
        GenerationJobService._spawn(job_id, control)
        return await generation_job_repo.get_job(job_id)

    @staticmethod
    async def start_book_job(novel_id: str, checkpoint_interval: int,
                             token_budget: Optional[int]) -> Dict[str, Any]:
        await novel_repo.get_novel_by_id(novel_id)  # 不存在抛 NotFoundError → 404
        chapters = await get_book_worklist(novel_id, include_content=True)
        if job_planner.first_needing_work(chapters) is None:
            raise ValueError("本书没有需要生成的章节（所有卷的章节都已有正文与状态回填，或还没有章节存根）")
        capacity = estimate_worklist_attempt_capacity(chapters)
        async with _get_start_lock():
            await GenerationJobService._guard_no_running()
            try:
                job_id = await generation_job_repo.create_job(
                    _new_job_doc(
                        novel_id, "book", None, checkpoint_interval,
                        token_budget, capacity,
                    )
                )
            except DuplicateKeyError as exc:
                raise ConflictError("已有正在运行的批量作业，请先暂停或等待其结束") from exc
        control = JobControl()
        GenerationJobService._spawn(job_id, control)
        return await generation_job_repo.get_job(job_id)

    @staticmethod
    async def resume_job(
        job_id: str, *, confirm_uncertain_retry: bool = False, skip_uncertain: bool = False
    ) -> Dict[str, Any]:
        async with _get_start_lock():
            job = await generation_job_repo.get_job(job_id)
            if not job_planner.can_resume(job["status"]):
                raise ValueError(f"作业当前状态 {job['status']} 不可恢复")
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
            if job.get("has_uncertain_attempts") and confirm_uncertain_retry:
                await generation_job_repo.acknowledge_uncertain_attempts(job_id, "retry")
            await GenerationJobService._guard_no_running()
            # 任何 resume 把检查点窗口推进到当前 progress 长度（设计 §7）。
            await generation_job_repo.update_job_fields(job_id, {
                "status": "running", "pause_reason": None, "error": None,
                "active_slot": "global",
                "has_uncertain_attempts": False if confirm_uncertain_retry else bool(
                    job.get("has_uncertain_attempts")
                ),
                "last_checkpoint_index": len(job.get("progress", [])),
            })
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
