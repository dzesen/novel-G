"""Persistence for user-owned managed image asset metadata."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from bson import ObjectId
from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from backend.db.base import BaseRepository
from backend.db.collections import IMAGE_ASSETS, NOVELS
from backend.db.utils import get_utc_now


class ImageAssetRepository(BaseRepository):
    """Keep managed-image metadata idempotent and owner-scoped."""

    def __init__(self) -> None:
        super().__init__(IMAGE_ASSETS)

    @staticmethod
    def _effective_stage_scope(stage: str) -> dict[str, Any]:
        return {
            "$or": [
                {"pipeline_stage": stage},
                {
                    "pipeline_stage": "external_import",
                    "external_import_target_stage": stage,
                },
            ]
        }

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

    async def get_owned_subject_asset(
        self,
        *,
        owner_id: ObjectId,
        novel_id: ObjectId,
        asset_id: ObjectId,
        subject_kind: str,
        subject_id: str,
    ) -> dict[str, Any] | None:
        """Resolve one active asset only through its complete business lineage."""

        return await self.collection.find_one(
            {
                "_id": asset_id,
                "owner_id": owner_id,
                "novel_id": novel_id,
                "subject_kind": subject_kind,
                "subject_id": subject_id,
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

    async def get_owned_subject_hash(
        self,
        *,
        owner_id: ObjectId,
        novel_id: ObjectId,
        subject_kind: str,
        subject_id: str,
        content_hash: str,
    ) -> dict[str, Any] | None:
        return await self.collection.find_one(
            {
                "owner_id": owner_id,
                "novel_id": novel_id,
                "subject_kind": subject_kind,
                "subject_id": subject_id,
                "content_hash": content_hash,
                "is_deleted": False,
            },
            sort=[("created_at", -1)],
        )

    async def get_latest_owned_imported_subject(
        self,
        *,
        owner_id: ObjectId,
        novel_id: ObjectId,
        subject_kind: str,
        subject_id: str,
    ) -> dict[str, Any] | None:
        return await self.collection.find_one(
            {
                "owner_id": owner_id,
                "novel_id": novel_id,
                "subject_kind": subject_kind,
                "subject_id": subject_id,
                "source": "imported",
                "is_deleted": False,
            },
            sort=[("created_at", -1), ("_id", -1)],
        )

    async def list_owned_subject(
        self,
        *,
        owner_id: ObjectId,
        novel_id: ObjectId,
        subject_kind: str,
        subject_id: str,
    ) -> list[dict[str, Any]]:
        cursor = self.collection.find(
            {
                "owner_id": owner_id,
                "novel_id": novel_id,
                "subject_kind": subject_kind,
                "subject_id": subject_id,
                "is_deleted": False,
            }
        ).sort([("created_at", -1), ("_id", -1)])
        return await cursor.to_list(length=None)

    async def get_owned_stage_candidate(
        self,
        *,
        owner_id: ObjectId,
        novel_id: ObjectId,
        brief_id: ObjectId,
        run_id: ObjectId,
        stage: str,
        asset_id: ObjectId,
        session: AsyncClientSession | None = None,
    ) -> dict[str, Any] | None:
        """Resolve one active candidate through its complete staged lineage."""

        return await self.collection.find_one(
            {
                "_id": asset_id,
                "owner_id": owner_id,
                "novel_id": novel_id,
                "subject_kind": "scene_illustration",
                "subject_id": str(brief_id),
                "illustration_brief_id": brief_id,
                "illustration_run_id": run_id,
                **self._effective_stage_scope(stage),
                "is_deleted": False,
            },
            session=session,
        )

    async def list_owned_stage_candidates(
        self,
        *,
        owner_id: ObjectId,
        novel_id: ObjectId,
        brief_id: ObjectId,
        run_id: ObjectId,
        stage: str,
        session: AsyncClientSession | None = None,
    ) -> list[dict[str, Any]]:
        cursor = self.collection.find(
            {
                "owner_id": owner_id,
                "novel_id": novel_id,
                "subject_kind": "scene_illustration",
                "subject_id": str(brief_id),
                "illustration_brief_id": brief_id,
                "illustration_run_id": run_id,
                **self._effective_stage_scope(stage),
                "is_deleted": False,
            },
            session=session,
        ).sort([("created_at", -1), ("_id", -1)])
        return await cursor.to_list(length=None)

    async def select_owned_stage_candidate(
        self,
        *,
        owner_id: ObjectId,
        novel_id: ObjectId,
        brief_id: ObjectId,
        run_id: ObjectId,
        stage: str,
        asset_id: ObjectId,
        session: AsyncClientSession | None = None,
    ) -> dict[str, Any] | None:
        """Mark exactly one selectable candidate selected within a stage."""

        scope = {
            "owner_id": owner_id,
            "novel_id": novel_id,
            "subject_kind": "scene_illustration",
            "subject_id": str(brief_id),
            "illustration_brief_id": brief_id,
            "illustration_run_id": run_id,
            **self._effective_stage_scope(stage),
            "is_deleted": False,
        }
        available_update = self._prepare_audit_fields_for_update(
            {
                "candidate_state": "available",
                "discarded_at": None,
                "discard_reason": None,
            }
        )
        await self.collection.update_many(
            {
                **scope,
                "_id": {"$ne": asset_id},
                "candidate_state": "selected",
            },
            {"$set": available_update},
            session=session,
        )
        selected_update = self._prepare_audit_fields_for_update(
            {
                "candidate_state": "selected",
                "discarded_at": None,
                "discard_reason": None,
            }
        )
        return await self.collection.find_one_and_update(
            {
                **scope,
                "_id": asset_id,
                "candidate_state": {"$in": ["available", "selected"]},
            },
            {"$set": selected_update},
            return_document=ReturnDocument.AFTER,
            session=session,
        )

    async def discard_owned_stage_candidate(
        self,
        *,
        owner_id: ObjectId,
        novel_id: ObjectId,
        brief_id: ObjectId,
        run_id: ObjectId,
        stage: str,
        asset_id: ObjectId,
        reason: str,
        session: AsyncClientSession | None = None,
    ) -> dict[str, Any] | None:
        update = self._prepare_audit_fields_for_update(
            {
                "candidate_state": "discarded",
                "discarded_at": get_utc_now(),
                "discard_reason": reason,
            }
        )
        return await self.collection.find_one_and_update(
            {
                "_id": asset_id,
                "owner_id": owner_id,
                "novel_id": novel_id,
                "subject_kind": "scene_illustration",
                "subject_id": str(brief_id),
                "illustration_brief_id": brief_id,
                "illustration_run_id": run_id,
                **self._effective_stage_scope(stage),
                "candidate_state": "available",
                "is_deleted": False,
            },
            {"$set": update},
            return_document=ReturnDocument.AFTER,
            session=session,
        )

    async def restore_owned_stage_candidate(
        self,
        *,
        owner_id: ObjectId,
        novel_id: ObjectId,
        brief_id: ObjectId,
        run_id: ObjectId,
        stage: str,
        asset_id: ObjectId,
        session: AsyncClientSession | None = None,
    ) -> dict[str, Any] | None:
        update = self._prepare_audit_fields_for_update(
            {
                "candidate_state": "available",
                "discarded_at": None,
                "discard_reason": None,
            }
        )
        return await self.collection.find_one_and_update(
            {
                "_id": asset_id,
                "owner_id": owner_id,
                "novel_id": novel_id,
                "subject_kind": "scene_illustration",
                "subject_id": str(brief_id),
                "illustration_brief_id": brief_id,
                "illustration_run_id": run_id,
                **self._effective_stage_scope(stage),
                "candidate_state": "discarded",
                "is_deleted": False,
            },
            {"$set": update},
            return_document=ReturnDocument.AFTER,
            session=session,
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
