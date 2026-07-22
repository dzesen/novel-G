"""恢复 standalone MongoDB 下未完成的领域 mutation。"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from backend.db.mutation import MutationEngine, MutationHandlerSpec, RecoveryScope
from backend.services.novel.chapter_service import ChapterService
from backend.services.novel.chapter_state_service import ChapterStateService
from backend.services.novel.character_state_service import CharacterStateService
from backend.services.novel.plot_thread_service import PlotThreadService
from backend.services.novel.volume_service import VolumeService


MutationExecutor = Callable[[Any, Any], Awaitable[Any]]


def _executors() -> dict[tuple[str, int], MutationHandlerSpec[Any]]:
    # 延迟构建目录，避免服务模块导入期间形成循环依赖。
    callbacks: dict[tuple[str, int], MutationExecutor] = {
        ("accept_chapter_outline", 1): ChapterService._execute_accept_chapter_outline,
        ("accept_chapter_state", 1): ChapterStateService._execute_accept_chapter_state,
        ("create_chapter", 1): ChapterService._execute_create_chapter,
        ("create_chapter", 2): ChapterService._execute_create_chapter,
        ("update_chapter", 1): ChapterService._execute_update_chapter,
        ("update_chapter", 2): ChapterService._execute_update_chapter,
        ("update_chapter_outline", 1): ChapterService._execute_update_chapter_outline,
        ("update_character_current_state", 1): CharacterStateService._execute_update_current_state,
        ("update_permanent_fact", 1): CharacterStateService._execute_update_fact,
        ("delete_permanent_fact", 1): CharacterStateService._execute_delete_fact,
        ("create_plot_thread", 1): PlotThreadService._execute_create,
        ("update_plot_thread", 1): PlotThreadService._execute_update,
        ("soft_delete_plot_thread", 1): PlotThreadService._execute_soft_delete,
        ("soft_delete_chapter", 1): ChapterService._execute_soft_delete_chapter,
        ("soft_delete_chapter", 2): ChapterService._execute_soft_delete_chapter,
        ("restore_chapter", 1): ChapterService._execute_restore_chapter,
        ("restore_chapter", 2): ChapterService._execute_restore_chapter,
        ("hard_delete_chapter", 1): ChapterService._execute_hard_delete_chapter,
        ("soft_delete_volume", 1): VolumeService._execute_soft_delete_volume,
        ("soft_delete_volume", 2): VolumeService._execute_soft_delete_volume,
        ("restore_volume", 1): VolumeService._execute_restore_volume,
        ("restore_volume", 2): VolumeService._execute_restore_volume,
        ("hard_delete_volume", 1): VolumeService._execute_hard_delete_volume,
        ("accept_volume_outline", 1): VolumeService._execute_accept_volume_outline,
        ("accept_volume_outline", 2): VolumeService._execute_accept_volume_outline,
        ("create_volume", 1): VolumeService._execute_create_volume,
        ("create_volume", 2): VolumeService._execute_create_volume,
        ("update_volume", 1): VolumeService._execute_update_volume,
    }
    return {
        key: MutationHandlerSpec(
            callback,
            advances_narrative_revision=True,
        )
        for key, callback in callbacks.items()
    }


async def recover_pending_mutations(novel_id: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """按更新时间恢复已知 mutation；未知操作保留 journal 并显式报告。"""
    return await MutationEngine(_executors()).recover(
        RecoveryScope(novel_id=novel_id)
    )
