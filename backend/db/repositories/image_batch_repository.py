"""Owner-scoped persistence for resumable image batches."""

from __future__ import annotations

from typing import Any

from pymongo import DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from backend.db import collections
from backend.db.base import BaseRepository
from backend.db.restored_authorization import RESTORED_AUTHORITY_FIELD
from backend.db.utils import get_utc_now, to_object_id


class ImageBatchRepository(BaseRepository):
    def __init__(self) -> None:
        super().__init__(collections.IMAGE_BATCHES)

    @staticmethod
    def _scope(*, owner_id: str, novel_id: str) -> dict[str, Any]:
        return {
            "owner_id": to_object_id(owner_id),
            "novel_id": to_object_id(novel_id),
            "kind": "character_portrait",
            "is_deleted": False,
        }

    async def create_batch(self, document: dict[str, Any]) -> dict[str, Any]:
        prepared = self._prepare_audit_fields_for_insert(document)
        prepared.update(
            {
                "owner_id": to_object_id(document["owner_id"]),
                "novel_id": to_object_id(document["novel_id"]),
                "kind": "character_portrait",
                "is_deleted": False,
                "deleted_at": None,
            }
        )
        prepared["items"] = [
            {
                **dict(item),
                "card_id": to_object_id(item["card_id"]),
            }
            for item in document.get("items") or ()
        ]
        try:
            result = await self.collection.insert_one(prepared)
            stored = await self.collection.find_one({"_id": result.inserted_id})
            was_created = True
        except DuplicateKeyError:
            stored = await self.find_active_owned_batch(
                owner_id=str(prepared["owner_id"]),
                novel_id=str(prepared["novel_id"]),
            )
            was_created = False
        if stored is None:
            raise RuntimeError("Image batch disappeared after creation")
        return {**stored, "_was_created": was_created}

    async def get_owned_batch(
        self,
        *,
        owner_id: str,
        novel_id: str,
        batch_id: str,
    ) -> dict[str, Any] | None:
        return await self.collection.find_one(
            {
                "_id": to_object_id(batch_id),
                **self._scope(owner_id=owner_id, novel_id=novel_id),
            }
        )

    async def find_active_owned_batch(
        self,
        *,
        owner_id: str,
        novel_id: str,
    ) -> dict[str, Any] | None:
        return await self.collection.find_one(
            {
                RESTORED_AUTHORITY_FIELD: None,
                **self._scope(owner_id=owner_id, novel_id=novel_id),
                "is_terminal": False,
            },
            sort=[("created_at", DESCENDING)],
        )

    async def bind_starting_portrait_job(
        self,
        *,
        owner_id: str,
        novel_id: str,
        batch_id: str,
        card_id: str,
        start_claim_token: str,
        job_id: str,
    ) -> bool:
        bound = await self.collection.find_one_and_update(
            {
                RESTORED_AUTHORITY_FIELD: None,
                "_id": to_object_id(batch_id),
                **self._scope(owner_id=owner_id, novel_id=novel_id),
                "is_terminal": False,
                "cancel_requested": False,
                "items": {
                    "$elemMatch": {
                        "card_id": to_object_id(card_id),
                        "status": "starting",
                        "start_claim_token": str(start_claim_token),
                    }
                },
            },
            {
                "$set": {
                    "items.$.status": "running",
                    "items.$.job_id": str(to_object_id(job_id)),
                    "items.$.job_status": "submitting",
                    "items.$.queue_position": None,
                    "items.$.submit_count": 0,
                    "items.$.completed_images": 0,
                    "items.$.failure": None,
                    "items.$.start_claim_token": None,
                    "items.$.start_claimed_at_epoch": None,
                    "status": "running",
                    "updated_at": get_utc_now(),
                },
                "$inc": {"revision": 1},
            },
            return_document=ReturnDocument.AFTER,
        )
        return bound is not None

    async def compare_and_update_owned_batch(
        self,
        *,
        owner_id: str,
        novel_id: str,
        batch_id: str,
        expected_revision: int,
        fields: dict[str, Any],
    ) -> dict[str, Any] | None:
        return await self.collection.find_one_and_update(
            {
                RESTORED_AUTHORITY_FIELD: None,
                "_id": to_object_id(batch_id),
                **self._scope(owner_id=owner_id, novel_id=novel_id),
                "revision": int(expected_revision),
            },
            {
                "$set": {
                    **dict(fields),
                    "updated_at": get_utc_now(),
                },
                "$inc": {"revision": 1},
            },
            return_document=ReturnDocument.AFTER,
        )


image_batch_repo = ImageBatchRepository()


__all__ = ["ImageBatchRepository", "image_batch_repo"]
