"""Persistence for owner-scoped chapter illustration briefs."""

from __future__ import annotations

from typing import Any

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.asynchronous.client_session import AsyncClientSession

from backend.db.base import BaseRepository
from backend.db.collections import ILLUSTRATION_BRIEFS


class IllustrationBriefRepository(BaseRepository):
    def __init__(self) -> None:
        super().__init__(ILLUSTRATION_BRIEFS)

    async def list_for_chapter(
        self,
        *,
        owner_id: ObjectId,
        novel_id: ObjectId,
        chapter_id: ObjectId,
        include_archived: bool = False,
        session: AsyncClientSession | None = None,
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {
            "owner_id": owner_id,
            "novel_id": novel_id,
            "chapter_id": chapter_id,
            "is_deleted": False,
        }
        if not include_archived:
            query["status"] = "active"
        cursor = self.collection.find(query, session=session).sort(
            [("sort_order", 1), ("_id", 1)]
        )
        return await cursor.to_list(length=None)

    async def get_owned(
        self,
        *,
        owner_id: ObjectId,
        novel_id: ObjectId,
        chapter_id: ObjectId,
        brief_id: ObjectId,
        session: AsyncClientSession | None = None,
    ) -> dict[str, Any] | None:
        return await self.collection.find_one(
            {
                "_id": brief_id,
                "owner_id": owner_id,
                "novel_id": novel_id,
                "chapter_id": chapter_id,
                "is_deleted": False,
            },
            session=session,
        )

    async def next_sort_order(
        self,
        *,
        owner_id: ObjectId,
        novel_id: ObjectId,
        chapter_id: ObjectId,
        session: AsyncClientSession | None = None,
    ) -> int:
        document = await self.collection.find_one(
            {
                "owner_id": owner_id,
                "novel_id": novel_id,
                "chapter_id": chapter_id,
                "is_deleted": False,
            },
            projection={"sort_order": 1},
            sort=[("sort_order", -1), ("_id", -1)],
            session=session,
        )
        return int(document.get("sort_order") or 0) + 10 if document else 10

    async def create_active(
        self,
        document: dict[str, Any],
        *,
        session: AsyncClientSession | None = None,
    ) -> dict[str, Any]:
        prepared = self._prepare_audit_fields_for_insert(document)
        prepared.update(
            {
                "is_deleted": False,
                "deleted_at": None,
                "revision": 1,
            }
        )
        result = await self.collection.insert_one(prepared, session=session)
        prepared["_id"] = result.inserted_id
        return prepared

    async def patch_if_revision(
        self,
        *,
        owner_id: ObjectId,
        novel_id: ObjectId,
        chapter_id: ObjectId,
        brief_id: ObjectId,
        expected_revision: int,
        changes: dict[str, Any],
        session: AsyncClientSession | None = None,
    ) -> dict[str, Any] | None:
        update = self._prepare_audit_fields_for_update(changes)
        return await self.collection.find_one_and_update(
            {
                "_id": brief_id,
                "owner_id": owner_id,
                "novel_id": novel_id,
                "chapter_id": chapter_id,
                "revision": expected_revision,
                "is_deleted": False,
            },
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


illustration_brief_repo = IllustrationBriefRepository()
