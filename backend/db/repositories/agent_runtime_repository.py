"""Persistence boundary for bounded Agent Runtime readiness, runs, steps, and events."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
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
SUCCESSOR_HANDOFF_TTL_SECONDS = 30
# Per step: 20 planner repairs + the accepted plan, 20 tool retries + the
# accepted result, and 20 pre-dispatch releases for each adapter kind.
MAX_ATTEMPT_LEDGER_ENTRIES_PER_STEP = 82


class _EventPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _AttemptLedgerUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    paid_attempts: int = Field(ge=0, le=10_000)
    input_tokens: int = Field(ge=0, le=1_000_000_000)
    output_tokens: int = Field(ge=0, le=1_000_000_000)
    total_tokens: int = Field(ge=0, le=1_000_000_000)


class _AttemptLedgerEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["agent_runtime_attempt_ledger.v1"] = (
        "agent_runtime_attempt_ledger.v1"
    )
    call_key: str = Field(min_length=1, max_length=240)
    kind: Literal["planner", "tool"]
    state: Literal[
        "settled",
        "released_pre_dispatch",
        "resolved_retry",
        "resolved_skip",
    ]
    conservative_paid_attempts: int = Field(ge=0, le=10_000)
    conservative_tokens: int = Field(ge=0, le=1_000_000_000)
    usage: _AttemptLedgerUsage | None = None
    release_reason: str | None = Field(default=None, min_length=1, max_length=160)
    uncertain_reason: str | None = Field(default=None, min_length=1, max_length=160)
    resolution_action: Literal["retry", "skip"] | None = None

    @model_validator(mode="after")
    def validate_state_projection(self) -> "_AttemptLedgerEntry":
        if self.state == "settled":
            if self.usage is None or any((
                self.release_reason,
                self.resolution_action,
            )):
                raise ValueError("settled attempt ledger projection is invalid")
        elif self.state == "released_pre_dispatch":
            if self.release_reason is None or self.usage is not None:
                raise ValueError("released attempt ledger projection is invalid")
        else:
            expected_action = "retry" if self.state == "resolved_retry" else "skip"
            if (
                self.usage is None
                or self.uncertain_reason is None
                or self.resolution_action != expected_action
            ):
                raise ValueError("resolved attempt ledger projection is invalid")
        return self


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
    "planning": frozenset({"policy_checked", "completed", "paused", "failed"}),
    "policy_checked": frozenset({
        "executing",
        "observed",
        "paused",
        "failed",
    }),
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


class AgentRuntimeCheckpointPending(AgentRuntimeStateConflict):
    """A step checkpoint must be projected before cancellation can proceed."""


class AgentRuntimeBudgetExceeded(ValueError):
    """A conservative call reservation would exceed the authorization."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = str(reason_code)
        super().__init__(self.reason_code)


@dataclass(frozen=True, slots=True)
class AgentRuntimeBinding:
    run: dict[str, Any]
    replayed: bool


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


def project_agent_runtime_attempt_ledger_entry(
    attempt: Mapping[str, Any],
) -> dict[str, Any]:
    """Project one terminal call attempt into its bounded audit record."""
    try:
        candidate = {
            "schema_version": "agent_runtime_attempt_ledger.v1",
            "call_key": str(attempt.get("call_key") or ""),
            "kind": attempt.get("kind"),
            "state": attempt.get("state"),
            "conservative_paid_attempts": int(
                attempt.get("conservative_paid_attempts") or 0
            ),
            "conservative_tokens": int(
                attempt.get("conservative_tokens") or 0
            ),
            "usage": deepcopy(attempt.get("usage")),
            "release_reason": attempt.get("release_reason"),
            "uncertain_reason": attempt.get("uncertain_reason"),
            "resolution_action": attempt.get("resolution_action"),
        }
        return _AttemptLedgerEntry.model_validate(candidate).model_dump(mode="json")
    except (TypeError, ValueError, ValidationError) as exc:
        raise ValueError("Agent attempt cannot enter the terminal ledger") from exc


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


def _step_checkpoint_needs_projection(
    run: Mapping[str, Any],
    step: Mapping[str, Any] | None,
) -> bool:
    """Return whether cancellation must preserve and project this checkpoint."""
    if not isinstance(step, Mapping):
        return False
    status = str(step.get("status") or "")
    if status == "paused":
        termination = dict(run.get("termination") or {})
        return not (
            run.get("status") == "paused"
            and termination.get("status") == "paused"
            and termination.get("reason_code") == step.get("pause_reason")
        )
    if status == "failed":
        return True
    return bool(
        status == "completed"
        and (step.get("planner_decision") or {}).get("kind") == "propose_finish"
        and (
            ((step.get("observation") or {}).get("completion") or {}).get(
                "satisfied"
            )
            is True
        )
    )


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
            "ttl_expires_at": expires_at,
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
    ) -> AgentRuntimeBinding:
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
            return AgentRuntimeBinding(run=existing, replayed=True)

        proposed_run_id = ObjectId()
        try:
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
                    },
                    "$unset": {"ttl_expires_at": ""},
                },
                return_document=ReturnDocument.AFTER,
            )
        except DuplicateKeyError as exc:
            raise AgentRuntimeReadinessConflict(
                "start_request_id already belongs to another readiness"
            ) from exc
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
            "termination_event_key": None,
            "predecessor_run_id": predecessor_object_id,
            "replay_of_run_id": replay_object_id,
            "lineage_root_run_id": lineage_root_object_id,
            "successor_run_id": None,
            "created_at": now,
            "updated_at": now,
            "is_deleted": False,
            "deleted_at": None,
        }
        if replay_object_id is not None:
            replay_source = await self.runs.find_one({
                "_id": replay_object_id,
                "owner_id": owner_object_id,
                "novel_id": novel_object_id,
                "status": {"$in": ["completed", "failed", "cancelled"]},
                "has_uncertain_attempts": {"$ne": True},
                "authorization.goal": authorization.get("goal"),
                "authorization.scope": authorization.get("scope"),
                "is_deleted": False,
                "$or": [
                    {"lease": None},
                    {"lease": {"$exists": False}},
                    {"lease.expires_at": {"$lte": now}},
                ],
            })
            if replay_source is None:
                raise AgentRuntimeReadinessConflict(
                    "replay source changed before readiness binding"
                )

        if predecessor_object_id is not None:
            if not isinstance(lineage, Mapping):
                raise AgentRuntimeReadinessConflict(
                    "predecessor readiness is missing its lineage checkpoint"
                )
            try:
                predecessor_epoch = int(lineage["predecessor_lease_epoch"])
            except (KeyError, TypeError, ValueError):
                raise AgentRuntimeReadinessConflict(
                    "predecessor readiness has an invalid lineage checkpoint"
                )
            predecessor_snapshot = await self.runs.find_one({
                "_id": predecessor_object_id,
                "owner_id": owner_object_id,
                "novel_id": novel_object_id,
                "authorization.goal": authorization.get("goal"),
                "authorization.scope": authorization.get("scope"),
                "is_deleted": False,
            })
            already_superseded = bool(
                predecessor_snapshot
                and predecessor_snapshot.get("status") == "superseded"
                and predecessor_snapshot.get("successor_run_id") == run_id
                and int(predecessor_snapshot.get("lease_epoch") or 0)
                == predecessor_epoch + 1
            )
            if predecessor_snapshot is None or (
                (
                    predecessor_snapshot.get("status") != "paused"
                    or int(predecessor_snapshot.get("lease_epoch") or 0)
                    != predecessor_epoch
                )
                and not already_superseded
            ):
                raise AgentRuntimeReadinessConflict(
                    "predecessor changed before successor binding"
                )
            predecessor_termination = dict(
                predecessor_snapshot.get("termination") or {}
            )
            predecessor_reason = str(
                predecessor_termination.get("reason_code") or ""
            )
            if not already_superseded and (
                predecessor_termination.get("status") != "paused"
                or predecessor_reason == "uncertain_paid_attempt"
                or any(
                    isinstance(item, Mapping)
                    and item.get("state") in {"dispatched", "uncertain"}
                    for item in predecessor_snapshot.get("attempts") or []
                )
            ):
                raise AgentRuntimeReadinessConflict(
                    "predecessor paused checkpoint is not canonical"
                )
            if not already_superseded:
                pause_event_key = str(
                    predecessor_snapshot.get("termination_event_key") or ""
                )
                expected_pause_event_key = str(
                    lineage.get("predecessor_pause_event_key")
                    or f"run-paused-{predecessor_reason}-{predecessor_epoch}"
                )
                pause_step_id = (
                    str(predecessor_termination["step_id"])
                    if predecessor_termination.get("step_id")
                    else None
                )
                pause_payload = project_agent_runtime_event_payload(
                    "run_paused",
                    {
                        "status": "paused",
                        "reason_code": predecessor_reason,
                    },
                )
                pause_event = (
                    await self.events.find_one({
                        "run_id": predecessor_object_id,
                        "event_key": pause_event_key,
                        "is_deleted": False,
                    })
                    if pause_event_key
                    else None
                )
                if pause_event is None:
                    raise AgentRuntimeCheckpointPending(
                        "predecessor pause event must be repaired before binding"
                    )
                if (
                    pause_event_key != expected_pause_event_key
                    or not _event_matches(
                        pause_event,
                        run_id=predecessor_object_id,
                        event_key=pause_event_key,
                        event_type="run_paused",
                        step_id=pause_step_id,
                        payload=pause_payload,
                    )
                ):
                    raise AgentRuntimeReadinessConflict(
                        "predecessor pause event does not match its checkpoint"
                    )

            predecessor = predecessor_snapshot if already_superseded else None
            fenced_step: dict[str, Any] | None = None
            successor_epoch = predecessor_epoch + 1
            predecessor_step_id = predecessor_snapshot.get("active_step_id")
            fence_worker_id = f"successor:{run_id}"
            if not already_superseded and predecessor_step_id:
                try:
                    fenced_step = await self._adopt_step_lease(
                        run=predecessor_snapshot,
                        step_id=str(predecessor_step_id),
                        worker_id=fence_worker_id,
                        expected_lease_epoch=predecessor_epoch,
                        lease_epoch=successor_epoch,
                        now=now,
                        expires_at=now + timedelta(
                            seconds=SUCCESSOR_HANDOFF_TTL_SECONDS
                        ),
                    )
                except AgentRuntimeStateConflict as exc:
                    raise AgentRuntimeReadinessConflict(
                        "predecessor has an active resume during successor binding"
                    ) from exc
                if not (
                    fenced_step.get("status") == "paused"
                    and fenced_step.get("pause_reason") == predecessor_reason
                    and fenced_step.get("paused_from_status")
                    in {"planning", "policy_checked", "executing", "observed"}
                ):
                    if fenced_step.get("status") not in {"completed", "failed"}:
                        await self._rollback_step_handoff(
                            run=predecessor_snapshot,
                            step_id=str(predecessor_step_id),
                            worker_id=fence_worker_id,
                            lease_epoch=successor_epoch,
                            previous_epoch=predecessor_epoch,
                            now=now,
                        )
                    raise AgentRuntimeReadinessConflict(
                        "predecessor paused checkpoint is not canonical"
                    )
            elif (
                not already_superseded
                and predecessor_reason != "concurrent_narrative_change"
            ):
                raise AgentRuntimeReadinessConflict(
                    "predecessor paused checkpoint is not canonical"
                )

            if not already_superseded:
                predecessor = await self.runs.find_one_and_update(
                    {
                        "_id": predecessor_object_id,
                        "owner_id": owner_object_id,
                        "novel_id": novel_object_id,
                        "status": "paused",
                        "successor_run_id": None,
                        "active_step_id": predecessor_step_id,
                        "lease_epoch": predecessor_epoch,
                        "has_uncertain_attempts": {"$ne": True},
                        "attempts.state": {"$nin": ["dispatched", "uncertain"]},
                        "authorization.goal": authorization.get("goal"),
                        "authorization.scope": authorization.get("scope"),
                        "is_deleted": False,
                        "$or": [
                            {"lease": None},
                            {"lease": {"$exists": False}},
                            {"lease.expires_at": {"$lte": now}},
                        ],
                    },
                    {"$set": {
                        "status": "superseded",
                        "successor_run_id": run_id,
                        "lease": None,
                        "lease_epoch": successor_epoch,
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
                    if (
                        fenced_step is not None
                        and fenced_step.get("status") not in {"completed", "failed"}
                    ):
                        await self._rollback_step_handoff(
                            run=predecessor_snapshot,
                            step_id=str(predecessor_step_id),
                            worker_id=fence_worker_id,
                            lease_epoch=successor_epoch,
                            previous_epoch=predecessor_epoch,
                            now=now,
                        )
                    raise AgentRuntimeReadinessConflict(
                        "predecessor changed before successor binding"
                    )
        created_run = False
        try:
            inserted = await self.runs.update_one(
                {"_id": run_id},
                {"$setOnInsert": run_document},
                upsert=True,
            )
            created_run = inserted.upserted_id is not None
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
        return AgentRuntimeBinding(run=stored, replayed=not created_run)

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
        run_object_id = _required_object_id(run_id, "run_id")
        owner_object_id = _required_object_id(owner_id, "owner_id")
        for _ in range(5):
            current = await self.get_run_owned(run_id=run_id, owner_id=owner_id)
            if current.get("status") in {
                "completed",
                "failed",
                "cancelled",
                "superseded",
            }:
                return current
            active_step_id = current.get("active_step_id")
            current_epoch = int(current.get("lease_epoch") or 0)
            current_termination = deepcopy(dict(termination))
            current_termination["step_id"] = (
                str(active_step_id) if active_step_id else None
            )
            if active_step_id:
                active_step = await self.steps.find_one({
                    "_id": str(active_step_id),
                    "run_id": run_object_id,
                    "owner_id": owner_object_id,
                    "is_deleted": False,
                })
                active_step_seed = current.get("active_step_seed")
                if active_step is None and isinstance(active_step_seed, Mapping):
                    if str(active_step_seed.get("step_id") or "") != str(
                        active_step_id
                    ):
                        raise AgentRuntimeStateConflict(
                            "active Agent step seed does not match its pointer"
                        )
                    active_step = await self._upsert_step_from_seed(
                        current,
                        dict(active_step_seed),
                    )
                if _step_checkpoint_needs_projection(current, active_step):
                    raise AgentRuntimeCheckpointPending(
                        "Agent step checkpoint must be projected before cancellation"
                    )
                protected_step_conditions: list[dict[str, Any]] = [
                    {"status": "failed"},
                    {
                        "status": "completed",
                        "planner_decision.kind": "propose_finish",
                        "observation.completion.satisfied": True,
                    },
                ]
                if current.get("status") != "paused":
                    protected_step_conditions.append({"status": "paused"})
                revoked = await self.steps.update_one(
                    {
                        "_id": str(active_step_id),
                        "run_id": run_object_id,
                        "owner_id": owner_object_id,
                        "lease_epoch": current_epoch,
                        "is_deleted": False,
                        "$nor": protected_step_conditions,
                    },
                    {
                        "$set": {"lease": None, "updated_at": now},
                        "$inc": {"lease_epoch": 1},
                    },
                )
                if revoked.modified_count != 1 and active_step is not None:
                    latest_step = await self.steps.find_one({
                        "_id": str(active_step_id),
                        "run_id": run_object_id,
                        "owner_id": owner_object_id,
                        "is_deleted": False,
                    })
                    if _step_checkpoint_needs_projection(current, latest_step):
                        raise AgentRuntimeCheckpointPending(
                            "Agent step checkpoint must be projected before cancellation"
                        )
                    continue
            else:
                latest_ordinal = int(current.get("next_ordinal") or 0) - 1
                latest_step = (
                    await self.steps.find_one({
                        "run_id": run_object_id,
                        "owner_id": owner_object_id,
                        "ordinal": latest_ordinal,
                        "is_deleted": False,
                    })
                    if latest_ordinal >= 0
                    else None
                )
                if _step_checkpoint_needs_projection(current, latest_step):
                    raise AgentRuntimeCheckpointPending(
                        "Agent step checkpoint must be projected before cancellation"
                    )
            document = await self.runs.find_one_and_update(
                {
                    "_id": run_object_id,
                    "owner_id": owner_object_id,
                    "status": {"$in": ["ready", "running", "paused"]},
                    "active_step_id": active_step_id,
                    "lease_epoch": current_epoch,
                    "is_deleted": False,
                },
                [
                    {"$set": {
                        "status": "cancelled",
                        "termination": current_termination,
                        "termination_event_key": None,
                        "lease": None,
                        "has_uncertain_attempts": {
                            "$anyElementTrue": {
                                "$map": {
                                    "input": {"$ifNull": ["$attempts", []]},
                                    "as": "attempt",
                                    "in": {
                                        "$in": [
                                            "$$attempt.state",
                                            ["dispatched", "uncertain"],
                                        ]
                                    },
                                }
                            }
                        },
                        "attempts": {
                            "$map": {
                                "input": {"$ifNull": ["$attempts", []]},
                                "as": "attempt",
                                "in": {
                                    "$cond": [
                                        {"$eq": ["$$attempt.state", "dispatched"]},
                                        {"$mergeObjects": [
                                            "$$attempt",
                                            {
                                                "state": "uncertain",
                                                "uncertain_reason": (
                                                    "cancelled_while_dispatched"
                                                ),
                                                "uncertain_at": now,
                                            },
                                        ]},
                                        "$$attempt",
                                    ]
                                },
                            }
                        },
                        "lease_epoch": {
                            "$add": [{"$ifNull": ["$lease_epoch", 0]}, 1]
                        },
                        "updated_at": now,
                    }},
                ],
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

    async def archive_step_call_attempts(
        self,
        *,
        run_id: str,
        owner_id: str,
        worker_id: str,
        lease_epoch: int,
        step_id: str,
        now: datetime,
    ) -> None:
        """Move one terminal step's attempts into its bounded audit ledger.

        The step write precedes the run-array pull deliberately.  A crash between
        them leaves duplicate evidence that a later call merges by call_key before
        retrying the pull; audit readers apply the same merge rule.
        """
        run_object_id = _required_object_id(run_id, "run_id")
        owner_object_id = _required_object_id(owner_id, "owner_id")
        run_filter = {
            "_id": run_object_id,
            "owner_id": owner_object_id,
            "status": {"$in": ["running", "paused"]},
            "lease.worker_id": str(worker_id),
            "lease.expires_at": {"$gt": now},
            "lease_epoch": int(lease_epoch),
            "is_deleted": False,
        }
        run = await self.runs.find_one(run_filter)
        if run is None:
            raise AgentRuntimeStateConflict(
                "terminal Agent step attempts could not be archived"
            )
        step = await self.steps.find_one({
            "_id": str(step_id),
            "run_id": run_object_id,
            "owner_id": owner_object_id,
            "status": {"$in": ["completed", "failed"]},
            "is_deleted": False,
        })
        if step is None:
            raise AgentRuntimeStateConflict(
                "only a terminal Agent step can archive call attempts"
            )

        raw_ledger = step.get("attempt_ledger")
        if raw_ledger is None:
            raw_ledger = []
        if not isinstance(raw_ledger, list):
            raise AgentRuntimeStateConflict("Agent step attempt ledger is invalid")
        merged: list[dict[str, Any]] = []
        positions: dict[str, int] = {}
        try:
            for raw_entry in raw_ledger:
                if not isinstance(raw_entry, Mapping):
                    raise ValueError("attempt ledger entry must be an object")
                projected = project_agent_runtime_attempt_ledger_entry(raw_entry)
                if dict(raw_entry) != projected:
                    raise ValueError("attempt ledger entry is not canonical")
                call_key = str(projected["call_key"])
                if call_key in positions:
                    raise ValueError("attempt ledger call_key is duplicated")
                positions[call_key] = len(merged)
                merged.append(projected)

            for raw_attempt in run.get("attempts") or []:
                if (
                    not isinstance(raw_attempt, Mapping)
                    or raw_attempt.get("step_id") != str(step_id)
                ):
                    continue
                projected = project_agent_runtime_attempt_ledger_entry(raw_attempt)
                call_key = str(projected["call_key"])
                previous_position = positions.get(call_key)
                if previous_position is None:
                    positions[call_key] = len(merged)
                    merged.append(projected)
                elif merged[previous_position] != projected:
                    raise ValueError("attempt ledger overlaps with different evidence")
        except (TypeError, ValueError) as exc:
            raise AgentRuntimeStateConflict(
                "Agent step attempt ledger evidence is invalid"
            ) from exc
        if len(merged) > MAX_ATTEMPT_LEDGER_ENTRIES_PER_STEP:
            raise AgentRuntimeStateConflict(
                "Agent step attempt ledger exceeds the Runtime v1 bound"
            )

        step_filter: dict[str, Any] = {
            "_id": str(step_id),
            "run_id": run_object_id,
            "owner_id": owner_object_id,
            "status": {"$in": ["completed", "failed"]},
            "is_deleted": False,
        }
        if "attempt_ledger" in step:
            step_filter["attempt_ledger"] = raw_ledger
        else:
            step_filter["attempt_ledger"] = {"$exists": False}
        ledger_result = await self.steps.update_one(
            step_filter,
            {"$set": {"attempt_ledger": merged, "updated_at": now}},
        )
        if ledger_result.matched_count != 1:
            current_step = await self.get_step_owned(
                run_id=run_id,
                owner_id=owner_id,
                step_id=step_id,
            )
            if current_step.get("attempt_ledger") != merged:
                raise AgentRuntimeStateConflict(
                    "Agent step attempt ledger changed concurrently"
                )

        pull_result = await self.runs.update_one(
            run_filter,
            {
                "$pull": {"attempts": {"step_id": str(step_id)}},
                "$set": {"updated_at": now},
            },
        )
        if pull_result.matched_count != 1:
            raise AgentRuntimeStateConflict(
                "terminal Agent step attempt archive lost its run lease"
            )

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
        worker_id: str,
        lease_epoch: int,
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
                "lease.worker_id": str(worker_id),
                "lease.expires_at": {"$gt": now},
                "lease_epoch": int(lease_epoch),
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
        run_object_id = _required_object_id(run_id, "run_id")
        owner_object_id = _required_object_id(owner_id, "owner_id")
        current = await self.get_run_owned(run_id=run_id, owner_id=owner_id)
        if current.get("status") not in {"ready", "running", "paused"}:
            raise AgentRuntimeLeaseUnavailable("Agent run cannot be leased")
        lease = dict(current.get("lease") or {})
        if (
            lease
            and lease.get("worker_id") != str(worker_id)
            and lease.get("expires_at") > now
        ):
            raise AgentRuntimeLeaseUnavailable("Agent run has another active lease")

        previous_epoch = int(current.get("lease_epoch") or 0)
        next_epoch = previous_epoch + 1
        active_step_id = current.get("active_step_id")
        adopted: dict[str, Any] | None = None
        if active_step_id:
            seed = current.get("active_step_seed")
            if isinstance(seed, Mapping):
                if str(seed.get("step_id") or "") != str(active_step_id):
                    raise AgentRuntimeStateConflict(
                        "active Agent step seed does not match its pointer"
                    )
                await self._upsert_step_from_seed(current, dict(seed))
            try:
                adopted = await self._adopt_step_lease(
                    run=current,
                    step_id=str(active_step_id),
                    worker_id=str(worker_id),
                    expected_lease_epoch=previous_epoch,
                    lease_epoch=next_epoch,
                    now=now,
                    expires_at=expires_at,
                )
            except AgentRuntimeStateConflict as exc:
                latest = await self.get_run_owned(run_id=run_id, owner_id=owner_id)
                if latest.get("status") not in {"ready", "running", "paused"}:
                    raise AgentRuntimeLeaseUnavailable(
                        "Agent run changed while its step was fenced"
                    ) from exc
                raise

        document = await self.runs.find_one_and_update(
            {
                "_id": run_object_id,
                "owner_id": owner_object_id,
                "status": {"$in": ["ready", "running", "paused"]},
                "active_step_id": active_step_id,
                "lease_epoch": previous_epoch,
                "is_deleted": False,
                "$or": [
                    {"lease": None},
                    {"lease": {"$exists": False}},
                    {"lease.expires_at": {"$lte": now}},
                    {"lease.worker_id": str(worker_id)},
                ],
            },
            {"$set": {
                "lease": {
                    "worker_id": str(worker_id),
                    "heartbeat_at": now,
                    "expires_at": expires_at,
                },
                "lease_epoch": next_epoch,
                "updated_at": now,
            }},
            return_document=ReturnDocument.AFTER,
        )
        if document is not None:
            return document
        if (
            adopted is not None
            and adopted.get("status") not in {"completed", "failed"}
        ):
            await self._rollback_step_handoff(
                run=current,
                step_id=str(active_step_id),
                worker_id=str(worker_id),
                lease_epoch=next_epoch,
                previous_epoch=previous_epoch,
                now=now,
            )
        raise AgentRuntimeLeaseUnavailable("Agent run lease changed during handoff")

    async def _rollback_step_handoff(
        self,
        *,
        run: Mapping[str, Any],
        step_id: str,
        worker_id: str,
        lease_epoch: int,
        previous_epoch: int,
        now: datetime,
    ) -> None:
        await self.steps.update_one(
            {
                "_id": str(step_id),
                "run_id": run["_id"],
                "owner_id": run["owner_id"],
                "lease.worker_id": str(worker_id),
                "lease_epoch": int(lease_epoch),
                "is_deleted": False,
            },
            {"$set": {
                "lease": None,
                "lease_epoch": int(previous_epoch),
                "updated_at": now,
            }},
        )

    async def _revoke_step_lease(
        self,
        *,
        run: Mapping[str, Any],
        step_id: str,
        worker_id: str,
        lease_epoch: int,
        now: datetime,
    ) -> None:
        await self.steps.update_one(
            {
                "_id": str(step_id),
                "run_id": run["_id"],
                "owner_id": run["owner_id"],
                "lease.worker_id": str(worker_id),
                "lease_epoch": int(lease_epoch),
                "is_deleted": False,
            },
            {"$set": {"lease": None, "updated_at": now}},
        )

    async def _adopt_step_lease(
        self,
        *,
        run: Mapping[str, Any],
        step_id: str,
        worker_id: str,
        expected_lease_epoch: int,
        lease_epoch: int,
        now: datetime,
        expires_at: datetime,
    ) -> dict[str, Any]:
        """Move a non-terminal step fence to the current run lease atomically."""
        document = await self.steps.find_one_and_update(
            {
                "_id": str(step_id),
                "run_id": run["_id"],
                "owner_id": run["owner_id"],
                "status": {"$nin": ["completed", "failed"]},
                "is_deleted": False,
                "$or": [
                    {
                        "lease_epoch": int(expected_lease_epoch),
                        "lease": None,
                    },
                    {
                        "lease_epoch": int(expected_lease_epoch),
                        "lease": {"$exists": False},
                    },
                    {
                        "lease_epoch": int(expected_lease_epoch),
                        "lease.expires_at": {"$lte": now},
                    },
                    {
                        "lease_epoch": int(expected_lease_epoch),
                        "lease.worker_id": str(worker_id),
                    },
                    {
                        "lease_epoch": int(lease_epoch),
                        "lease.worker_id": str(worker_id),
                    },
                    {
                        "lease_epoch": int(lease_epoch),
                        "lease.expires_at": {"$lte": now},
                    },
                ],
            },
            {"$set": {
                "lease": {
                    "worker_id": str(worker_id),
                    "expires_at": expires_at,
                },
                "lease_epoch": int(lease_epoch),
                "updated_at": now,
            }},
            return_document=ReturnDocument.AFTER,
        )
        if document is not None:
            return document
        stored = await self.steps.find_one({
            "_id": str(step_id),
            "run_id": run["_id"],
            "owner_id": run["owner_id"],
            "is_deleted": False,
        })
        if stored is not None and stored.get("status") in {"completed", "failed"}:
            return stored
        raise AgentRuntimeStateConflict(
            "active Agent step could not adopt the current lease"
        )

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
        run = await self.runs.find_one_and_update(
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
            return_document=ReturnDocument.AFTER,
        )
        if run is None:
            return False
        active_step_id = run.get("active_step_id")
        if not active_step_id:
            return True
        result = await self.steps.update_one(
            {
                "_id": str(active_step_id),
                "run_id": run["_id"],
                "owner_id": run["owner_id"],
                "status": {"$nin": ["completed", "failed"]},
                "lease.worker_id": str(worker_id),
                "lease.expires_at": {"$gt": now},
                "lease_epoch": int(lease_epoch),
                "is_deleted": False,
            },
            {"$set": {
                "lease.expires_at": expires_at,
                "updated_at": now,
            }},
        )
        if result.matched_count == 1:
            return True
        step = await self.steps.find_one({
            "_id": str(active_step_id),
            "run_id": run["_id"],
            "owner_id": run["owner_id"],
            "is_deleted": False,
        })
        return bool(step and step.get("status") in {"completed", "failed"})

    async def release_lease(
        self,
        *,
        run_id: str,
        owner_id: str,
        worker_id: str,
        lease_epoch: int,
        now: datetime,
    ) -> bool:
        run = await self.runs.find_one(
            {
                "_id": _required_object_id(run_id, "run_id"),
                "owner_id": _required_object_id(owner_id, "owner_id"),
                "lease.worker_id": str(worker_id),
                "lease_epoch": int(lease_epoch),
                "is_deleted": False,
            },
        )
        if run is None:
            return False
        if run.get("active_step_id"):
            await self.steps.update_one(
                {
                    "_id": str(run["active_step_id"]),
                    "run_id": run["_id"],
                    "owner_id": run["owner_id"],
                    "lease.worker_id": str(worker_id),
                    "lease_epoch": int(lease_epoch),
                    "is_deleted": False,
                },
                {"$set": {"lease": None, "updated_at": now}},
            )
        result = await self.runs.update_one(
            {
                "_id": run["_id"],
                "owner_id": run["owner_id"],
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
                lease = dict(run.get("lease") or {})
                adopted = await self._adopt_step_lease(
                    run=run,
                    step_id=str(step["step_id"]),
                    worker_id=str(worker_id),
                    expected_lease_epoch=int(lease_epoch),
                    lease_epoch=int(lease_epoch),
                    now=now,
                    expires_at=lease.get("expires_at"),
                )
                confirmed = await self.runs.find_one({
                    "_id": run_object_id,
                    "owner_id": owner_object_id,
                    "status": "running",
                    "active_step_id": str(step["step_id"]),
                    "lease.worker_id": str(worker_id),
                    "lease.expires_at": {"$gt": now},
                    "lease_epoch": int(lease_epoch),
                    "is_deleted": False,
                })
                if confirmed is None:
                    await self._revoke_step_lease(
                        run=run,
                        step_id=str(step["step_id"]),
                        worker_id=str(worker_id),
                        lease_epoch=int(lease_epoch),
                        now=now,
                    )
                    raise AgentRuntimeStateConflict(
                        "Agent run lease changed while its step was claimed"
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
        current = await self.runs.find_one({
            "_id": run_object_id,
            "owner_id": owner_object_id,
            "status": "running",
            "active_step_id": str(step["step_id"]),
            "lease.worker_id": str(worker_id),
            "lease.expires_at": {"$gt": now},
            "lease_epoch": int(lease_epoch),
            "is_deleted": False,
        })
        if current is None:
            await self._revoke_step_lease(
                run=claimed,
                step_id=str(step["step_id"]),
                worker_id=str(worker_id),
                lease_epoch=int(lease_epoch),
                now=now,
            )
            raise AgentRuntimeStateConflict(
                "Agent run lease changed while its step was materialized"
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
            "validation_evidence": None,
            "tool_invocation": None,
            "observation": None,
            "usage_delta": {},
            "attempt_ledger": [],
            "revision_before": run.get("expected_narrative_revision"),
            "revision_after": run.get("expected_narrative_revision"),
            "lease": deepcopy(run.get("lease")),
            "lease_epoch": int(run.get("lease_epoch") or 0),
            "pause_lease_epoch": None,
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
        document = await self.steps.find_one_and_update(
            {
                "_id": str(step_id),
                "run_id": _required_object_id(run_id, "run_id"),
                "owner_id": _required_object_id(owner_id, "owner_id"),
                "status": str(expected),
                "lease.worker_id": str(worker_id),
                "lease.expires_at": {"$gt": now},
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
