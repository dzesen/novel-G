"""Persistence for owner-scoped staged illustration runs."""

from __future__ import annotations

from typing import Any

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.asynchronous.client_session import AsyncClientSession

from backend.db.base import BaseRepository
from backend.db.collections import ILLUSTRATION_RUNS


TERMINAL_ILLUSTRATION_RUN_STATUSES = frozenset(
    {"finalized", "cancelled", "failed", "branched"}
)
MUTABLE_ILLUSTRATION_RUN_FIELDS = frozenset(
    {"status", "stages", "final_asset_id"}
)



class IllustrationRunRepository(BaseRepository):
    def __init__(self) -> None:
        super().__init__(ILLUSTRATION_RUNS)

    async def get_owned(
        self,
        *,
        owner_id: ObjectId,
        novel_id: ObjectId,
        run_id: ObjectId,
        session: AsyncClientSession | None = None,
    ) -> dict[str, Any] | None:
        return await self.collection.find_one(
            {
                "_id": run_id,
                "owner_id": owner_id,
                "novel_id": novel_id,
                "is_deleted": False,
            },
            session=session,
        )

    async def get_active_for_brief(
        self,
        *,
        owner_id: ObjectId,
        novel_id: ObjectId,
        illustration_brief_id: ObjectId,
        session: AsyncClientSession | None = None,
    ) -> dict[str, Any] | None:
        return await self.collection.find_one(
            {
                "owner_id": owner_id,
                "novel_id": novel_id,
                "illustration_brief_id": illustration_brief_id,
                "status": {"$nin": list(TERMINAL_ILLUSTRATION_RUN_STATUSES)},
                "is_deleted": False,
            },
            session=session,
        )

    async def create_active(
        self,
        document: dict[str, Any],
        *,
        session: AsyncClientSession | None = None,
    ) -> dict[str, Any]:
        prepared = self._prepare_audit_fields_for_insert(document)
        prepared.update(
            {
                "status": "active",
                "revision": 1,
                "is_deleted": False,
                "deleted_at": None,
            }
        )
        result = await self.collection.insert_one(prepared, session=session)
        stored = await self.collection.find_one(
            {"_id": result.inserted_id},
            session=session,
        )
        if stored is None:  # pragma: no cover - acknowledged insert invariant.
            raise RuntimeError("Illustration run disappeared after creation")
        return stored

    async def update_if_revision(
        self,
        *,
        owner_id: ObjectId,
        novel_id: ObjectId,
        run_id: ObjectId,
        expected_revision: int,
        changes: dict[str, Any],
        require_active: bool = True,
        session: AsyncClientSession | None = None,
    ) -> dict[str, Any] | None:
        invalid_fields = set(changes) - MUTABLE_ILLUSTRATION_RUN_FIELDS
        if invalid_fields:
            unsupported = ", ".join(sorted(invalid_fields))
            raise ValueError(
                "Illustration run snapshots are immutable; unsupported CAS "
                f"fields: {unsupported}"
            )
        query: dict[str, Any] = {
            "_id": run_id,
            "owner_id": owner_id,
            "novel_id": novel_id,
            "revision": expected_revision,
            "is_deleted": False,
        }
        if require_active:
            query["status"] = {
                "$nin": list(TERMINAL_ILLUSTRATION_RUN_STATUSES)
            }
        update = self._prepare_audit_fields_for_update(changes)
        return await self.collection.find_one_and_update(
            query,
            {"$set": update, "$inc": {"revision": 1}},
            return_document=ReturnDocument.AFTER,
            session=session,
        )

    async def hard_delete_for_chapter(
        self,
        *,
        chapter_id: ObjectId,
        session: AsyncClientSession | None = None,
    ) -> int:
        result = await self.collection.delete_many(
            {"chapter_id": chapter_id},
            session=session,
        )
        return result.deleted_count

    async def hard_delete_for_chapters(
        self,
        *,
        chapter_ids: list[ObjectId],
        session: AsyncClientSession | None = None,
    ) -> int:
        if not chapter_ids:
            return 0
        result = await self.collection.delete_many(
            {"chapter_id": {"$in": chapter_ids}},
            session=session,
        )
        return result.deleted_count


illustration_run_repo = IllustrationRunRepository()
