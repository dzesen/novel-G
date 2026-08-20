"""generation_jobs 仓储：批量作业记录的 CRUD 与进度追加。"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Dict, List
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo.results import BulkWriteResult

from backend.db import collections
from backend.db.base import BaseRepository
from backend.db.errors import NotFoundError
from backend.db.utils import get_utc_now, to_object_id
from backend.llm.models import TokenUsage
from backend.services.generation.candidate_repair_contracts import (
    MAX_CHAPTER_CANDIDATE_PIPELINE_CHECKPOINTS,
    CandidatePipelineCheckpointConflict,
    CandidatePipelineCompletionV1,
    CandidatePipelineCheckpointV1,
    PreDispatchFenceV1,
    StateCandidateCheckpointV1,
    parse_candidate_pipeline_checkpoint,
)


USAGE_SUMMARY_LIMIT = 100


MAX_ACTIVE_TOKEN_RESERVATIONS = 32
_ATOMIC_JOB_FIELDS = frozenset({
    "candidate_pipeline_checkpoints",
    "progress",
})


def _reject_atomic_field_updates(fields: Dict[str, Any]) -> None:
    if any(
        key == protected
        or (
            isinstance(key, str)
            and key.startswith(f"{protected}.")
        )
        for key in fields
        for protected in _ATOMIC_JOB_FIELDS
    ):
        raise ValueError(
            "Candidate pipeline checkpoints require an atomic repository command"
        )


def _validated_pre_dispatch_fence(
    fence: Any,
) -> PreDispatchFenceV1:
    if not isinstance(fence, PreDispatchFenceV1):
        raise ValueError("pre-dispatch fence contract is required")
    return PreDispatchFenceV1.model_validate(
        fence.model_dump(mode="python")
    )


class TokenBudgetExceeded(ValueError):
    """A Provider dispatch would exceed the explicitly authorized token budget."""


class TokenBudgetUnbounded(TokenBudgetExceeded):
    """A finite budget cannot authorize a call without a conservative bound."""


def _trusted_usage_tokens(usage: TokenUsage) -> int:
    reported_total = int(usage.total_tokens or 0)
    if reported_total > 0:
        return reported_total
    return max(0, int(usage.input_tokens or 0)) + max(
        0, int(usage.output_tokens or 0)
    )


class AttemptCapacityExceeded(ValueError):
    """作业固定 attempt 容量或当前章节 reservation 已耗尽。"""


class AttemptFenceExpired(AttemptCapacityExceeded):
    """A stale worker tried to claim against a replaced dispatch fence."""


class GenerationJobRepository(BaseRepository):
    def __init__(self) -> None:
        super().__init__(collections.GENERATION_JOBS)

    async def create_job(self, data: Dict[str, Any]) -> str:
        return await self.insert_one(dict(data))

    async def update_one(
        self,
        query: Dict[str, Any],
        update_data: Dict[str, Any],
        include_deleted: bool = False,
        session: AsyncClientSession | None = None,
    ) -> bool:
        _reject_atomic_field_updates(update_data)
        return await super().update_one(
            query,
            update_data,
            include_deleted=include_deleted,
            session=session,
        )

    async def update_many(
        self,
        query: Dict[str, Any],
        update_data: Dict[str, Any],
        include_deleted: bool = False,
        session: AsyncClientSession | None = None,
    ) -> int:
        _reject_atomic_field_updates(update_data)
        return await super().update_many(
            query,
            update_data,
            include_deleted=include_deleted,
            session=session,
        )

    async def increment_one(
        self,
        query: Dict[str, Any],
        increments: Dict[str, int],
        include_deleted: bool = False,
        session: AsyncClientSession | None = None,
    ) -> bool:
        _reject_atomic_field_updates(increments)
        return await super().increment_one(
            query,
            increments,
            include_deleted=include_deleted,
            session=session,
        )

    async def bulk_write(
        self,
        operations: Sequence[Any],
        ordered: bool = True,
        session: AsyncClientSession | None = None,
    ) -> BulkWriteResult | None:
        raise ValueError(
            "Generation job bulk writes require an atomic repository command"
        )

    async def bulk_update_one_set(
        self,
        updates: Iterable[tuple[Dict[str, Any], Dict[str, Any]]],
        include_deleted: bool = False,
        ordered: bool = True,
        session: AsyncClientSession | None = None,
    ) -> int:
        raise ValueError(
            "Generation job bulk writes require an atomic repository command"
        )

    async def get_job(self, job_id: str) -> Dict[str, Any]:
        doc = await self.find_one({"_id": to_object_id(job_id)})
        if doc is None:
            raise NotFoundError(f"Generation job not found: {job_id}")
        return doc

    async def list_jobs_by_novel(
        self,
        novel_id: str,
        *,
        limit: int = 0,
    ) -> List[Dict[str, Any]]:
        return await self.find_many(
            {"novel_id": to_object_id(novel_id)},
            sort=[("created_at", -1)],
            limit=max(0, int(limit)),
        )

    async def list_running_jobs(self) -> List[Dict[str, Any]]:
        return await self.find_many({"status": "running"})

    async def list_attempt_slots(
        self,
        job_id: str,
        *,
        chapter_id: str,
        step_prefix: str,
    ) -> List[Dict[str, Any]]:
        """Read one execution's persistent attempt ledger in claim order."""
        job = await self.get_job(job_id)
        return [
            dict(slot)
            for slot in list(job.get("attempt_slots") or [])
            if str(slot.get("chapter_id") or "") == str(chapter_id)
            and str(slot.get("step_id") or "").startswith(step_prefix)
        ]

    @staticmethod
    def _candidate_pipeline_checkpoints(
        job: Dict[str, Any],
        *,
        chapter_id: str,
    ) -> list[CandidatePipelineCheckpointV1]:
        raw = job.get("candidate_pipeline_checkpoints")
        if raw is None:
            raw = []
        if (
            not isinstance(raw, list)
            or len(raw) > MAX_CHAPTER_CANDIDATE_PIPELINE_CHECKPOINTS
        ):
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline checkpoint ledger is invalid"
            )
        parsed: list[CandidatePipelineCheckpointV1] = []
        checkpoint_ids: set[str] = set()
        for sequence, item in enumerate(raw, start=1):
            try:
                checkpoint = parse_candidate_pipeline_checkpoint(item)
            except Exception as exc:
                raise CandidatePipelineCheckpointConflict(
                    "Candidate pipeline checkpoint contract is invalid"
                ) from exc
            if (
                checkpoint.chapter_id != str(chapter_id)
                or checkpoint.sequence != sequence
                or checkpoint.checkpoint_id in checkpoint_ids
            ):
                raise CandidatePipelineCheckpointConflict(
                    "Candidate pipeline checkpoint order or scope changed"
                )
            checkpoint_ids.add(checkpoint.checkpoint_id)
            parsed.append(checkpoint)
        return parsed

    async def list_candidate_pipeline_checkpoints(
        self,
        job_id: str,
        *,
        chapter_id: str,
    ) -> List[Dict[str, Any]]:
        normalized_chapter_id = str(chapter_id or "")
        if not normalized_chapter_id:
            raise ValueError("Candidate pipeline chapter id is required")
        job = await self.get_job(job_id)
        if str(job.get("current_chapter_id") or "") != normalized_chapter_id:
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline chapter is no longer current"
            )
        return [
            checkpoint.model_dump(mode="json")
            for checkpoint in self._candidate_pipeline_checkpoints(
                job,
                chapter_id=normalized_chapter_id,
            )
        ]

    async def append_candidate_pipeline_checkpoint(
        self,
        job_id: str,
        checkpoint: CandidatePipelineCheckpointV1,
    ) -> bool:
        try:
            validated = parse_candidate_pipeline_checkpoint(checkpoint)
        except Exception as exc:
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline checkpoint contract is invalid"
            ) from exc
        value = validated.model_dump(mode="json")
        job = await self.get_job(job_id)
        if (
            str(job.get("status") or "") != "running"
            or str(job.get("current_chapter_id") or "")
            != validated.chapter_id
        ):
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline execution is no longer current"
            )
        existing = self._candidate_pipeline_checkpoints(
            job,
            chapter_id=validated.chapter_id,
        )
        matched = next(
            (
                item
                for item in existing
                if item.checkpoint_id == validated.checkpoint_id
            ),
            None,
        )
        if matched is not None:
            if matched == validated:
                return True
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline checkpoint replay diverged"
            )
        if validated.sequence != len(existing) + 1:
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline checkpoint sequence diverged"
            )
        result = await self.collection.update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "status": "running",
                "current_chapter_id": validated.chapter_id,
                "candidate_pipeline_checkpoints.checkpoint_id": {
                    "$ne": validated.checkpoint_id
                },
                "$expr": {
                    "$eq": [
                        {
                            "$size": {
                                "$ifNull": [
                                    "$candidate_pipeline_checkpoints",
                                    [],
                                ]
                            }
                        },
                        len(existing),
                    ]
                },
            },
            {
                "$push": {"candidate_pipeline_checkpoints": value},
                "$set": {"updated_at": get_utc_now()},
            },
        )
        if result.modified_count == 1:
            return True
        current = await self.get_job(job_id)
        if (
            str(current.get("current_chapter_id") or "")
            == validated.chapter_id
        ):
            for item in self._candidate_pipeline_checkpoints(
                current,
                chapter_id=validated.chapter_id,
            ):
                if item.checkpoint_id == validated.checkpoint_id:
                    if item == validated:
                        return True
                    break
        raise CandidatePipelineCheckpointConflict(
            "Candidate pipeline checkpoint append lost its execution fence"
        )

    @staticmethod
    def _completed_candidate_progress(
        job: Mapping[str, Any],
        *,
        expected_receipt: CandidatePipelineCompletionV1,
        entry: Mapping[str, Any],
    ) -> bool:
        raw_progress = job.get("progress")
        if raw_progress is None:
            raw_progress = []
        if not isinstance(raw_progress, list):
            raise CandidatePipelineCheckpointConflict(
                "Generation job progress ledger is invalid"
            )
        expected_entry = dict(entry)
        expected_entry.pop("completed_at", None)
        expected_entry.pop("candidate_pipeline_completion", None)
        matched: dict[str, Any] | None = None
        for raw_entry in raw_progress:
            if not isinstance(raw_entry, Mapping):
                continue
            raw_receipt = raw_entry.get("candidate_pipeline_completion")
            if raw_receipt is None:
                continue
            try:
                receipt = CandidatePipelineCompletionV1.model_validate(
                    raw_receipt
                )
            except Exception as exc:
                raise CandidatePipelineCheckpointConflict(
                    "Candidate pipeline completion receipt is invalid"
                ) from exc
            if receipt.checkpoint_id != expected_receipt.checkpoint_id:
                continue
            if receipt != expected_receipt:
                raise CandidatePipelineCheckpointConflict(
                    "Candidate pipeline completion receipt diverged"
                )
            candidate = dict(raw_entry)
            candidate.pop("completed_at", None)
            candidate.pop("candidate_pipeline_completion", None)
            if candidate != expected_entry or matched is not None:
                raise CandidatePipelineCheckpointConflict(
                    "Candidate pipeline completion replay diverged"
                )
            matched = candidate
        return matched is not None

    async def complete_candidate_pipeline_chapter(
        self,
        job_id: str,
        *,
        chapter_id: str,
        expected_checkpoint: StateCandidateCheckpointV1,
        entry: Mapping[str, Any],
        tokens_delta: int,
    ) -> bool:
        """Atomically publish progress and release exactly one checkpoint tail."""
        normalized_chapter_id = str(chapter_id or "")
        if (
            not isinstance(entry, Mapping)
            or str(entry.get("chapter_id") or "") != normalized_chapter_id
            or "candidate_pipeline_completion" in entry
        ):
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline progress identity is invalid"
            )
        if (
            type(tokens_delta) is not int
            or tokens_delta < 0
            or tokens_delta > 2**63 - 1
        ):
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline progress token delta is invalid"
            )
        try:
            validated_checkpoint = parse_candidate_pipeline_checkpoint(
                expected_checkpoint
            )
        except Exception as exc:
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline completion checkpoint is invalid"
            ) from exc
        if (
            not isinstance(validated_checkpoint, StateCandidateCheckpointV1)
            or validated_checkpoint.chapter_id != normalized_chapter_id
        ):
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline completion requires the scoped state checkpoint"
            )
        identity = {
            "prose_run_id": validated_checkpoint.source.source_run_id,
            "prose_run_revision": (
                validated_checkpoint.source.source_run_revision
            ),
            "prose_content_digest": (
                validated_checkpoint.source.source_content_digest
            ),
            "state_proposal_id": validated_checkpoint.proposal_id,
        }
        if any(entry.get(field) != value for field, value in identity.items()):
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline progress does not match its checkpoint"
            )
        receipt = CandidatePipelineCompletionV1(
            checkpoint_id=validated_checkpoint.checkpoint_id,
            sequence=validated_checkpoint.sequence,
            chapter_id=validated_checkpoint.chapter_id,
            source=validated_checkpoint.source,
            state_proposal_id=validated_checkpoint.proposal_id,
            tokens_delta=tokens_delta,
        )
        job = await self.get_job(job_id)
        if self._completed_candidate_progress(
            job,
            expected_receipt=receipt,
            entry=entry,
        ):
            return True
        if (
            str(job.get("status") or "") != "running"
            or str(job.get("current_chapter_id") or "")
            != normalized_chapter_id
        ):
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline execution is no longer current"
            )
        checkpoints = self._candidate_pipeline_checkpoints(
            dict(job),
            chapter_id=normalized_chapter_id,
        )
        if not checkpoints or not isinstance(
            checkpoints[-1],
            StateCandidateCheckpointV1,
        ):
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline has no final state checkpoint"
            )
        tail = checkpoints[-1]
        if tail != validated_checkpoint:
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline completion tail changed"
            )
        value = dict(entry)
        value["candidate_pipeline_completion"] = receipt.model_dump(
            mode="json"
        )
        value["completed_at"] = get_utc_now()
        result = await self.collection.update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "status": "running",
                "current_chapter_id": normalized_chapter_id,
                f"candidate_pipeline_checkpoints.{len(checkpoints) - 1}.checkpoint_id": (
                    tail.checkpoint_id
                ),
                "progress.candidate_pipeline_completion.checkpoint_id": {
                    "$ne": tail.checkpoint_id
                },
                "$expr": {
                    "$eq": [
                        {
                            "$size": {
                                "$ifNull": [
                                    "$candidate_pipeline_checkpoints",
                                    [],
                                ]
                            }
                        },
                        len(checkpoints),
                    ]
                },
            },
            {
                "$push": {"progress": value},
                "$inc": {"tokens_used": tokens_delta},
                "$set": {
                    "candidate_pipeline_checkpoints": [],
                    "current_chapter_id": None,
                    "updated_at": get_utc_now(),
                },
            },
        )
        if result.modified_count == 1:
            return True
        current = await self.get_job(job_id)
        if self._completed_candidate_progress(
            current,
            expected_receipt=receipt,
            entry=entry,
        ):
            return True
        raise CandidatePipelineCheckpointConflict(
            "Candidate pipeline completion lost its checkpoint fence"
        )

    async def update_job_fields(self, job_id: str, fields: Dict[str, Any]) -> bool:
        return await self.update_one({"_id": to_object_id(job_id)}, dict(fields))

    async def append_diagnostic(
        self,
        job_id: str,
        event: Dict[str, Any],
    ) -> bool:
        result = await self.collection.update_one(
            {"_id": to_object_id(job_id), "is_deleted": False},
            {
                "$push": {"diagnostics": {"$each": [dict(event)], "$slice": -200}},
                "$set": {"diagnostic_schema_version": 1, "updated_at": get_utc_now()},
            },
        )
        return result.matched_count > 0

    async def append_progress(self, job_id: str, entry: Dict[str, Any], tokens_delta: int) -> bool:
        # $push progress + $inc tokens_used 在一次原子 update 内完成。
        if "candidate_pipeline_completion" in entry:
            raise ValueError(
                "Candidate pipeline completion requires an atomic repository command"
            )
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
            (str(slot["attempt_id"]), slot.get("conservative_tokens"))
            for slot in job.get("attempt_slots") or []
            if slot.get("state") == "claimed"
        ]
        changed = 0
        for attempt_id, conservative_tokens in pending:
            if conservative_tokens is None:
                changed += int(await self.mark_attempt_uncertain(job_id, attempt_id, reason))
            else:
                changed += int(await self.mark_attempt_uncertain_with_budget(
                    job_id, attempt_id, reason
                ))
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

    async def bind_pre_dispatch_fence(
        self,
        job_id: str,
        chapter_id: str,
        fence: PreDispatchFenceV1,
    ) -> None:
        """Publish the receipt lease into the same document used for claims.

        A receipt takeover is cross-collection. Publishing its token here first
        makes every later Job attempt claim a local, atomic fencing check.
        """
        validated_fence = _validated_pre_dispatch_fence(fence)
        value = validated_fence.model_dump(mode="json")
        fence_path = "attempt_reservation.pre_dispatch_fence"
        result = await self.collection.update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "attempt_reservation.chapter_id": str(chapter_id),
                "$or": [
                    {fence_path: {"$exists": False}},
                    {fence_path: value},
                    {
                        f"{fence_path}.schema_version": value["schema_version"],
                        f"{fence_path}.receipt_id": value["receipt_id"],
                        f"{fence_path}.cycle": value["cycle"],
                        f"{fence_path}.claim_epoch": {
                            "$lt": value["claim_epoch"]
                        }
                    },
                    {
                        f"{fence_path}.schema_version": value["schema_version"],
                        f"{fence_path}.cycle": {"$lt": value["cycle"]},
                        "attempt_slots": {
                            "$not": {
                                "$elemMatch": {
                                    "state": {"$in": ["claimed", "uncertain"]}
                                }
                            }
                        },
                    },
                ],
            },
            {
                "$set": {
                    fence_path: value,
                    "updated_at": get_utc_now(),
                }
            },
        )
        if result.matched_count != 1:
            raise AttemptFenceExpired(
                "Chapter attempt reservation cannot publish the dispatch fence"
            )

    async def claim_attempt_with_budget(
        self,
        job_id: str,
        chapter_id: str,
        step_id: str,
        phase: str,
        provider_alias: str,
        conservative_tokens: int | None,
        *,
        pre_dispatch_fence: PreDispatchFenceV1 | None = None,
    ) -> str:
        """Atomically claim an attempt slot and reserve its worst-case tokens."""
        reserved = (
            None
            if conservative_tokens is None
            else max(1, int(conservative_tokens))
        )
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
            "conservative_tokens": reserved,
        }
        fence: dict[str, Any] | None = None
        if pre_dispatch_fence is not None:
            validated_fence = _validated_pre_dispatch_fence(
                pre_dispatch_fence
            )
            if validated_fence.step_id != str(step_id):
                raise ValueError("pre-dispatch fence step does not match the claim")
            fence = validated_fence.model_dump(mode="json")
            slot["pre_dispatch_fence"] = fence
        query: dict[str, Any] = {
            "_id": to_object_id(job_id),
            "is_deleted": False,
            "attempt_reservation.chapter_id": str(chapter_id),
        }
        if fence is not None:
            query["attempt_reservation.pre_dispatch_fence"] = fence
        if reserved is None:
            # A finite job budget must never silently accept an unbounded call.
            query["token_budget"] = None
        else:
            query["$expr"] = {
                "$and": [
                    {
                        "$lt": [
                            {"$ifNull": ["$usage_attempt_claimed", 0]},
                            {"$ifNull": ["$usage_attempt_capacity", 0]},
                        ]
                    },
                    {
                        "$lt": [
                            {"$ifNull": ["$attempt_reservation.claimed_slots", 0]},
                            {"$ifNull": ["$attempt_reservation.reserved_slots", 0]},
                        ]
                    },
                    {
                        "$lt": [
                            {"$size": {"$ifNull": ["$active_token_reservations", []]}},
                            MAX_ACTIVE_TOKEN_RESERVATIONS,
                        ]
                    },
                    {
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
                    },
                ]
            }
        update: dict[str, Any] = {
            "$inc": {
                "usage_attempt_claimed": 1,
                "attempt_reservation.claimed_slots": 1,
            },
            "$push": {"attempt_slots": slot},
            "$set": {"updated_at": now},
        }
        if reserved is not None:
            update["$inc"]["tokens_reserved"] = reserved
            update["$push"]["active_token_reservations"] = {
                "attempt_id": attempt_id,
                "chapter_id": str(chapter_id),
                "step_id": str(step_id),
                "phase": str(phase),
                "provider_alias": str(provider_alias),
                "conservative_tokens": reserved,
                "state": "claimed",
                "reserved_at": now,
            }
        result = await self.collection.update_one(query, update)
        if result.modified_count == 1:
            return attempt_id

        job = await self.get_job(job_id)
        if fence is not None:
            active_fence = (job.get("attempt_reservation") or {}).get(
                "pre_dispatch_fence"
            )
            if active_fence != fence:
                raise AttemptFenceExpired(
                    "Provider attempt dispatch fence was replaced"
                )
        budget = job.get("token_budget")
        if reserved is None and budget is not None:
            raise TokenBudgetUnbounded(
                "A finite token budget requires a conservative Provider bound"
            )
        if budget is not None and reserved is not None:
            used = int(job.get("tokens_used") or 0)
            already_reserved = int(job.get("tokens_reserved") or 0)
            if used + already_reserved + reserved > int(budget):
                raise TokenBudgetExceeded(
                    "Token budget would be exceeded before Provider dispatch"
                )
        if len(job.get("active_token_reservations") or []) >= (
            MAX_ACTIVE_TOKEN_RESERVATIONS
        ):
            raise AttemptCapacityExceeded("Active token reservation limit reached")
        raise AttemptCapacityExceeded(
            "Attempt capacity or chapter reservation is exhausted"
        )

    async def settle_attempt_budget(
        self,
        job_id: str,
        attempt_id: str,
        usage: TokenUsage,
        *,
        conservative_tokens: int | None,
    ) -> bool:
        """Release a reservation once; missing usage is charged conservatively."""
        if conservative_tokens is None:
            return await self.account_attempt(job_id, attempt_id, usage)
        reserved = max(1, int(conservative_tokens))
        observed = _trusted_usage_tokens(usage)
        charged = observed if observed > 0 else reserved
        accounted_usage = usage.model_copy(update={"total_tokens": charged})
        now = get_utc_now()
        summary = {
            "attempt_id": str(attempt_id),
            "usage": accounted_usage.model_dump(),
            "accounted_at": now,
            "charged_tokens": charged,
        }
        result = await self.collection.update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "usage_attempt_ids": {"$ne": str(attempt_id)},
                "attempt_slots": {
                    "$elemMatch": {
                        "attempt_id": str(attempt_id),
                        "state": {"$in": ["claimed", "uncertain"]},
                    }
                },
                "active_token_reservations": {
                    "$elemMatch": {
                        "attempt_id": str(attempt_id),
                        "conservative_tokens": reserved,
                    }
                },
            },
            {
                "$addToSet": {"usage_attempt_ids": str(attempt_id)},
                "$push": {
                    "usage_attempt_summaries": {
                        "$each": [summary],
                        "$slice": -USAGE_SUMMARY_LIMIT,
                    }
                },
                "$pull": {"active_token_reservations": {"attempt_id": str(attempt_id)}},
                "$inc": {
                    "tokens_used": charged,
                    "tokens_reserved": -reserved,
                },
                "$set": {
                    "attempt_slots.$[slot].state": "accounted",
                    "attempt_slots.$[slot].usage": accounted_usage.model_dump(),
                    "attempt_slots.$[slot].charged_tokens": charged,
                    "attempt_slots.$[slot].accounted_at": now,
                    "updated_at": now,
                },
            },
            array_filters=[{"slot.attempt_id": str(attempt_id)}],
        )
        return result.modified_count == 1

    async def mark_attempt_uncertain_with_budget(
        self,
        job_id: str,
        attempt_id: str,
        reason: str,
    ) -> bool:
        """Freeze the reservation when a dispatched Provider result is unknown."""
        now = get_utc_now()
        result = await self.collection.update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "attempt_slots": {
                    "$elemMatch": {
                        "attempt_id": str(attempt_id),
                        "state": "claimed",
                    }
                },
                "active_token_reservations": {
                    "$elemMatch": {"attempt_id": str(attempt_id)}
                },
            },
            {
                "$set": {
                    "attempt_slots.$[slot].state": "uncertain",
                    "attempt_slots.$[slot].uncertain_reason": str(reason),
                    "attempt_slots.$[slot].updated_at": now,
                    "active_token_reservations.$[reservation].state": "uncertain",
                    "active_token_reservations.$[reservation].uncertain_reason": str(reason),
                    "active_token_reservations.$[reservation].updated_at": now,
                    "has_uncertain_attempts": True,
                    "updated_at": now,
                },
                "$addToSet": {"uncertain_attempt_ids": str(attempt_id)},
            },
            array_filters=[
                {"slot.attempt_id": str(attempt_id)},
                {"reservation.attempt_id": str(attempt_id)},
            ],
        )
        return result.modified_count == 1

    async def release_attempt_budget(
        self,
        job_id: str,
        attempt_id: str,
        *,
        conservative_tokens: int | None,
        reason: str,
    ) -> bool:
        """Release only a proven pre-dispatch failure; uncertain calls stay frozen."""
        if conservative_tokens is None:
            return False
        reserved = max(1, int(conservative_tokens))
        now = get_utc_now()
        result = await self.collection.update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "attempt_slots": {
                    "$elemMatch": {
                        "attempt_id": str(attempt_id),
                        "state": "claimed",
                    }
                },
                "active_token_reservations": {
                    "$elemMatch": {
                        "attempt_id": str(attempt_id),
                        "conservative_tokens": reserved,
                    }
                },
            },
            {
                "$inc": {"tokens_reserved": -reserved},
                "$pull": {"active_token_reservations": {"attempt_id": str(attempt_id)}},
                "$set": {
                    "attempt_slots.$[slot].state": "released_pre_dispatch",
                    "attempt_slots.$[slot].release_reason": str(reason),
                    "attempt_slots.$[slot].updated_at": now,
                    "updated_at": now,
                },
            },
            array_filters=[{"slot.attempt_id": str(attempt_id)}],
        )
        return result.modified_count == 1

    async def discard_proven_pre_dispatch_attempt(
        self,
        job_id: str,
        chapter_id: str,
        step_id: str,
        attempt_id: str,
        *,
        current_pre_dispatch_fence: PreDispatchFenceV1 | None = None,
    ) -> bool:
        """Remove a cross-ledger orphan only after receipt fencing proves no dispatch."""
        current_fence: dict[str, Any] | None = None
        if current_pre_dispatch_fence is not None:
            validated_fence = _validated_pre_dispatch_fence(
                current_pre_dispatch_fence
            )
            if validated_fence.step_id != str(step_id):
                raise ValueError("pre-dispatch fence step does not match cleanup")
            current_fence = validated_fence.model_dump(mode="json")
        job = await self.get_job(job_id)
        slot = next(
            (
                item
                for item in list(job.get("attempt_slots") or [])
                if str(item.get("attempt_id") or "") == str(attempt_id)
                and str(item.get("chapter_id") or "") == str(chapter_id)
                and str(item.get("step_id") or "") == str(step_id)
                and item.get("state") in {
                    "claimed",
                    "released_pre_dispatch",
                }
            ),
            None,
        )
        if slot is None:
            return False
        if current_fence is not None:
            active_fence = (job.get("attempt_reservation") or {}).get(
                "pre_dispatch_fence"
            )
            if active_fence != current_fence:
                raise AttemptFenceExpired(
                    "Pre-dispatch cleanup fence was replaced"
                )
            slot_fence = slot.get("pre_dispatch_fence")
            if (
                slot.get("state") == "claimed"
                and slot_fence == current_fence
            ):
                return False
        state = str(slot["state"])
        bound = slot.get("conservative_tokens")
        reserved = (
            int(bound)
            if state == "claimed" and type(bound) is int and bound > 0
            else 0
        )
        query: dict[str, Any] = {
            "_id": to_object_id(job_id),
            "is_deleted": False,
            "usage_attempt_claimed": {"$gte": 1},
            "attempt_slots": {"$elemMatch": {
                "attempt_id": str(attempt_id),
                "chapter_id": str(chapter_id),
                "step_id": str(step_id),
                "state": state,
            }},
        }
        if current_fence is not None:
            query["attempt_reservation.pre_dispatch_fence"] = current_fence
        update: dict[str, Any] = {
            "$pull": {"attempt_slots": {"attempt_id": str(attempt_id)}},
            "$inc": {"usage_attempt_claimed": -1},
            "$set": {"updated_at": get_utc_now()},
        }
        reservation = job.get("attempt_reservation")
        if (
            isinstance(reservation, dict)
            and str(reservation.get("chapter_id") or "") == str(chapter_id)
            and int(reservation.get("claimed_slots") or 0) > 0
        ):
            query["attempt_reservation.chapter_id"] = str(chapter_id)
            query["attempt_reservation.claimed_slots"] = {"$gte": 1}
            update["$inc"]["attempt_reservation.claimed_slots"] = -1
        if reserved:
            query["tokens_reserved"] = {"$gte": reserved}
            query["active_token_reservations"] = {"$elemMatch": {
                "attempt_id": str(attempt_id),
                "conservative_tokens": reserved,
            }}
            update["$inc"]["tokens_reserved"] = -reserved
            update["$pull"]["active_token_reservations"] = {
                "attempt_id": str(attempt_id)
            }
        result = await self.collection.update_one(query, update)
        return result.modified_count == 1
generation_job_repo = GenerationJobRepository()
