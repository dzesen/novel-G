"""Novel-scoped character and world-building reference card service."""

from __future__ import annotations

from typing import Any, Dict, List

from backend.db.repositories.character_repository import character_repo
from backend.db.repositories.novel_repository import novel_repo
from backend.db.repositories.reference_card_repository import ReferenceCardRepository
from backend.db.repositories.worldbook_repository import worldbook_repo


CARD_TYPES = {"character", "location", "item", "rule"}


def validate_card_type(card_type: str) -> str:
    normalized = card_type.strip().lower()
    if normalized not in CARD_TYPES:
        raise ValueError(f"Unsupported reference card type: {card_type}")
    return normalized


def get_card_repository(card_type: str) -> ReferenceCardRepository:
    normalized = validate_card_type(card_type)
    return character_repo if normalized == "character" else worldbook_repo


class ReferenceCardService:
    @staticmethod
    async def create(novel_id: str, card_type: str, data: Dict[str, Any]) -> str:
        normalized = validate_card_type(card_type)
        await novel_repo.get_novel_by_id(novel_id)
        return await get_card_repository(normalized).create_card(novel_id, normalized, data)

    @staticmethod
    async def list(novel_id: str, card_type: str, *, deleted_only: bool = False) -> List[Dict[str, Any]]:
        normalized = validate_card_type(card_type)
        await novel_repo.get_novel_by_id(novel_id)
        return await get_card_repository(normalized).list_cards(
            novel_id,
            normalized,
            deleted_only=deleted_only,
        )

    @staticmethod
    async def get(novel_id: str, card_type: str, card_id: str, *, include_deleted: bool = False) -> Dict[str, Any]:
        normalized = validate_card_type(card_type)
        await novel_repo.get_novel_by_id(novel_id)
        return await get_card_repository(normalized).get_card(
            novel_id,
            normalized,
            card_id,
            include_deleted=include_deleted,
        )

    @staticmethod
    async def update(novel_id: str, card_type: str, card_id: str, data: Dict[str, Any]) -> bool:
        normalized = validate_card_type(card_type)
        await novel_repo.get_novel_by_id(novel_id)
        return await get_card_repository(normalized).update_card(novel_id, normalized, card_id, data)

    @staticmethod
    async def soft_delete(novel_id: str, card_type: str, card_id: str) -> bool:
        normalized = validate_card_type(card_type)
        await novel_repo.get_novel_by_id(novel_id)
        return await get_card_repository(normalized).soft_delete_card(novel_id, normalized, card_id)

    @staticmethod
    async def restore(novel_id: str, card_type: str, card_id: str) -> bool:
        normalized = validate_card_type(card_type)
        await novel_repo.get_novel_by_id(novel_id)
        return await get_card_repository(normalized).restore_card(novel_id, normalized, card_id)

    @staticmethod
    async def hard_delete(novel_id: str, card_type: str, card_id: str) -> bool:
        normalized = validate_card_type(card_type)
        await novel_repo.get_novel_by_id(novel_id)
        return await get_card_repository(normalized).hard_delete_card(novel_id, normalized, card_id)
