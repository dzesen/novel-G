"""一章的批量管线：按 skip-existing 跑 细纲→正文→状态回填，自动接受每步。

生成与接受均以 ChapterPipelineDeps 注入，使本编排可脱离 LLM 与 MongoDB 用假件测。
真实依赖的装配在 headless_generation.py（Task 5）+ 服务层 accept（Task 6 组装）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Tuple

from backend.services.generation.job_planner import result_to_accept_state


@dataclass
class ChapterOutcome:
    chapter_id: str
    order_index: int
    steps_done: List[str] = field(default_factory=list)
    steps_skipped: List[str] = field(default_factory=list)
    tokens: int = 0
    consistency_issues: List[dict] = field(default_factory=list)
    facts_added: int = 0
    threads_advanced: int = 0
    summary_written: bool = False
    dropped_ids: Dict[str, Any] = field(default_factory=dict)
    truncations: List[dict] = field(default_factory=list)


@dataclass(frozen=True)
class ChapterPipelineDeps:
    generate_outline: Callable[[str, dict], Awaitable[Tuple[dict, dict, int, dict]]]
    generate_prose: Callable[[str, dict], Awaitable[Tuple[str, int, dict]]]
    generate_state: Callable[[str, dict], Awaitable[Tuple[dict, dict, int, dict]]]
    accept_outline: Callable[[str, dict], Awaitable[None]]
    write_prose: Callable[[str, str], Awaitable[None]]
    accept_state: Callable[[str, dict], Awaitable[dict]]


def _has(chapter: Dict[str, Any], key: str) -> bool:
    if key == "outline":
        return bool(chapter.get("outline"))
    return bool(str(chapter.get(key) or "").strip())


def _record_truncation(outcome: ChapterOutcome, step: str, truncation: Dict[str, Any]) -> None:
    """截断信号非空才记（设计 §5.1 的"本章在信息不全下生成"信号）；空的不进，常见路径无噪音。"""
    truncated_sections = truncation.get("truncated_sections") or []
    dropped_item_counts = truncation.get("dropped_item_counts") or {}
    if truncated_sections or dropped_item_counts:
        outcome.truncations.append({
            "step": step,
            "truncated_sections": truncated_sections,
            "dropped_item_counts": dropped_item_counts,
        })


async def run_chapter(novel_id: str, chapter: Dict[str, Any], deps: ChapterPipelineDeps) -> ChapterOutcome:
    """跑一章剩余的管线子步；skip-existing 决策用入口快照（生成函数内部读库看得到本轮先前写入）。"""
    chapter_id = str(chapter["_id"])
    outcome = ChapterOutcome(chapter_id=chapter_id, order_index=int(chapter.get("order_index") or 0))

    # 1. 细纲
    if _has(chapter, "outline"):
        outcome.steps_skipped.append("outline")
    else:
        result, dropped, tokens, truncation = await deps.generate_outline(novel_id, chapter)
        await deps.accept_outline(chapter_id, result)
        outcome.steps_done.append("outline")
        outcome.tokens += tokens
        outcome.dropped_ids.update(dropped)
        _record_truncation(outcome, "outline", truncation)

    # 2. 正文（无 accept 端点：直接写 content）
    if _has(chapter, "content"):
        outcome.steps_skipped.append("prose")
    else:
        text, tokens, truncation = await deps.generate_prose(novel_id, chapter)
        await deps.write_prose(chapter_id, text)
        outcome.steps_done.append("prose")
        outcome.tokens += tokens
        _record_truncation(outcome, "prose", truncation)

    # 3. 状态回填
    if _has(chapter, "summary"):
        outcome.steps_skipped.append("state")
    else:
        result, dropped, tokens, truncation = await deps.generate_state(novel_id, chapter)
        outcome.consistency_issues = list(result.get("consistency_issues", []))
        outcome.dropped_ids.update(dropped)
        _record_truncation(outcome, "state", truncation)
        accept_payload = result_to_accept_state(result)
        report = await deps.accept_state(chapter_id, accept_payload)
        outcome.steps_done.append("state")
        outcome.tokens += tokens
        outcome.facts_added += int(report.get("facts_appended", 0))
        outcome.threads_advanced += int(report.get("threads_updated", 0))
        outcome.summary_written = True

    return outcome
