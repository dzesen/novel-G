"""Durable, owner-scoped receipts for paid reference-dependency repairs."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any, Literal, Mapping

from bson import ObjectId
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from backend.db import collections
from backend.db.mongo import get_database
from backend.db.utils import get_utc_now, to_object_id
from backend.llm.schemas.novel_pydantic import ChapterOutlineProposalSchema
from backend.services.generation.reference_card_auto_creation import (
    MAX_CANDIDATE_REPAIR_CYCLES_PER_CHAPTER,
)


_CLAIM_TTL_SECONDS = 30
_MAX_CLAIM_EPOCH = 1_000_000
_HEX_64_PATTERN = r"^[0-9a-f]{64}$"
REFERENCE_CARD_REPAIR_RECEIPT_SCHEMA = "reference_card_repair_receipt.v1"


class ReferenceCardRepairReceiptConflict(ValueError):
    """One receipt identity was reused with incompatible frozen evidence."""


class ReferenceCardRepairReceiptEnvelope(BaseModel):
    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        strict=True,
    )

    receipt_id: str = Field(alias="_id", pattern=_HEX_64_PATTERN)
    owner_id: ObjectId
    novel_id: ObjectId
    job_id: ObjectId
    chapter_id: ObjectId
    cycle: int = Field(ge=1, le=MAX_CANDIDATE_REPAIR_CYCLES_PER_CHAPTER)
    schema_version: Literal["reference_card_repair_receipt.v1"]
    authorization_digest: str = Field(pattern=_HEX_64_PATTERN)
    request_digest: str = Field(pattern=_HEX_64_PATTERN)
    source_digest: str = Field(pattern=_HEX_64_PATTERN)
    state: Literal["reserved", "dispatched", "completed"]
    claim_token: str = Field(min_length=1, max_length=128)
    claim_epoch: int = Field(ge=1, le=_MAX_CLAIM_EPOCH)
    claim_expires_at: datetime | None = None
    provider_attempt_ids: list[str] = Field(max_length=16)
    result: ChapterOutlineProposalSchema | None = None
    result_digest: str | None = Field(default=None, pattern=_HEX_64_PATTERN)
    finish_reason: str | None = Field(default=None, min_length=1, max_length=80)
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    is_deleted: Literal[False]

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "ReferenceCardRepairReceiptEnvelope":
        attempts = self.provider_attempt_ids
        if len(attempts) != len(set(attempts)) or any(
            not item or len(item) > 128 for item in attempts
        ):
            raise ValueError("reference-card repair attempt identities are invalid")
        if self.state == "reserved":
            valid = (
                isinstance(self.claim_expires_at, datetime)
                and not attempts
                and self.result is None
                and self.result_digest is None
                and self.finish_reason is None
                and self.completed_at is None
            )
        elif self.state == "dispatched":
            valid = (
                self.claim_expires_at is None
                and bool(attempts)
                and self.result is None
                and self.result_digest is None
                and self.finish_reason is None
                and self.completed_at is None
            )
        else:
            valid = (
                self.claim_expires_at is None
                and bool(attempts)
                and self.result is not None
                and self.result_digest is not None
                and isinstance(self.finish_reason, str)
                and isinstance(self.completed_at, datetime)
            )
        if not valid:
            raise ValueError("reference-card repair receipt lifecycle is invalid")
        return self


class ReferenceCardRepairReceiptRepository:
    @property
    def collection(self):
        return get_database()[collections.REFERENCE_CARD_REPAIR_RECEIPTS]

    @staticmethod
    def _receipt_id(
        *,
        owner_id: str,
        novel_id: str,
        job_id: str,
        chapter_id: str,
        cycle: int,
        authorization_digest: str,
    ) -> str:
        identity = "\x1f".join((
            str(owner_id),
            str(novel_id),
            str(job_id),
            str(chapter_id),
            str(cycle),
            str(authorization_digest),
        ))
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    @classmethod
    def _scope(
        cls,
        *,
        owner_id: str,
        novel_id: str,
        job_id: str,
        chapter_id: str,
        cycle: int,
        authorization_digest: str,
    ) -> dict[str, Any]:
        if (
            type(cycle) is not int
            or cycle < 1
            or cycle > MAX_CANDIDATE_REPAIR_CYCLES_PER_CHAPTER
        ):
            raise ReferenceCardRepairReceiptConflict(
                "reference-card repair cycle is invalid"
            )
        if (
            not isinstance(authorization_digest, str)
            or len(authorization_digest) != 64
            or any(char not in "0123456789abcdef" for char in authorization_digest)
        ):
            raise ReferenceCardRepairReceiptConflict(
                "reference-card repair authorization digest is invalid"
            )
        return {
            "_id": cls._receipt_id(
                owner_id=owner_id,
                novel_id=novel_id,
                job_id=job_id,
                chapter_id=chapter_id,
                cycle=cycle,
                authorization_digest=authorization_digest,
            ),
            "owner_id": to_object_id(owner_id),
            "novel_id": to_object_id(novel_id),
            "job_id": to_object_id(job_id),
            "chapter_id": to_object_id(chapter_id),
            "cycle": cycle,
            "authorization_digest": authorization_digest,
        }

    async def find_identity(
        self,
        *,
        owner_id: str,
        novel_id: str,
        job_id: str,
        chapter_id: str,
        cycle: int,
        authorization_digest: str,
    ) -> dict[str, Any] | None:
        scope = self._scope(
            owner_id=owner_id,
            novel_id=novel_id,
            job_id=job_id,
            chapter_id=chapter_id,
            cycle=cycle,
            authorization_digest=authorization_digest,
        )
        document = await self.collection.find_one({"_id": scope["_id"]})
        if document is None:
            return None
        frozen = self._validated(document)
        if any(frozen.get(field) != expected for field, expected in scope.items()):
            raise ReferenceCardRepairReceiptConflict(
                "reference-card repair receipt scope changed"
            )
        return frozen

    @staticmethod
    def _validated(document: Mapping[str, Any]) -> dict[str, Any]:
        try:
            parsed = ReferenceCardRepairReceiptEnvelope.model_validate(
                dict(document)
            )
        except (TypeError, ValueError) as exc:
            raise ReferenceCardRepairReceiptConflict(
                "reference-card repair receipt is invalid"
            ) from exc
        return parsed.model_dump(mode="python", by_alias=True)

    async def acquire(
        self,
        *,
        owner_id: str,
        novel_id: str,
        job_id: str,
        chapter_id: str,
        cycle: int,
        authorization_digest: str,
        request_digest: str,
        source_digest: str,
        claim_token: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        scope = self._scope(
            owner_id=owner_id,
            novel_id=novel_id,
            job_id=job_id,
            chapter_id=chapter_id,
            cycle=cycle,
            authorization_digest=authorization_digest,
        )
        if any(
            not isinstance(value, str)
            or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)
            for value in (request_digest, source_digest)
        ):
            raise ReferenceCardRepairReceiptConflict(
                "reference-card repair evidence digest is invalid"
            )
        if not isinstance(claim_token, str) or not claim_token or len(claim_token) > 128:
            raise ReferenceCardRepairReceiptConflict(
                "reference-card repair claim token is invalid"
            )
        current_time = now or get_utc_now()
        document = {
            **scope,
            "schema_version": REFERENCE_CARD_REPAIR_RECEIPT_SCHEMA,
            "request_digest": request_digest,
            "source_digest": source_digest,
            "state": "reserved",
            "claim_token": claim_token,
            "claim_epoch": 1,
            "claim_expires_at": current_time + timedelta(
                seconds=_CLAIM_TTL_SECONDS
            ),
            "provider_attempt_ids": [],
            "result": None,
            "result_digest": None,
            "finish_reason": None,
            "created_at": current_time,
            "updated_at": current_time,
            "completed_at": None,
            "is_deleted": False,
        }
        try:
            await self.collection.insert_one(document)
            return self._validated(document)
        except DuplicateKeyError:
            pass

        existing = await self.collection.find_one({"_id": scope["_id"]})
        if existing is None:
            raise ReferenceCardRepairReceiptConflict(
                "reference-card repair receipt disappeared"
            )
        frozen = self._validated(existing)
        if any(
            frozen.get(field) != expected
            for field, expected in (
                ("owner_id", scope["owner_id"]),
                ("novel_id", scope["novel_id"]),
                ("job_id", scope["job_id"]),
                ("chapter_id", scope["chapter_id"]),
                ("cycle", cycle),
                ("authorization_digest", authorization_digest),
                ("request_digest", request_digest),
                ("source_digest", source_digest),
            )
        ):
            raise ReferenceCardRepairReceiptConflict(
                "reference-card repair receipt evidence changed"
            )
        if frozen["state"] != "reserved" or frozen["claim_token"] == claim_token:
            return frozen
        expires_at = frozen.get("claim_expires_at")
        if isinstance(expires_at, datetime) and expires_at > current_time:
            return frozen
        epoch = int(frozen["claim_epoch"])
        if epoch >= _MAX_CLAIM_EPOCH:
            raise ReferenceCardRepairReceiptConflict(
                "reference-card repair claim epoch is exhausted"
            )
        replaced = await self.collection.find_one_and_update(
            {
                "_id": scope["_id"],
                "state": "reserved",
                "claim_token": frozen["claim_token"],
                "claim_epoch": epoch,
                "claim_expires_at": expires_at,
            },
            {
                "$set": {
                    "claim_token": claim_token,
                    "claim_epoch": epoch + 1,
                    "claim_expires_at": current_time + timedelta(
                        seconds=_CLAIM_TTL_SECONDS
                    ),
                    "updated_at": current_time,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if replaced is None:
            latest = await self.collection.find_one({"_id": scope["_id"]})
            if latest is None:
                raise ReferenceCardRepairReceiptConflict(
                    "reference-card repair receipt disappeared"
                )
            return self._validated(latest)
        return self._validated(replaced)

    async def mark_dispatched(
        self,
        *,
        receipt_id: str,
        claim_token: str,
        claim_epoch: int,
        attempt_id: str,
    ) -> dict[str, Any]:
        if not isinstance(attempt_id, str) or not attempt_id or len(attempt_id) > 128:
            raise ReferenceCardRepairReceiptConflict(
                "reference-card repair attempt id is invalid"
            )
        now = get_utc_now()
        result = await self.collection.find_one_and_update(
            {
                "_id": receipt_id,
                "state": {"$in": ["reserved", "dispatched"]},
                "claim_token": claim_token,
                "claim_epoch": claim_epoch,
            },
            {
                "$set": {
                    "state": "dispatched",
                    "claim_expires_at": None,
                    "updated_at": now,
                },
                "$addToSet": {"provider_attempt_ids": attempt_id},
            },
            return_document=ReturnDocument.AFTER,
        )
        if result is None:
            raise ReferenceCardRepairReceiptConflict(
                "reference-card repair dispatch fence changed"
            )
        return self._validated(result)

    async def release_reserved_claim(
        self,
        *,
        receipt_id: str,
        claim_token: str,
        claim_epoch: int,
    ) -> bool:
        """Expire a handled pre-dispatch claim so the same request can retry."""

        now = get_utc_now()
        result = await self.collection.update_one(
            {
                "_id": receipt_id,
                "state": "reserved",
                "claim_token": claim_token,
                "claim_epoch": claim_epoch,
            },
            {
                "$set": {
                    "claim_expires_at": now,
                    "updated_at": now,
                }
            },
        )
        return result.modified_count == 1

    async def complete(
        self,
        *,
        receipt_id: str,
        claim_token: str,
        claim_epoch: int,
        provider_attempt_ids: list[str],
        result: Mapping[str, Any],
        result_digest: str,
        finish_reason: str,
    ) -> dict[str, Any]:
        parsed_result = ChapterOutlineProposalSchema.model_validate(
            dict(result)
        ).model_dump(mode="json")
        expected_result_digest = hashlib.sha256(
            json.dumps(
                parsed_result,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if result_digest != expected_result_digest:
            raise ReferenceCardRepairReceiptConflict(
                "reference-card repair result digest is invalid"
            )
        if not isinstance(finish_reason, str) or not finish_reason or len(finish_reason) > 80:
            raise ReferenceCardRepairReceiptConflict(
                "reference-card repair finish reason is invalid"
            )
        now = get_utc_now()
        updated = await self.collection.find_one_and_update(
            {
                "_id": receipt_id,
                "state": "dispatched",
                "claim_token": claim_token,
                "claim_epoch": claim_epoch,
                "provider_attempt_ids": provider_attempt_ids,
            },
            {
                "$set": {
                    "state": "completed",
                    "result": parsed_result,
                    "result_digest": result_digest,
                    "finish_reason": finish_reason,
                    "completed_at": now,
                    "updated_at": now,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if updated is not None:
            return self._validated(updated)
        existing = await self.collection.find_one({"_id": receipt_id})
        if existing is None:
            raise ReferenceCardRepairReceiptConflict(
                "reference-card repair receipt disappeared"
            )
        frozen = self._validated(existing)
        if (
            frozen["state"] == "completed"
            and frozen["provider_attempt_ids"] == provider_attempt_ids
            and frozen["result"] == parsed_result
            and frozen["result_digest"] == result_digest
            and frozen["finish_reason"] == finish_reason
        ):
            return frozen
        raise ReferenceCardRepairReceiptConflict(
            "reference-card repair completion changed"
        )


reference_card_repair_receipt_repo = ReferenceCardRepairReceiptRepository()
