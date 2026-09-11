"""Deterministic, read-only story health signals.

This module deliberately has no dependency on the LLM runtime.  It turns
existing novel, volume, chapter, plot-thread, and character-card snapshots into
one versioned report that both the writing UI and later volume retrospectives
can consume.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.repositories.character_repository import character_repo
from backend.db.repositories.novel_repository import novel_repo
from backend.db.repositories.plot_thread_repository import (
    ACTIVE_THREAD_STATUSES,
    plot_thread_repo,
)
from backend.db.repositories.volume_repository import volume_repo
from backend.services.novel.chapter_timeline import ChapterPosition, ChapterTimeline


STORY_HEALTH_SCHEMA_VERSION: Literal["story_health.v1"] = "story_health.v1"
DEFAULT_CHAPTER_TARGET_WORD_COUNT = 3_000
WORD_DEVIATION_ATTENTION_RATIO = 0.20

DueState = Literal[
    "unscheduled",
    "upcoming",
    "due",
    "overdue",
    "unmapped",
    "not_started",
]
DeviationState = Literal[
    "under",
    "within_target",
    "over",
    "no_content",
    "no_chapters",
]
TargetSource = Literal[
    "chapter_outline",
    "novel_default",
    "system_default",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChapterHealthPosition(_StrictModel):
    chapter_id: str
    volume_id: str
    volume_order: int
    chapter_order: int
    book_ordinal: int
    volume_title: str
    chapter_title: str


class StoryHealthPolicies(_StrictModel):
    timeline_ordering: str = "volume_order,chapter_order,chapter_id"
    progress_basis: str = "latest chapter with saved prose (word_count > 0)"
    character_presence_basis: str = "outline.present_character_card_ids of chapters with saved prose"
    chapter_target_precedence: str = (
        "outline.target_word_count|novel.words_per_chapter|3000"
    )
    word_deviation_attention_ratio: float = WORD_DEVIATION_ATTENTION_RATIO


class PlotThreadHealth(_StrictModel):
    thread_id: str
    name: str
    status: str
    importance: str
    planted_at: ChapterHealthPosition | None
    due_target: dict[str, Any] | None
    due_at: ChapterHealthPosition | None
    due_book_ordinal: int | None
    age_in_chapters: int | None
    due_state: DueState
    chapters_until_due: int | None
    overdue_by_chapters: int | None
    attention_required: bool
    unavailable_reason: str | None


class CharacterAbsenceHealth(_StrictModel):
    card_id: str
    name: str
    importance: str
    last_present_at: ChapterHealthPosition | None
    consecutive_absent_chapters: int
    observed_outline_chapters: int
    never_present: bool
    currently_absent: bool


class ChapterWordCountHealth(_StrictModel):
    chapter_id: str
    volume_id: str
    volume_order: int
    chapter_order: int
    book_ordinal: int
    chapter_title: str
    actual_word_count: int
    target_word_count: int
    target_source: TargetSource
    delta_word_count: int
    completion_ratio: float
    deviation_ratio: float
    deviation_state: DeviationState
    attention_required: bool


class VolumeWordCountHealth(_StrictModel):
    volume_id: str
    volume_order: int
    volume_title: str
    chapter_count: int
    chapters_with_content: int
    actual_word_count: int
    target_word_count: int
    delta_word_count: int
    completion_ratio: float | None
    deviation_ratio: float | None
    deviation_state: DeviationState
    attention_required: bool


class StoryWordCountHealth(_StrictModel):
    volumes: list[VolumeWordCountHealth]
    chapters: list[ChapterWordCountHealth]


class StoryHealthObservation(_StrictModel):
    active_chapter_count: int
    progress_chapter_count: int
    outlined_chapter_count: int
    chapters_with_content: int


class StoryHealthScope(_StrictModel):
    kind: Literal["book", "volume"]
    volume_id: str | None


class StoryHealthSummary(_StrictModel):
    active_plot_thread_count: int
    due_plot_thread_count: int
    overdue_plot_thread_count: int
    unmapped_plot_thread_count: int
    character_count: int
    currently_absent_character_count: int
    chapter_word_deviation_count: int
    volume_word_deviation_count: int


class StoryHealthReport(_StrictModel):
    schema_version: Literal["story_health.v1"]
    novel_id: str
    scope: StoryHealthScope
    as_of: ChapterHealthPosition | None
    policies: StoryHealthPolicies
    observation: StoryHealthObservation
    summary: StoryHealthSummary
    plot_threads: list[PlotThreadHealth]
    character_absences: list[CharacterAbsenceHealth]
    word_counts: StoryWordCountHealth


def _positive_int(value: Any) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _safe_position(
    timeline: ChapterTimeline,
    chapter_id: Any,
) -> ChapterPosition | None:
    if not chapter_id:
        return None
    try:
        return timeline.position(str(chapter_id))
    except ValueError:
        return None


def _position_view(
    position: ChapterPosition | None,
    *,
    volumes_by_id: dict[str, dict[str, Any]],
    chapters_by_id: dict[str, dict[str, Any]],
) -> ChapterHealthPosition | None:
    if position is None:
        return None
    volume = volumes_by_id.get(position.volume_id, {})
    chapter = chapters_by_id.get(position.chapter_id, {})
    return ChapterHealthPosition(
        chapter_id=position.chapter_id,
        volume_id=position.volume_id,
        volume_order=position.volume_order,
        chapter_order=position.chapter_order,
        book_ordinal=position.book_ordinal,
        volume_title=str(volume.get("title") or ""),
        chapter_title=str(chapter.get("title") or ""),
    )


def _chapter_has_progress(chapter: dict[str, Any]) -> bool:
    return _positive_int(chapter.get("word_count")) is not None


def _due_state(
    *,
    due_ordinal: int | None,
    as_of: ChapterPosition | None,
    unmapped: bool,
    scheduled: bool,
) -> tuple[DueState, int | None, int | None]:
    if unmapped:
        return "unmapped", None, None
    if not scheduled:
        return "unscheduled", None, None
    if as_of is None:
        return "not_started", None, None
    if due_ordinal is None:
        return "unmapped", None, None
    remaining = due_ordinal - as_of.book_ordinal
    if remaining > 0:
        return "upcoming", remaining, 0
    if remaining == 0:
        return "due", 0, 0
    return "overdue", 0, abs(remaining)


def _build_plot_thread_health(
    *,
    threads: list[dict[str, Any]],
    timeline: ChapterTimeline,
    as_of: ChapterPosition | None,
    volumes_by_id: dict[str, dict[str, Any]],
    chapters_by_id: dict[str, dict[str, Any]],
) -> list[PlotThreadHealth]:
    signals: list[PlotThreadHealth] = []
    for thread in threads:
        if str(thread.get("status") or "") not in ACTIVE_THREAD_STATUSES:
            continue

        planted_id = thread.get("planted_chapter_id")
        planted_position = _safe_position(timeline, planted_id)
        if (
            as_of is not None
            and planted_position is not None
            and planted_position > as_of
        ):
            continue
        unavailable_reason = None
        if planted_position is None:
            unavailable_reason = "missing_stable_chapter_reference"

        age_in_chapters = None
        if planted_position is not None and as_of is not None:
            age_in_chapters = max(
                0,
                as_of.book_ordinal - planted_position.book_ordinal,
            )

        due_target = (
            dict(thread["due_target"])
            if isinstance(thread.get("due_target"), dict)
            else None
        )
        if due_target is not None and due_target.get("chapter_id") is not None:
            due_target["chapter_id"] = str(due_target["chapter_id"])
        due_position = None
        due_ordinal = None
        scheduled = False
        unmapped = False
        if due_target is not None:
            scheduled = True
            if due_target.get("kind") == "chapter":
                due_position = _safe_position(timeline, due_target.get("chapter_id"))
                due_ordinal = (
                    due_position.book_ordinal if due_position is not None else None
                )
                unmapped = due_position is None
                if unmapped and unavailable_reason is None:
                    unavailable_reason = "invalid_due_chapter_reference"
            elif due_target.get("kind") == "planned_ordinal":
                due_ordinal = _positive_int(due_target.get("ordinal"))
                unmapped = due_ordinal is None
                if unmapped and unavailable_reason is None:
                    unavailable_reason = "invalid_planned_ordinal"
            else:
                unmapped = True
                if unavailable_reason is None:
                    unavailable_reason = "invalid_due_target"
        elif thread.get("due_chapter_order") is not None:
            # Legacy local order is intentionally never interpreted as a
            # book-wide position.  Cross-volume duplicates make it ambiguous.
            scheduled = True
            unmapped = True
            if unavailable_reason is None:
                unavailable_reason = "legacy_due_chapter_order_requires_mapping"

        state, chapters_until_due, overdue_by_chapters = _due_state(
            due_ordinal=due_ordinal,
            as_of=as_of,
            unmapped=unmapped,
            scheduled=scheduled,
        )
        signals.append(
            PlotThreadHealth(
                thread_id=str(thread.get("_id") or ""),
                name=str(thread.get("name") or ""),
                status=str(thread.get("status") or ""),
                importance=str(thread.get("importance") or "sub"),
                planted_at=_position_view(
                    planted_position,
                    volumes_by_id=volumes_by_id,
                    chapters_by_id=chapters_by_id,
                ),
                due_target=due_target,
                due_at=_position_view(
                    due_position,
                    volumes_by_id=volumes_by_id,
                    chapters_by_id=chapters_by_id,
                ),
                due_book_ordinal=due_ordinal,
                age_in_chapters=age_in_chapters,
                due_state=state,
                chapters_until_due=chapters_until_due,
                overdue_by_chapters=overdue_by_chapters,
                attention_required=state in {"due", "overdue"},
                unavailable_reason=unavailable_reason,
            )
        )

    due_rank = {
        "overdue": 0,
        "due": 1,
        "upcoming": 2,
        "unmapped": 3,
        "not_started": 4,
        "unscheduled": 5,
    }
    signals.sort(
        key=lambda signal: (
            due_rank[signal.due_state],
            -(signal.overdue_by_chapters or 0),
            -(signal.age_in_chapters or 0),
            signal.importance != "main",
            signal.name,
            signal.thread_id,
        )
    )
    return signals


def _build_character_absences(
    *,
    character_cards: list[dict[str, Any]],
    outline_positions: list[ChapterPosition],
    chapters_by_id: dict[str, dict[str, Any]],
    volumes_by_id: dict[str, dict[str, Any]],
) -> list[CharacterAbsenceHealth]:
    present_by_chapter: dict[str, set[str]] = {}
    for position in outline_positions:
        outline = chapters_by_id[position.chapter_id].get("outline") or {}
        present_by_chapter[position.chapter_id] = {
            str(card_id)
            for card_id in (outline.get("present_character_card_ids") or [])
        }

    absences: list[CharacterAbsenceHealth] = []
    for card in character_cards:
        card_id = str(card.get("_id") or "")
        appearances = [
            position
            for position in outline_positions
            if card_id in present_by_chapter[position.chapter_id]
        ]
        last_present = appearances[-1] if appearances else None
        if last_present is None:
            consecutive_absent = len(outline_positions)
        else:
            consecutive_absent = sum(
                1 for position in outline_positions if position > last_present
            )
        absences.append(
            CharacterAbsenceHealth(
                card_id=card_id,
                name=str(card.get("name") or ""),
                importance=str(card.get("importance") or "sub"),
                last_present_at=_position_view(
                    last_present,
                    volumes_by_id=volumes_by_id,
                    chapters_by_id=chapters_by_id,
                ),
                consecutive_absent_chapters=consecutive_absent,
                observed_outline_chapters=len(outline_positions),
                never_present=last_present is None,
                currently_absent=consecutive_absent > 0,
            )
        )

    absences.sort(
        key=lambda signal: (
            -signal.consecutive_absent_chapters,
            signal.importance != "main",
            signal.name,
            signal.card_id,
        )
    )
    return absences


def _deviation(
    actual: int,
    target: int,
    *,
    empty_state: DeviationState,
) -> tuple[int, float | None, float | None, DeviationState, bool]:
    if target <= 0:
        return actual, None, None, empty_state, False
    delta = actual - target
    completion_ratio = round(actual / target, 4)
    deviation_ratio = round(delta / target, 4)
    if actual == 0:
        state: DeviationState = empty_state
    elif deviation_ratio < -WORD_DEVIATION_ATTENTION_RATIO:
        state = "under"
    elif deviation_ratio > WORD_DEVIATION_ATTENTION_RATIO:
        state = "over"
    else:
        state = "within_target"
    return (
        delta,
        completion_ratio,
        deviation_ratio,
        state,
        state in {"under", "over"},
    )


def _chapter_target(
    chapter: dict[str, Any],
    novel: dict[str, Any],
) -> tuple[int, TargetSource]:
    outline_target = _positive_int(
        (chapter.get("outline") or {}).get("target_word_count")
    )
    if outline_target is not None:
        return outline_target, "chapter_outline"
    novel_target = _positive_int(novel.get("words_per_chapter"))
    if novel_target is not None:
        return novel_target, "novel_default"
    return DEFAULT_CHAPTER_TARGET_WORD_COUNT, "system_default"


def _build_word_counts(
    *,
    novel: dict[str, Any],
    positions: list[ChapterPosition],
    included_volume_ids: set[str],
    volumes_by_id: dict[str, dict[str, Any]],
    chapters_by_id: dict[str, dict[str, Any]],
) -> StoryWordCountHealth:
    chapter_signals: list[ChapterWordCountHealth] = []
    for position in positions:
        chapter = chapters_by_id[position.chapter_id]
        actual = max(0, int(chapter.get("word_count") or 0))
        target, target_source = _chapter_target(chapter, novel)
        delta, completion, deviation, state, attention = _deviation(
            actual,
            target,
            empty_state="no_content",
        )
        chapter_signals.append(
            ChapterWordCountHealth(
                chapter_id=position.chapter_id,
                volume_id=position.volume_id,
                volume_order=position.volume_order,
                chapter_order=position.chapter_order,
                book_ordinal=position.book_ordinal,
                chapter_title=str(chapter.get("title") or ""),
                actual_word_count=actual,
                target_word_count=target,
                target_source=target_source,
                delta_word_count=delta,
                completion_ratio=completion or 0.0,
                deviation_ratio=deviation or 0.0,
                deviation_state=state,
                attention_required=attention,
            )
        )

    volume_signals: list[VolumeWordCountHealth] = []
    for volume_id, volume in sorted(
        (
            (volume_id, volume)
            for volume_id, volume in volumes_by_id.items()
            if volume_id in included_volume_ids
        ),
        key=lambda item: (
            int(item[1].get("order_index") or 0),
            item[0],
        ),
    ):
        scoped = [
            signal
            for signal in chapter_signals
            if signal.volume_id == volume_id
        ]
        actual = sum(signal.actual_word_count for signal in scoped)
        target = sum(signal.target_word_count for signal in scoped)
        delta, completion, deviation, state, attention = _deviation(
            actual,
            target,
            empty_state="no_chapters" if not scoped else "no_content",
        )
        volume_signals.append(
            VolumeWordCountHealth(
                volume_id=volume_id,
                volume_order=int(volume.get("order_index") or 0),
                volume_title=str(volume.get("title") or ""),
                chapter_count=len(scoped),
                chapters_with_content=sum(
                    1 for signal in scoped if signal.actual_word_count > 0
                ),
                actual_word_count=actual,
                target_word_count=target,
                delta_word_count=delta,
                completion_ratio=completion,
                deviation_ratio=deviation,
                deviation_state=state,
                attention_required=attention,
            )
        )

    return StoryWordCountHealth(
        volumes=volume_signals,
        chapters=chapter_signals,
    )


def build_story_health_report(
    *,
    novel: dict[str, Any],
    volumes: list[dict[str, Any]],
    chapters: list[dict[str, Any]],
    plot_threads: list[dict[str, Any]],
    character_cards: list[dict[str, Any]],
    volume_id: str | None = None,
) -> StoryHealthReport:
    """Build one deterministic report from already-loaded snapshots."""
    timeline = ChapterTimeline(volumes, chapters)
    volumes_by_id = {str(volume["_id"]): volume for volume in volumes}
    chapters_by_id = {str(chapter["_id"]): chapter for chapter in chapters}
    normalized_volume_id = str(volume_id) if volume_id is not None else None
    if (
        normalized_volume_id is not None
        and normalized_volume_id not in volumes_by_id
    ):
        raise ValueError(
            f"Volume '{normalized_volume_id}' is not active in this novel"
        )
    scoped_positions = [
        position
        for position in timeline.positions
        if (
            normalized_volume_id is None
            or position.volume_id == normalized_volume_id
        )
    ]

    progress_positions = [
        position
        for position in scoped_positions
        if _chapter_has_progress(chapters_by_id[position.chapter_id])
    ]
    as_of = progress_positions[-1] if progress_positions else None
    outline_positions = [
        position
        for position in progress_positions
        if chapters_by_id[position.chapter_id].get("outline")
    ]

    thread_signals = _build_plot_thread_health(
        threads=plot_threads,
        timeline=timeline,
        as_of=as_of,
        volumes_by_id=volumes_by_id,
        chapters_by_id=chapters_by_id,
    )
    character_signals = _build_character_absences(
        character_cards=character_cards,
        outline_positions=outline_positions,
        chapters_by_id=chapters_by_id,
        volumes_by_id=volumes_by_id,
    )
    word_counts = _build_word_counts(
        novel=novel,
        positions=scoped_positions,
        included_volume_ids=(
            {normalized_volume_id}
            if normalized_volume_id is not None
            else set(volumes_by_id)
        ),
        volumes_by_id=volumes_by_id,
        chapters_by_id=chapters_by_id,
    )

    return StoryHealthReport(
        schema_version=STORY_HEALTH_SCHEMA_VERSION,
        novel_id=str(novel.get("_id") or ""),
        scope=StoryHealthScope(
            kind="volume" if normalized_volume_id is not None else "book",
            volume_id=normalized_volume_id,
        ),
        as_of=_position_view(
            as_of,
            volumes_by_id=volumes_by_id,
            chapters_by_id=chapters_by_id,
        ),
        policies=StoryHealthPolicies(),
        observation=StoryHealthObservation(
            active_chapter_count=len(scoped_positions),
            progress_chapter_count=len(progress_positions),
            outlined_chapter_count=len(outline_positions),
            chapters_with_content=sum(
                1
                for position in scoped_positions
                if int(chapters_by_id[position.chapter_id].get("word_count") or 0) > 0
            ),
        ),
        summary=StoryHealthSummary(
            active_plot_thread_count=len(thread_signals),
            due_plot_thread_count=sum(
                1 for signal in thread_signals if signal.due_state == "due"
            ),
            overdue_plot_thread_count=sum(
                1 for signal in thread_signals if signal.due_state == "overdue"
            ),
            unmapped_plot_thread_count=sum(
                1 for signal in thread_signals if signal.due_state == "unmapped"
            ),
            character_count=len(character_signals),
            currently_absent_character_count=sum(
                1 for signal in character_signals if signal.currently_absent
            ),
            chapter_word_deviation_count=sum(
                1
                for signal in word_counts.chapters
                if signal.attention_required
            ),
            volume_word_deviation_count=sum(
                1
                for signal in word_counts.volumes
                if signal.attention_required
            ),
        ),
        plot_threads=thread_signals,
        character_absences=character_signals,
        word_counts=word_counts,
    )


class StoryHealthModule:
    """Read snapshots and expose the report through one read-only interface."""

    async def inspect(
        self,
        novel_id: str,
        *,
        volume_id: str | None = None,
    ) -> StoryHealthReport:
        novel = await novel_repo.get_novel_by_id(novel_id)
        volumes = await volume_repo.get_volumes_by_novel(novel_id)
        chapters = await chapter_repo.get_chapters_by_novel(
            novel_id,
            include_content=False,
        )
        threads = await plot_thread_repo.list_threads(novel_id)
        characters = await character_repo.list_cards(novel_id, "character")
        return build_story_health_report(
            novel=novel,
            volumes=volumes,
            chapters=chapters,
            plot_threads=threads,
            character_cards=characters,
            volume_id=volume_id,
        )


story_health = StoryHealthModule()
