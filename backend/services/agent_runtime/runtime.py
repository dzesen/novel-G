"""Persisted Plan-Act-Observe executor with deny-by-default policy gates."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any, Mapping
from uuid import uuid4

from bson import ObjectId

from backend.db.repositories.agent_runtime_repository import (
    AgentRuntimeBudgetExceeded,
    AgentRuntimeReadinessConflict,
    AgentRuntimeRepository,
    AgentRuntimeStateConflict,
    agent_runtime_repository,
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
    RuntimeToolContext,
    RuntimeToolDescriptor,
    RuntimeToolReference,
    RuntimeToolResult,
)
from backend.services.agent_runtime.policy import (
    AgentRuntimePolicyViolation,
    FORBIDDEN_RUNTIME_CHANGE_CLASSES,
    RuntimePolicyGate,
)


READINESS_TTL_SECONDS = 30 * 60
TERMINAL_RUN_STATUSES = frozenset({
    "completed",
    "failed",
    "cancelled",
    "superseded",
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


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


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
        for reference in request.allowed_tools:
            descriptor = self._tools.describe(reference)
            if descriptor.reference != reference:
                raise ValueError("tool registry returned another tool identity")
            if request.scope.kind not in descriptor.scope_kinds:
                raise ValueError("authorized tool does not support the target scope")
            if descriptor.effect_class not in allowed_effects:
                raise ValueError("authorized tool effect is missing from allowed_effects")
            if descriptor.effect_class == "system_write":
                raise ValueError("Runtime v1 cannot authorize formal system writes")
            if FORBIDDEN_RUNTIME_CHANGE_CLASSES & set(descriptor.change_classes):
                raise ValueError(
                    "Runtime v1 cannot authorize formal or reference-card writes"
                )
            if not set(descriptor.change_classes).issubset(allowed_changes):
                raise ValueError("authorized tool change class is not allowed")
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
            return await self._run_view(run_id=run_id, owner_id=str(owner_id))
        return await self._execute_owned_run(
            run_id=run_id,
            owner_id=str(owner_id),
            resumed=False,
        )

    async def resume(self, *, owner_id: str, run_id: str) -> AgentRunView:
        """Recover a crashed run using only its frozen authorization snapshot."""
        run = await self._repository.get_run_owned(
            run_id=run_id,
            owner_id=str(owner_id),
        )
        if run.get("status") in TERMINAL_RUN_STATUSES or run.get("status") == "paused":
            return await self._run_view(run_id=run_id, owner_id=str(owner_id))
        return await self._execute_owned_run(
            run_id=run_id,
            owner_id=str(owner_id),
            resumed=True,
        )

    async def cancel(self, *, owner_id: str, run_id: str) -> AgentRunView:
        """Idempotently cancel a non-terminal run without dispatching new work."""
        run = await self._repository.get_run_owned(
            run_id=run_id,
            owner_id=str(owner_id),
        )
        if run.get("status") in TERMINAL_RUN_STATUSES:
            return await self._run_view(run_id=run_id, owner_id=str(owner_id))
        await self._terminate(
            run_id=run_id,
            owner_id=str(owner_id),
            status="cancelled",
            reason_code="cancelled_by_user",
            step_id=(str(run["active_step_id"]) if run.get("active_step_id") else None),
            now=_aware(self._clock()),
        )
        return await self._run_view(run_id=run_id, owner_id=str(owner_id))

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
        """Recompute audit invariants without invoking Planner or Tool adapters."""
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
        if [int(item["ordinal"]) for item in steps] != list(range(len(steps))):
            violations.append("step_ordinal_mismatch")
        if [int(item["sequence"]) for item in events] != list(
            range(1, len(events) + 1)
        ):
            violations.append("event_sequence_mismatch")

        attempts = [
            item for item in run.get("attempts") or []
            if item.get("state") != "released_pre_dispatch"
        ]
        settled = [item for item in attempts if item.get("state") == "settled"]
        derived_usage = AgentRuntimeUsage(
            planner_calls=sum(item.get("kind") == "planner" for item in attempts),
            tool_calls=sum(item.get("kind") == "tool" for item in attempts),
            paid_attempts=sum(
                int((item.get("usage") or {}).get("paid_attempts") or 0)
                for item in settled
            ),
            input_tokens=sum(
                int((item.get("usage") or {}).get("input_tokens") or 0)
                for item in settled
            ),
            output_tokens=sum(
                int((item.get("usage") or {}).get("output_tokens") or 0)
                for item in settled
            ),
            total_tokens=sum(
                int((item.get("usage") or {}).get("total_tokens") or 0)
                for item in settled
            ),
        )
        persisted_usage = AgentRuntimeUsage.model_validate(run.get("usage") or {})
        if derived_usage != persisted_usage:
            violations.append("usage_mismatch")
        termination = run.get("termination")
        if isinstance(termination, Mapping):
            if termination.get("status") != run.get("status"):
                violations.append("termination_status_mismatch")
        elif run.get("status") in TERMINAL_RUN_STATUSES or run.get("status") == "paused":
            violations.append("termination_missing")
        return AgentReplayView(
            run_id=str(run["_id"]),
            consistent=not violations,
            violations=tuple(violations),
            derived_status=str(run["status"]),
            derived_usage=derived_usage,
            source_input_digest=self._source_input_digest(run, steps),
        )

    async def _execute_owned_run(
        self,
        *,
        run_id: str,
        owner_id: str,
        resumed: bool,
    ) -> AgentRunView:
        run = await self._repository.get_run_owned(
            run_id=run_id,
            owner_id=owner_id,
        )
        self._verify_runtime_snapshot(run.get("authorization") or {})
        now = _aware(self._clock())

        worker_id = uuid4().hex
        await self._repository.acquire_lease(
            run_id=run_id,
            owner_id=owner_id,
            worker_id=worker_id,
            now=now,
            expires_at=now + timedelta(seconds=self._lease_seconds),
        )
        try:
            await self._repository.set_run_status(
                run_id=run_id,
                owner_id=owner_id,
                expected=("ready", "running"),
                status="running",
                now=now,
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
                event_key="run-resumed" if resumed else "run-started",
                event_type="run_resumed" if resumed else "run_started",
                payload={"status": "running"},
                now=now,
            )
            await self._run_loop(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
            )
        finally:
            await self._repository.release_lease(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                now=_aware(self._clock()),
            )
        return await self._run_view(run_id=run_id, owner_id=owner_id)

    def _verify_runtime_snapshot(self, authorization: Mapping[str, Any]) -> None:
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
            deadline_at = datetime.fromisoformat(str(authorization["deadline_at"]))
            if now >= _aware(deadline_at):
                await self._terminate(
                    run_id=run_id,
                    owner_id=owner_id,
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
                now=now,
                observation_cursor=len(observations),
                observation_digest=_digest(observations),
            )
            step_id = str(step["step_id"])
            ordinal = int(step["ordinal"])
            await self._event(
                run_id=run_id,
                event_key=f"step-{ordinal}-claimed",
                event_type="step_claimed",
                payload={"ordinal": ordinal, "status": "planning"},
                step_id=step_id,
                now=now,
            )

            prepared = await self._prepare_step_decision(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
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
                step_id=step_id,
                ordinal=ordinal,
                decision=decision,
                observations=observations,
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
                    step_id=step_id,
                    expected_step_status="planning",
                    reason_code="uncertain_paid_attempt",
                    now=_aware(self._clock()),
                )
                return None
            except Exception:
                await self._fail_step_and_run(
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
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
            await self._repository.transition_step(
                run_id=run_id,
                step_id=step_id,
                expected=step_status,
                status="failed",
                fields={
                    "planner_decision": decision.model_dump(mode="json"),
                    "policy_decision": {
                        "allowed": False,
                        "reason_code": "policy_violation",
                    },
                },
                now=_aware(self._clock()),
            )
            await self._repository.clear_active_step(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                step_id=step_id,
                now=_aware(self._clock()),
            )
            await self._terminate(
                run_id=run_id,
                owner_id=owner_id,
                status="failed",
                reason_code="policy_violation",
                step_id=step_id,
                now=_aware(self._clock()),
            )
            return None

        if step_status == "planning":
            await self._repository.transition_step(
                run_id=run_id,
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
            recovered = (
                await recover(idempotency_key=f"{run_id}:{step_id}:planner")
                if callable(recover)
                else None
            )
            if recovered is None:
                await self._mark_uncertain(
                    run_id=run_id,
                    owner_id=owner_id,
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
                step_id=step_id,
                ordinal=ordinal,
                call_key=str(latest["call_key"]),
                result_checkpoint=result.model_dump(mode="json"),
                usage=result.usage.model_dump(mode="python"),
                now=now,
            )
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
                await self._record_step_planned(
                    run_id=run_id,
                    step_id=step_id,
                    ordinal=ordinal,
                    decision=result.decision,
                    now=now,
                )
                return result

        call_key = (
            base_call_key
            if not attempts
            else f"{base_call_key}-retry-{len(attempts)}"
        )
        await self._repository.reserve_call(
            run_id=run_id,
            owner_id=owner_id,
            worker_id=worker_id,
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
            call_key=call_key,
            now=now,
        )
        try:
            raw_result = await self._planner.plan(
                PlannerInput(
                    goal=str(authorization["goal"]),
                    scope=AgentScope.model_validate(authorization["scope"]),
                    ordinal=ordinal,
                    allowed_tools=tuple(authorization.get("tools") or []),
                    observations=tuple(observations),
                    authorization_digest=authorization_digest,
                ),
                idempotency_key=f"{run_id}:{step_id}:planner",
            )
        except Exception:
            await self._mark_uncertain(
                run_id=run_id,
                owner_id=owner_id,
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
                step_id=step_id,
                ordinal=ordinal,
                call_key=call_key,
                usage={},
                result_checkpoint={"error_code": "planner_output_invalid"},
                now=_aware(self._clock()),
            )
            return await self._plan(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                step_id=step_id,
                ordinal=ordinal,
                authorization=authorization,
                authorization_digest=authorization_digest,
                observations=observations,
            )
        await self._settle_runtime_call(
            run_id=run_id,
            owner_id=owner_id,
            step_id=step_id,
            ordinal=ordinal,
            call_key=call_key,
            usage=result.usage.model_dump(mode="python"),
            result_checkpoint=result.model_dump(mode="json"),
            now=_aware(self._clock()),
        )
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
        if step_status == "observed":
            await self._complete_observed_step(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
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
                    step_id=step_id,
                    expected_step_status=step_status,
                    reason_code="invariant_violation",
                    now=now,
                )
                return False
            candidate = RuntimeToolResult.model_validate(checkpoint)
            if candidate.status == "failed" and candidate.retryable:
                retry_count = sum(
                    isinstance(item.get("result_checkpoint"), Mapping)
                    and (item["result_checkpoint"]).get("status") == "failed"
                    and bool((item["result_checkpoint"]).get("retryable"))
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
                    step_id=step_id,
                    expected_step_status=step_status,
                    reason_code="uncertain_paid_attempt",
                    now=now,
                )
                return False
            try:
                recovered = await self._tools.recover(
                    idempotency_key=invocation["idempotency_key"],
                )
            except Exception:
                recovered = None
            if recovered is None:
                await self._mark_uncertain(
                    run_id=run_id,
                    owner_id=owner_id,
                    step_id=step_id,
                    ordinal=ordinal,
                    call_key=call_key,
                    now=now,
                )
                await self._pause_step_and_run(
                    run_id=run_id,
                    owner_id=owner_id,
                    step_id=step_id,
                    expected_step_status=step_status,
                    reason_code="uncertain_paid_attempt",
                    now=now,
                )
                return False
            result = RuntimeToolResult.model_validate(recovered)
            await self._settle_runtime_call(
                run_id=run_id,
                owner_id=owner_id,
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
                call_key=call_key,
                now=now,
            )
            await self._event(
                run_id=run_id,
                event_key=f"{call_key}-dispatched",
                event_type="tool_dispatched",
                payload={"ordinal": ordinal, "tool_name": decision.tool.name},
                step_id=step_id,
                now=now,
            )
            try:
                raw_result = await self._tools.execute(
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
                )
                result = RuntimeToolResult.model_validate(raw_result)
            except Exception:
                await self._mark_uncertain(
                    run_id=run_id,
                    owner_id=owner_id,
                    step_id=step_id,
                    ordinal=ordinal,
                    call_key=call_key,
                    now=_aware(self._clock()),
                )
                await self._pause_step_and_run(
                    run_id=run_id,
                    owner_id=owner_id,
                    step_id=step_id,
                    expected_step_status="executing",
                    reason_code="uncertain_paid_attempt",
                    now=_aware(self._clock()),
                )
                return False
            await self._settle_runtime_call(
                run_id=run_id,
                owner_id=owner_id,
                step_id=step_id,
                ordinal=ordinal,
                call_key=call_key,
                usage=result.usage.model_dump(mode="python"),
                result_checkpoint=result.model_dump(mode="json"),
                now=_aware(self._clock()),
            )
            if result.status == "failed" and result.retryable:
                return await self._act_and_observe(
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
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
        if result.status == "failed" and result.retryable:
            return await self._act_and_observe(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                step_id=step_id,
                ordinal=ordinal,
                authorization=authorization,
                authorization_digest=authorization_digest,
                decision=decision,
                descriptor=descriptor,
                payload=payload,
                step_status="executing",
            )
        try:
            validated_output = descriptor.output_schema.model_validate(result.data)
        except Exception:
            await self._fail_step_and_run(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                step_id=step_id,
                expected_step_status="executing",
                reason_code="invariant_violation",
                now=_aware(self._clock()),
            )
            return False
        observation = {
            "status": result.status,
            "code": result.code,
            "data": validated_output.model_dump(mode="json"),
            "planner_view": result.planner_view,
            "audit_view": result.audit_view,
            "evidence_refs": list(result.evidence_refs),
            "resource_revision": result.resource_revision,
        }
        await self._repository.transition_step(
            run_id=run_id,
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
            },
            step_id=step_id,
            now=_aware(self._clock()),
        )
        await self._complete_observed_step(
            run_id=run_id,
            owner_id=owner_id,
            worker_id=worker_id,
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
        step_id: str,
        ordinal: int,
        decision: PlannerDecision,
        observations: list[dict[str, Any]],
        step_status: str,
    ) -> bool:
        if step_status == "policy_checked":
            await self._repository.transition_step(
                run_id=run_id,
                step_id=step_id,
                expected="policy_checked",
                status="executing",
                fields={"tool_invocation": None},
                now=_aware(self._clock()),
            )
            step_status = "executing"
        if step_status == "executing":
            try:
                completion = CompletionDecision.model_validate(
                    await self._completion_policy.evaluate(
                        run=await self._repository.get_run_owned(
                            run_id=run_id,
                            owner_id=owner_id,
                        ),
                        observations=observations,
                        proposal=decision.model_dump(mode="json"),
                    )
                )
            except Exception:
                await self._fail_step_and_run(
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    step_id=step_id,
                    expected_step_status="executing",
                    reason_code="invariant_violation",
                    now=_aware(self._clock()),
                )
                return True
            await self._repository.transition_step(
                run_id=run_id,
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
            payload={"ordinal": ordinal, "decision_kind": decision.kind},
            step_id=step_id,
            now=now,
        )

    async def _mark_uncertain(
        self,
        *,
        run_id: str,
        owner_id: str,
        step_id: str,
        ordinal: int,
        call_key: str,
        now: datetime,
    ) -> None:
        await self._repository.mark_call_uncertain(
            run_id=run_id,
            owner_id=owner_id,
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
        step_id: str,
        ordinal: int,
        kind: str,
        now: datetime,
    ) -> None:
        await self._repository.transition_step(
            run_id=run_id,
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
            step_id=step_id,
            now=now,
        )
        await self._event(
            run_id=run_id,
            event_key=f"step-{ordinal}-completed",
            event_type="step_completed",
            payload={"ordinal": ordinal, "status": "completed", "kind": kind},
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
        now: datetime,
    ) -> None:
        ok = await self._repository.heartbeat_lease(
            run_id=run_id,
            owner_id=owner_id,
            worker_id=worker_id,
            now=now,
            expires_at=now + timedelta(seconds=self._lease_seconds),
        )
        if not ok:
            raise AgentRuntimeStateConflict("Agent Runtime lease expired")

    async def _pause_step_and_run(
        self,
        *,
        run_id: str,
        owner_id: str,
        step_id: str,
        expected_step_status: str,
        reason_code: str,
        now: datetime,
    ) -> None:
        await self._repository.transition_step(
            run_id=run_id,
            step_id=step_id,
            expected=expected_step_status,
            status="paused",
            fields={"pause_reason": reason_code},
            now=now,
        )
        await self._terminate(
            run_id=run_id,
            owner_id=owner_id,
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
        step_id: str,
        expected_step_status: str,
        reason_code: str,
        now: datetime,
    ) -> None:
        await self._repository.transition_step(
            run_id=run_id,
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
            step_id=step_id,
            now=now,
        )
        await self._terminate(
            run_id=run_id,
            owner_id=owner_id,
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
        status: str,
        reason_code: str,
        now: datetime,
        step_id: str | None = None,
    ) -> None:
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
        termination = {
            "status": status,
            "category": category,
            "reason_code": reason_code,
            "resumable": bool(
                status == "paused"
                and reason_code in {
                    "authorization_required",
                    "manual_approval_required",
                }
            ),
            "occurred_at": now,
            "step_id": step_id,
            "detail_code": reason_code,
        }
        await self._repository.set_run_status(
            run_id=run_id,
            owner_id=owner_id,
            expected=("ready", "running", "paused"),
            status=status,
            now=now,
            fields={"termination": termination},
        )
        await self._event(
            run_id=run_id,
            event_key=f"run-{status}-{reason_code}",
            event_type="run_terminated" if status != "paused" else "run_paused",
            payload={"status": status, "reason_code": reason_code},
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
