"""Bounded evidence packets for creative and continuity Agents."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Literal

from backend.db.narrative_revision import narrative_revision_store
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
    novel_id: str = ""
    scope: AgentScope = "novel"
    volume_id: str | None = None
    chapter_id: str | None = None
    narrative_revision: int = 0
    context_digest: str = ""
    chapter_scene_counts: tuple[tuple[str, int], ...] = ()
    fact_ids: tuple[str, ...] = ()
    thread_ids: tuple[str, ...] = ()

    def snapshot(self) -> dict[str, Any]:
        return {
            "novel_id": self.novel_id,
            "scope": self.scope,
            "volume_id": self.volume_id,
            "chapter_id": self.chapter_id,
            "narrative_revision": self.narrative_revision,
            "context_digest": self.context_digest,
            "chapter_scene_counts": [
                {
                    "chapter_id": chapter_id,
                    "scene_count": scene_count,
                }
                for chapter_id, scene_count in self.chapter_scene_counts
            ],
            "fact_ids": list(self.fact_ids),
            "thread_ids": list(self.thread_ids),
        }


class StaleAgentContext(ValueError):
    """The narrative changed while an Agent context was being used."""


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
    result = {
        key: outline.get(key)
        for key in (
            "core_conflict",
            "ending_hook",
            "threads_resolved",
            "new_threads",
        )
        if outline.get(key)
    }
    scenes = outline.get("scenes")
    if isinstance(scenes, list) and scenes:
        result["scenes"] = [
            {
                "scene_index": index,
                **(scene if isinstance(scene, dict) else {"summary": str(scene)}),
            }
            for index, scene in enumerate(scenes)
        ]
    return result or None


async def ensure_agent_context_current(context: AgentContextBundle) -> None:
    if not context.novel_id:
        return
    current = await narrative_revision_store.current(context.novel_id)
    if current != context.narrative_revision:
        raise StaleAgentContext(
            "小说内容在 Agent 运行期间发生变化，请基于最新内容重新执行"
        )


def is_valid_evidence_reference(
    context: AgentContextBundle,
    reference: dict[str, Any],
) -> bool:
    """Validate model-produced IDs against the exact captured evidence packet."""
    kind = str(reference.get("kind") or "")
    chapter_id = str(reference.get("chapter_id") or "")
    scene_counts = dict(context.chapter_scene_counts)
    if kind == "chapter":
        return bool(chapter_id and chapter_id in scene_counts)
    if kind == "scene":
        scene_index = reference.get("scene_index")
        return (
            bool(chapter_id and chapter_id in scene_counts)
            and isinstance(scene_index, int)
            and not isinstance(scene_index, bool)
            and 0 <= scene_index < scene_counts[chapter_id]
        )
    if kind == "fact":
        return str(reference.get("fact_id") or "") in set(context.fact_ids)
    if kind == "thread":
        return str(reference.get("thread_id") or "") in set(context.thread_ids)
    return False


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

    captured_revision = await narrative_revision_store.current(novel_id)
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
        ("lore", worldbook_repo),
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
    await character_state_repo.ensure_fact_ids(novel_id)
    raw_states = await character_state_repo.list_states(novel_id)
    states = []
    fact_ids: list[str] = []
    for item in raw_states:
        facts = []
        for fact in item.get("permanent_facts") or []:
            if not isinstance(fact, dict):
                facts.append(
                    {
                        "fact_id": "",
                        "fact": str(fact),
                        "kind": "",
                        "chapter_order": None,
                        "source_chapter_id": "",
                    }
                )
                continue
            fact_id = str(fact.get("id") or "")
            if fact_id:
                fact_ids.append(fact_id)
            facts.append(
                {
                    "fact_id": fact_id,
                    "fact": fact.get("fact", ""),
                    "kind": fact.get("kind", ""),
                    "chapter_order": fact.get("chapter_order"),
                    "source_chapter_id": str(
                        fact.get("source_chapter_id") or ""
                    ),
                }
            )
        states.append(
            {
                "card_id": str(item.get("card_id") or ""),
                "current_state": item.get("current_state", ""),
                "as_of_chapter_id": str(item.get("as_of_chapter_id") or ""),
                "as_of_chapter_order": item.get("as_of_chapter_order"),
                "permanent_facts": facts,
            }
        )
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
    raw_threads = await plot_thread_repo.list_threads(novel_id)
    threads = [
        {
            "thread_id": str(item.get("_id") or ""),
            "name": item.get("name", ""),
            "description": item.get("description", ""),
            "status": item.get("status", ""),
            "planted_chapter_id": str(item.get("planted_chapter_id") or ""),
            "resolved_chapter_id": str(item.get("resolved_chapter_id") or ""),
            "planted_chapter_order": item.get("planted_chapter_order"),
            "resolved_chapter_order": item.get("resolved_chapter_order"),
            "due_target": item.get("due_target"),
        }
        for item in raw_threads
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
    text = "\n\n".join(rendered)
    if await narrative_revision_store.current(novel_id) != captured_revision:
        raise StaleAgentContext(
            "小说内容在 Agent 上下文装配期间发生变化，请重试"
        )
    # Only authorize references whose stable ID was actually rendered into
    # the bounded packet. A large novel may truncate later sections; IDs from
    # those omitted sections must not be accepted merely because they existed
    # in the database while the packet was assembled.
    chapter_scene_counts = tuple(
        (
            str(item["_id"]),
            len((item.get("outline") or {}).get("scenes") or []),
        )
        for item in selected_chapters
        if str(item["_id"]) in text
    )
    context_digest = hashlib.sha256(
        json.dumps(
            {
                "novel_id": novel_id,
                "scope": scope,
                "volume_id": volume_id,
                "chapter_id": chapter_id,
                "narrative_revision": captured_revision,
                "text": text,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return AgentContextBundle(
        text=text,
        coverage=coverage,
        truncated_sections=tuple(truncated),
        target_label=target_label,
        novel_id=novel_id,
        scope=scope,
        volume_id=volume_id,
        chapter_id=chapter_id,
        narrative_revision=captured_revision,
        context_digest=context_digest,
        chapter_scene_counts=chapter_scene_counts,
        fact_ids=tuple(
            sorted(fact_id for fact_id in set(fact_ids) if fact_id in text)
        ),
        thread_ids=tuple(
            sorted(
                str(item.get("_id") or "")
                for item in raw_threads
                if item.get("_id") and str(item["_id"]) in text
            )
        ),
    )
