"""Read-only historical audit for chapter state-backfill coverage."""
from __future__ import annotations

from collections import Counter
from typing import Any

from backend.db import collections
from backend.db.mongo import get_database
from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.repositories.novel_repository import novel_repo
from backend.db.repositories.volume_repository import volume_repo
from backend.db.utils import to_object_id
from backend.services.generation.job_planner import order_book_chapters
from backend.services.novel.state_completion import state_completion_module


_REPAIRABLE = frozenset(
    {
        "missing",
        "stale_after_content_edit",
        "degraded_all_character_updates_dropped",
        "degraded_partial_reference_drop",
        "degraded_fact_accounting",
    }
)


def _job_reference_evidence(jobs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    evidence: dict[str, dict[str, Any]] = {}
    for job in jobs:
        for progress in job.get("progress") or []:
            chapter_id = str(progress.get("chapter_id") or "")
            dropped = progress.get("dropped_ids") or {}
            character_ids = [
                str(value)
                for value in dropped.get("character_updates") or []
            ]
            if not chapter_id or not character_ids:
                continue
            evidence[chapter_id] = {
                "job_id": str(job.get("_id") or ""),
                "dropped_character_ids": character_ids,
                "recorded_at": progress.get("completed_at"),
            }
    return evidence


class StateCompletenessAudit:
    async def audit(
        self,
        *,
        actor_id: str,
        novel_id: str,
        scope: str,
        volume_id: str | None = None,
    ) -> dict[str, Any]:
        del actor_id  # Ownership is enforced by the route before this read model.
        if scope not in {"book", "volume"}:
            raise ValueError("scope must be book or volume")
        await novel_repo.get_novel_by_id(novel_id)
        volumes = await volume_repo.get_volumes_by_novel(novel_id)
        volume_order = {
            str(volume["_id"]): int(volume.get("order_index") or 0)
            for volume in volumes
        }
        volume_names = {
            str(volume["_id"]): str(volume.get("title") or "")
            for volume in volumes
        }
        if scope == "volume":
            if not volume_id:
                raise ValueError("volume scope requires volume_id")
            volume = await volume_repo.get_volume_by_id(volume_id)
            if str(volume.get("novel_id")) != str(novel_id):
                raise ValueError("Volume does not belong to novel")
            chapters = await chapter_repo.get_chapters_by_volume(
                volume_id,
                include_content=True,
            )
        else:
            chapters = await chapter_repo.get_chapters_by_novel(
                novel_id,
                include_content=True,
            )
        chapters = order_book_chapters(chapters, volume_order)
        completions = await state_completion_module.inspect_many(chapters)
        jobs = await get_database()[collections.GENERATION_JOBS].find(
            {"novel_id": to_object_id(novel_id), "is_deleted": False},
            projection={"progress": 1},
        ).to_list(length=None)
        job_evidence = _job_reference_evidence(jobs)

        items: list[dict[str, Any]] = []
        repair_queue: list[str] = []
        for chapter in chapters:
            chapter_id = str(chapter["_id"])
            completion = completions[chapter_id]
            status = completion.status
            if status == "missing":
                if str(chapter.get("content") or "").strip():
                    category = (
                        "summary_without_delta"
                        if str(chapter.get("summary") or "").strip()
                        else "content_without_delta"
                    )
                else:
                    category = "no_prose"
            elif status == "current":
                category = (
                    "legal_empty"
                    if completion.completion_reason == "legitimate_empty"
                    else "current"
                )
            else:
                category = status

            historical = job_evidence.get(chapter_id)
            if (
                historical
                and category in {"current", "legal_empty", "unknown_legacy"}
            ):
                category = "degraded_job_reference_drop_evidence"

            repair_recommended = (
                status in _REPAIRABLE
                and completion.prose_eligible
                and category != "no_prose"
            ) or category == "degraded_job_reference_drop_evidence"
            if repair_recommended:
                repair_queue.append(chapter_id)
            items.append(
                {
                    "chapter_id": chapter_id,
                    "volume_id": str(chapter.get("volume_id") or ""),
                    "volume_title": volume_names.get(
                        str(chapter.get("volume_id") or ""),
                        "",
                    ),
                    "order_index": int(chapter.get("order_index") or 0),
                    "title": str(chapter.get("title") or ""),
                    "has_content": bool(
                        str(chapter.get("content") or "").strip()
                    ),
                    "has_summary": bool(
                        str(chapter.get("summary") or "").strip()
                    ),
                    "category": category,
                    "completion": completion.to_dict(),
                    "repair_recommended": repair_recommended,
                    "historical_job_evidence": historical,
                }
            )

        counts = Counter(item["category"] for item in items)
        healthy_categories = {"current", "legal_empty", "no_prose"}
        return {
            "novel_id": str(novel_id),
            "scope": scope,
            "volume_id": str(volume_id) if volume_id else None,
            "chapter_count": len(items),
            "issue_count": sum(
                count
                for category, count in counts.items()
                if category not in healthy_categories
            ),
            "counts": dict(sorted(counts.items())),
            "repair_queue": repair_queue,
            "chapters": items,
            "read_only": True,
        }


state_completeness_audit = StateCompletenessAudit()
