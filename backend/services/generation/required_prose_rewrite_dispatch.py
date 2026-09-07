"""Deterministic decisions for the already-authorized successor rewrite.

The Runtime still owns policy, dispatch, checkpoints and candidate mutation.
This adapter only translates one immutable request into two typed decisions.
"""
from __future__ import annotations

import re

from backend.db.repositories.agent_runtime_repository import agent_runtime_repository
from backend.services.agent_runtime.contracts import (
    AgentScope, PlannerDecision, PlannerDescriptor, PlannerInput, PlannerResult,
)
from backend.services.generation.required_prose_rewrite_contracts import (
    REQUIRED_REWRITE_FINISH, REQUIRED_REWRITE_SCOPE, REQUIRED_REWRITE_TOOL,
    contract_digest,
)


_CALL_KEY = re.compile(r"^([0-9a-f]{24}):([0-9a-f]{32}):planner$")


class FixedRequiredRewritePlanner:
    def __init__(self, *, binding, entry):
        self._binding, self._entry = binding, entry
        self._scope = AgentScope(
            kind=REQUIRED_REWRITE_SCOPE,
            object_id=entry.origin.request.source_run_id,
        )
        identity = contract_digest({
            "binding": binding.model_dump(mode="json"),
            "origin": entry.origin.model_dump(mode="json"),
        })
        self.descriptor = PlannerDescriptor(
            name="fixed-required-prose-rewrite",
            version=1,
            implementation_revision=f"fixed-required-rewrite-r1-{identity}",
            provider_alias="local",
            provider_model="fixed-rewrite-dispatch-v1",
            max_paid_attempts_per_call=0,
            max_tokens_per_call=0,
            external_data_categories=(),
        )

    def _decision(self, ordinal: int) -> PlannerResult:
        if type(ordinal) is not int or ordinal not in {0, 1}:
            raise ValueError("fixed_rewrite_ordinal_invalid")
        decision = (
            PlannerDecision(
                kind="call_tool", tool=REQUIRED_REWRITE_TOOL,
                scope=self._scope,
                arguments=self._entry.origin.request.tool_arguments(),
            )
            if ordinal == 0 else
            PlannerDecision(kind="propose_finish", finish_code=REQUIRED_REWRITE_FINISH)
        )
        return PlannerResult(decision=decision)

    async def plan(self, planner_input: PlannerInput, *, idempotency_key: str) -> PlannerResult:
        if (
            _CALL_KEY.fullmatch(idempotency_key) is None
            or self._entry.readiness_digest is None
            or planner_input.authorization_digest != self._entry.readiness_digest
            or planner_input.scope != self._scope
            or [tool.get("reference") for tool in planner_input.allowed_tools]
            != [REQUIRED_REWRITE_TOOL.model_dump(mode="json")]
        ):
            raise ValueError("fixed_rewrite_authorization_changed")
        # The goal and any free-form observation never select scope/arguments.
        return self._decision(planner_input.ordinal)

    async def recover(self, *, idempotency_key: str) -> PlannerResult:
        match = _CALL_KEY.fullmatch(idempotency_key)
        if match is None:
            raise ValueError("fixed_rewrite_recovery_identity_invalid")
        run_id, step_id = match.groups()
        run = await agent_runtime_repository.get_run_owned(
            run_id=run_id, owner_id=self._binding.owner_id,
        )
        authorization = run.get("authorization") or {}
        if (
            self._entry.readiness_digest is None
            or run.get("authorization_digest") != self._entry.readiness_digest
            or contract_digest(authorization) != self._entry.readiness_digest
            or str(run.get("novel_id")) != self._binding.novel_id
            or run.get("start_request_id") != self._entry.start_request_id
            or authorization.get("scope") != self._scope.model_dump(mode="json")
            or authorization.get("allowed_tools") != [REQUIRED_REWRITE_TOOL.model_dump(mode="json")]
            or authorization.get("planner") != self.descriptor.model_dump(mode="json")
        ):
            raise ValueError("fixed_rewrite_recovery_authorization_changed")
        step = await agent_runtime_repository.get_step_owned(
            run_id=run_id, owner_id=self._binding.owner_id, step_id=step_id,
        )
        # Reconstruct only the same original decision; no new attempt/allowance.
        return self._decision(step.get("ordinal"))
