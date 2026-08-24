"""Deterministic, on-demand narrative history projection.

Canonical inputs are current chapter-state deltas, manual corrections, and
plot-thread events.  Cached snapshots and current materialized documents are
not used to decide historical state.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pymongo.asynchronous.client_session import AsyncClientSession

from backend.db import collections
from backend.db.mongo import get_database
from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.repositories.plot_thread_repository import ACTIVE_THREAD_STATUSES
from backend.db.repositories.volume_repository import volume_repo
from backend.db.utils import get_utc_now, to_object_id
from backend.services.novel.chapter_timeline import ChapterTimeline


_EVENT_PRIORITY = {
    "plot_thread_planted": 10,
    "chapter_delta": 20,
    "plot_thread_event": 30,
    "manual_correction": 40,
    "chapter_tombstone": 50,
}


def _datetime_key(value: Any) -> str:
    if not isinstance(value, datetime):
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _jsonable(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return _datetime_key(value)
    return str(value) if value.__class__.__name__ == "ObjectId" else value


def _projection_digest(value: Any) -> str:
    encoded = json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class NarrativeEvent:
    event_id: str
    novel_id: str
    chapter_id: str
    book_ordinal: int
    kind: str
    subject_id: str
    source_operation: str
    source_revision: int
    payload: dict[str, Any]
    created_at: Any = None

    @property
    def sort_key(self) -> tuple[int, int, int, str, str]:
        return (
            self.book_ordinal,
            _EVENT_PRIORITY.get(self.kind, 99),
            self.source_revision,
            _datetime_key(self.created_at),
            self.event_id,
        )


@dataclass(frozen=True)
class NarrativeProjection:
    novel_id: str
    target_chapter_id: str
    events: tuple[NarrativeEvent, ...]
    states: dict[str, dict[str, Any]]
    threads: dict[str, dict[str, Any]]
    states_tracked: bool
    threads_tracked: bool
    digest: str

    @property
    def tracked(self) -> bool:
        return self.states_tracked or self.threads_tracked

    @property
    def active_threads(self) -> list[dict[str, Any]]:
        active = [
            deepcopy(thread)
            for thread in self.threads.values()
            if not thread.get("is_deleted")
            and thread.get("status") in ACTIVE_THREAD_STATUSES
        ]
        active.sort(
            key=lambda item: (
                item.get("due_chapter_order") is None,
                item.get("due_chapter_order") or 0,
                item.get("name") or "",
                item.get("_id") or "",
            )
        )
        return active

    def state_documents(self) -> list[dict[str, Any]]:
        return [deepcopy(value) for _, value in sorted(self.states.items())]


class NarrativeTimeline:
    async def _load(
        self,
        novel_id: str,
        *,
        session: AsyncClientSession | None = None,
    ) -> tuple[ChapterTimeline, list[NarrativeEvent], bool, bool]:
        volumes = await volume_repo.get_volumes_by_novel(novel_id, session=session)
        chapters = await chapter_repo.get_chapters_by_novel(novel_id, session=session)
        timeline = ChapterTimeline(volumes, chapters)
        positions = {position.chapter_id: position for position in timeline.positions}
        database = get_database()
        novel_oid = to_object_id(novel_id)

        deltas = await database[collections.CHAPTER_STATE_DELTAS].find(
            {
                "novel_id": novel_oid,
                "accepted_delta": {"$ne": None},
                "is_deleted": False,
            },
            session=session,
        ).to_list(length=None)
        corrections = await database[collections.MANUAL_CORRECTIONS].find(
            {"novel_id": novel_oid, "is_deleted": False},
            session=session,
        ).to_list(length=None)
        thread_events = await database[collections.PLOT_THREAD_EVENTS].find(
            {
                "novel_id": novel_oid,
                "is_deleted": False,
                "superseded_by": {"$in": [None, ""]},
            },
            session=session,
        ).to_list(length=None)
        thread_docs = await database[collections.PLOT_THREADS].find(
            {"novel_id": novel_oid},
            session=session,
        ).to_list(length=None)
        thread_by_id = {str(item["_id"]): item for item in thread_docs}

        events: list[NarrativeEvent] = []
        for delta in deltas:
            chapter_id = str(delta.get("chapter_id"))
            position = positions.get(chapter_id)
            if position is None:
                continue
            revision = int(delta.get("revision") or 0)
            events.append(
                NarrativeEvent(
                    event_id=f"delta:{chapter_id}:{revision}",
                    novel_id=str(novel_id),
                    chapter_id=chapter_id,
                    book_ordinal=position.book_ordinal,
                    kind="chapter_delta",
                    subject_id=chapter_id,
                    source_operation="accept_chapter_state",
                    source_revision=revision,
                    payload=deepcopy(delta.get("accepted_delta") or {}),
                    created_at=delta.get("updated_at") or delta.get("created_at"),
                )
            )

        for correction in corrections:
            chapter_id = str(correction.get("effective_chapter_id"))
            position = positions.get(chapter_id)
            if position is None:
                continue
            events.append(
                NarrativeEvent(
                    event_id=f"correction:{correction['_id']}",
                    novel_id=str(novel_id),
                    chapter_id=chapter_id,
                    book_ordinal=position.book_ordinal,
                    kind="manual_correction",
                    subject_id=str(correction.get("subject_id") or ""),
                    source_operation=str(
                        correction.get("source_operation") or "manual_correction"
                    ),
                    source_revision=int(correction.get("source_revision") or 1),
                    payload={
                        "correction_type": correction.get("correction_type"),
                        "fields": deepcopy(correction.get("fields") or {}),
                        "baseline": deepcopy(correction.get("baseline")),
                    },
                    created_at=correction.get("created_at"),
                )
            )

        recorded_plants: set[str] = set()
        for stored in thread_events:
            chapter_id = str(stored.get("chapter_id"))
            position = positions.get(chapter_id)
            if position is None:
                continue
            event_type = str(stored.get("event_type") or "")
            # Accepted status changes are canonical in the current chapter delta.
            if event_type == "status_changed":
                continue
            thread_id = str(stored.get("thread_id") or "")
            if not thread_id:
                continue
            if event_type == "planted":
                recorded_plants.add(thread_id)
            fields = deepcopy(stored.get("fields") or {})
            snapshot = fields.get("thread")
            if not isinstance(snapshot, dict):
                current = thread_by_id.get(thread_id) or {}
                snapshot = {
                    "_id": thread_id,
                    "name": current.get("name", ""),
                    "description": current.get("description", ""),
                    "status": "planted",
                    "importance": current.get("importance", "sub"),
                    "due_chapter_order": current.get("due_chapter_order"),
                    "is_deleted": False,
                }
            events.append(
                NarrativeEvent(
                    event_id=str(stored.get("idempotency_key") or stored["_id"]),
                    novel_id=str(novel_id),
                    chapter_id=chapter_id,
                    book_ordinal=position.book_ordinal,
                    kind=(
                        "plot_thread_planted"
                        if event_type == "planted"
                        else "plot_thread_event"
                    ),
                    subject_id=thread_id,
                    source_operation=str(
                        stored.get("source_operation") or "plot_thread_event"
                    ),
                    source_revision=int(stored.get("source_revision") or 1),
                    payload={"event_type": event_type, "fields": fields, "thread": snapshot},
                    created_at=stored.get("created_at"),
                )
            )

        # Stable chapter identities allow a conservative baseline for pre-event data.
        for thread_id, current in thread_by_id.items():
            if thread_id in recorded_plants:
                continue
            planted_chapter_id = current.get("planted_chapter_id")
            position = positions.get(str(planted_chapter_id)) if planted_chapter_id else None
            if position is None:
                continue
            events.append(
                NarrativeEvent(
                    event_id=f"thread-baseline:{thread_id}:{position.chapter_id}",
                    novel_id=str(novel_id),
                    chapter_id=position.chapter_id,
                    book_ordinal=position.book_ordinal,
                    kind="plot_thread_planted",
                    subject_id=thread_id,
                    source_operation="legacy_thread_baseline",
                    source_revision=0,
                    payload={
                        "event_type": "planted",
                        "fields": {},
                        "thread": {
                            "_id": thread_id,
                            "name": current.get("name", ""),
                            "description": current.get("description", ""),
                            "status": "planted",
                            "importance": current.get("importance", "sub"),
                            "due_chapter_order": current.get("due_chapter_order"),
                            "is_deleted": False,
                        },
                    },
                    created_at=current.get("created_at"),
                )
            )

        events.sort(key=lambda item: item.sort_key)
        states_tracked = bool(deltas) or any(
            event.kind == "manual_correction"
            and event.payload.get("correction_type") != "plot_thread"
            for event in events
        )
        threads_tracked = any(
            event.kind in {"plot_thread_planted", "plot_thread_event"}
            or (
                event.kind == "manual_correction"
                and event.payload.get("correction_type") == "plot_thread"
            )
            or (
                event.kind == "chapter_delta"
                and event.payload.get("accepted_thread_updates")
            )
            for event in events
        )
        return timeline, events, states_tracked, threads_tracked

    def _project(
        self,
        novel_id: str,
        target_chapter_id: str,
        events: tuple[NarrativeEvent, ...],
        *,
        states_tracked: bool,
        threads_tracked: bool,
    ) -> NarrativeProjection:
        states: dict[str, dict[str, Any]] = {}
        threads: dict[str, dict[str, Any]] = {}
        for event in events:
            if event.kind == "chapter_delta":
                for update in event.payload.get("character_updates") or []:
                    card_id = str(update["card_id"])
                    state = states.setdefault(
                        card_id,
                        {
                            "card_id": card_id,
                            "current_state": str(
                                update.get("prior_current_state") or ""
                            ),
                            "permanent_facts": [],
                            "as_of_chapter_id": update.get(
                                "prior_as_of_chapter_id"
                            ),
                            "as_of_chapter_order": int(
                                update.get("prior_as_of_chapter_order") or 0
                            ),
                        },
                    )
                    if update.get("write_current_state", True):
                        state["current_state"] = str(
                            update.get("current_state") or ""
                        )
                        state["as_of_chapter_id"] = event.chapter_id
                        state.pop("as_of_chapter_order", None)
                    known = {
                        str(fact.get("id")): fact
                        for fact in state["permanent_facts"]
                        if fact.get("id") is not None
                    }
                    for index, raw_fact in enumerate(
                        update.get("accepted_permanent_facts") or []
                    ):
                        fact = deepcopy(raw_fact)
                        fact_id = str(
                            fact.get("id")
                            or hashlib.sha256(
                                (
                                    f"{event.chapter_id}:{card_id}:{index}:"
                                    f"{fact.get('fact', '')}"
                                ).encode("utf-8")
                            ).hexdigest()[:24]
                        )
                        fact.update(
                            {
                                "id": fact_id,
                                "source_chapter_id": event.chapter_id,
                            }
                        )
                        if fact_id in known:
                            known[fact_id].update(fact)
                        else:
                            state["permanent_facts"].append(fact)
                            known[fact_id] = fact
                for update in event.payload.get("accepted_thread_updates") or []:
                    thread_id = str(update["thread_id"])
                    thread = threads.setdefault(
                        thread_id,
                        {"_id": thread_id, "status": "planted", "is_deleted": False},
                    )
                    thread["status"] = str(update["status"])
                    thread["as_of_chapter_id"] = event.chapter_id
                continue

            if event.kind == "plot_thread_planted":
                snapshot = deepcopy(event.payload.get("thread") or {})
                snapshot.setdefault("_id", event.subject_id)
                snapshot.setdefault("status", "planted")
                snapshot.setdefault("is_deleted", False)
                snapshot["planted_chapter_id"] = event.chapter_id
                threads[event.subject_id] = snapshot
                continue

            if event.kind == "plot_thread_event":
                thread = threads.setdefault(
                    event.subject_id,
                    {"_id": event.subject_id, "status": "planted", "is_deleted": False},
                )
                event_type = str(event.payload.get("event_type") or "")
                fields = deepcopy(event.payload.get("fields") or {})
                if event_type == "soft_deleted":
                    thread["is_deleted"] = True
                elif event_type == "restored":
                    thread["is_deleted"] = False
                thread.update({key: value for key, value in fields.items() if key != "thread"})
                continue

            if event.kind != "manual_correction":
                continue
            correction_type = str(event.payload.get("correction_type") or "")
            fields = deepcopy(event.payload.get("fields") or {})
            baseline = deepcopy(event.payload.get("baseline"))
            if isinstance(baseline, dict):
                if correction_type == "plot_thread":
                    baseline_id = str(
                        baseline.get("_id") or event.subject_id
                    )
                    if baseline_id not in threads:
                        baseline["_id"] = baseline_id
                        baseline["planted_chapter_id"] = event.chapter_id
                        threads[baseline_id] = baseline
                else:
                    baseline_card_id = str(
                        baseline.get("card_id")
                        or fields.get("card_id")
                        or event.subject_id
                    )
                    if baseline_card_id not in states:
                        baseline_facts = []
                        for raw_fact in baseline.get("permanent_facts") or []:
                            fact = deepcopy(raw_fact)
                            if fact.get("id") is not None:
                                fact["id"] = str(fact["id"])
                            if fact.get("source_chapter_id") is not None:
                                fact["source_chapter_id"] = str(
                                    fact["source_chapter_id"]
                                )
                            baseline_facts.append(fact)
                        states[baseline_card_id] = {
                            "card_id": baseline_card_id,
                            "current_state": str(
                                baseline.get("current_state") or ""
                            ),
                            "permanent_facts": baseline_facts,
                            "as_of_chapter_id": event.chapter_id,
                        }
            if correction_type == "character_current_state":
                state = states.setdefault(
                    event.subject_id,
                    {
                        "card_id": event.subject_id,
                        "current_state": "",
                        "permanent_facts": [],
                    },
                )
                state["current_state"] = str(fields.get("current_state") or "")
                state["as_of_chapter_id"] = event.chapter_id
            elif correction_type in {"permanent_fact", "permanent_fact_delete"}:
                for state in states.values():
                    for fact in list(state.get("permanent_facts") or []):
                        if str(fact.get("id")) != event.subject_id:
                            continue
                        if correction_type == "permanent_fact_delete":
                            state["permanent_facts"].remove(fact)
                        else:
                            for key in ("fact", "kind"):
                                if key in fields:
                                    fact[key] = fields[key]
                        break
            elif correction_type == "plot_thread":
                thread = threads.setdefault(
                    event.subject_id,
                    {"_id": event.subject_id, "status": "planted", "is_deleted": False},
                )
                thread.update(fields)

        digest = _projection_digest(
            {
                "novel_id": novel_id,
                "target_chapter_id": target_chapter_id,
                "events": [event.event_id for event in events],
                "states": states,
                "threads": threads,
            }
        )
        return NarrativeProjection(
            novel_id=str(novel_id),
            target_chapter_id=str(target_chapter_id),
            events=events,
            states=states,
            threads=threads,
            states_tracked=states_tracked,
            threads_tracked=threads_tracked,
            digest=digest,
        )

    async def context_before(
        self, novel_id: str, chapter_id: str
    ) -> NarrativeProjection:
        timeline, all_events, states_tracked, threads_tracked = await self._load(
            novel_id
        )
        target = timeline.position(chapter_id)
        events = tuple(
            event
            for event in all_events
            if event.book_ordinal < target.book_ordinal
        )
        return self._project(
            novel_id,
            chapter_id,
            events,
            states_tracked=states_tracked,
            threads_tracked=threads_tracked,
        )

    async def refresh(
        self,
        novel_id: str,
        *,
        session: AsyncClientSession | None = None,
    ) -> dict[str, Any]:
        """Rebuild current materialized views from canonical narrative events."""
        timeline, events, states_tracked, threads_tracked = await self._load(
            novel_id, session=session
        )
        projection = self._project(
            novel_id,
            "__current__",
            tuple(events),
            states_tracked=states_tracked,
            threads_tracked=threads_tracked,
        )
        database = get_database()
        novel_oid = to_object_id(novel_id)
        now = get_utc_now()

        state_ids: set[Any] = set()
        states_refreshed = 0
        for card_id, state in projection.states.items():
            card_oid = to_object_id(card_id)
            state_ids.add(card_oid)
            as_of_chapter_id = str(state.get("as_of_chapter_id") or "")
            as_of_position = (
                timeline.position(as_of_chapter_id) if as_of_chapter_id else None
            )
            prior_as_of_chapter_order = int(
                state.get("as_of_chapter_order") or 0
            )
            facts = []
            for raw_fact in state.get("permanent_facts") or []:
                fact = deepcopy(raw_fact)
                fact["id"] = to_object_id(str(fact["id"]))
                source_id = str(fact.get("source_chapter_id") or "")
                if source_id:
                    source_position = timeline.position(source_id)
                    fact["source_chapter_id"] = to_object_id(source_id)
                    fact["source_status"] = "tracked"
                    fact["chapter_order"] = source_position.chapter_order
                facts.append(fact)
            result = await database[collections.CHARACTER_STATES].update_one(
                {"novel_id": novel_oid, "card_id": card_oid},
                {
                    "$set": {
                        "current_state": str(state.get("current_state") or ""),
                        "permanent_facts": facts,
                        "as_of_chapter_id": (
                            to_object_id(as_of_chapter_id)
                            if as_of_chapter_id
                            else None
                        ),
                        "as_of_chapter_order": (
                            as_of_position.chapter_order
                            if as_of_position
                            else prior_as_of_chapter_order
                        ),
                        "history_status": "tracked",
                        "is_deleted": False,
                        "deleted_at": None,
                        "updated_at": now,
                    },
                    "$setOnInsert": {"created_at": now},
                },
                upsert=True,
                session=session,
            )
            states_refreshed += int(
                result.matched_count > 0 or result.upserted_id is not None
            )

        ghost_query: dict[str, Any] = {
            "novel_id": novel_oid,
            "history_status": "tracked",
            "is_deleted": False,
        }
        if state_ids:
            ghost_query["card_id"] = {"$nin": list(state_ids)}
        ghost_result = await database[collections.CHARACTER_STATES].update_many(
            ghost_query,
            {
                "$set": {
                    "current_state": "",
                    "permanent_facts": [],
                    "is_deleted": True,
                    "deleted_at": now,
                    "updated_at": now,
                }
            },
            session=session,
        )

        threads_refreshed = 0
        for thread_id, raw_thread in projection.threads.items():
            fields = {
                key: deepcopy(value)
                for key, value in raw_thread.items()
                if key
                in {
                    "name",
                    "description",
                    "status",
                    "importance",
                    "planted_chapter_order",
                    "due_chapter_order",
                    "resolved_chapter_order",
                    "planted_chapter_id",
                    "due_target",
                    "resolved_chapter_id",
                    "notes",
                    "source",
                    "is_deleted",
                    "deleted_at",
                }
            }
            for key in ("planted_chapter_id", "resolved_chapter_id"):
                if key in fields:
                    fields[key] = (
                        to_object_id(str(fields[key])) if fields[key] else None
                    )
            due_target = fields.get("due_target")
            if isinstance(due_target, dict) and due_target.get("kind") == "chapter":
                fields["due_target"] = {
                    **due_target,
                    "chapter_id": to_object_id(str(due_target["chapter_id"])),
                }
            fields["updated_at"] = now
            result = await database[collections.PLOT_THREADS].update_one(
                {"novel_id": novel_oid, "_id": to_object_id(thread_id)},
                {"$set": fields},
                session=session,
            )
            threads_refreshed += int(result.matched_count > 0)

        if events:
            await database[collections.CHAPTER_STATE_DELTAS].update_many(
                {"novel_id": novel_oid, "is_deleted": False},
                {"$set": {"stale": False, "updated_at": now}},
                session=session,
            )
        return {
            "novel_id": str(novel_id),
            "digest": projection.digest,
            "states_refreshed": states_refreshed,
            "states_removed": int(ghost_result.modified_count),
            "threads_refreshed": threads_refreshed,
            "requires_model": False,
        }

    async def audit(self, novel_id: str) -> dict[str, Any]:
        """Report canonical coverage and advisory cache state without mutation."""
        timeline, events, states_tracked, threads_tracked = await self._load(novel_id)
        projection = self._project(
            novel_id,
            "__current__",
            tuple(events),
            states_tracked=states_tracked,
            threads_tracked=threads_tracked,
        )
        database = get_database()
        novel_oid = to_object_id(novel_id)
        materialized = await database[collections.CHARACTER_STATES].find(
            {"novel_id": novel_oid, "is_deleted": False}
        ).to_list(length=None)
        projected_ids = set(projection.states)
        stale_deltas = await database[collections.CHAPTER_STATE_DELTAS].find(
            {"novel_id": novel_oid, "is_deleted": False, "stale": True},
            projection={"chapter_id": 1},
        ).to_list(length=None)
        active_ids = {position.chapter_id for position in timeline.positions}
        return {
            "novel_id": str(novel_id),
            "active_chapter_count": len(timeline.positions),
            "event_count": len(events),
            "delta_count": sum(event.kind == "chapter_delta" for event in events),
            "legacy_unknown_state_count": sum(
                str(state.get("card_id")) not in projected_ids
                for state in materialized
            ),
            "stale_chapter_ids": sorted(
                str(item["chapter_id"])
                for item in stale_deltas
                if str(item.get("chapter_id")) in active_ids
            ),
            "projection_digest": projection.digest,
            "states_tracked": states_tracked,
            "threads_tracked": threads_tracked,
            "requires_model": False,
            "estimated_tokens": 0,
        }


narrative_timeline = NarrativeTimeline()
