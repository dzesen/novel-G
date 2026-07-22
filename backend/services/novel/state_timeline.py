"""章节状态预览、接受增量、快照与人工订正的可回放时间线。"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.asynchronous.client_session import AsyncClientSession

from backend.config.config import CONFIG_PATH
from backend.config.lifecycle import FileSecretVersionStore
from backend.db import collections
from backend.db.mongo import get_database
from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.repositories.character_state_repository import character_state_repo
from backend.db.repositories.plot_thread_repository import plot_thread_repo
from backend.db.repositories.volume_repository import volume_repo
from backend.db.utils import get_utc_now, to_object_id
from backend.db.transaction import run_mongo_write_unit
from backend.services.novel.chapter_timeline import ChapterTimeline


PREVIEW_TTL_SECONDS = 15 * 60


class StaleStatePreview(ValueError):
    """状态候选过期、重复使用，或绑定的正文/状态已变化。"""


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (ObjectId, datetime)):
        return str(value)
    return value


def _digest(value: Any) -> str:
    encoded = json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _preview_key() -> bytes:
    store = FileSecretVersionStore(
        Path(CONFIG_PATH).with_name(".config-secret-versions.json")
    )
    return store.derive_key("chapter-state-preview")


def _content_digest(chapter: dict[str, Any]) -> str:
    return _digest({
        "chapter_id": str(chapter.get("_id")),
        "content": str(chapter.get("content") or ""),
        "updated_at": chapter.get("updated_at"),
    })


async def _state_revision(novel_id: str) -> str:
    states = await character_state_repo.list_states(novel_id)
    threads = await plot_thread_repo.list_threads(novel_id)
    return _digest({"states": states, "threads": threads})


def add_selection_ids(candidate: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(candidate)
    for character in result.get("character_updates") or []:
        character["selection_id"] = uuid4().hex
        for fact in character.get("new_permanent_facts") or []:
            fact["selection_id"] = uuid4().hex
    for thread in result.get("thread_updates") or []:
        thread["selection_id"] = uuid4().hex
    return result


class StatePreviewStore:
    @property
    def collection(self):
        return get_database()[collections.STATE_PREVIEWS]

    async def create(
        self,
        novel_id: str,
        chapter_id: str,
        candidate: dict[str, Any],
    ) -> dict[str, Any]:
        chapter = await chapter_repo.get_chapter_by_id(chapter_id)
        if str(chapter.get("novel_id")) != str(novel_id):
            raise ValueError("Chapter does not belong to novel")
        prepared = add_selection_ids(candidate)
        preview_id = ObjectId()
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=PREVIEW_TTL_SECONDS)
        content_digest = _content_digest(chapter)
        state_revision = await _state_revision(novel_id)
        candidate_digest = _digest(prepared)
        token_payload = f"{preview_id}:{content_digest}:{state_revision}:{candidate_digest}:{int(expires_at.timestamp())}"
        token = hmac.new(_preview_key(), token_payload.encode("utf-8"), hashlib.sha256).hexdigest()
        await self.collection.insert_one({
            "_id": preview_id,
            "novel_id": to_object_id(novel_id),
            "chapter_id": to_object_id(chapter_id),
            "candidate": prepared,
            "content_digest": content_digest,
            "state_revision": state_revision,
            "candidate_digest": candidate_digest,
            "token_digest": hashlib.sha256(token.encode("ascii")).hexdigest(),
            "expires_at": expires_at,
            "used_at": None,
            "created_at": get_utc_now(),
            "is_deleted": False,
        })
        return {
            **prepared,
            "preview_id": str(preview_id),
            "acceptance_token": token,
            "preview_expires_at": expires_at.isoformat(),
        }

    async def consume(
        self,
        *,
        chapter_id: str,
        preview_id: str,
        acceptance_token: str,
        selected_fact_ids: list[str],
        selected_thread_ids: list[str],
        edits: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        preview = await self.collection.find_one({"_id": to_object_id(preview_id)})
        if not preview or preview.get("used_at") is not None:
            raise StaleStatePreview("State preview is missing or has already been accepted")
        now = datetime.now(timezone.utc)
        expires_at = preview.get("expires_at")
        if isinstance(expires_at, datetime) and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if not isinstance(expires_at, datetime) or expires_at <= now:
            raise StaleStatePreview("State preview has expired")
        if str(preview.get("chapter_id")) != str(chapter_id):
            raise StaleStatePreview("State preview belongs to another chapter")
        expected = hashlib.sha256(acceptance_token.encode("ascii")).hexdigest()
        if not hmac.compare_digest(expected, str(preview.get("token_digest") or "")):
            raise StaleStatePreview("State preview token is invalid")
        chapter = await chapter_repo.get_chapter_by_id(chapter_id)
        if _content_digest(chapter) != preview.get("content_digest"):
            raise StaleStatePreview("Chapter content changed after state generation")
        novel_id = str(preview["novel_id"])
        if await _state_revision(novel_id) != preview.get("state_revision"):
            raise StaleStatePreview("Narrative state changed after state generation")

        candidate = deepcopy(preview["candidate"])
        facts_by_id = {
            fact["selection_id"]: (character, fact)
            for character in candidate.get("character_updates") or []
            for fact in character.get("new_permanent_facts") or []
        }
        threads_by_id = {
            thread["selection_id"]: thread
            for thread in candidate.get("thread_updates") or []
        }
        if not set(selected_fact_ids).issubset(facts_by_id):
            raise StaleStatePreview("Unknown permanent-fact selection id")
        if not set(selected_thread_ids).issubset(threads_by_id):
            raise StaleStatePreview("Unknown plot-thread selection id")

        allowed_edits = edits or {}
        unknown_edits = set(allowed_edits) - {"summary", "current_states"}
        if unknown_edits:
            raise StaleStatePreview(f"Unsupported preview edits: {sorted(unknown_edits)}")
        current_state_edits = allowed_edits.get("current_states") or {}
        payload = {
            "summary": str(allowed_edits.get("summary", candidate.get("summary") or "")),
            "character_updates": [],
            "accepted_thread_updates": [],
        }
        for character in candidate.get("character_updates") or []:
            card_id = str(character["card_id"])
            selected = [
                {key: value for key, value in fact.items() if key != "selection_id"}
                for fact in character.get("new_permanent_facts") or []
                if fact["selection_id"] in selected_fact_ids
            ]
            payload["character_updates"].append({
                "card_id": card_id,
                "current_state": str(current_state_edits.get(card_id, character.get("current_state") or "")),
                "accepted_permanent_facts": selected,
            })
        payload["accepted_thread_updates"] = [
            {"thread_id": str(thread["thread_id"]), "status": thread["status"]}
            for thread in candidate.get("thread_updates") or []
            if thread["selection_id"] in selected_thread_ids
        ]
        consumed = await self.collection.find_one_and_update(
            {"_id": preview["_id"], "used_at": None},
            {"$set": {"used_at": now}},
            return_document=ReturnDocument.AFTER,
        )
        if consumed is None:
            raise StaleStatePreview("State preview was accepted concurrently")
        return payload, {
            "novel_id": novel_id,
            "manual_edits": allowed_edits,
            "preview_id": preview_id,
            "candidate_digest": preview.get("candidate_digest"),
            "evidence": [
                {
                    "selection_id": thread.get("selection_id"),
                    "thread_id": str(thread.get("thread_id")),
                    "evidence": str(thread.get("evidence") or ""),
                    "selected": thread.get("selection_id") in selected_thread_ids,
                }
                for thread in candidate.get("thread_updates") or []
            ],
            "consistency_issues": deepcopy(candidate.get("consistency_issues") or []),
            "human_feedback": {
                "selected_fact_count": len(selected_fact_ids),
                "rejected_fact_count": max(0, len(facts_by_id) - len(selected_fact_ids)),
                "selected_thread_count": len(selected_thread_ids),
                "rejected_thread_count": max(0, len(threads_by_id) - len(selected_thread_ids)),
                "edited_fields": sorted(allowed_edits),
            },
            "confidence": "human_reviewed",
        }


state_preview_store = StatePreviewStore()


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
        key = f"accept:{chapter_id}:{update['thread_id']}:{update['status']}"
        await db[collections.PLOT_THREAD_EVENTS].update_one(
            {"novel_id": to_object_id(novel_id), "idempotency_key": key},
            {"$setOnInsert": {
                "chapter_id": to_object_id(chapter_id),
                "thread_id": to_object_id(update["thread_id"]),
                "event_type": "status_changed",
                "status": update["status"],
                "created_at": now,
                "updated_at": now,
                "is_deleted": False,
            }},
            upsert=True,
            session=session,
        )
    await mark_downstream_stale(novel_id, chapter_id, session=session)
    return revision


async def build_replay_plan(novel_id: str) -> dict[str, Any]:
    """生成纯本地确定性重放计划；不会触发任何模型调用。"""
    volumes = await volume_repo.get_volumes_by_novel(novel_id)
    chapters = await chapter_repo.get_chapters_by_novel(novel_id)
    timeline = ChapterTimeline(volumes, chapters)
    active_ids = {position.chapter_id for position in timeline.positions}
    deltas = await get_database()[collections.CHAPTER_STATE_DELTAS].find({
        "novel_id": to_object_id(novel_id),
        "is_deleted": False,
    }).to_list(length=None)
    stale_ids = sorted(
        (
            str(delta["chapter_id"])
            for delta in deltas
            if delta.get("stale") and str(delta.get("chapter_id")) in active_ids
        ),
        key=lambda chapter_id: timeline.position(chapter_id).book_ordinal,
    )
    states = await character_state_repo.list_states(novel_id)
    legacy_unknown = sum(1 for state in states if not state.get("as_of_chapter_id"))
    return {
        "novel_id": str(novel_id),
        "stale_chapter_ids": stale_ids,
        "delta_count": sum(
            1 for delta in deltas if str(delta.get("chapter_id")) in active_ids
        ),
        "legacy_unknown_state_count": legacy_unknown,
        "requires_model": False,
        "estimated_tokens": 0,
    }


def _stable_replay_fact_id(chapter_id: str, card_id: str, index: int, fact: dict[str, Any]) -> ObjectId:
    supplied = fact.get("id")
    if supplied:
        return to_object_id(str(supplied))
    digest = hashlib.sha256(
        f"{chapter_id}:{card_id}:{index}:{fact.get('fact', '')}".encode("utf-8")
    ).hexdigest()
    return ObjectId(digest[:24])


def _normalize_replayed_thread_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Restore the BSON shapes enforced by PlotThreadRepository writes."""
    normalized = deepcopy(fields)
    for key in ("planted_chapter_id", "resolved_chapter_id"):
        if key in normalized:
            normalized[key] = to_object_id(normalized[key]) if normalized[key] else None
    due_target = normalized.get("due_target")
    if isinstance(due_target, dict) and due_target.get("kind") == "chapter":
        normalized["due_target"] = {
            **due_target,
            "chapter_id": to_object_id(due_target.get("chapter_id")),
        }
        normalized["due_chapter_order"] = None
    elif isinstance(due_target, dict) and due_target.get("kind") == "planned_ordinal":
        normalized["due_target"] = {
            "kind": "planned_ordinal",
            "ordinal": int(due_target.get("ordinal")),
        }
        normalized["due_chapter_order"] = int(due_target.get("ordinal"))
    return normalized


async def rebuild_stale_projections(novel_id: str) -> dict[str, Any]:
    """从 accepted_delta + manual_correction 确定性重建快照与最新物化视图。"""
    started_at = time.perf_counter()
    volumes = await volume_repo.get_volumes_by_novel(novel_id)
    chapters = await chapter_repo.get_chapters_by_novel(novel_id)
    timeline = ChapterTimeline(volumes, chapters)
    position_by_id = {position.chapter_id: position for position in timeline.positions}
    database = get_database()
    deltas = await database[collections.CHAPTER_STATE_DELTAS].find({
        "novel_id": to_object_id(novel_id),
        "accepted_delta": {"$ne": None},
        "is_deleted": False,
    }).to_list(length=None)
    deltas = [delta for delta in deltas if str(delta["chapter_id"]) in position_by_id]
    deltas.sort(key=lambda delta: position_by_id[str(delta["chapter_id"])].book_ordinal)
    corrections = await database[collections.MANUAL_CORRECTIONS].find({
        "novel_id": to_object_id(novel_id), "is_deleted": False,
    }).to_list(length=None)
    corrections = [
        correction for correction in corrections
        if str(correction.get("effective_chapter_id")) in position_by_id
    ]
    corrections.sort(key=lambda correction: (
        position_by_id[str(correction["effective_chapter_id"])].book_ordinal,
        int(correction.get("priority") or 0),
        str(correction.get("created_at") or ""),
    ))
    thread_events = await database[collections.PLOT_THREAD_EVENTS].find({
        "novel_id": to_object_id(novel_id), "is_deleted": False,
    }).to_list(length=None)
    thread_events = [
        event for event in thread_events
        if str(event.get("chapter_id")) in position_by_id
    ]
    thread_events.sort(key=lambda event: (
        position_by_id[str(event["chapter_id"])].book_ordinal,
        str(event.get("created_at") or ""),
    ))

    async def _rebuild(session):
        projection: dict[str, dict[str, Any]] = {}
        correction_index = 0
        snapshots_rebuilt = 0
        for delta in deltas:
            chapter_id = str(delta["chapter_id"])
            ordinal = position_by_id[chapter_id].book_ordinal
            accepted = delta.get("accepted_delta") or {}
            for update in accepted.get("character_updates") or []:
                card_id = str(update["card_id"])
                state = projection.setdefault(card_id, {
                    "current_state": "", "permanent_facts": [],
                })
                state["current_state"] = str(update.get("current_state") or "")
                known_ids = {str(fact.get("id")) for fact in state["permanent_facts"]}
                for index, fact in enumerate(update.get("accepted_permanent_facts") or []):
                    fact_id = _stable_replay_fact_id(chapter_id, card_id, index, fact)
                    if str(fact_id) in known_ids:
                        continue
                    state["permanent_facts"].append({
                        **deepcopy(fact),
                        "id": fact_id,
                        "source_chapter_id": to_object_id(chapter_id),
                        "chapter_order": position_by_id[chapter_id].chapter_order,
                    })
                    known_ids.add(str(fact_id))

            while correction_index < len(corrections):
                correction = corrections[correction_index]
                correction_ordinal = position_by_id[
                    str(correction["effective_chapter_id"])
                ].book_ordinal
                if correction_ordinal > ordinal:
                    break
                kind = correction.get("correction_type")
                subject_id = str(correction.get("subject_id") or "")
                fields = deepcopy(correction.get("fields") or {})
                if kind == "character_current_state":
                    projection.setdefault(subject_id, {
                        "current_state": "", "permanent_facts": [],
                    })["current_state"] = str(fields.get("current_state") or "")
                elif kind in {"permanent_fact", "permanent_fact_delete"}:
                    for state in projection.values():
                        for fact in list(state["permanent_facts"]):
                            if str(fact.get("id")) != subject_id:
                                continue
                            if kind == "permanent_fact_delete":
                                state["permanent_facts"].remove(fact)
                            else:
                                for key in ("fact", "kind"):
                                    if key in fields:
                                        fact[key] = fields[key]
                            break
                correction_index += 1

            now = get_utc_now()
            for card_id, state in projection.items():
                await database[collections.CHARACTER_STATE_SNAPSHOTS].update_one(
                    {
                        "novel_id": to_object_id(novel_id),
                        "chapter_id": to_object_id(chapter_id),
                        "card_id": to_object_id(card_id),
                    },
                    {"$set": {
                        **deepcopy(state),
                        "delta_revision": int(delta.get("revision") or 0),
                        "stale": False,
                        "updated_at": now,
                        "is_deleted": False,
                    }, "$setOnInsert": {"created_at": now}},
                    upsert=True,
                    session=session,
                )
                snapshots_rebuilt += 1
            await database[collections.CHAPTER_STATE_DELTAS].update_one(
                {"_id": delta["_id"]},
                {"$set": {"stale": False, "updated_at": now}},
                session=session,
            )

        if deltas:
            last_chapter_id = str(deltas[-1]["chapter_id"])
            last_position = position_by_id[last_chapter_id]
            for card_id, state in projection.items():
                await character_state_repo.collection.update_one(
                    {
                        "novel_id": to_object_id(novel_id),
                        "card_id": to_object_id(card_id),
                        "is_deleted": False,
                    },
                    {"$set": {
                        **deepcopy(state),
                        "as_of_chapter_id": to_object_id(last_chapter_id),
                        "as_of_chapter_order": last_position.chapter_order,
                        "history_status": "tracked",
                        "updated_at": get_utc_now(),
                    }},
                    session=session,
                )
        thread_projection: dict[str, dict[str, Any]] = {}
        for event in thread_events:
            thread_id = str(event.get("thread_id") or "")
            if not thread_id:
                continue
            projected = thread_projection.setdefault(thread_id, {})
            event_type = str(event.get("event_type") or "")
            fields = deepcopy(event.get("fields") or {})
            if event_type == "planted":
                projected.update({"status": "planted", "is_deleted": False})
            elif event_type == "status_changed":
                projected["status"] = str(event.get("status") or fields.get("status") or "developing")
                projected["is_deleted"] = False
            elif event_type == "soft_deleted":
                projected["is_deleted"] = True
        for correction in corrections:
            if correction.get("correction_type") != "plot_thread":
                continue
            thread_id = str(correction.get("subject_id") or "")
            if thread_id:
                thread_projection.setdefault(thread_id, {}).update(
                    _normalize_replayed_thread_fields(correction.get("fields") or {})
                )
        threads_rebuilt = 0
        for thread_id, fields in thread_projection.items():
            result = await plot_thread_repo.collection.update_one(
                {"_id": to_object_id(thread_id), "novel_id": to_object_id(novel_id)},
                {"$set": {**fields, "updated_at": get_utc_now()}},
                session=session,
            )
            threads_rebuilt += int(result.matched_count > 0)
        return {
            "snapshots_rebuilt": snapshots_rebuilt,
            "threads_rebuilt": threads_rebuilt,
        }

    rebuilt = await run_mongo_write_unit(_rebuild, "rebuild_state_timeline")
    return {
        "novel_id": str(novel_id),
        **rebuilt,
        "delta_count": len(deltas),
        "requires_model": False,
        "tokens_used": 0,
        "elapsed_ms": int((time.perf_counter() - started_at) * 1000),
    }


async def mark_downstream_stale(
    novel_id: str,
    chapter_id: str,
    *,
    session: AsyncClientSession | None = None,
) -> None:
    volumes = await volume_repo.get_volumes_by_novel(novel_id, session=session)
    chapters = await chapter_repo.get_chapters_by_novel(novel_id, session=session)
    timeline = ChapterTimeline(volumes, chapters)
    target = timeline.position(chapter_id)
    later = [
        to_object_id(position.chapter_id)
        for position in timeline.positions
        if position.book_ordinal > target.book_ordinal
    ]
    if not later:
        return
    db = get_database()
    query = {"novel_id": to_object_id(novel_id), "chapter_id": {"$in": later}}
    await db[collections.CHAPTER_STATE_DELTAS].update_many(
        query, {"$set": {"stale": True, "updated_at": get_utc_now()}}, session=session
    )
    await db[collections.CHARACTER_STATE_SNAPSHOTS].update_many(
        query, {"$set": {"stale": True, "updated_at": get_utc_now()}}, session=session
    )


async def mark_from_chapter_stale(
    novel_id: str,
    chapter_id: str,
    *,
    session: AsyncClientSession | None = None,
) -> None:
    """把目标章及其后的投影标 stale，供卷/章位置重排使用。"""
    volumes = await volume_repo.get_volumes_by_novel(novel_id, session=session)
    chapters = await chapter_repo.get_chapters_by_novel(novel_id, session=session)
    timeline = ChapterTimeline(volumes, chapters)
    target = timeline.position(chapter_id)
    affected = [
        to_object_id(position.chapter_id)
        for position in timeline.positions
        if position.book_ordinal >= target.book_ordinal
    ]
    if not affected:
        return
    query = {
        "novel_id": to_object_id(novel_id),
        "chapter_id": {"$in": affected},
    }
    now = get_utc_now()
    db = get_database()
    await db[collections.CHAPTER_STATE_DELTAS].update_many(
        query, {"$set": {"stale": True, "updated_at": now}}, session=session
    )
    await db[collections.CHARACTER_STATE_SNAPSHOTS].update_many(
        query, {"$set": {"stale": True, "updated_at": now}}, session=session
    )


async def snapshot_before(novel_id: str, chapter_id: str) -> list[dict[str, Any]]:
    """返回严格早于目标章节的最新非 stale 人物快照。"""
    volumes = await volume_repo.get_volumes_by_novel(novel_id)
    chapters = await chapter_repo.get_chapters_by_novel(novel_id)
    timeline = ChapterTimeline(volumes, chapters)
    target = timeline.position(chapter_id)
    cursor = get_database()[collections.CHARACTER_STATE_SNAPSHOTS].find({
        "novel_id": to_object_id(novel_id),
        "stale": False,
        "is_deleted": False,
    })
    docs = await cursor.to_list(length=None)
    latest: dict[str, tuple[int, dict[str, Any]]] = {}
    for doc in docs:
        try:
            position = timeline.position(str(doc["chapter_id"]))
        except ValueError:
            continue
        if position.book_ordinal >= target.book_ordinal:
            continue
        card_id = str(doc["card_id"])
        if card_id not in latest or latest[card_id][0] < position.book_ordinal:
            latest[card_id] = (position.book_ordinal, doc)
    return [item[1] for item in latest.values()]


async def has_tracked_timeline(novel_id: str) -> bool:
    """区分“尚无时间线”与“时间线存在但目标前无有效快照”。"""
    return await get_database()[collections.CHAPTER_STATE_DELTAS].count_documents({
        "novel_id": to_object_id(novel_id),
        "accepted_delta": {"$ne": None},
        "is_deleted": False,
    }) > 0


async def record_manual_correction(
    novel_id: str,
    effective_chapter_id: str,
    correction_type: str,
    subject_id: str,
    fields: dict[str, Any],
    *,
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
            "priority": 100,
            "created_at": now,
            "updated_at": now,
            "is_deleted": False,
        }},
        upsert=True,
        session=session,
    )
    current_query = {
        "novel_id": to_object_id(novel_id),
        "chapter_id": to_object_id(effective_chapter_id),
        "is_deleted": False,
    }
    stale_update = {"$set": {"stale": True, "updated_at": now}}
    await get_database()[collections.CHAPTER_STATE_DELTAS].update_many(
        current_query,
        stale_update,
        session=session,
    )
    await get_database()[collections.CHARACTER_STATE_SNAPSHOTS].update_many(
        current_query,
        stale_update,
        session=session,
    )
    await mark_downstream_stale(
        novel_id, effective_chapter_id, session=session
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
