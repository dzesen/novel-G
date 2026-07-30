"""Novel-scoped character and world-building reference card service."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Dict, List

from bson import ObjectId

from backend.db.errors import NotFoundError
from backend.db.mutation import MutationCommand, commit_mutation
from backend.db.repositories.character_repository import character_repo
from backend.db.repositories.novel_repository import novel_repo
from backend.db.repositories.reference_card_repository import ReferenceCardRepository
from backend.db.repositories.worldbook_repository import worldbook_repo
from backend.services.novel.character_profile import normalize_character_profile
from backend.services.novel.appearance_anchor import (
    AppearanceAnchorConflictError,
    normalize_appearance_anchor,
    require_appearance_anchor_reset_confirmation,
)


CARD_TYPES = {"character", "location", "item", "rule", "lore"}


def validate_card_type(card_type: str) -> str:
    normalized = card_type.strip().lower()
    if normalized not in CARD_TYPES:
        raise ValueError(f"Unsupported reference card type: {card_type}")
    return normalized


def get_card_repository(card_type: str) -> ReferenceCardRepository:
    normalized = validate_card_type(card_type)
    return character_repo if normalized == "character" else worldbook_repo


def reference_card_writing_participation(
    card: dict[str, Any],
    *,
    isolated_fields: list[str],
) -> dict[str, Any]:
    """Derive the user-facing status from fields that prose can actually read."""

    profile = card.get("character_profile") or {}
    details = card.get("details") or {}
    projected_fields: list[str] = []
    if str(card.get("description") or "").strip():
        projected_fields.append("description")
    if str(details.get("personality") or "").strip():
        projected_fields.append("details.personality")
    if str(profile.get("portrayal_context") or "").strip():
        projected_fields.append("character_profile.portrayal_context")
    if str(profile.get("portrayal_notes") or "").strip():
        projected_fields.append("character_profile.portrayal_notes")
    if profile.get("dialogue_examples"):
        projected_fields.append("character_profile.dialogue_examples")
    status = "active" if projected_fields else "not_participating"
    return {
        "status": status,
        "label": (
            "已参与写作"
            if status == "active"
            else "已导入但未参与写作"
        ),
        "projected_fields": projected_fields,
        "isolated_fields": sorted(set(isolated_fields)),
    }


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
        if "interop" in prepared:
            prepared["interop"] = dict(prepared["interop"] or {})
            participation = prepared["interop"].get(
                "writing_participation"
            ) or {}
            prepared["interop"]["writing_participation"] = (
                reference_card_writing_participation(
                    prepared,
                    isolated_fields=list(
                        participation.get("isolated_fields") or []
                    ),
                )
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
    async def get_appearance_anchor(
        novel_id: str,
        card_id: str,
    ) -> dict[str, Any] | None:
        """Return the validated image-only anchor without projecting it to prose."""

        card = await ReferenceCardService.get(
            novel_id,
            "character",
            card_id,
        )
        anchor = card.get("appearance_anchor")
        return (
            normalize_appearance_anchor(anchor)
            if anchor is not None
            else None
        )

    @staticmethod
    async def get_appearance_anchor_descriptor(
        novel_id: str,
        card_id: str,
    ) -> str | None:
        """Return the future scene-image mandatory prefix, if established."""

        anchor = await ReferenceCardService.get_appearance_anchor(
            novel_id,
            card_id,
        )
        return str(anchor["descriptor"]) if anchor is not None else None

    @staticmethod
    async def establish_appearance_anchor(
        novel_id: str,
        card_id: str,
        anchor: Any,
    ) -> dict[str, Any]:
        """Establish the first anchor only when the character has none."""

        await novel_repo.get_novel_by_id(novel_id)
        current = await character_repo.get_card(
            novel_id,
            "character",
            card_id,
        )
        if current.get("appearance_anchor") is not None:
            raise AppearanceAnchorConflictError(
                "Appearance anchor already exists"
            )
        replacement = normalize_appearance_anchor(anchor)
        changed = await character_repo.compare_and_set_appearance_anchor(
            novel_id,
            card_id,
            expected=None,
            replacement=replacement,
        )
        if not changed:
            raise AppearanceAnchorConflictError(
                "Appearance anchor already exists"
            )
        return replacement

    @staticmethod
    async def reset_appearance_anchor(
        novel_id: str,
        card_id: str,
        anchor: Any,
        *,
        expected_previous: Any,
        confirmed: bool,
    ) -> dict[str, Any]:
        """Replace an anchor only after warning acknowledgement and whole-value CAS."""

        await novel_repo.get_novel_by_id(novel_id)
        current = await character_repo.get_card(
            novel_id,
            "character",
            card_id,
        )
        current_anchor = current.get("appearance_anchor")
        if current_anchor is None:
            raise AppearanceAnchorConflictError(
                "Appearance anchor changed or no longer exists"
            )
        require_appearance_anchor_reset_confirmation(
            current_anchor,
            confirmed=confirmed,
        )
        expected = normalize_appearance_anchor(expected_previous)
        if normalize_appearance_anchor(current_anchor) != expected:
            raise AppearanceAnchorConflictError(
                "Appearance anchor changed after reset was prepared"
            )
        replacement = normalize_appearance_anchor(anchor)
        changed = await character_repo.compare_and_set_appearance_anchor(
            novel_id,
            card_id,
            expected=current_anchor,
            replacement=replacement,
        )
        if not changed:
            raise AppearanceAnchorConflictError(
                "Appearance anchor changed after reset was prepared"
            )
        return replacement

    @staticmethod
    async def clear_appearance_anchor(
        novel_id: str,
        card_id: str,
        *,
        expected_previous: Any,
    ) -> None:
        """Clear a frozen image anchor only if it still matches the inspected value."""

        await novel_repo.get_novel_by_id(novel_id)
        current = await character_repo.get_card(
            novel_id,
            "character",
            card_id,
        )
        current_anchor = current.get("appearance_anchor")
        expected = normalize_appearance_anchor(expected_previous)
        if (
            current_anchor is None
            or normalize_appearance_anchor(current_anchor) != expected
        ):
            raise AppearanceAnchorConflictError(
                "外观锚点已变化，请刷新角色卡后再决定是否解绑"
            )
        changed = await character_repo.compare_and_clear_appearance_anchor(
            novel_id,
            card_id,
            expected=current_anchor,
        )
        if not changed:
            raise AppearanceAnchorConflictError(
                "外观锚点已变化，请刷新角色卡后再决定是否解绑"
            )

    @staticmethod
    async def set_favorite(
        novel_id: str,
        card_type: str,
        card_id: str,
        is_favorite: bool,
    ) -> bool:
        """Atomically set UI-only metadata without a replayable narrative mutation."""

        normalized = validate_card_type(card_type)
        if normalized != "character":
            raise ValueError("Favorites are only supported for character cards")
        if type(is_favorite) is not bool:
            raise ValueError("is_favorite must be a boolean")
        await novel_repo.get_novel_by_id(novel_id)
        # This is one atomic document update with an explicit target state. It
        # deliberately has no recoverable journal: an old failed journal could
        # otherwise replay after a newer toggle and overwrite the user's final
        # choice. Retrying the same target state is already safe.
        return await get_card_repository(normalized).set_favorite(
            novel_id,
            normalized,
            card_id,
            is_favorite,
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
            "interop",
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
        if "interop" in prepared:
            prepared["interop"] = dict(prepared["interop"] or {})
        if (
            current.get("interop") is not None
            and set(prepared) & {"description", "details", "character_profile"}
        ):
            interop = deepcopy(
                prepared.get("interop")
                if "interop" in prepared
                else current.get("interop") or {}
            )
            participation = interop.get("writing_participation") or {}
            interop["writing_participation"] = (
                reference_card_writing_participation(
                    {**current, **prepared},
                    isolated_fields=list(
                        participation.get("isolated_fields") or []
                    ),
                )
            )
            prepared["interop"] = interop
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
        # 世界资料卡的 interop.display_metadata 已参与章细纲紧凑索引
        # （keys/constant/insertion_order/正则隔离标记）。API 当前不直接开放该字段，
        # 但导入合并和内部服务可更新它；把 worldbook interop 视为上下文字段能守住
        # “索引变化必须让缓存失效”的不变量。宁可对 raw_entry 等少量元数据更新
        # 过度失效，也不能漏掉关键词索引变化后继续使用旧上下文。
        world_entry_index_metadata_changed = (
            normalized != "character" and "interop" in changes
        )
        affects_context = (
            bool(set(changes) & ReferenceCardService.CONTEXT_FIELDS)
            or personality_changed
            or world_entry_index_metadata_changed
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
