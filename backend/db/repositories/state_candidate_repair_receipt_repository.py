"""Durable, owner-scoped idempotency receipts for paid state repairs."""

from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from backend.db import collections
from backend.db.mongo import get_database
from backend.db.utils import get_utc_now, to_object_id


_CLAIM_TTL_SECONDS = 30


class StateCandidateRepairReceiptConflict(ValueError):
    """A repair idempotency identity was reused with incompatible evidence."""


class StateCandidateRepairResultProjection(BaseModel):
    """Metadata-only pointer to a published state proposal."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["state_candidate_repair_result.v1"] = (
        "state_candidate_repair_result.v1"
    )
    proposal_id: str = Field(min_length=1, max_length=128)
    truncated_section_count: int = Field(ge=0, le=100)
    dropped_item_count: int = Field(ge=0, le=10_000)
    dropped_reference_count: int = Field(ge=0, le=1_000)


class StateCandidateRepairReceiptRepository:
    @property
    def collection(self):
        return get_database()[collections.STATE_CANDIDATE_REPAIR_RECEIPTS]

    @staticmethod
    def _receipt_id(
        *,
        owner_id: str,
        novel_id: str,
        chapter_id: str,
        execution_id: str,
        cycle: int,
    ) -> str:
        identity = "\x1f".join((
            str(owner_id),
            str(novel_id),
            str(chapter_id),
            str(execution_id),
            str(cycle),
        ))
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    @classmethod
    def _scope(
        cls,
        *,
        owner_id: str,
        novel_id: str,
        chapter_id: str,
        execution_id: str,
        cycle: int,
    ) -> dict[str, Any]:
        if type(cycle) is not int or cycle < 1 or cycle > 8:
            raise StateCandidateRepairReceiptConflict(
                "state repair receipt cycle is invalid"
            )
        return {
            "_id": cls._receipt_id(
                owner_id=owner_id,
                novel_id=novel_id,
                chapter_id=chapter_id,
                execution_id=execution_id,
                cycle=cycle,
            ),
            "owner_id": to_object_id(owner_id),
            "novel_id": to_object_id(novel_id),
            "chapter_id": to_object_id(chapter_id),
            "execution_id": to_object_id(execution_id),
            "cycle": cycle,
        }

    @staticmethod
    def _validate(
        receipt: dict[str, Any],
        *,
        scope: dict[str, Any],
        request_digest: str,
    ) -> None:
        if receipt.get("is_deleted") is True or any(
            receipt.get(field) != expected
            for field, expected in scope.items()
        ):
            raise StateCandidateRepairReceiptConflict(
                "state repair receipt scope is invalid"
            )
        if str(receipt.get("request_digest") or "") != str(request_digest):
            raise StateCandidateRepairReceiptConflict(
                "state repair receipt request digest changed"
            )
        if str(receipt.get("state") or "") not in {
            "reserved",
            "dispatched",
            "completed",
        }:
            raise StateCandidateRepairReceiptConflict(
                "state repair receipt state is invalid"
            )

    async def find_receipt(
        self,
        *,
        owner_id: str,
        novel_id: str,
        chapter_id: str,
        execution_id: str,
        cycle: int,
        request_digest: str,
    ) -> dict[str, Any] | None:
        scope = self._scope(
            owner_id=owner_id,
            novel_id=novel_id,
            chapter_id=chapter_id,
            execution_id=execution_id,
            cycle=cycle,
        )
        receipt = await self.collection.find_one({"_id": scope["_id"]})
        if receipt is None:
            return None
        self._validate(receipt, scope=scope, request_digest=request_digest)
        return receipt

    async def claim_receipt(
        self,
        *,
        owner_id: str,
        novel_id: str,
        chapter_id: str,
        execution_id: str,
        cycle: int,
        request_digest: str,
        claim_token: str,
    ) -> tuple[str, dict[str, Any]]:
        if not claim_token:
            raise StateCandidateRepairReceiptConflict(
                "state repair receipt claim token is missing"
            )
        scope = self._scope(
            owner_id=owner_id,
            novel_id=novel_id,
            chapter_id=chapter_id,
            execution_id=execution_id,
            cycle=cycle,
        )
        now = get_utc_now()
        document = {
            **scope,
            "schema_version": "state_candidate_repair_receipt.v1",
            "request_digest": str(request_digest),
            "state": "reserved",
            "claim_token": str(claim_token),
            "claim_expires_at": now + timedelta(seconds=_CLAIM_TTL_SECONDS),
            "provider_attempt_ids": [],
            "created_at": now,
            "updated_at": now,
            "is_deleted": False,
        }
        try:
            await self.collection.insert_one(document)
            return "claimed", document
        except DuplicateKeyError:
            pass
        receipt = await self.collection.find_one({"_id": scope["_id"]})
        if receipt is None:
            raise StateCandidateRepairReceiptConflict(
                "state repair receipt disappeared after claim conflict"
            )
        self._validate(receipt, scope=scope, request_digest=request_digest)
        state = str(receipt["state"])
        if state == "completed":
            return "completed", receipt
        if (
            state == "reserved"
            and str(receipt.get("claim_token") or "") == str(claim_token)
        ):
            return "claimed", receipt
        expires_at = receipt.get("claim_expires_at")
        if state == "reserved" and expires_at is not None and expires_at <= now:
            reclaimed = await self.collection.find_one_and_update(
                {
                    "_id": scope["_id"],
                    "state": "reserved",
                    "claim_token": str(receipt.get("claim_token") or ""),
                    "claim_expires_at": expires_at,
                },
                {
                    "$set": {
                        "claim_token": str(claim_token),
                        "claim_expires_at": (
                            now + timedelta(seconds=_CLAIM_TTL_SECONDS)
                        ),
                        "updated_at": now,
                    }
                },
                return_document=ReturnDocument.AFTER,
            )
            if reclaimed is not None:
                return "claimed", reclaimed
            receipt = await self.collection.find_one({"_id": scope["_id"]})
            if receipt is None:
                raise StateCandidateRepairReceiptConflict(
                    "state repair receipt disappeared during reclaim"
                )
            self._validate(receipt, scope=scope, request_digest=request_digest)
            state = str(receipt["state"])
            if state == "completed":
                return "completed", receipt
        return f"in_progress_{state}", receipt

    async def mark_dispatched(
        self,
        *,
        receipt_id: str,
        claim_token: str,
        attempt_id: str,
    ) -> None:
        if not attempt_id:
            raise StateCandidateRepairReceiptConflict(
                "state repair Provider attempt id is missing"
            )
        updated = await self.collection.find_one_and_update(
            {
                "_id": str(receipt_id),
                "state": {"$in": ["reserved", "dispatched"]},
                "claim_token": str(claim_token),
                "is_deleted": False,
            },
            {
                "$set": {"state": "dispatched", "updated_at": get_utc_now()},
                "$addToSet": {"provider_attempt_ids": str(attempt_id)},
                "$unset": {"claim_expires_at": ""},
            },
            return_document=ReturnDocument.AFTER,
        )
        if updated is None:
            raise StateCandidateRepairReceiptConflict(
                "state repair Provider dispatch authority expired"
            )

    async def release_pre_dispatch(
        self,
        *,
        receipt_id: str,
        claim_token: str,
        attempt_id: str,
    ) -> None:
        now = get_utc_now()
        reverted = await self.collection.find_one_and_update(
            {
                "_id": str(receipt_id),
                "state": "dispatched",
                "claim_token": str(claim_token),
                "provider_attempt_ids": [str(attempt_id)],
            },
            {
                "$set": {
                    "state": "reserved",
                    "claim_expires_at": (
                        now + timedelta(seconds=_CLAIM_TTL_SECONDS)
                    ),
                    "updated_at": now,
                },
                "$pull": {"provider_attempt_ids": str(attempt_id)},
            },
            return_document=ReturnDocument.AFTER,
        )
        if reverted is not None:
            return
        await self.collection.update_one(
            {
                "_id": str(receipt_id),
                "state": "dispatched",
                "claim_token": str(claim_token),
            },
            {
                "$pull": {"provider_attempt_ids": str(attempt_id)},
                "$set": {"updated_at": now},
            },
        )

    async def complete_receipt(
        self,
        *,
        receipt_id: str,
        claim_token: str,
        result_projection: dict[str, Any],
    ) -> dict[str, Any]:
        projection = StateCandidateRepairResultProjection.model_validate(
            result_projection
        ).model_dump(mode="json")
        now = get_utc_now()
        receipt = await self.collection.find_one_and_update(
            {
                "_id": str(receipt_id),
                "state": "dispatched",
                "claim_token": str(claim_token),
                "is_deleted": False,
            },
            {
                "$set": {
                    "state": "completed",
                    "result_projection": projection,
                    "completed_at": now,
                    "updated_at": now,
                },
                "$unset": {"claim_expires_at": ""},
            },
            return_document=ReturnDocument.AFTER,
        )
        if receipt is None:
            receipt = await self.collection.find_one({"_id": str(receipt_id)})
        if receipt is None or str(receipt.get("state") or "") != "completed":
            raise StateCandidateRepairReceiptConflict(
                "state repair receipt could not be completed"
            )
        stored = StateCandidateRepairResultProjection.model_validate(
            receipt.get("result_projection")
        ).model_dump(mode="json")
        if stored != projection:
            raise StateCandidateRepairReceiptConflict(
                "state repair receipt result projection conflicts"
            )
        return receipt


state_candidate_repair_receipt_repo = (
    StateCandidateRepairReceiptRepository()
)
