"""批量作业服务：CRUD、全局单作业守卫、拉起/控制进程内任务。"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from backend.db.repositories.generation_job_repository import generation_job_repo
from backend.db.utils import to_object_id
from backend.services.generation import job_planner
from backend.services.generation.chapter_pipeline import run_chapter
from backend.services.generation.headless_generation import build_chapter_pipeline_deps
from backend.services.generation.job_engine import (
    JobControl, JobEngineDeps, run_job, _REGISTRY,
)
from backend.services.novel.chapter_service import ChapterService
from backend.db.repositories.volume_repository import volume_repo


class ConflictError(Exception):
    """已有在跑作业（全局单作业约束）。路由映射为 409。"""


def _new_job_doc(novel_id, volume_id, checkpoint_interval, token_budget) -> Dict[str, Any]:
    return {
        "novel_id": to_object_id(novel_id), "scope": "volume", "volume_id": to_object_id(volume_id),
        "status": "running", "pause_reason": None,
        "checkpoint_interval": int(checkpoint_interval), "token_budget": token_budget,
        "tokens_used": 0, "current_chapter_id": None, "progress": [],
        "last_checkpoint_index": 0, "error": None,
    }


class GenerationJobService:
    @staticmethod
    async def _guard_no_running() -> None:
        running = await generation_job_repo.list_running_jobs()
        if not job_planner.can_start_new(len(running)):
            raise ConflictError("已有正在运行的批量作业，请先暂停或等待其结束")

    @staticmethod
    def _spawn(job_id: str, control: JobControl) -> None:
        """在当前事件循环拉起后台任务并记入注册表。测试用 monkeypatch 换成 no-op。"""
        deps = JobEngineDeps(
            list_volume_chapters=ChapterService.get_chapters_by_volume,
            run_chapter=lambda nid, ch: run_chapter(nid, ch, build_chapter_pipeline_deps()),
        )
        task = asyncio.create_task(run_job(job_id, deps, control))
        _REGISTRY[job_id] = (task, control)

    @staticmethod
    async def start_volume_job(volume_id: str, checkpoint_interval: int,
                               token_budget: Optional[int]) -> Dict[str, Any]:
        volume = await volume_repo.get_volume_by_id(volume_id)  # 不存在抛 NotFoundError
        novel_id = str(volume["novel_id"])
        chapters = await ChapterService.get_chapters_by_volume(volume_id)
        if job_planner.next_chapter_needing_work(chapters) is None:
            raise ValueError("本卷没有需要生成的章节（都已有正文与状态回填，或还没有章节存根）")
        await GenerationJobService._guard_no_running()

        job_id = await generation_job_repo.create_job(_new_job_doc(novel_id, volume_id, checkpoint_interval, token_budget))
        control = JobControl()
        GenerationJobService._spawn(job_id, control)
        return await generation_job_repo.get_job(job_id)

    @staticmethod
    async def resume_job(job_id: str) -> Dict[str, Any]:
        job = await generation_job_repo.get_job(job_id)
        if not job_planner.can_resume(job["status"]):
            raise ValueError(f"作业当前状态 {job['status']} 不可恢复")
        await GenerationJobService._guard_no_running()
        # 任何 resume 把检查点窗口推进到当前 progress 长度（设计 §7）。
        await generation_job_repo.update_job_fields(job_id, {
            "status": "running", "pause_reason": None, "error": None,
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
        await generation_job_repo.update_job_fields(job_id, {"status": "aborted", "current_chapter_id": None})
        return await generation_job_repo.get_job(job_id)

    @staticmethod
    async def get_job(job_id: str) -> Dict[str, Any]:
        return await generation_job_repo.get_job(job_id)

    @staticmethod
    async def list_jobs(novel_id: str) -> List[Dict[str, Any]]:
        return await generation_job_repo.list_jobs_by_novel(novel_id)
