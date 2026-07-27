"""Persisted, user-owned prose drafts and segment checkpoints."""
from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import uuid4

from pymongo import ReturnDocument

from backend.db import collections
from backend.db.base import BaseRepository
from backend.db.errors import NotFoundError
from backend.db.utils import get_utc_now, to_object_id


class StaleProseRun(ValueError):
    """The requested revision or execution lease no longer owns the run."""


class ProseRunRepository(BaseRepository):
    def __init__(self) -> None:
        super().__init__(collections.PROSE_RUNS)

    async def create_run(self, document: dict[str, Any]) -> dict[str, Any]:
        owner_id = to_object_id(document["owner_id"])
        chapter_id = to_object_id(document["chapter_id"])
        await self.collection.update_many(
            {
                "owner_id": owner_id,
                "chapter_id": chapter_id,
                "status": {"$in": ["active", "incomplete", "complete"]},
                "is_deleted": False,
            },
            {
                "$set": {
                    "status": "superseded",
                    "updated_at": get_utc_now(),
                }
            },
        )
        prepared = {
            **document,
            "owner_id": owner_id,
            "novel_id": to_object_id(document["novel_id"]),
            "chapter_id": chapter_id,
            "revision": 1,
            "segments": [],
            "status": "active",
            "lease": None,
        }
        run_id = await self.insert_one(prepared)
        return await self.get_run(run_id, str(owner_id))

    async def get_run(self, run_id: str, owner_id: str) -> dict[str, Any]:
        document = await self.find_one(
            {
                "_id": to_object_id(run_id),
                "owner_id": to_object_id(owner_id),
            }
        )
        if document is None:
            raise NotFoundError(f"Prose run not found: {run_id}")
        return document

    async def find_active(
        self,
        *,
        chapter_id: str,
        owner_id: str,
    ) -> dict[str, Any] | None:
        documents = await self.find_many(
            {
                "chapter_id": to_object_id(chapter_id),
                "owner_id": to_object_id(owner_id),
                "status": {"$in": ["active", "incomplete", "complete"]},
            },
            limit=1,
            sort=[("updated_at", -1)],
        )
        return documents[0] if documents else None

    async def claim(
        self,
        *,
        run_id: str,
        owner_id: str,
        expected_revision: int,
        lease_seconds: int = 300,
    ) -> dict[str, Any]:
        now = get_utc_now()
        token = uuid4().hex
        document = await self.collection.find_one_and_update(
            {
                "_id": to_object_id(run_id),
                "owner_id": to_object_id(owner_id),
                "is_deleted": False,
                "revision": int(expected_revision),
                "status": {"$in": ["active", "incomplete", "complete"]},
                "$or": [
                    {"lease": None},
                    {"lease.expires_at": {"$lte": now}},
                ],
            },
            {
                "$set": {
                    "lease": {
                        "token": token,
                        "claimed_at": now,
                        "expires_at": now + timedelta(seconds=max(30, lease_seconds)),
                    },
                    "status": "active",
                    "updated_at": now,
                },
                "$inc": {"revision": 1},
            },
            return_document=ReturnDocument.AFTER,
        )
        if document is None:
            raise StaleProseRun("正文草稿已被其他页面继续或版本已经变化")
        return document

    async def append_segment(
        self,
        *,
        run_id: str,
        owner_id: str,
        lease_token: str,
        segment: dict[str, Any],
    ) -> dict[str, Any]:
        sequence = int(segment["sequence_index"])
        now = get_utc_now()
        base_query = {
            "_id": to_object_id(run_id),
            "owner_id": to_object_id(owner_id),
            "is_deleted": False,
            "lease.token": str(lease_token),
        }
        existing = await self.collection.find_one(
            {
                **base_query,
                "segments": {"$elemMatch": {"sequence_index": sequence}},
            },
            projection={"_id": 1},
        )
        update: dict[str, Any]
        array_filters = None
        if existing is not None:
            update = {
                "$set": {
                    "segments.$[segment]": dict(segment),
                    "status": (
                        "active"
                        if segment.get("status") == "completed"
                        else "incomplete"
                    ),
                    "updated_at": now,
                    "lease.expires_at": now + timedelta(minutes=5),
                },
                "$inc": {"revision": 1},
            }
            array_filters = [{"segment.sequence_index": sequence}]
        else:
            update = {
                "$push": {"segments": dict(segment)},
                "$inc": {"revision": 1},
                "$set": {
                    "status": (
                        "active"
                        if segment.get("status") == "completed"
                        else "incomplete"
                    ),
                    "updated_at": now,
                    "lease.expires_at": now + timedelta(minutes=5),
                },
            }
        document = await self.collection.find_one_and_update(
            {
                **base_query,
            },
            update,
            array_filters=array_filters,
            return_document=ReturnDocument.AFTER,
        )
        if document is None:
            raise StaleProseRun("正文分段执行租约已失效")
        return document

    async def finish(
        self,
        *,
        run_id: str,
        owner_id: str,
        lease_token: str,
        status: str,
        completion: dict[str, Any],
        assembled_text: str,
    ) -> dict[str, Any]:
        if status not in {"complete", "incomplete", "stale"}:
            raise ValueError(f"Unsupported prose run status: {status}")
        document = await self.collection.find_one_and_update(
            {
                "_id": to_object_id(run_id),
                "owner_id": to_object_id(owner_id),
                "is_deleted": False,
                "lease.token": str(lease_token),
            },
            {
                "$set": {
                    "status": status,
                    "completion": dict(completion),
                    "assembled_text": str(assembled_text),
                    "lease": None,
                    "updated_at": get_utc_now(),
                },
                "$inc": {"revision": 1},
            },
            return_document=ReturnDocument.AFTER,
        )
        if document is None:
            raise StaleProseRun("正文草稿执行租约已失效")
        return document

    async def mark_status(
        self,
        *,
        run_id: str,
        owner_id: str,
        status: str,
    ) -> bool:
        return await self.update_one(
            {
                "_id": to_object_id(run_id),
                "owner_id": to_object_id(owner_id),
            },
            {"status": status, "lease": None},
        )


prose_run_repo = ProseRunRepository()
