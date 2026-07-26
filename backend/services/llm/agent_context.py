"""Bounded evidence packets for creative and continuity Agents."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Literal

from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.repositories.character_repository import character_repo
from backend.db.repositories.character_state_repository import character_state_repo
from backend.db.repositories.faction_repository import faction_repo
from backend.db.repositories.novel_repository import novel_repo
from backend.db.repositories.plot_thread_repository import plot_thread_repo
from backend.db.repositories.volume_repository import volume_repo
from backend.db.repositories.worldbook_repository import worldbook_repo


AgentScope = Literal["novel", "volume", "chapter"]
MAX_CONTEXT_CHARACTERS = 36_000


@dataclass(frozen=True)
class AgentContextBundle:
    text: str
    coverage: str
    truncated_sections: tuple[str, ...]
    target_label: str


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _clip(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    suffix = "\n…（内容已截断）"
    if limit <= len(suffix):
        return suffix[: max(0, limit)], True
    return value[: limit - len(suffix)].rstrip() + suffix, True


def _outline_view(outline: Any) -> dict[str, Any] | None:
    if not isinstance(outline, dict):
        return None
    return {
        key: outline.get(key)
        for key in (
            "core_conflict",
            "ending_hook",
            "scenes",
            "threads_resolved",
            "new_threads",
        )
        if outline.get(key)
    } or None


async def build_agent_context(
    *,
    novel_id: str,
    scope: AgentScope,
    volume_id: str | None = None,
    chapter_id: str | None = None,
    max_characters: int = MAX_CONTEXT_CHARACTERS,
) -> AgentContextBundle:
    """Build a deterministic, bounded evidence packet for one analysis scope."""
    if scope not in {"novel", "volume", "chapter"}:
        raise ValueError(f"不支持的 Agent 作用范围: {scope}")
    if scope == "novel" and (volume_id or chapter_id):
        raise ValueError("全书级 Agent 请求不能同时指定 volume_id 或 chapter_id")
    if scope == "volume" and not volume_id:
        raise ValueError("卷级 Agent 请求必须指定 volume_id")
    if scope == "volume" and chapter_id:
        raise ValueError("卷级 Agent 请求不能同时指定 chapter_id")
    if scope == "chapter" and not chapter_id:
        raise ValueError("章节级 Agent 请求必须指定 chapter_id")
    if scope == "chapter" and volume_id:
        raise ValueError("章节级 Agent 请求不能同时指定 volume_id")

    novel = await novel_repo.get_novel_by_id(novel_id)
    volumes = await volume_repo.get_volumes_by_novel(novel_id)
    all_chapters = await chapter_repo.get_chapters_by_novel(
        novel_id,
        include_content=True,
    )
    volume_by_id = {str(item["_id"]): item for item in volumes}

    selected_volume: dict[str, Any] | None = None
    selected_chapter: dict[str, Any] | None = None
    if volume_id:
        selected_volume = volume_by_id.get(volume_id)
        if selected_volume is None:
            raise ValueError("指定卷不属于当前小说")
    if chapter_id:
        selected_chapter = next(
            (item for item in all_chapters if str(item["_id"]) == chapter_id),
            None,
        )
        if selected_chapter is None:
            raise ValueError("指定章节不属于当前小说")
        selected_volume = volume_by_id.get(str(selected_chapter["volume_id"]))
        if selected_volume is None:
            raise ValueError("目标章节所属卷不存在或已删除")

    if scope == "volume":
        selected_chapters = [
            item
            for item in all_chapters
            if str(item["volume_id"]) == str(selected_volume["_id"])
        ]
        target_label = f"卷：{selected_volume.get('title') or volume_id}"
    elif scope == "chapter":
        selected_chapters = [selected_chapter]
        target_label = (
            f"章节：{selected_chapter.get('title') or chapter_id}"
        )
    else:
        selected_chapters = all_chapters
        target_label = f"全书：{novel.get('title') or novel_id}"

    cards = []
    for card_type, repository in (
        ("character", character_repo),
        ("location", worldbook_repo),
        ("item", worldbook_repo),
        ("rule", worldbook_repo),
    ):
        for card in await repository.list_cards(novel_id, card_type):
            cards.append({
                "id": str(card["_id"]),
                "type": card_type,
                "name": card.get("name", ""),
                "description": card.get("description", ""),
                "details": card.get("details", {}),
                "importance": card.get("importance"),
            })
    states = [
        {
            "card_id": str(item.get("card_id") or ""),
            "current_state": item.get("current_state", ""),
            "as_of_chapter_id": str(item.get("as_of_chapter_id") or ""),
            "as_of_chapter_order": item.get("as_of_chapter_order"),
            "permanent_facts": item.get("permanent_facts", []),
        }
        for item in await character_state_repo.list_states(novel_id)
    ]
    factions = [
        {
            "name": item.get("name", ""),
            "positioning": item.get("positioning", ""),
            "core_goal": item.get("core_goal", ""),
            "hidden_goal": item.get("hidden_goal", ""),
            "conflict_with_mainline": item.get("conflict_with_mainline", ""),
            "active_status": item.get("active_status", ""),
        }
        for item in await faction_repo.get_factions_by_novel(novel_id)
    ]
    threads = [
        {
            "name": item.get("name", ""),
            "description": item.get("description", ""),
            "status": item.get("status", ""),
            "planted_chapter_id": str(item.get("planted_chapter_id") or ""),
            "resolved_chapter_id": str(item.get("resolved_chapter_id") or ""),
            "planted_chapter_order": item.get("planted_chapter_order"),
            "resolved_chapter_order": item.get("resolved_chapter_order"),
            "due_target": item.get("due_target"),
        }
        for item in await plot_thread_repo.list_threads(novel_id)
    ]

    core = {
        "title": novel.get("title", ""),
        "genre": novel.get("genre", ""),
        "summary": novel.get("summary", ""),
        "core_seed": novel.get("core_seed", ""),
        "worldview": novel.get("worldview", ""),
        "writing_style": novel.get("writing_style", ""),
        "narrative_pov": novel.get("narrative_pov", ""),
        "tone": novel.get("tone", ""),
        "era_background": novel.get("era_background", ""),
        "plot": novel.get("plot", ""),
    }
    volume_structure = [
        {
            "id": str(item["_id"]),
            "order": item.get("order_index"),
            "title": item.get("title", ""),
            "summary": item.get("summary", ""),
            "arc": item.get("arc", ""),
            "chapter_range": item.get("chapter_range"),
        }
        for item in (
            [selected_volume]
            if scope in {"volume", "chapter"} and selected_volume
            else volumes
        )
    ]

    chapter_views = []
    for item in selected_chapters:
        content = str(item.get("content") or "")
        if scope == "chapter":
            content_excerpt, _ = _clip(content, 14_000)
        elif scope == "volume":
            content_excerpt, _ = _clip(content, 1_400)
        else:
            content_excerpt = ""
        chapter_views.append({
            "id": str(item["_id"]),
            "volume_id": str(item["volume_id"]),
            "order": item.get("order_index"),
            "title": item.get("title", ""),
            "summary": item.get("summary", ""),
            "outline": _outline_view(item.get("outline")),
            "content_excerpt": content_excerpt or None,
        })

    sections = [
        ("小说核心设定", _json(core)),
        ("目标范围", _json({
            "scope": scope,
            "target": target_label,
            "volumes": volume_structure,
            "chapters": chapter_views,
        })),
        ("人物状态与永久事实", _json(states)),
        ("伏笔", _json(threads)),
        ("人物与世界资料卡", _json(cards)),
        ("势力", _json(factions)),
    ]

    # Keep the total packet within a hard budget while reserving evidence for
    # every category. State/fact and plot-thread evidence deliberately receive
    # budget before descriptive cards because they are more valuable to a
    # continuity review.
    header_cost = sum(len(f"【{name}】\n") for name, _ in sections)
    separator_cost = 2 * max(0, len(sections) - 1)
    content_budget = max(0, max_characters - header_cost - separator_cost)
    weights = (14, 46, 15, 10, 10, 5)
    allocations = [
        min(len(content), content_budget * weight // 100)
        for (_, content), weight in zip(sections, weights, strict=True)
    ]
    remaining_budget = content_budget - sum(allocations)
    for index in (1, 2, 3, 0, 4, 5):
        if remaining_budget <= 0:
            break
        content = sections[index][1]
        extra = min(len(content) - allocations[index], remaining_budget)
        allocations[index] += extra
        remaining_budget -= extra

    rendered: list[str] = []
    truncated: list[str] = []
    used = 0
    for (name, content), allocation in zip(sections, allocations, strict=True):
        header = f"【{name}】\n"
        separator = 2 if rendered else 0
        remaining = max_characters - used - separator - len(header)
        if remaining <= 0:
            truncated.append(name)
            continue
        clipped, was_clipped = _clip(content, min(allocation, remaining))
        if not clipped:
            truncated.append(name)
            continue
        rendered.append(header + clipped)
        used += separator + len(header) + len(clipped)
        if was_clipped:
            truncated.append(name)

    content_modes = {
        "novel": "全书卷纲、章摘要与细纲；不装入整本正文",
        "volume": "目标卷全部章摘要、细纲与每章正文片段",
        "chapter": "目标章完整正文（超长时截断）及相邻结构化资料",
    }
    coverage = (
        f"{content_modes[scope]}；资料卡 {len(cards)} 张，"
        f"人物状态 {len(states)} 份，势力 {len(factions)} 个，伏笔 {len(threads)} 条。"
    )
    if truncated:
        coverage += f" 截断段落：{', '.join(truncated)}。"
    return AgentContextBundle(
        text="\n\n".join(rendered),
        coverage=coverage,
        truncated_sections=tuple(truncated),
        target_label=target_label,
    )
