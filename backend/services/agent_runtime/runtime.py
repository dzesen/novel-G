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
    AgentRuntimeRepository,
    AgentRuntimeStateConflict,
    agent_runtime_repository,
)
from backend.services.agent_runtime.contracts import (
    AgentEventView,
    AgentReadinessRequest,
    AgentReadinessView,
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
    RuntimePolicyGate,
)


READINESS_TTL_SECONDS = 30 * 60
TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "aborted"})


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

        readiness_id = str(ObjectId())
        expires_at = now + timedelta(seconds=READINESS_TTL_SECONDS)
        deadline_at = now + timedelta(seconds=request.limits.deadline_seconds)
        authorization = {
            "schema_version": "agent_runtime_authorization.v1",
            "readiness_id": readiness_id,
            "binding_mode": "single_use",
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
        if run.get("status") in TERMINAL_RUN_STATUSES or run.get("status") == "paused":
            return await self._run_view(run_id=run_id, owner_id=str(owner_id))
        self._verify_runtime_snapshot(run.get("authorization") or {})

        worker_id = uuid4().hex
        await self._repository.acquire_lease(
            run_id=run_id,
            owner_id=str(owner_id),
            worker_id=worker_id,
            now=now,
            expires_at=now + timedelta(seconds=self._lease_seconds),
        )
        try:
            await self._repository.set_run_status(
                run_id=run_id,
                owner_id=str(owner_id),
                expected=("ready", "running"),
                status="running",
                now=now,
            )
            await self._event(
                run_id=run_id,
                event_key="run-started",
                event_type="run_started",
                payload={"status": "running"},
                now=now,
            )
            await self._run_loop(
                run_id=run_id,
                owner_id=str(owner_id),
                worker_id=worker_id,
            )
        finally:
            await self._repository.release_lease(
                run_id=run_id,
                owner_id=str(owner_id),
                worker_id=worker_id,
                now=_aware(self._clock()),
            )
        return await self._run_view(run_id=run_id, owner_id=str(owner_id))

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
            if int(run.get("next_ordinal") or 0) >= int(limits["max_steps"]):
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
                return
            except Exception:
                await self._fail_step_and_run(
                    run_id=run_id,
                    owner_id=owner_id,
                    worker_id=worker_id,
                    step_id=step_id,
                    expected_step_status="planning",
                    reason_code="planner_execution_failed",
                    now=_aware(self._clock()),
                )
                return

            decision = planner_result.decision
            try:
                if decision.kind == "call_tool":
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
            except (AgentRuntimePolicyViolation, ValueError):
                await self._repository.transition_step(
                    run_id=run_id,
                    step_id=step_id,
                    expected="planning",
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
                return

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
                event_type="policy_checked",
                payload={
                    "ordinal": ordinal,
                    "allowed": True,
                    "decision_kind": decision.kind,
                },
                step_id=step_id,
                now=_aware(self._clock()),
            )

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
            )
            if completed:
                return

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
        call_key = f"step-{ordinal}-planner"
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
        await self._repository.mark_call_dispatched(
            run_id=run_id,
            owner_id=owner_id,
            worker_id=worker_id,
            call_key=call_key,
            now=now,
        )
        result = PlannerResult.model_validate(await self._planner.plan(
            PlannerInput(
                goal=str(authorization["goal"]),
                scope=AgentScope.model_validate(authorization["scope"]),
                ordinal=ordinal,
                allowed_tools=tuple(authorization.get("tools") or []),
                observations=tuple(observations),
                authorization_digest=authorization_digest,
            ),
            idempotency_key=f"{run_id}:{step_id}:planner",
        ))
        await self._repository.settle_call(
            run_id=run_id,
            owner_id=owner_id,
            call_key=call_key,
            usage=result.usage.model_dump(mode="python"),
            now=_aware(self._clock()),
        )
        await self._event(
            run_id=run_id,
            event_key=f"step-{ordinal}-planned",
            event_type="planner_completed",
            payload={"ordinal": ordinal, "decision_kind": result.decision.kind},
            step_id=step_id,
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
    ) -> bool:
        now = _aware(self._clock())
        if not await self._revision_matches(
            owner_id=owner_id,
            authorization=authorization,
        ):
            await self._pause_step_and_run(
                run_id=run_id,
                owner_id=owner_id,
                step_id=step_id,
                expected_step_status="policy_checked",
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
        call_key = f"step-{ordinal}-tool"
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
                expected_step_status="policy_checked",
                reason_code="budget_exhausted",
                now=now,
            )
            return False

        assert decision.tool is not None and decision.scope is not None
        invocation = {
            "tool": decision.tool.model_dump(mode="json"),
            "scope": decision.scope.model_dump(mode="json"),
            "arguments": payload.model_dump(mode="json"),
            "idempotency_key": f"{run_id}:{step_id}:tool",
        }
        await self._repository.transition_step(
            run_id=run_id,
            step_id=step_id,
            expected="policy_checked",
            status="executing",
            fields={"tool_invocation": invocation},
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
            validated_output = descriptor.output_schema.model_validate(result.data)
        except Exception:
            await self._fail_step_and_run(
                run_id=run_id,
                owner_id=owner_id,
                worker_id=worker_id,
                step_id=step_id,
                expected_step_status="executing",
                reason_code="tool_execution_failed",
                now=_aware(self._clock()),
            )
            return False
        await self._repository.settle_call(
            run_id=run_id,
            owner_id=owner_id,
            call_key=call_key,
            usage=result.usage.model_dump(mode="python"),
            now=_aware(self._clock()),
        )
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
        await self._repository.transition_step(
            run_id=run_id,
            step_id=step_id,
            expected="observed",
            status="completed",
            fields={},
            now=_aware(self._clock()),
        )
        await self._repository.clear_active_step(
            run_id=run_id,
            owner_id=owner_id,
            worker_id=worker_id,
            step_id=step_id,
            now=_aware(self._clock()),
        )
        await self._event(
            run_id=run_id,
            event_key=f"step-{ordinal}-completed",
            event_type="step_completed",
            payload={"ordinal": ordinal, "status": "completed", "kind": "tool"},
            step_id=step_id,
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
    ) -> bool:
        await self._repository.transition_step(
            run_id=run_id,
            step_id=step_id,
            expected="policy_checked",
            status="executing",
            fields={"tool_invocation": None},
            now=_aware(self._clock()),
        )
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
                reason_code="completion_check_failed",
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
        await self._repository.transition_step(
            run_id=run_id,
            step_id=step_id,
            expected="observed",
            status="completed",
            fields={},
            now=_aware(self._clock()),
        )
        await self._repository.clear_active_step(
            run_id=run_id,
            owner_id=owner_id,
            worker_id=worker_id,
            step_id=step_id,
            now=_aware(self._clock()),
        )
        await self._event(
            run_id=run_id,
            event_key=f"step-{ordinal}-completed",
            event_type="step_completed",
            payload={"ordinal": ordinal, "status": "completed", "kind": "finish"},
            step_id=step_id,
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
        termination = {
            "status": status,
            "reason_code": reason_code,
            "occurred_at": now,
            "step_id": step_id,
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
        )
