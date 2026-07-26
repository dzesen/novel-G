"""Persistence for user-defined Agent profiles."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from pymongo import ReturnDocument

from backend.db.base import BaseRepository
from backend.db.collections import AGENT_DEFINITIONS
from backend.db.errors import NotFoundError
from backend.db.utils import get_utc_now, to_object_id


class AgentDefinitionVersionConflict(ValueError):
    """The saved Agent changed after the editor loaded it."""


class AgentDefinitionRepository(BaseRepository):
    def __init__(self) -> None:
        super().__init__(AGENT_DEFINITIONS)

    async def create_definition(
        self,
        *,
        owner_id: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        document = {
            **dict(data),
            "agent_id": f"custom_{uuid4().hex}",
            "owner_id": to_object_id(owner_id),
            "origin": "custom",
            "version": 1,
        }
        inserted_id = await self.insert_one(document)
        created = await self.collection.find_one({"_id": to_object_id(inserted_id)})
        if created is None:  # pragma: no cover - Mongo acknowledged the insert.
            raise RuntimeError("Agent definition disappeared after creation")
        return created

    async def list_visible(
        self,
        *,
        actor_id: str,
        include_disabled: bool = False,
        capability: str | None = None,
    ) -> list[dict[str, Any]]:
        owned_filter: dict[str, Any] = {"owner_id": to_object_id(actor_id)}
        if not include_disabled:
            owned_filter["enabled"] = True
        query: dict[str, Any] = {
            "$or": [
                owned_filter,
                {"visibility": "shared", "enabled": True},
            ],
            "is_deleted": False,
        }
        if capability:
            query["capability"] = capability
        cursor = self.collection.find(query).sort(
            [("origin", 1), ("label", 1), ("updated_at", -1)]
        )
        return await cursor.to_list(length=None)

    async def get_visible(
        self,
        *,
        actor_id: str,
        agent_id: str,
        include_disabled: bool = False,
    ) -> dict[str, Any]:
        query: dict[str, Any] = {
            "agent_id": agent_id,
            "$or": [
                {"owner_id": to_object_id(actor_id)},
                {"visibility": "shared"},
            ],
            "is_deleted": False,
        }
        if not include_disabled:
            query["enabled"] = True
        document = await self.collection.find_one(query)
        if document is None:
            raise NotFoundError(f"Agent '{agent_id}' was not found")
        return document

    async def get_owned(
        self,
        *,
        owner_id: str,
        agent_id: str,
        include_deleted: bool = False,
    ) -> dict[str, Any]:
        query: dict[str, Any] = {
            "agent_id": agent_id,
            "owner_id": to_object_id(owner_id),
        }
        if not include_deleted:
            query["is_deleted"] = False
        document = await self.collection.find_one(query)
        if document is None:
            raise NotFoundError(f"Editable Agent '{agent_id}' was not found")
        return document

    async def update_owned(
        self,
        *,
        owner_id: str,
        agent_id: str,
        expected_version: int,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        now = get_utc_now()
        updated = await self.collection.find_one_and_update(
            {
                "agent_id": agent_id,
                "owner_id": to_object_id(owner_id),
                "version": expected_version,
                "is_deleted": False,
            },
            {
                "$set": {**dict(updates), "updated_at": now},
                "$inc": {"version": 1},
            },
            return_document=ReturnDocument.AFTER,
        )
        if updated is not None:
            return updated

        existing = await self.collection.find_one(
            {
                "agent_id": agent_id,
                "owner_id": to_object_id(owner_id),
                "is_deleted": False,
            },
            projection={"version": 1},
        )
        if existing is None:
            raise NotFoundError(f"Editable Agent '{agent_id}' was not found")
        raise AgentDefinitionVersionConflict(
            f"Agent 已更新到版本 {existing.get('version', '?')}，请刷新后重试"
        )

    async def soft_delete_owned(self, *, owner_id: str, agent_id: str) -> bool:
        return await self.soft_delete_one(
            {
                "agent_id": agent_id,
                "owner_id": to_object_id(owner_id),
            }
        )


agent_definition_repo = AgentDefinitionRepository()
