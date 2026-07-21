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


def next_chapter_needing_work(chapters: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """按 order_index 升序，返回第一个仍需处理的章；全完成返回 None。"""
    for chapter in sorted(chapters, key=lambda c: int(c.get("order_index") or 0)):
        if chapter_needs_work(chapter):
            return chapter
    return None


def result_to_accept_state(result: Dict[str, Any]) -> Dict[str, Any]:
    """ChapterStateResultSchema dump → ChapterStateAcceptSchema dump（自动接受全部）。

    设计 §5：批量即时提交全部——new_permanent_facts 全进 accepted_permanent_facts，
    thread_updates 全进 accepted_thread_updates（去掉只给人看的 evidence）。
    consistency_issues 不进 accept（不入库），由引擎另取。
    """
    return {
        "summary": result["summary"],
        "character_updates": [
            {
                "card_id": cu["card_id"],
                "current_state": cu.get("current_state", ""),
                "accepted_permanent_facts": [dict(f) for f in cu.get("new_permanent_facts", [])],
            }
            for cu in result.get("character_updates", [])
        ],
        "accepted_thread_updates": [
            {"thread_id": tu["thread_id"], "status": tu["status"]}
            for tu in result.get("thread_updates", [])
        ],
    }


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
