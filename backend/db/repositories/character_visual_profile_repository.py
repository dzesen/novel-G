"""Persistence for owner-scoped character visual profiles."""

from __future__ import annotations

from typing import Any

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.asynchronous.client_session import AsyncClientSession

from backend.db.base import BaseRepository
from backend.db.collections import CHARACTER_VISUAL_PROFILES


class CharacterVisualProfileRepository(BaseRepository):
    def __init__(self) -> None:
        super().__init__(CHARACTER_VISUAL_PROFILES)

    async def get_active(
        self,
        *,
        owner_id: ObjectId,
        novel_id: ObjectId,
        character_card_id: ObjectId,
        session: AsyncClientSession | None = None,
    ) -> dict[str, Any] | None:
        return await self.collection.find_one(
            {
                "owner_id": owner_id,
                "novel_id": novel_id,
                "character_card_id": character_card_id,
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
                "is_deleted": False,
                "deleted_at": None,
                "revision": 1,
            }
        )
        result = await self.collection.insert_one(prepared, session=session)
        prepared["_id"] = result.inserted_id
        return prepared

    async def replace_if_revision(
        self,
        *,
        owner_id: ObjectId,
        novel_id: ObjectId,
        character_card_id: ObjectId,
        expected_revision: int,
        references: list[dict[str, Any]],
        external_adapter: dict[str, Any] | None,
        session: AsyncClientSession | None = None,
    ) -> dict[str, Any] | None:
        update = self._prepare_audit_fields_for_update(
            {
                "references": references,
                "external_adapter": external_adapter,
            }
        )
        return await self.collection.find_one_and_update(
            {
                "owner_id": owner_id,
                "novel_id": novel_id,
                "character_card_id": character_card_id,
                "revision": expected_revision,
                "is_deleted": False,
            },
            {
                "$set": update,
                "$inc": {"revision": 1},
            },
            return_document=ReturnDocument.AFTER,
            session=session,
        )

    async def hard_delete_for_character_card(
        self,
        *,
        novel_id: ObjectId,
        character_card_id: ObjectId,
        session: AsyncClientSession | None = None,
    ) -> int:
        result = await self.collection.delete_many(
            {
                "novel_id": novel_id,
                "character_card_id": character_card_id,
            },
            session=session,
        )
        return result.deleted_count


character_visual_profile_repo = CharacterVisualProfileRepository()
