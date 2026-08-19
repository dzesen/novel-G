"""Monotonic, idempotent narrative revision storage for generation leases."""

from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Any

from pymongo import ReturnDocument

from backend.db import collections
from backend.db.mongo import get_database
from backend.db.utils import get_utc_now, to_object_id


class NarrativeRevisionConflict(ValueError):
    """A context mutation was prepared against a stale narrative revision."""


class NarrativeRevisionFenceConflict(NarrativeRevisionConflict):
    """A short-lived candidate mutation fence could not be acquired."""


class NarrativeRevisionStore:
    async def acquire_write_fence(
        self,
        novel_id: str,
        *,
        expected_revision: int,
        fence_token: str,
        ttl_seconds: int = 30,
    ) -> None:
        """Fence context writers while one candidate CAS validates its basis."""
        if not fence_token:
            raise ValueError("fence_token is required")
        now = get_utc_now()
        novel = await get_database()[collections.NOVELS].find_one_and_update(
            {
                "_id": to_object_id(novel_id),
                "$expr": {
                    "$eq": [
                        {"$ifNull": ["$narrative_revision", 0]},
                        int(expected_revision),
                    ]
                },
                "$or": [
                    {"narrative_write_fence": {"$exists": False}},
                    {"narrative_write_fence": None},
                    {"narrative_write_fence.expires_at": {"$lte": now}},
                    {"narrative_write_fence.token": str(fence_token)},
                ],
            },
            {
                "$set": {
                    "narrative_write_fence": {
                        "token": str(fence_token),
                        "expires_at": now
                        + timedelta(seconds=max(1, int(ttl_seconds))),
                    }
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if novel is None:
            raise NarrativeRevisionFenceConflict(
                "Narrative revision changed before the candidate mutation"
            )

    async def release_write_fence(
        self,
        novel_id: str,
        *,
        fence_token: str,
    ) -> None:
        if not fence_token:
            return
        await get_database()[collections.NOVELS].update_one(
            {
                "_id": to_object_id(novel_id),
                "narrative_write_fence.token": str(fence_token),
            },
            {"$unset": {"narrative_write_fence": ""}},
        )

    async def current(self, novel_id: str, *, session: Any = None) -> int:
        novel = await get_database()[collections.NOVELS].find_one(
            {"_id": to_object_id(novel_id)},
            projection={"narrative_revision": 1},
            session=session,
        )
        if novel is None:
            raise ValueError(f"Novel {novel_id} does not exist")
        return int(novel.get("narrative_revision") or 0)

    async def advance(
        self,
        novel_id: str,
        operation_id: str,
        *,
        expected_revision: int | None = None,
        session: Any = None,
    ) -> int:
        if not operation_id:
            raise ValueError("operation_id is required to advance narrative revision")
        operation_key = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
        marker = f"narrative_revision_operations.{operation_key}"
        query: dict[str, Any] = {
            "_id": to_object_id(novel_id),
            marker: {"$exists": False},
            "$or": [
                {"narrative_write_fence": {"$exists": False}},
                {"narrative_write_fence": None},
                {
                    "narrative_write_fence.expires_at": {
                        "$lte": get_utc_now()
                    }
                },
            ],
        }
        if expected_revision is not None:
            query["$expr"] = {
                "$eq": [
                    {"$ifNull": ["$narrative_revision", 0]},
                    int(expected_revision),
                ]
            }
        novel = await get_database()[collections.NOVELS].find_one_and_update(
            query,
            {
                "$inc": {"narrative_revision": 1},
                "$set": {marker: get_utc_now()},
            },
            return_document=ReturnDocument.AFTER,
            session=session,
        )
        if novel is not None:
            return int(novel.get("narrative_revision") or 0)
        current = await get_database()[collections.NOVELS].find_one(
            {"_id": to_object_id(novel_id)},
            projection={"narrative_revision": 1, marker: 1},
            session=session,
        )
        if current is None:
            raise ValueError(f"Novel {novel_id} does not exist")
        if operation_key in (current.get("narrative_revision_operations") or {}):
            return int(current.get("narrative_revision") or 0)
        raise NarrativeRevisionConflict(
            "Narrative revision changed or is fenced before the authorized mutation"
        )


narrative_revision_store = NarrativeRevisionStore()
