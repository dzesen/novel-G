"""Character visual assets and optional external LoRA registration.

This module is deliberately image-only. It neither participates in prose
context assembly nor advances the novel narrative revision.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any, Literal

from bson import ObjectId
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from pymongo.errors import DuplicateKeyError

from backend.db.errors import InvalidIdError, NotFoundError
from backend.db.repositories.character_visual_profile_repository import (
    CharacterVisualProfileRepository,
    character_visual_profile_repo,
)
from backend.db.repositories.image_asset_repository import (
    ImageAssetRepository,
    image_asset_repo,
)
from backend.db.utils import to_object_id
from backend.services.novel.appearance_anchor import AppearanceAnchorSchema
from backend.services.novel.reference_card_service import ReferenceCardService


MAX_CHARACTER_VISUAL_REFERENCES = 32
_LORA_FORBIDDEN = re.compile(r"""[\\/:*?"<>|;&`$]""")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


class CharacterVisualProfileRevisionConflict(RuntimeError):
    """The caller edited a visual profile from a stale revision."""


def _optional_text(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _canonical_object_id(value: Any, *, field_name: str) -> ObjectId:
    if value is None or not str(value).strip():
        raise InvalidIdError(f"{field_name} must be a valid ObjectId")
    return to_object_id(str(value).strip())


class CharacterVisualReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: str
    view: str | None = Field(default=None, max_length=80)
    framing: str | None = Field(default=None, max_length=80)
    expression: str | None = Field(default=None, max_length=120)
    costume: str | None = Field(default=None, max_length=240)
    note: str | None = Field(default=None, max_length=500)

    @field_validator("asset_id", mode="before")
    @classmethod
    def normalize_asset_id(cls, value: Any) -> str:
        try:
            return str(_canonical_object_id(value, field_name="asset_id"))
        except InvalidIdError as error:
            raise ValueError(
                "asset_id must be a valid image asset id"
            ) from error

    @field_validator(
        "view",
        "framing",
        "expression",
        "costume",
        "note",
        mode="before",
    )
    @classmethod
    def normalize_optional_text(cls, value: Any) -> str | None:
        return _optional_text(value)


class ExternalLoraAdapter(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["lora"] = "lora"
    lora_name: str = Field(min_length=1, max_length=500)
    trigger_word: str | None = Field(default=None, max_length=300)
    strength: float = Field(default=1.0, ge=0.0, le=2.0)
    base_model_family: str = Field(min_length=1, max_length=120)
    version_note: str | None = Field(default=None, max_length=500)

    @field_validator("lora_name", mode="before")
    @classmethod
    def validate_lora_name(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        lowered = normalized.lower()
        if (
            not normalized
            or normalized.startswith("-")
            or ".." in normalized
            or lowered.startswith(("http:", "https:", "data:", "file:"))
            or _LORA_FORBIDDEN.search(normalized)
            or _CONTROL_CHARACTERS.search(normalized)
            or " --" in normalized
        ):
            raise ValueError(
                "lora_name must be a safe provider filename, not a path, URL, "
                "or shell argument"
            )
        return normalized

    @field_validator(
        "trigger_word",
        "base_model_family",
        "version_note",
        mode="before",
    )
    @classmethod
    def normalize_text(cls, value: Any) -> str | None:
        return _optional_text(value)

    @field_validator("base_model_family", mode="after")
    @classmethod
    def require_base_model_family(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("base_model_family is required")
        return value

    @field_validator("strength", mode="after")
    @classmethod
    def require_finite_strength(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("strength must be finite")
        return value


class CharacterVisualProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_revision: int = Field(ge=0)
    references: tuple[CharacterVisualReference, ...] = Field(
        default_factory=tuple,
        max_length=MAX_CHARACTER_VISUAL_REFERENCES,
    )
    external_adapter: ExternalLoraAdapter | None = None

    @model_validator(mode="after")
    def reject_duplicate_assets(self) -> "CharacterVisualProfileUpdate":
        ids = [reference.asset_id for reference in self.references]
        if len(ids) != len(set(ids)):
            raise ValueError("references contain duplicate asset_id values")
        return self


class CharacterVisualProfileProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    exists: bool
    profile_id: str | None
    owner_id: str
    novel_id: str
    character_card_id: str
    references: tuple[CharacterVisualReference, ...] = ()
    external_adapter: ExternalLoraAdapter | None = None
    appearance_anchor: AppearanceAnchorSchema | None = None
    revision: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CharacterVisualProfileService:
    def __init__(
        self,
        *,
        repository: CharacterVisualProfileRepository | None = None,
        assets: ImageAssetRepository | None = None,
    ) -> None:
        self._repository = repository or character_visual_profile_repo
        self._assets = assets or image_asset_repo

    async def _require_scope(
        self,
        *,
        owner_id: ObjectId,
        novel_id: ObjectId,
        character_card_id: ObjectId,
    ) -> None:
        if not await self._assets.novel_belongs_to_owner(
            owner_id=owner_id,
            novel_id=novel_id,
        ):
            raise NotFoundError("Novel or character card was not found")
        await ReferenceCardService.get(
            str(novel_id),
            "character",
            str(character_card_id),
        )

    async def _appearance_anchor(
        self,
        *,
        novel_id: ObjectId,
        character_card_id: ObjectId,
    ) -> AppearanceAnchorSchema | None:
        anchor = await ReferenceCardService.get_appearance_anchor(
            str(novel_id),
            str(character_card_id),
        )
        return (
            AppearanceAnchorSchema.model_validate(anchor)
            if anchor is not None
            else None
        )

    @staticmethod
    def _project(
        document: dict[str, Any] | None,
        *,
        owner_id: ObjectId,
        novel_id: ObjectId,
        character_card_id: ObjectId,
        appearance_anchor: AppearanceAnchorSchema | None,
    ) -> CharacterVisualProfileProjection:
        if document is None:
            return CharacterVisualProfileProjection(
                exists=False,
                profile_id=None,
                owner_id=str(owner_id),
                novel_id=str(novel_id),
                character_card_id=str(character_card_id),
                appearance_anchor=appearance_anchor,
            )
        return CharacterVisualProfileProjection(
            exists=True,
            profile_id=str(document["_id"]),
            owner_id=str(document["owner_id"]),
            novel_id=str(document["novel_id"]),
            character_card_id=str(document["character_card_id"]),
            references=tuple(
                CharacterVisualReference(
                    **{
                        **reference,
                        "asset_id": str(reference["asset_id"]),
                    }
                )
                for reference in document.get("references") or []
            ),
            external_adapter=(
                ExternalLoraAdapter.model_validate(
                    document["external_adapter"]
                )
                if document.get("external_adapter") is not None
                else None
            ),
            appearance_anchor=appearance_anchor,
            revision=int(document.get("revision") or 0),
            created_at=document.get("created_at"),
            updated_at=document.get("updated_at"),
        )

    async def get(
        self,
        *,
        owner_id: str | ObjectId,
        novel_id: str | ObjectId,
        character_card_id: str | ObjectId,
    ) -> CharacterVisualProfileProjection:
        owner = _canonical_object_id(owner_id, field_name="owner_id")
        novel = _canonical_object_id(novel_id, field_name="novel_id")
        card = _canonical_object_id(
            character_card_id,
            field_name="character_card_id",
        )
        await self._require_scope(
            owner_id=owner,
            novel_id=novel,
            character_card_id=card,
        )
        document = await self._repository.get_active(
            owner_id=owner,
            novel_id=novel,
            character_card_id=card,
        )
        anchor = await self._appearance_anchor(
            novel_id=novel,
            character_card_id=card,
        )
        return self._project(
            document,
            owner_id=owner,
            novel_id=novel,
            character_card_id=card,
            appearance_anchor=anchor,
        )

    async def _validate_references(
        self,
        *,
        owner_id: ObjectId,
        novel_id: ObjectId,
        character_card_id: ObjectId,
        references: tuple[CharacterVisualReference, ...],
    ) -> list[dict[str, Any]]:
        stored: list[dict[str, Any]] = []
        for reference in references:
            asset_id = _canonical_object_id(
                reference.asset_id,
                field_name="asset_id",
            )
            asset = await self._assets.get_owned_subject_asset(
                owner_id=owner_id,
                novel_id=novel_id,
                asset_id=asset_id,
                subject_kind="character_portrait",
                subject_id=str(character_card_id),
            )
            if asset is None:
                raise ValueError(
                    "Every visual profile reference must be an active "
                    "character_portrait asset for this owner, novel, and card"
                )
            payload = reference.model_dump(mode="python")
            payload["asset_id"] = asset_id
            stored.append(payload)
        return stored

    async def replace(
        self,
        *,
        owner_id: str | ObjectId,
        novel_id: str | ObjectId,
        character_card_id: str | ObjectId,
        update: CharacterVisualProfileUpdate,
    ) -> CharacterVisualProfileProjection:
        owner = _canonical_object_id(owner_id, field_name="owner_id")
        novel = _canonical_object_id(novel_id, field_name="novel_id")
        card = _canonical_object_id(
            character_card_id,
            field_name="character_card_id",
        )
        await self._require_scope(
            owner_id=owner,
            novel_id=novel,
            character_card_id=card,
        )
        references = await self._validate_references(
            owner_id=owner,
            novel_id=novel,
            character_card_id=card,
            references=update.references,
        )
        adapter = (
            update.external_adapter.model_dump(mode="python")
            if update.external_adapter is not None
            else None
        )
        current = await self._repository.get_active(
            owner_id=owner,
            novel_id=novel,
            character_card_id=card,
        )
        if current is None:
            if update.expected_revision != 0:
                raise CharacterVisualProfileRevisionConflict(
                    "Character visual profile revision is stale"
                )
            try:
                stored = await self._repository.create_active(
                    {
                        "owner_id": owner,
                        "novel_id": novel,
                        "character_card_id": card,
                        "references": references,
                        "external_adapter": adapter,
                    }
                )
            except DuplicateKeyError as error:
                raise CharacterVisualProfileRevisionConflict(
                    "Character visual profile revision is stale"
                ) from error
        else:
            if int(current.get("revision") or 0) != update.expected_revision:
                raise CharacterVisualProfileRevisionConflict(
                    "Character visual profile revision is stale"
                )
            if (
                current.get("references") == references
                and current.get("external_adapter") == adapter
            ):
                stored = current
            else:
                stored = await self._repository.replace_if_revision(
                    owner_id=owner,
                    novel_id=novel,
                    character_card_id=card,
                    expected_revision=update.expected_revision,
                    references=references,
                    external_adapter=adapter,
                )
                if stored is None:
                    raise CharacterVisualProfileRevisionConflict(
                        "Character visual profile revision is stale"
                    )
        anchor = await self._appearance_anchor(
            novel_id=novel,
            character_card_id=card,
        )
        return self._project(
            stored,
            owner_id=owner,
            novel_id=novel,
            character_card_id=card,
            appearance_anchor=anchor,
        )

    async def add_reference(
        self,
        *,
        owner_id: str | ObjectId,
        novel_id: str | ObjectId,
        character_card_id: str | ObjectId,
        expected_revision: int,
        reference: CharacterVisualReference,
    ) -> CharacterVisualProfileProjection:
        current = await self.get(
            owner_id=owner_id,
            novel_id=novel_id,
            character_card_id=character_card_id,
        )
        return await self.replace(
            owner_id=owner_id,
            novel_id=novel_id,
            character_card_id=character_card_id,
            update=CharacterVisualProfileUpdate(
                expected_revision=expected_revision,
                references=(*current.references, reference),
                external_adapter=current.external_adapter,
            ),
        )

    async def remove_reference(
        self,
        *,
        owner_id: str | ObjectId,
        novel_id: str | ObjectId,
        character_card_id: str | ObjectId,
        asset_id: str | ObjectId,
        expected_revision: int,
    ) -> CharacterVisualProfileProjection:
        canonical_asset_id = str(
            _canonical_object_id(asset_id, field_name="asset_id")
        )
        current = await self.get(
            owner_id=owner_id,
            novel_id=novel_id,
            character_card_id=character_card_id,
        )
        references = tuple(
            reference
            for reference in current.references
            if reference.asset_id != canonical_asset_id
        )
        if len(references) == len(current.references):
            raise ValueError("Visual profile reference was not found")
        return await self.replace(
            owner_id=owner_id,
            novel_id=novel_id,
            character_card_id=character_card_id,
            update=CharacterVisualProfileUpdate(
                expected_revision=expected_revision,
                references=references,
                external_adapter=current.external_adapter,
            ),
        )

    async def set_external_adapter(
        self,
        *,
        owner_id: str | ObjectId,
        novel_id: str | ObjectId,
        character_card_id: str | ObjectId,
        expected_revision: int,
        external_adapter: ExternalLoraAdapter | None,
    ) -> CharacterVisualProfileProjection:
        current = await self.get(
            owner_id=owner_id,
            novel_id=novel_id,
            character_card_id=character_card_id,
        )
        return await self.replace(
            owner_id=owner_id,
            novel_id=novel_id,
            character_card_id=character_card_id,
            update=CharacterVisualProfileUpdate(
                expected_revision=expected_revision,
                references=current.references,
                external_adapter=external_adapter,
            ),
        )


character_visual_profile_service = CharacterVisualProfileService()


__all__ = [
    "MAX_CHARACTER_VISUAL_REFERENCES",
    "CharacterVisualProfileProjection",
    "CharacterVisualProfileRevisionConflict",
    "CharacterVisualProfileService",
    "CharacterVisualProfileUpdate",
    "CharacterVisualReference",
    "ExternalLoraAdapter",
    "character_visual_profile_service",
]
