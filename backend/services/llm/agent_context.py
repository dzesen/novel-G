"""Bounded evidence packets for creative, continuity, and review Agents."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Literal

from backend.db.errors import InvalidIdError, NotFoundError
from backend.db.narrative_revision import narrative_revision_store
from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.repositories.character_repository import character_repo
from backend.db.repositories.character_state_repository import character_state_repo
from backend.db.repositories.faction_repository import faction_repo
from backend.db.repositories.novel_repository import novel_repo
from backend.db.repositories.plot_thread_repository import plot_thread_repo
from backend.db.repositories.volume_repository import volume_repo
from backend.db.repositories.worldbook_repository import worldbook_repo
from backend.db.utils import to_object_id
from backend.services.llm.context_builder import normalize_outline_references
from backend.services.novel.chapter_timeline import ChapterPosition, ChapterTimeline
from backend.services.novel.story_health import StoryHealthReport, story_health


AgentScope = Literal["novel", "volume", "chapter"]
IllustrationAgentScope = Literal["character", "novel", "chapter"]
AgentContextScope = Literal["character", "novel", "volume", "chapter"]
StyleAgentScope = Literal["volume", "chapter"]
StyleEvidenceKind = Literal["chapter_paragraph", "character_profile"]
StyleEvidenceRole = Literal["target", "baseline"]
VolumeRetrospectiveEvidenceKind = Literal[
    "volume_outline",
    "chapter_outline",
    "chapter_prose",
    "story_health_plot_thread",
    "story_health_character_absence",
    "story_health_volume_word_count",
    "story_health_chapter_word_count",
]
VolumeRetrospectiveEvidenceRole = Literal[
    "promise",
    "outcome",
    "deterministic",
]
MAX_CONTEXT_CHARACTERS = 36_000
STYLE_EVIDENCE_EXCERPT_CHARACTERS = 700
STYLE_BASELINE_CHAPTER_LIMIT = 4
STYLE_BASELINE_PARAGRAPHS_PER_CHAPTER = 3
STYLE_TARGET_CHAPTER_PARAGRAPH_LIMIT = 18
STYLE_TARGET_VOLUME_CHAPTER_LIMIT = 12
STYLE_TARGET_VOLUME_PARAGRAPHS_PER_CHAPTER = 2
STYLE_CONTEXT_MIN_CHARACTERS = 4_000
RETROSPECTIVE_CONTEXT_MIN_CHARACTERS = 8_000
RETROSPECTIVE_CHAPTER_SAMPLE_LIMIT = 18
RETROSPECTIVE_PROSE_PARAGRAPHS_PER_CHAPTER = 2
RETROSPECTIVE_EVIDENCE_EXCERPT_CHARACTERS = 700
ILLUSTRATION_CONTEXT_MIN_CHARACTERS = 4_000
ILLUSTRATION_CONTEXT_MAX_CHARACTERS = 12_000
ILLUSTRATION_NOVEL_FIELD_LIMITS: tuple[tuple[str, int], ...] = (
    # Sum to 1,800 raw characters. JSON control-character escaping can expand
    # each character to six serialized characters (for example ``\u0000``),
    # so the complete required novel record still fits the 12k hard cap.
    ("title", 60),
    ("subtitle", 100),
    ("genre", 80),
    ("summary", 400),
    ("core_seed", 250),
    ("worldview", 500),
    ("writing_style", 120),
    ("narrative_pov", 60),
    ("tone", 100),
    ("era_background", 130),
)
# Required character-scope cards use the same 1,800-character raw ceiling as
# the required novel/chapter records, so worst-case JSON escaping still fits.
ILLUSTRATION_CARD_DESCRIPTION_CHARACTERS = 800
ILLUSTRATION_CARD_DETAIL_CHARACTERS = 400
ILLUSTRATION_SCENE_SUMMARY_CHARACTERS = 600
ILLUSTRATION_SCENE_PURPOSE_CHARACTERS = 300
ILLUSTRATION_DECLARED_CARD_ID_LIMIT = 64


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
class AgentVolumeRetrospectiveEvidence:
    """One exact record authorized for volume-retrospective references."""

    evidence_id: str
    role: VolumeRetrospectiveEvidenceRole
    kind: VolumeRetrospectiveEvidenceKind
    label: str
    excerpt: str
    volume_id: str | None = None
    chapter_id: str | None = None
    paragraph_index: int | None = None
    thread_id: str | None = None
    card_id: str | None = None

    def prompt_view(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "evidence_id": self.evidence_id,
                "role": self.role,
                "kind": self.kind,
                "label": self.label,
                "excerpt": self.excerpt,
                "volume_id": self.volume_id,
                "chapter_id": self.chapter_id,
                "paragraph_index": self.paragraph_index,
                "thread_id": self.thread_id,
                "card_id": self.card_id,
            }.items()
            if value is not None
        }

    def snapshot_view(self) -> dict[str, Any]:
        """Persist stable coordinates without duplicating evidence text."""

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
    scope: AgentContextScope = "novel"
    volume_id: str | None = None
    chapter_id: str | None = None
    character_card_id: str | None = None
    narrative_revision: int = 0
    context_digest: str = ""
    story_health_schema_version: str | None = None
    chapter_scene_counts: tuple[tuple[str, int], ...] = ()
    fact_ids: tuple[str, ...] = ()
    thread_ids: tuple[str, ...] = ()
    style_evidence: tuple[AgentStyleEvidence, ...] = ()
    volume_retrospective_evidence: tuple[
        AgentVolumeRetrospectiveEvidence,
        ...,
    ] = ()

    def snapshot(self) -> dict[str, Any]:
        return {
            "novel_id": self.novel_id,
            "scope": self.scope,
            "volume_id": self.volume_id,
            "chapter_id": self.chapter_id,
            "character_card_id": self.character_card_id,
            "narrative_revision": self.narrative_revision,
            "context_digest": self.context_digest,
            "story_health_schema_version": self.story_health_schema_version,
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
            "volume_retrospective_evidence": [
                item.snapshot_view()
                for item in self.volume_retrospective_evidence
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
            "小说内容在生成期间发生变化，请基于最新内容重新执行"
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


def is_valid_volume_retrospective_evidence_reference(
    context: AgentContextBundle,
    reference: dict[str, Any],
) -> bool:
    """Validate a retrospective citation against its captured evidence."""

    evidence_id = str(reference.get("evidence_id") or "")
    item = next(
        (
            candidate
            for candidate in context.volume_retrospective_evidence
            if candidate.evidence_id == evidence_id
        ),
        None,
    )
    if item is None:
        return False
    return (
        str(reference.get("role") or "") == item.role
        and str(reference.get("kind") or "") == item.kind
        and (
            str(reference.get("volume_id") or "")
            == str(item.volume_id or "")
        )
        and (
            str(reference.get("chapter_id") or "")
            == str(item.chapter_id or "")
        )
        and reference.get("paragraph_index") == item.paragraph_index
        and (
            str(reference.get("thread_id") or "")
            == str(item.thread_id or "")
        )
        and str(reference.get("card_id") or "") == str(item.card_id or "")
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
        raise ValueError(f"不支持的生成工具作用范围: {scope}")
    if scope == "novel" and (volume_id or chapter_id):
        raise ValueError("全书级生成工具请求不能同时指定 volume_id 或 chapter_id")
    if scope == "volume" and not volume_id:
        raise ValueError("卷级生成工具请求必须指定 volume_id")
    if scope == "volume" and chapter_id:
        raise ValueError("卷级生成工具请求不能同时指定 chapter_id")
    if scope == "chapter" and not chapter_id:
        raise ValueError("章节级生成工具请求必须指定 chapter_id")
    if scope == "chapter" and volume_id:
        raise ValueError("章节级生成工具请求不能同时指定 volume_id")

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
            "小说内容在生成上下文装配期间发生变化，请重试"
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


def _illustration_text(
    value: Any,
    limit: int,
) -> tuple[str, bool]:
    return _clip(str(value or "").strip(), limit)


def _illustration_novel_record(
    novel: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    record: dict[str, Any] = {"kind": "novel"}
    truncated = False
    for field, limit in ILLUSTRATION_NOVEL_FIELD_LIMITS:
        value, was_truncated = _illustration_text(
            novel.get(field),
            limit,
        )
        if value:
            record[field] = value
        truncated = truncated or was_truncated
    return record, truncated


def _illustration_card_record(
    card: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    name, name_truncated = _illustration_text(card.get("name"), 200)
    description, description_truncated = _illustration_text(
        card.get("description"),
        ILLUSTRATION_CARD_DESCRIPTION_CHARACTERS,
    )
    details = (
        card.get("details")
        if isinstance(card.get("details"), dict)
        else {}
    )
    visual_details: dict[str, str] = {}
    details_truncated = False
    for field in ("appearance", "personality"):
        value, was_truncated = _illustration_text(
            details.get(field),
            ILLUSTRATION_CARD_DETAIL_CHARACTERS,
        )
        if value:
            visual_details[field] = value
        details_truncated = details_truncated or was_truncated
    record = {
        "kind": "reference_card",
        "card_id": str(card.get("_id") or ""),
        "card_type": str(card.get("card_type") or ""),
        "name": name,
        "description": description,
        "details": visual_details or None,
        "importance": str(card.get("importance") or ""),
    }
    return (
        {
            key: value
            for key, value in record.items()
            if value not in ("", None)
        },
        name_truncated or description_truncated or details_truncated,
    )


def _illustration_chapter_record(
    chapter: dict[str, Any],
    outline: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    title, title_truncated = _illustration_text(
        chapter.get("title"),
        200,
    )
    core_conflict, conflict_truncated = _illustration_text(
        outline.get("core_conflict"),
        800,
    )
    ending_hook, hook_truncated = _illustration_text(
        outline.get("ending_hook"),
        800,
    )
    record = {
        "kind": "chapter_outline",
        "chapter_id": str(chapter.get("_id") or ""),
        "title": title,
        "order_index": chapter.get("order_index"),
        "core_conflict": core_conflict,
        "ending_hook": ending_hook,
    }
    return (
        {
            key: value
            for key, value in record.items()
            if value not in ("", None)
        },
        title_truncated or conflict_truncated or hook_truncated,
    )


def _illustration_scene_record(
    scene: Any,
    index: int,
) -> tuple[dict[str, Any], bool]:
    source = scene if isinstance(scene, dict) else {"summary": str(scene)}
    summary, summary_truncated = _illustration_text(
        source.get("summary"),
        ILLUSTRATION_SCENE_SUMMARY_CHARACTERS,
    )
    purpose, purpose_truncated = _illustration_text(
        source.get("purpose"),
        ILLUSTRATION_SCENE_PURPOSE_CHARACTERS,
    )
    return (
        {
            "kind": "chapter_scene",
            "scene_index": index,
            "summary": summary,
            "purpose": purpose,
        },
        summary_truncated or purpose_truncated,
    )


def _canonical_declared_card_ids(
    values: Any,
    *,
    field_name: str,
) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, (list, tuple)):
        raise ValueError(f"{field_name} 必须是正式 card_id 列表")
    if len(values) > ILLUSTRATION_DECLARED_CARD_ID_LIMIT:
        raise ValueError(
            f"{field_name} 超过数量上限 "
            f"{ILLUSTRATION_DECLARED_CARD_ID_LIMIT}"
        )
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value is None:
            raise ValueError(f"{field_name} 包含非正式 card_id")
        try:
            canonical = str(to_object_id(value))
        except InvalidIdError as exc:
            raise ValueError(
                f"{field_name} 包含非正式 card_id"
            ) from exc
        if canonical in seen:
            continue
        seen.add(canonical)
        result.append(canonical)
    return result


def _render_illustration_records(
    *,
    required_records: list[dict[str, Any]],
    optional_records: list[tuple[str, dict[str, Any]]],
    max_characters: int,
) -> tuple[str, tuple[str, ...], dict[str, int]]:
    header = "【插图提示词有界证据】\n"
    selected = list(required_records)
    rendered = header + _json(selected)
    if len(rendered) > max_characters:
        raise ValueError("插图提示词的必需证据超过上下文预算")

    truncated: list[str] = []
    selected_counts: dict[str, int] = {}
    for section, record in optional_records:
        candidate = [*selected, record]
        candidate_rendered = header + _json(candidate)
        if len(candidate_rendered) > max_characters:
            if section not in truncated:
                truncated.append(section)
            continue
        selected = candidate
        rendered = candidate_rendered
        selected_counts[section] = selected_counts.get(section, 0) + 1
    return rendered, tuple(truncated), selected_counts


async def _declared_character_cards(
    *,
    novel_id: str,
    card_ids: list[str],
) -> tuple[list[dict[str, Any]], int]:
    cards: list[dict[str, Any]] = []
    unresolved = 0
    for card_id in card_ids:
        try:
            cards.append(
                await character_repo.get_card(
                    novel_id,
                    "character",
                    card_id,
                )
            )
        except (InvalidIdError, NotFoundError):
            unresolved += 1
    return cards, unresolved


async def _declared_worldbook_cards(
    *,
    novel_id: str,
    card_ids: list[str],
) -> tuple[list[dict[str, Any]], int]:
    cards: list[dict[str, Any]] = []
    unresolved = 0
    for card_id in card_ids:
        resolved: dict[str, Any] | None = None
        for card_type in ("location", "item", "rule", "lore"):
            try:
                resolved = await worldbook_repo.get_card(
                    novel_id,
                    card_type,
                    card_id,
                )
                break
            except (InvalidIdError, NotFoundError):
                continue
        if resolved is None:
            unresolved += 1
        else:
            cards.append(resolved)
    return cards, unresolved


async def build_illustration_prompt_context(
    *,
    novel_id: str,
    scope: IllustrationAgentScope,
    character_card_id: str | None = None,
    chapter_id: str | None = None,
    max_characters: int = ILLUSTRATION_CONTEXT_MAX_CHARACTERS,
) -> AgentContextBundle:
    """Build a hard-bounded visual packet without prose or inferred cards."""

    if not (
        ILLUSTRATION_CONTEXT_MIN_CHARACTERS
        <= max_characters
        <= ILLUSTRATION_CONTEXT_MAX_CHARACTERS
    ):
        raise ValueError(
            "插图提示词上下文预算必须在 "
            f"{ILLUSTRATION_CONTEXT_MIN_CHARACTERS} 到 "
            f"{ILLUSTRATION_CONTEXT_MAX_CHARACTERS} 字符之间"
        )
    if scope not in {"character", "novel", "chapter"}:
        raise ValueError("插图提示词仅支持角色、全书或章节范围")
    if scope == "character" and (not character_card_id or chapter_id):
        raise ValueError(
            "角色插图提示词必须且只能指定 character_card_id"
        )
    if scope == "novel" and (character_card_id or chapter_id):
        raise ValueError("全书插图提示词不能指定角色或章节")
    if scope == "chapter" and (not chapter_id or character_card_id):
        raise ValueError("章节插图提示词必须且只能指定 chapter_id")

    if novel_id is None:
        raise ValueError("novel_id 不是正式小说 ID")
    try:
        novel_id = str(to_object_id(novel_id))
    except InvalidIdError as exc:
        raise ValueError("novel_id 不是正式小说 ID") from exc

    captured_revision = await narrative_revision_store.current(novel_id)
    novel = await novel_repo.get_novel_by_id(novel_id)
    novel_record, novel_fields_truncated = _illustration_novel_record(
        novel
    )
    required_records: list[dict[str, Any]] = []
    optional_records: list[tuple[str, dict[str, Any]]] = []
    projection_truncated: list[str] = []
    normalized_character_id: str | None = None
    normalized_chapter_id: str | None = None
    unresolved_references = 0

    if novel_fields_truncated:
        projection_truncated.append("小说视觉设定字段")

    if scope == "character":
        try:
            normalized_character_id = str(
                to_object_id(character_card_id)
            )
        except InvalidIdError as exc:
            raise ValueError(
                "character_card_id 不是正式角色卡 ID"
            ) from exc
        card = await character_repo.get_card(
            novel_id,
            "character",
            normalized_character_id,
        )
        card_record, card_truncated = _illustration_card_record(card)
        required_records.append(card_record)
        optional_records.append(("小说视觉设定", novel_record))
        if card_truncated:
            projection_truncated.append("目标角色卡字段")
        target_label = f"角色：{card.get('name') or normalized_character_id}"
    elif scope == "novel":
        required_records.append(novel_record)
        target_label = f"全书：{novel.get('title') or novel_id}"
    else:
        try:
            normalized_chapter_id = str(to_object_id(chapter_id))
        except InvalidIdError as exc:
            raise ValueError("chapter_id 不是正式章节 ID") from exc
        chapter_not_found = (
            f"Chapter with id {normalized_chapter_id} not found"
        )
        try:
            chapter = await chapter_repo.get_chapter_by_id(
                normalized_chapter_id
            )
        except NotFoundError as exc:
            raise NotFoundError(chapter_not_found) from exc
        if str(chapter.get("novel_id") or "") != str(novel_id):
            raise NotFoundError(chapter_not_found)
        outline = normalize_outline_references(
            chapter.get("outline")
            if isinstance(chapter.get("outline"), dict)
            else None
        ) or {}
        chapter_record, chapter_truncated = (
            _illustration_chapter_record(chapter, outline)
        )
        required_records.append(chapter_record)
        if chapter_truncated:
            projection_truncated.append("章节细纲字段")

        scene_records: list[dict[str, Any]] = []
        for index, scene in enumerate(outline.get("scenes") or []):
            scene_record, scene_truncated = _illustration_scene_record(
                scene,
                index,
            )
            scene_records.append(scene_record)
            if scene_truncated and "章节场景字段" not in projection_truncated:
                projection_truncated.append("章节场景字段")
        if scene_records:
            optional_records.append(("章节场景", scene_records[0]))

        declared_character_ids = _canonical_declared_card_ids(
            outline.get("present_character_card_ids"),
            field_name="present_character_card_ids",
        )
        declared_worldbook_ids = _canonical_declared_card_ids(
            outline.get("referenced_worldbook_card_ids"),
            field_name="referenced_worldbook_card_ids",
        )
        character_cards, unresolved_characters = (
            await _declared_character_cards(
                novel_id=novel_id,
                card_ids=declared_character_ids,
            )
        )
        unresolved_references += unresolved_characters
        for card in character_cards:
            card_record, card_truncated = _illustration_card_record(card)
            optional_records.append(("细纲声明角色卡", card_record))
            if (
                card_truncated
                and "细纲声明角色卡字段" not in projection_truncated
            ):
                projection_truncated.append("细纲声明角色卡字段")

        worldbook_cards, unresolved_worldbook = (
            await _declared_worldbook_cards(
                novel_id=novel_id,
                card_ids=declared_worldbook_ids,
            )
        )
        unresolved_references += unresolved_worldbook
        for card in worldbook_cards:
            card_record, card_truncated = _illustration_card_record(card)
            optional_records.append(("细纲声明世界卡", card_record))
            if (
                card_truncated
                and "细纲声明世界卡字段" not in projection_truncated
            ):
                projection_truncated.append("细纲声明世界卡字段")
        optional_records.extend(
            ("章节场景", scene_record)
            for scene_record in scene_records[1:]
        )
        optional_records.append(("小说视觉设定", novel_record))
        target_label = (
            f"章节：{chapter.get('title') or normalized_chapter_id}"
        )

    text, budget_truncated, selected_counts = (
        _render_illustration_records(
            required_records=required_records,
            optional_records=optional_records,
            max_characters=max_characters,
        )
    )
    truncated_sections = tuple(
        dict.fromkeys([*projection_truncated, *budget_truncated])
    )
    coverage_parts = [
        f"范围={scope}",
        f"上下文 {len(text)}/{max_characters} 字符",
    ]
    if scope == "chapter":
        coverage_parts.extend(
            [
                f"场景 {selected_counts.get('章节场景', 0)} 条",
                (
                    "正式角色卡 "
                    f"{selected_counts.get('细纲声明角色卡', 0)} 张"
                ),
                (
                    "正式世界卡 "
                    f"{selected_counts.get('细纲声明世界卡', 0)} 张"
                ),
            ]
        )
        if unresolved_references:
            coverage_parts.append(
                f"未解析正式引用 {unresolved_references} 个"
            )
    if truncated_sections:
        coverage_parts.append(
            f"截断段落：{', '.join(truncated_sections)}"
        )
    coverage = "；".join(coverage_parts) + "。"

    if await narrative_revision_store.current(novel_id) != captured_revision:
        raise StaleAgentContext(
            "小说内容在插图提示词上下文装配期间发生变化，请重试"
        )
    context_digest = hashlib.sha256(
        json.dumps(
            {
                "novel_id": novel_id,
                "scope": scope,
                "character_card_id": normalized_character_id,
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
        truncated_sections=truncated_sections,
        target_label=target_label,
        novel_id=novel_id,
        scope=scope,
        chapter_id=normalized_chapter_id,
        character_card_id=normalized_character_id,
        narrative_revision=captured_revision,
        context_digest=context_digest,
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
            "小说内容在生成上下文装配期间发生变化，请重试"
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


def _retrospective_health_evidence(
    report: StoryHealthReport,
) -> list[AgentVolumeRetrospectiveEvidence]:
    """Project every required StoryHealth field without recomputing it."""

    evidence: list[AgentVolumeRetrospectiveEvidence] = []

    def append_record(
        *,
        evidence_id: str,
        kind: VolumeRetrospectiveEvidenceKind,
        label: str,
        payload: dict[str, Any],
        volume_id: str | None = None,
        chapter_id: str | None = None,
        thread_id: str | None = None,
        card_id: str | None = None,
    ) -> None:
        excerpt = _json(payload)
        if len(excerpt) > 2_000:
            raise ValueError(
                f"确定性故事健康记录超过单条证据上限: {evidence_id}"
            )
        evidence.append(
            AgentVolumeRetrospectiveEvidence(
                evidence_id=evidence_id,
                role="deterministic",
                kind=kind,
                label=label,
                excerpt=excerpt,
                volume_id=volume_id,
                chapter_id=chapter_id,
                thread_id=thread_id,
                card_id=card_id,
            )
        )

    for item in report.plot_threads:
        append_record(
            evidence_id=f"story_health:plot_thread:{item.thread_id}",
            kind="story_health_plot_thread",
            label=f"伏笔健康记录 · {item.name or item.thread_id}",
            payload=item.model_dump(mode="json"),
            thread_id=item.thread_id,
        )
    for item in report.character_absences:
        append_record(
            evidence_id=f"story_health:character_absence:{item.card_id}",
            kind="story_health_character_absence",
            label=f"角色缺席记录 · {item.name or item.card_id}",
            payload=item.model_dump(mode="json"),
            card_id=item.card_id,
        )
    for item in report.word_counts.volumes:
        append_record(
            evidence_id=f"story_health:volume_word_count:{item.volume_id}",
            kind="story_health_volume_word_count",
            label=f"卷字数记录 · {item.volume_title or item.volume_id}",
            payload=item.model_dump(mode="json"),
            volume_id=item.volume_id,
        )
    for item in report.word_counts.chapters:
        append_record(
            evidence_id=f"story_health:chapter_word_count:{item.chapter_id}",
            kind="story_health_chapter_word_count",
            label=(
                f"章节字数记录 · 第{item.volume_order}卷"
                f"第{item.chapter_order}章《{item.chapter_title}》"
            ),
            payload=item.model_dump(mode="json"),
            volume_id=item.volume_id,
            chapter_id=item.chapter_id,
        )
    return evidence


def _fit_retrospective_evidence(
    evidence: list[AgentVolumeRetrospectiveEvidence],
    budget: int,
) -> tuple[list[AgentVolumeRetrospectiveEvidence], str]:
    selected: list[AgentVolumeRetrospectiveEvidence] = []
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


def _retrospective_chapter_outline_evidence(
    *,
    positions: list[ChapterPosition],
    chapters_by_id: dict[str, dict[str, Any]],
) -> tuple[list[AgentVolumeRetrospectiveEvidence], bool]:
    evidence: list[AgentVolumeRetrospectiveEvidence] = []
    clipped_any = False
    for position in positions:
        chapter = chapters_by_id[position.chapter_id]
        outline = _outline_view(chapter.get("outline"))
        if outline is None:
            continue
        target_word_count = (chapter.get("outline") or {}).get(
            "target_word_count"
        )
        if target_word_count is not None:
            outline["target_word_count"] = target_word_count
        excerpt, clipped = _clip(_json(outline), 1_800)
        clipped_any = clipped_any or clipped
        evidence.append(
            AgentVolumeRetrospectiveEvidence(
                evidence_id=f"promise:chapter_outline:{position.chapter_id}",
                role="promise",
                kind="chapter_outline",
                label=(
                    f"第{position.volume_order}卷·"
                    f"第{position.chapter_order}章《"
                    f"{chapter.get('title') or ''}》细纲"
                ),
                excerpt=excerpt,
                volume_id=position.volume_id,
                chapter_id=position.chapter_id,
            )
        )
    return evidence, clipped_any


def _retrospective_prose_evidence(
    *,
    positions: list[ChapterPosition],
    chapters_by_id: dict[str, dict[str, Any]],
) -> list[AgentVolumeRetrospectiveEvidence]:
    evidence: list[AgentVolumeRetrospectiveEvidence] = []
    for position in positions:
        chapter = chapters_by_id[position.chapter_id]
        paragraphs = _sample_evenly(
            _paragraphs_with_indexes(chapter.get("content")),
            RETROSPECTIVE_PROSE_PARAGRAPHS_PER_CHAPTER,
        )
        for paragraph_index, paragraph in paragraphs:
            excerpt, _ = _clip(
                paragraph,
                RETROSPECTIVE_EVIDENCE_EXCERPT_CHARACTERS,
            )
            evidence.append(
                AgentVolumeRetrospectiveEvidence(
                    evidence_id=(
                        f"outcome:chapter:{position.chapter_id}:"
                        f"paragraph:{paragraph_index}"
                    ),
                    role="outcome",
                    kind="chapter_prose",
                    label=(
                        f"第{position.volume_order}卷·"
                        f"第{position.chapter_order}章《"
                        f"{chapter.get('title') or ''}》"
                        f"第{paragraph_index + 1}段"
                    ),
                    excerpt=excerpt,
                    volume_id=position.volume_id,
                    chapter_id=position.chapter_id,
                    paragraph_index=paragraph_index,
                )
            )
    return evidence


async def build_volume_retrospective_context(
    *,
    novel_id: str,
    volume_id: str,
    max_characters: int = MAX_CONTEXT_CHARACTERS,
) -> AgentContextBundle:
    """Build a volume review packet around authoritative StoryHealth facts."""

    if max_characters < RETROSPECTIVE_CONTEXT_MIN_CHARACTERS:
        raise ValueError(
            "卷级复盘上下文预算不能低于 "
            f"{RETROSPECTIVE_CONTEXT_MIN_CHARACTERS} 字符"
        )
    normalized_volume_id = str(volume_id or "").strip()
    if not normalized_volume_id:
        raise ValueError("卷级复盘必须指定 volume_id")

    captured_revision = await narrative_revision_store.current(novel_id)
    health_report = await story_health.inspect(
        novel_id,
        volume_id=normalized_volume_id,
    )
    if (
        health_report.schema_version != "story_health.v1"
        or health_report.scope.kind != "volume"
        or health_report.scope.volume_id != normalized_volume_id
    ):
        raise ValueError("故事健康报告版本或卷范围与复盘请求不一致")

    volumes = await volume_repo.get_volumes_by_novel(novel_id)
    chapters = await chapter_repo.get_chapters_by_novel(
        novel_id,
        include_content=True,
    )
    timeline = ChapterTimeline(volumes, chapters)
    volumes_by_id = {
        str(volume["_id"]): volume
        for volume in volumes
        if not volume.get("is_deleted")
    }
    target_volume = volumes_by_id.get(normalized_volume_id)
    if target_volume is None:
        raise ValueError("指定卷不属于当前小说")
    chapters_by_id = {
        str(chapter["_id"]): chapter
        for chapter in chapters
        if not chapter.get("is_deleted")
    }
    target_positions = [
        position
        for position in timeline.positions
        if position.volume_id == normalized_volume_id
    ]
    written_positions = [
        position
        for position in target_positions
        if str(chapters_by_id[position.chapter_id].get("content") or "").strip()
    ]
    if not written_positions:
        raise ValueError("目标卷没有可复盘的正文")

    volume_outline = {
        "title": str(target_volume.get("title") or ""),
        "summary": str(target_volume.get("summary") or ""),
        "arc": str(target_volume.get("arc") or ""),
        "chapter_range": target_volume.get("chapter_range"),
    }
    if not volume_outline["summary"] and not volume_outline["arc"]:
        raise ValueError("目标卷没有可核对的卷纲摘要或卷内弧线")
    volume_outline_excerpt, volume_outline_clipped = _clip(
        _json(volume_outline),
        2_000,
    )
    volume_outline_evidence = AgentVolumeRetrospectiveEvidence(
        evidence_id=f"promise:volume_outline:{normalized_volume_id}",
        role="promise",
        kind="volume_outline",
        label=f"卷纲承诺 · {target_volume.get('title') or normalized_volume_id}",
        excerpt=volume_outline_excerpt,
        volume_id=normalized_volume_id,
    )

    health_evidence = _retrospective_health_evidence(health_report)
    sampled_positions = _sample_evenly(
        target_positions,
        RETROSPECTIVE_CHAPTER_SAMPLE_LIMIT,
    )
    sampled_written_positions = _sample_evenly(
        written_positions,
        RETROSPECTIVE_CHAPTER_SAMPLE_LIMIT,
    )
    outline_candidates, outline_clipped = (
        _retrospective_chapter_outline_evidence(
            positions=sampled_positions,
            chapters_by_id=chapters_by_id,
        )
    )
    prose_candidates = _retrospective_prose_evidence(
        positions=sampled_written_positions,
        chapters_by_id=chapters_by_id,
    )

    metadata = _json(
        {
            "story_health_schema_version": health_report.schema_version,
            "story_health_scope": health_report.scope.model_dump(mode="json"),
            "story_health_observation": health_report.observation.model_dump(
                mode="json"
            ),
            "story_health_summary": health_report.summary.model_dump(
                mode="json"
            ),
            "story_health_policies": health_report.policies.model_dump(
                mode="json"
            ),
            "semantic_sampling_policy": {
                "chapters": (
                    "目标卷按稳定章节顺序均匀抽取最多 18 章，"
                    "保留首章与末章"
                ),
                "prose": "每个抽样有正文章节均匀抽取最多 2 段",
                "deterministic_story_health": (
                    "plot_threads、character_absences、"
                    "word_counts.volumes、word_counts.chapters 全量装入；"
                    "不允许模型重新统计"
                ),
            },
        }
    )
    health_rendered = _json(
        [item.prompt_view() for item in health_evidence]
    )
    volume_outline_rendered = _json([volume_outline_evidence.prompt_view()])
    empty_sections = [
        f"【故事健康报告元数据】\n{metadata}",
        f"【确定性故事健康证据（全量，不得重算）】\n{health_rendered}",
        f"【卷纲承诺】\n{volume_outline_rendered}",
        "【抽样章细纲承诺】\n[]",
        "【抽样正文结果】\n[]",
    ]
    empty_text = "\n\n".join(empty_sections)
    if len(empty_text) > max_characters:
        raise ValueError(
            "目标卷的确定性故事健康证据超过复盘上下文硬上限；"
            "系统不会静默裁剪后让模型重新统计"
        )

    available = max_characters - len(empty_text)
    outline_budget = 2 + available * 40 // 100
    prose_budget = 2 + available - (outline_budget - 2)
    selected_outlines, outlines_rendered = _fit_retrospective_evidence(
        outline_candidates,
        outline_budget,
    )
    selected_prose, prose_rendered = _fit_retrospective_evidence(
        prose_candidates,
        prose_budget,
    )
    if not selected_prose:
        raise ValueError("卷级复盘上下文预算不足以容纳正文结果证据")

    text = "\n\n".join(
        [
            f"【故事健康报告元数据】\n{metadata}",
            f"【确定性故事健康证据（全量，不得重算）】\n{health_rendered}",
            f"【卷纲承诺】\n{volume_outline_rendered}",
            f"【抽样章细纲承诺】\n{outlines_rendered}",
            f"【抽样正文结果】\n{prose_rendered}",
        ]
    )
    if len(text) > max_characters:
        raise ValueError("卷级复盘上下文超过硬预算")

    truncated: list[str] = []
    if volume_outline_clipped:
        truncated.append("卷纲承诺")
    if (
        len(sampled_positions) < len(target_positions)
        or len(selected_outlines) < len(outline_candidates)
        or outline_clipped
    ):
        truncated.append("抽样章细纲承诺")
    if (
        len(sampled_written_positions) < len(written_positions)
        or len(selected_prose) < len(prose_candidates)
    ):
        truncated.append("抽样正文结果")

    target_label = (
        f"卷：{target_volume.get('title') or normalized_volume_id}"
    )
    coverage = (
        f"直接消费 {health_report.schema_version}："
        f"伏笔 {len(health_report.plot_threads)} 条、"
        f"角色缺席 {len(health_report.character_absences)} 条、"
        f"卷字数 {len(health_report.word_counts.volumes)} 条、"
        f"章字数 {len(health_report.word_counts.chapters)} 条；"
        f"语义证据抽样 {len(selected_outlines)}/"
        f"{len([position for position in target_positions if chapters_by_id[position.chapter_id].get('outline')])}"
        f" 份章细纲与 {len(selected_prose)} 段正文，覆盖 "
        f"{len({item.chapter_id for item in selected_prose})}/"
        f"{len(written_positions)} 个有正文章节。"
    )
    if truncated:
        coverage += f" 截断段落：{', '.join(truncated)}。"

    if await narrative_revision_store.current(novel_id) != captured_revision:
        raise StaleAgentContext(
            "小说内容在卷级复盘上下文装配期间发生变化，请重试"
        )
    context_digest = hashlib.sha256(
        json.dumps(
            {
                "novel_id": novel_id,
                "scope": "volume",
                "volume_id": normalized_volume_id,
                "narrative_revision": captured_revision,
                "text": text,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    selected_evidence = (
        *health_evidence,
        volume_outline_evidence,
        *selected_outlines,
        *selected_prose,
    )
    return AgentContextBundle(
        text=text,
        coverage=coverage,
        truncated_sections=tuple(truncated),
        target_label=target_label,
        novel_id=novel_id,
        scope="volume",
        volume_id=normalized_volume_id,
        chapter_id=None,
        narrative_revision=captured_revision,
        context_digest=context_digest,
        story_health_schema_version=health_report.schema_version,
        thread_ids=tuple(item.thread_id for item in health_report.plot_threads),
        volume_retrospective_evidence=tuple(selected_evidence),
    )
