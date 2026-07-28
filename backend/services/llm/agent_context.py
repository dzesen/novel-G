"""Bounded evidence packets for creative, continuity, and style Agents."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
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
from backend.services.novel.chapter_timeline import ChapterPosition, ChapterTimeline


AgentScope = Literal["novel", "volume", "chapter"]
StyleAgentScope = Literal["volume", "chapter"]
StyleEvidenceKind = Literal["chapter_paragraph", "character_profile"]
StyleEvidenceRole = Literal["target", "baseline"]
MAX_CONTEXT_CHARACTERS = 36_000
STYLE_EVIDENCE_EXCERPT_CHARACTERS = 700
STYLE_BASELINE_CHAPTER_LIMIT = 4
STYLE_BASELINE_PARAGRAPHS_PER_CHAPTER = 3
STYLE_TARGET_CHAPTER_PARAGRAPH_LIMIT = 18
STYLE_TARGET_VOLUME_CHAPTER_LIMIT = 12
STYLE_TARGET_VOLUME_PARAGRAPHS_PER_CHAPTER = 2
STYLE_CONTEXT_MIN_CHARACTERS = 4_000


@dataclass(frozen=True)
class AgentStyleEvidence:
    """One exact style-evidence item authorized for model references."""

    evidence_id: str
    role: StyleEvidenceRole
    kind: StyleEvidenceKind
    label: str
    excerpt: str
    chapter_id: str | None = None
    paragraph_index: int | None = None
    card_id: str | None = None
    profile_field: str | None = None
    example_index: int | None = None

    def prompt_view(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "evidence_id": self.evidence_id,
                "role": self.role,
                "kind": self.kind,
                "label": self.label,
                "excerpt": self.excerpt,
                "chapter_id": self.chapter_id,
                "paragraph_index": self.paragraph_index,
                "card_id": self.card_id,
                "profile_field": self.profile_field,
                "example_index": self.example_index,
            }.items()
            if value is not None
        }

    def snapshot_view(self) -> dict[str, Any]:
        """Persist stable coordinates without duplicating prose in run metadata."""

        return {
            key: value
            for key, value in self.prompt_view().items()
            if key != "excerpt"
        }


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
    style_evidence: tuple[AgentStyleEvidence, ...] = ()

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
            "style_evidence": [
                item.snapshot_view() for item in self.style_evidence
            ],
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


def is_valid_style_evidence_reference(
    context: AgentContextBundle,
    reference: dict[str, Any],
) -> bool:
    """Validate a style finding against the exact bounded evidence packet."""

    evidence_id = str(reference.get("evidence_id") or "")
    item = next(
        (
            candidate
            for candidate in context.style_evidence
            if candidate.evidence_id == evidence_id
        ),
        None,
    )
    if item is None:
        return False
    if str(reference.get("role") or "") != item.role:
        return False
    if str(reference.get("kind") or "") != item.kind:
        return False
    if item.kind == "chapter_paragraph":
        return (
            str(reference.get("chapter_id") or "") == item.chapter_id
            and reference.get("paragraph_index") == item.paragraph_index
        )
    return (
        str(reference.get("card_id") or "") == item.card_id
        and str(reference.get("profile_field") or "") == item.profile_field
        and reference.get("example_index") == item.example_index
    )


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


def _paragraphs_with_indexes(content: Any) -> list[tuple[int, str]]:
    """Split persisted prose into stable, zero-based paragraph coordinates."""

    paragraphs = [
        paragraph.strip()
        for paragraph in re.split(
            r"(?:\r?\n[ \t]*)+",
            str(content or ""),
        )
        if paragraph.strip()
    ]
    return list(enumerate(paragraphs))


def _sample_evenly(values: list[Any], limit: int) -> list[Any]:
    """Select deterministic head-to-tail coverage without random sampling."""

    if limit <= 0 or not values:
        return []
    if len(values) <= limit:
        return list(values)
    if limit == 1:
        return [values[0]]
    indexes = [
        round(index * (len(values) - 1) / (limit - 1))
        for index in range(limit)
    ]
    return [values[index] for index in dict.fromkeys(indexes)]


def _chapter_paragraph_evidence(
    *,
    positions: list[ChapterPosition],
    chapters_by_id: dict[str, dict[str, Any]],
    role: StyleEvidenceRole,
    paragraphs_per_chapter: int,
) -> list[AgentStyleEvidence]:
    evidence: list[AgentStyleEvidence] = []
    for position in positions:
        chapter = chapters_by_id[position.chapter_id]
        paragraphs = _sample_evenly(
            _paragraphs_with_indexes(chapter.get("content")),
            paragraphs_per_chapter,
        )
        for paragraph_index, paragraph in paragraphs:
            excerpt, _ = _clip(
                paragraph,
                STYLE_EVIDENCE_EXCERPT_CHARACTERS,
            )
            chapter_title = str(chapter.get("title") or "")
            evidence.append(
                AgentStyleEvidence(
                    evidence_id=(
                        f"{role}:chapter:{position.chapter_id}:"
                        f"paragraph:{paragraph_index}"
                    ),
                    role=role,
                    kind="chapter_paragraph",
                    label=(
                        f"第{position.volume_order}卷·"
                        f"第{position.chapter_order}章《{chapter_title}》"
                        f"第{paragraph_index + 1}段"
                    ),
                    excerpt=excerpt,
                    chapter_id=position.chapter_id,
                    paragraph_index=paragraph_index,
                )
            )
    return evidence


def _character_profile_evidence(
    *,
    cards: list[dict[str, Any]],
    declared_card_ids: set[str],
) -> list[AgentStyleEvidence]:
    """Project only declared formal character IDs and only voice fields."""

    evidence: list[AgentStyleEvidence] = []
    selected_cards = sorted(
        (
            card
            for card in cards
            if str(card.get("_id") or "") in declared_card_ids
        ),
        key=lambda card: (
            str(card.get("importance") or "") != "main",
            str(card.get("name") or ""),
            str(card.get("_id") or ""),
        ),
    )
    for card in selected_cards:
        card_id = str(card.get("_id") or "")
        name = str(card.get("name") or card_id)
        profile = (
            card.get("character_profile")
            if isinstance(card.get("character_profile"), dict)
            else {}
        )
        portrayal_notes = str(profile.get("portrayal_notes") or "").strip()
        if portrayal_notes:
            excerpt, _ = _clip(
                portrayal_notes,
                STYLE_EVIDENCE_EXCERPT_CHARACTERS,
            )
            evidence.append(
                AgentStyleEvidence(
                    evidence_id=(
                        f"baseline:card:{card_id}:portrayal_notes"
                    ),
                    role="baseline",
                    kind="character_profile",
                    label=f"{name} · 人物塑造备注",
                    excerpt=excerpt,
                    card_id=card_id,
                    profile_field="portrayal_notes",
                )
            )
        for example_index, example in enumerate(
            profile.get("dialogue_examples") or []
        ):
            normalized = str(example or "").strip()
            if not normalized:
                continue
            excerpt, _ = _clip(
                normalized,
                STYLE_EVIDENCE_EXCERPT_CHARACTERS,
            )
            evidence.append(
                AgentStyleEvidence(
                    evidence_id=(
                        f"baseline:card:{card_id}:dialogue_examples:"
                        f"{example_index}"
                    ),
                    role="baseline",
                    kind="character_profile",
                    label=f"{name} · 对白样例 {example_index + 1}",
                    excerpt=excerpt,
                    card_id=card_id,
                    profile_field="dialogue_examples",
                    example_index=example_index,
                )
            )
    return evidence


def _fit_style_evidence(
    evidence: list[AgentStyleEvidence],
    budget: int,
) -> tuple[list[AgentStyleEvidence], str]:
    """Keep complete JSON evidence records within one section allocation."""

    selected: list[AgentStyleEvidence] = []
    rendered = "[]"
    for item in evidence:
        candidate = [*selected, item]
        candidate_rendered = _json(
            [record.prompt_view() for record in candidate]
        )
        if len(candidate_rendered) > budget:
            continue
        selected = candidate
        rendered = candidate_rendered
    return selected, rendered


async def build_style_consistency_context(
    *,
    novel_id: str,
    scope: StyleAgentScope,
    volume_id: str | None = None,
    chapter_id: str | None = None,
    max_characters: int = MAX_CONTEXT_CHARACTERS,
) -> AgentContextBundle:
    """Build style baselines and target prose as one hard-bounded packet.

    Character voice evidence is deny-by-default: only formal character card
    IDs explicitly declared by target chapter outlines are projected.
    """

    if max_characters < STYLE_CONTEXT_MIN_CHARACTERS:
        raise ValueError(
            f"文风一致性上下文预算不能低于 {STYLE_CONTEXT_MIN_CHARACTERS} 字符"
        )
    if scope not in {"volume", "chapter"}:
        raise ValueError("文风与人物声音一致性仅支持卷或章节范围")
    if scope == "volume" and (not volume_id or chapter_id):
        raise ValueError("卷级文风检查必须且只能指定 volume_id")
    if scope == "chapter" and (not chapter_id or volume_id):
        raise ValueError("章节级文风检查必须且只能指定 chapter_id")

    captured_revision = await narrative_revision_store.current(novel_id)
    novel = await novel_repo.get_novel_by_id(novel_id)
    volumes = await volume_repo.get_volumes_by_novel(novel_id)
    chapters = await chapter_repo.get_chapters_by_novel(
        novel_id,
        include_content=True,
    )
    timeline = ChapterTimeline(volumes, chapters)
    volumes_by_id = {
        str(item["_id"]): item
        for item in volumes
        if not item.get("is_deleted")
    }
    chapters_by_id = {
        str(item["_id"]): item
        for item in chapters
        if not item.get("is_deleted")
    }

    if scope == "volume":
        normalized_volume_id = str(volume_id)
        selected_volume = volumes_by_id.get(normalized_volume_id)
        if selected_volume is None:
            raise ValueError("指定卷不属于当前小说")
        target_positions = [
            position
            for position in timeline.positions
            if position.volume_id == normalized_volume_id
        ]
        target_label = (
            f"卷：{selected_volume.get('title') or normalized_volume_id}"
        )
        normalized_chapter_id = None
    else:
        normalized_chapter_id = str(chapter_id)
        try:
            target_position = timeline.position(normalized_chapter_id)
        except ValueError as exc:
            raise ValueError("指定章节不属于当前小说") from exc
        target_positions = [target_position]
        target_chapter = chapters_by_id[target_position.chapter_id]
        target_label = (
            f"章节：{target_chapter.get('title') or normalized_chapter_id}"
        )
        normalized_volume_id = None

    written_target_positions = [
        position
        for position in target_positions
        if str(
            chapters_by_id[position.chapter_id].get("content") or ""
        ).strip()
    ]
    if not written_target_positions:
        raise ValueError("目标范围没有可检查的正文")

    first_target_ordinal = min(
        position.book_ordinal for position in target_positions
    )
    early_positions = [
        position
        for position in timeline.positions
        if position.book_ordinal < first_target_ordinal
        and str(
            chapters_by_id[position.chapter_id].get("content") or ""
        ).strip()
    ][:STYLE_BASELINE_CHAPTER_LIMIT]
    baseline_candidates = _chapter_paragraph_evidence(
        positions=early_positions,
        chapters_by_id=chapters_by_id,
        role="baseline",
        paragraphs_per_chapter=STYLE_BASELINE_PARAGRAPHS_PER_CHAPTER,
    )

    if scope == "chapter":
        sampled_target_positions = written_target_positions
        target_paragraph_limit = STYLE_TARGET_CHAPTER_PARAGRAPH_LIMIT
    else:
        sampled_target_positions = _sample_evenly(
            written_target_positions,
            STYLE_TARGET_VOLUME_CHAPTER_LIMIT,
        )
        target_paragraph_limit = STYLE_TARGET_VOLUME_PARAGRAPHS_PER_CHAPTER
    target_candidates = _chapter_paragraph_evidence(
        positions=sampled_target_positions,
        chapters_by_id=chapters_by_id,
        role="target",
        paragraphs_per_chapter=target_paragraph_limit,
    )

    declared_card_ids: set[str] = set()
    for position in target_positions:
        outline = chapters_by_id[position.chapter_id].get("outline")
        if not isinstance(outline, dict):
            continue
        declared_card_ids.update(
            str(card_id)
            for card_id in (
                outline.get("present_character_card_ids") or []
            )
            if card_id
        )
    profile_candidates = _character_profile_evidence(
        cards=await character_repo.list_cards(novel_id, "character"),
        declared_card_ids=declared_card_ids,
    )

    metadata = _json(
        {
            "scope": scope,
            "target": target_label,
            "novel_title": novel.get("title", ""),
            "sampling_policy": {
                "overall_style_baseline": (
                    "小说开头、且位于目标范围之前的最多 4 个有正文章节；"
                    "每章均匀抽取最多 3 段"
                ),
                "target_chapter": "目标章均匀抽取最多 18 段",
                "target_volume": (
                    "目标卷均匀抽取最多 12 个有正文章节；"
                    "每章均匀抽取最多 2 段"
                ),
                "character_voice": (
                    "仅目标细纲 present_character_card_ids 声明的正式角色卡；"
                    "只投影 dialogue_examples 与 portrayal_notes"
                ),
            },
        }
    )
    section_specs = [
        ("整体文风基准（早期章节抽样）", baseline_candidates, 25),
        ("人物声音基准（细纲 ID 白名单）", profile_candidates, 25),
        ("待检查正文（有界段落抽样）", target_candidates, 50),
    ]
    metadata_header = "【检查范围与抽样策略】\n"
    evidence_headers = [
        f"【{name}】\n" for name, _, _ in section_specs
    ]
    separator_cost = 2 * len(section_specs)
    fixed_cost = (
        len(metadata_header)
        + len(metadata)
        + sum(len(header) for header in evidence_headers)
        + separator_cost
    )
    evidence_budget = max_characters - fixed_cost
    if evidence_budget <= 0:
        raise ValueError("文风一致性上下文预算不足以容纳抽样策略")

    active_weight = sum(
        weight for _, candidates, weight in section_specs if candidates
    )
    selected_sections: list[
        tuple[str, list[AgentStyleEvidence], str]
    ] = []
    truncated: list[str] = []
    for name, candidates, weight in section_specs:
        allocation = (
            evidence_budget * weight // active_weight
            if candidates and active_weight
            else 2
        )
        selected, rendered = _fit_style_evidence(
            candidates,
            max(2, allocation),
        )
        selected_sections.append((name, selected, rendered))
        if len(selected) < len(candidates):
            truncated.append(name)

    selected_target = selected_sections[2][1]
    if not selected_target:
        raise ValueError("文风一致性上下文预算不足以容纳目标正文证据")

    rendered_sections = [metadata_header + metadata]
    for name, _, rendered in selected_sections:
        rendered_sections.append(f"【{name}】\n{rendered}")
    text = "\n\n".join(rendered_sections)
    if len(text) > max_characters:
        raise ValueError("文风一致性上下文超过硬预算")

    selected_evidence = tuple(
        item
        for _, selected, _ in selected_sections
        for item in selected
    )
    baseline_selected = selected_sections[0][1]
    profile_selected = selected_sections[1][1]
    profile_card_count = len(
        {
            item.card_id
            for item in profile_selected
            if item.card_id is not None
        }
    )
    coverage = (
        f"目标正文抽样 {len(selected_target)} 段，覆盖 "
        f"{len({item.chapter_id for item in selected_target})}/"
        f"{len(written_target_positions)} 个有正文章节；"
        f"整体文风早期基准 {len(baseline_selected)} 段，来自 "
        f"{len({item.chapter_id for item in baseline_selected})} 章；"
        f"人物声音基准 {len(profile_selected)} 条，来自 "
        f"{profile_card_count} 张细纲已声明角色卡。"
    )
    if not baseline_selected:
        coverage += " 目标范围之前没有可用的早期正文基准。"
    if not profile_selected:
        coverage += " 目标细纲没有可用的对白样例或人物塑造备注。"
    if truncated:
        coverage += f" 截断段落：{', '.join(truncated)}。"

    if await narrative_revision_store.current(novel_id) != captured_revision:
        raise StaleAgentContext(
            "小说内容在 Agent 上下文装配期间发生变化，请重试"
        )
    context_digest = hashlib.sha256(
        json.dumps(
            {
                "novel_id": novel_id,
                "scope": scope,
                "volume_id": normalized_volume_id,
                "chapter_id": normalized_chapter_id,
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
        volume_id=normalized_volume_id,
        chapter_id=normalized_chapter_id,
        narrative_revision=captured_revision,
        context_digest=context_digest,
        style_evidence=selected_evidence,
    )
