"""恢复 standalone MongoDB 下未完成的领域 mutation。"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from backend.db.mutation import list_recoverable_mutations, resume_persisted_mutation
from backend.services.novel.chapter_service import ChapterService
from backend.services.novel.chapter_state_service import ChapterStateService
from backend.services.novel.character_state_service import CharacterStateService
from backend.services.novel.plot_thread_service import PlotThreadService
from backend.services.novel.volume_service import VolumeService


logger = logging.getLogger(__name__)
MutationExecutor = Callable[[Any, Any], Awaitable[Any]]


def _executors() -> dict[str, MutationExecutor]:
    # 延迟构建目录，避免服务模块导入期间形成循环依赖。
    return {
        "accept_chapter_outline": ChapterService._execute_accept_chapter_outline,
        "accept_chapter_state": ChapterStateService._execute_accept_chapter_state,
        "create_chapter": ChapterService._execute_create_chapter,
        "update_chapter": ChapterService._execute_update_chapter,
        "update_chapter_outline": ChapterService._execute_update_chapter_outline,
        "update_character_current_state": CharacterStateService._execute_update_current_state,
        "update_permanent_fact": CharacterStateService._execute_update_fact,
        "delete_permanent_fact": CharacterStateService._execute_delete_fact,
        "create_plot_thread": PlotThreadService._execute_create,
        "update_plot_thread": PlotThreadService._execute_update,
        "soft_delete_plot_thread": PlotThreadService._execute_soft_delete,
        "soft_delete_chapter": ChapterService._execute_soft_delete_chapter,
        "restore_chapter": ChapterService._execute_restore_chapter,
        "hard_delete_chapter": ChapterService._execute_hard_delete_chapter,
        "soft_delete_volume": VolumeService._execute_soft_delete_volume,
        "restore_volume": VolumeService._execute_restore_volume,
        "hard_delete_volume": VolumeService._execute_hard_delete_volume,
        "accept_volume_outline": VolumeService._execute_accept_volume_outline,
        "create_volume": VolumeService._execute_create_volume,
        "update_volume": VolumeService._execute_update_volume,
    }


async def recover_pending_mutations(novel_id: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """按更新时间恢复已知 mutation；未知操作保留 journal 并显式报告。"""
    recovered: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []
    executors = _executors()

    for journal in await list_recoverable_mutations(novel_id):
        journal_id = str(journal["_id"])
        operation = str(journal.get("operation") or "")
        executor = executors.get(operation)
        if executor is None:
            unsupported.append({"journal_id": journal_id, "operation": operation})
            continue
        try:
            result = await resume_persisted_mutation(journal, executor)
            recovered.append({
                "journal_id": journal_id,
                "operation": operation,
                "result": result,
            })
        except Exception as exc:
            logger.exception(
                "Mutation recovery failed: journal_id=%s operation=%s",
                journal_id,
                operation,
            )
            failed.append({
                "journal_id": journal_id,
                "operation": operation,
                "error_type": type(exc).__name__,
                "message": str(exc),
            })

    return {"recovered": recovered, "failed": failed, "unsupported": unsupported}
