"""Owner-scoped persistence for resumable image generation jobs."""

from __future__ import annotations

from statistics import median
from typing import Any

from pymongo import DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from backend.db import collections
from backend.db.base import BaseRepository
from backend.db.restored_authorization import RESTORED_AUTHORITY_FIELD
from backend.db.utils import get_utc_now, to_object_id


class ImageJobRepository(BaseRepository):
    def __init__(self) -> None:
        super().__init__(collections.IMAGE_JOBS)

    @staticmethod
    def _scope(
        *,
        owner_id: str,
        novel_id: str,
        usage: str = "character_portrait",
        subject_id: str | None = None,
        card_id: str | None = None,
    ) -> dict[str, Any]:
        raw_subject_id = subject_id if subject_id is not None else card_id
        if raw_subject_id is None:
            raise ValueError("subject_id is required")
        canonical_subject_id = to_object_id(raw_subject_id)
        scope: dict[str, Any] = {
            "owner_id": to_object_id(owner_id),
            "novel_id": to_object_id(novel_id),
            "usage": str(usage),
            "is_deleted": False,
        }
        if usage == "character_portrait" and subject_id is None:
            # Slice 6 persisted only character_card_id. New jobs also carry the
            # generic subject_id, while this fallback keeps in-flight legacy
            # handles resumable across the schema transition.
            scope["$or"] = [
                {"subject_id": canonical_subject_id},
                {
                    "subject_id": {"$exists": False},
                    "character_card_id": canonical_subject_id,
                },
            ]
        else:
            scope["subject_id"] = canonical_subject_id
        return scope

    async def create_job(self, document: dict[str, Any]) -> dict[str, Any]:
        usage = str(document.get("usage") or "")
        raw_subject_id = document.get("subject_id")
        if raw_subject_id is None:
            raw_subject_id = document.get("character_card_id")
        if not usage or raw_subject_id is None:
            raise ValueError("usage and subject_id are required")
        subject_id = to_object_id(raw_subject_id)
        prepared = self._prepare_audit_fields_for_insert(document)
        prepared.update(
            {
                "owner_id": to_object_id(document["owner_id"]),
                "novel_id": to_object_id(document["novel_id"]),
                "usage": usage,
                "subject_id": subject_id,
                "is_deleted": False,
                "deleted_at": None,
            }
        )
        for field in (
            "illustration_brief_id",
            "illustration_run_id",
            "parent_asset_id",
            "portrait_batch_id",
        ):
            value = document.get(field)
            if value is None:
                # ObjectId(None) creates a new identifier, so optional lineage
                # must be omitted before any conversion is attempted.
                prepared.pop(field, None)
                continue
            prepared[field] = to_object_id(value)
        if usage == "character_portrait":
            # Keep the old field during the compatibility window so both the
            # legacy and generic active-job indexes guard new portrait jobs.
            prepared["character_card_id"] = to_object_id(
                document.get("character_card_id") or subject_id
            )
        else:
            prepared.pop("character_card_id", None)
        try:
            result = await self.collection.insert_one(prepared)
            stored = await self.collection.find_one({"_id": result.inserted_id})
            was_created = True
        except DuplicateKeyError:
            stored = await self.collection.find_one(
                {
                    "owner_id": prepared["owner_id"],
                    "idempotency_key": prepared.get("idempotency_key"),
                    "usage": usage,
                    "is_terminal": False,
                    "is_deleted": False,
                }
            )
            if stored is None:
                stored = await self.collection.find_one(
                    {
                        **self._scope(
                            owner_id=str(prepared["owner_id"]),
                            novel_id=str(prepared["novel_id"]),
                            usage=usage,
                            subject_id=str(subject_id),
                        ),
                        "is_terminal": False,
                    }
                )
            was_created = False
        if stored is None:
            raise RuntimeError("Image job disappeared after creation")
        return {**stored, "_was_created": was_created}

    async def get_owned_job(
        self,
        *,
        owner_id: str,
        novel_id: str,
        usage: str = "character_portrait",
        subject_id: str | None = None,
        card_id: str | None = None,
        job_id: str,
    ) -> dict[str, Any] | None:
        return await self.collection.find_one(
            {
                "_id": to_object_id(job_id),
                **self._scope(
                    owner_id=owner_id,
                    novel_id=novel_id,
                    usage=usage,
                    subject_id=subject_id,
                    card_id=card_id,
                ),
            }
        )

    async def update_owned_job(
        self,
        *,
        owner_id: str,
        novel_id: str,
        usage: str = "character_portrait",
        subject_id: str | None = None,
        card_id: str | None = None,
        job_id: str,
        fields: dict[str, Any],
    ) -> dict[str, Any] | None:
        return await self.collection.find_one_and_update(
            {
                "_id": to_object_id(job_id),
                RESTORED_AUTHORITY_FIELD: None,
                **self._scope(
                    owner_id=owner_id,
                    novel_id=novel_id,
                    usage=usage,
                    subject_id=subject_id,
                    card_id=card_id,
                ),
            },
            {
                "$set": {
                    **dict(fields),
                    "updated_at": get_utc_now(),
                }
            },
            return_document=ReturnDocument.AFTER,
        )

    async def compare_and_update_owned_job(
        self,
        *,
        owner_id: str,
        novel_id: str,
        usage: str = "character_portrait",
        subject_id: str | None = None,
        card_id: str | None = None,
        job_id: str,
        expected_revision: int,
        fields: dict[str, Any],
    ) -> dict[str, Any] | None:
        return await self.collection.find_one_and_update(
            {
                "_id": to_object_id(job_id),
                RESTORED_AUTHORITY_FIELD: None,
                **self._scope(
                    owner_id=owner_id,
                    novel_id=novel_id,
                    usage=usage,
                    subject_id=subject_id,
                    card_id=card_id,
                ),
                "is_terminal": False,
                "job_revision": int(expected_revision),
            },
            {
                "$set": {
                    **dict(fields),
                    "updated_at": get_utc_now(),
                },
                "$inc": {"job_revision": 1},
            },
            return_document=ReturnDocument.AFTER,
        )

    async def attach_late_handle(
        self,
        *,
        owner_id: str,
        novel_id: str,
        usage: str = "character_portrait",
        subject_id: str | None = None,
        card_id: str | None = None,
        job_id: str,
        fields: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Reopen an explicitly abandoned submit when its handle arrives.

        The exact ``job_lost`` tombstone and missing-handle predicates keep
        this from rewriting an unrelated terminal job. Reopening lets the
        normal leased cancel/poll/storage pipeline account for a provider job
        that finished after the user abandoned the handle wait.
        """

        try:
            return await self.collection.find_one_and_update(
                {
                    "_id": to_object_id(job_id),
                    RESTORED_AUTHORITY_FIELD: None,
                    **self._scope(
                        owner_id=owner_id,
                        novel_id=novel_id,
                        usage=usage,
                        subject_id=subject_id,
                        card_id=card_id,
                    ),
                    "is_terminal": True,
                    "status": "failed",
                    "failure.code": "job_lost",
                    "handle": {"$exists": False},
                },
                {
                    "$set": {
                        **dict(fields),
                        "late_handle_attached": True,
                        "updated_at": get_utc_now(),
                    },
                    "$inc": {"job_revision": 1},
                },
                return_document=ReturnDocument.AFTER,
            )
        except DuplicateKeyError:
            # A user may already have started a replacement after explicitly
            # abandoning the missing handle. Never displace that active job.
            return None

    async def attach_terminal_late_handle(
        self,
        *,
        owner_id: str,
        novel_id: str,
        usage: str = "character_portrait",
        subject_id: str | None = None,
        card_id: str | None = None,
        job_id: str,
        fields: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Audit a late handle without displacing a replacement active job."""

        return await self.collection.find_one_and_update(
            {
                "_id": to_object_id(job_id),
                RESTORED_AUTHORITY_FIELD: None,
                **self._scope(
                    owner_id=owner_id,
                    novel_id=novel_id,
                    usage=usage,
                    subject_id=subject_id,
                    card_id=card_id,
                ),
                "is_terminal": True,
                "status": "failed",
                "failure.code": "job_lost",
                "handle": {"$exists": False},
            },
            {
                "$set": {
                    **dict(fields),
                    "is_terminal": True,
                    "late_handle_attached": True,
                    "late_reconciliation": True,
                    "updated_at": get_utc_now(),
                },
                "$inc": {"job_revision": 1},
            },
            return_document=ReturnDocument.AFTER,
        )

    async def compare_and_update_terminal_job(
        self,
        *,
        owner_id: str,
        novel_id: str,
        usage: str = "character_portrait",
        subject_id: str | None = None,
        card_id: str | None = None,
        job_id: str,
        expected_revision: int,
        fields: dict[str, Any],
    ) -> dict[str, Any] | None:
        return await self.collection.find_one_and_update(
            {
                "_id": to_object_id(job_id),
                RESTORED_AUTHORITY_FIELD: None,
                **self._scope(
                    owner_id=owner_id,
                    novel_id=novel_id,
                    usage=usage,
                    subject_id=subject_id,
                    card_id=card_id,
                ),
                "is_terminal": True,
                "late_reconciliation": True,
                "job_revision": int(expected_revision),
            },
            {
                "$set": {
                    **dict(fields),
                    "updated_at": get_utc_now(),
                },
                "$inc": {"job_revision": 1},
            },
            return_document=ReturnDocument.AFTER,
        )

    async def find_active_owned_job(
        self,
        *,
        owner_id: str,
        novel_id: str,
        usage: str = "character_portrait",
        subject_id: str | None = None,
        card_id: str | None = None,
    ) -> dict[str, Any] | None:
        return await self.collection.find_one(
            {
                RESTORED_AUTHORITY_FIELD: None,
                **self._scope(
                    owner_id=owner_id,
                    novel_id=novel_id,
                    usage=usage,
                    subject_id=subject_id,
                    card_id=card_id,
                ),
                "is_terminal": False,
            },
            sort=[("created_at", DESCENDING)],
        )

    async def find_pending_cleanup_owned_job(
        self,
        *,
        owner_id: str,
        novel_id: str,
        usage: str = "character_portrait",
        subject_id: str | None = None,
        card_id: str | None = None,
    ) -> dict[str, Any] | None:
        return await self.collection.find_one(
            {
                RESTORED_AUTHORITY_FIELD: None,
                **self._scope(
                    owner_id=owner_id,
                    novel_id=novel_id,
                    usage=usage,
                    subject_id=subject_id,
                    card_id=card_id,
                ),
                "cleanup_pending": True,
            },
            sort=[("created_at", DESCENDING)],
        )

    async def find_latest_owned_job(
        self,
        *,
        owner_id: str,
        novel_id: str,
        usage: str = "character_portrait",
        subject_id: str | None = None,
        card_id: str | None = None,
    ) -> dict[str, Any] | None:
        return await self.collection.find_one(
            self._scope(
                owner_id=owner_id,
                novel_id=novel_id,
                usage=usage,
                subject_id=subject_id,
                card_id=card_id,
            ),
            sort=[("created_at", DESCENDING)],
        )

    async def find_owned_portrait_batch_job(
        self,
        *,
        owner_id: str,
        novel_id: str,
        batch_id: str,
        card_id: str,
    ) -> dict[str, Any] | None:
        """Recover the one portrait job created for a frozen batch item."""

        return await self.collection.find_one(
            {
                **self._scope(
                    owner_id=owner_id,
                    novel_id=novel_id,
                    usage="character_portrait",
                    subject_id=card_id,
                ),
                "portrait_batch_id": to_object_id(batch_id),
            },
            sort=[("created_at", DESCENDING)],
        )

    async def list_busy_portrait_subject_ids(
        self,
        *,
        owner_id: str,
        novel_id: str,
        card_ids: list[str] | tuple[str, ...],
    ) -> set[str]:
        """Return selected cards with an active or late-cleanup portrait job."""

        canonical_ids = [to_object_id(card_id) for card_id in card_ids]
        if not canonical_ids:
            return set()
        cursor = self.collection.find(
            {
                "owner_id": to_object_id(owner_id),
                "novel_id": to_object_id(novel_id),
                "usage": "character_portrait",
                "is_deleted": False,
                "$and": [
                    {
                        "$or": [
                            {"subject_id": {"$in": canonical_ids}},
                            {
                                "subject_id": {"$exists": False},
                                "character_card_id": {"$in": canonical_ids},
                            },
                        ]
                    },
                    {
                        "$or": [
                            {"is_terminal": False},
                            {"cleanup_pending": True},
                        ]
                    },
                ],
            },
            projection={"subject_id": 1, "character_card_id": 1},
        )
        # One selected card may legitimately have both a current job and an
        # older late-cleanup job. Limiting rows to the number of card ids can
        # therefore hide a busy card that appears later in the result set.
        documents = await cursor.to_list(length=None)
        return {
            str(document.get("subject_id") or document.get("character_card_id"))
            for document in documents
            if document.get("subject_id") is not None
            or document.get("character_card_id") is not None
        }

    async def list_anchor_dependencies(
        self,
        *,
        owner_id: str,
        novel_id: str,
        card_id: str,
        limit: int = 5,
    ) -> tuple[int, list[dict[str, Any]]]:
        """List scene jobs that still depend on a character anchor."""

        canonical_card_id = str(to_object_id(card_id))
        query = {
            "owner_id": to_object_id(owner_id),
            "novel_id": to_object_id(novel_id),
            "usage": "scene_illustration",
            "appearance_anchor_card_ids": canonical_card_id,
            "is_deleted": False,
            "$or": [
                {"is_terminal": False},
                {"status": "succeeded"},
            ],
        }
        total = await self.collection.count_documents(query)
        cursor = (
            self.collection.find(
                query,
                projection={
                    "subject_id": 1,
                    "status": 1,
                    "created_at": 1,
                },
            )
            .sort("created_at", DESCENDING)
            .limit(max(1, min(int(limit), 20)))
        )
        return total, await cursor.to_list(length=max(1, min(int(limit), 20)))

    async def median_completed_seconds(
        self,
        *,
        provider_alias: str,
        usage: str = "character_portrait",
    ) -> int | None:
        cursor = (
            self.collection.find(
                {
                    "provider_alias": provider_alias,
                    "usage": usage,
                    "status": "succeeded",
                    "is_terminal": True,
                    "elapsed_seconds": {"$gt": 0},
                    "is_deleted": False,
                },
                projection={"elapsed_seconds": 1},
            )
            .sort("created_at", DESCENDING)
            .limit(101)
        )
        documents = await cursor.to_list(length=101)
        values = [
            int(document["elapsed_seconds"])
            for document in documents
            if int(document.get("elapsed_seconds") or 0) > 0
        ]
        if not values:
            return None
        return max(1, int(median(values)))


image_job_repo = ImageJobRepository()
