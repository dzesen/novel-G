"""Bounded chapter review archives, separate from authorization and state."""
from __future__ import annotations

from typing import Any

from bson import BSON

from backend.db import collections
from backend.db.base import BaseRepository
from backend.db.errors import NotFoundError
from backend.db.utils import get_utc_now, to_object_id


def required_id(value):
    if value is None or not str(value).strip():
        raise ValueError("review record identity is required")
    return to_object_id(value)


class JudgeReviewRecordRepository(BaseRepository):
    def __init__(self):
        super().__init__(collections.JUDGE_REVIEW_RECORDS)

    @staticmethod
    def scope(*, owner_id, chapter_id, novel_id=None, record_id=None):
        query = {"owner_id": required_id(owner_id), "chapter_id": required_id(chapter_id), "is_deleted": False}
        if novel_id is not None:
            query["novel_id"] = required_id(novel_id)
        if record_id is not None:
            query["_id"] = required_id(record_id)
        return query

    async def begin(self, document: dict[str, Any]) -> str:
        data = dict(document)
        for key in ("owner_id", "novel_id", "chapter_id"):
            data[key] = required_id(data.get(key))
        if data.get("job_id") is not None:
            data["job_id"] = required_id(data["job_id"])
        if len(BSON.encode(data)) > 32_768:
            raise ValueError("review record metadata exceeds its bound")
        return await self.insert_one({**data, "status": "running", "round_count": 0, "rounds": []})

    async def append_round(self, *, owner_id, chapter_id, record_id, ordinal: int, response: dict):
        if ordinal not in (1, 2) or len(BSON.encode(response)) > 96_000:
            raise ValueError("review response exceeds its archive bound")
        query = self.scope(owner_id=owner_id, chapter_id=chapter_id, record_id=record_id)
        query.update({"status": "running", "round_count": ordinal - 1})
        result = await self.collection.update_one(query, {
            "$push": {"rounds": {**response, "ordinal": ordinal}},
            "$inc": {"round_count": 1}, "$set": {"updated_at": get_utc_now()},
        })
        if result.modified_count != 1:
            raise ValueError("review response archive no longer writable")

    async def finish(self, *, owner_id, chapter_id, record_id, result: dict):
        # At most two 96 KB rounds plus a 256 KB conclusion, below Mongo's
        # document limit. The existing total backup size guard still applies.
        if len(BSON.encode(result)) > 262_144:
            raise ValueError("review conclusion exceeds its archive bound")
        query = self.scope(owner_id=owner_id, chapter_id=chapter_id, record_id=record_id)
        query["status"] = "running"
        if not await self.update_one(query, result):
            raise ValueError("review conclusion archive no longer writable")

    async def list_chapter(self, *, owner_id, chapter_id, before=None, limit=20):
        query = self.scope(owner_id=owner_id, chapter_id=chapter_id)
        if before is not None:
            query["_id"] = {"$lt": required_id(before)}
        projection = {"rounds": 0, "evidence": 0, "evidence_excerpts": 0, "diagnostics": 0}
        return await self.collection.find(query, projection).sort("_id", -1).limit(min(max(int(limit), 1), 50)).to_list(length=None)

    async def detail(self, *, owner_id, chapter_id, record_id):
        result = await self.find_one(self.scope(owner_id=owner_id, chapter_id=chapter_id, record_id=record_id))
        if result is None:
            raise NotFoundError("审查记录不存在")
        return result

    async def legacy_jobs(self, *, owner_id, chapter_id, job_id=None):
        chapter = required_id(chapter_id)
        query = {"owner_id": required_id(owner_id), "is_deleted": False, "$or": [
            {"current_chapter_id": {"$in": [str(chapter), chapter]}},
            {"required_adherence_journal.binding.chapter_id": str(chapter)},
            {"readiness.source_binding.chapter_id": str(chapter)},
        ]}
        if job_id is not None:
            query["_id"] = required_id(job_id)
        projection = {key: 1 for key in (
            "_id", "novel_id", "created_at", "scope", "current_chapter_id", "attempt_slots",
            "interactive_completion_evidence.outline_adherence", "interactive_completion_progress",
            "readiness.source_binding", "readiness.generation_plans.adherence.provider_alias",
            "readiness.generation_plans.adherence.provider_model", "required_adherence_journal.binding.chapter_id",
            "required_adherence_journal.entries",
        )}
        return await BaseRepository(collections.GENERATION_JOBS).collection.find(query, projection).sort("_id", -1).limit(20).to_list(length=None)


judge_review_record_repo = JudgeReviewRecordRepository()
