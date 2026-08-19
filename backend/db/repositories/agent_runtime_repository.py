"""Persistence boundary for bounded Agent Runtime readiness, runs, steps, and events."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
from typing import Any, Mapping, Sequence
from uuid import UUID, uuid5

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from backend.db import collections
from backend.db.errors import NotFoundError
from backend.db.mongo import get_database
from backend.db.utils import get_utc_now, to_object_id


STEP_NAMESPACE = UUID("e0dc1a47-9a48-4cb5-a165-2259c139fbb3")
EVENT_NAMESPACE = UUID("814b1e75-93a6-49da-903c-03de77a202c6")
CALL_NAMESPACE = UUID("73998e71-c0cf-4a99-91a8-8a01ad636aa9")
MAX_EVENT_PAYLOAD_BYTES = 16_384
SENSITIVE_EVENT_FIELDS = frozenset({
    "api_key",
    "arguments",
    "content",
    "data",
    "password",
    "prompt",
    "raw_prompt",
    "request",
    "result",
    "secret",
    "text",
})

STEP_TRANSITIONS: dict[str, frozenset[str]] = {
    "planning": frozenset({"policy_checked", "paused", "failed"}),
    "policy_checked": frozenset({"executing", "observed", "paused", "failed"}),
    "executing": frozenset({"observed", "paused", "failed"}),
    "observed": frozenset({"completed", "paused", "failed"}),
    "paused": frozenset({"planning", "policy_checked", "executing", "failed"}),
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


def _validate_event_payload(value: Any, *, path: str = "payload") -> None:
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key)
            if key.lower() in SENSITIVE_EVENT_FIELDS:
                raise ValueError(f"sensitive event field is forbidden: {path}.{key}")
            _validate_event_payload(item, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_event_payload(item, path=f"{path}[{index}]")


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
            "successor_run_id": None,
            "created_at": now,
            "updated_at": now,
            "is_deleted": False,
            "deleted_at": None,
        }
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
                "is_deleted": False,
            },
            {"$set": update},
            return_document=ReturnDocument.AFTER,
        )
        if document is None:
            raise AgentRuntimeStateConflict("Agent run status changed concurrently")
        return document

    async def reserve_call(
        self,
        *,
        run_id: str,
        owner_id: str,
        worker_id: str,
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
        call_key: str,
        usage: Mapping[str, Any],
        now: datetime,
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
        result = await self.runs.update_one(
            {
                "_id": _required_object_id(run_id, "run_id"),
                "owner_id": _required_object_id(owner_id, "owner_id"),
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
        now: datetime,
        expires_at: datetime,
    ) -> bool:
        result = await self.runs.update_one(
            {
                "_id": _required_object_id(run_id, "run_id"),
                "owner_id": _required_object_id(owner_id, "owner_id"),
                "lease.worker_id": str(worker_id),
                "lease.expires_at": {"$gt": now},
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
        now: datetime,
    ) -> bool:
        result = await self.runs.update_one(
            {
                "_id": _required_object_id(run_id, "run_id"),
                "owner_id": _required_object_id(owner_id, "owner_id"),
                "lease.worker_id": str(worker_id),
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
        now: datetime,
        observation_cursor: int,
        observation_digest: str,
    ) -> dict[str, Any]:
        run_object_id = _required_object_id(run_id, "run_id")
        owner_object_id = _required_object_id(owner_id, "owner_id")
        run = await self.get_run_owned(run_id=run_id, owner_id=owner_id)
        seed = deepcopy(run.get("active_step_seed"))
        if run.get("active_step_id") and isinstance(seed, Mapping):
            return await self._upsert_step_from_seed(run, dict(seed))

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
        return await self._upsert_step_from_seed(claimed, seed)

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
        document = await self.steps.find_one_and_update(
            {
                "_id": str(step_id),
                "run_id": _required_object_id(run_id, "run_id"),
                "status": str(expected),
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
                "lease.worker_id": str(worker_id),
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
        projected = deepcopy(dict(payload))
        _validate_event_payload(projected)
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
        event_id = uuid5(EVENT_NAMESPACE, f"{run_id}:{event_key}").hex
        existing = await self.events.find_one({"_id": event_id})
        if existing is not None:
            return existing
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
            "event_key": str(event_key),
            "type": str(event_type),
            "step_id": str(step_id) if step_id else None,
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
            if existing is not None:
                return existing
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


agent_runtime_repository = AgentRuntimeRepository()
