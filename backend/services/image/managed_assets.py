"""Owner-scoped, content-addressed storage for generated and imported images."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

from bson import ObjectId
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from backend.db.repositories.image_asset_repository import ImageAssetRepository
from backend.db.utils import to_object_id
from backend.services.image.contracts import (
    ImageFailure,
    ImagePollResult,
    ImagePollStatus,
)
from backend.services.image.image_probe import (
    InvalidImageError,
    ProbedImage,
    probe_image,
)
from backend.services.image.illustration_lineage import (
    IllustrationAssetLineage,
    IllustrationCandidateState,
    IllustrationPipelineStage,
)


MANAGED_IMAGE_ASSET_ROOT = (
    Path(__file__).resolve().parents[3] / "managed-assets" / "images"
)
_CONTENT_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ASSET_FILENAME_PATTERN = re.compile(
    r"^(?P<content_hash>[0-9a-f]{64})(?P<extension>\.(?:png|jpg|gif|webp))$"
)
_ALLOWED_EXTENSIONS = frozenset({".png", ".jpg", ".gif", ".webp"})
_MAX_REQUEST_PARAMS_JSON_BYTES = 64 * 1024

ImageSubjectKind = Literal[
    "character_portrait",
    "cover",
    "scene_illustration",
]


class ImageAssetError(RuntimeError):
    """Base error for the managed image boundary."""


class ImageAssetNotFoundError(ImageAssetError):
    """The requested owner-scoped asset is not visible or does not exist."""


class ImageAssetIntegrityError(ImageAssetError):
    """The content-addressed path does not contain the expected bytes."""


class InvalidImageAssetError(ImageAssetError):
    """The save request cannot be represented safely in managed storage."""


class _AssetCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    owner_id: str
    novel_id: str
    subject_kind: ImageSubjectKind
    subject_id: str = Field(min_length=1, max_length=512)
    illustration_lineage: IllustrationAssetLineage | None = None

    @field_validator("owner_id", "novel_id", mode="before")
    @classmethod
    def validate_path_id(cls, value: Any) -> str:
        if value is None:
            raise ValueError("owner_id and novel_id must be existing ObjectIds")
        try:
            return str(to_object_id(value))
        except Exception as exc:
            raise ValueError(
                "owner_id and novel_id must be existing ObjectIds"
            ) from exc

    @model_validator(mode="after")
    def validate_illustration_scope(self) -> "_AssetCommand":
        lineage = self.illustration_lineage
        if lineage is None:
            return self
        if self.subject_kind != "scene_illustration":
            raise ValueError(
                "staged illustration lineage requires scene_illustration"
            )
        try:
            subject_id = str(to_object_id(self.subject_id))
        except Exception as exc:
            raise ValueError(
                "staged illustration subject_id must be an existing ObjectId"
            ) from exc
        if subject_id != lineage.illustration_brief_id:
            raise ValueError(
                "staged illustration subject_id must equal illustration_brief_id"
            )
        return self


def _validate_json_metadata(value: Any, *, path: str = "request_params") -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must contain finite JSON numbers")
        return value
    if isinstance(value, str):
        if value.lstrip().lower().startswith("data:"):
            raise ValueError(
                f"{path} must not embed image content as a data URL"
            )
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise ValueError(f"{path} must not contain raw image content")
    if isinstance(value, (list, tuple)):
        return [
            _validate_json_metadata(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} keys must be strings")
            normalized[key] = _validate_json_metadata(
                item,
                path=f"{path}.{key}",
            )
        return normalized
    raise ValueError(f"{path} must contain only JSON metadata, not image content")


def _normalize_request_params(value: Any) -> dict[str, Any]:
    normalized = _validate_json_metadata(value)
    if not isinstance(normalized, dict):
        raise ValueError("request_params must be an object")
    serialized = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(serialized) > _MAX_REQUEST_PARAMS_JSON_BYTES:
        raise ValueError(
            "request_params metadata exceeds the 64 KiB storage limit"
        )
    return normalized


class GeneratedImageAssetCreate(_AssetCommand):
    source: Literal["generated"] = "generated"
    provider_alias: str = Field(min_length=1, max_length=200)
    model: str = Field(min_length=1, max_length=500)
    request_params: dict[str, Any] = Field(default_factory=dict)
    final_prompt: str = Field(min_length=1)
    negative_prompt: str | None = None
    seed: int
    revised_prompt: str | None = None

    @field_validator("request_params", mode="before")
    @classmethod
    def validate_request_params(cls, value: Any) -> dict[str, Any]:
        return _normalize_request_params(value)

    @field_validator("seed", mode="before")
    @classmethod
    def validate_seed(cls, value: Any) -> int:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= (2**64 - 1)
        ):
            raise ValueError("seed must be an integer from 0 through 2^64-1")
        return value

    @model_validator(mode="after")
    def reject_external_import_stage(self) -> "GeneratedImageAssetCreate":
        if (
            self.illustration_lineage is not None
            and self.illustration_lineage.pipeline_stage == "external_import"
        ):
            raise ValueError("external_import assets must use imported source")
        return self


class ImportedImageAssetCreate(_AssetCommand):
    source: Literal["imported"] = "imported"
    external_import_target_stage: Literal[
        "compose",
        "identity_edit",
        "refine",
    ] | None = None

    @model_validator(mode="after")
    def validate_external_import_stage(self) -> "ImportedImageAssetCreate":
        lineage = self.illustration_lineage
        if lineage is None:
            if self.external_import_target_stage is not None:
                raise ValueError(
                    "external_import_target_stage requires staged lineage"
                )
            return self
        if lineage.pipeline_stage != "external_import":
            raise ValueError(
                "staged imported assets require external_import stage"
            )
        if self.external_import_target_stage is None:
            raise ValueError(
                "staged imported assets require external_import_target_stage"
            )
        return self


ImageAssetCreate = GeneratedImageAssetCreate | ImportedImageAssetCreate


class ImageAssetRecord(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    asset_id: str
    owner_id: str
    novel_id: str
    subject_kind: ImageSubjectKind
    subject_id: str
    content_hash: str
    relative_path: str
    mime: str
    width: int
    height: int
    byte_size: int
    provider_alias: str | None
    model: str | None
    request_params: dict[str, Any]
    final_prompt: str | None
    negative_prompt: str | None
    seed: int | None
    revised_prompt: str | None
    source: Literal["generated", "imported"]
    illustration_brief_id: str | None = None
    illustration_run_id: str | None = None
    pipeline_stage: IllustrationPipelineStage | None = None
    external_import_target_stage: Literal[
        "compose",
        "identity_edit",
        "refine",
    ] | None = None
    derived_from_asset_id: str | None = None
    candidate_state: IllustrationCandidateState | None = None
    discarded_at: datetime | None = None
    discard_reason: str | None = None
    created_at: datetime | None = None


class ImageAssetRepositoryProtocol(Protocol):
    async def novel_belongs_to_owner(
        self,
        *,
        owner_id: ObjectId,
        novel_id: ObjectId,
    ) -> bool: ...

    async def upsert_metadata(
        self,
        document: dict[str, Any],
    ) -> dict[str, Any]: ...

    async def get_owned_by_id(
        self,
        *,
        owner_id: ObjectId,
        asset_id: ObjectId,
    ) -> dict[str, Any] | None: ...

    async def get_owned_by_relative_path(
        self,
        *,
        owner_id: ObjectId,
        relative_path: str,
    ) -> dict[str, Any] | None: ...


def _require_content_hash(value: str) -> str:
    if not _CONTENT_HASH_PATTERN.fullmatch(value):
        raise InvalidImageAssetError(
            "content_hash must be exactly 64 lowercase hexadecimal characters"
        )
    return value


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(slots=True)
class _PathLockEntry:
    lock: Any
    users: int


_PATH_LOCK_REGISTRY_GUARD = threading.Lock()
_PATH_LOCK_REGISTRY: dict[str, _PathLockEntry] = {}


@contextmanager
def _serialize_target_writes(target: Path):
    """Serialize same-path writes across service instances in this process."""

    key = os.path.normcase(os.path.abspath(target))
    with _PATH_LOCK_REGISTRY_GUARD:
        entry = _PATH_LOCK_REGISTRY.get(key)
        if entry is None:
            entry = _PathLockEntry(lock=threading.Lock(), users=0)
            _PATH_LOCK_REGISTRY[key] = entry
        entry.users += 1

    entry.lock.acquire()
    try:
        yield
    finally:
        entry.lock.release()
        with _PATH_LOCK_REGISTRY_GUARD:
            entry.users -= 1
            if entry.users == 0:
                _PATH_LOCK_REGISTRY.pop(key, None)


def _resolved_path_is_within(*, root: Path, target: Path) -> bool:
    resolved_root = root.resolve()
    resolved_target = target.resolve(strict=False)
    normalized_root = os.path.normcase(str(resolved_root))
    normalized_target = os.path.normcase(str(resolved_target))
    try:
        return os.path.commonpath(
            [normalized_root, normalized_target]
        ) == normalized_root
    except ValueError:
        return False


def _ensure_content_file(
    *,
    root: Path,
    target: Path,
    content: bytes,
    content_hash: str,
) -> None:
    with _serialize_target_writes(target):
        if not _resolved_path_is_within(root=root, target=target):
            raise InvalidImageAssetError(
                "Managed image path resolves outside the asset root"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        if not _resolved_path_is_within(root=root, target=target):
            raise InvalidImageAssetError(
                "Managed image path resolves outside the asset root"
            )

        if target.exists():
            if not target.is_file() or _hash_file(target) != content_hash:
                raise ImageAssetIntegrityError(
                    "Existing content-hash path has a hash mismatch"
                )
            return

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{content_hash}.",
            suffix=".tmp",
            dir=target.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            if _hash_file(temporary) != content_hash:
                raise ImageAssetIntegrityError(
                    "Temporary image bytes do not match the computed content hash"
                )
            if not _resolved_path_is_within(root=root, target=target):
                raise InvalidImageAssetError(
                    "Managed image path resolves outside the asset root"
                )
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)


class ManagedImageAssetService:
    """Save and read image bytes without exposing arbitrary filesystem paths."""

    def __init__(
        self,
        *,
        root: str | os.PathLike[str] | Path = MANAGED_IMAGE_ASSET_ROOT,
        repository: ImageAssetRepositoryProtocol | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.repository = repository or ImageAssetRepository()

    @staticmethod
    def _relative_path(
        *,
        owner_id: str,
        novel_id: str,
        content_hash: str,
        detected: ProbedImage,
    ) -> str:
        _require_content_hash(content_hash)
        if detected.extension not in _ALLOWED_EXTENSIONS:
            raise InvalidImageAssetError("Unsupported managed image extension")
        return PurePosixPath(
            "owners",
            owner_id,
            "novels",
            novel_id,
            f"{content_hash}{detected.extension}",
        ).as_posix()

    def _resolve_relative(self, relative_path: str) -> Path:
        if "\\" in relative_path:
            raise InvalidImageAssetError("Managed paths must use '/' separators")
        pure = PurePosixPath(relative_path)
        if pure.is_absolute() or ".." in pure.parts or "." in pure.parts:
            raise InvalidImageAssetError("Managed path traversal is not allowed")
        target = self.root.joinpath(*pure.parts)
        normalized_root = os.path.normcase(os.path.abspath(self.root))
        normalized_target = os.path.normcase(os.path.abspath(target))
        try:
            inside_root = (
                os.path.commonpath([normalized_root, normalized_target])
                == normalized_root
            )
        except ValueError:
            inside_root = False
        if not inside_root:
            raise InvalidImageAssetError(
                "Managed image path resolves outside the asset root"
            )
        return target

    @staticmethod
    def _metadata_fingerprint(document: dict[str, Any]) -> str:
        stable = {
            key: (
                str(value)
                if isinstance(value, ObjectId)
                else value
            )
            for key, value in document.items()
        }
        encoded = json.dumps(
            stable,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _record(document: dict[str, Any]) -> ImageAssetRecord:
        return ImageAssetRecord(
            asset_id=str(document["_id"]),
            owner_id=str(document["owner_id"]),
            novel_id=str(document["novel_id"]),
            subject_kind=document["subject_kind"],
            subject_id=document["subject_id"],
            content_hash=document["content_hash"],
            relative_path=document["relative_path"],
            mime=document["mime"],
            width=document["width"],
            height=document["height"],
            byte_size=document["byte_size"],
            provider_alias=document.get("provider_alias"),
            model=document.get("model"),
            request_params=dict(document.get("request_params") or {}),
            final_prompt=document.get("final_prompt"),
            negative_prompt=document.get("negative_prompt"),
            seed=document.get("seed"),
            revised_prompt=document.get("revised_prompt"),
            source=document["source"],
            illustration_brief_id=(
                str(document["illustration_brief_id"])
                if document.get("illustration_brief_id") is not None
                else None
            ),
            illustration_run_id=(
                str(document["illustration_run_id"])
                if document.get("illustration_run_id") is not None
                else None
            ),
            pipeline_stage=document.get("pipeline_stage"),
            external_import_target_stage=document.get(
                "external_import_target_stage"
            ),
            derived_from_asset_id=(
                str(document["derived_from_asset_id"])
                if document.get("derived_from_asset_id") is not None
                else None
            ),
            candidate_state=document.get("candidate_state"),
            discarded_at=document.get("discarded_at"),
            discard_reason=document.get("discard_reason"),
            created_at=document.get("created_at"),
        )

    async def put(
        self,
        *,
        content: bytes,
        command: ImageAssetCreate,
    ) -> ImageAssetRecord:
        if not isinstance(content, bytes) or not content:
            raise InvalidImageAssetError("Managed image content must be non-empty bytes")

        safe_request_params: dict[str, Any] | None = None
        if isinstance(command, GeneratedImageAssetCreate):
            try:
                safe_request_params = _normalize_request_params(
                    command.request_params
                )
            except ValueError as exc:
                raise InvalidImageAssetError(
                    f"Unsafe request_params metadata: {exc}"
                ) from exc

        owner_object_id = to_object_id(command.owner_id)
        novel_object_id = to_object_id(command.novel_id)
        if not await self.repository.novel_belongs_to_owner(
            owner_id=owner_object_id,
            novel_id=novel_object_id,
        ):
            raise ImageAssetNotFoundError("Owned novel not found")

        try:
            detected = await asyncio.to_thread(probe_image, content)
        except InvalidImageError as exc:
            raise InvalidImageAssetError(
                f"Content is not a valid supported image: {exc}"
            ) from exc
        content_hash = _require_content_hash(
            hashlib.sha256(content).hexdigest()
        )
        relative_path = self._relative_path(
            owner_id=command.owner_id,
            novel_id=command.novel_id,
            content_hash=content_hash,
            detected=detected,
        )
        target = self._resolve_relative(relative_path)
        await asyncio.to_thread(
            _ensure_content_file,
            root=self.root,
            target=target,
            content=content,
            content_hash=content_hash,
        )

        if isinstance(command, GeneratedImageAssetCreate):
            assert safe_request_params is not None
            generation_metadata = {
                "provider_alias": command.provider_alias,
                "model": command.model,
                "request_params": safe_request_params,
                "final_prompt": command.final_prompt,
                "negative_prompt": command.negative_prompt,
                # MongoDB/BSON integers are signed int64. Persist the complete
                # uint64 generation seed as exact canonical decimal text.
                "seed": str(command.seed),
                "revised_prompt": command.revised_prompt,
            }
        else:
            generation_metadata = {
                "provider_alias": None,
                "model": None,
                "request_params": {},
                "final_prompt": None,
                "negative_prompt": None,
                "seed": None,
                "revised_prompt": None,
            }
        document: dict[str, Any] = {
            "owner_id": owner_object_id,
            "novel_id": novel_object_id,
            "subject_kind": command.subject_kind,
            "subject_id": command.subject_id,
            "content_hash": content_hash,
            "relative_path": relative_path,
            "mime": detected.mime,
            "width": detected.width,
            "height": detected.height,
            "byte_size": len(content),
            **generation_metadata,
            "source": command.source,
        }
        if command.illustration_lineage is not None:
            lineage = command.illustration_lineage.persisted_fields()
            document.update(
                {
                    "illustration_brief_id": to_object_id(
                        lineage["illustration_brief_id"]
                    ),
                    "illustration_run_id": to_object_id(
                        lineage["illustration_run_id"]
                    ),
                    "pipeline_stage": lineage["pipeline_stage"],
                    "candidate_state": "available",
                    "discarded_at": None,
                    "discard_reason": None,
                }
            )
            if (
                isinstance(command, ImportedImageAssetCreate)
                and command.external_import_target_stage is not None
            ):
                document["external_import_target_stage"] = (
                    command.external_import_target_stage
                )
            if lineage.get("derived_from_asset_id") is not None:
                document["derived_from_asset_id"] = to_object_id(
                    lineage["derived_from_asset_id"]
                )
        document["metadata_fingerprint"] = self._metadata_fingerprint(document)
        stored = await self.repository.upsert_metadata(document)
        return self._record(stored)

    @staticmethod
    def _parse_owned_relative_path(
        *,
        owner_id: str,
        relative_path: str,
    ) -> tuple[str, str]:
        if "\\" in relative_path:
            raise ImageAssetNotFoundError("Image asset not found")
        pure = PurePosixPath(relative_path)
        if pure.is_absolute() or len(pure.parts) != 5:
            raise ImageAssetNotFoundError("Image asset not found")
        if pure.parts[0] != "owners" or pure.parts[2] != "novels":
            raise ImageAssetNotFoundError("Image asset not found")
        try:
            path_owner_id = str(to_object_id(pure.parts[1]))
            novel_id = str(to_object_id(pure.parts[3]))
        except Exception as exc:
            raise ImageAssetNotFoundError("Image asset not found") from exc
        if path_owner_id != owner_id or pure.parts[1] != path_owner_id:
            raise ImageAssetNotFoundError("Image asset not found")
        match = _ASSET_FILENAME_PATTERN.fullmatch(pure.parts[4])
        if match is None:
            raise ImageAssetNotFoundError("Image asset not found")
        return novel_id, match.group("content_hash")

    async def read_owned_path(
        self,
        *,
        owner_id: str,
        relative_path: str,
    ) -> bytes:
        try:
            canonical_owner_id = str(to_object_id(owner_id))
        except Exception as exc:
            raise ImageAssetNotFoundError("Image asset not found") from exc
        novel_id, content_hash = self._parse_owned_relative_path(
            owner_id=canonical_owner_id,
            relative_path=relative_path,
        )
        document = await self.repository.get_owned_by_relative_path(
            owner_id=to_object_id(canonical_owner_id),
            relative_path=relative_path,
        )
        if (
            document is None
            or str(document.get("novel_id")) != novel_id
            or document.get("content_hash") != content_hash
        ):
            raise ImageAssetNotFoundError("Image asset not found")
        try:
            target = self._resolve_relative(relative_path)
        except InvalidImageAssetError as exc:
            raise ImageAssetNotFoundError("Image asset not found") from exc
        if not _resolved_path_is_within(root=self.root, target=target):
            raise ImageAssetNotFoundError("Image asset not found")
        if not target.is_file():
            raise ImageAssetNotFoundError("Image asset not found")
        content = await asyncio.to_thread(target.read_bytes)
        if hashlib.sha256(content).hexdigest() != content_hash:
            raise ImageAssetIntegrityError(
                "Stored image does not match its content hash"
            )
        return content

    async def read_owned_asset(
        self,
        *,
        owner_id: str,
        asset_id: str,
    ) -> bytes:
        try:
            owner_object_id = to_object_id(owner_id)
            asset_object_id = to_object_id(asset_id)
        except Exception as exc:
            raise ImageAssetNotFoundError("Image asset not found") from exc
        document = await self.repository.get_owned_by_id(
            owner_id=owner_object_id,
            asset_id=asset_object_id,
        )
        if document is None:
            raise ImageAssetNotFoundError("Image asset not found")
        return await self.read_owned_path(
            owner_id=str(owner_object_id),
            relative_path=str(document["relative_path"]),
        )


class ImageAssetWriterProtocol(Protocol):
    async def put(
        self,
        *,
        content: bytes,
        command: ImageAssetCreate,
    ) -> ImageAssetRecord: ...


@dataclass(frozen=True, slots=True)
class ImagePollAssetConsumption:
    """Result of interpreting one poll response at the asset boundary."""

    status: ImagePollStatus
    terminal: bool
    assets: tuple[ImageAssetRecord, ...] = ()
    failure: ImageFailure | None = None


class ImagePollAssetConsumer:
    """Persist successful artifacts while preserving retryable poll handles."""

    def __init__(
        self,
        *,
        writer: ImageAssetWriterProtocol | None = None,
    ) -> None:
        self.writer = writer or ManagedImageAssetService()

    async def consume(
        self,
        *,
        result: ImagePollResult,
        command: GeneratedImageAssetCreate,
    ) -> ImagePollAssetConsumption:
        if not isinstance(command, GeneratedImageAssetCreate):
            raise InvalidImageAssetError(
                "Provider poll results require a generated asset command"
            )
        if result.status == "succeeded":
            records = tuple(
                [
                    await self.writer.put(
                        content=artifact.content,
                        command=command,
                    )
                    for artifact in result.artifacts
                ]
            )
            return ImagePollAssetConsumption(
                status=result.status,
                terminal=True,
                assets=records,
            )
        return ImagePollAssetConsumption(
            status=result.status,
            terminal=result.is_terminal,
            failure=result.failure,
        )
