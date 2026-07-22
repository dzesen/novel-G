"""Shared persistence for character and world-building reference cards."""

from __future__ import annotations

from typing import Any, Dict, List

from bson import ObjectId
from pymongo.asynchronous.client_session import AsyncClientSession

from backend.db.base import BaseRepository
from backend.db.errors import NotFoundError
from backend.db.utils import to_object_id

# 与 §4.3 plot_threads 的 importance 词汇保持一致：主要角色的
# permanent_facts 无条件装配（见设计 §5.2），次要角色不享受该待遇。
CARD_IMPORTANCE_VALUES = {"main", "sub"}


class ReferenceCardRepository(BaseRepository):
    """Store a single family of novel-scoped reference cards."""

    def __init__(self, collection_name: str, supported_types: set[str]):
        super().__init__(collection_name)
        self.supported_types = supported_types

    def _validate_type(self, card_type: str) -> None:
        if card_type not in self.supported_types:
            raise ValueError(f"Unsupported reference card type: {card_type}")

    async def _next_sort_order(
        self,
        novel_id: ObjectId,
        card_type: str,
        session: AsyncClientSession | None = None,
    ) -> int:
        cursor = self.collection.find(
            {"novel_id": novel_id, "card_type": card_type, "is_deleted": False},
            projection={"sort_order": 1},
            session=session,
        ).sort("sort_order", -1).limit(1)
        rows = await cursor.to_list(length=1)
        return int(rows[0].get("sort_order", 0)) + 10 if rows else 10

    async def create_card(
        self,
        novel_id: str,
        card_type: str,
        data: Dict[str, Any],
        session: AsyncClientSession | None = None,
        *,
        card_id: str | None = None,
    ) -> str:
        self._validate_type(card_type)
        name = str(data.get("name", "")).strip()
        if not name:
            raise ValueError("Card name cannot be empty")

        importance = str(data.get("importance") or "sub")
        if importance not in CARD_IMPORTANCE_VALUES:
            raise ValueError(f"Unsupported card importance: {importance}")

        obj_id = to_object_id(novel_id)
        prepared = {
            "novel_id": obj_id,
            "card_type": card_type,
            "name": name,
            "subtitle": str(data.get("subtitle", "")).strip(),
            "description": str(data.get("description", "")).strip(),
            "details": dict(data.get("details") or {}),
            "tags": list(data.get("tags") or []),
            "importance": importance,
            "sort_order": int(data.get("sort_order") or await self._next_sort_order(obj_id, card_type, session)),
        }
        if card_id is not None:
            prepared["_id"] = to_object_id(card_id)
        return await self.insert_one(prepared, session=session)

    async def list_cards(
        self,
        novel_id: str,
        card_type: str,
        *,
        deleted_only: bool = False,
        session: AsyncClientSession | None = None,
    ) -> List[Dict[str, Any]]:
        self._validate_type(card_type)
        query = {
            "novel_id": to_object_id(novel_id),
            "card_type": card_type,
            "is_deleted": deleted_only,
        }
        cursor = self.collection.find(query, session=session).sort(
            [("sort_order", 1), ("updated_at", -1)]
        )
        return await cursor.to_list(length=None)

    async def get_card(
        self,
        novel_id: str,
        card_type: str,
        card_id: str,
        *,
        include_deleted: bool = False,
        session: AsyncClientSession | None = None,
    ) -> Dict[str, Any]:
        self._validate_type(card_type)
        card = await self.find_one(
            {
                "_id": to_object_id(card_id),
                "novel_id": to_object_id(novel_id),
                "card_type": card_type,
            },
            include_deleted=include_deleted,
            session=session,
        )
        if not card:
            raise NotFoundError(f"Reference card '{card_id}' was not found")
        return card

    async def update_card(
        self,
        novel_id: str,
        card_type: str,
        card_id: str,
        data: Dict[str, Any],
        session: AsyncClientSession | None = None,
    ) -> bool:
        current = await self.get_card(novel_id, card_type, card_id, session=session)
        allowed_fields = {"name", "subtitle", "description", "details", "tags", "sort_order", "importance"}
        prepared = {key: value for key, value in data.items() if key in allowed_fields}
        if "name" in prepared:
            prepared["name"] = str(prepared["name"]).strip()
            if not prepared["name"]:
                raise ValueError("Card name cannot be empty")
        if "details" in prepared:
            prepared["details"] = dict(prepared["details"] or {})
        if "tags" in prepared:
            prepared["tags"] = list(prepared["tags"] or [])
        if "importance" in prepared:
            prepared["importance"] = str(prepared["importance"])
            if prepared["importance"] not in CARD_IMPORTANCE_VALUES:
                raise ValueError(f"Unsupported card importance: {prepared['importance']}")
        if not prepared:
            return False
        return await self.update_one({"_id": current["_id"]}, prepared, session=session)

    async def soft_delete_card(
        self,
        novel_id: str,
        card_type: str,
        card_id: str,
        session: AsyncClientSession | None = None,
    ) -> bool:
        current = await self.get_card(novel_id, card_type, card_id, session=session)
        return await self.soft_delete_one({"_id": current["_id"]}, session=session)

    async def restore_card(
        self,
        novel_id: str,
        card_type: str,
        card_id: str,
        session: AsyncClientSession | None = None,
    ) -> bool:
        current = await self.get_card(
            novel_id,
            card_type,
            card_id,
            include_deleted=True,
            session=session,
        )
        if not current.get("is_deleted"):
            raise ValueError("Only deleted reference cards can be restored")
        return await self.restore_one({"_id": current["_id"]}, session=session)

    async def hard_delete_card(
        self,
        novel_id: str,
        card_type: str,
        card_id: str,
        session: AsyncClientSession | None = None,
    ) -> bool:
        current = await self.get_card(
            novel_id,
            card_type,
            card_id,
            include_deleted=True,
            session=session,
        )
        if not current.get("is_deleted"):
            raise ValueError("Only deleted reference cards can be permanently deleted")
        return await self.hard_delete_one({"_id": current["_id"]}, session=session)
