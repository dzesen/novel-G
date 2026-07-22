"""Rebuild denormalized novel and volume statistics from active primary data."""

from __future__ import annotations

from typing import Any, Dict

from pymongo.asynchronous.client_session import AsyncClientSession

from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.repositories.novel_repository import novel_repo
from backend.db.repositories.volume_repository import volume_repo
from backend.db.utils import to_object_id


class DerivedStats:
    """Own the single write path for cached volume and novel counters."""

    async def refresh(
        self,
        novel_id: str,
        *,
        session: AsyncClientSession | None = None,
    ) -> Dict[str, Any]:
        await novel_repo.get_novel_by_id(novel_id, session=session)
        volumes = await volume_repo.get_volumes_by_novel(novel_id, session=session)
        chapters = await chapter_repo.get_chapters_by_novel(
            novel_id,
            include_content=False,
            session=session,
        )

        volume_ids = {volume["_id"] for volume in volumes}
        volume_stats: Dict[str, Dict[str, int]] = {
            str(volume["_id"]): {"chapter_count": 0, "word_count": 0}
            for volume in volumes
        }
        for chapter in chapters:
            volume_id = chapter.get("volume_id")
            if volume_id not in volume_ids:
                continue
            current = volume_stats[str(volume_id)]
            current["chapter_count"] += 1
            current["word_count"] += int(chapter.get("word_count") or 0)

        for volume_id, stats in volume_stats.items():
            await volume_repo.update_one(
                {"_id": to_object_id(volume_id)},
                stats,
                session=session,
            )

        novel_stats = {
            "current_volume_count": len(volumes),
            "current_chapter_count": sum(
                stats["chapter_count"] for stats in volume_stats.values()
            ),
            "current_word_count": sum(
                stats["word_count"] for stats in volume_stats.values()
            ),
        }
        await novel_repo.update_novel_stats(
            novel_id,
            novel_stats,
            session=session,
        )
        return {"novel": novel_stats, "volumes": volume_stats}


derived_stats = DerivedStats()
