"""伏笔跨集合审计（读 chapters + plot_threads）。

审计要同时读 chapters 与 plot_threads，不塞进只认单集合的伏笔仓储。
孤儿布尔判定（是否被任一章节引用）对 order_index 是否全书唯一免疫；
referenced_by_chapter_orders 里的章号仅作展示（已知 order_index 非全书唯一，
跨卷同号时展示略有歧义，见设计 §9，不影响是否孤儿的判断）。
"""

from __future__ import annotations

from typing import Dict, List

from backend.db.repositories.chapter_repository import chapter_repo


async def audit_thread_references(novel_id: str) -> Dict[str, List[int]]:
    chapters = await chapter_repo.get_chapters_by_novel(novel_id)
    refs: Dict[str, List[int]] = {}
    for chapter in chapters:
        outline = chapter.get("outline") or {}
        order = chapter.get("order_index")
        thread_ids = list(outline.get("threads_planted") or []) + list(
            outline.get("threads_resolved") or []
        )
        for tid in thread_ids:
            key = str(tid)
            bucket = refs.setdefault(key, [])
            if order is not None and order not in bucket:
                bucket.append(order)
    for key in refs:
        refs[key].sort()
    return refs
