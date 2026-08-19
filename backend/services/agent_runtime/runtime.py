"""Persisted Plan-Act-Observe executor with deny-by-default policy gates."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any, Literal, Mapping
from uuid import uuid4

from bson import ObjectId

from backend.db.repositories.agent_runtime_repository import (
    AgentRuntimeBudgetExceeded,
    AgentRuntimeReadinessConflict,
    AgentRuntimeRepository,
    AgentRuntimeStateConflict,
    agent_runtime_repository,
    project_agent_runtime_event_payload,
)
from backend.services.agent_runtime.contracts import (
    AgentEventView,
    AgentReadinessRequest,
    AgentReadinessView,
    AgentReplayView,
    AgentRunView,
    AgentRuntimeUsage,
    AgentScope,
    AgentStepView,
    AgentTermination,
    CompletionDecision,
    PlannerDecision,
    PlannerInput,
    PlannerResult,
    RuntimeObservation,
    RuntimeToolContext,
    RuntimeToolDescriptor,
    RuntimeToolReference,
    RuntimeToolResult,
    V1_RUNTIME_CHANGE_CLASSES,
    V1_RUNTIME_EFFECT_CLASSES,
    V1_RUNTIME_PROPOSAL_KINDS,
)
from backend.services.agent_runtime.policy import (
    AgentRuntimePolicyViolation,
    RuntimePolicyGate,
)


READINESS_TTL_SECONDS = 30 * 60
TERMINAL_RUN_STATUSES = frozenset({
    "completed",
    "failed",
    "cancelled",
    "superseded",
})
RESUMABLE_PAUSE_REASONS = frozenset({
    "authorization_required",
    "manual_approval_required",
    "concurrent_narrative_change",
    "uncertain_paid_attempt",
})
LINEAGE_LIMIT_FIELDS = (
    "max_steps",
    "max_planner_calls",
    "max_tool_calls",
    "max_paid_attempts",
    "token_budget",
)


class _UncertainDispatchedCall(RuntimeError):
    pass


class _DeadlineExceeded(RuntimeError):
    pass


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _schema_digest(schema: type) -> str:
    return _digest(schema.model_json_schema())


def _tool_snapshot(descriptor: RuntimeToolDescriptor) -> dict[str, Any]:
    return {
        "schema_version": descriptor.schema_version,
        "reference": descriptor.reference.model_dump(mode="json"),
        "label": descriptor.label,
        "input_schema_digest": _schema_digest(descriptor.input_schema),
        "output_schema_digest": _schema_digest(descriptor.output_schema),
        "scope_kinds": list(descriptor.scope_kinds),
        "effect_class": descriptor.effect_class,
        "proposal_kinds": list(descriptor.proposal_kinds),
        "change_classes": list(descriptor.change_classes),
        "max_paid_attempts_per_call": descriptor.max_paid_attempts_per_call,
        "max_tokens_per_call": descriptor.max_tokens_per_call,
        "implementation_revision": descriptor.implementation_revision,
        "context_policy_revision": descriptor.context_policy_revision,
        "external_data_categories": list(descriptor.external_data_categories),
        "idempotent": descriptor.idempotent,
    }


def _step_audit_projection(step: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "step_id": str(step.get("step_id") or ""),
        "ordinal": int(step.get("ordinal") or 0),
        "status": str(step.get("status") or ""),
        "planner_decision": step.get("planner_decision"),
        "policy_decision": step.get("policy_decision"),
        "tool_invocation": step.get("tool_invocation"),
        "observation": step.get("observation"),
        "usage_delta": step.get("usage_delta"),
    }


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _termination_projection(
    *,
    status: str,
    reason_code: str,
    now: datetime,
    step_id: str | None,
) -> dict[str, Any]:
    category_by_status = {
        "completed": "success",
        "paused": "pause",
        "failed": "failure",
        "cancelled": "cancelled",
        "superseded": "superseded",
    }
    try:
        category = category_by_status[status]
    except KeyError as exc:
        raise ValueError(f"unsupported Agent termination status: {status}") from exc
    return {
        "status": status,
        "category": category,
        "reason_code": reason_code,
        "resumable": bool(
            status == "paused" and reason_code in RESUMABLE_PAUSE_REASONS
        ),
        "occurred_at": now,
        "step_id": step_id,
        "detail_code": reason_code,
    }


def _termination_event_projection(
    *,
    status: str,
    reason_code: str,
    lease_epoch: int,
) -> tuple[str, str, dict[str, Any]]:
    event_type = "run_paused" if status == "paused" else "run_terminated"
    event_key = (
        f"run-paused-{reason_code}-{int(lease_epoch)}"
        if status == "paused"
        else f"run-{status}-{reason_code}"
    )
    return event_key, event_type, {
        "status": status,
        "reason_code": reason_code,
    }


class AgentRuntime:
    """One application seam for readiness inspection and bounded execution."""

    def __init__(
        self,
        *,
        planner: Any,
        tools: Any,
        completion_policy: Any,
        revision_reader: Any,
        scope_validator: Any,
        clock: Any,
        lease_seconds: int = 30,
        repository: AgentRuntimeRepository = agent_runtime_repository,
        policy_gate: RuntimePolicyGate | None = None,
    ) -> None:
        if int(lease_seconds) < 1:
            raise ValueError("lease_seconds must be positive")
        self._planner = planner
        self._tools = tools
        self._completion_policy = completion_policy
        self._revision_reader = revision_reader
        self._scope_validator = scope_validator
        self._clock = clock
        self._lease_seconds = int(lease_seconds)
        self._repository = repository
        self._policy_gate = policy_gate or RuntimePolicyGate()

    async def inspect_readiness(
        self,
        *,
        owner_id: str,
        request: AgentReadinessRequest,
    ) -> AgentReadinessView:
        """Freeze exact authorization without invoking a planner or a tool."""
        now = _aware(self._clock())
        await self._scope_validator(
            owner_id=str(owner_id),
            novel_id=request.novel_id,
            scope=request.scope,
        )
        baseline_revision = int(await self._revision_reader(
            owner_id=str(owner_id),
            novel_id=request.novel_id,
        ))
        planner_snapshot = self._planner.descriptor.model_dump(mode="json")
        tool_snapshots: list[dict[str, Any]] = []
        allowed_effects = set(request.allowed_effects)
        allowed_changes = set(request.allowed_change_classes)
        allowed_external = set(request.allowed_external_data_categories)
        if request.approval_mode != "proposal_only":
            raise ValueError("Runtime v1 only accepts proposal_only approval")
        if not allowed_effects.issubset(V1_RUNTIME_EFFECT_CLASSES):
            raise ValueError("readiness contains an unknown Runtime v1 effect class")
        if not allowed_changes.issubset(V1_RUNTIME_CHANGE_CLASSES):
            raise ValueError("readiness contains an unknown Runtime v1 change class")
        for reference in request.allowed_tools:
            descriptor = self._tools.describe(reference)
            if descriptor.reference != reference:
                raise ValueError("tool registry returned another tool identity")
            if request.scope.kind not in descriptor.scope_kinds:
                raise ValueError("authorized tool does not support the target scope")
            if descriptor.effect_class not in allowed_effects:
                raise ValueError("authorized tool effect is missing from allowed_effects")
            if descriptor.effect_class not in V1_RUNTIME_EFFECT_CLASSES:
                raise ValueError(
                    "Runtime v1 cannot authorize an unknown effect class"
                )
            if not set(descriptor.change_classes).issubset(
                V1_RUNTIME_CHANGE_CLASSES
            ):
                raise ValueError("Runtime v1 cannot authorize this change class")
            if not set(descriptor.change_classes).issubset(allowed_changes):
                raise ValueError("authorized tool change class is not allowed")
            if not set(descriptor.proposal_kinds).issubset(
                V1_RUNTIME_PROPOSAL_KINDS
            ):
                raise ValueError("Runtime v1 cannot authorize this proposal kind")
            if not set(descriptor.external_data_categories).issubset(allowed_external):
                raise ValueError("authorized tool external-data category is not allowed")
            tool_snapshots.append(_tool_snapshot(descriptor))

        max_tool_paid = max(
            (int(item["max_paid_attempts_per_call"]) for item in tool_snapshots),
            default=0,
        )
        max_tool_tokens = max(
            (int(item["max_tokens_per_call"]) for item in tool_snapshots),
            default=0,
        )
        budget_projection = {
            "planner": {
                "max_calls": request.limits.max_planner_calls,
                "max_paid_attempts": (
                    request.limits.max_planner_calls
                    * self._planner.descriptor.max_paid_attempts_per_call
                ),
                "max_tokens": (
                    request.limits.max_planner_calls
                    * self._planner.descriptor.max_tokens_per_call
                ),
            },
            "tools": {
                "max_calls": request.limits.max_tool_calls,
                "max_paid_attempts": request.limits.max_tool_calls * max_tool_paid,
                "max_tokens": request.limits.max_tool_calls * max_tool_tokens,
            },
            "authorization_ceiling": {
                "max_paid_attempts": request.limits.max_paid_attempts,
                "token_budget": request.limits.token_budget,
            },
        }
        lineage, replay_source_input_digest = await self._resolve_lineage(
            owner_id=str(owner_id),
            request=request,
        )

        readiness_id = str(ObjectId())
        expires_at = now + timedelta(seconds=READINESS_TTL_SECONDS)
        deadline_at = now + timedelta(seconds=request.limits.deadline_seconds)
        authorization = {
            "schema_version": "agent_runtime_authorization.v1",
            "readiness_id": readiness_id,
            "binding_mode": "single_use",
            "owner_id": str(owner_id),
            "novel_id": request.novel_id,
            "goal": request.goal,
            "scope": request.scope.model_dump(mode="json"),
            "allowed_tools": [item.model_dump(mode="json") for item in request.allowed_tools],
            "allowed_effects": list(request.allowed_effects),
            "allowed_change_classes": list(request.allowed_change_classes),
            "allowed_external_data_categories": list(
                request.allowed_external_data_categories
            ),
            "approval_mode": request.approval_mode,
            "limits": request.limits.model_dump(mode="json"),
            "planner": planner_snapshot,
            "tool_registry_revision": str(self._tools.registry_revision),
            "tools": tool_snapshots,
            "budget_projection": budget_projection,
            "completion_policy_revision": str(self._completion_policy.revision),
            "baseline_narrative_revision": baseline_revision,
            "predecessor_run_id": request.predecessor_run_id,
            "replay_of_run_id": request.replay_of_run_id,
            "lineage": lineage,
            "replay_source_input_digest": replay_source_input_digest,
            "issued_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "deadline_at": deadline_at.isoformat(),
        }
        authorization_digest = _digest(authorization)
        await self._repository.create_readiness(
            readiness_id=readiness_id,
            owner_id=str(owner_id),
            novel_id=request.novel_id,
            digest=authorization_digest,
            authorization=authorization,
            issued_at=now,
            expires_at=expires_at,
        )
        return AgentReadinessView(
            readiness_id=readiness_id,
            digest=authorization_digest,
            expires_at=expires_at,
            deadline_at=deadline_at,
            baseline_narrative_revision=baseline_revision,
            authorization=authorization,
        )

    async def _resolve_lineage(
        self,
        *,
        owner_id: str,
        request: AgentReadinessRequest,
    ) -> tuple[dict[str, Any] | None, str | None]:
        source_id = request.predecessor_run_id or request.replay_of_run_id
        if source_id is None:
            return None, None
        source = await self._repository.get_run_owned(
            run_id=source_id,
            owner_id=owner_id,
        )
        source_authorization = dict(source.get("authorization") or {})
        if (
            str(source.get("novel_id")) != request.novel_id
            or source_authorization.get("goal") != request.goal
            or source_authorization.get("scope")
            != request.scope.model_dump(mode="json")
        ):
            raise AgentRuntimeReadinessConflict(
                "lineage source does not match novel, goal, and scope"
            )

        source_steps = await self._repository.list_steps_owned(
            run_id=source_id,
            owner_id=owner_id,
        )
        source_input_digest = self._source_input_digest(source, source_steps)
        if request.replay_of_run_id:
            if source.get("status") not in {"completed", "failed", "cancelled"}:
                raise AgentRuntimeReadinessConflict(
                    "replay source must be terminal and cannot be superseded"
                )
            return None, source_input_digest

        if source.get("status") != "paused" or source.get("successor_run_id"):
            raise AgentRuntimeReadinessConflict(
                "predecessor must be paused without an existing successor"
            )
        previous_lineage = source_authorization.get("lineage")
        previous_actual = (
            dict(previous_lineage.get("cumulative_actual_usage") or {})
            if isinstance(previous_lineage, Mapping)
            else {}
        )
        source_usage = AgentRuntimeUsage.model_validate(source.get("usage") or {})
        cumulative_actual = {
            field: int(previous_actual.get(field) or 0)
            + int(getattr(source_usage, field))
            for field in AgentRuntimeUsage.model_fields
        }
        previous_authorized = (
            dict(previous_lineage.get("cumulative_authorized_upper_bound") or {})
            if isinstance(previous_lineage, Mapping)
            else {
                field: int((source_authorization.get("limits") or {}).get(field) or 0)
                for field in LINEAGE_LIMIT_FIELDS
            }
        )
        new_limits = request.limits.model_dump(mode="python")
        cumulative_authorized = {
            field: int(previous_authorized.get(field) or 0)
            + int(new_limits.get(field) or 0)
            for field in LINEAGE_LIMIT_FIELDS
        }
        root_run_id = (
            str(previous_lineage.get("root_run_id"))
            if isinstance(previous_lineage, Mapping)
            else str(source["_id"])
        )
        return {
            "root_run_id": root_run_id,
            "predecessor_run_id": str(source["_id"]),
            "predecessor_input_digest": source_input_digest,
            "cumulative_actual_usage": cumulative_actual,
            "new_authorized_upper_bound": {
                field: int(new_limits.get(field) or 0)
                for field in LINEAGE_LIMIT_FIELDS
            },
            "cumulative_authorized_upper_bound": cumulative_authorized,
        }, None

    @staticmethod
    def _source_input_digest(
        run: Mapping[str, Any],
        steps: list[dict[str, Any]],
    ) -> str:
        authorization = dict(run.get("authorization") or {})
        return _digest({
            "schema_version": "agent_runtime_replay_input.v1",
            "authorization_digest": str(run.get("authorization_digest") or ""),
            "goal": authorization.get("goal"),
            "scope": authorization.get("scope"),
            "baseline_narrative_revision": authorization.get(
                "baseline_narrative_revision"
            ),
            "step_inputs": [
                {
                    "ordinal": int(step.get("ordinal") or 0),
                    "observation_cursor": int(
                        step.get("input_observation_cursor") or 0
                    ),
                    "observation_digest": str(
                        step.get("input_observation_digest") or ""
                    ),
                }
                for step in steps
            ],
        })

    async def start(
        self,
        *,
        owner_id: str,
        readiness_id: str,
        digest: str,
        start_request_id: str,
    ) -> AgentRunView:
        now = _aware(self._clock())
        run = await self._repository.bind_readiness(
            readiness_id=readiness_id,
            owner_id=str(owner_id),
            digest=digest,
            start_request_id=start_request_id,
            now=now,
        )
        run_id = str(run["_id"])
        if run.get("predecessor_run_id"):
            predecessor_id = str(run["predecessor_run_id"])
            await self._event(
                run_id=predecessor_id,
                event_key=f"superseded-by-{run_id}",
                event_type="run_superseded",
                payload={
                    "status": "superseded",
                    "successor_run_id": run_id,
                    "reason_code": "continued_by_successor",
                },
                now=now,
            )
        if run.get("status") in TERMINAL_RUN_STATUSES or run.get("status") == "paused":
            await self._repair_projection_audit(
                run_id=run_id,
                owner_id=str(owner_id),
                now=now,
            )
            return await self._run_view(run_id=run_id, owner_id=str(owner_id))
        return await self._execute_owned_run(
            run_id=run_id,
            owner_id=str(owner_id),
            resumed=False,
        )

    async def resume(
        self,
        *,
        owner_id: str,
        run_id: str,
        uncertain_action: Literal["retry", "skip"] | None = None,
        conditions_confirmed: bool = False,
    ) -> AgentRunView:
        """Recover a crashed run using only its frozen authorization snapshot."""
        run = await self._repository.get_run_owned(
            run_id=run_id,
            owner_id=str(owner_id),
        )
        if run.get("status") in TERMINAL_RUN_STATUSES:
            await self._repair_projection_audit(
                run_id=run_id,
                owner_id=str(owner_id),
                now=_aware(self._clock()),
            )
            return await self._run_view(run_id=run_id, owner_id=str(owner_id))
        if run.get("status") == "paused":
            await self._repair_projection_audit(
                run_id=run_id,
                owner_id=str(owner_id),
                now=_aware(self._clock()),
            )
            termination = dict(run.get("termination") or {})
            reason_code = str(termination.get("reason_code") or "")
            if reason_code == "uncertain_paid_attempt" and uncertain_action is None:
                return await self._run_view(run_id=run_id, owner_id=str(owner_id))
            if reason_code in {"authorization_required", "manual_approval_required"}:
                if not conditions_confirmed:
                    return await self._run_view(run_id=run_id, owner_id=str(owner_id))
            elif reason_code not in {
                "uncertain_paid_attempt",
                "concurrent_narrative_change",
            }:
                return await self._run_view(run_id=run_id, owner_id=str(owner_id))
        return await self._execute_owned_run(
            run_id=run_id,
            owner_id=str(owner_id),
            resumed=True,
            uncertain_action=uncertain_action,
            conditions_confirmed=conditions_confirmed,
        )

    async def cancel(self, *, owner_id: str, run_id: str) -> AgentRunView:
        """Idempotently cancel a non-terminal run without dispatching new work."""
        normalized_owner_id = str(owner_id)
        run = await self._repository.get_run_owned(
            run_id=run_id,
            owner_id=normalized_owner_id,
        )
        if run.get("status") in TERMINAL_RUN_STATUSES:
            if run.get("status") == "cancelled":
                await self._repair_projection_audit(
                    run_id=run_id,
                    owner_id=normalized_owner_id,
                    now=_aware(self._clock()),
                )
            return await self._run_view(
                run_id=run_id,
                owner_id=normalized_owner_id,
            )
        now = _aware(self._clock())
        await self._repository.cancel_run(
            run_id=run_id,
            owner_id=normalized_owner_id,
            termination={
                "status": "cancelled",
                "category": "cancelled",
                "reason_code": "cancelled_by_user",
                "resumable": False,
                "occurred_at": now,
                "step_id": (
                    str(run["active_step_id"])
                    if run.get("active_step_id")
                    else None
                ),
                "detail_code": "cancelled_by_user",
            },
            now=now,
        )
        await self._repair_projection_audit(
            run_id=run_id,
            owner_id=normalized_owner_id,
            now=now,
        )
        return await self._run_view(run_id=run_id, owner_id=normalized_owner_id)

    async def _repair_projection_audit(
        self,
        *,
        run_id: str,
        owner_id: str,
        now: datetime,
    ) -> None:
        """Idempotently complete events whose authoritative projection committed first."""
        run = await self._repository.get_run_owned(
            run_id=run_id,
            owner_id=owner_id,
        )
        steps = await self._repository.list_steps_owned(
            run_id=run_id,
            owner_id=owner_id,
        )
        await self._event(
            run_id=run_id,
            event_key="run-created",
            event_type="run_created",
            payload={"status": "ready"},
            now=now,
        )
        await self._event(
            run_id=run_id,
            event_key="readiness-bound",
            event_type="readiness_bound",
            payload={"status": "bound"},
            now=now,
        )
        if run.get("status") != "ready" or steps or run.get("attempts"):
            await self._event(
                run_id=run_id,
                event_key="run-started",
                event_type="run_started",
                payload={"status": "running"},
                now=now,
            )

        attempts_by_step: dict[str, list[dict[str, Any]]] = {}
        for raw_attempt in run.get("attempts") or []:
            if isinstance(raw_attempt, Mapping) and raw_attempt.get("step_id"):
                attempts_by_step.setdefault(
                    str(raw_attempt["step_id"]),
                    [],
                ).append(dict(raw_attempt))

        for step in steps:
            step_id = str(step["step_id"])
            ordinal = int(step["ordinal"])
            attempts = attempts_by_step.get(step_id, [])
            planner_attempts = [
                attempt for attempt in attempts if attempt.get("kind") == "planner"
            ]
            tool_attempts = [
                attempt for attempt in attempts if attempt.get("kind") == "tool"
            ]
            for attempt in planner_attempts:
                await self._repair_attempt_events(
                    run_id=run_id,
                    step_id=step_id,
                    ordinal=ordinal,
                    attempt=attempt,
                    decision=None,
                    invocation=None,
                    now=now,
                )

            decision: PlannerDecision | None = None
            try:
                decision = PlannerDecision.model_validate(step.get("planner_decision"))
            except Exception:
                decision = None
            if decision is not None:
                await self._record_step_planned(
                    run_id=run_id,
                    step_id=step_id,
                    ordinal=ordinal,
                    decision=decision,
                    now=now,
                )
                policy_decision = step.get("policy_decision")
                if isinstance(policy_decision, Mapping):
                    await self._event(
                        run_id=run_id,
                        event_key=f"step-{ordinal}-policy",
                        event_type="policy_decided",
                        payload={
                            "ordinal": ordinal,
                            "allowed": bool(policy_decision.get("allowed")),
                            "decision_kind": decision.kind,
                            "policy_digest": _digest(policy_decision),
                        },
                        step_id=step_id,
                        now=now,
                    )

            invocation = (
                dict(step["tool_invocation"])
                if isinstance(step.get("tool_invocation"), Mapping)
                else None
            )
            for attempt in tool_attempts:
                await self._repair_attempt_events(
                    run_id=run_id,
                    step_id=step_id,
                    ordinal=ordinal,
                    attempt=attempt,
                    decision=decision,
                    invocation=invocation,
                    now=now,
                )

            observation = step.get("observation")
            if (
                decision is not None
                and decision.kind == "call_tool"
                and isinstance(observation, Mapping)
            ):
                validated = RuntimeObservation.model_validate(observation)
                await self._event(
                    run_id=run_id,
                    event_key=f"step-{ordinal}-tool-observed",
                    event_type="tool_observed",
                    payload={
                        "ordinal": ordinal,
                        "status": validated.status,
                        "code": validated.code,
                        "observation_digest": _digest(observation),
                    },
                    step_id=step_id,
                    now=now,
                )
            if step.get("status") == "completed" and decision is not None:
                kind = "finish" if decision.kind == "propose_finish" else "tool"
                await self._event(
                    run_id=run_id,
                    event_key=f"step-{ordinal}-completed",
                    event_type="step_completed",
                    payload={
                        "ordinal": ordinal,
                        "status": "completed",
                        "kind": kind,
                        "step_digest": _digest(_step_audit_projection(step)),
                    },
                    step_id=step_id,
                    now=now,
                )

        termination = run.get("termination")
        if not isinstance(termination, Mapping):
            return
        status = str(termination.get("status") or "")
        reason_code = str(termination.get("reason_code") or "")
        termination_step_id = (
            str(termination["step_id"])
            if termination.get("step_id")
            else None
        )
        if status == "paused":
            projected_key, event_type, event_payload = _termination_event_projection(
                status=status,
                reason_code=reason_code,
                lease_epoch=int(run.get("lease_epoch") or 0),
            )
            await self._event(
                run_id=run_id,
                event_key=str(run.get("termination_event_key") or projected_key),
                event_type=event_type,
                payload=event_payload,
                step_id=termination_step_id,
                now=now,
            )
        elif status == "superseded" and run.get("successor_run_id"):
            successor_id = str(run["successor_run_id"])
            await self._event(
                run_id=run_id,
                event_key=f"superseded-by-{successor_id}",
                event_type="run_superseded",
                payload={
                    "status": "superseded",
                    "successor_run_id": successor_id,
                    "reason_code": "continued_by_successor",
                },
                now=now,
            )
        elif status in {"completed", "failed", "cancelled"}:
            event_key, event_type, event_payload = _termination_event_projection(
                status=status,
                reason_code=reason_code,
                lease_epoch=int(run.get("lease_epoch") or 0),
            )
            await self._event(
                run_id=run_id,
                event_key=event_key,
                event_type=event_type,
                payload=event_payload,
                step_id=termination_step_id,
                now=now,
            )

    async def _repair_attempt_events(
        self,
        *,
        run_id: str,
        step_id: str,
        ordinal: int,
        attempt: Mapping[str, Any],
        decision: PlannerDecision | None,
        invocation: Mapping[str, Any] | None,
        now: datetime,
    ) -> None:
        call_key = str(attempt.get("call_key") or "")
        kind = str(attempt.get("kind") or "")
        if not call_key or kind not in {"planner", "tool"}:
            raise AgentRuntimeStateConflict("Agent attempt identity is invalid")
        await self._event(
            run_id=run_id,
            event_key=f"{call_key}-reserved",
            event_type="attempt_reserved",
            payload={"ordinal": ordinal, "kind": kind},
            step_id=step_id,
            now=now,
        )
        state = str(attempt.get("state") or "")
        if kind == "tool" and state in {
            "dispatched",
            "settled",
            "uncertain",
            "resolved_retry",
            "resolved_skip",
        }:
            if (
                decision is None
                or decision.kind != "call_tool"
                or decision.tool is None
                or invocation is None
            ):
                raise AgentRuntimeStateConflict(
                    "dispatched tool attempt has no invocation projection"
                )
            await self._event(
                run_id=run_id,
                event_key=f"{call_key}-dispatched",
                event_type="tool_dispatched",
                payload={
                    "ordinal": ordinal,
                    "tool_name": decision.tool.name,
                    "tool_version": decision.tool.version,
                    "invocation_digest": _digest(invocation),
                },
                step_id=step_id,
                now=now,
            )
        if state == "settled":
            await self._event(
                run_id=run_id,
                event_key=f"{call_key}-settled",
                event_type="attempt_settled",
                payload={"ordinal": ordinal},
                step_id=step_id,
                now=now,
            )
        elif state in {"uncertain", "resolved_retry", "resolved_skip"}:
            await self._event(
                run_id=run_id,
                event_key=f"{call_key}-uncertain",
                event_type="attempt_uncertain",
                payload={
                    "ordinal": ordinal,
                    "reason_code": "dispatch_outcome_unknown",
                },
                step_id=step_id,
                now=now,
            )

    async def get(self, *, owner_id: str, run_id: str) -> AgentRunView:
        return await self._run_view(run_id=run_id, owner_id=str(owner_id))

    async def list(self, *, owner_id: str) -> tuple[AgentRunView, ...]:
        runs = await self._repository.list_runs_owned(owner_id=str(owner_id))
        return tuple([
            await self._run_view(run_id=str(run["_id"]), owner_id=str(owner_id))
            for run in runs
        ])

    async def audit_replay(
        self,
        *,
        owner_id: str,
        run_id: str,
    ) -> AgentReplayView:
        """Replay frozen authorization, Policy, steps, and events without adapters."""
        run = await self._repository.get_run_owned(
            run_id=run_id,
            owner_id=str(owner_id),
        )
        steps = await self._repository.list_steps_owned(
            run_id=run_id,
            owner_id=str(owner_id),
        )
        events = await self._repository.list_events_owned(
            run_id=run_id,
            owner_id=str(owner_id),
        )
        violations: list[str] = []

        def add_violation(code: str) -> None:
            if code not in violations:
                violations.append(code)

        authorization = dict(run.get("authorization") or {})
        if (
            authorization.get("schema_version")
            != "agent_runtime_authorization.v1"
            or _digest(authorization)
            != str(run.get("authorization_digest") or "")
        ):
            add_violation("authorization_digest_mismatch")

        if [int(item["ordinal"]) for item in steps] != list(range(len(steps))):
            add_violation("step_ordinal_mismatch")

        sequences = [int(item["sequence"]) for item in events]
        if any(
            current <= previous
            for previous, current in zip(sequences, sequences[1:])
        ):
            add_violation("event_sequence_mismatch")

        step_by_id = {str(item["step_id"]): item for item in steps}
        attempts_by_step: dict[str, list[dict[str, Any]]] = {}
        for raw_attempt in run.get("attempts") or []:
            if not isinstance(raw_attempt, Mapping):
                add_violation("attempt_event_identity_mismatch")
                continue
            attempt = dict(raw_attempt)
            attempt_step_id = str(attempt.get("step_id") or "")
            call_key = str(attempt.get("call_key") or "")
            if not attempt_step_id or not call_key:
                add_violation("attempt_event_identity_mismatch")
                continue
            attempts_by_step.setdefault(attempt_step_id, []).append(attempt)
        event_positions_by_step: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
        derived_status = "ready"
        run_event_started = False
        terminal_event: Mapping[str, Any] | None = None
        terminal_position: int | None = None
        for position, event in enumerate(events):
            event_type = str(event.get("type") or "")
            payload = dict(event.get("payload") or {})
            if event.get("schema_version") != "agent_runtime_event.v1":
                add_violation("event_schema_mismatch")
            try:
                projected = project_agent_runtime_event_payload(event_type, payload)
                if projected != payload:
                    add_violation("event_payload_mismatch")
            except ValueError:
                add_violation("event_payload_mismatch")

            step_id = str(event.get("step_id") or "")
            if step_id:
                if step_id not in step_by_id:
                    add_violation("event_step_mismatch")
                event_positions_by_step.setdefault(step_id, []).append(
                    (position, event)
                )

            if terminal_position is not None:
                add_violation("run_event_order_mismatch")
                continue
            if event_type == "run_created":
                if position != 0 or run_event_started:
                    add_violation("run_event_order_mismatch")
                derived_status = "ready"
                run_event_started = True
            elif event_type == "readiness_bound":
                if not run_event_started or derived_status != "ready":
                    add_violation("run_event_order_mismatch")
            elif event_type == "run_started":
                if not run_event_started or derived_status != "ready":
                    add_violation("run_event_order_mismatch")
                derived_status = "running"
            elif event_type == "run_resumed":
                if derived_status not in {"running", "paused"}:
                    add_violation("run_event_order_mismatch")
                derived_status = "running"
            elif event_type == "run_paused":
                if derived_status != "running":
                    add_violation("run_event_order_mismatch")
                derived_status = "paused"
                terminal_event = event
            elif event_type == "run_superseded":
                if derived_status != "paused":
                    add_violation("run_event_order_mismatch")
                derived_status = "superseded"
                terminal_event = event
                terminal_position = position
            elif event_type == "run_terminated":
                if derived_status not in {"ready", "running", "paused"}:
                    add_violation("run_event_order_mismatch")
                candidate_status = str(payload.get("status") or "")
                if candidate_status not in TERMINAL_RUN_STATUSES - {"superseded"}:
                    add_violation("run_status_mismatch")
                else:
                    derived_status = candidate_status
                terminal_event = event
                terminal_position = position
            elif event_type not in {
                "step_planned",
                "policy_decided",
                "attempt_reserved",
                "tool_dispatched",
                "tool_observed",
                "proposal_recorded",
                "mutation_committed",
                "attempt_settled",
                "attempt_uncertain",
                "step_completed",
            }:
                add_violation("event_type_unknown")
            elif derived_status != "running":
                add_violation("run_event_order_mismatch")

        if not run_event_started:
            add_violation("run_event_order_mismatch")
        if derived_status != str(run.get("status") or ""):
            add_violation("run_status_mismatch")

        for step in steps:
            step_id = str(step["step_id"])
            ordinal = int(step["ordinal"])
            positioned = event_positions_by_step.get(step_id, [])
            positions_by_type: dict[str, list[int]] = {}
            events_by_type: dict[str, list[Mapping[str, Any]]] = {}
            for position, event in positioned:
                event_type = str(event.get("type") or "")
                positions_by_type.setdefault(event_type, []).append(position)
                events_by_type.setdefault(event_type, []).append(event)
                payload = event.get("payload") or {}
                if "ordinal" in payload and int(payload["ordinal"]) != ordinal:
                    add_violation("event_step_mismatch")

            attempt_event_types = {
                "attempt_reserved",
                "attempt_settled",
                "attempt_uncertain",
                "tool_dispatched",
            }
            actual_attempt_events = [
                event
                for _, event in positioned
                if str(event.get("type") or "") in attempt_event_types
            ]
            actual_attempt_keys = [
                str(event.get("event_key") or "")
                for event in actual_attempt_events
            ]
            expected_attempt_keys: list[str] = []
            expected_chain_by_call: list[list[str]] = []
            for attempt in attempts_by_step.get(step_id, []):
                call_key = str(attempt["call_key"])
                state = str(attempt.get("state") or "")
                kind = str(attempt.get("kind") or "")
                chain = [f"{call_key}-reserved"]
                if kind == "tool" and state in {
                    "dispatched",
                    "settled",
                    "uncertain",
                    "resolved_retry",
                    "resolved_skip",
                }:
                    chain.append(f"{call_key}-dispatched")
                if state == "settled":
                    chain.append(f"{call_key}-settled")
                elif state in {"uncertain", "resolved_retry", "resolved_skip"}:
                    chain.append(f"{call_key}-uncertain")
                expected_attempt_keys.extend(chain)
                expected_chain_by_call.append(chain)
            if (
                len(actual_attempt_keys) != len(set(actual_attempt_keys))
                or set(actual_attempt_keys) != set(expected_attempt_keys)
            ):
                add_violation("attempt_event_identity_mismatch")
            event_position_by_key = {
                str(event.get("event_key") or ""): position
                for position, event in positioned
                if str(event.get("type") or "") in attempt_event_types
            }
            prior_attempt_position: int | None = None
            for chain in expected_chain_by_call:
                chain_positions = [
                    event_position_by_key[key]
                    for key in chain
                    if key in event_position_by_key
                ]
                if len(chain_positions) != len(chain):
                    continue
                if chain_positions != sorted(chain_positions):
                    add_violation("step_event_order_mismatch")
                if (
                    prior_attempt_position is not None
                    and chain_positions[0] <= prior_attempt_position
                ):
                    add_violation("step_event_order_mismatch")
                prior_attempt_position = chain_positions[-1]

            decision: PlannerDecision | None = None
            try:
                decision = PlannerDecision.model_validate(step.get("planner_decision"))
            except Exception:
                effective_status = (
                    str(step.get("paused_from_status") or "")
                    if str(step.get("status") or "") == "paused"
                    else str(step.get("status") or "")
                )
                failed_before_decision = (
                    effective_status == "failed"
                    and step.get("failure_reason") in {
                        "deadline_exceeded",
                        "planner_output_exhausted",
                    }
                )
                if effective_status != "planning" and not failed_before_decision:
                    add_violation("planner_decision_mismatch")

            planned_positions = positions_by_type.get("step_planned", [])
            policy_positions = positions_by_type.get("policy_decided", [])
            completed_positions = positions_by_type.get("step_completed", [])
            planned_position = planned_positions[0] if planned_positions else None
            policy_position = policy_positions[0] if policy_positions else None
            last_planner_reservation: int | None = None
            last_planner_settlement: int | None = None
            last_tool_reservation: int | None = None
            last_tool_dispatch: int | None = None
            last_tool_settlement: int | None = None
            last_tool_uncertain: int | None = None
            for position, event in positioned:
                event_type = str(event.get("type") or "")
                payload = event.get("payload") or {}
                if event_type == "attempt_reserved":
                    if payload.get("kind") == "planner":
                        if planned_position is not None and position > planned_position:
                            add_violation("step_event_order_mismatch")
                        last_planner_reservation = position
                    elif payload.get("kind") == "tool":
                        if policy_position is None or position <= policy_position:
                            add_violation("step_event_order_mismatch")
                        last_tool_reservation = position
                elif event_type == "attempt_settled":
                    if policy_position is not None and position > policy_position:
                        if (
                            last_tool_reservation is None
                            or last_tool_reservation >= position
                        ):
                            add_violation("step_event_order_mismatch")
                        last_tool_settlement = position
                    else:
                        if (
                            last_planner_reservation is None
                            or last_planner_reservation >= position
                        ):
                            add_violation("step_event_order_mismatch")
                        last_planner_settlement = position
                elif event_type == "step_planned":
                    if (
                        last_planner_settlement is None
                        or last_planner_settlement >= position
                    ):
                        add_violation("step_event_order_mismatch")
                elif event_type == "policy_decided":
                    if planned_position is None or position <= planned_position:
                        add_violation("step_event_order_mismatch")
                elif event_type == "tool_dispatched":
                    if (
                        policy_position is None
                        or position <= policy_position
                        or last_tool_reservation is None
                        or last_tool_reservation >= position
                    ):
                        add_violation("step_event_order_mismatch")
                    last_tool_dispatch = position
                elif event_type == "attempt_uncertain":
                    if policy_position is not None and position > policy_position:
                        if (
                            last_tool_dispatch is None
                            or last_tool_dispatch >= position
                        ):
                            add_violation("step_event_order_mismatch")
                        last_tool_uncertain = position
                    elif (
                        last_planner_reservation is None
                        or last_planner_reservation >= position
                    ):
                        add_violation("step_event_order_mismatch")
                elif event_type == "tool_observed":
                    if (
                        last_tool_dispatch is None
                        or last_tool_dispatch >= position
                        or last_tool_settlement is None
                        or last_tool_settlement >= position
                        or (
                            last_tool_uncertain is not None
                            and last_tool_uncertain > last_tool_settlement
                        )
                    ):
                        add_violation("step_event_order_mismatch")
            if decision is not None:
                if len(planned_positions) != 1:
                    add_violation("step_event_order_mismatch")
                else:
                    planned_payload = events_by_type["step_planned"][0].get(
                        "payload"
                    ) or {}
                    if (
                        planned_payload.get("decision_kind") != decision.kind
                        or planned_payload.get("decision_digest")
                        != _digest(decision.model_dump(mode="json"))
                    ):
                        add_violation("planner_decision_mismatch")

                stored_policy = step.get("policy_decision")
                expected_policy: dict[str, Any] | None = None
                policy_rejected = False
                if decision.kind == "propose_finish":
                    expected_policy = {
                        "allowed": True,
                        "reason_code": "finish_proposal_allowed",
                    }
                else:
                    assert decision.tool is not None
                    descriptor = next((
                        item
                        for item in authorization.get("tools") or []
                        if (item.get("reference") or {})
                        == decision.tool.model_dump(mode="json")
                    ), None)
                    if not isinstance(descriptor, Mapping):
                        policy_rejected = True
                    else:
                        try:
                            expected_policy = (
                                self._policy_gate.authorize_tool_snapshot(
                                    authorization=authorization,
                                    decision=decision,
                                    descriptor=descriptor,
                                )
                            )
                        except (AgentRuntimePolicyViolation, ValueError):
                            policy_rejected = True

                if policy_rejected:
                    if not (
                        isinstance(stored_policy, Mapping)
                        and stored_policy.get("allowed") is False
                        and stored_policy.get("reason_code") == "policy_violation"
                    ):
                        add_violation("policy_decision_mismatch")
                elif stored_policy != expected_policy:
                    add_violation("policy_decision_mismatch")

                if len(policy_positions) != 1:
                    add_violation("step_event_order_mismatch")
                else:
                    policy_payload = events_by_type["policy_decided"][0].get(
                        "payload"
                    ) or {}
                    if (
                        policy_payload.get("decision_kind") != decision.kind
                        or policy_payload.get("allowed")
                        != bool((stored_policy or {}).get("allowed"))
                        or policy_payload.get("policy_digest")
                        != _digest(stored_policy or {})
                    ):
                        add_violation("policy_decision_mismatch")
                    if (
                        planned_positions
                        and policy_positions[0] <= planned_positions[0]
                    ):
                        add_violation("step_event_order_mismatch")

                if decision.kind == "call_tool":
                    invocation = step.get("tool_invocation")
                    if isinstance(invocation, Mapping):
                        if (
                            invocation.get("tool")
                            != decision.tool.model_dump(mode="json")
                            or invocation.get("scope")
                            != decision.scope.model_dump(mode="json")
                            or invocation.get("idempotency_key")
                            != f"{run_id}:{step_id}:tool"
                        ):
                            add_violation("tool_invocation_mismatch")
                        for dispatched_event in events_by_type.get(
                            "tool_dispatched",
                            [],
                        ):
                            dispatched_payload = dispatched_event.get(
                                "payload"
                            ) or {}
                            if (
                                dispatched_payload.get("tool_name")
                                != decision.tool.name
                                or dispatched_payload.get("tool_version")
                                != decision.tool.version
                                or dispatched_payload.get("invocation_digest")
                                != _digest(invocation)
                            ):
                                add_violation("tool_invocation_mismatch")
                    elif str(step.get("status") or "") in {
                        "executing",
                        "observed",
                        "completed",
                    }:
                        add_violation("tool_invocation_mismatch")

                    observation = step.get("observation")
                    if isinstance(observation, Mapping):
                        try:
                            validated_observation = RuntimeObservation.model_validate(
                                observation
                            )
                            expected_observation_id = _digest({
                                "run_id": run_id,
                                "step_id": step_id,
                                "tool": decision.tool.model_dump(mode="json"),
                            })[:32]
                            if (
                                validated_observation.observation_id
                                != expected_observation_id
                                or validated_observation.step_id != step_id
                                or validated_observation.tool != decision.tool
                            ):
                                add_violation("observation_mismatch")
                            observed_events = events_by_type.get(
                                "tool_observed",
                                [],
                            )
                            if len(observed_events) != 1:
                                add_violation("observation_mismatch")
                            else:
                                observed_payload = observed_events[0].get(
                                    "payload"
                                ) or {}
                                if (
                                    observed_payload.get("status")
                                    != validated_observation.status
                                    or observed_payload.get("code")
                                    != validated_observation.code
                                    or observed_payload.get("observation_digest")
                                    != _digest(observation)
                                ):
                                    add_violation("observation_mismatch")
                        except Exception:
                            add_violation("observation_mismatch")
                    elif str(step.get("status") or "") in {"observed", "completed"}:
                        add_violation("observation_mismatch")

                    dispatched = positions_by_type.get("tool_dispatched", [])
                    observed = positions_by_type.get("tool_observed", [])
                    if observed and (
                        not dispatched or observed[0] <= dispatched[-1]
                    ):
                        add_violation("step_event_order_mismatch")
                else:
                    if positions_by_type.get("tool_dispatched") or positions_by_type.get(
                        "tool_observed"
                    ):
                        add_violation("step_event_order_mismatch")
                    observation = step.get("observation")
                    if isinstance(observation, Mapping):
                        try:
                            completion = CompletionDecision.model_validate(
                                observation.get("completion")
                            )
                            if observation.get("status") != "finish_evaluated":
                                add_violation("observation_mismatch")
                        except Exception:
                            add_violation("observation_mismatch")
                    elif str(step.get("status") or "") in {"observed", "completed"}:
                        add_violation("observation_mismatch")

            derived_step_status = "planning"
            if policy_positions:
                policy_payload = events_by_type["policy_decided"][0].get(
                    "payload"
                ) or {}
                derived_step_status = (
                    "policy_checked"
                    if policy_payload.get("allowed") is True
                    else "failed"
                )
            if positions_by_type.get("tool_dispatched"):
                derived_step_status = "executing"
            if positions_by_type.get("tool_observed"):
                derived_step_status = "observed"
            if completed_positions:
                derived_step_status = "completed"
                if len(completed_positions) != 1:
                    add_violation("step_event_order_mismatch")
                if policy_positions and completed_positions[0] <= policy_positions[0]:
                    add_violation("step_event_order_mismatch")
                completed_payload = events_by_type["step_completed"][0].get(
                    "payload"
                ) or {}
                expected_kind = (
                    "finish"
                    if decision is not None and decision.kind == "propose_finish"
                    else "tool"
                )
                if (
                    completed_payload.get("kind") != expected_kind
                    or completed_payload.get("step_digest")
                    != _digest(_step_audit_projection(step))
                ):
                    add_violation("step_status_mismatch")

            persisted_step_status = str(step.get("status") or "")
            if persisted_step_status == "paused":
                matching_pause = any(
                    event.get("type") == "run_paused"
                    and str(event.get("step_id") or "") == step_id
                    for event in events
                )
                if not matching_pause:
                    add_violation("step_status_mismatch")
            elif persisted_step_status == "failed":
                matching_failure = any(
                    event.get("type") == "run_terminated"
                    and str(event.get("step_id") or "") == step_id
                    and (event.get("payload") or {}).get("status") == "failed"
                    for event in events
                )
                if not matching_failure:
                    add_violation("step_status_mismatch")
            elif persisted_step_status != derived_step_status:
                add_violation("step_status_mismatch")

        attempts = [
            item for item in run.get("attempts") or []
            if item.get("state") != "released_pre_dispatch"
        ]
        accounted = [
            item
            for item in attempts
            if item.get("state") in {
                "settled",
                "resolved_retry",
                "resolved_skip",
            }
        ]
        derived_usage = AgentRuntimeUsage(
            planner_calls=sum(item.get("kind") == "planner" for item in attempts),
            tool_calls=sum(item.get("kind") == "tool" for item in attempts),
            paid_attempts=sum(
                int((item.get("usage") or {}).get("paid_attempts") or 0)
                for item in accounted
            ),
            input_tokens=sum(
                int((item.get("usage") or {}).get("input_tokens") or 0)
                for item in accounted
            ),
            output_tokens=sum(
                int((item.get("usage") or {}).get("output_tokens") or 0)
                for item in accounted
            ),
            total_tokens=sum(
                int((item.get("usage") or {}).get("total_tokens") or 0)
                for item in accounted
            ),
        )
        persisted_usage = AgentRuntimeUsage.model_validate(run.get("usage") or {})
        if derived_usage != persisted_usage:
            add_violation("usage_mismatch")
        termination = run.get("termination")
        if isinstance(termination, Mapping):
            if termination.get("status") != run.get("status"):
                add_violation("termination_status_mismatch")
            if terminal_event is None:
                add_violation("termination_event_missing")
            else:
                terminal_payload = terminal_event.get("payload") or {}
                if (
                    terminal_payload.get("status") != termination.get("status")
                    or terminal_payload.get("reason_code")
                    != termination.get("reason_code")
                ):
                    add_violation("termination_status_mismatch")
        elif run.get("status") in TERMINAL_RUN_STATUSES or run.get("status") == "paused":
            add_violation("termination_missing")

        completed_finish = any(
            (step.get("planner_decision") or {}).get("kind")
            == "propose_finish"
            and step.get("status") == "completed"
            and (
                ((step.get("observation") or {}).get("completion") or {}).get(
                    "satisfied"
                )
                is True
            )
            for step in steps
        )
        if (derived_status == "completed") != completed_finish:
            add_violation("completion_condition_mismatch")
        return AgentReplayView(
            run_id=str(run["_id"]),
            consistent=not violations,
            violations=tuple(violations),
            derived_status=derived_status,
            derived_usage=derived_usage,
            source_input_digest=self._source_input_digest(run, steps),
        )

    async def _execute_owned_run(
        self,
        *,
        run_id: str,
        owner_id: str,
        resumed: bool,
        uncertain_action: Literal["retry", "skip"] | None = None,
        conditions_confirmed: bool = False,
    ) -> AgentRunView:
        run = await self._repository.get_run_owned(
            run_id=run_id,
            owner_id=owner_id,
        )
        self._verify_runtime_snapshot(
            run.get("authorization") or {},
            authorization_digest=str(run.get("authorization_digest") or ""),
        )
        now = _aware(self._clock())

        worker_id = uuid4().hex
        leased = await self._repository.acquire_lease(
            run_id=run_id,
            owner_id=owner_id,
            worker_id=worker_id,
            now=now,
            expires_at=now + timedelta(seconds=self._lease_seconds),
        )
        lease_epoch = int(leased.get("lease_epoch") or 0)
        try:
            run = await self._repair_checkpoint_projection(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                now=now,
            )
            await self._repair_projection_audit(
                run_id=run_id,
                owner_id=owner_id,
                now=now,
            )
            if run.get("status") in TERMINAL_RUN_STATUSES:
                return await self._run_view(run_id=run_id, owner_id=owner_id)
            if run.get("status") == "paused":
                reason_code = str(
                    (run.get("termination") or {}).get("reason_code") or ""
                )
                if reason_code == "uncertain_paid_attempt" and uncertain_action is None:
                    return await self._run_view(run_id=run_id, owner_id=owner_id)
                if reason_code in {
                    "authorization_required",
                    "manual_approval_required",
                } and not conditions_confirmed:
                    return await self._run_view(run_id=run_id, owner_id=owner_id)
                if reason_code not in RESUMABLE_PAUSE_REASONS:
                    return await self._run_view(run_id=run_id, owner_id=owner_id)
                should_continue = await self._resume_paused_checkpoint(
                    run=run,
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                    uncertain_action=uncertain_action,
                    now=now,
                )
                if not should_continue:
                    return await self._run_view(run_id=run_id, owner_id=owner_id)
            await self._repository.set_run_status(
                run_id=run_id,
                owner_id=owner_id,
                expected=("ready", "running", "paused"),
                status="running",
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                now=now,
                fields={"termination": None, "termination_event_key": None},
            )
            if not resumed:
                await self._event(
                    run_id=run_id,
                    event_key="run-created",
                    event_type="run_created",
                    payload={"status": "ready"},
                    now=now,
                )
                await self._event(
                    run_id=run_id,
                    event_key="readiness-bound",
                    event_type="readiness_bound",
                    payload={"status": "bound"},
                    now=now,
                )
            await self._event(
                run_id=run_id,
                event_key=(
                    f"run-resumed-{lease_epoch}"
                    if resumed
                    else "run-started"
                ),
                event_type="run_resumed" if resumed else "run_started",
                payload={"status": "running"},
                now=now,
            )
            await self._run_loop(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
            )
        except AgentRuntimeStateConflict:
            current = await self._repository.get_run_owned(
                run_id=run_id,
                owner_id=owner_id,
            )
            if (
                current.get("status") == "running"
                and int(current.get("lease_epoch") or 0) == lease_epoch
            ):
                raise
        finally:
            await self._repository.release_lease(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                now=_aware(self._clock()),
            )
        return await self._run_view(run_id=run_id, owner_id=owner_id)

    async def _repair_checkpoint_projection(
        self,
        *,
        run_id: str,
        owner_id: str,
        worker_id: str,
        lease_epoch: int,
        now: datetime,
    ) -> dict[str, Any]:
        """Finish a run projection whose active step checkpoint committed first."""
        run = await self._repository.get_run_owned(
            run_id=run_id,
            owner_id=owner_id,
        )
        if run.get("status") in TERMINAL_RUN_STATUSES:
            return run
        active_step_id = str(run.get("active_step_id") or "")
        if not active_step_id:
            if run.get("status") != "running":
                return run
            steps = await self._repository.list_steps_owned(
                run_id=run_id,
                owner_id=owner_id,
            )
            if not steps:
                return run
            latest = steps[-1]
            if (
                latest.get("status") != "failed"
                or int(latest.get("ordinal") or 0)
                != int(run.get("next_ordinal") or 0) - 1
            ):
                return run
            reason_code = str(latest.get("failure_reason") or "")
            if not reason_code:
                raise AgentRuntimeStateConflict(
                    "failed Agent step is missing its termination reason"
                )
            return await self._repository.set_run_status(
                run_id=run_id,
                owner_id=owner_id,
                expected=("running",),
                status="failed",
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                now=now,
                fields={
                    "termination": _termination_projection(
                        status="failed",
                        reason_code=reason_code,
                        now=now,
                        step_id=str(latest["step_id"]),
                    ),
                    "termination_event_key": f"run-failed-{reason_code}",
                },
            )
        step = await self._repository.get_step_owned(
            run_id=run_id,
            owner_id=owner_id,
            step_id=active_step_id,
        )
        if step.get("status") != "paused" or run.get("status") == "paused":
            return run
        reason_code = str(step.get("pause_reason") or "")
        if not reason_code:
            raise AgentRuntimeStateConflict(
                "paused Agent step is missing its termination reason"
            )
        pause_epoch = int(step.get("pause_lease_epoch") or lease_epoch)
        pause_event_key, _, _ = _termination_event_projection(
            status="paused",
            reason_code=reason_code,
            lease_epoch=pause_epoch,
        )
        return await self._repository.set_run_status(
            run_id=run_id,
            owner_id=owner_id,
            expected=(str(run.get("status") or ""),),
            status="paused",
            worker_id=worker_id,
            lease_epoch=lease_epoch,
            now=now,
            fields={
                "termination": _termination_projection(
                    status="paused",
                    reason_code=reason_code,
                    now=now,
                    step_id=active_step_id,
                ),
                "termination_event_key": pause_event_key,
            },
        )

    async def _resume_paused_checkpoint(
        self,
        *,
        run: Mapping[str, Any],
        run_id: str,
        owner_id: str,
        worker_id: str,
        lease_epoch: int,
        uncertain_action: Literal["retry", "skip"] | None,
        now: datetime,
    ) -> bool:
        step_id = str(run.get("active_step_id") or "")
        termination = dict(run.get("termination") or {})
        if not step_id:
            if termination.get("reason_code") == "concurrent_narrative_change":
                return True
            raise AgentRuntimeStateConflict("paused Agent run has no active step")
        step = await self._repository.get_step_owned(
            run_id=run_id,
            owner_id=owner_id,
            step_id=step_id,
        )
        if step.get("status") != "paused":
            raise AgentRuntimeStateConflict("paused Agent run checkpoint is inconsistent")
        paused_from = str(step.get("paused_from_status") or "")
        if paused_from not in {"planning", "policy_checked", "executing", "observed"}:
            raise AgentRuntimeStateConflict("paused Agent step has no resumable checkpoint")

        if termination.get("reason_code") == "uncertain_paid_attempt":
            if uncertain_action is None:
                return False
            unresolved = [
                dict(item)
                for item in run.get("attempts") or []
                if (
                    isinstance(item, Mapping)
                    and item.get("step_id") == step_id
                    and item.get("state") == "uncertain"
                )
            ]
            resolved = [
                dict(item)
                for item in run.get("attempts") or []
                if (
                    isinstance(item, Mapping)
                    and item.get("step_id") == step_id
                    and item.get("state") in {"resolved_retry", "resolved_skip"}
                )
            ]
            if len(unresolved) == 1:
                attempt = unresolved[0]
                await self._repository.resolve_uncertain_call(
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                    call_key=str(attempt["call_key"]),
                    action=uncertain_action,
                    now=now,
                )
            elif not unresolved and resolved:
                attempt = resolved[-1]
                if attempt.get("state") != f"resolved_{uncertain_action}":
                    raise AgentRuntimeStateConflict(
                        "uncertain Agent call was resolved with another action"
                    )
            else:
                raise AgentRuntimeStateConflict(
                    "uncertain pause must reference exactly one frozen attempt"
                )
            if uncertain_action == "skip":
                await self._fail_step_and_run(
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                    step_id=step_id,
                    expected_step_status="paused",
                    reason_code=(
                        "planner_output_exhausted"
                        if attempt.get("kind") == "planner"
                        else "tool_failure_exhausted"
                    ),
                    now=now,
                )
                return False

        await self._repository.transition_step(
            run_id=run_id,
            owner_id=owner_id,
            worker_id=worker_id,
            lease_epoch=lease_epoch,
            step_id=step_id,
            expected="paused",
            status=paused_from,
            fields={"pause_reason": None, "paused_from_status": None},
            now=now,
        )
        return True

    def _verify_runtime_snapshot(
        self,
        authorization: Mapping[str, Any],
        *,
        authorization_digest: str,
    ) -> None:
        if authorization.get("schema_version") != "agent_runtime_authorization.v1":
            raise AgentRuntimeStateConflict("authorization schema version is unknown")
        if _digest(dict(authorization)) != authorization_digest:
            raise AgentRuntimeStateConflict("authorization digest no longer matches")
        if authorization.get("planner") != self._planner.descriptor.model_dump(mode="json"):
            raise AgentRuntimeStateConflict("planner contract drifted after readiness")
        if authorization.get("tool_registry_revision") != str(
            self._tools.registry_revision
        ):
            raise AgentRuntimeStateConflict("tool registry drifted after readiness")
        if authorization.get("completion_policy_revision") != str(
            self._completion_policy.revision
        ):
            raise AgentRuntimeStateConflict("completion policy drifted after readiness")
        expected_tools = authorization.get("tools") or []
        current_tools = [
            _tool_snapshot(self._tools.describe(RuntimeToolReference.model_validate(
                item.get("reference") or {}
            )))
            for item in expected_tools
        ]
        if current_tools != expected_tools:
            raise AgentRuntimeStateConflict("tool contract drifted after readiness")

    async def _run_loop(
        self,
        *,
        run_id: str,
        owner_id: str,
        worker_id: str,
        lease_epoch: int,
    ) -> None:
        while True:
            run = await self._repository.get_run_owned(
                run_id=run_id,
                owner_id=owner_id,
            )
            if run.get("status") != "running":
                return
            authorization = dict(run.get("authorization") or {})
            authorization_digest = str(run["authorization_digest"])
            now = _aware(self._clock())
            active_step_id = str(run.get("active_step_id") or "")
            if active_step_id:
                active_step = await self._repository.get_step_owned(
                    run_id=run_id,
                    owner_id=owner_id,
                    step_id=active_step_id,
                )
                active_status = str(active_step.get("status") or "")
                if active_status in {"completed", "failed"}:
                    cleared = await self._repository.clear_active_step(
                        run_id=run_id,
                        owner_id=owner_id,
                        worker_id=worker_id,
                        lease_epoch=lease_epoch,
                        step_id=active_step_id,
                        now=now,
                    )
                    if not cleared:
                        raise AgentRuntimeStateConflict(
                            "terminal active Agent step could not be cleared"
                        )
                    ordinal = int(active_step.get("ordinal") or 0)
                    if active_status == "failed":
                        await self._terminate(
                            run_id=run_id,
                            owner_id=owner_id,
                            worker_id=worker_id,
                            lease_epoch=lease_epoch,
                            status="failed",
                            reason_code=str(
                                active_step.get("failure_reason")
                                or "invariant_violation"
                            ),
                            step_id=active_step_id,
                            now=now,
                        )
                        return
                    decision = dict(active_step.get("planner_decision") or {})
                    kind = (
                        "finish"
                        if decision.get("kind") == "propose_finish"
                        else "tool"
                    )
                    await self._event(
                        run_id=run_id,
                        event_key=f"step-{ordinal}-completed",
                        event_type="step_completed",
                        payload={
                            "ordinal": ordinal,
                            "status": "completed",
                            "kind": kind,
                            "step_digest": _digest(
                                _step_audit_projection(active_step)
                            ),
                        },
                        step_id=active_step_id,
                        now=now,
                    )
                    completion = dict(
                        (active_step.get("observation") or {}).get("completion")
                        or {}
                    )
                    if kind == "finish" and completion.get("satisfied") is True:
                        await self._terminate(
                            run_id=run_id,
                            owner_id=owner_id,
                            worker_id=worker_id,
                            lease_epoch=lease_epoch,
                            status="completed",
                            reason_code="goal_satisfied",
                            step_id=active_step_id,
                            now=now,
                        )
                        return
                    continue
            deadline_at = datetime.fromisoformat(str(authorization["deadline_at"]))
            if now >= _aware(deadline_at):
                await self._terminate(
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                    status="failed",
                    reason_code="deadline_exceeded",
                    now=now,
                )
                return
            if not await self._revision_matches(
                owner_id=owner_id,
                authorization=authorization,
            ):
                await self._terminate(
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                    status="paused",
                    reason_code="concurrent_narrative_change",
                    now=now,
                )
                return

            limits = dict(authorization.get("limits") or {})
            if (
                not run.get("active_step_id")
                and int(run.get("next_ordinal") or 0) >= int(limits["max_steps"])
            ):
                await self._terminate(
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                    status="failed",
                    reason_code="max_steps_exhausted",
                    now=now,
                )
                return

            observations = await self._planner_observations(
                run_id=run_id,
                owner_id=owner_id,
            )
            step = await self._repository.claim_step(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                now=now,
                observation_cursor=len(observations),
                observation_digest=_digest(observations),
            )
            step_id = str(step["step_id"])
            ordinal = int(step["ordinal"])
            prepared = await self._prepare_step_decision(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                step=step,
                authorization=authorization,
                authorization_digest=authorization_digest,
                observations=observations,
            )
            if prepared is None:
                return
            decision, descriptor, payload = prepared

            if decision.kind == "call_tool":
                assert descriptor is not None and payload is not None
                finished = await self._act_and_observe(
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                    step_id=step_id,
                    ordinal=ordinal,
                    authorization=authorization,
                    authorization_digest=authorization_digest,
                    decision=decision,
                    descriptor=descriptor,
                    payload=payload,
                    step_status=(
                        "policy_checked"
                        if str(step["status"]) == "planning"
                        else str(step["status"])
                    ),
                )
                if not finished:
                    return
                continue

            completed = await self._observe_finish(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                step_id=step_id,
                ordinal=ordinal,
                decision=decision,
                observations=observations,
                authorization=authorization,
                step_status=(
                    "policy_checked"
                    if str(step["status"]) == "planning"
                    else str(step["status"])
                ),
            )
            if completed:
                return

    async def _prepare_step_decision(
        self,
        *,
        run_id: str,
        owner_id: str,
        worker_id: str,
        lease_epoch: int,
        step: Mapping[str, Any],
        authorization: Mapping[str, Any],
        authorization_digest: str,
        observations: list[dict[str, Any]],
    ) -> tuple[PlannerDecision, RuntimeToolDescriptor | None, Any | None] | None:
        step_id = str(step["step_id"])
        ordinal = int(step["ordinal"])
        step_status = str(step["status"])
        if step_status == "planning":
            try:
                planner_result = await self._plan(
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                    step_id=step_id,
                    ordinal=ordinal,
                    authorization=authorization,
                    authorization_digest=authorization_digest,
                    observations=observations,
                )
            except AgentRuntimeBudgetExceeded:
                await self._pause_step_and_run(
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                    step_id=step_id,
                    expected_step_status="planning",
                    reason_code="budget_exhausted",
                    now=_aware(self._clock()),
                )
                return None
            except _UncertainDispatchedCall:
                await self._pause_step_and_run(
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                    step_id=step_id,
                    expected_step_status="planning",
                    reason_code="uncertain_paid_attempt",
                    now=_aware(self._clock()),
                )
                return None
            except _DeadlineExceeded:
                await self._fail_step_and_run(
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                    step_id=step_id,
                    expected_step_status="planning",
                    reason_code="deadline_exceeded",
                    now=_aware(self._clock()),
                )
                return None
            except Exception:
                await self._fail_step_and_run(
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                    step_id=step_id,
                    expected_step_status="planning",
                    reason_code="planner_output_exhausted",
                    now=_aware(self._clock()),
                )
                return None
            decision = planner_result.decision
        elif step_status in {"policy_checked", "executing", "observed"}:
            try:
                decision = PlannerDecision.model_validate(step.get("planner_decision"))
            except Exception:
                await self._fail_step_and_run(
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                    step_id=step_id,
                    expected_step_status=step_status,
                    reason_code="invariant_violation",
                    now=_aware(self._clock()),
                )
                return None
        else:
            await self._terminate(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                status="failed",
                reason_code="invariant_violation",
                step_id=step_id,
                now=_aware(self._clock()),
            )
            return None

        try:
            if decision.kind == "call_tool":
                assert decision.tool is not None and decision.scope is not None
                descriptor = self._tools.describe(decision.tool)
                payload, policy_decision = self._policy_gate.authorize_tool(
                    authorization=authorization,
                    decision=decision,
                    descriptor=descriptor,
                )
                await self._scope_validator(
                    owner_id=owner_id,
                    novel_id=str(authorization["novel_id"]),
                    scope=decision.scope,
                )
            else:
                descriptor = None
                payload = None
                policy_decision = {
                    "allowed": True,
                    "reason_code": "finish_proposal_allowed",
                }
        except (AgentRuntimePolicyViolation, ValueError, AssertionError):
            policy_decision = {
                "allowed": False,
                "reason_code": "policy_violation",
            }
            await self._repository.transition_step(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                step_id=step_id,
                expected=step_status,
                status="failed",
                fields={
                    "planner_decision": decision.model_dump(mode="json"),
                    "policy_decision": policy_decision,
                    "failure_reason": "policy_violation",
                },
                now=_aware(self._clock()),
            )
            await self._event(
                run_id=run_id,
                event_key=f"step-{ordinal}-policy",
                event_type="policy_decided",
                payload={
                    "ordinal": ordinal,
                    "allowed": False,
                    "decision_kind": decision.kind,
                    "policy_digest": _digest(policy_decision),
                },
                step_id=step_id,
                now=_aware(self._clock()),
            )
            await self._repository.clear_active_step(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                step_id=step_id,
                now=_aware(self._clock()),
            )
            await self._terminate(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                status="failed",
                reason_code="policy_violation",
                step_id=step_id,
                now=_aware(self._clock()),
            )
            return None

        if step_status == "planning":
            await self._repository.transition_step(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                step_id=step_id,
                expected="planning",
                status="policy_checked",
                fields={
                    "planner_decision": decision.model_dump(mode="json"),
                    "policy_decision": policy_decision,
                },
                now=_aware(self._clock()),
            )
            await self._event(
                run_id=run_id,
                event_key=f"step-{ordinal}-policy",
                event_type="policy_decided",
                payload={
                    "ordinal": ordinal,
                    "allowed": True,
                    "decision_kind": decision.kind,
                    "policy_digest": _digest(policy_decision),
                },
                step_id=step_id,
                now=_aware(self._clock()),
            )
        return decision, descriptor, payload

    async def _plan(
        self,
        *,
        run_id: str,
        owner_id: str,
        worker_id: str,
        lease_epoch: int,
        step_id: str,
        ordinal: int,
        authorization: Mapping[str, Any],
        authorization_digest: str,
        observations: list[dict[str, Any]],
    ) -> PlannerResult:
        now = _aware(self._clock())
        await self._heartbeat(
            run_id=run_id,
            owner_id=owner_id,
            worker_id=worker_id,
            lease_epoch=lease_epoch,
            now=now,
        )
        descriptor = self._planner.descriptor
        base_call_key = f"step-{ordinal}-planner"
        run = await self._repository.get_run_owned(
            run_id=run_id,
            owner_id=owner_id,
        )
        attempts = self._step_attempts(run, step_id=step_id, kind="planner")
        latest = attempts[-1] if attempts else None
        if latest is not None and latest.get("state") == "reserved":
            await self._repository.release_call_pre_dispatch(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                call_key=str(latest["call_key"]),
                reason="process_interrupted_before_dispatch",
                now=now,
            )
            latest = dict(latest, state="released_pre_dispatch")
        released_count = sum(
            item.get("state") == "released_pre_dispatch" for item in attempts
        )
        if released_count > int(
            (authorization.get("limits") or {}).get("max_predispatch_retries", 0)
        ):
            raise AgentRuntimeStateConflict("planner predispatch retries are exhausted")
        if latest is not None and latest.get("state") in {"dispatched", "uncertain"}:
            if latest.get("state") == "uncertain":
                raise _UncertainDispatchedCall()
            recover = getattr(self._planner, "recover", None)
            try:
                recovered = (
                    await self._await_adapter(
                        recover(idempotency_key=f"{run_id}:{step_id}:planner"),
                        run_id=run_id,
                        owner_id=owner_id,
                        worker_id=worker_id,
                        lease_epoch=lease_epoch,
                        authorization=authorization,
                    )
                    if callable(recover)
                    else None
                )
            except _DeadlineExceeded:
                await self._mark_uncertain(
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                    step_id=step_id,
                    ordinal=ordinal,
                    call_key=str(latest["call_key"]),
                    now=_aware(self._clock()),
                )
                raise
            if recovered is None:
                await self._mark_uncertain(
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                    step_id=step_id,
                    ordinal=ordinal,
                    call_key=str(latest["call_key"]),
                    now=now,
                )
                raise _UncertainDispatchedCall()
            result = PlannerResult.model_validate(recovered)
            await self._settle_runtime_call(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                step_id=step_id,
                ordinal=ordinal,
                call_key=str(latest["call_key"]),
                result_checkpoint=result.model_dump(mode="json"),
                usage=result.usage.model_dump(mode="python"),
                now=now,
            )
            self._raise_if_deadline_exceeded(authorization)
            await self._record_step_planned(
                run_id=run_id,
                step_id=step_id,
                ordinal=ordinal,
                decision=result.decision,
                now=now,
            )
            return result
        if latest is not None and latest.get("state") == "settled":
            checkpoint = latest.get("result_checkpoint")
            if not isinstance(checkpoint, Mapping):
                raise AgentRuntimeStateConflict("settled planner result is missing")
            if checkpoint.get("error_code") == "planner_output_invalid":
                repair_count = sum(
                    (item.get("result_checkpoint") or {}).get("error_code")
                    == "planner_output_invalid"
                    for item in attempts
                    if isinstance(item.get("result_checkpoint"), Mapping)
                )
                if repair_count > int(
                    (authorization.get("limits") or {}).get(
                        "max_planner_repairs",
                        0,
                    )
                ):
                    raise AgentRuntimeStateConflict("planner repairs are exhausted")
            else:
                result = PlannerResult.model_validate(checkpoint)
                self._raise_if_deadline_exceeded(authorization)
                await self._record_step_planned(
                    run_id=run_id,
                    step_id=step_id,
                    ordinal=ordinal,
                    decision=result.decision,
                    now=now,
                )
                return result

        self._raise_if_deadline_exceeded(authorization)
        call_key = (
            base_call_key
            if not attempts
            else f"{base_call_key}-retry-{len(attempts)}"
        )
        await self._repository.reserve_call(
            run_id=run_id,
            owner_id=owner_id,
            worker_id=worker_id,
            lease_epoch=lease_epoch,
            step_id=step_id,
            call_key=call_key,
            call_kind="planner",
            conservative_paid_attempts=descriptor.max_paid_attempts_per_call,
            conservative_tokens=descriptor.max_tokens_per_call,
            now=now,
        )
        await self._event(
            run_id=run_id,
            event_key=f"{call_key}-reserved",
            event_type="attempt_reserved",
            payload={"ordinal": ordinal, "kind": "planner"},
            step_id=step_id,
            now=now,
        )
        await self._repository.mark_call_dispatched(
            run_id=run_id,
            owner_id=owner_id,
            worker_id=worker_id,
            lease_epoch=lease_epoch,
            call_key=call_key,
            now=now,
        )
        try:
            raw_result = await self._await_adapter(
                self._planner.plan(
                    PlannerInput(
                        goal=str(authorization["goal"]),
                        scope=AgentScope.model_validate(authorization["scope"]),
                        ordinal=ordinal,
                        allowed_tools=tuple(authorization.get("tools") or []),
                        observations=tuple(observations),
                        authorization_digest=authorization_digest,
                    ),
                    idempotency_key=f"{run_id}:{step_id}:planner",
                ),
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                authorization=authorization,
            )
        except _DeadlineExceeded:
            await self._mark_uncertain(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                step_id=step_id,
                ordinal=ordinal,
                call_key=call_key,
                now=_aware(self._clock()),
            )
            raise
        except Exception:
            await self._mark_uncertain(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                step_id=step_id,
                ordinal=ordinal,
                call_key=call_key,
                now=_aware(self._clock()),
            )
            raise _UncertainDispatchedCall()
        try:
            result = PlannerResult.model_validate(raw_result)
        except Exception:
            await self._settle_runtime_call(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                step_id=step_id,
                ordinal=ordinal,
                call_key=call_key,
                usage={},
                result_checkpoint={"error_code": "planner_output_invalid"},
                now=_aware(self._clock()),
            )
            self._raise_if_deadline_exceeded(authorization)
            return await self._plan(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                step_id=step_id,
                ordinal=ordinal,
                authorization=authorization,
                authorization_digest=authorization_digest,
                observations=observations,
            )
        await self._settle_runtime_call(
            run_id=run_id,
            owner_id=owner_id,
            worker_id=worker_id,
            lease_epoch=lease_epoch,
            step_id=step_id,
            ordinal=ordinal,
            call_key=call_key,
            usage=result.usage.model_dump(mode="python"),
            result_checkpoint=result.model_dump(mode="json"),
            now=_aware(self._clock()),
        )
        self._raise_if_deadline_exceeded(authorization)
        await self._record_step_planned(
            run_id=run_id,
            step_id=step_id,
            ordinal=ordinal,
            decision=result.decision,
            now=_aware(self._clock()),
        )
        return result

    async def _act_and_observe(
        self,
        *,
        run_id: str,
        owner_id: str,
        worker_id: str,
        lease_epoch: int,
        step_id: str,
        ordinal: int,
        authorization: Mapping[str, Any],
        authorization_digest: str,
        decision: PlannerDecision,
        descriptor: RuntimeToolDescriptor,
        payload: Any,
        step_status: str,
    ) -> bool:
        now = _aware(self._clock())
        if self._deadline_is_exceeded(authorization):
            await self._fail_step_and_run(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                step_id=step_id,
                expected_step_status=step_status,
                reason_code="deadline_exceeded",
                now=now,
            )
            return False
        if step_status == "observed":
            await self._complete_observed_step(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                step_id=step_id,
                ordinal=ordinal,
                kind="tool",
                now=now,
            )
            return True
        if step_status not in {"policy_checked", "executing"}:
            await self._fail_step_and_run(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                step_id=step_id,
                expected_step_status=step_status,
                reason_code="invariant_violation",
                now=now,
            )
            return False
        if not await self._revision_matches(
            owner_id=owner_id,
            authorization=authorization,
        ):
            await self._pause_step_and_run(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                step_id=step_id,
                expected_step_status=step_status,
                reason_code="concurrent_narrative_change",
                now=now,
            )
            return False
        await self._heartbeat(
            run_id=run_id,
            owner_id=owner_id,
            worker_id=worker_id,
            lease_epoch=lease_epoch,
            now=now,
        )
        assert decision.tool is not None and decision.scope is not None
        invocation = {
            "tool": decision.tool.model_dump(mode="json"),
            "scope": decision.scope.model_dump(mode="json"),
            "arguments": payload.model_dump(mode="json"),
            "idempotency_key": f"{run_id}:{step_id}:tool",
        }
        if step_status == "executing":
            stored_step = await self._repository.get_step_owned(
                run_id=run_id,
                owner_id=owner_id,
                step_id=step_id,
            )
            if stored_step.get("tool_invocation") != invocation:
                await self._fail_step_and_run(
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                    step_id=step_id,
                    expected_step_status="executing",
                    reason_code="invariant_violation",
                    now=now,
                )
                return False

        run = await self._repository.get_run_owned(
            run_id=run_id,
            owner_id=owner_id,
        )
        attempts = self._step_attempts(run, step_id=step_id, kind="tool")
        latest = attempts[-1] if attempts else None
        if latest is not None and latest.get("state") == "reserved":
            await self._repository.release_call_pre_dispatch(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                call_key=str(latest["call_key"]),
                reason="process_interrupted_before_dispatch",
                now=now,
            )
            latest = dict(latest, state="released_pre_dispatch")
        released_count = sum(
            item.get("state") == "released_pre_dispatch" for item in attempts
        )
        if released_count > int(
            (authorization.get("limits") or {}).get("max_predispatch_retries", 0)
        ):
            await self._fail_step_and_run(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                step_id=step_id,
                expected_step_status=step_status,
                reason_code="tool_failure_exhausted",
                now=now,
            )
            return False

        result: RuntimeToolResult | None = None
        if latest is not None and latest.get("state") == "settled":
            checkpoint = latest.get("result_checkpoint")
            if not isinstance(checkpoint, Mapping):
                await self._fail_step_and_run(
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                    step_id=step_id,
                    expected_step_status=step_status,
                    reason_code="invariant_violation",
                    now=now,
                )
                return False
            candidate = RuntimeToolResult.model_validate(checkpoint)
            if candidate.status == "retryable_error":
                retry_count = sum(
                    isinstance(item.get("result_checkpoint"), Mapping)
                    and (item["result_checkpoint"]).get("status")
                    == "retryable_error"
                    for item in attempts
                )
                if (
                    retry_count
                    > int((authorization.get("limits") or {}).get(
                        "max_tool_retries",
                        0,
                    ))
                    or not descriptor.idempotent
                ):
                    await self._fail_step_and_run(
                        run_id=run_id,
                        owner_id=owner_id,
                        worker_id=worker_id,
                        lease_epoch=lease_epoch,
                        step_id=step_id,
                        expected_step_status=step_status,
                        reason_code="tool_failure_exhausted",
                        now=now,
                    )
                    return False
                latest = dict(latest, state="retryable_failure")
            else:
                result = candidate
        if result is None and latest is not None and latest.get("state") in {
            "dispatched",
            "uncertain",
        }:
            call_key = str(latest["call_key"])
            if latest.get("state") == "uncertain":
                await self._pause_step_and_run(
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                    step_id=step_id,
                    expected_step_status=step_status,
                    reason_code="uncertain_paid_attempt",
                    now=now,
                )
                return False
            await self._event(
                run_id=run_id,
                event_key=f"{call_key}-dispatched",
                event_type="tool_dispatched",
                payload={
                    "ordinal": ordinal,
                    "tool_name": decision.tool.name,
                    "tool_version": decision.tool.version,
                    "invocation_digest": _digest(invocation),
                },
                step_id=step_id,
                now=now,
            )
            try:
                recovered = await self._await_adapter(
                    self._tools.recover(
                        idempotency_key=invocation["idempotency_key"],
                    ),
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                    authorization=authorization,
                )
            except _DeadlineExceeded:
                await self._mark_uncertain(
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                    step_id=step_id,
                    ordinal=ordinal,
                    call_key=call_key,
                    now=_aware(self._clock()),
                )
                await self._fail_step_and_run(
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                    step_id=step_id,
                    expected_step_status=step_status,
                    reason_code="deadline_exceeded",
                    now=_aware(self._clock()),
                )
                return False
            except Exception:
                recovered = None
            if recovered is None:
                await self._mark_uncertain(
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                    step_id=step_id,
                    ordinal=ordinal,
                    call_key=call_key,
                    now=now,
                )
                await self._pause_step_and_run(
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                    step_id=step_id,
                    expected_step_status=step_status,
                    reason_code="uncertain_paid_attempt",
                    now=now,
                )
                return False
            result = RuntimeToolResult.model_validate(recovered)
            if result.status == "uncertain":
                await self._mark_uncertain(
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                    step_id=step_id,
                    ordinal=ordinal,
                    call_key=call_key,
                    now=now,
                )
                await self._pause_step_and_run(
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                    step_id=step_id,
                    expected_step_status=step_status,
                    reason_code="uncertain_paid_attempt",
                    now=now,
                )
                return False
            await self._settle_runtime_call(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                step_id=step_id,
                ordinal=ordinal,
                call_key=call_key,
                usage=result.usage.model_dump(mode="python"),
                result_checkpoint=result.model_dump(mode="json"),
                now=now,
            )
        elif result is None:
            base_call_key = f"step-{ordinal}-tool"
            call_key = (
                base_call_key
                if not attempts
                else f"{base_call_key}-retry-{len(attempts)}"
            )
            try:
                await self._repository.reserve_call(
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                    step_id=step_id,
                    call_key=call_key,
                    call_kind="tool",
                    conservative_paid_attempts=descriptor.max_paid_attempts_per_call,
                    conservative_tokens=descriptor.max_tokens_per_call,
                    now=now,
                )
            except AgentRuntimeBudgetExceeded:
                await self._pause_step_and_run(
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                    step_id=step_id,
                    expected_step_status=step_status,
                    reason_code="budget_exhausted",
                    now=now,
                )
                return False
            await self._event(
                run_id=run_id,
                event_key=f"{call_key}-reserved",
                event_type="attempt_reserved",
                payload={"ordinal": ordinal, "kind": "tool"},
                step_id=step_id,
                now=now,
            )
            if step_status == "policy_checked":
                await self._repository.transition_step(
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                    step_id=step_id,
                    expected="policy_checked",
                    status="executing",
                    fields={"tool_invocation": invocation},
                    now=now,
                )
                step_status = "executing"
            await self._repository.mark_call_dispatched(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                call_key=call_key,
                now=now,
            )
            await self._event(
                run_id=run_id,
                event_key=f"{call_key}-dispatched",
                event_type="tool_dispatched",
                payload={
                    "ordinal": ordinal,
                    "tool_name": decision.tool.name,
                    "tool_version": decision.tool.version,
                    "invocation_digest": _digest(invocation),
                },
                step_id=step_id,
                now=now,
            )
            try:
                raw_result = await self._await_adapter(
                    self._tools.execute(
                        decision.tool,
                        payload,
                        context=RuntimeToolContext(
                            owner_id=owner_id,
                            novel_id=str(authorization["novel_id"]),
                            run_id=run_id,
                            step_id=step_id,
                            scope=decision.scope,
                            authorization_digest=authorization_digest,
                        ),
                        idempotency_key=invocation["idempotency_key"],
                    ),
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                    authorization=authorization,
                )
                result = RuntimeToolResult.model_validate(raw_result)
            except _DeadlineExceeded:
                await self._mark_uncertain(
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                    step_id=step_id,
                    ordinal=ordinal,
                    call_key=call_key,
                    now=_aware(self._clock()),
                )
                await self._fail_step_and_run(
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                    step_id=step_id,
                    expected_step_status="executing",
                    reason_code="deadline_exceeded",
                    now=_aware(self._clock()),
                )
                return False
            except Exception:
                await self._mark_uncertain(
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                    step_id=step_id,
                    ordinal=ordinal,
                    call_key=call_key,
                    now=_aware(self._clock()),
                )
                await self._pause_step_and_run(
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                    step_id=step_id,
                    expected_step_status="executing",
                    reason_code="uncertain_paid_attempt",
                    now=_aware(self._clock()),
                )
                return False
            if result.status == "uncertain":
                await self._mark_uncertain(
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                    step_id=step_id,
                    ordinal=ordinal,
                    call_key=call_key,
                    now=_aware(self._clock()),
                )
                await self._pause_step_and_run(
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                    step_id=step_id,
                    expected_step_status="executing",
                    reason_code="uncertain_paid_attempt",
                    now=_aware(self._clock()),
                )
                return False
            await self._settle_runtime_call(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                step_id=step_id,
                ordinal=ordinal,
                call_key=call_key,
                usage=result.usage.model_dump(mode="python"),
                result_checkpoint=result.model_dump(mode="json"),
                now=_aware(self._clock()),
            )
            if result.status == "retryable_error":
                return await self._act_and_observe(
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                    step_id=step_id,
                    ordinal=ordinal,
                    authorization=authorization,
                    authorization_digest=authorization_digest,
                    decision=decision,
                    descriptor=descriptor,
                    payload=payload,
                    step_status="executing",
                )

        assert result is not None
        if result.status == "retryable_error":
            return await self._act_and_observe(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                step_id=step_id,
                ordinal=ordinal,
                authorization=authorization,
                authorization_digest=authorization_digest,
                decision=decision,
                descriptor=descriptor,
                payload=payload,
                step_status="executing",
            )
        if result.status == "uncertain":
            await self._pause_step_and_run(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                step_id=step_id,
                expected_step_status="executing",
                reason_code="uncertain_paid_attempt",
                now=_aware(self._clock()),
            )
            return False
        if self._deadline_is_exceeded(authorization):
            await self._fail_step_and_run(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                step_id=step_id,
                expected_step_status="executing",
                reason_code="deadline_exceeded",
                now=_aware(self._clock()),
            )
            return False
        try:
            validated_output = descriptor.output_schema.model_validate(result.data)
        except Exception:
            await self._fail_step_and_run(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                step_id=step_id,
                expected_step_status="executing",
                reason_code="invariant_violation",
                now=_aware(self._clock()),
            )
            return False
        observation = RuntimeObservation(
            observation_id=_digest({
                "run_id": run_id,
                "step_id": step_id,
                "tool": decision.tool.model_dump(mode="json"),
            })[:32],
            step_id=step_id,
            tool=decision.tool,
            status=result.status,
            code=result.code,
            data=validated_output.model_dump(mode="json"),
            planner_view=result.planner_view,
            audit_view=result.audit_view,
            evidence_refs=result.evidence_refs,
            resource_revision=result.resource_revision,
            resource_digest=result.resource_digest,
            usage=result.usage,
            error_summary=result.error_summary,
        ).model_dump(mode="json")
        await self._repository.transition_step(
            run_id=run_id,
            owner_id=owner_id,
            worker_id=worker_id,
            lease_epoch=lease_epoch,
            step_id=step_id,
            expected="executing",
            status="observed",
            fields={
                "observation": observation,
                "usage_delta": result.usage.model_dump(mode="json"),
            },
            now=_aware(self._clock()),
        )
        await self._event(
            run_id=run_id,
            event_key=f"step-{ordinal}-tool-observed",
            event_type="tool_observed",
            payload={
                "ordinal": ordinal,
                "status": result.status,
                "code": result.code,
                "observation_digest": _digest(observation),
            },
            step_id=step_id,
            now=_aware(self._clock()),
        )
        if result.status == "blocked":
            await self._pause_step_and_run(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                step_id=step_id,
                expected_step_status="observed",
                reason_code="authorization_required",
                now=_aware(self._clock()),
            )
            return False
        if result.status == "permanent_error":
            await self._fail_step_and_run(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                step_id=step_id,
                expected_step_status="observed",
                reason_code="tool_failure_exhausted",
                now=_aware(self._clock()),
            )
            return False
        await self._complete_observed_step(
            run_id=run_id,
            owner_id=owner_id,
            worker_id=worker_id,
            lease_epoch=lease_epoch,
            step_id=step_id,
            ordinal=ordinal,
            kind="tool",
            now=_aware(self._clock()),
        )
        return True

    async def _observe_finish(
        self,
        *,
        run_id: str,
        owner_id: str,
        worker_id: str,
        lease_epoch: int,
        step_id: str,
        ordinal: int,
        decision: PlannerDecision,
        observations: list[dict[str, Any]],
        authorization: Mapping[str, Any],
        step_status: str,
    ) -> bool:
        if self._deadline_is_exceeded(authorization):
            await self._fail_step_and_run(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                step_id=step_id,
                expected_step_status=step_status,
                reason_code="deadline_exceeded",
                now=_aware(self._clock()),
            )
            return True
        if step_status == "policy_checked":
            await self._repository.transition_step(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                step_id=step_id,
                expected="policy_checked",
                status="executing",
                fields={"tool_invocation": None},
                now=_aware(self._clock()),
            )
            step_status = "executing"
        if step_status == "executing":
            try:
                run_snapshot = await self._repository.get_run_owned(
                    run_id=run_id,
                    owner_id=owner_id,
                )
                completion = CompletionDecision.model_validate(
                    await self._await_adapter(
                        self._completion_policy.evaluate(
                            run=run_snapshot,
                            observations=observations,
                            proposal=decision.model_dump(mode="json"),
                        ),
                        run_id=run_id,
                        owner_id=owner_id,
                        worker_id=worker_id,
                        lease_epoch=lease_epoch,
                        authorization=authorization,
                    )
                )
            except _DeadlineExceeded:
                await self._fail_step_and_run(
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                    step_id=step_id,
                    expected_step_status="executing",
                    reason_code="deadline_exceeded",
                    now=_aware(self._clock()),
                )
                return True
            except Exception:
                await self._fail_step_and_run(
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                    step_id=step_id,
                    expected_step_status="executing",
                    reason_code="invariant_violation",
                    now=_aware(self._clock()),
                )
                return True
            if self._deadline_is_exceeded(authorization):
                await self._fail_step_and_run(
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                    step_id=step_id,
                    expected_step_status="executing",
                    reason_code="deadline_exceeded",
                    now=_aware(self._clock()),
                )
                return True
            await self._repository.transition_step(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                step_id=step_id,
                expected="executing",
                status="observed",
                fields={"observation": {
                    "status": "finish_evaluated",
                    "code": decision.finish_code,
                    "completion": completion.model_dump(mode="json"),
                }},
                now=_aware(self._clock()),
            )
        elif step_status == "observed":
            stored_step = await self._repository.get_step_owned(
                run_id=run_id,
                owner_id=owner_id,
                step_id=step_id,
            )
            completion = CompletionDecision.model_validate(
                (stored_step.get("observation") or {}).get("completion")
            )
        else:
            await self._fail_step_and_run(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                step_id=step_id,
                expected_step_status=step_status,
                reason_code="invariant_violation",
                now=_aware(self._clock()),
            )
            return True

        await self._complete_observed_step(
            run_id=run_id,
            owner_id=owner_id,
            worker_id=worker_id,
            lease_epoch=lease_epoch,
            step_id=step_id,
            ordinal=ordinal,
            kind="finish",
            now=_aware(self._clock()),
        )
        if not completion.satisfied:
            return False
        await self._terminate(
            run_id=run_id,
            owner_id=owner_id,
            worker_id=worker_id,
            lease_epoch=lease_epoch,
            status="completed",
            reason_code="goal_satisfied",
            step_id=step_id,
            now=_aware(self._clock()),
        )
        return True

    @staticmethod
    def _step_attempts(
        run: Mapping[str, Any],
        *,
        step_id: str,
        kind: str,
    ) -> list[dict[str, Any]]:
        return [
            dict(item)
            for item in run.get("attempts") or []
            if isinstance(item, Mapping)
            and item.get("step_id") == str(step_id)
            and item.get("kind") == str(kind)
        ]

    async def _settle_runtime_call(
        self,
        *,
        run_id: str,
        owner_id: str,
        worker_id: str,
        lease_epoch: int,
        step_id: str,
        ordinal: int,
        call_key: str,
        usage: Mapping[str, Any],
        result_checkpoint: Mapping[str, Any],
        now: datetime,
    ) -> None:
        await self._repository.settle_call(
            run_id=run_id,
            owner_id=owner_id,
            worker_id=worker_id,
            lease_epoch=lease_epoch,
            call_key=call_key,
            usage=usage,
            result_checkpoint=result_checkpoint,
            now=now,
        )
        await self._event(
            run_id=run_id,
            event_key=f"{call_key}-settled",
            event_type="attempt_settled",
            payload={"ordinal": ordinal},
            step_id=step_id,
            now=now,
        )

    async def _record_step_planned(
        self,
        *,
        run_id: str,
        step_id: str,
        ordinal: int,
        decision: PlannerDecision,
        now: datetime,
    ) -> None:
        await self._event(
            run_id=run_id,
            event_key=f"step-{ordinal}-planned",
            event_type="step_planned",
            payload={
                "ordinal": ordinal,
                "decision_kind": decision.kind,
                "decision_digest": _digest(decision.model_dump(mode="json")),
            },
            step_id=step_id,
            now=now,
        )

    async def _mark_uncertain(
        self,
        *,
        run_id: str,
        owner_id: str,
        worker_id: str,
        lease_epoch: int,
        step_id: str,
        ordinal: int,
        call_key: str,
        now: datetime,
    ) -> None:
        await self._repository.mark_call_uncertain(
            run_id=run_id,
            owner_id=owner_id,
            worker_id=worker_id,
            lease_epoch=lease_epoch,
            call_key=call_key,
            reason="dispatch_outcome_unknown",
            now=now,
        )
        await self._event(
            run_id=run_id,
            event_key=f"{call_key}-uncertain",
            event_type="attempt_uncertain",
            payload={"ordinal": ordinal, "reason_code": "dispatch_outcome_unknown"},
            step_id=step_id,
            now=now,
        )

    async def _complete_observed_step(
        self,
        *,
        run_id: str,
        owner_id: str,
        worker_id: str,
        lease_epoch: int,
        step_id: str,
        ordinal: int,
        kind: str,
        now: datetime,
    ) -> None:
        completed_step = await self._repository.transition_step(
            run_id=run_id,
            owner_id=owner_id,
            worker_id=worker_id,
            lease_epoch=lease_epoch,
            step_id=step_id,
            expected="observed",
            status="completed",
            fields={},
            now=now,
        )
        await self._repository.clear_active_step(
            run_id=run_id,
            owner_id=owner_id,
            worker_id=worker_id,
            lease_epoch=lease_epoch,
            step_id=step_id,
            now=now,
        )
        await self._event(
            run_id=run_id,
            event_key=f"step-{ordinal}-completed",
            event_type="step_completed",
            payload={
                "ordinal": ordinal,
                "status": "completed",
                "kind": kind,
                "step_digest": _digest(
                    _step_audit_projection(completed_step)
                ),
            },
            step_id=step_id,
            now=now,
        )

    async def _revision_matches(
        self,
        *,
        owner_id: str,
        authorization: Mapping[str, Any],
    ) -> bool:
        current = int(await self._revision_reader(
            owner_id=owner_id,
            novel_id=str(authorization["novel_id"]),
        ))
        return current == int(authorization["baseline_narrative_revision"])

    def _deadline_is_exceeded(self, authorization: Mapping[str, Any]) -> bool:
        deadline = datetime.fromisoformat(str(authorization["deadline_at"]))
        return _aware(self._clock()) >= _aware(deadline)

    def _raise_if_deadline_exceeded(
        self,
        authorization: Mapping[str, Any],
    ) -> None:
        if self._deadline_is_exceeded(authorization):
            raise _DeadlineExceeded()

    async def _planner_observations(
        self,
        *,
        run_id: str,
        owner_id: str,
    ) -> list[dict[str, Any]]:
        steps = await self._repository.list_steps_owned(
            run_id=run_id,
            owner_id=owner_id,
        )
        observations: list[dict[str, Any]] = []
        for step in steps:
            observation = step.get("observation")
            if step.get("status") != "completed" or not isinstance(observation, Mapping):
                continue
            planner_view = observation.get("planner_view")
            if isinstance(planner_view, Mapping):
                observations.append(dict(planner_view))
        return observations

    async def _heartbeat(
        self,
        *,
        run_id: str,
        owner_id: str,
        worker_id: str,
        lease_epoch: int,
        now: datetime,
    ) -> None:
        ok = await self._repository.heartbeat_lease(
            run_id=run_id,
            owner_id=owner_id,
            worker_id=worker_id,
            lease_epoch=lease_epoch,
            now=now,
            expires_at=now + timedelta(seconds=self._lease_seconds),
        )
        if not ok:
            raise AgentRuntimeStateConflict("Agent Runtime lease expired")

    async def _await_adapter(
        self,
        operation: Any,
        *,
        run_id: str,
        owner_id: str,
        worker_id: str,
        lease_epoch: int,
        authorization: Mapping[str, Any],
    ) -> Any:
        """Keep the lease alive while an adapter awaits and fence late results."""
        self._raise_if_deadline_exceeded(authorization)
        task = asyncio.create_task(operation)
        interval = max(0.05, min(float(self._lease_seconds) / 3.0, 5.0))
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=interval)
                if done:
                    result = task.result()
                    await self._heartbeat(
                        run_id=run_id,
                        owner_id=owner_id,
                        worker_id=worker_id,
                        lease_epoch=lease_epoch,
                        now=_aware(self._clock()),
                    )
                    return result
                if self._deadline_is_exceeded(authorization):
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                    raise _DeadlineExceeded()
                await self._heartbeat(
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                    now=_aware(self._clock()),
                )
        except BaseException:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            raise

    async def _pause_step_and_run(
        self,
        *,
        run_id: str,
        owner_id: str,
        worker_id: str,
        lease_epoch: int,
        step_id: str,
        expected_step_status: str,
        reason_code: str,
        now: datetime,
    ) -> None:
        await self._repository.transition_step(
            run_id=run_id,
            owner_id=owner_id,
            worker_id=worker_id,
            lease_epoch=lease_epoch,
            step_id=step_id,
            expected=expected_step_status,
            status="paused",
            fields={
                "pause_reason": reason_code,
                "paused_from_status": expected_step_status,
                "pause_lease_epoch": int(lease_epoch),
            },
            now=now,
        )
        await self._terminate(
            run_id=run_id,
            owner_id=owner_id,
            worker_id=worker_id,
            lease_epoch=lease_epoch,
            status="paused",
            reason_code=reason_code,
            step_id=step_id,
            now=now,
        )

    async def _fail_step_and_run(
        self,
        *,
        run_id: str,
        owner_id: str,
        worker_id: str,
        lease_epoch: int,
        step_id: str,
        expected_step_status: str,
        reason_code: str,
        now: datetime,
    ) -> None:
        await self._repository.transition_step(
            run_id=run_id,
            owner_id=owner_id,
            worker_id=worker_id,
            lease_epoch=lease_epoch,
            step_id=step_id,
            expected=expected_step_status,
            status="failed",
            fields={"failure_reason": reason_code},
            now=now,
        )
        await self._repository.clear_active_step(
            run_id=run_id,
            owner_id=owner_id,
            worker_id=worker_id,
            lease_epoch=lease_epoch,
            step_id=step_id,
            now=now,
        )
        await self._terminate(
            run_id=run_id,
            owner_id=owner_id,
            worker_id=worker_id,
            lease_epoch=lease_epoch,
            status="failed",
            reason_code=reason_code,
            step_id=step_id,
            now=now,
        )

    async def _terminate(
        self,
        *,
        run_id: str,
        owner_id: str,
        worker_id: str,
        lease_epoch: int,
        status: str,
        reason_code: str,
        now: datetime,
        step_id: str | None = None,
    ) -> None:
        termination = _termination_projection(
            status=status,
            reason_code=reason_code,
            now=now,
            step_id=step_id,
        )
        event_key, event_type, event_payload = _termination_event_projection(
            status=status,
            reason_code=reason_code,
            lease_epoch=lease_epoch,
        )
        project_agent_runtime_event_payload(event_type, event_payload)
        await self._repository.set_run_status(
            run_id=run_id,
            owner_id=owner_id,
            expected=("ready", "running", "paused"),
            status=status,
            worker_id=worker_id,
            lease_epoch=lease_epoch,
            now=now,
            fields={
                "termination": termination,
                "termination_event_key": event_key,
            },
        )
        await self._event(
            run_id=run_id,
            event_key=event_key,
            event_type=event_type,
            payload=event_payload,
            step_id=step_id,
            now=now,
        )

    async def _event(
        self,
        *,
        run_id: str,
        event_key: str,
        event_type: str,
        payload: Mapping[str, Any],
        now: datetime,
        step_id: str | None = None,
    ) -> None:
        await self._repository.append_event(
            run_id=run_id,
            event_key=event_key,
            event_type=event_type,
            payload=payload,
            now=now,
            step_id=step_id,
        )

    async def _run_view(self, *, run_id: str, owner_id: str) -> AgentRunView:
        run = await self._repository.get_run_owned(
            run_id=run_id,
            owner_id=owner_id,
        )
        steps = await self._repository.list_steps_owned(
            run_id=run_id,
            owner_id=owner_id,
        )
        events = await self._repository.list_events_owned(
            run_id=run_id,
            owner_id=owner_id,
        )
        termination = run.get("termination")
        return AgentRunView(
            run_id=str(run["_id"]),
            status=str(run["status"]),
            authorization_digest=str(run["authorization_digest"]),
            usage=AgentRuntimeUsage.model_validate(run.get("usage") or {}),
            termination=(
                AgentTermination.model_validate(termination)
                if isinstance(termination, Mapping)
                else None
            ),
            steps=tuple(AgentStepView(
                step_id=str(step["step_id"]),
                ordinal=int(step["ordinal"]),
                status=str(step["status"]),
                planner_decision=step.get("planner_decision"),
                policy_decision=step.get("policy_decision"),
                tool_invocation=step.get("tool_invocation"),
                observation=step.get("observation"),
            ) for step in steps),
            events=tuple(AgentEventView(
                event_id=str(event["event_id"]),
                sequence=int(event["sequence"]),
                type=str(event["type"]),
                step_id=event.get("step_id"),
                payload=dict(event.get("payload") or {}),
                created_at=event["created_at"],
            ) for event in events),
            has_uncertain_attempts=bool(run.get("has_uncertain_attempts")),
            predecessor_run_id=(
                str(run["predecessor_run_id"])
                if run.get("predecessor_run_id")
                else None
            ),
            replay_of_run_id=(
                str(run["replay_of_run_id"])
                if run.get("replay_of_run_id")
                else None
            ),
            successor_run_id=(
                str(run["successor_run_id"])
                if run.get("successor_run_id")
                else None
            ),
            lineage_root_run_id=(
                str(run["lineage_root_run_id"])
                if run.get("lineage_root_run_id")
                else None
            ),
        )
