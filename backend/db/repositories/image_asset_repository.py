"""Persistence for user-owned managed image asset metadata."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from backend.db.base import BaseRepository
from backend.db.collections import IMAGE_ASSETS, NOVELS


class ImageAssetRepository(BaseRepository):
    """Keep managed-image metadata idempotent and owner-scoped."""

    def __init__(self) -> None:
        super().__init__(IMAGE_ASSETS)

    async def novel_belongs_to_owner(
        self,
        *,
        owner_id: ObjectId,
        novel_id: ObjectId,
    ) -> bool:
        novel = await self.collection.database[NOVELS].find_one(
            {
                "_id": novel_id,
                "owner_id": owner_id,
                "is_deleted": False,
            },
            projection={"_id": 1},
        )
        return novel is not None

    async def upsert_metadata(self, document: dict[str, Any]) -> dict[str, Any]:
        """Insert one logical metadata record, or return its existing record."""

        owner_id = document["owner_id"]
        novel_id = document["novel_id"]
        metadata_fingerprint = document["metadata_fingerprint"]
        query = {
            "owner_id": owner_id,
            "novel_id": novel_id,
            "metadata_fingerprint": metadata_fingerprint,
            "is_deleted": False,
        }
        prepared = self._prepare_audit_fields_for_insert(document)
        prepared.update(
            {
                "owner_id": owner_id,
                "novel_id": novel_id,
                "metadata_fingerprint": metadata_fingerprint,
                "is_deleted": False,
                "deleted_at": None,
            }
        )

        try:
            stored = await self.collection.find_one_and_update(
                query,
                {"$setOnInsert": prepared},
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
        except DuplicateKeyError:
            # A concurrent upsert may win after this operation has decided that
            # it needs to insert. The partial unique index makes that race
            # observable; returning the winner preserves idempotent semantics.
            stored = await self.collection.find_one(query)

        if stored is None:  # pragma: no cover - acknowledged upsert invariant.
            raise RuntimeError("Image asset metadata disappeared after upsert")
        return stored

    async def get_owned_by_id(
        self,
        *,
        owner_id: ObjectId,
        asset_id: ObjectId,
    ) -> dict[str, Any] | None:
        return await self.collection.find_one(
            {
                "_id": asset_id,
                "owner_id": owner_id,
                "is_deleted": False,
            }
        )

    async def get_owned_by_relative_path(
        self,
        *,
        owner_id: ObjectId,
        relative_path: str,
    ) -> dict[str, Any] | None:
        return await self.collection.find_one(
            {
                "owner_id": owner_id,
                "relative_path": relative_path,
                "is_deleted": False,
            }
        )

    def iter_owned_metadata(
        self,
        *,
        owner_id: ObjectId,
    ) -> AsyncIterator[dict[str, Any]]:
        return (
            self.collection.find(
                {
                    "owner_id": owner_id,
                    "is_deleted": False,
                },
                projection={
                    "_id": 1,
                    "novel_id": 1,
                    "subject_kind": 1,
                    "subject_id": 1,
                    "content_hash": 1,
                    "relative_path": 1,
                },
            )
            .sort("_id", 1)
            .batch_size(256)
        )

    async def find_owned_referenced_paths(
        self,
        *,
        owner_id: ObjectId,
        relative_paths: Sequence[str],
    ) -> set[str]:
        if not relative_paths:
            return set()
        paths = await self.collection.distinct(
            "relative_path",
            filter={
                "owner_id": owner_id,
                "relative_path": {"$in": list(relative_paths)},
                "is_deleted": False,
            },
        )
        return {
            str(relative_path)
            for relative_path in paths
            if isinstance(relative_path, str)
        }


image_asset_repo = ImageAssetRepository()
