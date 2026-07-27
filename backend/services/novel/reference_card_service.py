"""Novel-scoped character and world-building reference card service."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List

from bson import ObjectId

from backend.db.errors import NotFoundError
from backend.db.mutation import MutationCommand, commit_mutation
from backend.db.repositories.character_repository import character_repo
from backend.db.repositories.novel_repository import novel_repo
from backend.db.repositories.reference_card_repository import ReferenceCardRepository
from backend.db.repositories.worldbook_repository import worldbook_repo
from backend.services.novel.character_profile import normalize_character_profile


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
    CONTEXT_FIELDS = frozenset(
        {"name", "description", "importance", "sort_order", "character_profile"}
    )

    @staticmethod
    async def _execute_mutation(session, mutation):
        command = mutation.journal["command"]["payload"]
        novel_id = str(mutation.journal["novel_id"])
        card_type = str(command["card_type"])
        card_id = str(command["card_id"])
        repository = get_card_repository(card_type)
        operation = str(mutation.journal["operation"])
        if operation == "create_reference_card":
            try:
                await repository.get_card(
                    novel_id,
                    card_type,
                    card_id,
                    include_deleted=True,
                    session=session,
                )
            except NotFoundError:
                await repository.create_card(
                    novel_id,
                    card_type,
                    command["data"],
                    session=session,
                    card_id=card_id,
                )
            return card_id
        if operation in {
            "update_reference_card_context",
            "update_reference_card_metadata",
        }:
            await repository.update_card(
                novel_id,
                card_type,
                card_id,
                command["data"],
                session=session,
            )
            return True
        if operation == "soft_delete_reference_card":
            current = await repository.get_card(
                novel_id,
                card_type,
                card_id,
                include_deleted=True,
                session=session,
            )
            if not current.get("is_deleted"):
                await repository.soft_delete_card(
                    novel_id, card_type, card_id, session=session
                )
            return True
        if operation == "restore_reference_card":
            current = await repository.get_card(
                novel_id,
                card_type,
                card_id,
                include_deleted=True,
                session=session,
            )
            if current.get("is_deleted"):
                await repository.restore_card(
                    novel_id, card_type, card_id, session=session
                )
            return True
        if operation == "hard_delete_reference_card":
            try:
                current = await repository.get_card(
                    novel_id,
                    card_type,
                    card_id,
                    include_deleted=True,
                    session=session,
                )
            except NotFoundError:
                return True
            if not current.get("is_deleted"):
                raise ValueError("Only deleted reference cards can be permanently deleted")
            await repository.hard_delete_card(
                novel_id, card_type, card_id, session=session
            )
            return True
        raise ValueError(f"Unsupported reference-card mutation: {operation}")

    @staticmethod
    def _digest(value: Any) -> str:
        return hashlib.sha256(
            json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode(
                "utf-8"
            )
        ).hexdigest()

    @staticmethod
    async def create(novel_id: str, card_type: str, data: Dict[str, Any]) -> str:
        normalized = validate_card_type(card_type)
        await novel_repo.get_novel_by_id(novel_id)
        # Validate before persisting the mutation intent.
        if not str(data.get("name") or "").strip():
            raise ValueError("Card name cannot be empty")
        importance = str(data.get("importance") or "sub")
        if importance not in {"main", "sub"}:
            raise ValueError(f"Unsupported card importance: {importance}")
        prepared = dict(data)
        if "character_profile" in prepared:
            if normalized != "character":
                raise ValueError(
                    "Character profile is only supported for character cards"
                )
            prepared["character_profile"] = normalize_character_profile(
                prepared["character_profile"]
            )
        card_id = str(ObjectId())
        return await commit_mutation(
            MutationCommand(
                novel_id=novel_id,
                idempotency_key=f"create-reference-card:{card_id}",
                operation="create_reference_card",
                payload={
                    "card_type": normalized,
                    "card_id": card_id,
                    "data": prepared,
                },
                child_ids={"card": card_id},
            ),
            ReferenceCardService._execute_mutation,
        )

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
        repository = get_card_repository(normalized)
        current = await repository.get_card(novel_id, normalized, card_id)
        allowed_fields = {
            "name",
            "subtitle",
            "description",
            "details",
            "tags",
            "sort_order",
            "importance",
            "character_profile",
        }
        prepared = {key: value for key, value in data.items() if key in allowed_fields}
        if "name" in prepared:
            prepared["name"] = str(prepared["name"] or "").strip()
        if "subtitle" in prepared:
            prepared["subtitle"] = str(prepared["subtitle"] or "").strip()
        if "description" in prepared:
            prepared["description"] = str(prepared["description"] or "").strip()
        if "details" in prepared:
            prepared["details"] = dict(prepared["details"] or {})
        if "tags" in prepared:
            prepared["tags"] = list(prepared["tags"] or [])
        if "importance" in prepared:
            prepared["importance"] = str(prepared["importance"])
        if "character_profile" in prepared:
            if normalized != "character":
                raise ValueError(
                    "Character profile is only supported for character cards"
                )
            prepared["character_profile"] = normalize_character_profile(
                prepared["character_profile"]
            )
        changes = {
            key: value for key, value in prepared.items() if current.get(key) != value
        }
        if not changes:
            return False
        # Reuse repository validation before the journal is created.
        if "name" in changes and not str(changes["name"] or "").strip():
            raise ValueError("Card name cannot be empty")
        if "importance" in changes and str(changes["importance"]) not in {"main", "sub"}:
            raise ValueError(f"Unsupported card importance: {changes['importance']}")
        personality_changed = (
            normalized == "character"
            and "details" in changes
            and str((current.get("details") or {}).get("personality") or "")
            != str((changes.get("details") or {}).get("personality") or "")
        )
        affects_context = (
            bool(set(changes) & ReferenceCardService.CONTEXT_FIELDS)
            or personality_changed
        )
        operation = (
            "update_reference_card_context"
            if affects_context
            else "update_reference_card_metadata"
        )
        return await commit_mutation(
            MutationCommand(
                novel_id=novel_id,
                idempotency_key=(
                    f"update-reference-card:{card_id}:{current.get('updated_at')}:"
                    f"{ReferenceCardService._digest(changes)}"
                ),
                operation=operation,
                payload={
                    "card_type": normalized,
                    "card_id": card_id,
                    "data": changes,
                },
                before_image={key: current.get(key) for key in changes},
            ),
            ReferenceCardService._execute_mutation,
            advances_narrative_revision=affects_context,
        )

    @staticmethod
    async def soft_delete(novel_id: str, card_type: str, card_id: str) -> bool:
        normalized = validate_card_type(card_type)
        await novel_repo.get_novel_by_id(novel_id)
        current = await get_card_repository(normalized).get_card(
            novel_id, normalized, card_id
        )
        return await ReferenceCardService._commit_lifecycle(
            novel_id, normalized, card_id, current, "soft_delete_reference_card"
        )

    @staticmethod
    async def restore(novel_id: str, card_type: str, card_id: str) -> bool:
        normalized = validate_card_type(card_type)
        await novel_repo.get_novel_by_id(novel_id)
        current = await get_card_repository(normalized).get_card(
            novel_id, normalized, card_id, include_deleted=True
        )
        if not current.get("is_deleted"):
            raise ValueError("Only deleted reference cards can be restored")
        return await ReferenceCardService._commit_lifecycle(
            novel_id, normalized, card_id, current, "restore_reference_card"
        )

    @staticmethod
    async def hard_delete(novel_id: str, card_type: str, card_id: str) -> bool:
        normalized = validate_card_type(card_type)
        await novel_repo.get_novel_by_id(novel_id)
        current = await get_card_repository(normalized).get_card(
            novel_id, normalized, card_id, include_deleted=True
        )
        if not current.get("is_deleted"):
            raise ValueError("Only deleted reference cards can be permanently deleted")
        return await ReferenceCardService._commit_lifecycle(
            novel_id, normalized, card_id, current, "hard_delete_reference_card"
        )

    @staticmethod
    async def _commit_lifecycle(
        novel_id: str,
        card_type: str,
        card_id: str,
        current: Dict[str, Any],
        operation: str,
    ) -> bool:
        return await commit_mutation(
            MutationCommand(
                novel_id=novel_id,
                idempotency_key=(
                    f"{operation}:{card_id}:{current.get('updated_at')}"
                ),
                operation=operation,
                payload={"card_type": card_type, "card_id": card_id},
                before_image={
                    "is_deleted": bool(current.get("is_deleted")),
                    "updated_at": current.get("updated_at"),
                },
            ),
            ReferenceCardService._execute_mutation,
        )
