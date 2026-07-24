"""章节状态预览、接受增量、快照与人工订正的可回放时间线。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from pymongo.asynchronous.client_session import AsyncClientSession

from backend.db import collections
from backend.db.mongo import get_database
from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.repositories.character_state_repository import character_state_repo
from backend.db.repositories.volume_repository import volume_repo
from backend.db.utils import get_utc_now, to_object_id
from backend.services.novel.chapter_timeline import ChapterTimeline
from backend.services.novel.state_proposal import (
    _content_digest,
    _digest,
)


async def record_acceptance(
    novel_id: str,
    chapter_id: str,
    payload: dict[str, Any],
    *,
    evaluation: dict[str, Any] | None = None,
    session: AsyncClientSession | None = None,
) -> int:
    """幂等写入每章唯一完整 delta，并派生人物快照和伏笔事件。"""
    db = get_database()
    now = get_utc_now()
    existing = await db[collections.CHAPTER_STATE_DELTAS].find_one(
        {"novel_id": to_object_id(novel_id), "chapter_id": to_object_id(chapter_id)},
        session=session,
    )
    revision = int((existing or {}).get("revision") or 0) + 1
    await db[collections.CHAPTER_STATE_DELTAS].update_one(
        {"novel_id": to_object_id(novel_id), "chapter_id": to_object_id(chapter_id)},
        {"$set": {
            "accepted_delta": deepcopy(payload),
            "evaluation": deepcopy(evaluation or {}),
            "revision": revision,
            "stale": False,
            "updated_at": now,
            "is_deleted": False,
        }, "$setOnInsert": {"created_at": now}},
        upsert=True,
        session=session,
    )
    superseded_by = f"accept:{chapter_id}:{revision}"
    await db[collections.PLOT_THREAD_EVENTS].update_many(
        {
            "novel_id": to_object_id(novel_id),
            "chapter_id": to_object_id(chapter_id),
            "event_type": "status_changed",
            "is_deleted": False,
            "superseded_by": None,
            "$or": [
                {"source_operation": "accept_chapter_state"},
                {"source_operation": {"$exists": False}},
            ],
        },
        {"$set": {"superseded_by": superseded_by, "superseded_at": now}},
        session=session,
    )
    for update in payload.get("character_updates") or []:
        state = await character_state_repo.get_state(
            novel_id, str(update["card_id"]), session=session
        )
        if state is None:
            continue
        await db[collections.CHARACTER_STATE_SNAPSHOTS].update_one(
            {
                "novel_id": to_object_id(novel_id),
                "chapter_id": to_object_id(chapter_id),
                "card_id": to_object_id(update["card_id"]),
            },
            {"$set": {
                "current_state": state.get("current_state", ""),
                "permanent_facts": deepcopy(state.get("permanent_facts") or []),
                "delta_revision": revision,
                "stale": False,
                "updated_at": now,
                "is_deleted": False,
            }, "$setOnInsert": {"created_at": now}},
            upsert=True,
            session=session,
        )
    for update in payload.get("accepted_thread_updates") or []:
        key = (
            f"accept:{chapter_id}:{revision}:"
            f"{update['thread_id']}:{update['status']}"
        )
        await db[collections.PLOT_THREAD_EVENTS].update_one(
            {"novel_id": to_object_id(novel_id), "idempotency_key": key},
            {"$setOnInsert": {
                "chapter_id": to_object_id(chapter_id),
                "thread_id": to_object_id(update["thread_id"]),
                "event_type": "status_changed",
                "status": update["status"],
                "source_operation": "accept_chapter_state",
                "source_operation_id": superseded_by,
                "source_revision": revision,
                "superseded_by": None,
                "created_at": now,
                "updated_at": now,
                "is_deleted": False,
            }},
            upsert=True,
            session=session,
        )
    return revision


async def record_manual_correction(
    novel_id: str,
    effective_chapter_id: str,
    correction_type: str,
    subject_id: str,
    fields: dict[str, Any],
    *,
    baseline: dict[str, Any] | None = None,
    source_operation: str = "manual_correction",
    source_operation_id: str | None = None,
    source_revision: int = 1,
    session: AsyncClientSession | None = None,
) -> None:
    key = _digest({
        "chapter": effective_chapter_id,
        "type": correction_type,
        "subject": subject_id,
        "fields": fields,
    })
    now = get_utc_now()
    await get_database()[collections.MANUAL_CORRECTIONS].update_one(
        {"novel_id": to_object_id(novel_id), "idempotency_key": key},
        {"$setOnInsert": {
            "effective_chapter_id": to_object_id(effective_chapter_id),
            "correction_type": correction_type,
            "subject_id": str(subject_id),
            "fields": deepcopy(fields),
            "baseline": deepcopy(baseline),
            "source_operation": source_operation,
            "source_operation_id": source_operation_id,
            "source_revision": source_revision,
            "priority": 100,
            "created_at": now,
            "updated_at": now,
            "is_deleted": False,
        }},
        upsert=True,
        session=session,
    )
async def latest_chapter_id(novel_id: str) -> str:
    volumes = await volume_repo.get_volumes_by_novel(novel_id)
    chapters = await chapter_repo.get_chapters_by_novel(novel_id)
    timeline = ChapterTimeline(volumes, chapters)
    if not timeline.positions:
        raise ValueError("Novel has no active chapter for an effective correction position")
    return timeline.positions[-1].chapter_id


async def record_plot_thread_event(
    novel_id: str,
    chapter_id: str,
    thread_id: str,
    event_type: str,
    fields: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    source_operation: str = "plot_thread_event",
    source_operation_id: str | None = None,
    source_revision: int = 1,
    session: AsyncClientSession | None = None,
) -> None:
    key = idempotency_key or _digest({
        "chapter": chapter_id,
        "thread": thread_id,
        "event": event_type,
        "fields": fields,
    })
    now = get_utc_now()
    await get_database()[collections.PLOT_THREAD_EVENTS].update_one(
        {"novel_id": to_object_id(novel_id), "idempotency_key": key},
        {"$setOnInsert": {
            "chapter_id": to_object_id(chapter_id),
            "thread_id": to_object_id(thread_id),
            "event_type": event_type,
            "fields": deepcopy(fields),
            "source_operation": source_operation,
            "source_operation_id": source_operation_id,
            "source_revision": source_revision,
            "superseded_by": None,
            "created_at": now,
            "updated_at": now,
            "is_deleted": False,
        }},
        upsert=True,
        session=session,
    )


async def record_chapter_tombstone(
    chapter: dict[str, Any],
    *,
    session: AsyncClientSession | None = None,
) -> None:
    """硬删前保存定位与内容摘要，使后续重放不依赖已不存在的章节。"""
    now = get_utc_now()
    await get_database()[collections.CHAPTER_STATE_DELTAS].update_one(
        {
            "novel_id": chapter["novel_id"],
            "chapter_id": chapter["_id"],
        },
        {"$set": {
            "tombstone": {
                "chapter_id": chapter["_id"],
                "volume_id": chapter.get("volume_id"),
                "order_index": chapter.get("order_index"),
                "title": chapter.get("title", ""),
                "content_digest": _content_digest(chapter),
                "deleted_at": now,
            },
            "stale": True,
            "updated_at": now,
            "is_deleted": False,
        }, "$setOnInsert": {
            "revision": 0,
            "accepted_delta": None,
            "created_at": now,
        }},
        upsert=True,
        session=session,
    )
