"""稳定章节 ID 与卷/章复合叙事位置之间的唯一映射。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True, order=True)
class ChapterPosition:
    volume_order: int
    chapter_order: int
    book_ordinal: int
    chapter_id: str
    volume_id: str

    @property
    def label(self) -> str:
        return f"第{self.volume_order}卷·第{self.chapter_order}章"


class ChapterTimeline:
    """从当前卷/章顺序派生位置；身份始终由 chapter_id 保持稳定。"""

    def __init__(self, volumes: Iterable[dict[str, Any]], chapters: Iterable[dict[str, Any]]) -> None:
        volume_order = {
            str(volume["_id"]): int(volume.get("order_index") or 0)
            for volume in volumes
            if not volume.get("is_deleted")
        }
        ordered: list[tuple[int, int, str, str]] = []
        for chapter in chapters:
            if chapter.get("is_deleted"):
                continue
            volume_id = str(chapter.get("volume_id") or "")
            if volume_id not in volume_order:
                continue
            chapter_id = str(chapter["_id"])
            ordered.append(
                (
                    volume_order[volume_id],
                    int(chapter.get("order_index") or 0),
                    chapter_id,
                    volume_id,
                )
            )
        ordered.sort(key=lambda item: (item[0], item[1], item[2]))
        self._positions = tuple(
            ChapterPosition(
                volume_order=volume_order_value,
                chapter_order=chapter_order,
                book_ordinal=index,
                chapter_id=chapter_id,
                volume_id=volume_id,
            )
            for index, (volume_order_value, chapter_order, chapter_id, volume_id) in enumerate(
                ordered, start=1
            )
        )
        self._by_id = {position.chapter_id: position for position in self._positions}

    @property
    def positions(self) -> tuple[ChapterPosition, ...]:
        return self._positions

    def position(self, chapter_id: str) -> ChapterPosition:
        try:
            return self._by_id[str(chapter_id)]
        except KeyError as exc:
            raise ValueError(f"Chapter '{chapter_id}' is not present in the active timeline") from exc

    def recent_before(self, chapter_id: str, limit: int) -> tuple[ChapterPosition, ...]:
        target = self.position(chapter_id)
        return tuple(position for position in self._positions if position < target)[-limit:]

    def unique_legacy_order(self, chapter_order: int) -> ChapterPosition | None:
        matches = [p for p in self._positions if p.chapter_order == int(chapter_order)]
        return matches[0] if len(matches) == 1 else None

    def is_before_or_equal(self, source_chapter_id: str, target_chapter_id: str) -> bool:
        return self.position(source_chapter_id) <= self.position(target_chapter_id)


async def validate_chapter_reference(novel_id: str, chapter_id: str) -> dict[str, Any]:
    """验证稳定章节引用属于当前小说，并返回章节快照。"""
    from backend.db.repositories.chapter_repository import chapter_repo

    chapter = await chapter_repo.get_chapter_by_id(chapter_id, include_deleted=True)
    if str(chapter.get("novel_id")) != str(novel_id):
        raise ValueError(f"Chapter '{chapter_id}' does not belong to novel '{novel_id}'")
    return chapter
