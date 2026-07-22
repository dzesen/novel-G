"""批量作业的纯决策逻辑：无 IO、无 LLM、无 MongoDB。可用普通 dict 单测。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

RESUMABLE_STATUSES = frozenset({"paused", "interrupted", "failed"})


def chapter_needs_work(chapter: Dict[str, Any]) -> bool:
    """一章未完整跑完管线：缺 content 或缺 summary（状态未回填）。设计 §3.1。

    只看 content 是错的——有正文却没回填状态，其新事实没提交，会破坏后续章上下文。
    """
    has_content = bool(str(chapter.get("content") or "").strip())
    has_summary = bool(str(chapter.get("summary") or "").strip())
    return not (has_content and has_summary)


def first_needing_work(chapters: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """按入参顺序返回第一个仍需处理的章；全完成返回 None。

    **不重排**——排序职责在工作清单提供者（整卷 get_chapters_by_volume 天然按
    order_index 升序；整本 order_book_chapters 复合排序）。设计 §2.1。
    """
    for chapter in chapters:
        if chapter_needs_work(chapter):
            return chapter
    return None


def order_book_chapters(chapters: List[Dict[str, Any]],
                        volume_order_map: Dict[str, int]) -> List[Dict[str, Any]]:
    """整本工作清单排序：按 (卷叙事序 volume.order_index, 卷内 chapter.order_index) 升序。

    volume_order_map: {volume_id(str): volume.order_index}。章的 volume_id 可能是
    ObjectId，故按 str() 查表。未知卷排末尾（+∞），不崩。稳定排序。设计 §2.2。
    """
    def _key(chapter: Dict[str, Any]):
        vol_order = volume_order_map.get(str(chapter.get("volume_id")), float("inf"))
        return (vol_order, int(chapter.get("order_index") or 0))
    return sorted(chapters, key=_key)


def should_checkpoint(progress_len: int, last_checkpoint_index: int, interval: int) -> bool:
    """距上次检查点已满 interval 章。任何 resume 会把 last_checkpoint_index 推进到当前
    progress 长度（设计 §7），故此处只需判增量。"""
    return (progress_len - last_checkpoint_index) >= interval


def over_budget(tokens_used: int, token_budget: Optional[int]) -> bool:
    """章边界软天花板；token_budget=None 表示不限。设计 §8.1。"""
    if token_budget is None:
        return False
    return tokens_used >= token_budget


def can_resume(status: str) -> bool:
    return status in RESUMABLE_STATUSES


def can_start_new(running_count: int) -> bool:
    """全局同时只允许一个在跑作业。设计 §8.2。"""
    return running_count == 0
