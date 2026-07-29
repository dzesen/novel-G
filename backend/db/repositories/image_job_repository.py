"""Owner-scoped persistence for resumable image generation jobs."""

from __future__ import annotations

from statistics import median
from typing import Any

from pymongo import DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from backend.db import collections
from backend.db.base import BaseRepository
from backend.db.utils import get_utc_now, to_object_id


class ImageJobRepository(BaseRepository):
    def __init__(self) -> None:
        super().__init__(collections.IMAGE_JOBS)

    @staticmethod
    def _scope(
        *,
        owner_id: str,
        novel_id: str,
        card_id: str,
    ) -> dict[str, Any]:
        return {
            "owner_id": to_object_id(owner_id),
            "novel_id": to_object_id(novel_id),
            "character_card_id": to_object_id(card_id),
            "usage": "character_portrait",
            "is_deleted": False,
        }

    async def create_job(self, document: dict[str, Any]) -> dict[str, Any]:
        prepared = self._prepare_audit_fields_for_insert(document)
        prepared.update(
            {
                "owner_id": to_object_id(document["owner_id"]),
                "novel_id": to_object_id(document["novel_id"]),
                "character_card_id": to_object_id(
                    document["character_card_id"]
                ),
                "is_deleted": False,
                "deleted_at": None,
            }
        )
        try:
            result = await self.collection.insert_one(prepared)
            stored = await self.collection.find_one({"_id": result.inserted_id})
            was_created = True
        except DuplicateKeyError:
            stored = await self.collection.find_one(
                {
                    "owner_id": prepared["owner_id"],
                    "idempotency_key": prepared.get("idempotency_key"),
                    "usage": "character_portrait",
                    "is_terminal": False,
                    "is_deleted": False,
                }
            )
            if stored is None:
                stored = await self.collection.find_one(
                    {
                        "owner_id": prepared["owner_id"],
                        "novel_id": prepared["novel_id"],
                        "character_card_id": prepared[
                            "character_card_id"
                        ],
                        "usage": "character_portrait",
                        "is_terminal": False,
                        "is_deleted": False,
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
        card_id: str,
        job_id: str,
    ) -> dict[str, Any] | None:
        return await self.collection.find_one(
            {
                "_id": to_object_id(job_id),
                **self._scope(
                    owner_id=owner_id,
                    novel_id=novel_id,
                    card_id=card_id,
                ),
            }
        )

    async def update_owned_job(
        self,
        *,
        owner_id: str,
        novel_id: str,
        card_id: str,
        job_id: str,
        fields: dict[str, Any],
    ) -> dict[str, Any] | None:
        return await self.collection.find_one_and_update(
            {
                "_id": to_object_id(job_id),
                **self._scope(
                    owner_id=owner_id,
                    novel_id=novel_id,
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
        card_id: str,
        job_id: str,
        expected_revision: int,
        fields: dict[str, Any],
    ) -> dict[str, Any] | None:
        return await self.collection.find_one_and_update(
            {
                "_id": to_object_id(job_id),
                **self._scope(
                    owner_id=owner_id,
                    novel_id=novel_id,
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
        card_id: str,
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
                    **self._scope(
                        owner_id=owner_id,
                        novel_id=novel_id,
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
        card_id: str,
        job_id: str,
        fields: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Audit a late handle without displacing a replacement active job."""

        return await self.collection.find_one_and_update(
            {
                "_id": to_object_id(job_id),
                **self._scope(
                    owner_id=owner_id,
                    novel_id=novel_id,
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
        card_id: str,
        job_id: str,
        expected_revision: int,
        fields: dict[str, Any],
    ) -> dict[str, Any] | None:
        return await self.collection.find_one_and_update(
            {
                "_id": to_object_id(job_id),
                **self._scope(
                    owner_id=owner_id,
                    novel_id=novel_id,
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
        card_id: str,
    ) -> dict[str, Any] | None:
        return await self.collection.find_one(
            {
                **self._scope(
                    owner_id=owner_id,
                    novel_id=novel_id,
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
        card_id: str,
    ) -> dict[str, Any] | None:
        return await self.collection.find_one(
            {
                **self._scope(
                    owner_id=owner_id,
                    novel_id=novel_id,
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
        card_id: str,
    ) -> dict[str, Any] | None:
        return await self.collection.find_one(
            self._scope(
                owner_id=owner_id,
                novel_id=novel_id,
                card_id=card_id,
            ),
            sort=[("created_at", DESCENDING)],
        )

    async def median_completed_seconds(
        self,
        *,
        provider_alias: str,
    ) -> int | None:
        cursor = (
            self.collection.find(
                {
                    "provider_alias": provider_alias,
                    "usage": "character_portrait",
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
