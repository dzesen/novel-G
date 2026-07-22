"""Monotonic, idempotent narrative revision storage for generation leases."""

from __future__ import annotations

import hashlib
from typing import Any

from pymongo import ReturnDocument

from backend.db import collections
from backend.db.mongo import get_database
from backend.db.utils import get_utc_now, to_object_id


class NarrativeRevisionStore:
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
        session: Any = None,
    ) -> int:
        if not operation_id:
            raise ValueError("operation_id is required to advance narrative revision")
        operation_key = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
        marker = f"narrative_revision_operations.{operation_key}"
        novel = await get_database()[collections.NOVELS].find_one_and_update(
            {
                "_id": to_object_id(novel_id),
                marker: {"$exists": False},
            },
            {
                "$inc": {"narrative_revision": 1},
                "$set": {marker: get_utc_now()},
            },
            return_document=ReturnDocument.AFTER,
            session=session,
        )
        if novel is not None:
            return int(novel.get("narrative_revision") or 0)
        return await self.current(novel_id, session=session)


narrative_revision_store = NarrativeRevisionStore()
