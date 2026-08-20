"""generation_jobs 仓储：批量作业记录的 CRUD 与进度追加。"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any, Dict, List
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo.results import BulkWriteResult

from backend.db import collections
from backend.db.base import BaseRepository
from backend.db.errors import NotFoundError
from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.utils import get_utc_now, to_object_id
from backend.llm.models import TokenUsage
from backend.services.generation.candidate_repair_contracts import (
    MAX_CANDIDATE_PIPELINE_PROGRESS_ENTRIES,
    MAX_CANDIDATE_OUTLINE_SCENES,
    MAX_CHAPTER_CANDIDATE_PIPELINE_CHECKPOINTS,
    CandidatePipelineCheckpointConflict,
    CandidatePipelineCompletionEvidenceV1,
    CandidatePipelineCompletionV1,
    CandidatePipelineCheckpointV1,
    CandidatePipelineProgressV1,
    JobMutationRecoveryBindingV1,
    JobMutationReceiptV1,
    PreDispatchFenceV1,
    STATE_DISPATCH_RESOLUTION_ACTIONS,
    STATE_DISPATCH_RESOLUTION_PHASES,
    StateDispatchResolutionV3,
    candidate_pipeline_checkpoint_digest,
    candidate_pipeline_checkpoint_ledger_digest,
    parse_candidate_pipeline_checkpoint,
    parse_candidate_pipeline_progress,
    validate_candidate_pipeline_completion_chain,
)
from backend.services.generation.job_execution import (
    JOB_EXECUTION_LEASE_SECONDS,
    JobExecutionLeaseLost,
    JobExecutionLeaseUnavailable,
    JobExecutionLeaseV1,
    current_job_execution,
)


USAGE_SUMMARY_LIMIT = 100


MAX_ACTIVE_TOKEN_RESERVATIONS = 32
_ATOMIC_JOB_FIELDS = frozenset({
    "candidate_pipeline_checkpoints",
    "expected_narrative_revision",
    "execution_epoch",
    "execution_lease",
    "job_mutation_recovery",
    "progress",
    "state_dispatch_resolution",
})
_MAX_NARRATIVE_REVISION = 2**63 - 1


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


def _validate_initial_candidate_ledgers(document: Mapping[str, Any]) -> None:
    checkpoints = document.get("candidate_pipeline_checkpoints", [])
    if not isinstance(checkpoints, list) or checkpoints:
        raise ValueError("Candidate checkpoint ledger must start empty")
    progress = document.get("progress", [])
    if (
        not isinstance(progress, list)
        or len(progress) > MAX_CANDIDATE_PIPELINE_PROGRESS_ENTRIES
    ):
        raise ValueError("Generation job progress ledger is invalid")
    if any(
        isinstance(item, Mapping)
        and (
            "candidate_pipeline_completion" in item
            or "job_mutation_receipt" in item
        )
        for item in progress
    ):
        raise ValueError(
            "Candidate completion receipts require an atomic repository command"
        )
    if document.get("job_mutation_recovery") is not None:
        raise ValueError("Job mutation recovery binding must start empty")
    if document.get("state_dispatch_resolution") is not None:
        raise ValueError("State dispatch resolution must start empty")
    if document.get("execution_lease") is not None:
        raise ValueError("Generation job execution lease must start empty")
    execution_epoch = document.get("execution_epoch", 0)
    if type(execution_epoch) is not int or execution_epoch != 0:
        raise ValueError("Generation job execution epoch must start at zero")
    revision = document.get("expected_narrative_revision")
    if revision is not None and (
        type(revision) is not int
        or revision < 0
        or revision > _MAX_NARRATIVE_REVISION
    ):
        raise ValueError("Generation job narrative revision cursor is invalid")


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


def _live_attempt_transition_fields(
    *,
    source_states: Sequence[str],
    target_state: str,
    now: datetime,
    reason: str | None = None,
) -> dict[str, Any]:
    """Build one atomic, metadata-only transition for both attempt ledgers."""

    source = list(source_states)
    replacement: dict[str, Any] = {
        "state": target_state,
        "updated_at": now,
    }
    if reason is not None:
        replacement["uncertain_reason"] = reason

    def mapped(field: str, alias: str) -> dict[str, Any]:
        return {
            "$map": {
                "input": {"$ifNull": [f"${field}", []]},
                "as": alias,
                "in": {
                    "$cond": [
                        {"$in": [f"$${alias}.state", source]},
                        {
                            "$mergeObjects": [
                                f"$${alias}",
                                replacement,
                            ]
                        },
                        f"$${alias}",
                    ]
                },
            }
        }

    return {
        "attempt_slots": mapped("attempt_slots", "slot"),
        "active_token_reservations": mapped(
            "active_token_reservations",
            "reservation",
        ),
        "uncertain_attempt_ids": {
            "$setUnion": [
                {"$ifNull": ["$uncertain_attempt_ids", []]},
                {
                    "$map": {
                        "input": {
                            "$filter": {
                                "input": {
                                    "$ifNull": ["$attempt_slots", []]
                                },
                                "as": "slot",
                                "cond": {
                                    "$in": ["$$slot.state", source]
                                },
                            }
                        },
                        "as": "slot",
                        "in": "$$slot.attempt_id",
                    }
                },
            ]
        },
    }


class AttemptCapacityExceeded(ValueError):
    """作业固定 attempt 容量或当前章节 reservation 已耗尽。"""


class AttemptFenceExpired(AttemptCapacityExceeded):
    """A stale worker tried to claim against a replaced dispatch fence."""


class GenerationJobRepository:
    """Generation Job persistence behind one lease-aware surface.

    This repository deliberately uses ``BaseRepository`` by composition.  A
    leased worker must not inherit a new generic CRUD method that bypasses the
    execution authority checks below.
    """

    def __init__(self) -> None:
        self._base = BaseRepository(collections.GENERATION_JOBS)

    @property
    def collection(self) -> Any:
        return self._base.collection

    @staticmethod
    def _execution_authority_filter(
        lease: JobExecutionLeaseV1,
        *,
        now: datetime,
        require_live: bool = True,
    ) -> dict[str, Any]:
        """Match one strict V1 lease without freezing heartbeat timestamps."""

        expires_at: dict[str, Any] = {"$type": "date"}
        if require_live:
            expires_at["$gt"] = now
        return {
            "$and": [
                {
                    "_id": to_object_id(lease.job_id),
                    "execution_epoch": lease.epoch,
                    "execution_lease.schema_version": (
                        "job_execution_lease.v1"
                    ),
                    "execution_lease.job_id": lease.job_id,
                    "execution_lease.worker_id": lease.worker_id,
                    "execution_lease.epoch": lease.epoch,
                    "execution_lease.heartbeat_at": {"$type": "date"},
                    "execution_lease.expires_at": expires_at,
                },
                {
                    "$expr": {
                        "$and": [
                            {
                                "$in": [
                                    {"$type": "$execution_epoch"},
                                    ["int", "long"],
                                ]
                            },
                            {
                                "$in": [
                                    {"$type": "$execution_lease.epoch"},
                                    ["int", "long"],
                                ]
                            },
                            {
                                "$eq": [
                                    {
                                        "$size": {
                                            "$objectToArray": {
                                                "$ifNull": [
                                                    "$execution_lease",
                                                    {},
                                                ]
                                            }
                                        }
                                    },
                                    6,
                                ]
                            },
                            {
                                "$gt": [
                                    "$execution_lease.expires_at",
                                    "$execution_lease.heartbeat_at",
                                ]
                            },
                        ]
                    }
                },
            ]
        }

    @staticmethod
    def _execution_filter(query: Mapping[str, Any]) -> dict[str, Any]:
        """Inject the current worker lease into every owned Job operation."""

        base = dict(query)
        lease = current_job_execution()
        if lease is None:
            return base
        expected_id = to_object_id(lease.job_id)
        supplied_id = base.get("_id")
        if supplied_id is not None and supplied_id != expected_id:
            raise JobExecutionLeaseLost(
                "Generation Job worker attempted to access another Job"
            )
        return {
            "$and": [
                base,
                GenerationJobRepository._execution_authority_filter(
                    lease,
                    now=get_utc_now(),
                ),
            ]
        }

    async def _collection_update_one(
        self,
        query: Mapping[str, Any],
        update: Mapping[str, Any] | Sequence[Mapping[str, Any]],
        **kwargs: Any,
    ) -> Any:
        update_document: Any = (
            [dict(stage) for stage in update]
            if isinstance(update, Sequence) and not isinstance(update, Mapping)
            else dict(update)
        )
        result = await self.collection.update_one(
            self._execution_filter(query),
            update_document,
            **kwargs,
        )
        if result.matched_count == 0:
            await self._assert_execution_current()
        return result

    async def _collection_find_one_and_update(
        self,
        query: Mapping[str, Any],
        update: Mapping[str, Any] | Sequence[Mapping[str, Any]],
        **kwargs: Any,
    ) -> Any:
        update_document: Any = (
            [dict(stage) for stage in update]
            if isinstance(update, Sequence) and not isinstance(update, Mapping)
            else dict(update)
        )
        document = await self.collection.find_one_and_update(
            self._execution_filter(query),
            update_document,
            **kwargs,
        )
        if document is None:
            await self._assert_execution_current()
        return document

    async def _assert_execution_current(self) -> None:
        """Raise only when the task-local lease itself is no longer valid."""

        lease = current_job_execution()
        if lease is None:
            return
        current = await self.collection.find_one({
            "$and": [
                {"is_deleted": False},
                self._execution_authority_filter(
                    lease,
                    now=get_utc_now(),
                ),
            ]
        })
        if current is None:
            raise JobExecutionLeaseLost(
                "Generation Job execution lease is no longer current"
            )

    async def acquire_execution_lease(
        self,
        job_id: str,
        worker_id: str,
        *,
        now: datetime,
        expires_at: datetime,
    ) -> JobExecutionLeaseV1:
        """Atomically consume an optional retry receipt and own Job execution."""

        if expires_at <= now:
            raise ValueError("Generation job execution lease expiry is invalid")
        current = await self.get_job(job_id)
        previous_epoch = current.get("execution_epoch", 0)
        if (
            type(previous_epoch) is not int
            or previous_epoch < 0
            or previous_epoch >= _MAX_NARRATIVE_REVISION
        ):
            raise JobExecutionLeaseUnavailable(
                "Generation Job execution epoch is invalid"
            )
        raw_lease = current.get("execution_lease")
        existing: JobExecutionLeaseV1 | None = None
        if raw_lease is not None:
            try:
                existing = JobExecutionLeaseV1.model_validate(raw_lease)
            except (TypeError, ValueError) as exc:
                raise JobExecutionLeaseUnavailable(
                    "Persisted Generation Job execution lease is invalid"
                ) from exc
            if existing.job_id != str(job_id):
                raise JobExecutionLeaseUnavailable(
                    "Persisted Generation Job execution lease changed scope"
                )
            if existing.epoch != previous_epoch:
                raise JobExecutionLeaseUnavailable(
                    "Persisted Generation Job execution lease epoch diverged"
                )
            if existing.expires_at > now:
                if existing.worker_id == str(worker_id):
                    return existing
                raise JobExecutionLeaseUnavailable(
                    "Generation Job already has a live execution worker"
                )
        next_epoch = previous_epoch + 1
        try:
            lease = JobExecutionLeaseV1(
                schema_version="job_execution_lease.v1",
                job_id=str(job_id),
                worker_id=str(worker_id),
                epoch=next_epoch,
                heartbeat_at=now,
                expires_at=expires_at,
            )
        except (TypeError, ValueError) as exc:
            raise JobExecutionLeaseUnavailable(
                "Generation Job execution lease command is invalid"
            ) from exc

        raw_resolution = current.get("state_dispatch_resolution")
        resolution_query: dict[str, Any]
        if raw_resolution is None:
            resolution_query = {"state_dispatch_resolution": None}
        else:
            try:
                resolution = StateDispatchResolutionV3.model_validate(
                    raw_resolution
                )
            except (TypeError, ValueError) as exc:
                raise JobExecutionLeaseUnavailable(
                    "Generation Job retry launch receipt is invalid"
                ) from exc
            if resolution.action != "retry" or resolution.phase != "job_transitioned":
                raise JobExecutionLeaseUnavailable(
                    "Generation Job action is not ready to launch"
                )
            resolution_query = {
                "state_dispatch_resolution": resolution.model_dump(mode="json")
            }

        epoch_query: dict[str, Any] = {"execution_epoch": previous_epoch}
        if previous_epoch == 0:
            epoch_query = {
                "$or": [
                    {"execution_epoch": 0},
                    {"execution_epoch": {"$exists": False}},
                ]
            }
        query: dict[str, Any] = {
            "$and": [
                {
                    "_id": to_object_id(job_id),
                    "is_deleted": False,
                    "status": {"$in": ["running", "interrupted"]},
                    **resolution_query,
                },
                epoch_query,
                (
                    {"execution_lease": existing.model_dump(mode="python")}
                    if existing is not None
                    else {
                        "$or": [
                            {"execution_lease": {"$exists": False}},
                            {"execution_lease": None},
                        ]
                    }
                ),
            ]
        }
        update: dict[str, Any] = {
            "$set": {
                "status": "running",
                "pause_reason": None,
                "error": None,
                "active_slot": "global",
                "execution_epoch": next_epoch,
                "execution_lease": lease.model_dump(mode="python"),
                "updated_at": now,
            }
        }
        if raw_resolution is not None:
            update["$unset"] = {"state_dispatch_resolution": ""}
        document = await self.collection.find_one_and_update(
            query,
            update,
            return_document=ReturnDocument.AFTER,
        )
        if document is not None:
            return lease
        latest = await self.get_job(job_id)
        try:
            existing = JobExecutionLeaseV1.model_validate(
                latest.get("execution_lease")
            )
        except (TypeError, ValueError) as exc:
            raise JobExecutionLeaseUnavailable(
                "Generation Job execution lease raced"
            ) from exc
        latest_epoch = latest.get("execution_epoch")
        if (
            existing.worker_id == worker_id
            and existing.job_id == str(job_id)
            and type(latest_epoch) is int
            and existing.epoch == latest_epoch
            and existing.expires_at > now
        ):
            return existing
        raise JobExecutionLeaseUnavailable(
            "Generation Job execution lease is owned by another worker"
        )

    async def heartbeat_execution_lease(
        self,
        lease: JobExecutionLeaseV1,
        *,
        now: datetime,
        expires_at: datetime,
    ) -> JobExecutionLeaseV1 | None:
        """Extend one live lease; return ``None`` after ownership is lost."""

        frozen = JobExecutionLeaseV1.model_validate(
            lease.model_dump(mode="python")
        )
        if expires_at <= now:
            raise ValueError("Generation job execution heartbeat is invalid")
        renewed = frozen.model_copy(
            update={"heartbeat_at": now, "expires_at": expires_at}
        )
        result = await self.collection.update_one(
            {
                "$and": [
                    {"is_deleted": False, "status": "running"},
                    self._execution_authority_filter(
                        frozen,
                        now=now,
                    ),
                ]
            },
            {
                "$set": {
                    "execution_lease": renewed.model_dump(mode="python"),
                    "updated_at": now,
                }
            },
        )
        return renewed if result.modified_count == 1 else None

    async def release_execution_lease(
        self,
        lease: JobExecutionLeaseV1,
    ) -> bool:
        """Release only the worker/epoch owned by the completed local task."""

        frozen = JobExecutionLeaseV1.model_validate(
            lease.model_dump(mode="python")
        )
        result = await self.collection.update_one(
            {
                "$and": [
                    {"is_deleted": False},
                    self._execution_authority_filter(
                        frozen,
                        now=get_utc_now(),
                        require_live=False,
                    ),
                ]
            },
            {
                "$unset": {"execution_lease": ""},
                "$set": {"updated_at": get_utc_now()},
            },
        )
        if result.modified_count == 1:
            return True
        latest = await self.collection.find_one({
            "_id": to_object_id(frozen.job_id),
            "is_deleted": False,
        })
        return bool(latest is not None and latest.get("execution_lease") is None)

    async def interrupt_stale_execution(
        self,
        job_id: str,
        *,
        now: datetime,
    ) -> bool:
        """Fence only a missing/expired worker before startup recovery writes."""

        current = await self.get_job(job_id)
        if str(current.get("status") or "") != "running":
            return False
        previous_epoch = current.get("execution_epoch", 0)
        if (
            type(previous_epoch) is not int
            or previous_epoch < 0
            or previous_epoch >= _MAX_NARRATIVE_REVISION
        ):
            return False
        raw_lease = current.get("execution_lease")
        if raw_lease is None:
            lease_query: dict[str, Any] = {
                "$or": [
                    {"execution_lease": {"$exists": False}},
                    {"execution_lease": None},
                ]
            }
        else:
            try:
                lease = JobExecutionLeaseV1.model_validate(raw_lease)
            except (TypeError, ValueError):
                return False
            if (
                lease.job_id != str(job_id)
                or lease.epoch != previous_epoch
                or lease.expires_at > now
            ):
                return False
            lease_query = {
                "execution_lease": lease.model_dump(mode="python")
            }
        epoch_query: dict[str, Any] = {"execution_epoch": previous_epoch}
        if previous_epoch == 0:
            epoch_query = {
                "$or": [
                    {"execution_epoch": 0},
                    {"execution_epoch": {"$exists": False}},
                ]
            }
        reason = "backend process interrupted before usage was recorded"
        pending_or_uncertain = {
            "$or": [
                {"$eq": ["$has_uncertain_attempts", True]},
                {
                    "$gt": [
                        {
                            "$size": {
                                "$filter": {
                                    "input": {
                                        "$ifNull": ["$attempt_slots", []]
                                    },
                                    "as": "slot",
                                    "cond": {
                                        "$in": [
                                            "$$slot.state",
                                            ["claimed", "uncertain"],
                                        ]
                                    },
                                }
                            }
                        },
                        0,
                    ]
                },
            ]
        }
        result = await self.collection.update_one(
            {
                "$and": [
                    {
                        "_id": to_object_id(job_id),
                        "is_deleted": False,
                        "status": "running",
                    },
                    epoch_query,
                    lease_query,
                ]
            },
            [
                {
                    "$set": {
                        **_live_attempt_transition_fields(
                            source_states=("claimed",),
                            target_state="uncertain",
                            now=now,
                            reason=reason,
                        ),
                    }
                },
                {
                    "$set": {
                        "execution_epoch": previous_epoch + 1,
                        "execution_lease": "$$REMOVE",
                        "status": "interrupted",
                        "pause_reason": {
                            "$cond": [
                                pending_or_uncertain,
                                "uncertain_attempt",
                                "process_restart",
                            ]
                        },
                        "current_chapter_id": {
                            "$cond": [
                                {
                                    "$or": [
                                        {
                                            "$gt": [
                                                {
                                                    "$size": {
                                                        "$ifNull": [
                                                            "$candidate_pipeline_checkpoints",
                                                            [],
                                                        ]
                                                    }
                                                },
                                                0,
                                            ]
                                        },
                                        {
                                            "$eq": [
                                                {
                                                    "$type": (
                                                        "$job_mutation_recovery"
                                                    )
                                                },
                                                "object",
                                            ]
                                        },
                                    ]
                                },
                                "$current_chapter_id",
                                None,
                            ]
                        },
                        "active_slot": None,
                        "has_uncertain_attempts": pending_or_uncertain,
                        "updated_at": now,
                    }
                },
            ],
        )
        return result.modified_count == 1

    async def create_job(self, data: Dict[str, Any]) -> str:
        return await self.insert_one(dict(data))

    async def insert_one(
        self,
        document: Dict[str, Any],
        session: AsyncClientSession | None = None,
    ) -> str:
        _validate_initial_candidate_ledgers(document)
        return await self._base.insert_one(dict(document), session=session)

    async def insert_many(
        self,
        documents: List[Dict[str, Any]],
        session: AsyncClientSession | None = None,
    ) -> List[str]:
        for document in documents:
            _validate_initial_candidate_ledgers(document)
        return await self._base.insert_many(
            [dict(document) for document in documents],
            session=session,
        )

    async def find_one(
        self,
        query: Dict[str, Any],
        include_deleted: bool = False,
        session: AsyncClientSession | None = None,
    ) -> Dict[str, Any] | None:
        document = await self._base.find_one(
            self._execution_filter(query),
            include_deleted=include_deleted,
            session=session,
        )
        if document is None:
            await self._assert_execution_current()
        return document

    async def find_many(
        self,
        query: Dict[str, Any],
        include_deleted: bool = False,
        limit: int = 0,
        skip: int = 0,
        sort: Any = None,
        session: AsyncClientSession | None = None,
    ) -> List[Dict[str, Any]]:
        documents = await self._base.find_many(
            self._execution_filter(query),
            include_deleted=include_deleted,
            limit=limit,
            skip=skip,
            sort=sort,
            session=session,
        )
        if not documents:
            await self._assert_execution_current()
        return documents

    async def update_one(
        self,
        query: Dict[str, Any],
        update_data: Dict[str, Any],
        include_deleted: bool = False,
        session: AsyncClientSession | None = None,
    ) -> bool:
        _reject_atomic_field_updates(update_data)
        updated = await self._base.update_one(
            self._execution_filter(query),
            update_data,
            include_deleted=include_deleted,
            session=session,
        )
        if not updated:
            await self._assert_execution_current()
        return updated

    async def update_many(
        self,
        query: Dict[str, Any],
        update_data: Dict[str, Any],
        include_deleted: bool = False,
        session: AsyncClientSession | None = None,
    ) -> int:
        _reject_atomic_field_updates(update_data)
        updated = await self._base.update_many(
            self._execution_filter(query),
            update_data,
            include_deleted=include_deleted,
            session=session,
        )
        if updated == 0:
            await self._assert_execution_current()
        return updated

    async def increment_one(
        self,
        query: Dict[str, Any],
        increments: Dict[str, int],
        include_deleted: bool = False,
        session: AsyncClientSession | None = None,
    ) -> bool:
        _reject_atomic_field_updates(increments)
        updated = await self._base.increment_one(
            self._execution_filter(query),
            increments,
            include_deleted=include_deleted,
            session=session,
        )
        if not updated:
            await self._assert_execution_current()
        return updated

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

    async def soft_delete_one(
        self,
        query: Dict[str, Any],
        session: AsyncClientSession | None = None,
    ) -> bool:
        changed = await self._base.soft_delete_one(
            self._execution_filter(query),
            session=session,
        )
        if not changed:
            await self._assert_execution_current()
        return changed

    async def restore_one(
        self,
        query: Dict[str, Any],
        session: AsyncClientSession | None = None,
    ) -> bool:
        changed = await self._base.restore_one(
            self._execution_filter(query),
            session=session,
        )
        if not changed:
            await self._assert_execution_current()
        return changed

    async def hard_delete_one(
        self,
        query: Dict[str, Any],
        session: AsyncClientSession | None = None,
    ) -> bool:
        changed = await self._base.hard_delete_one(
            self._execution_filter(query),
            session=session,
        )
        if not changed:
            await self._assert_execution_current()
        return changed

    async def hard_delete_many(
        self,
        query: Dict[str, Any],
        session: AsyncClientSession | None = None,
    ) -> int:
        changed = await self._base.hard_delete_many(
            self._execution_filter(query),
            session=session,
        )
        if changed == 0:
            await self._assert_execution_current()
        return changed

    async def count_documents(
        self,
        query: Dict[str, Any],
        include_deleted: bool = False,
        session: AsyncClientSession | None = None,
    ) -> int:
        count = await self._base.count_documents(
            self._execution_filter(query),
            include_deleted=include_deleted,
            session=session,
        )
        if count == 0:
            await self._assert_execution_current()
        return count

    async def exists(
        self,
        query: Dict[str, Any],
        include_deleted: bool = False,
        session: AsyncClientSession | None = None,
    ) -> bool:
        return (
            await self.count_documents(
                query,
                include_deleted=include_deleted,
                session=session,
            )
        ) > 0

    async def paginate(
        self,
        query: Dict[str, Any],
        page: int = 1,
        page_size: int = 10,
        include_deleted: bool = False,
        sort: Any = None,
        session: AsyncClientSession | None = None,
    ) -> Dict[str, Any]:
        skip = (page - 1) * page_size
        items = await self.find_many(
            query,
            include_deleted=include_deleted,
            limit=page_size,
            skip=skip,
            sort=sort,
            session=session,
        )
        total = await self.count_documents(
            query,
            include_deleted=include_deleted,
            session=session,
        )
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (
                (total + page_size - 1) // page_size
                if page_size > 0
                else 0
            ),
        }

    async def get_job(self, job_id: str) -> Dict[str, Any]:
        doc = await self._base.find_one(
            self._execution_filter({"_id": to_object_id(job_id)})
        )
        if doc is None:
            if current_job_execution() is not None:
                raise JobExecutionLeaseLost(
                    "Generation Job execution lease is no longer current"
                )
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

    @staticmethod
    def _validate_candidate_completion_chain(
        checkpoints: Sequence[CandidatePipelineCheckpointV1],
        *,
        entry: CandidatePipelineProgressV1,
        expected_scene_count: int,
        max_repair_cycles: int,
    ) -> CandidatePipelineCompletionEvidenceV1:
        evidence = validate_candidate_pipeline_completion_chain(
            checkpoints,
            chapter_id=entry.chapter_id,
            expected_scene_count=expected_scene_count,
            max_repair_cycles=max_repair_cycles,
        )
        if (
            entry.source != evidence.prose.source
            or entry.state_proposal_id != evidence.state.proposal_id
            or entry.attempt_count != evidence.attempt_count
            or entry.truncation_count != evidence.truncation_count
            or entry.outline_issue_categories
            != evidence.adherence.issue_categories
            or entry.consistency_issue_count
            != evidence.state.consistency_issue_count
            or entry.scene_coverage_count != expected_scene_count
            or entry.repair_cycles_used != evidence.repair_cycles_used
        ):
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline progress does not match its checkpoint"
            )
        return evidence

    @staticmethod
    async def _candidate_completion_authority(
        job: Mapping[str, Any],
        *,
        chapter_id: str,
    ) -> tuple[int, int]:
        """Re-read the two authorities that terminal progress cannot self-report."""

        try:
            from backend.services.generation.chapter_candidate_authorization import (
                CANDIDATE_PIPELINE_REVISION,
            )
            from backend.services.generation.chapter_finalization import (
                parse_chapter_finalization_authorization,
            )

            readiness = job.get("readiness")
            if (
                not isinstance(readiness, Mapping)
                or type(readiness.get("version")) is not int
                or readiness.get("version") != 2
            ):
                raise ValueError("candidate readiness is missing")
            planning = readiness.get("planning")
            if not isinstance(planning, Mapping):
                raise ValueError("candidate readiness planning is missing")
            revision = planning.get("chapter_candidate_pipeline_revision")
            if type(revision) is not int or revision != CANDIDATE_PIPELINE_REVISION:
                raise ValueError("candidate pipeline revision changed")
            finalization = parse_chapter_finalization_authorization(
                planning.get("chapter_finalization_authorization")
            )
            max_repair_cycles = finalization["max_repair_cycles"]
            work = readiness.get("work")
            raw_snapshots = (
                work.get("chapters") if isinstance(work, Mapping) else None
            )
            if not isinstance(raw_snapshots, list):
                raise ValueError("candidate worklist is missing")
            matching_snapshots = [
                snapshot
                for snapshot in raw_snapshots
                if isinstance(snapshot, Mapping)
                and str(snapshot.get("chapter_id") or "") == chapter_id
            ]
            if (
                len(matching_snapshots) != 1
                or matching_snapshots[0].get("has_content") is not False
            ):
                raise ValueError("candidate chapter is outside the worklist")
            chapter = await chapter_repo.get_chapter_by_id(chapter_id)
            if str(chapter.get("novel_id") or "") != str(
                job.get("novel_id") or ""
            ):
                raise ValueError("candidate chapter scope changed")
            outline = chapter.get("outline")
            scenes = outline.get("scenes") if isinstance(outline, Mapping) else None
            if (
                not isinstance(scenes, list)
                or not 1 <= len(scenes) <= MAX_CANDIDATE_OUTLINE_SCENES
                or any(not isinstance(scene, Mapping) for scene in scenes)
            ):
                raise ValueError("candidate chapter outline is invalid")
        except Exception as exc:
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline completion authority diverged"
            ) from exc
        return len(scenes), max_repair_cycles

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
        result = await self._collection_update_one(
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
        entry: CandidatePipelineProgressV1,
    ) -> bool:
        raw_progress = job.get("progress")
        if raw_progress is None:
            raw_progress = []
        if (
            not isinstance(raw_progress, list)
            or len(raw_progress) > MAX_CANDIDATE_PIPELINE_PROGRESS_ENTRIES
        ):
            raise CandidatePipelineCheckpointConflict(
                "Generation job progress ledger is invalid"
            )
        matched = False
        expected_fields = set(CandidatePipelineProgressV1.model_fields)
        persisted_fields = expected_fields | {
            "candidate_pipeline_completion",
            "completed_at",
        }
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
            if receipt.chapter_id != expected_receipt.chapter_id:
                continue
            if receipt != expected_receipt:
                raise CandidatePipelineCheckpointConflict(
                    "Candidate pipeline completion receipt diverged"
                )
            if set(raw_entry) != persisted_fields:
                raise CandidatePipelineCheckpointConflict(
                    "Candidate pipeline completion progress is invalid"
                )
            try:
                candidate = parse_candidate_pipeline_progress({
                    field: raw_entry[field]
                    for field in expected_fields
                })
            except Exception as exc:
                raise CandidatePipelineCheckpointConflict(
                    "Candidate pipeline completion progress is invalid"
                ) from exc
            if candidate != entry or matched:
                raise CandidatePipelineCheckpointConflict(
                    "Candidate pipeline completion replay diverged"
                )
            matched = True
        return matched

    async def advance_narrative_revision_cursor(
        self,
        job_id: str,
        *,
        chapter_id: str,
        expected_revision: int,
        next_revision: int,
    ) -> bool:
        """Advance one Job-owned mutation cursor under the active chapter fence."""

        normalized_chapter_id = str(chapter_id or "")
        if (
            not normalized_chapter_id
            or type(expected_revision) is not int
            or type(next_revision) is not int
            or expected_revision < 0
            or next_revision != expected_revision + 1
            or next_revision > _MAX_NARRATIVE_REVISION
        ):
            raise CandidatePipelineCheckpointConflict(
                "Generation job narrative revision transition is invalid"
            )
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "status": "running",
                "current_chapter_id": normalized_chapter_id,
                "expected_narrative_revision": expected_revision,
            },
            {
                "$set": {
                    "expected_narrative_revision": next_revision,
                    "updated_at": get_utc_now(),
                }
            },
        )
        if result.modified_count == 1:
            return True
        current = await self.get_job(job_id)
        if (
            str(current.get("status") or "") == "running"
            and str(current.get("current_chapter_id") or "")
            == normalized_chapter_id
            and type(current.get("expected_narrative_revision")) is int
            and current["expected_narrative_revision"] == next_revision
        ):
            return True
        raise CandidatePipelineCheckpointConflict(
            "Generation job narrative revision cursor changed"
        )

    async def bind_job_mutation_recovery(
        self,
        job_id: str,
        binding: JobMutationRecoveryBindingV1,
    ) -> bool:
        """Persist one exact state-only mutation identity before Provider work."""

        try:
            frozen = JobMutationRecoveryBindingV1.model_validate(
                binding.model_dump(mode="python")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise CandidatePipelineCheckpointConflict(
                "Job mutation recovery binding is invalid"
            ) from exc
        if frozen.job_id != str(job_id):
            raise CandidatePipelineCheckpointConflict(
                "Job mutation recovery binding belongs to another Job"
            )
        canonical = frozen.model_dump(mode="json")
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "status": "running",
                "current_chapter_id": frozen.chapter_id,
                "expected_narrative_revision": (
                    frozen.expected_narrative_revision
                ),
                "$or": [
                    {"job_mutation_recovery": {"$exists": False}},
                    {"job_mutation_recovery": None},
                    {"job_mutation_recovery": canonical},
                ],
            },
            {
                "$set": {
                    "job_mutation_recovery": canonical,
                    "updated_at": get_utc_now(),
                }
            },
        )
        if result.matched_count == 1:
            return True
        raise CandidatePipelineCheckpointConflict(
            "Job mutation recovery binding changed"
        )

    async def clear_job_mutation_recovery(
        self,
        job_id: str,
        binding: JobMutationRecoveryBindingV1,
        *,
        terminal_status: str,
    ) -> bool:
        """Clear one exact terminal state-only recovery marker."""

        try:
            frozen = JobMutationRecoveryBindingV1.model_validate(
                binding.model_dump(mode="python")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise CandidatePipelineCheckpointConflict(
                "Job mutation recovery binding is invalid"
            ) from exc
        if frozen.job_id != str(job_id):
            raise CandidatePipelineCheckpointConflict(
                "Job mutation recovery binding belongs to another Job"
            )
        if terminal_status not in {"failed", "aborted"}:
            raise ValueError("Job mutation recovery terminal status is invalid")
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "status": terminal_status,
                "job_mutation_recovery": frozen.model_dump(mode="json"),
            },
            {
                "$unset": {"job_mutation_recovery": ""},
                "$set": {"updated_at": get_utc_now()},
            },
        )
        if result.modified_count == 1:
            return True
        current = await self.get_job(job_id)
        if (
            str(current.get("status") or "") == terminal_status
            and current.get("job_mutation_recovery") is None
        ):
            return True
        raise CandidatePipelineCheckpointConflict(
            "Job mutation recovery release lost its terminal fence"
        )

    @staticmethod
    def _validated_state_dispatch_resolution(
        resolution: StateDispatchResolutionV3,
    ) -> StateDispatchResolutionV3:
        try:
            return StateDispatchResolutionV3.model_validate(
                resolution.model_dump(mode="python")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise CandidatePipelineCheckpointConflict(
                "State dispatch resolution is invalid"
            ) from exc

    async def begin_state_dispatch_resolution(
        self,
        job_id: str,
        binding: JobMutationRecoveryBindingV1,
        action: str,
    ) -> StateDispatchResolutionV3:
        """Persist one immutable explicit action before touching either ledger."""

        try:
            frozen_binding = JobMutationRecoveryBindingV1.model_validate(
                binding.model_dump(mode="python")
            )
            intent = StateDispatchResolutionV3(
                schema_version="state_dispatch_resolution.v3",
                binding=frozen_binding,
                action=action,
                phase="intent",
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise CandidatePipelineCheckpointConflict(
                "State dispatch resolution intent is invalid"
            ) from exc
        if frozen_binding.job_id != str(job_id):
            raise CandidatePipelineCheckpointConflict(
                "State dispatch resolution belongs to another Job"
            )
        canonical_binding = frozen_binding.model_dump(mode="json")
        canonical_intent = intent.model_dump(mode="json")
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "job_mutation_recovery": canonical_binding,
                "$or": [
                    {"state_dispatch_resolution": {"$exists": False}},
                    {"state_dispatch_resolution": None},
                ],
            },
            {
                "$inc": {"execution_epoch": 1},
                "$set": {
                    "state_dispatch_resolution": canonical_intent,
                    "updated_at": get_utc_now(),
                },
                "$unset": {"execution_lease": ""},
            },
        )
        if result.modified_count == 1:
            return intent
        current = await self.get_job(job_id)
        raw = current.get("state_dispatch_resolution")
        try:
            existing = StateDispatchResolutionV3.model_validate(raw)
        except (TypeError, ValueError) as exc:
            raise CandidatePipelineCheckpointConflict(
                "Persisted state dispatch resolution is invalid"
            ) from exc
        if existing.binding == frozen_binding and existing.action == intent.action:
            return existing
        raise CandidatePipelineCheckpointConflict(
            "State dispatch resolution intent diverged"
        )

    async def advance_state_dispatch_resolution(
        self,
        job_id: str,
        resolution: StateDispatchResolutionV3,
        next_phase: str,
    ) -> StateDispatchResolutionV3:
        """Advance exactly one durable action phase with the Job marker fenced."""

        current = self._validated_state_dispatch_resolution(resolution)
        preparation_phases = STATE_DISPATCH_RESOLUTION_PHASES[:4]
        try:
            current_index = preparation_phases.index(current.phase)
            next_index = preparation_phases.index(next_phase)
        except ValueError as exc:
            raise CandidatePipelineCheckpointConflict(
                "State dispatch resolution phase is invalid"
            ) from exc
        if next_index != current_index + 1:
            raise CandidatePipelineCheckpointConflict(
                "State dispatch resolution phase is not monotonic"
            )
        advanced = StateDispatchResolutionV3.model_validate({
            **current.model_dump(mode="python"),
            "phase": next_phase,
        })
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "job_mutation_recovery": current.binding.model_dump(mode="json"),
                "state_dispatch_resolution": current.model_dump(mode="json"),
            },
            {
                "$set": {
                    "state_dispatch_resolution": advanced.model_dump(mode="json"),
                    "updated_at": get_utc_now(),
                }
            },
        )
        if result.modified_count == 1:
            return advanced
        latest = await self.get_job(job_id)
        try:
            existing = StateDispatchResolutionV3.model_validate(
                latest.get("state_dispatch_resolution")
            )
        except (TypeError, ValueError) as exc:
            raise CandidatePipelineCheckpointConflict(
                "Persisted state dispatch resolution disappeared"
            ) from exc
        if existing == advanced:
            return existing
        raise CandidatePipelineCheckpointConflict(
            "State dispatch resolution phase raced"
        )

    async def acknowledge_state_dispatch_attempts(
        self,
        job_id: str,
        resolution: StateDispatchResolutionV3,
    ) -> StateDispatchResolutionV3:
        """Resolve every live outer attempt under the durable action fence."""

        current = self._validated_state_dispatch_resolution(resolution)
        if current.phase != "proposal_acknowledged":
            raise CandidatePipelineCheckpointConflict(
                "State dispatch attempts are not ready for resolution"
            )
        advanced = StateDispatchResolutionV3.model_validate({
            **current.model_dump(mode="python"),
            "phase": "attempts_acknowledged",
        })
        acknowledged_state = f"uncertain_{current.action}_acknowledged"
        now = get_utc_now()
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "job_mutation_recovery": current.binding.model_dump(mode="json"),
                "state_dispatch_resolution": current.model_dump(mode="json"),
            },
            {
                "$set": {
                    "attempt_slots.$[slot].state": acknowledged_state,
                    "attempt_slots.$[slot].updated_at": now,
                    "active_token_reservations.$[reservation].state": (
                        acknowledged_state
                    ),
                    "active_token_reservations.$[reservation].updated_at": now,
                    "has_uncertain_attempts": False,
                    "attempt_reservation": None,
                    "state_dispatch_resolution": advanced.model_dump(mode="json"),
                    "updated_at": now,
                },
            },
            array_filters=[
                {"slot.state": {"$in": ["claimed", "uncertain"]}},
                {"reservation.state": {"$in": ["claimed", "uncertain"]}},
            ],
        )
        if result.modified_count == 1:
            return advanced
        latest = await self.get_job(job_id)
        try:
            existing = StateDispatchResolutionV3.model_validate(
                latest.get("state_dispatch_resolution")
            )
        except (TypeError, ValueError) as exc:
            raise CandidatePipelineCheckpointConflict(
                "State dispatch attempt resolution disappeared"
            ) from exc
        if existing == advanced and not latest.get("has_uncertain_attempts"):
            return existing
        raise CandidatePipelineCheckpointConflict(
            "State dispatch attempt resolution raced"
        )

    async def transition_state_dispatch_retry(
        self,
        job_id: str,
        resolution: StateDispatchResolutionV3,
        *,
        last_checkpoint_index: int,
    ) -> StateDispatchResolutionV3:
        """Set Job running only after both ledgers and the receipt key are resolved."""

        current = self._validated_state_dispatch_resolution(resolution)
        if current.action != "retry" or current.phase != "proposal_released":
            raise CandidatePipelineCheckpointConflict(
                "State dispatch retry is not ready to transition"
            )
        if (
            type(last_checkpoint_index) is not int
            or last_checkpoint_index < 0
            or last_checkpoint_index > MAX_CANDIDATE_PIPELINE_PROGRESS_ENTRIES
        ):
            raise CandidatePipelineCheckpointConflict(
                "State dispatch retry checkpoint cursor is invalid"
            )
        transitioned = StateDispatchResolutionV3.model_validate({
            **current.model_dump(mode="python"),
            "phase": "job_transitioned",
        })
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "job_mutation_recovery": current.binding.model_dump(mode="json"),
                "state_dispatch_resolution": current.model_dump(mode="json"),
            },
            {
                "$set": {
                    "status": "running",
                    "pause_reason": None,
                    "error": None,
                    "active_slot": "global",
                    "has_uncertain_attempts": False,
                    "confirm_uncertain_prose_retry": True,
                    "last_checkpoint_index": last_checkpoint_index,
                    "state_dispatch_resolution": transitioned.model_dump(
                        mode="json"
                    ),
                    "updated_at": get_utc_now(),
                }
            },
        )
        if result.modified_count == 1:
            return transitioned
        latest = await self.get_job(job_id)
        try:
            existing = StateDispatchResolutionV3.model_validate(
                latest.get("state_dispatch_resolution")
            )
        except (TypeError, ValueError) as exc:
            raise CandidatePipelineCheckpointConflict(
                "State dispatch retry transition disappeared"
            ) from exc
        if existing == transitioned and latest.get("status") == "running":
            return existing
        raise CandidatePipelineCheckpointConflict(
            "State dispatch retry transition raced"
        )

    async def complete_state_dispatch_terminal(
        self,
        job_id: str,
        resolution: StateDispatchResolutionV3,
    ) -> StateDispatchResolutionV3:
        """Publish a replayable skip/abort receipt with the terminal Job state."""

        current = self._validated_state_dispatch_resolution(resolution)
        expected_status = {"skip": "failed", "abort": "aborted"}.get(
            current.action
        )
        if current.phase != "proposal_released" or expected_status is None:
            raise CandidatePipelineCheckpointConflict(
                "State dispatch terminal resolution is not ready"
            )
        terminal = StateDispatchResolutionV3.model_validate({
            **current.model_dump(mode="python"),
            "phase": "terminal",
        })
        terminal_fields: dict[str, Any] = {
            "status": expected_status,
            "pause_reason": (
                "uncertain_skipped" if current.action == "skip" else None
            ),
            "active_slot": None,
            "current_chapter_id": None,
            "has_uncertain_attempts": False,
            "attempt_reservation": None,
            "error": (
                {
                    "step": "uncertain_attempt",
                    "message": (
                        "用户选择跳过可能已发出的 Provider 请求；"
                        "请人工检查章节后再恢复"
                    ),
                }
                if current.action == "skip"
                else None
            ),
            "state_dispatch_resolution": terminal.model_dump(mode="json"),
            "updated_at": get_utc_now(),
        }
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "job_mutation_recovery": current.binding.model_dump(mode="json"),
                "state_dispatch_resolution": current.model_dump(mode="json"),
            },
            {
                "$set": terminal_fields,
                "$unset": {"job_mutation_recovery": ""},
            },
        )
        if result.modified_count == 1:
            return terminal
        latest = await self.get_job(job_id)
        try:
            existing = StateDispatchResolutionV3.model_validate(
                latest.get("state_dispatch_resolution")
            )
        except (TypeError, ValueError) as exc:
            raise CandidatePipelineCheckpointConflict(
                "State dispatch terminal receipt disappeared"
            ) from exc
        if existing == terminal and latest.get("status") == expected_status:
            return existing
        raise CandidatePipelineCheckpointConflict(
            "State dispatch terminal transition raced"
        )

    async def complete_job_mutation_chapter(
        self,
        job_id: str,
        *,
        receipt: JobMutationReceiptV1,
        entry: Mapping[str, Any],
        tokens_delta: int,
    ) -> bool:
        """Publish state-only progress and advance its exact receipt atomically."""

        try:
            frozen = JobMutationReceiptV1.model_validate(
                receipt.model_dump(mode="python")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise CandidatePipelineCheckpointConflict(
                "Job mutation receipt is invalid"
            ) from exc
        binding = frozen.binding
        if binding.job_id != str(job_id):
            raise CandidatePipelineCheckpointConflict(
                "Job mutation receipt belongs to another Job"
            )
        if (
            type(tokens_delta) is not int
            or tokens_delta < 0
            or tokens_delta > _MAX_NARRATIVE_REVISION
        ):
            raise CandidatePipelineCheckpointConflict(
                "Job mutation progress token delta is invalid"
            )
        progress = dict(entry)
        canonical_receipt = frozen.model_dump(mode="json")
        if progress.get("job_mutation_receipt") != canonical_receipt:
            raise CandidatePipelineCheckpointConflict(
                "Job mutation progress receipt diverged"
            )
        if str(progress.get("chapter_id") or "") != binding.chapter_id:
            raise CandidatePipelineCheckpointConflict(
                "Job mutation progress chapter diverged"
            )
        supplied_tokens_delta = progress.get("job_mutation_tokens_delta")
        if supplied_tokens_delta is not None and (
            type(supplied_tokens_delta) is not int
            or supplied_tokens_delta != tokens_delta
        ):
            raise CandidatePipelineCheckpointConflict(
                "Job mutation progress token delta diverged"
            )
        progress["job_mutation_tokens_delta"] = tokens_delta
        canonical_binding = binding.model_dump(mode="json")
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "status": "running",
                "current_chapter_id": binding.chapter_id,
                "expected_narrative_revision": (
                    binding.expected_narrative_revision
                ),
                "job_mutation_recovery": canonical_binding,
                "progress": {
                    "$not": {
                        "$elemMatch": {
                            "job_mutation_receipt": canonical_receipt,
                        }
                    }
                },
                "$expr": {
                    "$lt": [
                        {"$size": {"$ifNull": ["$progress", []]}},
                        MAX_CANDIDATE_PIPELINE_PROGRESS_ENTRIES,
                    ]
                },
            },
            {
                "$push": {"progress": progress},
                "$inc": {"tokens_used": tokens_delta},
                "$set": {
                    "expected_narrative_revision": (
                        frozen.next_narrative_revision
                    ),
                    "current_chapter_id": None,
                    "updated_at": get_utc_now(),
                },
                "$unset": {"job_mutation_recovery": ""},
            },
        )
        if result.modified_count == 1:
            return True
        current = await self.get_job(job_id)
        matching_progress = [
            dict(item)
            for item in current.get("progress") or []
            if isinstance(item, Mapping)
            and item.get("job_mutation_receipt") == canonical_receipt
        ]
        if matching_progress:
            def replay_projection(value: Mapping[str, Any]) -> dict[str, Any]:
                projected = dict(value)
                # completed_at is display metadata, not part of the stable
                # mutation/result identity.
                projected.pop("completed_at", None)
                return projected

            if (
                len(matching_progress) == 1
                and current.get("expected_narrative_revision")
                == frozen.next_narrative_revision
                and current.get("job_mutation_recovery") is None
                and replay_projection(matching_progress[0])
                == replay_projection(progress)
            ):
                return True
            raise CandidatePipelineCheckpointConflict(
                "Job mutation completion replay diverged"
            )
        raise CandidatePipelineCheckpointConflict(
            "Job mutation completion lost its revision fence"
        )

    async def update_job_authorization(
        self,
        job_id: str,
        fields: Dict[str, Any],
        *,
        previous_revision: int | None,
        next_revision: int,
        previous_status: str,
        previous_authorization_revision: int | None,
        previous_readiness_digest: str | None,
        previous_active_slot: str | None,
        previous_execution_epoch: int,
    ) -> bool:
        """Rebind readiness and its revision cursor in one fenced update."""

        _reject_atomic_field_updates(fields)
        if (
            (previous_revision is not None and type(previous_revision) is not int)
            or type(next_revision) is not int
            or next_revision < 0
            or next_revision > _MAX_NARRATIVE_REVISION
            or previous_status not in {"paused", "interrupted", "failed"}
            or type(previous_execution_epoch) is not int
            or previous_execution_epoch < 0
            or (
                previous_authorization_revision is not None
                and type(previous_authorization_revision) is not int
            )
            or (
                previous_readiness_digest is not None
                and not isinstance(previous_readiness_digest, str)
            )
            or (
                previous_active_slot is not None
                and not isinstance(previous_active_slot, str)
            )
        ):
            raise ValueError("Generation job reauthorization revision is invalid")
        query: dict[str, Any] = {
            "_id": to_object_id(job_id),
            "is_deleted": False,
            "status": previous_status,
            "active_slot": previous_active_slot,
            "candidate_pipeline_checkpoints": [],
            "$or": [
                {"job_mutation_recovery": {"$exists": False}},
                {"job_mutation_recovery": None},
            ],
        }
        if previous_execution_epoch == 0:
            query["$and"] = [{
                "$or": [
                    {"execution_epoch": 0},
                    {"execution_epoch": {"$exists": False}},
                ]
            }]
        else:
            query["execution_epoch"] = previous_execution_epoch
        if previous_revision is None:
            query["expected_narrative_revision"] = {"$exists": False}
        else:
            query["expected_narrative_revision"] = previous_revision
        if previous_authorization_revision is None:
            query["authorization_revision"] = {"$exists": False}
        else:
            query["authorization_revision"] = previous_authorization_revision
        if previous_readiness_digest is None:
            query["readiness.digest"] = {"$exists": False}
        else:
            query["readiness.digest"] = previous_readiness_digest
        result = await self._collection_update_one(
            query,
            {
                "$inc": {"execution_epoch": 1},
                "$unset": {"execution_lease": ""},
                "$set": {
                    **dict(fields),
                    "expected_narrative_revision": next_revision,
                    "updated_at": get_utc_now(),
                }
            },
        )
        if result.modified_count == 1:
            return True
        raise CandidatePipelineCheckpointConflict(
            "Generation job reauthorization lost its revision fence"
        )

    async def transition_job_resume(
        self,
        job_id: str,
        fields: Mapping[str, Any],
        *,
        previous_status: str,
        previous_execution_epoch: int,
    ) -> bool:
        """Publish a manual resume while revoking any finishing old worker."""

        updates = dict(fields)
        _reject_atomic_field_updates(updates)
        if (
            previous_status not in {"paused", "interrupted", "failed"}
            or type(previous_execution_epoch) is not int
            or previous_execution_epoch < 0
            or updates.get("status") != "running"
        ):
            raise CandidatePipelineCheckpointConflict(
                "Generation job resume command is invalid"
            )
        epoch_query: dict[str, Any] = {
            "execution_epoch": previous_execution_epoch
        }
        if previous_execution_epoch == 0:
            epoch_query = {
                "$or": [
                    {"execution_epoch": 0},
                    {"execution_epoch": {"$exists": False}},
                ]
            }
        result = await self._collection_update_one(
            {
                "$and": [
                    {
                        "_id": to_object_id(job_id),
                        "is_deleted": False,
                        "status": previous_status,
                        "state_dispatch_resolution": None,
                    },
                    epoch_query,
                ]
            },
            {
                "$inc": {"execution_epoch": 1},
                "$unset": {"execution_lease": ""},
                "$set": {**updates, "updated_at": get_utc_now()},
            },
        )
        if result.modified_count == 1:
            return True
        raise CandidatePipelineCheckpointConflict(
            "Generation job resume lost its execution fence"
        )

    async def complete_job_abort(
        self,
        job_id: str,
        fields: Mapping[str, Any],
    ) -> bool:
        """Publish abort while atomically freezing every live paid attempt."""

        updates = dict(fields)
        _reject_atomic_field_updates(updates)
        if updates.get("status") != "aborted":
            raise CandidatePipelineCheckpointConflict(
                "Generation job abort command is invalid"
            )
        current = await self.get_job(job_id)
        previous_epoch = current.get("execution_epoch", 0)
        if (
            type(previous_epoch) is not int
            or previous_epoch < 0
            or previous_epoch >= _MAX_NARRATIVE_REVISION
        ):
            raise CandidatePipelineCheckpointConflict(
                "Generation job abort execution epoch is invalid"
            )
        epoch_query: dict[str, Any] = {
            "execution_epoch": previous_epoch
        }
        if previous_epoch == 0:
            epoch_query = {
                "$or": [
                    {"execution_epoch": 0},
                    {"execution_epoch": {"$exists": False}},
                ]
            }
        for enforced in (
            "attempt_reservation",
            "has_uncertain_attempts",
        ):
            updates.pop(enforced, None)
        now = get_utc_now()
        acknowledged_state = "uncertain_abort_acknowledged"
        result = await self._collection_update_one(
            {
                "$and": [
                    {
                        "_id": to_object_id(job_id),
                        "is_deleted": False,
                        "status": {"$nin": ["completed", "aborted"]},
                        "state_dispatch_resolution": None,
                    },
                    epoch_query,
                ]
            },
            [
                {
                    "$set": {
                        **_live_attempt_transition_fields(
                            source_states=("claimed", "uncertain"),
                            target_state=acknowledged_state,
                            now=now,
                        ),
                        "execution_epoch": previous_epoch + 1,
                        "execution_lease": "$$REMOVE",
                        "attempt_reservation": None,
                        "has_uncertain_attempts": False,
                        **updates,
                        "updated_at": now,
                    }
                }
            ],
        )
        if result.modified_count == 1:
            return True
        current = await self.get_job(job_id)
        if current.get("status") == "aborted":
            return True
        raise CandidatePipelineCheckpointConflict(
            "Generation job abort lost its execution fence"
        )

    async def complete_candidate_pipeline_chapter(
        self,
        job_id: str,
        *,
        chapter_id: str,
        expected_checkpoints: Sequence[CandidatePipelineCheckpointV1],
        entry: CandidatePipelineProgressV1,
        tokens_delta: int,
        expected_narrative_revision: int | None = None,
        next_narrative_revision: int | None = None,
    ) -> bool:
        """Atomically publish progress and release exactly one checkpoint tail."""
        normalized_chapter_id = str(chapter_id or "")
        try:
            validated_entry = parse_candidate_pipeline_progress(entry)
        except Exception as exc:
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline progress identity is invalid"
            ) from exc
        if validated_entry.chapter_id != normalized_chapter_id:
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
        revision_transition_supplied = (
            expected_narrative_revision is not None
            or next_narrative_revision is not None
        )
        if revision_transition_supplied and (
            type(expected_narrative_revision) is not int
            or type(next_narrative_revision) is not int
            or expected_narrative_revision < 0
            or next_narrative_revision != expected_narrative_revision + 1
            or next_narrative_revision > _MAX_NARRATIVE_REVISION
        ):
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline narrative revision transition is invalid"
            )
        if (
            isinstance(expected_checkpoints, (str, bytes))
            or not isinstance(expected_checkpoints, Sequence)
            or not 1
            <= len(expected_checkpoints)
            <= MAX_CHAPTER_CANDIDATE_PIPELINE_CHECKPOINTS
        ):
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline completion ledger is invalid"
            )
        try:
            validated_checkpoints = tuple(
                parse_candidate_pipeline_checkpoint(checkpoint)
                for checkpoint in expected_checkpoints
            )
        except Exception as exc:
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline completion ledger is invalid"
            ) from exc
        if any(
            checkpoint.chapter_id != normalized_chapter_id
            or checkpoint.sequence != sequence
            for sequence, checkpoint in enumerate(
                validated_checkpoints,
                start=1,
            )
        ) or len({
            checkpoint.checkpoint_id for checkpoint in validated_checkpoints
        }) != len(validated_checkpoints):
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline completion ledger is invalid"
            )
        validated_checkpoint = validated_checkpoints[-1]
        receipt = CandidatePipelineCompletionV1(
            schema_version="candidate_pipeline_completion.v1",
            checkpoint_id=validated_checkpoint.checkpoint_id,
            checkpoint_digest=candidate_pipeline_checkpoint_digest(
                validated_checkpoint
            ),
            ledger_digest=candidate_pipeline_checkpoint_ledger_digest(
                validated_checkpoints
            ),
            sequence=validated_checkpoint.sequence,
            chapter_id=validated_checkpoint.chapter_id,
            source=validated_checkpoint.source,
            state_proposal_id=validated_checkpoint.proposal_id,
            tokens_delta=tokens_delta,
        )
        job = await self.get_job(job_id)
        current_revision = job.get("expected_narrative_revision")
        if current_revision is not None and type(current_revision) is not int:
            raise CandidatePipelineCheckpointConflict(
                "Generation job narrative revision cursor is invalid"
            )
        if (current_revision is not None) != revision_transition_supplied:
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline narrative revision transition is required"
            )
        if self._completed_candidate_progress(
            job,
            expected_receipt=receipt,
            entry=validated_entry,
        ):
            if (
                revision_transition_supplied
                and current_revision != next_narrative_revision
            ):
                raise CandidatePipelineCheckpointConflict(
                    "Candidate pipeline completion revision diverged"
                )
            return True
        if (
            revision_transition_supplied
            and current_revision != expected_narrative_revision
        ):
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline narrative revision cursor changed"
            )
        expected_scene_count, max_repair_cycles = (
            await self._candidate_completion_authority(
                job,
                chapter_id=normalized_chapter_id,
            )
        )
        evidence = self._validate_candidate_completion_chain(
            validated_checkpoints,
            entry=validated_entry,
            expected_scene_count=expected_scene_count,
            max_repair_cycles=max_repair_cycles,
        )
        if evidence.state != validated_checkpoint:
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline completion ledger is invalid"
            )
        raw_progress = job.get("progress")
        if raw_progress is None:
            raw_progress = []
        if (
            not isinstance(raw_progress, list)
            or len(raw_progress) >= MAX_CANDIDATE_PIPELINE_PROGRESS_ENTRIES
        ):
            raise CandidatePipelineCheckpointConflict(
                "Generation job progress ledger capacity is exhausted"
            )
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
        self._validate_candidate_completion_chain(
            checkpoints,
            entry=validated_entry,
            expected_scene_count=expected_scene_count,
            max_repair_cycles=max_repair_cycles,
        )
        if tuple(checkpoints) != validated_checkpoints:
            raise CandidatePipelineCheckpointConflict(
                "Candidate pipeline completion ledger changed"
            )
        tail = checkpoints[-1]
        value = validated_entry.model_dump(mode="json")
        value["candidate_pipeline_completion"] = receipt.model_dump(
            mode="json"
        )
        value["completed_at"] = get_utc_now()
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "status": "running",
                "current_chapter_id": normalized_chapter_id,
                "candidate_pipeline_checkpoints": [
                    checkpoint.model_dump(mode="json")
                    for checkpoint in checkpoints
                ],
                "progress.candidate_pipeline_completion.ledger_digest": {
                    "$ne": receipt.ledger_digest
                },
                **(
                    {
                        "expected_narrative_revision": (
                            expected_narrative_revision
                        )
                    }
                    if revision_transition_supplied
                    else {}
                ),
                "$expr": {
                    "$lt": [
                        {
                            "$size": {
                                "$ifNull": ["$progress", []]
                            }
                        },
                        MAX_CANDIDATE_PIPELINE_PROGRESS_ENTRIES,
                    ]
                },
            },
            {
                "$push": {"progress": value},
                "$inc": {"tokens_used": tokens_delta},
                "$set": {
                    "candidate_pipeline_checkpoints": [],
                    "current_chapter_id": None,
                    **(
                        {
                            "expected_narrative_revision": (
                                next_narrative_revision
                            )
                        }
                        if revision_transition_supplied
                        else {}
                    ),
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
            entry=validated_entry,
        ):
            if (
                revision_transition_supplied
                and current.get("expected_narrative_revision")
                != next_narrative_revision
            ):
                raise CandidatePipelineCheckpointConflict(
                    "Candidate pipeline completion revision diverged"
                )
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
        result = await self._collection_update_one(
            {"_id": to_object_id(job_id), "is_deleted": False},
            {
                "$push": {"diagnostics": {"$each": [dict(event)], "$slice": -200}},
                "$set": {"diagnostic_schema_version": 1, "updated_at": get_utc_now()},
            },
        )
        return result.matched_count > 0

    async def append_progress(self, job_id: str, entry: Dict[str, Any], tokens_delta: int) -> bool:
        # $push progress + $inc tokens_used 在一次原子 update 内完成。
        if (
            "candidate_pipeline_completion" in entry
            or "job_mutation_receipt" in entry
        ):
            raise ValueError(
                "Mutation completion requires an atomic repository command"
            )
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "$expr": {
                    "$lt": [
                        {
                            "$size": {
                                "$ifNull": ["$progress", []]
                            }
                        },
                        MAX_CANDIDATE_PIPELINE_PROGRESS_ENTRIES,
                    ]
                },
            },
            {
                "$push": {"progress": entry},
                "$inc": {"tokens_used": int(tokens_delta)},
                "$set": {"updated_at": get_utc_now()},
            },
        )
        if result.matched_count > 0:
            return True
        current = await self.get_job(job_id)
        raw_progress = current.get("progress")
        if (
            isinstance(raw_progress, list)
            and len(raw_progress) >= MAX_CANDIDATE_PIPELINE_PROGRESS_ENTRIES
        ):
            raise ValueError(
                "Generation job progress ledger capacity is exhausted"
            )
        return False

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
        result = await self._collection_find_one_and_update(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "state_dispatch_resolution": None,
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
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "state_dispatch_resolution": None,
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
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "state_dispatch_resolution": None,
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
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "state_dispatch_resolution": None,
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
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "state_dispatch_resolution": None,
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
        if action not in STATE_DISPATCH_RESOLUTION_ACTIONS:
            raise ValueError("Unknown uncertain-attempt action")
        now = get_utc_now()
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "state_dispatch_resolution": None,
            },
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
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "state_dispatch_resolution": None,
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
            "state_dispatch_resolution": None,
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
        result = await self._collection_update_one(query, update)
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
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "state_dispatch_resolution": None,
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
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "state_dispatch_resolution": None,
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
        result = await self._collection_update_one(
            {
                "_id": to_object_id(job_id),
                "is_deleted": False,
                "state_dispatch_resolution": None,
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
        result = await self._collection_update_one(query, update)
        return result.modified_count == 1
generation_job_repo = GenerationJobRepository()
