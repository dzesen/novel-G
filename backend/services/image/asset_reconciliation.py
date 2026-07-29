"""Owner-scoped reconciliation for managed image metadata and files."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol

from bson import ObjectId
from pydantic import BaseModel, ConfigDict

from backend.db.repositories.image_asset_repository import ImageAssetRepository
from backend.db.utils import to_object_id
from backend.services.image.managed_assets import (
    MANAGED_IMAGE_ASSET_ROOT,
    _ASSET_FILENAME_PATTERN,
    ImageAssetNotFoundError,
    InvalidImageAssetError,
    ManagedImageAssetService,
    _resolved_path_is_within,
)


RECONCILIATION_BATCH_SIZE = 256
RECONCILIATION_DETAIL_LIMIT = 200


class MissingImageAsset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: str
    novel_id: str
    subject_kind: str
    subject_id: str
    content_hash: str
    relative_path: str | None
    reason: Literal["file_missing", "invalid_reference_path"]


class OrphanImageFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    novel_id: str | None
    content_hash: str | None
    relative_path: str
    byte_size: int


class UnmanagedImageFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    relative_path: str
    byte_size: int


class ImageAssetReconciliationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    checked_at: datetime
    missing_asset_count: int
    orphan_file_count: int
    unmanaged_file_count: int
    missing_assets: tuple[MissingImageAsset, ...]
    orphan_files: tuple[OrphanImageFile, ...]
    unmanaged_files: tuple[UnmanagedImageFile, ...]
    missing_assets_truncated: bool
    orphan_files_truncated: bool
    unmanaged_files_truncated: bool


class ImageAssetReconciliationRepository(Protocol):
    def iter_owned_metadata(
        self,
        *,
        owner_id: ObjectId,
    ) -> AsyncIterator[dict[str, Any]]: ...

    async def find_owned_referenced_paths(
        self,
        *,
        owner_id: ObjectId,
        relative_paths: Sequence[str],
    ) -> set[str]: ...


class _DiscoveredFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    relative_path: str
    byte_size: int
    has_managed_filename: bool
    novel_id: str | None
    content_hash: str | None


def _path_is_link_or_junction(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction and is_junction())
    except OSError:
        return True


def _safe_owner_root(*, root: Path, owner_id: str) -> Path | None:
    owner_root = root / "owners" / owner_id
    if _path_is_link_or_junction(owner_root):
        return None
    try:
        lexical = os.path.normcase(os.path.abspath(owner_root))
        resolved = os.path.normcase(str(owner_root.resolve(strict=False)))
    except OSError:
        return None
    if lexical != resolved:
        return None
    if not _resolved_path_is_within(root=root, target=owner_root):
        return None
    return owner_root


def _inspect_metadata_batch(
    *,
    root: Path,
    owner_id: str,
    documents: Sequence[dict[str, Any]],
) -> list[MissingImageAsset]:
    service = ManagedImageAssetService(root=root)
    owner_root = _safe_owner_root(root=root, owner_id=owner_id)
    missing: list[MissingImageAsset] = []
    for document in documents:
        relative_path_value = document.get("relative_path")
        relative_path = (
            relative_path_value
            if isinstance(relative_path_value, str)
            else None
        )
        valid_relative_path: str | None = None
        reason: Literal["file_missing", "invalid_reference_path"]
        try:
            if relative_path is None or owner_root is None:
                raise ImageAssetNotFoundError("Image asset not found")
            novel_id, content_hash = service._parse_owned_relative_path(
                owner_id=owner_id,
                relative_path=relative_path,
            )
            if (
                novel_id != str(document.get("novel_id"))
                or content_hash != document.get("content_hash")
            ):
                raise ImageAssetNotFoundError("Image asset not found")
            target = service._resolve_relative(relative_path)
            if (
                _path_is_link_or_junction(target)
                or not _resolved_path_is_within(
                    root=owner_root,
                    target=target,
                )
            ):
                raise ImageAssetNotFoundError("Image asset not found")
            valid_relative_path = relative_path
            if target.is_file():
                continue
            reason = "file_missing"
        except (
            ImageAssetNotFoundError,
            InvalidImageAssetError,
            OSError,
            TypeError,
            ValueError,
        ):
            reason = "invalid_reference_path"

        missing.append(
            MissingImageAsset(
                asset_id=str(document.get("_id", "")),
                novel_id=str(document.get("novel_id", "")),
                subject_kind=str(document.get("subject_kind", "")),
                subject_id=str(document.get("subject_id", "")),
                content_hash=str(document.get("content_hash", "")),
                relative_path=valid_relative_path,
                reason=reason,
            )
        )
    return missing


def _iter_owned_files(
    *,
    root: Path,
    owner_id: str,
) -> Iterator[_DiscoveredFile]:
    owner_root = _safe_owner_root(root=root, owner_id=owner_id)
    if owner_root is None:
        return
    if not owner_root.exists():
        return
    if (
        not owner_root.is_dir()
        or not _resolved_path_is_within(root=root, target=owner_root)
    ):
        return

    directories = [owner_root]
    while directories:
        directory = directories.pop()
        if (
            _path_is_link_or_junction(directory)
            or not _resolved_path_is_within(
                root=owner_root,
                target=directory,
            )
        ):
            continue
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        candidate = Path(entry.path)
                        if _path_is_link_or_junction(candidate):
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if _resolved_path_is_within(
                                root=owner_root,
                                target=candidate,
                            ):
                                directories.append(candidate)
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        path = candidate
                        if not _resolved_path_is_within(
                            root=owner_root,
                            target=path,
                        ):
                            continue
                        relative_path = path.relative_to(root).as_posix()
                        novel_id: str | None = None
                        filename_match = _ASSET_FILENAME_PATTERN.fullmatch(
                            path.name
                        )
                        content_hash = (
                            filename_match.group("content_hash")
                            if filename_match is not None
                            else None
                        )
                        if filename_match is not None:
                            try:
                                novel_id, content_hash = (
                                    ManagedImageAssetService
                                    ._parse_owned_relative_path(
                                        owner_id=owner_id,
                                        relative_path=relative_path,
                                    )
                                )
                            except ImageAssetNotFoundError:
                                pass
                        yield _DiscoveredFile(
                            relative_path=relative_path,
                            byte_size=entry.stat(
                                follow_symlinks=False
                            ).st_size,
                            has_managed_filename=(
                                filename_match is not None
                            ),
                            novel_id=novel_id,
                            content_hash=content_hash,
                        )
                    except OSError:
                        continue
        except OSError:
            continue


def _take_file_batch(
    iterator: Iterator[_DiscoveredFile],
    batch_size: int,
) -> list[_DiscoveredFile]:
    batch: list[_DiscoveredFile] = []
    for _ in range(batch_size):
        try:
            batch.append(next(iterator))
        except StopIteration:
            break
    return batch


class ManagedImageAssetReconciler:
    """Compare one owner's metadata and files without mutating either side."""

    def __init__(
        self,
        *,
        root: str | os.PathLike[str] | Path = MANAGED_IMAGE_ASSET_ROOT,
        repository: ImageAssetReconciliationRepository | None = None,
        batch_size: int = RECONCILIATION_BATCH_SIZE,
        detail_limit: int = RECONCILIATION_DETAIL_LIMIT,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        if detail_limit < 0:
            raise ValueError("detail_limit must not be negative")
        self.root = Path(root).resolve()
        self.repository = repository or ImageAssetRepository()
        self.batch_size = batch_size
        self.detail_limit = detail_limit

    async def reconcile(
        self,
        *,
        owner_id: str,
    ) -> ImageAssetReconciliationReport:
        if owner_id is None:
            raise ValueError("owner_id must be an existing ObjectId")
        canonical_owner_id = str(to_object_id(owner_id))
        owner_object_id = to_object_id(canonical_owner_id)

        missing_asset_count = 0
        missing_assets: list[MissingImageAsset] = []
        metadata_batch: list[dict[str, Any]] = []

        async def inspect_metadata() -> None:
            nonlocal missing_asset_count
            if not metadata_batch:
                return
            discovered = await asyncio.to_thread(
                _inspect_metadata_batch,
                root=self.root,
                owner_id=canonical_owner_id,
                documents=tuple(metadata_batch),
            )
            missing_asset_count += len(discovered)
            available = max(self.detail_limit - len(missing_assets), 0)
            missing_assets.extend(discovered[:available])
            metadata_batch.clear()

        async for document in self.repository.iter_owned_metadata(
            owner_id=owner_object_id,
        ):
            metadata_batch.append(document)
            if len(metadata_batch) >= self.batch_size:
                await inspect_metadata()
        await inspect_metadata()

        orphan_file_count = 0
        orphan_files: list[OrphanImageFile] = []
        unmanaged_file_count = 0
        unmanaged_files: list[UnmanagedImageFile] = []
        file_iterator = _iter_owned_files(
            root=self.root,
            owner_id=canonical_owner_id,
        )
        while True:
            batch = await asyncio.to_thread(
                _take_file_batch,
                file_iterator,
                self.batch_size,
            )
            if not batch:
                break
            managed_relative_paths = [
                discovered.relative_path
                for discovered in batch
                if discovered.has_managed_filename
            ]
            referenced = (
                await self.repository.find_owned_referenced_paths(
                    owner_id=owner_object_id,
                    relative_paths=managed_relative_paths,
                )
                if managed_relative_paths
                else set()
            )
            for discovered in batch:
                if not discovered.has_managed_filename:
                    unmanaged_file_count += 1
                    if len(unmanaged_files) < self.detail_limit:
                        unmanaged_files.append(
                            UnmanagedImageFile(
                                relative_path=discovered.relative_path,
                                byte_size=discovered.byte_size,
                            )
                        )
                    continue
                if discovered.relative_path in referenced:
                    continue
                orphan_file_count += 1
                if len(orphan_files) >= self.detail_limit:
                    continue
                orphan_files.append(
                    OrphanImageFile(
                        novel_id=discovered.novel_id,
                        content_hash=discovered.content_hash,
                        relative_path=discovered.relative_path,
                        byte_size=discovered.byte_size,
                    )
                )

        return ImageAssetReconciliationReport(
            checked_at=datetime.now(timezone.utc),
            missing_asset_count=missing_asset_count,
            orphan_file_count=orphan_file_count,
            unmanaged_file_count=unmanaged_file_count,
            missing_assets=tuple(missing_assets),
            orphan_files=tuple(orphan_files),
            unmanaged_files=tuple(unmanaged_files),
            missing_assets_truncated=(
                missing_asset_count > len(missing_assets)
            ),
            orphan_files_truncated=(
                orphan_file_count > len(orphan_files)
            ),
            unmanaged_files_truncated=(
                unmanaged_file_count > len(unmanaged_files)
            ),
        )


async def reconcile_managed_image_assets(
    *,
    owner_id: str,
) -> ImageAssetReconciliationReport:
    """Run the production reconciliation boundary for one owner."""

    return await ManagedImageAssetReconciler().reconcile(owner_id=owner_id)
