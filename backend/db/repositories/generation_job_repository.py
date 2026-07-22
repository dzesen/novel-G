"""generation_jobs 仓储：批量作业记录的 CRUD 与进度追加。"""
from __future__ import annotations

from typing import Any, Dict, List
from uuid import uuid4

from pymongo import ReturnDocument

from backend.db import collections
from backend.db.base import BaseRepository
from backend.db.errors import NotFoundError
from backend.db.utils import get_utc_now, to_object_id
from backend.llm.models import TokenUsage


USAGE_SUMMARY_LIMIT = 100


class AttemptCapacityExceeded(ValueError):
    """作业固定 attempt 容量或当前章节 reservation 已耗尽。"""


class GenerationJobRepository(BaseRepository):
    def __init__(self) -> None:
        super().__init__(collections.GENERATION_JOBS)

    async def create_job(self, data: Dict[str, Any]) -> str:
        return await self.insert_one(dict(data))

    async def get_job(self, job_id: str) -> Dict[str, Any]:
        doc = await self.find_one({"_id": to_object_id(job_id)})
        if doc is None:
            raise NotFoundError(f"Generation job not found: {job_id}")
        return doc

    async def list_jobs_by_novel(self, novel_id: str) -> List[Dict[str, Any]]:
        return await self.find_many(
            {"novel_id": to_object_id(novel_id)},
            sort=[("created_at", -1)],
        )

    async def list_running_jobs(self) -> List[Dict[str, Any]]:
        return await self.find_many({"status": "running"})

    async def update_job_fields(self, job_id: str, fields: Dict[str, Any]) -> bool:
        return await self.update_one({"_id": to_object_id(job_id)}, dict(fields))

    async def append_progress(self, job_id: str, entry: Dict[str, Any], tokens_delta: int) -> bool:
        # $push progress + $inc tokens_used 在一次原子 update 内完成。
        result = await self.collection.update_one(
            {"_id": to_object_id(job_id), "is_deleted": False},
            {
                "$push": {"progress": entry},
                "$inc": {"tokens_used": int(tokens_delta)},
                "$set": {"updated_at": get_utc_now()},
            },
        )
        return result.matched_count > 0

    async def reserve_attempts(self, job_id: str, chapter_id: str, slots: int) -> Dict[str, Any]:
        """为下一章保留固定数量的 Provider 调用槽，不扩大作业总容量。"""
        requested = int(slots)
        if requested < 0:
            raise ValueError("Attempt reservation cannot be negative")
        job = await self.get_job(job_id)
        capacity = int(job.get("usage_attempt_capacity") or 0)
        claimed = int(job.get("usage_attempt_claimed") or 0)
        reservation = job.get("attempt_reservation") or {}
        if (
            str(reservation.get("chapter_id") or "") == str(chapter_id)
            and int(reservation.get("reserved_slots") or 0) >= requested
        ):
            return reservation
        if claimed + requested > capacity:
            raise AttemptCapacityExceeded(
                f"Attempt capacity exhausted: need {requested}, "
                f"remaining {max(0, capacity - claimed)}"
            )
        prepared = {
            "chapter_id": str(chapter_id),
            "reserved_slots": requested,
            "claimed_slots": 0,
            "created_at": get_utc_now(),
        }
        result = await self.collection.find_one_and_update(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "$expr": {
                    "$lte": [
                        {"$add": [{"$ifNull": ["$usage_attempt_claimed", 0]}, requested]},
                        {"$ifNull": ["$usage_attempt_capacity", 0]},
                    ]
                },
            },
            {"$set": {"attempt_reservation": prepared, "updated_at": get_utc_now()}},
            return_document=ReturnDocument.AFTER,
        )
        if result is None:
            raise AttemptCapacityExceeded("Attempt capacity changed while reserving chapter")
        return dict(result["attempt_reservation"])

    async def claim_attempt(
        self,
        job_id: str,
        chapter_id: str,
        step_id: str,
        phase: str,
        provider_alias: str,
    ) -> str:
        """在发起 Provider 请求前原子占用一个已预留槽。"""
        attempt_id = uuid4().hex
        now = get_utc_now()
        slot = {
            "attempt_id": attempt_id,
            "chapter_id": str(chapter_id),
            "step_id": str(step_id),
            "phase": str(phase),
            "provider_alias": str(provider_alias),
            "state": "claimed",
            "claimed_at": now,
        }
        result = await self.collection.update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "attempt_reservation.chapter_id": str(chapter_id),
                "$expr": {"$and": [
                    {"$lt": [
                        {"$ifNull": ["$usage_attempt_claimed", 0]},
                        {"$ifNull": ["$usage_attempt_capacity", 0]},
                    ]},
                    {"$lt": [
                        {"$ifNull": ["$attempt_reservation.claimed_slots", 0]},
                        {"$ifNull": ["$attempt_reservation.reserved_slots", 0]},
                    ]},
                ]},
            },
            {
                "$inc": {
                    "usage_attempt_claimed": 1,
                    "attempt_reservation.claimed_slots": 1,
                },
                "$push": {"attempt_slots": slot},
                "$set": {"updated_at": now},
            },
        )
        if result.modified_count != 1:
            raise AttemptCapacityExceeded("Attempt capacity or chapter reservation is exhausted")
        return attempt_id

    async def account_attempt(
        self,
        job_id: str,
        attempt_id: str,
        usage: TokenUsage,
    ) -> bool:
        """逐 attempt 幂等计费；摘要裁剪不影响永久 ID 去重账本。"""
        now = get_utc_now()
        summary = {
            "attempt_id": str(attempt_id),
            "usage": usage.model_dump(),
            "accounted_at": now,
        }
        result = await self.collection.update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "usage_attempt_ids": {"$ne": str(attempt_id)},
                "attempt_slots": {"$elemMatch": {
                    "attempt_id": str(attempt_id),
                    "state": {"$in": ["claimed", "uncertain"]},
                }},
            },
            {
                "$addToSet": {"usage_attempt_ids": str(attempt_id)},
                "$push": {
                    "usage_attempt_summaries": {
                        "$each": [summary],
                        "$slice": -USAGE_SUMMARY_LIMIT,
                    }
                },
                "$inc": {"tokens_used": int(usage.total_tokens or 0)},
                "$set": {
                    "attempt_slots.$[slot].state": "accounted",
                    "attempt_slots.$[slot].usage": usage.model_dump(),
                    "attempt_slots.$[slot].accounted_at": now,
                    "updated_at": now,
                },
            },
            array_filters=[{"slot.attempt_id": str(attempt_id)}],
        )
        return result.modified_count == 1

    async def mark_attempt_uncertain(self, job_id: str, attempt_id: str, reason: str) -> bool:
        now = get_utc_now()
        result = await self.collection.update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "attempt_slots": {"$elemMatch": {
                    "attempt_id": str(attempt_id),
                    "state": "claimed",
                }},
            },
            {
                "$set": {
                    "attempt_slots.$[slot].state": "uncertain",
                    "attempt_slots.$[slot].uncertain_reason": str(reason),
                    "attempt_slots.$[slot].updated_at": now,
                    "has_uncertain_attempts": True,
                    "updated_at": now,
                },
                "$addToSet": {"uncertain_attempt_ids": str(attempt_id)},
            },
            array_filters=[{"slot.attempt_id": str(attempt_id)}],
        )
        return result.modified_count == 1

    async def finish_attempt_reservation(self, job_id: str, chapter_id: str) -> bool:
        result = await self.collection.update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "attempt_reservation.chapter_id": str(chapter_id),
            },
            {"$set": {"attempt_reservation": None, "updated_at": get_utc_now()}},
        )
        return result.matched_count == 1

    async def mark_claimed_attempts_uncertain(self, job_id: str, reason: str) -> int:
        job = await self.get_job(job_id)
        pending = [
            str(slot["attempt_id"])
            for slot in job.get("attempt_slots") or []
            if slot.get("state") == "claimed"
        ]
        changed = 0
        for attempt_id in pending:
            changed += int(await self.mark_attempt_uncertain(job_id, attempt_id, reason))
        return changed

    async def acknowledge_uncertain_attempts(self, job_id: str, action: str) -> bool:
        if action not in {"retry", "skip"}:
            raise ValueError("Unknown uncertain-attempt action")
        now = get_utc_now()
        result = await self.collection.update_one(
            {"_id": to_object_id(job_id), "is_deleted": False},
            {
                "$set": {
                    "attempt_slots.$[slot].state": f"uncertain_{action}_acknowledged",
                    "attempt_slots.$[slot].updated_at": now,
                    "has_uncertain_attempts": False,
                    "attempt_reservation": None,
                    "updated_at": now,
                }
            },
            array_filters=[{"slot.state": "uncertain"}],
        )
        return result.matched_count == 1


generation_job_repo = GenerationJobRepository()
