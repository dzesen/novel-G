"""Persisted, user-owned prose drafts and segment checkpoints."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from backend.db import collections
from backend.db.base import BaseRepository
from backend.db.errors import NotFoundError
from backend.db.mongo import get_database
from backend.db.remediation_receipts import (
    InvalidRemediationReceiptPointer,
    parse_remediation_receipt_pointer,
)
from backend.db.repositories.generation_job_repository import (
    TokenBudgetExceeded,
    TokenBudgetUnbounded,
)
from backend.db.utils import get_utc_now, to_object_id
from backend.llm.models import TokenUsage


class StaleProseRun(ValueError):
    """The requested revision or execution lease no longer owns the run."""


CURRENT_PROSE_RUN_STATUSES = ("active", "incomplete", "complete")
LEFTOVER_PROSE_RUN_STATUSES = ("incomplete", "superseded", "stale")
DISCARDABLE_PROSE_RUN_STATUSES = (
    "active",
    "incomplete",
    "complete",
    "superseded",
    "stale",
)


class ProseRunRepository(BaseRepository):
    def __init__(self) -> None:
        super().__init__(collections.PROSE_RUNS)

    async def create_run(
        self,
        document: dict[str, Any],
        *,
        replace_run_id: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        owner_id = to_object_id(document["owner_id"])
        chapter_id = to_object_id(document["chapter_id"])
        now = get_utc_now()
        if replace_run_id is not None:
            if expected_revision is None:
                raise ValueError(
                    "Replacing a prose run requires its expected revision"
                )
            replaced = await self.collection.update_one(
                {
                    "_id": to_object_id(replace_run_id),
                    "owner_id": owner_id,
                    "chapter_id": chapter_id,
                    "revision": int(expected_revision),
                    "status": {"$in": list(CURRENT_PROSE_RUN_STATUSES)},
                    "is_deleted": False,
                    "$or": [
                        {"lease": None},
                        {"lease.expires_at": {"$lte": now}},
                    ],
                },
                {
                    "$set": {
                        "status": "superseded",
                        "updated_at": now,
                    }
                },
            )
            if replaced.modified_count != 1:
                raise StaleProseRun(
                    "正文草稿已被其他页面继续或重新生成"
                )
        else:
            if expected_revision is not None:
                raise ValueError(
                    "expected_revision is only valid for a replacement run"
                )
            await self.collection.update_many(
                {
                    "owner_id": owner_id,
                    "chapter_id": chapter_id,
                    "status": {"$in": list(CURRENT_PROSE_RUN_STATUSES)},
                    "is_deleted": False,
                },
                {
                    "$set": {
                        "status": "superseded",
                        "updated_at": now,
                    }
                },
            )
        prepared = {
            **document,
            "owner_id": owner_id,
            "novel_id": to_object_id(document["novel_id"]),
            "chapter_id": chapter_id,
            "revision": 1,
            "segments": [],
            "scene_progress": [],
            "status": "active",
            "lease": None,
            "tokens_used": int(document.get("tokens_used") or 0),
            "tokens_reserved": int(document.get("tokens_reserved") or 0),
            "frozen_tokens_reserved": int(
                document.get("frozen_tokens_reserved") or 0
            ),
            "active_token_reservation": None,
            "has_uncertain_attempt": False,
            "provider_attempt_count": 0,
        }
        generation_job_id = document.get("generation_job_id")
        if generation_job_id is None:
            prepared.pop("generation_job_id", None)
        else:
            prepared["generation_job_id"] = to_object_id(generation_job_id)
        try:
            run_id = await self.insert_one(prepared)
        except DuplicateKeyError as exc:
            raise StaleProseRun(
                "正文草稿已被其他页面重新生成"
            ) from exc
        return await self.get_run(run_id, str(owner_id))

    async def get_run(self, run_id: str, owner_id: str) -> dict[str, Any]:
        document = await self.find_one(
            {
                "_id": to_object_id(run_id),
                "owner_id": to_object_id(owner_id),
            }
        )
        if document is None:
            raise NotFoundError(f"Prose run not found: {run_id}")
        return document

    async def find_active(
        self,
        *,
        chapter_id: str,
        owner_id: str,
    ) -> dict[str, Any] | None:
        documents = await self.find_many(
            {
                "chapter_id": to_object_id(chapter_id),
                "owner_id": to_object_id(owner_id),
                "status": {"$in": list(CURRENT_PROSE_RUN_STATUSES)},
            },
            limit=1,
            sort=[("updated_at", -1)],
        )
        return documents[0] if documents else None

    async def list_leftovers(
        self,
        *,
        novel_id: str,
        owner_id: str,
    ) -> list[dict[str, Any]]:
        """List unresolved, user-owned prose drafts without mutating them."""
        return await self.find_many(
            {
                "novel_id": to_object_id(novel_id),
                "owner_id": to_object_id(owner_id),
                "status": {"$in": list(LEFTOVER_PROSE_RUN_STATUSES)},
                "completion.can_write_formal_prose": {"$ne": True},
            },
            sort=[("updated_at", -1), ("_id", -1)],
        )

    async def list_telemetry_by_novel(
        self,
        *,
        novel_id: str,
        owner_id: str,
        limit: int = 100,
        skip: int = 0,
        chapter_id: str | None = None,
        generation_job_id: str | None = None,
        related_run_ids: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        """List user-owned runs for metadata-only operational inspection."""
        query: dict[str, Any] = {
            "novel_id": to_object_id(novel_id),
            "owner_id": to_object_id(owner_id),
        }
        if chapter_id is not None:
            query["chapter_id"] = to_object_id(chapter_id)
        if generation_job_id is not None:
            associations: list[dict[str, Any]] = [{
                "generation_job_id": to_object_id(generation_job_id),
            }]
            if related_run_ids:
                associations.append({
                    "_id": {
                        "$in": [to_object_id(run_id) for run_id in related_run_ids]
                    },
                })
            query["$or"] = associations
        return await self.find_many(
            query,
            limit=max(1, int(limit)),
            skip=max(0, int(skip)),
            sort=[("updated_at", -1), ("_id", -1)],
        )

    async def discard(
        self,
        *,
        run_id: str,
        owner_id: str,
        novel_id: str,
        chapter_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Discard a stable, idle snapshot without deleting its recovery data."""
        now = get_utc_now()
        document = await self.collection.find_one_and_update(
            {
                "_id": to_object_id(run_id),
                "owner_id": to_object_id(owner_id),
                "novel_id": to_object_id(novel_id),
                "chapter_id": to_object_id(chapter_id),
                "is_deleted": False,
                "revision": int(expected_revision),
                "status": {"$in": list(DISCARDABLE_PROSE_RUN_STATUSES)},
                "$or": [
                    {"lease": None},
                    {"lease.expires_at": {"$lte": now}},
                ],
            },
            {
                "$set": {
                    "status": "discarded",
                    "lease": None,
                    "updated_at": now,
                },
                "$inc": {"revision": 1},
            },
            return_document=ReturnDocument.AFTER,
        )
        if document is None:
            raise StaleProseRun(
                "正文草稿已被其他页面继续、写入或丢弃，请刷新后重试"
            )
        return document

    async def claim(
        self,
        *,
        run_id: str,
        owner_id: str,
        expected_revision: int,
        lease_seconds: int = 300,
    ) -> dict[str, Any]:
        now = get_utc_now()
        token = uuid4().hex
        try:
            document = await self.collection.find_one_and_update(
                {
                    "_id": to_object_id(run_id),
                    "owner_id": to_object_id(owner_id),
                    "is_deleted": False,
                    "revision": int(expected_revision),
                    "status": {"$in": list(CURRENT_PROSE_RUN_STATUSES)},
                    "$or": [
                        {"lease": None},
                        {"lease.expires_at": {"$lte": now}},
                    ],
                },
                {
                    "$set": {
                        "lease": {
                            "token": token,
                            "claimed_at": now,
                            "expires_at": now + timedelta(
                                seconds=max(30, lease_seconds)
                            ),
                        },
                        "status": "active",
                        "updated_at": now,
                    },
                    "$inc": {"revision": 1},
                },
                return_document=ReturnDocument.AFTER,
            )
        except DuplicateKeyError as exc:
            raise StaleProseRun(
                "正文草稿已被其他页面继续或重新生成"
            ) from exc
        if document is None:
            raise StaleProseRun("正文草稿已被其他页面继续或版本已经变化")
        return document

    async def append_segment(
        self,
        *,
        run_id: str,
        owner_id: str,
        lease_token: str,
        segment: dict[str, Any],
    ) -> dict[str, Any]:
        sequence = int(segment["sequence_index"])
        now = get_utc_now()
        base_query = {
            "_id": to_object_id(run_id),
            "owner_id": to_object_id(owner_id),
            "is_deleted": False,
            "status": {"$in": list(CURRENT_PROSE_RUN_STATUSES)},
            "lease.token": str(lease_token),
        }
        existing = await self.collection.find_one(
            {
                **base_query,
                "segments": {"$elemMatch": {"sequence_index": sequence}},
            },
            projection={"_id": 1},
        )
        update: dict[str, Any]
        array_filters = None
        if existing is not None:
            update = {
                "$set": {
                    "segments.$[segment]": dict(segment),
                    "status": (
                        "active"
                        if segment.get("status") == "completed"
                        else "incomplete"
                    ),
                    "updated_at": now,
                    "lease.expires_at": now + timedelta(minutes=5),
                },
                "$inc": {"revision": 1},
            }
            array_filters = [{"segment.sequence_index": sequence}]
        else:
            update = {
                "$push": {"segments": dict(segment)},
                "$inc": {"revision": 1},
                "$set": {
                    "status": (
                        "active"
                        if segment.get("status") == "completed"
                        else "incomplete"
                    ),
                    "updated_at": now,
                    "lease.expires_at": now + timedelta(minutes=5),
                },
            }
        document = await self.collection.find_one_and_update(
            {
                **base_query,
            },
            update,
            array_filters=array_filters,
            return_document=ReturnDocument.AFTER,
        )
        if document is None:
            raise StaleProseRun("正文分段执行租约已失效")
        return document

    async def update_scene_progress(
        self,
        *,
        run_id: str,
        owner_id: str,
        lease_token: str,
        scene_progress: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Persist a scene-level checkpoint without inventing a prose segment."""
        now = get_utc_now()
        paused = any(
            str(item.get("status") or "") == "paused"
            for item in scene_progress
        )
        document = await self.collection.find_one_and_update(
            {
                "_id": to_object_id(run_id),
                "owner_id": to_object_id(owner_id),
                "is_deleted": False,
                "status": {"$in": list(CURRENT_PROSE_RUN_STATUSES)},
                "lease.token": str(lease_token),
            },
            {
                "$set": {
                    "scene_progress": [dict(item) for item in scene_progress],
                    "status": "incomplete" if paused else "active",
                    "updated_at": now,
                    "lease.expires_at": now + timedelta(minutes=5),
                },
                "$inc": {"revision": 1},
            },
            return_document=ReturnDocument.AFTER,
        )
        if document is None:
            raise StaleProseRun("正文场景进度的执行租约已失效")
        return document

    async def update_authorization(
        self,
        *,
        run_id: str,
        owner_id: str,
        lease_token: str,
        authorization: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist a new authorization without changing the content identity."""
        document = await self.collection.find_one_and_update(
            {
                "_id": to_object_id(run_id),
                "owner_id": to_object_id(owner_id),
                "is_deleted": False,
                "lease.token": str(lease_token),
                "status": {"$in": list(CURRENT_PROSE_RUN_STATUSES)},
            },
            {
                "$set": {
                    "prose_continuation_authorization": dict(authorization),
                    "authorization_revision": int(
                        authorization.get("authorization_revision") or 1
                    ),
                    "token_budget": authorization.get("token_budget"),
                    "updated_at": get_utc_now(),
                },
                "$inc": {"revision": 1},
            },
            return_document=ReturnDocument.AFTER,
        )
        if document is None:
            raise StaleProseRun("正文续写授权的执行租约已失效")
        return document

    async def claim_call_budget(
        self,
        *,
        run_id: str,
        owner_id: str,
        lease_token: str,
        provider_alias: str,
        phase: str,
        conservative_tokens: int | None,
    ) -> str:
        """Claim the one in-flight prose call and reserve its token ceiling."""
        reserved = (
            None
            if conservative_tokens is None
            else max(1, int(conservative_tokens))
        )
        attempt_id = uuid4().hex
        now = get_utc_now()
        reservation = {
            "attempt_id": attempt_id,
            "provider_alias": str(provider_alias),
            "phase": str(phase),
            "conservative_tokens": reserved,
            "state": "claimed",
            "reserved_at": now,
        }
        query: dict[str, Any] = {
            "_id": to_object_id(run_id),
            "owner_id": to_object_id(owner_id),
            "is_deleted": False,
            "status": {"$in": list(CURRENT_PROSE_RUN_STATUSES)},
            "lease.token": str(lease_token),
            "$or": [
                {"active_token_reservation": None},
                {"active_token_reservation": {"$exists": False}},
            ],
        }
        if reserved is None:
            query["token_budget"] = None
        else:
            query["$expr"] = {
                "$or": [
                    {"$eq": [{"$ifNull": ["$token_budget", None]}, None]},
                    {
                        "$lte": [
                            {
                                "$add": [
                                    {"$ifNull": ["$tokens_used", 0]},
                                    {"$ifNull": ["$tokens_reserved", 0]},
                                    reserved,
                                ]
                            },
                            {"$ifNull": ["$token_budget", 0]},
                        ]
                    },
                ]
            }
        update: dict[str, Any] = {
            "$set": {
                "active_token_reservation": reservation,
                "updated_at": now,
                "lease.expires_at": now + timedelta(minutes=5),
            },
            "$inc": {"provider_attempt_count": 1},
        }
        if reserved is not None:
            update["$inc"]["tokens_reserved"] = reserved
        document = await self.collection.find_one_and_update(
            query,
            update,
            return_document=ReturnDocument.AFTER,
        )
        if document is not None:
            return attempt_id

        current = await self.get_run(run_id, owner_id)
        budget = current.get("token_budget")
        if reserved is None and budget is not None:
            raise TokenBudgetUnbounded(
                "A finite token budget requires a conservative Provider bound"
            )
        if budget is not None and reserved is not None:
            used = int(current.get("tokens_used") or 0)
            already_reserved = int(current.get("tokens_reserved") or 0)
            if used + already_reserved + reserved > int(budget):
                raise TokenBudgetExceeded(
                    "Token budget would be exceeded before Provider dispatch"
                )
        if current.get("active_token_reservation") is not None:
            raise StaleProseRun("正文草稿已有未结算的 Provider 调用")
        raise StaleProseRun("正文草稿的执行租约已失效")

    async def settle_call_budget(
        self,
        *,
        run_id: str,
        owner_id: str,
        lease_token: str,
        attempt_id: str,
        usage: TokenUsage,
        conservative_tokens: int | None,
    ) -> bool:
        """Release one reservation and add actual or conservative usage once."""
        reserved = (
            None
            if conservative_tokens is None
            else max(1, int(conservative_tokens))
        )
        observed = max(
            int(usage.total_tokens or 0),
            int(usage.input_tokens or 0) + int(usage.output_tokens or 0),
        )
        charged = observed if observed > 0 else reserved
        now = get_utc_now()
        query = {
            "_id": to_object_id(run_id),
            "owner_id": to_object_id(owner_id),
            "is_deleted": False,
            "lease.token": str(lease_token),
            "active_token_reservation.attempt_id": str(attempt_id),
            "active_token_reservation.conservative_tokens": reserved,
            "active_token_reservation.state": {"$in": ["claimed", "uncertain"]},
        }
        increments: dict[str, int] = {}
        if reserved is not None:
            increments["tokens_reserved"] = -reserved
        if charged is not None:
            increments["tokens_used"] = int(charged)
        update: dict[str, Any] = {
            "$set": {
                "active_token_reservation": None,
                "has_uncertain_attempt": False,
                "last_provider_attempt": {
                    "attempt_id": str(attempt_id),
                    "state": "accounted",
                    "usage": usage.model_dump(),
                    "charged_tokens": charged,
                    "accounted_at": now,
                },
                "updated_at": now,
                "lease.expires_at": now + timedelta(minutes=5),
            },
        }
        if increments:
            update["$inc"] = increments
        result = await self.collection.update_one(query, update)
        return result.modified_count == 1

    async def mark_call_budget_uncertain(
        self,
        *,
        run_id: str,
        owner_id: str,
        lease_token: str,
        attempt_id: str,
        reason: str,
    ) -> bool:
        """Keep an unknown dispatched call reserved until the user decides."""
        now = get_utc_now()
        result = await self.collection.update_one(
            {
                "_id": to_object_id(run_id),
                "owner_id": to_object_id(owner_id),
                "is_deleted": False,
                "lease.token": str(lease_token),
                "active_token_reservation.attempt_id": str(attempt_id),
                "active_token_reservation.state": "claimed",
            },
            {
                "$set": {
                    "active_token_reservation.state": "uncertain",
                    "active_token_reservation.uncertain_reason": str(reason),
                    "active_token_reservation.updated_at": now,
                    "has_uncertain_attempt": True,
                    "updated_at": now,
                    "lease.expires_at": now + timedelta(minutes=5),
                }
            },
        )
        return result.modified_count == 1

    async def acknowledge_uncertain_call_budget(
        self,
        *,
        run_id: str,
        owner_id: str,
        lease_token: str,
        action: str,
    ) -> bool:
        """Freeze an acknowledged uncertain cost without retaining an attempt log."""
        if action not in {"retry", "skip"}:
            raise ValueError("Unknown uncertain prose attempt action")
        current = await self.get_run(run_id, owner_id)
        reservation = dict(current.get("active_token_reservation") or {})
        if reservation.get("state") != "uncertain":
            return False
        reserved = int(reservation.get("conservative_tokens") or 0)
        now = get_utc_now()
        result = await self.collection.update_one(
            {
                "_id": to_object_id(run_id),
                "owner_id": to_object_id(owner_id),
                "is_deleted": False,
                "lease.token": str(lease_token),
                "active_token_reservation.attempt_id": str(
                    reservation.get("attempt_id") or ""
                ),
                "active_token_reservation.state": "uncertain",
            },
            {
                "$inc": {"frozen_tokens_reserved": reserved},
                "$set": {
                    "active_token_reservation": None,
                    "has_uncertain_attempt": False,
                    "last_provider_attempt": {
                        "attempt_id": str(reservation.get("attempt_id") or ""),
                        "state": f"uncertain_{action}_acknowledged",
                        "acknowledged_at": now,
                    },
                    "updated_at": now,
                    "lease.expires_at": now + timedelta(minutes=5),
                },
            },
        )
        return result.modified_count == 1

    async def release_call_budget_pre_dispatch(
        self,
        *,
        run_id: str,
        owner_id: str,
        lease_token: str,
        attempt_id: str,
        conservative_tokens: int | None,
        reason: str,
    ) -> bool:
        """Release only a proven pre-dispatch rejection."""
        reserved = (
            None
            if conservative_tokens is None
            else max(1, int(conservative_tokens))
        )
        now = get_utc_now()
        query = {
            "_id": to_object_id(run_id),
            "owner_id": to_object_id(owner_id),
            "is_deleted": False,
            "lease.token": str(lease_token),
            "active_token_reservation.attempt_id": str(attempt_id),
            "active_token_reservation.conservative_tokens": reserved,
            "active_token_reservation.state": "claimed",
        }
        update: dict[str, Any] = {
            "$set": {
                "active_token_reservation": None,
                "last_provider_attempt": {
                    "attempt_id": str(attempt_id),
                    "state": "released_pre_dispatch",
                    "release_reason": str(reason),
                    "released_at": now,
                },
                "updated_at": now,
                "lease.expires_at": now + timedelta(minutes=5),
            }
        }
        if reserved is not None:
            update["$inc"] = {"tokens_reserved": -reserved}
        result = await self.collection.update_one(query, update)
        return result.modified_count == 1

    @property
    def remediation_receipts(self):
        return get_database()[collections.PROSE_REMEDIATION_RECEIPTS]

    @staticmethod
    def _remediation_receipt_id(
        *,
        run_id: str,
        owner_id: str,
        novel_id: str,
        idempotency_key: str,
    ) -> str:
        identity = "\x1f".join((
            str(owner_id),
            str(novel_id),
            str(run_id),
            str(idempotency_key),
        ))
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    @staticmethod
    def _matching_legacy_remediation_receipt(
        document: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        return next(
            (
                dict(item)
                for item in document.get("remediation_receipts") or []
                if str(item.get("idempotency_key") or "")
                == str(idempotency_key)
            ),
            None,
        )

    @staticmethod
    def _validate_remediation_receipt_digest(
        receipt: dict[str, Any],
        *,
        request_digest: str,
    ) -> None:
        if str(receipt.get("request_digest") or "") != str(request_digest):
            raise StaleProseRun(
                "同一正文修复幂等键对应了不同的候选输入"
            )

    @staticmethod
    def _validate_remediation_receipt_scope(
        receipt: dict[str, Any],
        *,
        run_id: str,
        owner_id: str,
        novel_id: str,
        source_revision: int | None = None,
    ) -> None:
        if (
            str(receipt.get("prose_run_id") or "") != str(run_id)
            or str(receipt.get("owner_id") or "") != str(owner_id)
            or str(receipt.get("novel_id") or "") != str(novel_id)
            or receipt.get("is_deleted") is True
        ):
            raise StaleProseRun("正文修复回执不属于当前授权作用域")
        if (
            source_revision is not None
            and int(receipt.get("source_revision") or 0)
            != int(source_revision)
        ):
            raise StaleProseRun("正文修复回执的来源版本不一致")

    async def _owned_remediation_run(
        self,
        *,
        run_id: str,
        owner_id: str,
        novel_id: str,
    ) -> dict[str, Any]:
        document = await self.collection.find_one({
            "_id": to_object_id(run_id),
            "owner_id": to_object_id(owner_id),
            "novel_id": to_object_id(novel_id),
            "is_deleted": False,
        })
        if document is None:
            raise NotFoundError("正文草稿不存在")
        return document

    async def _migrate_legacy_remediation_receipt(
        self,
        *,
        document: dict[str, Any],
        run_id: str,
        owner_id: str,
        novel_id: str,
        idempotency_key: str,
        request_digest: str,
    ) -> dict[str, Any] | None:
        """Lazily preserve receipts written by the preceding embedded format."""
        legacy = self._matching_legacy_remediation_receipt(
            document,
            idempotency_key=idempotency_key,
        )
        if legacy is None:
            return None
        self._validate_remediation_receipt_digest(
            legacy,
            request_digest=request_digest,
        )
        now = get_utc_now()
        receipt_id = self._remediation_receipt_id(
            run_id=run_id,
            owner_id=owner_id,
            novel_id=novel_id,
            idempotency_key=idempotency_key,
        )
        migrated = {
            **legacy,
            "_id": receipt_id,
            "owner_id": to_object_id(owner_id),
            "novel_id": to_object_id(novel_id),
            "prose_run_id": to_object_id(run_id),
            "idempotency_key": str(idempotency_key),
            "request_digest": str(request_digest),
            "state": str(legacy.get("state") or "completed"),
            "created_at": legacy.get("created_at") or now,
            "updated_at": legacy.get("updated_at") or now,
            "is_deleted": False,
        }
        await self.remediation_receipts.update_one(
            {"_id": receipt_id},
            {"$setOnInsert": migrated},
            upsert=True,
        )
        return await self.remediation_receipts.find_one({"_id": receipt_id})

    @staticmethod
    def _remediation_receipt_pointer(
        document: dict[str, Any],
    ) -> dict[str, Any] | None:
        try:
            return parse_remediation_receipt_pointer(document)
        except InvalidRemediationReceiptPointer as exc:
            raise StaleProseRun("正文修复回执指针证据无效") from exc

    @classmethod
    def _latest_remediation_receipt(
        cls,
        document: dict[str, Any],
        *,
        idempotency_key: str,
        request_digest: str,
    ) -> dict[str, Any] | None:
        latest = cls._remediation_receipt_pointer(document)
        if latest is None:
            return None
        if (
            str(latest.get("idempotency_key") or "")
            != str(idempotency_key)
            or str(latest.get("request_digest") or "")
            != str(request_digest)
        ):
            return None
        return latest

    async def _repair_remediation_receipt_from_pointer(
        self,
        *,
        document: dict[str, Any],
        receipt: dict[str, Any],
        run_id: str,
        owner_id: str,
        novel_id: str,
        idempotency_key: str,
        request_digest: str,
    ) -> dict[str, Any]:
        if str(receipt.get("state") or "") == "completed":
            return receipt
        latest = self._latest_remediation_receipt(
            document,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
        )
        if latest is None:
            return receipt
        await self.remediation_receipts.update_one(
            {
                "_id": receipt["_id"],
                "owner_id": to_object_id(owner_id),
                "novel_id": to_object_id(novel_id),
                "prose_run_id": to_object_id(run_id),
                "request_digest": str(request_digest),
                "state": {"$in": ["reserved", "dispatched"]},
            },
            {
                "$set": {
                    "state": "completed",
                    "result_revision": int(latest["result_revision"]),
                    "result_projection": dict(latest["result_projection"]),
                    "completed_at": latest.get("completed_at")
                    or get_utc_now(),
                    "updated_at": get_utc_now(),
                },
                "$unset": {"claim_expires_at": ""},
            },
        )
        repaired = await self.remediation_receipts.find_one({
            "_id": receipt["_id"],
        })
        if repaired is None:
            raise StaleProseRun("正文修复回执恢复后丢失")
        return repaired

    async def _drain_latest_remediation_receipt(
        self,
        *,
        document: dict[str, Any],
        run_id: str,
        owner_id: str,
        novel_id: str,
    ) -> dict[str, Any] | None:
        """Close the current pointer before a later rewrite can replace it."""
        latest = self._remediation_receipt_pointer(document)
        if latest is None:
            return None
        idempotency_key = str(latest["idempotency_key"])
        request_digest = str(latest["request_digest"])
        receipt = await self.remediation_receipts.find_one({
            "_id": self._remediation_receipt_id(
                run_id=run_id,
                owner_id=owner_id,
                novel_id=novel_id,
                idempotency_key=idempotency_key,
            )
        })
        if receipt is None:
            raise StaleProseRun("正文修复回执指针缺少持久回执")
        self._validate_remediation_receipt_scope(
            receipt,
            run_id=run_id,
            owner_id=owner_id,
            novel_id=novel_id,
            source_revision=int(latest["source_revision"]),
        )
        self._validate_remediation_receipt_digest(
            receipt,
            request_digest=request_digest,
        )
        receipt = await self._repair_remediation_receipt_from_pointer(
            document=document,
            receipt=receipt,
            run_id=run_id,
            owner_id=owner_id,
            novel_id=novel_id,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
        )
        if (
            str(receipt.get("state") or "") != "completed"
            or int(receipt.get("result_revision") or 0)
            != int(latest["result_revision"])
            or dict(receipt.get("result_projection") or {})
            != dict(latest["result_projection"])
        ):
            raise StaleProseRun("正文修复回执指针尚未闭合")
        return latest

    async def claim_remediation_receipt(
        self,
        *,
        run_id: str,
        owner_id: str,
        novel_id: str,
        idempotency_key: str,
        request_digest: str,
        source_revision: int,
        claim_token: str,
        force_reclaim_reserved: bool = False,
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        """Claim one durable rewrite identity before Provider dispatch.

        ``force_reclaim_reserved`` is reserved for an enclosing Agent worker
        that already owns the newer run lease and is closing a deadline or
        revision boundary.  The CAS revokes only a receipt that still proves
        the inner Provider was not dispatched; a concurrent dispatch wins and
        remains unknown.
        """
        now = get_utc_now()
        document = await self._owned_remediation_run(
            run_id=run_id,
            owner_id=owner_id,
            novel_id=novel_id,
        )
        await self._drain_latest_remediation_receipt(
            document=document,
            run_id=run_id,
            owner_id=owner_id,
            novel_id=novel_id,
        )
        receipt_id = self._remediation_receipt_id(
            run_id=run_id,
            owner_id=owner_id,
            novel_id=novel_id,
            idempotency_key=idempotency_key,
        )
        claim = {
            "_id": receipt_id,
            "owner_id": to_object_id(owner_id),
            "novel_id": to_object_id(novel_id),
            "prose_run_id": to_object_id(run_id),
            "idempotency_key": str(idempotency_key),
            "request_digest": str(request_digest),
            "source_revision": int(source_revision),
            "state": "reserved",
            "claim_token": str(claim_token),
            "claim_expires_at": now + timedelta(seconds=30),
            "created_at": now,
            "updated_at": now,
            "is_deleted": False,
        }
        receipt = await self._migrate_legacy_remediation_receipt(
            document=document,
            run_id=run_id,
            owner_id=owner_id,
            novel_id=novel_id,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
        )
        if receipt is None:
            receipt = await self.remediation_receipts.find_one_and_update(
                {"_id": receipt_id},
                {"$setOnInsert": claim},
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
        if receipt is None:
            raise StaleProseRun("正文修复回执占用失败")
        self._validate_remediation_receipt_scope(
            receipt,
            run_id=run_id,
            owner_id=owner_id,
            novel_id=novel_id,
            source_revision=source_revision,
        )
        self._validate_remediation_receipt_digest(
            receipt,
            request_digest=request_digest,
        )
        receipt = await self._repair_remediation_receipt_from_pointer(
            document=document,
            receipt=receipt,
            run_id=run_id,
            owner_id=owner_id,
            novel_id=novel_id,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
        )
        state = str(receipt.get("state") or "completed")
        if state == "completed":
            return (
                "completed",
                await self._owned_remediation_run(
                    run_id=run_id,
                    owner_id=owner_id,
                    novel_id=novel_id,
                ),
                receipt,
            )
        if (
            state == "reserved"
            and str(receipt.get("claim_token") or "") == str(claim_token)
        ):
            return "claimed", document, receipt
        if state == "reserved":
            expires_at = receipt.get("claim_expires_at")
            if force_reclaim_reserved or (
                expires_at is not None and expires_at <= now
            ):
                reclaimed = await self.remediation_receipts.find_one_and_update(
                    {
                        "_id": receipt_id,
                        "owner_id": to_object_id(owner_id),
                        "novel_id": to_object_id(novel_id),
                        "prose_run_id": to_object_id(run_id),
                        "is_deleted": False,
                        "idempotency_key": str(idempotency_key),
                        "request_digest": str(request_digest),
                        "state": "reserved",
                        "claim_token": str(receipt.get("claim_token") or ""),
                        "claim_expires_at": expires_at,
                    },
                    {
                        "$set": {
                            "claim_token": str(claim_token),
                            "claim_expires_at": (
                                now + timedelta(seconds=30)
                            ),
                            "updated_at": now,
                        }
                    },
                    return_document=ReturnDocument.AFTER,
                )
                if reclaimed is not None:
                    return "claimed", document, reclaimed
                receipt = await self.remediation_receipts.find_one({
                    "_id": receipt_id,
                })
                if receipt is None:
                    raise StaleProseRun("正文修复回执占用后丢失")
                self._validate_remediation_receipt_scope(
                    receipt,
                    run_id=run_id,
                    owner_id=owner_id,
                    novel_id=novel_id,
                    source_revision=source_revision,
                )
                self._validate_remediation_receipt_digest(
                    receipt,
                    request_digest=request_digest,
                )
                state = str(receipt.get("state") or "")
                if state == "completed":
                    return (
                        "completed",
                        await self._owned_remediation_run(
                            run_id=run_id,
                            owner_id=owner_id,
                            novel_id=novel_id,
                        ),
                        receipt,
                    )
                if (
                    state == "reserved"
                    and str(receipt.get("claim_token") or "")
                    == str(claim_token)
                ):
                    return "claimed", document, receipt
        return f"in_progress_{state}", document, receipt

    async def mark_remediation_receipt_dispatched(
        self,
        *,
        run_id: str,
        owner_id: str,
        novel_id: str,
        idempotency_key: str,
        request_digest: str,
        claim_token: str,
    ) -> None:
        """Freeze that the claimed logical rewrite has reached Provider."""
        now = get_utc_now()
        receipt = await self.remediation_receipts.find_one_and_update(
            {
                "_id": self._remediation_receipt_id(
                    run_id=run_id,
                    owner_id=owner_id,
                    novel_id=novel_id,
                    idempotency_key=idempotency_key,
                ),
                "owner_id": to_object_id(owner_id),
                "novel_id": to_object_id(novel_id),
                "prose_run_id": to_object_id(run_id),
                "is_deleted": False,
                "idempotency_key": str(idempotency_key),
                "request_digest": str(request_digest),
                "state": "reserved",
                "claim_token": str(claim_token),
            },
            {
                "$set": {
                    "state": "dispatched",
                    "updated_at": now,
                },
                "$unset": {"claim_expires_at": ""},
            },
            return_document=ReturnDocument.AFTER,
        )
        if receipt is None:
            raise StaleProseRun("正文修复 Provider 派发权已经失效")

    async def complete_remediation_receipt(
        self,
        *,
        run_id: str,
        owner_id: str,
        novel_id: str,
        idempotency_key: str,
        request_digest: str,
        claim_token: str,
        result_projection: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Persist a known Tool result that did not mutate the candidate."""
        now = get_utc_now()
        raw_result_revision = result_projection.get("resource_revision")
        try:
            result_revision = int(raw_result_revision)
        except (TypeError, ValueError):
            result_revision = 0
        receipt_id = self._remediation_receipt_id(
            run_id=run_id,
            owner_id=owner_id,
            novel_id=novel_id,
            idempotency_key=idempotency_key,
        )
        receipt = await self.remediation_receipts.find_one_and_update(
            {
                "_id": receipt_id,
                "owner_id": to_object_id(owner_id),
                "novel_id": to_object_id(novel_id),
                "prose_run_id": to_object_id(run_id),
                "is_deleted": False,
                "idempotency_key": str(idempotency_key),
                "request_digest": str(request_digest),
                "state": {"$in": ["reserved", "dispatched"]},
                "claim_token": str(claim_token),
            },
            {
                "$set": {
                    "state": "completed",
                    "result_revision": result_revision,
                    "result_projection": dict(result_projection),
                    "completed_at": now,
                    "updated_at": now,
                },
                "$unset": {"claim_expires_at": ""},
            },
            return_document=ReturnDocument.AFTER,
        )
        if receipt is None:
            receipt = await self.remediation_receipts.find_one({
                "_id": receipt_id,
            })
        document = await self._owned_remediation_run(
            run_id=run_id,
            owner_id=owner_id,
            novel_id=novel_id,
        )
        if receipt is None:
            raise StaleProseRun("正文修复回执不存在")
        self._validate_remediation_receipt_scope(
            receipt,
            run_id=run_id,
            owner_id=owner_id,
            novel_id=novel_id,
        )
        self._validate_remediation_receipt_digest(
            receipt,
            request_digest=request_digest,
        )
        if str(receipt.get("state") or "") != "completed":
            raise StaleProseRun("正文修复回执尚未完成")
        if dict(receipt.get("result_projection") or {}) != dict(
            result_projection
        ):
            raise StaleProseRun("正文修复回执内容发生冲突")
        return document, receipt

    async def find_remediation_receipt(
        self,
        *,
        run_id: str,
        owner_id: str,
        novel_id: str,
        idempotency_key: str,
        request_digest: str,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Recover one proposal-only rewrite without reading Agent events."""
        try:
            document = await self._owned_remediation_run(
                run_id=run_id,
                owner_id=owner_id,
                novel_id=novel_id,
            )
        except NotFoundError:
            return None
        receipt = await self.remediation_receipts.find_one({
            "_id": self._remediation_receipt_id(
                run_id=run_id,
                owner_id=owner_id,
                novel_id=novel_id,
                idempotency_key=idempotency_key,
            )
        })
        if receipt is None:
            receipt = await self._migrate_legacy_remediation_receipt(
                document=document,
                run_id=run_id,
                owner_id=owner_id,
                novel_id=novel_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
            )
        if receipt is None:
            return None
        self._validate_remediation_receipt_scope(
            receipt,
            run_id=run_id,
            owner_id=owner_id,
            novel_id=novel_id,
        )
        self._validate_remediation_receipt_digest(
            receipt,
            request_digest=request_digest,
        )
        receipt = await self._repair_remediation_receipt_from_pointer(
            document=document,
            receipt=receipt,
            run_id=run_id,
            owner_id=owner_id,
            novel_id=novel_id,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
        )
        if str(receipt.get("state") or "completed") != "completed":
            return None
        return document, receipt

    async def acquire_remediation_write_fence(
        self,
        *,
        run_id: str,
        owner_id: str,
        novel_id: str,
        expected_revision: int,
        expected_narrative_revision: int,
        fence_token: str,
        expires_at: datetime,
    ) -> None:
        """Fence the ProseRun so an expired Novel fence can revoke old writers."""
        if not fence_token:
            raise ValueError("fence_token is required")
        now = get_utc_now()
        document = await self.collection.find_one_and_update(
            {
                "_id": to_object_id(run_id),
                "owner_id": to_object_id(owner_id),
                "novel_id": to_object_id(novel_id),
                "is_deleted": False,
                "status": {"$in": ["complete", "incomplete"]},
                "revision": int(expected_revision),
                "narrative_revision": int(expected_narrative_revision),
                "$or": [
                    {"remediation_write_fence": {"$exists": False}},
                    {"remediation_write_fence": None},
                    {"remediation_write_fence.expires_at": {"$lte": now}},
                    {"remediation_write_fence.token": str(fence_token)},
                ],
            },
            {
                "$set": {
                    "remediation_write_fence": {
                        "token": str(fence_token),
                        "expires_at": expires_at,
                    },
                    "updated_at": now,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if document is None:
            raise StaleProseRun("正文候选写入栅栏已经失效")

    async def release_remediation_write_fence(
        self,
        *,
        run_id: str,
        owner_id: str,
        novel_id: str,
        fence_token: str,
    ) -> None:
        if not fence_token:
            return
        await self.collection.update_one(
            {
                "_id": to_object_id(run_id),
                "owner_id": to_object_id(owner_id),
                "novel_id": to_object_id(novel_id),
                "remediation_write_fence.token": str(fence_token),
            },
            {"$unset": {"remediation_write_fence": ""}},
        )

    async def release_expired_remediation_write_fence(
        self,
        *,
        run_id: str,
        owner_id: str,
        novel_id: str,
        fence_token: str,
        expires_at: datetime,
    ) -> bool:
        """Revoke only the exact expired resource token observed by a writer."""
        if not fence_token or not isinstance(expires_at, datetime):
            return False
        result = await self.collection.update_one(
            {
                "_id": to_object_id(run_id),
                "owner_id": to_object_id(owner_id),
                "novel_id": to_object_id(novel_id),
                "remediation_write_fence.token": str(fence_token),
                "$and": [
                    {"remediation_write_fence.expires_at": expires_at},
                    {
                        "remediation_write_fence.expires_at": {
                            "$lte": get_utc_now()
                        }
                    },
                ],
            },
            {"$unset": {"remediation_write_fence": ""}},
        )
        return result.modified_count == 1

    async def apply_remediation_candidate(
        self,
        *,
        run_id: str,
        owner_id: str,
        novel_id: str,
        expected_revision: int,
        expected_text: str,
        expected_narrative_revision: int,
        expected_outline_revision: str,
        idempotency_key: str,
        request_digest: str,
        claim_token: str,
        assembled_text: str,
        source_content_digest: str,
        completion: dict[str, Any],
        target_issue_categories: list[str],
        target_scene_indexes: list[int],
        result_projection: dict[str, Any],
        write_fence_token: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """CAS one temporary candidate and publish one bounded receipt pointer."""
        receipt_id = self._remediation_receipt_id(
            run_id=run_id,
            owner_id=owner_id,
            novel_id=novel_id,
            idempotency_key=idempotency_key,
        )
        receipt = await self.remediation_receipts.find_one({
            "_id": receipt_id
        })
        if receipt is None:
            raise StaleProseRun("正文修复 receipt 不存在")
        self._validate_remediation_receipt_scope(
            receipt,
            run_id=run_id,
            owner_id=owner_id,
            novel_id=novel_id,
            source_revision=expected_revision,
        )
        self._validate_remediation_receipt_digest(
            receipt,
            request_digest=request_digest,
        )
        current_document = await self._owned_remediation_run(
            run_id=run_id,
            owner_id=owner_id,
            novel_id=novel_id,
        )
        previous_pointer = await self._drain_latest_remediation_receipt(
            document=current_document,
            run_id=run_id,
            owner_id=owner_id,
            novel_id=novel_id,
        )
        receipt = await self.remediation_receipts.find_one({"_id": receipt_id})
        if receipt is None:
            raise StaleProseRun("正文修复 receipt 不存在")
        if str(receipt.get("state") or "") == "completed":
            return current_document, receipt
        if (
            str(receipt.get("state") or "") != "dispatched"
            or str(receipt.get("claim_token") or "") != str(claim_token)
        ):
            raise StaleProseRun("正文修复 Provider 派发权已经失效")

        now = get_utc_now()
        next_revision = int(expected_revision) + 1
        query: dict[str, Any] = {
            "_id": to_object_id(run_id),
            "owner_id": to_object_id(owner_id),
            "novel_id": to_object_id(novel_id),
            "is_deleted": False,
            "status": {"$in": ["complete", "incomplete"]},
            "revision": int(expected_revision),
            "assembled_text": str(expected_text),
            "narrative_revision": int(expected_narrative_revision),
            "outline_revision": str(expected_outline_revision),
            "remediation_write_fence.token": str(write_fence_token),
            "remediation_write_fence.expires_at": {"$gt": now},
            "$or": [
                {"lease": None},
                {"lease": {"$exists": False}},
            ],
        }
        if previous_pointer is None:
            query["$and"] = [{
                "$or": [
                    {"remediation.latest_receipt": {"$exists": False}},
                    {"remediation.latest_receipt": None},
                ]
            }]
        else:
            query.update({
                "remediation.latest_receipt.schema_version": (
                    "prose_remediation_receipt_pointer.v1"
                ),
                "remediation.latest_receipt.idempotency_key": str(
                    previous_pointer["idempotency_key"]
                ),
                "remediation.latest_receipt.request_digest": str(
                    previous_pointer["request_digest"]
                ),
                "remediation.latest_receipt.source_revision": int(
                    previous_pointer["source_revision"]
                ),
                "remediation.latest_receipt.result_revision": int(
                    previous_pointer["result_revision"]
                ),
                "remediation.latest_receipt.result_projection": dict(
                    previous_pointer["result_projection"]
                ),
            })
        document = await self.collection.find_one_and_update(
            query,
            {
                "$set": {
                    "status": "complete",
                    "assembled_text": str(assembled_text),
                    "completion": dict(completion),
                    "updated_at": now,
                    "remediation": {
                        "schema_version": "prose_run_remediation.v1",
                        "latest_idempotency_key": str(idempotency_key),
                        "latest_revision": next_revision,
                        "latest_content_digest": str(
                            result_projection.get("resource_digest") or ""
                        ),
                        "source_content_digest": str(source_content_digest),
                        "source_narrative_revision": int(
                            expected_narrative_revision
                        ),
                        "source_outline_revision": str(
                            expected_outline_revision
                        ),
                        "target_issue_categories": list(
                            target_issue_categories
                        ),
                        "target_scene_indexes": list(target_scene_indexes),
                        "latest_receipt": {
                            "schema_version": (
                                "prose_remediation_receipt_pointer.v1"
                            ),
                            "idempotency_key": str(idempotency_key),
                            "request_digest": str(request_digest),
                            "claim_token": str(claim_token),
                            "source_revision": int(expected_revision),
                            "result_revision": next_revision,
                            "result_projection": dict(result_projection),
                            "completed_at": now,
                        },
                        "verification": None,
                        "updated_at": now,
                    },
                },
                "$inc": {"revision": 1},
            },
            return_document=ReturnDocument.AFTER,
        )
        if document is not None:
            return await self.complete_remediation_receipt(
                run_id=run_id,
                owner_id=owner_id,
                novel_id=novel_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                claim_token=claim_token,
                result_projection=result_projection,
            )

        recovered = await self._owned_remediation_run(
            run_id=run_id,
            owner_id=owner_id,
            novel_id=novel_id,
        )
        latest = self._latest_remediation_receipt(
            recovered,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
        )
        if latest is not None:
            return await self.complete_remediation_receipt(
                run_id=run_id,
                owner_id=owner_id,
                novel_id=novel_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                claim_token=claim_token,
                result_projection=result_projection,
            )
        raise StaleProseRun(
            "正文候选已被其他运行修改，当前修复结果不能覆盖"
        )

    async def verify_remediation_candidate(
        self,
        *,
        run_id: str,
        owner_id: str,
        novel_id: str,
        agent_run_id: str,
        expected_revision: int,
        expected_text: str,
        expected_content_digest: str,
        expected_narrative_revision: int,
        expected_outline_revision: str,
        completion: dict[str, Any],
        write_fence_token: str,
    ) -> dict[str, Any]:
        """Unlock one exact temporary candidate after its bound review passes."""
        now = get_utc_now()
        verification = {
            "schema_version": "prose_remediation_verification.v1",
            "agent_run_id": str(agent_run_id),
            "candidate_revision": int(expected_revision),
            "content_digest": str(expected_content_digest),
            "verified_at": now,
        }
        document = await self.collection.find_one_and_update(
            {
                "_id": to_object_id(run_id),
                "owner_id": to_object_id(owner_id),
                "novel_id": to_object_id(novel_id),
                "is_deleted": False,
                "status": "complete",
                "revision": int(expected_revision),
                "assembled_text": str(expected_text),
                "narrative_revision": int(expected_narrative_revision),
                "outline_revision": str(expected_outline_revision),
                "remediation_write_fence.token": str(write_fence_token),
                "remediation_write_fence.expires_at": {"$gt": now},
                "remediation.schema_version": "prose_run_remediation.v1",
                "remediation.latest_revision": int(expected_revision),
                "remediation.latest_content_digest": str(
                    expected_content_digest
                ),
                "remediation.verification": None,
                "completion.can_write_formal_prose": False,
            },
            {
                "$set": {
                    "completion": dict(completion),
                    "remediation.verification": verification,
                    "remediation.updated_at": now,
                    "updated_at": now,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if document is not None:
            return document
        current = await self.collection.find_one({
            "_id": to_object_id(run_id),
            "owner_id": to_object_id(owner_id),
            "novel_id": to_object_id(novel_id),
            "is_deleted": False,
        })
        if current is not None:
            existing = dict(
                (current.get("remediation") or {}).get("verification") or {}
            )
            if (
                int(current.get("revision") or 0) == int(expected_revision)
                and str(current.get("assembled_text") or "")
                == str(expected_text)
                and int(existing.get("candidate_revision") or 0)
                == int(expected_revision)
                and str(existing.get("content_digest") or "")
                == str(expected_content_digest)
                and existing.get("schema_version")
                == "prose_remediation_verification.v1"
                and str(existing.get("agent_run_id") or "")
                == str(agent_run_id)
                and int(current.get("narrative_revision") or -1)
                == int(expected_narrative_revision)
                and str(current.get("outline_revision") or "")
                == str(expected_outline_revision)
                and bool(
                    (current.get("completion") or {}).get(
                        "can_write_formal_prose"
                    )
                )
            ):
                return current
        raise StaleProseRun(
            "正文候选在复检通过后又发生变化，不能解锁正式接受"
        )

    async def finish(
        self,
        *,
        run_id: str,
        owner_id: str,
        lease_token: str,
        status: str,
        completion: dict[str, Any],
        assembled_text: str,
    ) -> dict[str, Any]:
        if status not in {"complete", "incomplete", "stale"}:
            raise ValueError(f"Unsupported prose run status: {status}")
        document = await self.collection.find_one_and_update(
            {
                "_id": to_object_id(run_id),
                "owner_id": to_object_id(owner_id),
                "is_deleted": False,
                "status": {"$in": list(CURRENT_PROSE_RUN_STATUSES)},
                "lease.token": str(lease_token),
            },
            {
                "$set": {
                    "status": status,
                    "completion": dict(completion),
                    "assembled_text": str(assembled_text),
                    "lease": None,
                    "updated_at": get_utc_now(),
                },
                "$inc": {"revision": 1},
            },
            return_document=ReturnDocument.AFTER,
        )
        if document is None:
            raise StaleProseRun("正文草稿执行租约已失效")
        return document

    async def mark_status(
        self,
        *,
        run_id: str,
        owner_id: str,
        status: str,
    ) -> bool:
        query: dict[str, Any] = {
            "_id": to_object_id(run_id),
            "owner_id": to_object_id(owner_id),
        }
        if status in CURRENT_PROSE_RUN_STATUSES:
            query["status"] = {"$in": list(CURRENT_PROSE_RUN_STATUSES)}
        try:
            return await self.update_one(
                query,
                {
                    "status": status,
                    "lease": None,
                },
            )
        except DuplicateKeyError as exc:
            raise StaleProseRun(
                "正文草稿已被其他页面继续或重新生成"
            ) from exc


prose_run_repo = ProseRunRepository()
