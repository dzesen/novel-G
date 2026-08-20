"""Durable, owner-scoped idempotency receipts for paid state repairs."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any, Literal

from bson import ObjectId
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from backend.db import collections
from backend.db.mongo import get_database
from backend.db.utils import get_utc_now, to_object_id
from backend.services.generation.candidate_repair_contracts import (
    MAX_CHAPTER_CANDIDATE_REPAIR_CYCLES,
)


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


class StateCandidateRepairReceiptEnvelope(BaseModel):
    """Closed persisted envelope for every receipt lifecycle state."""

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        strict=True,
    )

    receipt_id: str = Field(alias="_id", min_length=64, max_length=64)
    owner_id: ObjectId
    novel_id: ObjectId
    chapter_id: ObjectId
    execution_id: ObjectId
    cycle: int = Field(ge=1, le=MAX_CHAPTER_CANDIDATE_REPAIR_CYCLES)
    schema_version: Literal["state_candidate_repair_receipt.v1"]
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: Literal["reserved", "dispatched", "completed"]
    claim_token: str = Field(min_length=1, max_length=128)
    claim_expires_at: datetime | None = None
    provider_attempt_ids: list[str] = Field(max_length=64)
    result_projection: StateCandidateRepairResultProjection | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    is_deleted: Literal[False]

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "StateCandidateRepairReceiptEnvelope":
        if len(set(self.provider_attempt_ids)) != len(
            self.provider_attempt_ids
        ) or any(
            not item or len(item) > 128 for item in self.provider_attempt_ids
        ):
            raise ValueError("receipt attempt identities are invalid")
        if self.state == "reserved":
            valid = (
                isinstance(self.claim_expires_at, datetime)
                and not self.provider_attempt_ids
                and self.result_projection is None
                and self.completed_at is None
            )
        elif self.state == "dispatched":
            valid = (
                self.claim_expires_at is None
                and bool(self.provider_attempt_ids)
                and self.result_projection is None
                and self.completed_at is None
            )
        else:
            valid = (
                self.claim_expires_at is None
                and bool(self.provider_attempt_ids)
                and self.result_projection is not None
                and isinstance(self.completed_at, datetime)
            )
        if not valid:
            raise ValueError("receipt lifecycle projection is invalid")
        return self


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
        if (
            type(cycle) is not int
            or cycle < 1
            or cycle > MAX_CHAPTER_CANDIDATE_REPAIR_CYCLES
        ):
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
    ) -> StateCandidateRepairReceiptEnvelope:
        if receipt.get("schema_version") != "state_candidate_repair_receipt.v1":
            raise StateCandidateRepairReceiptConflict(
                "state repair receipt schema is invalid"
            )
        try:
            envelope = StateCandidateRepairReceiptEnvelope.model_validate(
                receipt
            )
        except ValidationError as exc:
            raise StateCandidateRepairReceiptConflict(
                "state repair receipt envelope is invalid"
            ) from exc
        if any(
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
        return envelope

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

    async def reopen_released_pre_dispatch(
        self,
        *,
        receipt_id: str,
        claim_token: str,
        new_claim_token: str,
        attempt_ids: tuple[str, ...],
    ) -> dict[str, Any]:
        """Repair a receipt after the Job ledger proves zero dispatch."""
        if (
            not new_claim_token
            or not attempt_ids
            or len(set(attempt_ids)) != len(attempt_ids)
        ):
            raise StateCandidateRepairReceiptConflict(
                "state repair released attempts are invalid"
            )
        now = get_utc_now()
        reopened = await self.collection.find_one_and_update(
            {
                "_id": str(receipt_id),
                "state": "dispatched",
                "claim_token": str(claim_token),
                "provider_attempt_ids": list(attempt_ids),
                "is_deleted": False,
            },
            {
                "$set": {
                    "state": "reserved",
                    "claim_token": str(new_claim_token),
                    "claim_expires_at": (
                        now + timedelta(seconds=_CLAIM_TTL_SECONDS)
                    ),
                    "provider_attempt_ids": [],
                    "updated_at": now,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if reopened is None:
            reopened = await self.collection.find_one({"_id": str(receipt_id)})
        if reopened is None:
            raise StateCandidateRepairReceiptConflict(
                "state repair receipt disappeared during release repair"
            )
        try:
            StateCandidateRepairReceiptEnvelope.model_validate(reopened)
        except ValidationError as exc:
            raise StateCandidateRepairReceiptConflict(
                "state repair released receipt is invalid"
            ) from exc
        if str(reopened.get("state") or "") != "reserved":
            raise StateCandidateRepairReceiptConflict(
                "state repair released receipt could not reopen"
            )
        return reopened

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
        try:
            StateCandidateRepairReceiptEnvelope.model_validate(receipt)
        except ValidationError as exc:
            raise StateCandidateRepairReceiptConflict(
                "state repair completed receipt is invalid"
            ) from exc
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
