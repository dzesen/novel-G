"""Persistence boundary for bounded Agent Runtime readiness, runs, steps, and events."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
from typing import Any, Literal, Mapping, Sequence
from uuid import UUID, uuid5

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from backend.db import collections
from backend.db.errors import NotFoundError
from backend.db.mongo import get_database
from backend.db.utils import get_utc_now, to_object_id


STEP_NAMESPACE = UUID("e0dc1a47-9a48-4cb5-a165-2259c139fbb3")
EVENT_NAMESPACE = UUID("814b1e75-93a6-49da-903c-03de77a202c6")
CALL_NAMESPACE = UUID("73998e71-c0cf-4a99-91a8-8a01ad636aa9")
MAX_EVENT_PAYLOAD_BYTES = 16_384


class _EventPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _ReadyEventPayload(_EventPayload):
    status: Literal["ready"]


class _BoundEventPayload(_EventPayload):
    status: Literal["bound"]


class _RunningEventPayload(_EventPayload):
    status: Literal["running"]


class _RunPausedEventPayload(_EventPayload):
    status: Literal["paused"]
    reason_code: Literal[
        "budget_exhausted",
        "authorization_required",
        "concurrent_narrative_change",
        "ambiguous_identity",
        "manual_approval_required",
        "uncertain_paid_attempt",
    ]


class _RunTerminatedEventPayload(_EventPayload):
    status: Literal["completed", "failed", "cancelled"]
    reason_code: Literal[
        "goal_satisfied",
        "no_change_required",
        "max_steps_exhausted",
        "deadline_exceeded",
        "planner_output_exhausted",
        "tool_failure_exhausted",
        "policy_violation",
        "invariant_violation",
        "cancelled_by_user",
    ]

    @model_validator(mode="after")
    def validate_status_reason_pair(self) -> "_RunTerminatedEventPayload":
        valid_reasons = {
            "completed": {"goal_satisfied", "no_change_required"},
            "failed": {
                "max_steps_exhausted",
                "deadline_exceeded",
                "planner_output_exhausted",
                "tool_failure_exhausted",
                "policy_violation",
                "invariant_violation",
            },
            "cancelled": {"cancelled_by_user"},
        }
        if self.reason_code not in valid_reasons[self.status]:
            raise ValueError("termination status and reason do not match")
        return self


class _RunSupersededEventPayload(_EventPayload):
    status: Literal["superseded"]
    successor_run_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    reason_code: Literal["continued_by_successor"]


class _OrdinalEventPayload(_EventPayload):
    ordinal: int = Field(ge=0)


class _DecisionEventPayload(_OrdinalEventPayload):
    decision_kind: Literal["call_tool", "propose_finish"]


class _AttemptReservedEventPayload(_OrdinalEventPayload):
    kind: Literal["planner", "tool"]


class _StepPlannedEventPayload(_DecisionEventPayload):
    decision_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class _PolicyDecidedEventPayload(_DecisionEventPayload):
    allowed: bool
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class _ToolDispatchedEventPayload(_OrdinalEventPayload):
    tool_name: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
    )
    tool_version: int = Field(ge=1)
    invocation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class _ToolObservedEventPayload(_OrdinalEventPayload):
    status: Literal[
        "ok",
        "retryable_error",
        "blocked",
        "uncertain",
        "permanent_error",
    ]
    code: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[a-z][a-z0-9_.-]*$",
    )
    observation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class _AttemptUncertainEventPayload(_OrdinalEventPayload):
    reason_code: Literal["dispatch_outcome_unknown"]


class _StepCompletedEventPayload(_OrdinalEventPayload):
    status: Literal["completed"]
    kind: Literal["tool", "finish"]
    step_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class _ProposalRecordedEventPayload(_OrdinalEventPayload):
    proposal_id: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    proposal_kind: Literal["chapter_prose_candidate"]


class _MutationCommittedEventPayload(_OrdinalEventPayload):
    mutation_id: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    change_class: Literal["temporary_candidate"]


EVENT_PAYLOAD_MODELS: dict[str, type[_EventPayload]] = {
    "run_created": _ReadyEventPayload,
    "readiness_bound": _BoundEventPayload,
    "run_started": _RunningEventPayload,
    "run_resumed": _RunningEventPayload,
    "step_planned": _StepPlannedEventPayload,
    "policy_decided": _PolicyDecidedEventPayload,
    "attempt_reserved": _AttemptReservedEventPayload,
    "tool_dispatched": _ToolDispatchedEventPayload,
    "tool_observed": _ToolObservedEventPayload,
    "proposal_recorded": _ProposalRecordedEventPayload,
    "mutation_committed": _MutationCommittedEventPayload,
    "attempt_settled": _OrdinalEventPayload,
    "attempt_uncertain": _AttemptUncertainEventPayload,
    "step_completed": _StepCompletedEventPayload,
    "run_paused": _RunPausedEventPayload,
    "run_superseded": _RunSupersededEventPayload,
    "run_terminated": _RunTerminatedEventPayload,
}

STEP_TRANSITIONS: dict[str, frozenset[str]] = {
    "planning": frozenset({"policy_checked", "paused", "failed"}),
    "policy_checked": frozenset({"executing", "observed", "paused", "failed"}),
    "executing": frozenset({"observed", "paused", "failed"}),
    "observed": frozenset({"completed", "paused", "failed"}),
    "paused": frozenset({
        "planning",
        "policy_checked",
        "executing",
        "observed",
        "failed",
    }),
    "completed": frozenset(),
    "failed": frozenset(),
}


class AgentRuntimeReadinessConflict(ValueError):
    """A readiness was expired, drifted, or already bound by another request."""


class AgentRuntimeLeaseUnavailable(ValueError):
    """Another non-expired worker lease owns the run."""


class AgentRuntimeStateConflict(ValueError):
    """A persisted run or step no longer matches the requested transition."""


class AgentRuntimeBudgetExceeded(ValueError):
    """A conservative call reservation would exceed the authorization."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = str(reason_code)
        super().__init__(self.reason_code)


def _required_object_id(value: str | ObjectId | None, field: str) -> ObjectId:
    if value is None or not str(value).strip():
        raise ValueError(f"{field} is required")
    return to_object_id(value)


def project_agent_runtime_event_payload(
    event_type: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    model = EVENT_PAYLOAD_MODELS.get(str(event_type))
    if model is None:
        raise ValueError(f"unsupported Agent event type: {event_type}")
    try:
        return model.model_validate(dict(payload)).model_dump(mode="json")
    except ValidationError as exc:
        raise ValueError(
            f"Agent event payload is invalid for '{event_type}'"
        ) from exc


def _event_matches(
    existing: Mapping[str, Any],
    *,
    run_id: ObjectId,
    event_key: str,
    event_type: str,
    step_id: str | None,
    payload: Mapping[str, Any],
) -> bool:
    return all((
        existing.get("run_id") == run_id,
        existing.get("event_key") == event_key,
        existing.get("type") == event_type,
        existing.get("step_id") == step_id,
        existing.get("payload") == dict(payload),
    ))


def _attempt_by_key(run: Mapping[str, Any], call_key: str) -> dict[str, Any] | None:
    for raw in run.get("attempts") or []:
        if isinstance(raw, Mapping) and raw.get("call_key") == str(call_key):
            return dict(raw)
    return None


class AgentRuntimeRepository:
    @property
    def readiness(self):
        return get_database()[collections.AGENT_RUNTIME_READINESS]

    @property
    def runs(self):
        return get_database()[collections.AGENT_RUNTIME_RUNS]

    @property
    def steps(self):
        return get_database()[collections.AGENT_RUNTIME_STEPS]

    @property
    def events(self):
        return get_database()[collections.AGENT_RUNTIME_EVENTS]

    async def create_readiness(
        self,
        *,
        readiness_id: str,
        owner_id: str,
        novel_id: str,
        digest: str,
        authorization: Mapping[str, Any],
        issued_at: datetime,
        expires_at: datetime,
    ) -> dict[str, Any]:
        readiness_object_id = _required_object_id(readiness_id, "readiness_id")
        owner_object_id = _required_object_id(owner_id, "owner_id")
        novel_object_id = _required_object_id(novel_id, "novel_id")
        document = {
            "_id": readiness_object_id,
            "owner_id": owner_object_id,
            "novel_id": novel_object_id,
            "schema_version": "agent_readiness_envelope.v1",
            "status": "inspected",
            "digest": str(digest),
            "authorization": deepcopy(dict(authorization)),
            "issued_at": issued_at,
            "expires_at": expires_at,
            "bound_run_id": None,
            "start_request_id": None,
            "bound_at": None,
            "created_at": issued_at,
            "updated_at": issued_at,
            "is_deleted": False,
            "deleted_at": None,
        }
        await self.readiness.update_one(
            {"_id": readiness_object_id},
            {"$setOnInsert": document},
            upsert=True,
        )
        stored = await self.readiness.find_one({"_id": readiness_object_id})
        if stored is None:
            raise AgentRuntimeStateConflict("readiness insert did not become visible")
        if (
            stored.get("owner_id") != owner_object_id
            or stored.get("digest") != str(digest)
        ):
            raise AgentRuntimeReadinessConflict("readiness id already has another identity")
        return stored

    async def get_readiness_owned(
        self,
        *,
        readiness_id: str,
        owner_id: str,
    ) -> dict[str, Any]:
        document = await self.readiness.find_one({
            "_id": _required_object_id(readiness_id, "readiness_id"),
            "owner_id": _required_object_id(owner_id, "owner_id"),
            "is_deleted": False,
        })
        if document is None:
            raise NotFoundError(f"Agent readiness '{readiness_id}' was not found")
        return document

    async def bind_readiness(
        self,
        *,
        readiness_id: str,
        owner_id: str,
        digest: str,
        start_request_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        if not str(start_request_id).strip():
            raise ValueError("start_request_id is required")
        readiness_object_id = _required_object_id(readiness_id, "readiness_id")
        owner_object_id = _required_object_id(owner_id, "owner_id")

        existing = await self.runs.find_one({
            "owner_id": owner_object_id,
            "start_request_id": str(start_request_id),
            "is_deleted": False,
        })
        if existing is not None:
            if (
                existing.get("readiness_id") != readiness_object_id
                or existing.get("authorization_digest") != str(digest)
            ):
                raise AgentRuntimeReadinessConflict(
                    "start_request_id already belongs to another readiness"
                )
            return existing

        proposed_run_id = ObjectId()
        bound = await self.readiness.find_one_and_update(
            {
                "_id": readiness_object_id,
                "owner_id": owner_object_id,
                "status": "inspected",
                "digest": str(digest),
                "expires_at": {"$gt": now},
                "is_deleted": False,
            },
            {
                "$set": {
                    "status": "bound",
                    "bound_run_id": proposed_run_id,
                    "start_request_id": str(start_request_id),
                    "bound_at": now,
                    "updated_at": now,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if bound is None:
            current = await self.get_readiness_owned(
                readiness_id=readiness_id,
                owner_id=owner_id,
            )
            if current.get("status") == "inspected" and current.get("expires_at") <= now:
                await self.readiness.update_one(
                    {"_id": readiness_object_id, "status": "inspected"},
                    {"$set": {"status": "expired", "updated_at": now}},
                )
                raise AgentRuntimeReadinessConflict("readiness has expired")
            if (
                current.get("status") == "bound"
                and current.get("start_request_id") == str(start_request_id)
                and current.get("digest") == str(digest)
            ):
                bound = current
            else:
                raise AgentRuntimeReadinessConflict(
                    "readiness digest drifted or was already bound"
                )

        run_id = _required_object_id(bound.get("bound_run_id"), "bound_run_id")
        authorization = deepcopy(dict(bound.get("authorization") or {}))
        novel_object_id = _required_object_id(bound.get("novel_id"), "novel_id")
        predecessor_object_id = (
            _required_object_id(
                authorization.get("predecessor_run_id"),
                "predecessor_run_id",
            )
            if authorization.get("predecessor_run_id")
            else None
        )
        replay_object_id = (
            _required_object_id(
                authorization.get("replay_of_run_id"),
                "replay_of_run_id",
            )
            if authorization.get("replay_of_run_id")
            else None
        )
        lineage = authorization.get("lineage")
        lineage_root_object_id = (
            _required_object_id(lineage.get("root_run_id"), "lineage.root_run_id")
            if isinstance(lineage, Mapping) and lineage.get("root_run_id")
            else run_id
        )
        run_document = {
            "_id": run_id,
            "owner_id": owner_object_id,
            "novel_id": novel_object_id,
            "schema_version": "agent_runtime_run.v1",
            "readiness_id": readiness_object_id,
            "authorization_digest": str(digest),
            "authorization": authorization,
            "start_request_id": str(start_request_id),
            "status": "ready",
            "next_ordinal": 0,
            "active_step_id": None,
            "active_step_seed": None,
            "next_event_sequence": 0,
            "usage": {
                "planner_calls": 0,
                "tool_calls": 0,
                "paid_attempts": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            },
            "tokens_reserved": 0,
            "paid_attempts_reserved": 0,
            "attempts": [],
            "has_uncertain_attempts": False,
            "expected_narrative_revision": authorization.get(
                "baseline_narrative_revision"
            ),
            "lease": None,
            "lease_epoch": 0,
            "termination": None,
            "predecessor_run_id": predecessor_object_id,
            "replay_of_run_id": replay_object_id,
            "lineage_root_run_id": lineage_root_object_id,
            "successor_run_id": None,
            "created_at": now,
            "updated_at": now,
            "is_deleted": False,
            "deleted_at": None,
        }
        if predecessor_object_id is not None:
            predecessor = await self.runs.find_one_and_update(
                {
                    "_id": predecessor_object_id,
                    "owner_id": owner_object_id,
                    "novel_id": novel_object_id,
                    "status": "paused",
                    "successor_run_id": None,
                    "authorization.goal": authorization.get("goal"),
                    "authorization.scope": authorization.get("scope"),
                    "is_deleted": False,
                },
                {"$set": {
                    "status": "superseded",
                    "successor_run_id": run_id,
                    "termination": {
                        "status": "superseded",
                        "category": "superseded",
                        "reason_code": "continued_by_successor",
                        "resumable": False,
                        "occurred_at": now,
                        "step_id": None,
                        "detail_code": "continued_by_successor",
                    },
                    "updated_at": now,
                }},
                return_document=ReturnDocument.AFTER,
            )
            if predecessor is None:
                current_predecessor = await self.runs.find_one({
                    "_id": predecessor_object_id,
                    "owner_id": owner_object_id,
                    "is_deleted": False,
                })
                if not (
                    current_predecessor
                    and current_predecessor.get("status") == "superseded"
                    and current_predecessor.get("successor_run_id") == run_id
                ):
                    await self.readiness.update_one(
                        {
                            "_id": readiness_object_id,
                            "status": "bound",
                            "bound_run_id": run_id,
                            "start_request_id": str(start_request_id),
                        },
                        {"$set": {
                            "status": "inspected",
                            "bound_run_id": None,
                            "start_request_id": None,
                            "bound_at": None,
                            "updated_at": now,
                        }},
                    )
                    raise AgentRuntimeReadinessConflict(
                        "predecessor changed before successor binding"
                    )
        try:
            await self.runs.update_one(
                {"_id": run_id},
                {"$setOnInsert": run_document},
                upsert=True,
            )
        except DuplicateKeyError:
            pass
        stored = await self.runs.find_one({"_id": run_id, "is_deleted": False})
        if stored is None:
            stored = await self.runs.find_one({
                "owner_id": owner_object_id,
                "start_request_id": str(start_request_id),
                "is_deleted": False,
            })
        if stored is None:
            raise AgentRuntimeStateConflict("bound Agent run did not become visible")
        if (
            stored.get("readiness_id") != readiness_object_id
            or stored.get("authorization_digest") != str(digest)
        ):
            raise AgentRuntimeReadinessConflict("bound run identity does not match")
        return stored

    async def get_run_owned(self, *, run_id: str, owner_id: str) -> dict[str, Any]:
        document = await self.runs.find_one({
            "_id": _required_object_id(run_id, "run_id"),
            "owner_id": _required_object_id(owner_id, "owner_id"),
            "is_deleted": False,
        })
        if document is None:
            raise NotFoundError(f"Agent runtime run '{run_id}' was not found")
        return document

    async def set_run_status(
        self,
        *,
        run_id: str,
        owner_id: str,
        expected: Sequence[str],
        status: str,
        worker_id: str,
        lease_epoch: int,
        now: datetime,
        fields: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        update = dict(fields or {})
        update.update({"status": str(status), "updated_at": now})
        document = await self.runs.find_one_and_update(
            {
                "_id": _required_object_id(run_id, "run_id"),
                "owner_id": _required_object_id(owner_id, "owner_id"),
                "status": {"$in": [str(item) for item in expected]},
                "lease.worker_id": str(worker_id),
                "lease.expires_at": {"$gt": now},
                "lease_epoch": int(lease_epoch),
                "is_deleted": False,
            },
            {"$set": update},
            return_document=ReturnDocument.AFTER,
        )
        if document is None:
            raise AgentRuntimeStateConflict("Agent run status changed concurrently")
        return document

    async def cancel_run(
        self,
        *,
        run_id: str,
        owner_id: str,
        termination: Mapping[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        """Atomically cancel a run, invalidate its lease, and freeze in-flight calls."""
        current = await self.get_run_owned(run_id=run_id, owner_id=owner_id)
        if current.get("status") in {"completed", "failed", "cancelled", "superseded"}:
            return current
        has_dispatched = any(
            isinstance(item, Mapping) and item.get("state") == "dispatched"
            for item in current.get("attempts") or []
        )
        update: dict[str, Any] = {
            "$set": {
                "status": "cancelled",
                "termination": deepcopy(dict(termination)),
                "lease": None,
                "has_uncertain_attempts": has_dispatched
                or bool(current.get("has_uncertain_attempts")),
                "updated_at": now,
            },
            "$inc": {"lease_epoch": 1},
        }
        array_filters = None
        if has_dispatched:
            update["$set"].update({
                "attempts.$[attempt].state": "uncertain",
                "attempts.$[attempt].uncertain_reason": "cancelled_while_dispatched",
                "attempts.$[attempt].uncertain_at": now,
            })
            array_filters = [{"attempt.state": "dispatched"}]
        document = await self.runs.find_one_and_update(
            {
                "_id": _required_object_id(run_id, "run_id"),
                "owner_id": _required_object_id(owner_id, "owner_id"),
                "status": {"$in": ["ready", "running", "paused"]},
                "is_deleted": False,
            },
            update,
            array_filters=array_filters,
            return_document=ReturnDocument.AFTER,
        )
        if document is not None:
            return document
        current = await self.get_run_owned(run_id=run_id, owner_id=owner_id)
        if current.get("status") == "cancelled":
            return current
        raise AgentRuntimeStateConflict("Agent run cancellation changed concurrently")

    async def reserve_call(
        self,
        *,
        run_id: str,
        owner_id: str,
        worker_id: str,
        lease_epoch: int,
        step_id: str,
        call_key: str,
        call_kind: str,
        conservative_paid_attempts: int,
        conservative_tokens: int,
        now: datetime,
    ) -> dict[str, Any]:
        """Atomically reserve a bounded planner/tool call before dispatch."""
        kind = str(call_kind)
        if kind not in {"planner", "tool"}:
            raise ValueError("call_kind must be planner or tool")
        if not str(call_key).strip():
            raise ValueError("call_key is required")
        paid_bound = int(conservative_paid_attempts)
        token_bound = int(conservative_tokens)
        if paid_bound < 0 or token_bound < 0:
            raise ValueError("conservative call bounds cannot be negative")

        run = await self.get_run_owned(run_id=run_id, owner_id=owner_id)
        existing = _attempt_by_key(run, call_key)
        if existing is not None:
            return existing
        if run.get("status") != "running":
            raise AgentRuntimeStateConflict("Agent run is not running")
        lease = run.get("lease") or {}
        if lease.get("worker_id") != str(worker_id) or lease.get("expires_at") <= now:
            raise AgentRuntimeLeaseUnavailable("Agent run lease is unavailable")
        if int(run.get("lease_epoch") or 0) != int(lease_epoch):
            raise AgentRuntimeLeaseUnavailable("Agent run lease epoch is stale")

        limits = dict((run.get("authorization") or {}).get("limits") or {})
        usage = dict(run.get("usage") or {})
        call_field = "planner_calls" if kind == "planner" else "tool_calls"
        call_limit_field = "max_planner_calls" if kind == "planner" else "max_tool_calls"
        current_calls = int(usage.get(call_field) or 0)
        if current_calls + 1 > int(limits.get(call_limit_field) or 0):
            raise AgentRuntimeBudgetExceeded(f"{kind}_call_limit")
        current_paid = int(usage.get("paid_attempts") or 0)
        reserved_paid = int(run.get("paid_attempts_reserved") or 0)
        if current_paid + reserved_paid + paid_bound > int(
            limits.get("max_paid_attempts") or 0
        ):
            raise AgentRuntimeBudgetExceeded("paid_attempt_budget")
        current_tokens = int(usage.get("total_tokens") or 0)
        reserved_tokens = int(run.get("tokens_reserved") or 0)
        if current_tokens + reserved_tokens + token_bound > int(
            limits.get("token_budget") or 0
        ):
            raise AgentRuntimeBudgetExceeded("token_budget")

        attempt_id = uuid5(CALL_NAMESPACE, f"{run_id}:{call_key}").hex
        attempt = {
            "attempt_id": attempt_id,
            "call_key": str(call_key),
            "step_id": str(step_id),
            "kind": kind,
            "state": "reserved",
            "conservative_paid_attempts": paid_bound,
            "conservative_tokens": token_bound,
            "reserved_at": now,
            "dispatched_at": None,
            "settled_at": None,
            "usage": None,
        }
        result = await self.runs.update_one(
            {
                "_id": _required_object_id(run_id, "run_id"),
                "owner_id": _required_object_id(owner_id, "owner_id"),
                "status": "running",
                "lease.worker_id": str(worker_id),
                "lease.expires_at": {"$gt": now},
                "lease_epoch": int(lease_epoch),
                "is_deleted": False,
                "attempts.call_key": {"$ne": str(call_key)},
                f"usage.{call_field}": current_calls,
                "usage.paid_attempts": current_paid,
                "paid_attempts_reserved": reserved_paid,
                "usage.total_tokens": current_tokens,
                "tokens_reserved": reserved_tokens,
            },
            {
                "$inc": {
                    f"usage.{call_field}": 1,
                    "paid_attempts_reserved": paid_bound,
                    "tokens_reserved": token_bound,
                },
                "$push": {"attempts": attempt},
                "$set": {"updated_at": now},
            },
        )
        if result.modified_count == 1:
            return attempt
        current = await self.get_run_owned(run_id=run_id, owner_id=owner_id)
        existing = _attempt_by_key(current, call_key)
        if existing is not None:
            return existing
        raise AgentRuntimeStateConflict("Agent call reservation changed concurrently")

    async def mark_call_dispatched(
        self,
        *,
        run_id: str,
        owner_id: str,
        worker_id: str,
        lease_epoch: int,
        call_key: str,
        now: datetime,
    ) -> dict[str, Any]:
        result = await self.runs.update_one(
            {
                "_id": _required_object_id(run_id, "run_id"),
                "owner_id": _required_object_id(owner_id, "owner_id"),
                "status": "running",
                "lease.worker_id": str(worker_id),
                "lease.expires_at": {"$gt": now},
                "lease_epoch": int(lease_epoch),
                "attempts": {"$elemMatch": {
                    "call_key": str(call_key),
                    "state": "reserved",
                }},
                "is_deleted": False,
            },
            {"$set": {
                "attempts.$[attempt].state": "dispatched",
                "attempts.$[attempt].dispatched_at": now,
                "updated_at": now,
            }},
            array_filters=[{"attempt.call_key": str(call_key)}],
        )
        run = await self.get_run_owned(run_id=run_id, owner_id=owner_id)
        attempt = _attempt_by_key(run, call_key)
        if attempt is None:
            raise AgentRuntimeStateConflict("Agent call reservation was not found")
        if result.modified_count != 1 and attempt.get("state") not in {
            "dispatched",
            "settled",
        }:
            raise AgentRuntimeStateConflict("Agent call could not be dispatched")
        return attempt

    async def settle_call(
        self,
        *,
        run_id: str,
        owner_id: str,
        worker_id: str,
        lease_epoch: int,
        call_key: str,
        usage: Mapping[str, Any],
        now: datetime,
        result_checkpoint: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Release a call reservation once; missing usage is charged conservatively."""
        run = await self.get_run_owned(run_id=run_id, owner_id=owner_id)
        attempt = _attempt_by_key(run, call_key)
        if attempt is None:
            raise AgentRuntimeStateConflict("Agent call reservation was not found")
        if attempt.get("state") == "settled":
            return attempt
        if attempt.get("state") != "dispatched":
            raise AgentRuntimeStateConflict("only a dispatched Agent call can settle")

        paid_bound = int(attempt.get("conservative_paid_attempts") or 0)
        token_bound = int(attempt.get("conservative_tokens") or 0)
        reported_paid = int(usage.get("paid_attempts") or 0)
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        reported_total = int(usage.get("total_tokens") or 0)
        if min(reported_paid, input_tokens, output_tokens, reported_total) < 0:
            raise ValueError("Agent call usage cannot be negative")
        paid = reported_paid if reported_paid > 0 else paid_bound
        observed_tokens = max(reported_total, input_tokens + output_tokens)
        charged_tokens = observed_tokens if observed_tokens > 0 else token_bound
        if paid > paid_bound or charged_tokens > token_bound:
            raise AgentRuntimeStateConflict("Agent call exceeded its conservative bound")
        charged_usage = {
            "paid_attempts": paid,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": charged_tokens,
        }
        checkpoint = (
            deepcopy(dict(result_checkpoint))
            if result_checkpoint is not None
            else None
        )
        result = await self.runs.update_one(
            {
                "_id": _required_object_id(run_id, "run_id"),
                "owner_id": _required_object_id(owner_id, "owner_id"),
                "status": "running",
                "lease.worker_id": str(worker_id),
                "lease.expires_at": {"$gt": now},
                "lease_epoch": int(lease_epoch),
                "attempts": {"$elemMatch": {
                    "call_key": str(call_key),
                    "state": "dispatched",
                }},
                "paid_attempts_reserved": {"$gte": paid_bound},
                "tokens_reserved": {"$gte": token_bound},
                "is_deleted": False,
            },
            {
                "$inc": {
                    "paid_attempts_reserved": -paid_bound,
                    "tokens_reserved": -token_bound,
                    "usage.paid_attempts": paid,
                    "usage.input_tokens": input_tokens,
                    "usage.output_tokens": output_tokens,
                    "usage.total_tokens": charged_tokens,
                },
                "$set": {
                    "attempts.$[attempt].state": "settled",
                    "attempts.$[attempt].usage": charged_usage,
                    "attempts.$[attempt].result_checkpoint": checkpoint,
                    "attempts.$[attempt].settled_at": now,
                    "updated_at": now,
                },
            },
            array_filters=[{"attempt.call_key": str(call_key)}],
        )
        if result.modified_count != 1:
            current = await self.get_run_owned(run_id=run_id, owner_id=owner_id)
            settled = _attempt_by_key(current, call_key)
            if settled is not None and settled.get("state") == "settled":
                return settled
            raise AgentRuntimeStateConflict("Agent call settlement changed concurrently")
        current = await self.get_run_owned(run_id=run_id, owner_id=owner_id)
        settled = _attempt_by_key(current, call_key)
        if settled is None:
            raise AgentRuntimeStateConflict("settled Agent call was not found")
        return settled

    async def release_call_pre_dispatch(
        self,
        *,
        run_id: str,
        owner_id: str,
        worker_id: str,
        lease_epoch: int,
        call_key: str,
        reason: str,
        now: datetime,
    ) -> bool:
        """Release only a call proven not to have crossed the adapter boundary."""
        run = await self.get_run_owned(run_id=run_id, owner_id=owner_id)
        attempt = _attempt_by_key(run, call_key)
        if attempt is None:
            raise AgentRuntimeStateConflict("Agent call reservation was not found")
        if attempt.get("state") == "released_pre_dispatch":
            return True
        if attempt.get("state") != "reserved":
            return False
        kind = str(attempt.get("kind"))
        if kind not in {"planner", "tool"}:
            raise AgentRuntimeStateConflict("Agent call kind is invalid")
        call_field = "planner_calls" if kind == "planner" else "tool_calls"
        paid_bound = int(attempt.get("conservative_paid_attempts") or 0)
        token_bound = int(attempt.get("conservative_tokens") or 0)
        result = await self.runs.update_one(
            {
                "_id": _required_object_id(run_id, "run_id"),
                "owner_id": _required_object_id(owner_id, "owner_id"),
                "status": "running",
                "lease.worker_id": str(worker_id),
                "lease.expires_at": {"$gt": now},
                "lease_epoch": int(lease_epoch),
                "attempts": {"$elemMatch": {
                    "call_key": str(call_key),
                    "state": "reserved",
                }},
                f"usage.{call_field}": {"$gte": 1},
                "paid_attempts_reserved": {"$gte": paid_bound},
                "tokens_reserved": {"$gte": token_bound},
                "is_deleted": False,
            },
            {
                "$inc": {
                    f"usage.{call_field}": -1,
                    "paid_attempts_reserved": -paid_bound,
                    "tokens_reserved": -token_bound,
                },
                "$set": {
                    "attempts.$[attempt].state": "released_pre_dispatch",
                    "attempts.$[attempt].release_reason": str(reason),
                    "attempts.$[attempt].released_at": now,
                    "updated_at": now,
                },
            },
            array_filters=[{"attempt.call_key": str(call_key)}],
        )
        return result.modified_count == 1

    async def mark_call_uncertain(
        self,
        *,
        run_id: str,
        owner_id: str,
        worker_id: str,
        lease_epoch: int,
        call_key: str,
        reason: str,
        now: datetime,
    ) -> bool:
        """Freeze a dispatched call reservation when its outcome is unknown."""
        result = await self.runs.update_one(
            {
                "_id": _required_object_id(run_id, "run_id"),
                "owner_id": _required_object_id(owner_id, "owner_id"),
                "status": "running",
                "lease.worker_id": str(worker_id),
                "lease.expires_at": {"$gt": now},
                "lease_epoch": int(lease_epoch),
                "attempts": {"$elemMatch": {
                    "call_key": str(call_key),
                    "state": "dispatched",
                }},
                "is_deleted": False,
            },
            {
                "$set": {
                    "attempts.$[attempt].state": "uncertain",
                    "attempts.$[attempt].uncertain_reason": str(reason),
                    "attempts.$[attempt].uncertain_at": now,
                    "has_uncertain_attempts": True,
                    "updated_at": now,
                }
            },
            array_filters=[{"attempt.call_key": str(call_key)}],
        )
        if result.modified_count == 1:
            return True
        run = await self.get_run_owned(run_id=run_id, owner_id=owner_id)
        attempt = _attempt_by_key(run, call_key)
        return bool(attempt and attempt.get("state") == "uncertain")

    async def resolve_uncertain_call(
        self,
        *,
        run_id: str,
        owner_id: str,
        call_key: str,
        action: str,
        now: datetime,
    ) -> dict[str, Any]:
        """Charge a frozen unknown attempt once after explicit user resolution."""
        if action not in {"retry", "skip"}:
            raise ValueError("uncertain action must be retry or skip")
        run = await self.get_run_owned(run_id=run_id, owner_id=owner_id)
        attempt = _attempt_by_key(run, call_key)
        if attempt is None:
            raise AgentRuntimeStateConflict("uncertain Agent call was not found")
        target_state = f"resolved_{action}"
        if attempt.get("state") == target_state:
            return attempt
        if attempt.get("state") != "uncertain":
            raise AgentRuntimeStateConflict("Agent call is not uncertain")
        paid_bound = int(attempt.get("conservative_paid_attempts") or 0)
        token_bound = int(attempt.get("conservative_tokens") or 0)
        charged_usage = {
            "paid_attempts": paid_bound,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": token_bound,
        }
        result = await self.runs.update_one(
            {
                "_id": _required_object_id(run_id, "run_id"),
                "owner_id": _required_object_id(owner_id, "owner_id"),
                "status": "paused",
                "attempts": {"$elemMatch": {
                    "call_key": str(call_key),
                    "state": "uncertain",
                }},
                "paid_attempts_reserved": {"$gte": paid_bound},
                "tokens_reserved": {"$gte": token_bound},
                "is_deleted": False,
            },
            {
                "$inc": {
                    "paid_attempts_reserved": -paid_bound,
                    "tokens_reserved": -token_bound,
                    "usage.paid_attempts": paid_bound,
                    "usage.total_tokens": token_bound,
                },
                "$set": {
                    "attempts.$[attempt].state": target_state,
                    "attempts.$[attempt].usage": charged_usage,
                    "attempts.$[attempt].resolved_at": now,
                    "attempts.$[attempt].resolution_action": action,
                    "has_uncertain_attempts": False,
                    "updated_at": now,
                },
            },
            array_filters=[{"attempt.call_key": str(call_key)}],
        )
        if result.modified_count != 1:
            current = await self.get_run_owned(run_id=run_id, owner_id=owner_id)
            resolved = _attempt_by_key(current, call_key)
            if resolved is not None and resolved.get("state") == target_state:
                return resolved
            raise AgentRuntimeStateConflict("uncertain Agent call resolution changed")
        current = await self.get_run_owned(run_id=run_id, owner_id=owner_id)
        resolved = _attempt_by_key(current, call_key)
        if resolved is None:
            raise AgentRuntimeStateConflict("resolved Agent call was not found")
        return resolved

    async def acquire_lease(
        self,
        *,
        run_id: str,
        owner_id: str,
        worker_id: str,
        now: datetime,
        expires_at: datetime,
    ) -> dict[str, Any]:
        if expires_at <= now:
            raise ValueError("lease expiry must be in the future")
        document = await self.runs.find_one_and_update(
            {
                "_id": _required_object_id(run_id, "run_id"),
                "owner_id": _required_object_id(owner_id, "owner_id"),
                "status": {"$in": ["ready", "running", "paused"]},
                "is_deleted": False,
                "$or": [
                    {"lease": None},
                    {"lease": {"$exists": False}},
                    {"lease.expires_at": {"$lte": now}},
                    {"lease.worker_id": str(worker_id)},
                ],
            },
            {
                "$set": {
                    "lease": {
                        "worker_id": str(worker_id),
                        "heartbeat_at": now,
                        "expires_at": expires_at,
                    },
                    "updated_at": now,
                },
                "$inc": {"lease_epoch": 1},
            },
            return_document=ReturnDocument.AFTER,
        )
        if document is None:
            raise AgentRuntimeLeaseUnavailable("Agent run has another active lease")
        return document

    async def heartbeat_lease(
        self,
        *,
        run_id: str,
        owner_id: str,
        worker_id: str,
        lease_epoch: int,
        now: datetime,
        expires_at: datetime,
    ) -> bool:
        result = await self.runs.update_one(
            {
                "_id": _required_object_id(run_id, "run_id"),
                "owner_id": _required_object_id(owner_id, "owner_id"),
                "status": "running",
                "lease.worker_id": str(worker_id),
                "lease.expires_at": {"$gt": now},
                "lease_epoch": int(lease_epoch),
                "is_deleted": False,
            },
            {"$set": {
                "lease.heartbeat_at": now,
                "lease.expires_at": expires_at,
                "updated_at": now,
            }},
        )
        return result.matched_count == 1

    async def release_lease(
        self,
        *,
        run_id: str,
        owner_id: str,
        worker_id: str,
        lease_epoch: int,
        now: datetime,
    ) -> bool:
        result = await self.runs.update_one(
            {
                "_id": _required_object_id(run_id, "run_id"),
                "owner_id": _required_object_id(owner_id, "owner_id"),
                "lease.worker_id": str(worker_id),
                "lease_epoch": int(lease_epoch),
                "is_deleted": False,
            },
            {"$set": {"lease": None, "updated_at": now}},
        )
        return result.modified_count == 1

    async def claim_step(
        self,
        *,
        run_id: str,
        owner_id: str,
        worker_id: str,
        lease_epoch: int,
        now: datetime,
        observation_cursor: int,
        observation_digest: str,
    ) -> dict[str, Any]:
        run_object_id = _required_object_id(run_id, "run_id")
        owner_object_id = _required_object_id(owner_id, "owner_id")
        run = await self.get_run_owned(run_id=run_id, owner_id=owner_id)
        seed = deepcopy(run.get("active_step_seed"))
        if run.get("active_step_id") and isinstance(seed, Mapping):
            step = await self._upsert_step_from_seed(run, dict(seed))
            if step.get("status") not in {"completed", "failed"}:
                adopted = await self.steps.find_one_and_update(
                    {
                        "_id": str(step["step_id"]),
                        "run_id": run_object_id,
                        "status": {"$nin": ["completed", "failed"]},
                        "is_deleted": False,
                    },
                    {"$set": {"lease_epoch": int(lease_epoch), "updated_at": now}},
                    return_document=ReturnDocument.AFTER,
                )
                if adopted is None:
                    raise AgentRuntimeStateConflict(
                        "active Agent step could not adopt the current lease"
                    )
                return adopted
            return step

        ordinal = int(run.get("next_ordinal") or 0)
        step_id = uuid5(STEP_NAMESPACE, f"{run_id}:{ordinal}").hex
        seed = {
            "step_id": step_id,
            "ordinal": ordinal,
            "observation_cursor": max(0, int(observation_cursor)),
            "observation_digest": str(observation_digest),
            "created_at": now,
        }
        claimed = await self.runs.find_one_and_update(
            {
                "_id": run_object_id,
                "owner_id": owner_object_id,
                "status": "running",
                "active_step_id": None,
                "next_ordinal": ordinal,
                "lease.worker_id": str(worker_id),
                "lease.expires_at": {"$gt": now},
                "lease_epoch": int(lease_epoch),
                "is_deleted": False,
            },
            {
                "$set": {
                    "active_step_id": step_id,
                    "active_step_seed": seed,
                    "updated_at": now,
                },
                "$inc": {"next_ordinal": 1},
            },
            return_document=ReturnDocument.AFTER,
        )
        if claimed is None:
            current = await self.get_run_owned(run_id=run_id, owner_id=owner_id)
            current_seed = current.get("active_step_seed")
            if current.get("active_step_id") and isinstance(current_seed, Mapping):
                return await self._upsert_step_from_seed(current, dict(current_seed))
            raise AgentRuntimeStateConflict("Agent step could not be claimed")
        step = await self._upsert_step_from_seed(claimed, seed)
        await self.steps.update_one(
            {"_id": str(step["step_id"]), "is_deleted": False},
            {"$set": {"lease_epoch": int(lease_epoch), "updated_at": now}},
        )
        return await self.get_step_owned(
            run_id=run_id,
            owner_id=owner_id,
            step_id=str(step["step_id"]),
        )

    async def _upsert_step_from_seed(
        self,
        run: Mapping[str, Any],
        seed: Mapping[str, Any],
    ) -> dict[str, Any]:
        step_id = str(seed["step_id"])
        created_at = seed.get("created_at") or get_utc_now()
        document = {
            "_id": step_id,
            "step_id": step_id,
            "run_id": run["_id"],
            "owner_id": run["owner_id"],
            "novel_id": run["novel_id"],
            "schema_version": "agent_runtime_step.v1",
            "ordinal": int(seed["ordinal"]),
            "status": "planning",
            "input_observation_cursor": int(seed.get("observation_cursor") or 0),
            "input_observation_digest": str(seed.get("observation_digest") or ""),
            "planner_decision": None,
            "policy_decision": None,
            "tool_invocation": None,
            "observation": None,
            "usage_delta": {},
            "revision_before": run.get("expected_narrative_revision"),
            "revision_after": run.get("expected_narrative_revision"),
            "lease_epoch": int(run.get("lease_epoch") or 0),
            "created_at": created_at,
            "updated_at": created_at,
            "is_deleted": False,
            "deleted_at": None,
        }
        await self.steps.update_one(
            {"_id": step_id},
            {"$setOnInsert": document},
            upsert=True,
        )
        stored = await self.steps.find_one({"_id": step_id, "is_deleted": False})
        if stored is None:
            raise AgentRuntimeStateConflict("claimed Agent step did not become visible")
        return stored

    async def transition_step(
        self,
        *,
        run_id: str,
        owner_id: str,
        worker_id: str,
        lease_epoch: int,
        step_id: str,
        expected: str,
        status: str,
        fields: Mapping[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        allowed = STEP_TRANSITIONS.get(str(expected))
        if allowed is None or str(status) not in allowed:
            raise ValueError(f"invalid Agent step transition: {expected} -> {status}")
        forbidden = {"_id", "step_id", "run_id", "owner_id", "novel_id", "ordinal", "status"}
        overlap = forbidden & set(fields)
        if overlap:
            raise ValueError(f"Agent step identity fields are immutable: {sorted(overlap)}")
        update = deepcopy(dict(fields))
        update.update({"status": str(status), "updated_at": now})
        lease = await self.runs.find_one({
            "_id": _required_object_id(run_id, "run_id"),
            "owner_id": _required_object_id(owner_id, "owner_id"),
            "status": {"$in": ["running", "paused"]},
            "lease.worker_id": str(worker_id),
            "lease.expires_at": {"$gt": now},
            "lease_epoch": int(lease_epoch),
            "is_deleted": False,
        })
        if lease is None:
            raise AgentRuntimeStateConflict("Agent step lease fence is stale")
        if str(expected) == "paused":
            await self.steps.update_one(
                {
                    "_id": str(step_id),
                    "run_id": _required_object_id(run_id, "run_id"),
                    "status": "paused",
                    "is_deleted": False,
                },
                {"$set": {"lease_epoch": int(lease_epoch), "updated_at": now}},
            )
        document = await self.steps.find_one_and_update(
            {
                "_id": str(step_id),
                "run_id": _required_object_id(run_id, "run_id"),
                "status": str(expected),
                "lease_epoch": int(lease_epoch),
                "is_deleted": False,
            },
            {"$set": update},
            return_document=ReturnDocument.AFTER,
        )
        if document is None:
            raise AgentRuntimeStateConflict("Agent step status changed concurrently")
        return document

    async def clear_active_step(
        self,
        *,
        run_id: str,
        owner_id: str,
        worker_id: str,
        lease_epoch: int,
        step_id: str,
        now: datetime,
    ) -> bool:
        terminal = await self.steps.find_one({
            "_id": str(step_id),
            "run_id": _required_object_id(run_id, "run_id"),
            "status": {"$in": ["completed", "failed"]},
            "is_deleted": False,
        })
        if terminal is None:
            raise AgentRuntimeStateConflict("active Agent step is not terminal")
        result = await self.runs.update_one(
            {
                "_id": _required_object_id(run_id, "run_id"),
                "owner_id": _required_object_id(owner_id, "owner_id"),
                "active_step_id": str(step_id),
                "status": {"$in": ["running", "paused"]},
                "lease.worker_id": str(worker_id),
                "lease.expires_at": {"$gt": now},
                "lease_epoch": int(lease_epoch),
                "is_deleted": False,
            },
            {"$set": {
                "active_step_id": None,
                "active_step_seed": None,
                "updated_at": now,
            }},
        )
        return result.modified_count == 1

    async def append_event(
        self,
        *,
        run_id: str,
        event_key: str,
        event_type: str,
        payload: Mapping[str, Any],
        now: datetime,
        step_id: str | None = None,
    ) -> dict[str, Any]:
        if not str(event_key).strip():
            raise ValueError("event_key is required")
        normalized_event_key = str(event_key)
        normalized_event_type = str(event_type)
        normalized_step_id = str(step_id) if step_id else None
        projected = project_agent_runtime_event_payload(
            normalized_event_type,
            payload,
        )
        encoded = json.dumps(
            projected,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        if len(encoded) > MAX_EVENT_PAYLOAD_BYTES:
            raise ValueError("Agent event payload exceeds the audit boundary")

        run_object_id = _required_object_id(run_id, "run_id")
        event_id = uuid5(EVENT_NAMESPACE, f"{run_id}:{normalized_event_key}").hex
        existing = await self.events.find_one({"_id": event_id})
        if existing is not None:
            if _event_matches(
                existing,
                run_id=run_object_id,
                event_key=normalized_event_key,
                event_type=normalized_event_type,
                step_id=normalized_step_id,
                payload=projected,
            ):
                return existing
            raise AgentRuntimeStateConflict(
                "Agent event key was replayed with different content"
            )
        run = await self.runs.find_one_and_update(
            {"_id": run_object_id, "is_deleted": False},
            {
                "$inc": {"next_event_sequence": 1},
                "$set": {"updated_at": now},
            },
            return_document=ReturnDocument.AFTER,
        )
        if run is None:
            raise NotFoundError(f"Agent runtime run '{run_id}' was not found")
        sequence = int(run.get("next_event_sequence") or 0)
        document = {
            "_id": event_id,
            "event_id": event_id,
            "run_id": run_object_id,
            "owner_id": run["owner_id"],
            "novel_id": run["novel_id"],
            "sequence": sequence,
            "event_key": normalized_event_key,
            "type": normalized_event_type,
            "step_id": normalized_step_id,
            "schema_version": "agent_runtime_event.v1",
            "payload": projected,
            "created_at": now,
            "is_deleted": False,
            "deleted_at": None,
        }
        try:
            await self.events.insert_one(document)
        except DuplicateKeyError:
            existing = await self.events.find_one({"_id": event_id})
            if existing is not None and _event_matches(
                existing,
                run_id=run_object_id,
                event_key=normalized_event_key,
                event_type=normalized_event_type,
                step_id=normalized_step_id,
                payload=projected,
            ):
                return existing
            if existing is not None:
                raise AgentRuntimeStateConflict(
                    "Agent event key was replayed with different content"
                )
            raise
        return document

    async def list_steps_owned(
        self,
        *,
        run_id: str,
        owner_id: str,
    ) -> list[dict[str, Any]]:
        run = await self.get_run_owned(run_id=run_id, owner_id=owner_id)
        cursor = self.steps.find({
            "run_id": run["_id"],
            "owner_id": run["owner_id"],
            "is_deleted": False,
        }).sort("ordinal", 1)
        return await cursor.to_list(length=None)

    async def get_step_owned(
        self,
        *,
        run_id: str,
        owner_id: str,
        step_id: str,
    ) -> dict[str, Any]:
        run = await self.get_run_owned(run_id=run_id, owner_id=owner_id)
        document = await self.steps.find_one({
            "_id": str(step_id),
            "run_id": run["_id"],
            "owner_id": run["owner_id"],
            "is_deleted": False,
        })
        if document is None:
            raise NotFoundError(f"Agent runtime step '{step_id}' was not found")
        return document

    async def list_events_owned(
        self,
        *,
        run_id: str,
        owner_id: str,
    ) -> list[dict[str, Any]]:
        run = await self.get_run_owned(run_id=run_id, owner_id=owner_id)
        cursor = self.events.find({
            "run_id": run["_id"],
            "owner_id": run["owner_id"],
            "is_deleted": False,
        }).sort("sequence", 1)
        return await cursor.to_list(length=None)

    async def list_runs_owned(self, *, owner_id: str) -> list[dict[str, Any]]:
        cursor = self.runs.find({
            "owner_id": _required_object_id(owner_id, "owner_id"),
            "is_deleted": False,
        }).sort([("created_at", -1), ("_id", -1)])
        return await cursor.to_list(length=None)


agent_runtime_repository = AgentRuntimeRepository()
