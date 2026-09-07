"""Shared persistence for character and world-building reference cards."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Sequence

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
        if card_type == "character":
            # Novel-G 的收藏是本地组织状态。外部导入即使携带同名字段也不能
            # 继承该状态；新角色卡一律从未收藏开始。
            prepared["is_favorite"] = False
        if "character_profile" in data:
            if card_type != "character":
                raise ValueError(
                    "Character profile is only supported for character cards"
                )
            prepared["character_profile"] = dict(data["character_profile"] or {})
        if "interop" in data:
            prepared["interop"] = dict(data["interop"] or {})
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

    async def list_context_cards(
        self,
        novel_id: str,
        card_type: str,
        *,
        purpose: Literal["outline", "prose"],
        declared_card_ids: Sequence[str] = (),
        mentioned_card_ids: Sequence[str] = (),
        session: AsyncClientSession | None = None,
    ) -> List[Dict[str, Any]]:
        """Read a catalog or declared bodies through a database-side projection.

        Prose also needs the names of mentioned and major characters for the
        permanent-fact guard; only explicitly present characters have bodies.
        Invalid, absent and external names cannot become internal ObjectIds.
        """
        self._validate_type(card_type)
        if purpose not in {"outline", "prose"}:
            raise ValueError("context_read_purpose_invalid")

        def object_ids(values: Sequence[str]) -> list[ObjectId]:
            return [
                ObjectId(str(value)) for value in values
                if isinstance(value, (str, ObjectId)) and ObjectId.is_valid(value)
            ]

        declared = object_ids(declared_card_ids)
        query: dict[str, Any] = {
            "novel_id": to_object_id(novel_id),
            "card_type": card_type,
            "is_deleted": False,
        }
        projection: dict[str, Any] = {
            "name": 1, "importance": 1, "card_type": 1,
            "sort_order": 1, "description": 1,
        }
        if purpose == "outline":
            if card_type == "character":
                projection["character_profile.aliases"] = 1
            else:
                for key in ("keys", "constant", "insertion_order", "regex_fields", "preview_notices"):
                    projection[f"interop.display_metadata.{key}"] = 1
        elif card_type == "character":
            query["$or"] = [
                {"_id": {"$in": declared + object_ids(mentioned_card_ids)}},
                {"importance": "main"},
            ]
            for field in ("description", "details", "character_profile"):
                projection[field] = {
                    "$cond": [{"$in": ["$_id", declared]}, f"${field}", "$$REMOVE"],
                }
        else:
            if not declared:
                return []
            query["_id"] = {"$in": declared}
        cursor = await self.collection.aggregate([
            {"$match": query},
            {"$sort": {"sort_order": 1, "updated_at": -1}},
            {"$project": projection},
        ], session=session)
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
        allowed_fields = {
            "name",
            "subtitle",
            "description",
            "details",
            "tags",
            "sort_order",
            "importance",
            "character_profile",
            "interop",
        }
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
        if "character_profile" in prepared:
            if card_type != "character":
                raise ValueError(
                    "Character profile is only supported for character cards"
                )
            prepared["character_profile"] = dict(
                prepared["character_profile"] or {}
            )
        if "interop" in prepared:
            prepared["interop"] = dict(prepared["interop"] or {})
        if not prepared:
            return False
        return await self.update_one({"_id": current["_id"]}, prepared, session=session)

    async def set_favorite(
        self,
        novel_id: str,
        card_type: str,
        card_id: str,
        is_favorite: bool,
        session: AsyncClientSession | None = None,
    ) -> bool:
        """Update character-card organization state without touching content audit order.

        ``updated_at`` participates in the default card-list tie breaker. Changing it
        for a UI-only favorite would be an indirect route into roster/context ordering
        when two cards share ``sort_order``, so this metadata has a deliberately
        separate write path.
        """

        self._validate_type(card_type)
        if card_type != "character":
            raise ValueError("Favorites are only supported for character cards")
        if type(is_favorite) is not bool:
            raise ValueError("is_favorite must be a boolean")
        result = await self.collection.update_one(
            {
                "_id": to_object_id(card_id),
                "novel_id": to_object_id(novel_id),
                "card_type": card_type,
                "is_deleted": False,
            },
            {"$set": {"is_favorite": is_favorite}},
            session=session,
        )
        if result.matched_count == 0:
            raise NotFoundError(f"Reference card '{card_id}' was not found")
        return result.modified_count > 0

    async def compare_and_set_appearance_anchor(
        self,
        novel_id: str,
        card_id: str,
        *,
        expected: dict[str, Any] | None,
        replacement: dict[str, Any],
        session: AsyncClientSession | None = None,
    ) -> bool:
        """Atomically establish or replace a character's image-only anchor.

        This write deliberately bypasses ``BaseRepository.update_one`` so it
        does not touch ``updated_at``. That field breaks ties in reference-card
        ordering and can therefore affect prose context indirectly.
        """

        if "character" not in self.supported_types:
            raise ValueError("Appearance anchors are only supported for characters")
        query: dict[str, Any] = {
            "_id": to_object_id(card_id),
            "novel_id": to_object_id(novel_id),
            "card_type": "character",
            "is_deleted": False,
        }
        if expected is None:
            query["$or"] = [
                {"appearance_anchor": {"$exists": False}},
                {"appearance_anchor": None},
            ]
        else:
            query["appearance_anchor"] = dict(expected)
        result = await self.collection.update_one(
            query,
            {"$set": {"appearance_anchor": dict(replacement)}},
            session=session,
        )
        return result.modified_count == 1

    async def compare_and_clear_appearance_anchor(
        self,
        novel_id: str,
        card_id: str,
        *,
        expected: dict[str, Any],
        session: AsyncClientSession | None = None,
    ) -> bool:
        """Clear exactly the anchor the user inspected without touching prose order."""

        if "character" not in self.supported_types:
            raise ValueError("Appearance anchors are only supported for characters")
        result = await self.collection.update_one(
            {
                "_id": to_object_id(card_id),
                "novel_id": to_object_id(novel_id),
                "card_type": "character",
                "is_deleted": False,
                "appearance_anchor": dict(expected),
            },
            {"$unset": {"appearance_anchor": ""}},
            session=session,
        )
        return result.modified_count == 1

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
