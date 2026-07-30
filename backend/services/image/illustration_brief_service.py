"""Chapter illustration briefs captured from formal outline scenes.

This module is image-only. Reads compute drift without mutating snapshots,
and none of its writes advance the prose narrative revision.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Literal

from bson import ObjectId
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from backend.db.errors import InvalidIdError, NotFoundError
from backend.db.repositories.chapter_repository import ChapterRepository
from backend.db.repositories.illustration_brief_repository import (
    IllustrationBriefRepository,
    illustration_brief_repo,
)
from backend.db.repositories.image_asset_repository import (
    ImageAssetRepository,
    image_asset_repo,
)
from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.utils import to_object_id


MAX_SCENE_CHARACTER_CARD_IDS = 64
_PIPELINE_ALIAS = re.compile(r"^[A-Za-z0-9._-]{1,120}$")


class IllustrationBriefRevisionConflict(RuntimeError):
    """The caller edited an illustration brief from a stale revision."""


def _canonical_object_id(value: Any, *, field_name: str) -> ObjectId:
    if value is None or not str(value).strip():
        raise InvalidIdError(f"{field_name} must be a valid ObjectId")
    return to_object_id(str(value).strip())


def _optional_object_id(value: Any, *, field_name: str) -> ObjectId | None:
    if value is None or not str(value).strip():
        return None
    return _canonical_object_id(value, field_name=field_name)


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _mongo_utc_now() -> datetime:
    """Match MongoDB's millisecond precision before first projection."""

    now = datetime.now(timezone.utc)
    return now.replace(microsecond=(now.microsecond // 1000) * 1000)


def _as_utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _normalized_title(value: Any) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError("title must not be empty")
    return normalized


def _normalized_alias(value: Any) -> str | None:
    if value is None or not str(value).strip():
        return None
    normalized = str(value).strip()
    if not _PIPELINE_ALIAS.fullmatch(normalized):
        raise ValueError(
            "default_pipeline_alias must use only ASCII letters, digits, "
            "periods, underscores, and hyphens"
        )
    return normalized


def _canonical_card_ids(value: Any) -> tuple[str, ...]:
    values = tuple(value or ())
    if len(values) > MAX_SCENE_CHARACTER_CARD_IDS:
        raise ValueError(
            f"scene_character_card_ids must contain at most "
            f"{MAX_SCENE_CHARACTER_CARD_IDS} ids"
        )
    canonical: list[str] = []
    for card_id in values:
        try:
            canonical.append(
                str(
                    _canonical_object_id(
                        card_id,
                        field_name="scene_character_card_id",
                    )
                )
            )
        except InvalidIdError as error:
            raise ValueError(
                "scene_character_card_ids must contain valid ObjectIds"
            ) from error
    if len(canonical) != len(set(canonical)):
        raise ValueError("scene_character_card_ids contain duplicate ids")
    return tuple(canonical)


def _canonical_optional_card_id(value: Any) -> str | None:
    try:
        parsed = _optional_object_id(
            value,
            field_name="default_reference_character_card_id",
        )
    except InvalidIdError as error:
        raise ValueError(
            "default_reference_character_card_id must be a valid ObjectId"
        ) from error
    return str(parsed) if parsed is not None else None


class _CharacterSelectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scene_character_card_ids: tuple[str, ...] = Field(default_factory=tuple)
    default_reference_character_card_id: str | None = None

    @field_validator("scene_character_card_ids", mode="before")
    @classmethod
    def normalize_character_ids(cls, value: Any) -> tuple[str, ...]:
        return _canonical_card_ids(value)

    @field_validator(
        "default_reference_character_card_id",
        mode="before",
    )
    @classmethod
    def normalize_reference_id(cls, value: Any) -> str | None:
        return _canonical_optional_card_id(value)

    @model_validator(mode="after")
    def require_reference_in_scene(self):
        reference = self.default_reference_character_card_id
        if reference is not None and reference not in self.scene_character_card_ids:
            raise ValueError(
                "default reference character must be included in "
                "scene_character_card_ids"
            )
        return self


class IllustrationBriefCreate(_CharacterSelectionModel):
    title: str = Field(min_length=1, max_length=200)
    source_scene_index: int = Field(ge=0)
    default_pipeline_alias: str | None = None
    sort_order: int | None = Field(default=None, ge=0)

    @field_validator("title", mode="before")
    @classmethod
    def normalize_title(cls, value: Any) -> str:
        return _normalized_title(value)

    @field_validator("default_pipeline_alias", mode="before")
    @classmethod
    def normalize_alias(cls, value: Any) -> str | None:
        return _normalized_alias(value)


class IllustrationBriefPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_revision: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    scene_character_card_ids: tuple[str, ...] | None = None
    default_reference_character_card_id: str | None = None
    default_pipeline_alias: str | None = None
    status: Literal["active", "archived"] | None = None
    sort_order: int | None = Field(default=None, ge=0)

    @field_validator("title", mode="before")
    @classmethod
    def normalize_title(cls, value: Any) -> str | None:
        return None if value is None else _normalized_title(value)

    @field_validator("scene_character_card_ids", mode="before")
    @classmethod
    def normalize_character_ids(
        cls,
        value: Any,
    ) -> tuple[str, ...] | None:
        return None if value is None else _canonical_card_ids(value)

    @field_validator(
        "default_reference_character_card_id",
        mode="before",
    )
    @classmethod
    def normalize_reference_id(cls, value: Any) -> str | None:
        return _canonical_optional_card_id(value)

    @field_validator("default_pipeline_alias", mode="before")
    @classmethod
    def normalize_alias(cls, value: Any) -> str | None:
        return _normalized_alias(value)

    @model_validator(mode="after")
    def reject_null_for_non_clearable_fields(self):
        for field_name in (
            "title",
            "scene_character_card_ids",
            "status",
            "sort_order",
        ):
            if (
                field_name in self.model_fields_set
                and getattr(self, field_name) is None
            ):
                raise ValueError(f"{field_name} cannot be null")
        return self


class IllustrationBriefRefresh(_CharacterSelectionModel):
    expected_revision: int = Field(ge=1)
    source_scene_index: int = Field(ge=0)


class IllustrationSceneSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_scene_index: int = Field(ge=0)
    summary: str
    purpose: str
    source_outline_revision: str = Field(min_length=64, max_length=64)
    source_scene_fingerprint: str = Field(min_length=64, max_length=64)
    captured_at: datetime


class IllustrationFieldDiff(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    before: str | None
    after: str | None


class IllustrationBriefDiff(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    current_outline_revision: str
    outline_revision_changed: bool
    scene_missing: bool
    summary: IllustrationFieldDiff
    purpose: IllustrationFieldDiff
    characters_added: tuple[str, ...] = ()
    characters_removed: tuple[str, ...] = ()


class IllustrationBriefProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    exists: bool
    brief_id: str
    owner_id: str
    novel_id: str
    chapter_id: str
    title: str
    scene_snapshot: IllustrationSceneSnapshot
    scene_character_card_ids: tuple[str, ...] = ()
    default_reference_character_card_id: str | None = None
    default_pipeline_alias: str | None = None
    current_asset_id: str | None = None
    status: Literal["active", "archived"]
    sort_order: int
    revision: int
    stale: bool
    diff: IllustrationBriefDiff | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class IllustrationBriefListProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    data: tuple[IllustrationBriefProjection, ...]


class LegacyAssetAdoptionProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    brief_id: str
    source_asset_id: str
    association_asset_id: str
    physical_bytes_reused: bool


class IllustrationBriefService:
    def __init__(
        self,
        *,
        repository: IllustrationBriefRepository | None = None,
        assets: ImageAssetRepository | None = None,
        chapters: ChapterRepository | None = None,
    ) -> None:
        self._repository = repository or illustration_brief_repo
        self._assets = assets or image_asset_repo
        self._chapters = chapters or chapter_repo

    async def _require_scope(
        self,
        *,
        owner_id: ObjectId,
        novel_id: ObjectId,
        chapter_id: ObjectId,
    ) -> tuple[dict[str, Any], dict[str, Any], tuple[ObjectId, ...]]:
        if not await self._assets.novel_belongs_to_owner(
            owner_id=owner_id,
            novel_id=novel_id,
        ):
            raise NotFoundError("Novel or chapter was not found")
        chapter = await self._chapters.get_chapter_by_id(str(chapter_id))
        if chapter.get("novel_id") != novel_id:
            raise NotFoundError("Novel or chapter was not found")
        outline = chapter.get("outline")
        if not isinstance(outline, dict):
            raise ValueError(
                "Chapter outline is required before creating illustration briefs"
            )
        scenes = outline.get("scenes")
        if not isinstance(scenes, list) or not scenes:
            raise ValueError("Chapter outline must contain at least one scene")
        declared = _canonical_card_ids(
            outline.get("present_character_card_ids") or ()
        )
        return chapter, outline, tuple(ObjectId(value) for value in declared)

    @staticmethod
    def _validate_selection(
        *,
        declared: tuple[ObjectId, ...],
        scene_character_card_ids: tuple[str, ...],
        default_reference_character_card_id: str | None,
    ) -> tuple[list[ObjectId], ObjectId | None]:
        selected = [ObjectId(value) for value in scene_character_card_ids]
        declared_set = set(declared)
        if any(value not in declared_set for value in selected):
            raise ValueError(
                "Every scene character must be declared by the chapter outline"
            )
        reference = (
            ObjectId(default_reference_character_card_id)
            if default_reference_character_card_id is not None
            else None
        )
        if reference is not None and reference not in selected:
            raise ValueError(
                "The default reference character must be selected for the scene"
            )
        return selected, reference

    @staticmethod
    def _capture_snapshot(
        *,
        outline: dict[str, Any],
        source_scene_index: int,
    ) -> dict[str, Any]:
        scenes = outline["scenes"]
        if source_scene_index >= len(scenes):
            raise ValueError("source_scene_index does not exist in the outline")
        scene = scenes[source_scene_index]
        if not isinstance(scene, dict):
            raise ValueError("The selected outline scene is invalid")
        summary = str(scene.get("summary") or "").strip()
        purpose = str(scene.get("purpose") or "").strip()
        now = _mongo_utc_now()
        return {
            "source_scene_index": source_scene_index,
            "summary": summary,
            "purpose": purpose,
            "source_outline_revision": _canonical_digest(outline),
            "source_scene_fingerprint": _canonical_digest(
                {"summary": summary, "purpose": purpose}
            ),
            "captured_at": now,
        }

    @staticmethod
    def _project(
        document: dict[str, Any],
        *,
        outline: dict[str, Any],
    ) -> IllustrationBriefProjection:
        snapshot = document["scene_snapshot"]
        current_outline_revision = _canonical_digest(outline)
        outline_changed = (
            current_outline_revision
            != snapshot["source_outline_revision"]
        )
        scene_index = int(snapshot["source_scene_index"])
        scenes = outline.get("scenes") or []
        scene_missing = scene_index >= len(scenes)
        current_scene = None if scene_missing else scenes[scene_index]
        if not isinstance(current_scene, dict):
            scene_missing = True
            current_scene = {}
        current_summary = (
            None
            if scene_missing
            else str(current_scene.get("summary") or "").strip()
        )
        current_purpose = (
            None
            if scene_missing
            else str(current_scene.get("purpose") or "").strip()
        )
        stored_characters = tuple(
            str(value)
            for value in document.get("scene_character_card_ids") or ()
        )
        current_declared = tuple(
            str(value)
            for value in outline.get("present_character_card_ids") or ()
        )
        stale = outline_changed or scene_missing
        diff = None
        if stale:
            stored_set = set(stored_characters)
            declared_set = set(current_declared)
            diff = IllustrationBriefDiff(
                current_outline_revision=current_outline_revision,
                outline_revision_changed=outline_changed,
                scene_missing=scene_missing,
                summary=IllustrationFieldDiff(
                    before=str(snapshot.get("summary") or ""),
                    after=current_summary,
                ),
                purpose=IllustrationFieldDiff(
                    before=str(snapshot.get("purpose") or ""),
                    after=current_purpose,
                ),
                characters_added=tuple(
                    value
                    for value in current_declared
                    if value not in stored_set
                ),
                characters_removed=tuple(
                    value
                    for value in stored_characters
                    if value not in declared_set
                ),
            )
        reference = document.get("default_reference_character_card_id")
        current_asset = document.get("current_asset_id")
        projected_snapshot = dict(snapshot)
        projected_snapshot["captured_at"] = _as_utc_datetime(
            snapshot["captured_at"]
        )
        return IllustrationBriefProjection(
            exists=True,
            brief_id=str(document["_id"]),
            owner_id=str(document["owner_id"]),
            novel_id=str(document["novel_id"]),
            chapter_id=str(document["chapter_id"]),
            title=str(document["title"]),
            scene_snapshot=IllustrationSceneSnapshot.model_validate(
                projected_snapshot
            ),
            scene_character_card_ids=stored_characters,
            default_reference_character_card_id=(
                str(reference) if reference is not None else None
            ),
            default_pipeline_alias=document.get("default_pipeline_alias"),
            current_asset_id=(
                str(current_asset) if current_asset is not None else None
            ),
            status=document.get("status", "active"),
            sort_order=int(document.get("sort_order") or 0),
            revision=int(document.get("revision") or 0),
            stale=stale,
            diff=diff,
            created_at=document.get("created_at"),
            updated_at=document.get("updated_at"),
        )

    async def create_brief(
        self,
        *,
        owner_id: str | ObjectId,
        novel_id: str | ObjectId,
        chapter_id: str | ObjectId,
        request: IllustrationBriefCreate,
    ) -> IllustrationBriefProjection:
        owner = _canonical_object_id(owner_id, field_name="owner_id")
        novel = _canonical_object_id(novel_id, field_name="novel_id")
        chapter = _canonical_object_id(chapter_id, field_name="chapter_id")
        _chapter, outline, declared = await self._require_scope(
            owner_id=owner,
            novel_id=novel,
            chapter_id=chapter,
        )
        characters, reference = self._validate_selection(
            declared=declared,
            scene_character_card_ids=request.scene_character_card_ids,
            default_reference_character_card_id=(
                request.default_reference_character_card_id
            ),
        )
        snapshot = self._capture_snapshot(
            outline=outline,
            source_scene_index=request.source_scene_index,
        )
        sort_order = request.sort_order
        if sort_order is None:
            sort_order = await self._repository.next_sort_order(
                owner_id=owner,
                novel_id=novel,
                chapter_id=chapter,
            )
        stored = await self._repository.create_active(
            {
                "owner_id": owner,
                "novel_id": novel,
                "chapter_id": chapter,
                "title": request.title,
                "scene_snapshot": snapshot,
                "scene_character_card_ids": characters,
                "default_reference_character_card_id": reference,
                "default_pipeline_alias": request.default_pipeline_alias,
                "current_asset_id": None,
                "status": "active",
                "sort_order": sort_order,
            }
        )
        return self._project(stored, outline=outline)

    async def list_briefs(
        self,
        *,
        owner_id: str | ObjectId,
        novel_id: str | ObjectId,
        chapter_id: str | ObjectId,
        include_archived: bool = False,
    ) -> IllustrationBriefListProjection:
        owner = _canonical_object_id(owner_id, field_name="owner_id")
        novel = _canonical_object_id(novel_id, field_name="novel_id")
        chapter = _canonical_object_id(chapter_id, field_name="chapter_id")
        _chapter, outline, _declared = await self._require_scope(
            owner_id=owner,
            novel_id=novel,
            chapter_id=chapter,
        )
        documents = await self._repository.list_for_chapter(
            owner_id=owner,
            novel_id=novel,
            chapter_id=chapter,
            include_archived=include_archived,
        )
        return IllustrationBriefListProjection(
            data=tuple(
                self._project(document, outline=outline)
                for document in documents
            )
        )

    async def _current(
        self,
        *,
        owner_id: ObjectId,
        novel_id: ObjectId,
        chapter_id: ObjectId,
        brief_id: ObjectId,
    ) -> dict[str, Any]:
        current = await self._repository.get_owned(
            owner_id=owner_id,
            novel_id=novel_id,
            chapter_id=chapter_id,
            brief_id=brief_id,
        )
        if current is None:
            raise NotFoundError("Illustration brief was not found")
        return current

    async def patch_brief(
        self,
        *,
        owner_id: str | ObjectId,
        novel_id: str | ObjectId,
        chapter_id: str | ObjectId,
        brief_id: str | ObjectId,
        request: IllustrationBriefPatch,
    ) -> IllustrationBriefProjection:
        owner = _canonical_object_id(owner_id, field_name="owner_id")
        novel = _canonical_object_id(novel_id, field_name="novel_id")
        chapter = _canonical_object_id(chapter_id, field_name="chapter_id")
        brief = _canonical_object_id(brief_id, field_name="brief_id")
        _chapter, outline, declared = await self._require_scope(
            owner_id=owner,
            novel_id=novel,
            chapter_id=chapter,
        )
        current = await self._current(
            owner_id=owner,
            novel_id=novel,
            chapter_id=chapter,
            brief_id=brief,
        )
        if int(current.get("revision") or 0) != request.expected_revision:
            raise IllustrationBriefRevisionConflict(
                "Illustration brief revision is stale"
            )
        fields = request.model_fields_set - {"expected_revision"}
        changes: dict[str, Any] = {}
        for field_name in fields:
            value = getattr(request, field_name)
            if field_name == "scene_character_card_ids":
                changes[field_name] = [ObjectId(item) for item in (value or ())]
            elif field_name == "default_reference_character_card_id":
                changes[field_name] = (
                    ObjectId(value) if value is not None else None
                )
            else:
                changes[field_name] = value
        final_characters = tuple(
            str(value)
            for value in changes.get(
                "scene_character_card_ids",
                current.get("scene_character_card_ids") or (),
            )
        )
        final_reference_value = changes.get(
            "default_reference_character_card_id",
            current.get("default_reference_character_card_id"),
        )
        final_reference = (
            str(final_reference_value)
            if final_reference_value is not None
            else None
        )
        self._validate_selection(
            declared=declared,
            scene_character_card_ids=final_characters,
            default_reference_character_card_id=final_reference,
        )
        if all(current.get(key) == value for key, value in changes.items()):
            stored = current
        else:
            stored = await self._repository.patch_if_revision(
                owner_id=owner,
                novel_id=novel,
                chapter_id=chapter,
                brief_id=brief,
                expected_revision=request.expected_revision,
                changes=changes,
            )
            if stored is None:
                raise IllustrationBriefRevisionConflict(
                    "Illustration brief revision is stale"
                )
        return self._project(stored, outline=outline)

    async def update_brief_from_outline(
        self,
        *,
        owner_id: str | ObjectId,
        novel_id: str | ObjectId,
        chapter_id: str | ObjectId,
        brief_id: str | ObjectId,
        request: IllustrationBriefRefresh,
    ) -> IllustrationBriefProjection:
        owner = _canonical_object_id(owner_id, field_name="owner_id")
        novel = _canonical_object_id(novel_id, field_name="novel_id")
        chapter = _canonical_object_id(chapter_id, field_name="chapter_id")
        brief = _canonical_object_id(brief_id, field_name="brief_id")
        _chapter, outline, declared = await self._require_scope(
            owner_id=owner,
            novel_id=novel,
            chapter_id=chapter,
        )
        current = await self._current(
            owner_id=owner,
            novel_id=novel,
            chapter_id=chapter,
            brief_id=brief,
        )
        if int(current.get("revision") or 0) != request.expected_revision:
            raise IllustrationBriefRevisionConflict(
                "Illustration brief revision is stale"
            )
        characters, reference = self._validate_selection(
            declared=declared,
            scene_character_card_ids=request.scene_character_card_ids,
            default_reference_character_card_id=(
                request.default_reference_character_card_id
            ),
        )
        snapshot = self._capture_snapshot(
            outline=outline,
            source_scene_index=request.source_scene_index,
        )
        stored = await self._repository.patch_if_revision(
            owner_id=owner,
            novel_id=novel,
            chapter_id=chapter,
            brief_id=brief,
            expected_revision=request.expected_revision,
            changes={
                "scene_snapshot": snapshot,
                "scene_character_card_ids": characters,
                "default_reference_character_card_id": reference,
            },
        )
        if stored is None:
            raise IllustrationBriefRevisionConflict(
                "Illustration brief revision is stale"
            )
        return self._project(stored, outline=outline)

    async def adopt_legacy_asset(
        self,
        *,
        owner_id: str | ObjectId,
        novel_id: str | ObjectId,
        chapter_id: str | ObjectId,
        brief_id: str | ObjectId,
        asset_id: str | ObjectId,
        expected_revision: int,
    ) -> LegacyAssetAdoptionProjection:
        owner = _canonical_object_id(owner_id, field_name="owner_id")
        novel = _canonical_object_id(novel_id, field_name="novel_id")
        chapter = _canonical_object_id(chapter_id, field_name="chapter_id")
        brief = _canonical_object_id(brief_id, field_name="brief_id")
        asset = _canonical_object_id(asset_id, field_name="asset_id")
        await self._require_scope(
            owner_id=owner,
            novel_id=novel,
            chapter_id=chapter,
        )
        current = await self._current(
            owner_id=owner,
            novel_id=novel,
            chapter_id=chapter,
            brief_id=brief,
        )
        if int(current.get("revision") or 0) != expected_revision:
            raise IllustrationBriefRevisionConflict(
                "Illustration brief revision is stale"
            )
        source = await self._assets.get_owned_subject_asset(
            owner_id=owner,
            novel_id=novel,
            asset_id=asset,
            subject_kind="scene_illustration",
            subject_id=str(chapter),
        )
        if source is None or source.get("legacy_source_asset_id") is not None:
            raise ValueError(
                "The legacy scene illustration is not available in this scope"
            )
        fingerprint = _canonical_digest(
            {
                "kind": "illustration_brief_legacy_adoption_v1",
                "owner_id": str(owner),
                "novel_id": str(novel),
                "brief_id": str(brief),
                "source_asset_id": str(asset),
            }
        )
        association = {
            key: value
            for key, value in source.items()
            if key
            not in {
                "_id",
                "created_at",
                "updated_at",
                "is_deleted",
                "deleted_at",
                "metadata_fingerprint",
            }
        }
        association.update(
            {
                "owner_id": owner,
                "novel_id": novel,
                "subject_kind": "illustration_brief",
                "subject_id": str(brief),
                "legacy_source_asset_id": asset,
                "metadata_fingerprint": fingerprint,
            }
        )
        stored = await self._assets.upsert_metadata(association)
        return LegacyAssetAdoptionProjection(
            brief_id=str(brief),
            source_asset_id=str(asset),
            association_asset_id=str(stored["_id"]),
            physical_bytes_reused=True,
        )


illustration_brief_service = IllustrationBriefService()
