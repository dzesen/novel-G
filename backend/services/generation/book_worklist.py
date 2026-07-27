"""整本工作清单派生：跨卷取章 + 按 (卷叙事序, 卷内章序) 复合排序（设计 §3.3）。

批量作业专属的取数+排序编排，故落在 generation 包，不塞进 ChapterService。
纯排序委托 job_planner.order_book_chapters。
"""
from __future__ import annotations

from typing import Any, Dict, List

from backend.db.repositories.volume_repository import volume_repo
from backend.services.novel.chapter_service import ChapterService
from backend.services.generation import job_planner
from backend.services.novel.state_completion import state_completion_module


async def get_book_worklist(novel_id: str, *, include_content: bool = True) -> List[Dict[str, Any]]:
    """整本工作清单：全书章节按 (volume.order_index, chapter.order_index) 复合升序。

    volume.order_index 全书唯一（volume_repository 保证），是可靠的卷叙事序来源；
    **不复用** chapter_repo.get_chapters_by_novel 的 (volume_id, order_index) 排序——那按
    ObjectId 创建序排卷，乱序建卷/调卷序时会错。设计 §2.3。
    """
    volumes = await volume_repo.get_volumes_by_novel(novel_id)
    chapters = await ChapterService.get_chapters_by_novel(novel_id, include_content=include_content)
    volume_order_map = {str(v["_id"]): int(v.get("order_index") or 0) for v in volumes}
    ordered = job_planner.order_book_chapters(chapters, volume_order_map)
    return await state_completion_module.attach_many(ordered)
