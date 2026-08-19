"""Deterministic authorization checks applied before every tool adapter call."""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

from pydantic import BaseModel

from backend.services.agent_runtime.contracts import (
    AgentScope,
    PlannerDecision,
    RuntimeToolDescriptor,
    RuntimeToolReference,
    V1_RUNTIME_CHANGE_CLASSES,
    V1_RUNTIME_EFFECT_CLASSES,
    V1_RUNTIME_PROPOSAL_KINDS,
)


_URI_RE = re.compile(
    r"(?:^|[\s\"'(<\[])[A-Za-z][A-Za-z0-9+.-]{1,31}:(?://)?[^\s\"'<>]*"
)
_WINDOWS_PATH_RE = re.compile(
    r"(?:^|[\s\"'(<\[])(?:[A-Za-z]:[\\/]|\\\\)[^\s\"'<>]*"
)
_LOCAL_PATH_RE = re.compile(
    r"(?:^|[\s\"'(<\[])(?:/[^/\s][^\s\"'<>]*|\.\.?[\\/][^\s\"'<>]+)"
)


class AgentRuntimePolicyViolation(ValueError):
    """A planner decision exceeded the immutable readiness authorization."""


def _contains_forbidden_location(value: Any) -> bool:
    if isinstance(value, str):
        return any(
            pattern.search(value)
            for pattern in (_URI_RE, _WINDOWS_PATH_RE, _LOCAL_PATH_RE)
        )
    if isinstance(value, Mapping):
        return any(_contains_forbidden_location(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_forbidden_location(item) for item in value)
    return False


class RuntimePolicyGate:
    """Authorize an exact tool/version/scope/effect tuple and validate its input."""

    def authorize_tool_snapshot(
        self,
        *,
        authorization: Mapping[str, Any],
        decision: PlannerDecision,
        descriptor: Mapping[str, Any],
    ) -> dict[str, Any]:
        if decision.kind != "call_tool" or decision.tool is None or decision.scope is None:
            raise AgentRuntimePolicyViolation("a tool decision is required")
        allowed_tools = {
            RuntimeToolReference.model_validate(item)
            for item in authorization.get("allowed_tools") or []
        }
        if decision.tool not in allowed_tools:
            raise AgentRuntimePolicyViolation("tool/version is not authorized")
        descriptor_reference = RuntimeToolReference.model_validate(
            descriptor.get("reference") or {}
        )
        if descriptor_reference != decision.tool:
            raise AgentRuntimePolicyViolation("tool descriptor identity drifted")

        authorized_scope = AgentScope.model_validate(authorization.get("scope") or {})
        if decision.scope != authorized_scope:
            raise AgentRuntimePolicyViolation("tool scope is outside the authorized target")
        scope_kinds = tuple(str(item) for item in descriptor.get("scope_kinds") or ())
        if decision.scope.kind not in scope_kinds:
            raise AgentRuntimePolicyViolation("tool does not support the authorized scope kind")

        if authorization.get("approval_mode") != "proposal_only":
            raise AgentRuntimePolicyViolation(
                "Runtime v1 only accepts proposal_only approval"
            )
        allowed_effects = set(authorization.get("allowed_effects") or [])
        if not allowed_effects.issubset(V1_RUNTIME_EFFECT_CLASSES):
            raise AgentRuntimePolicyViolation(
                "authorization contains an unknown Runtime v1 effect class"
            )
        effect_class = str(descriptor.get("effect_class") or "")
        if effect_class not in V1_RUNTIME_EFFECT_CLASSES:
            raise AgentRuntimePolicyViolation(
                "tool effect class is outside the Runtime v1 closed set"
            )
        if effect_class not in allowed_effects:
            raise AgentRuntimePolicyViolation("tool effect class is not authorized")

        allowed_changes = set(authorization.get("allowed_change_classes") or [])
        if not allowed_changes.issubset(V1_RUNTIME_CHANGE_CLASSES):
            raise AgentRuntimePolicyViolation(
                "authorization contains an unknown Runtime v1 change class"
            )
        change_classes = set(descriptor.get("change_classes") or [])
        if not change_classes.issubset(V1_RUNTIME_CHANGE_CLASSES):
            raise AgentRuntimePolicyViolation(
                "tool change class is outside the Runtime v1 closed set"
            )
        if not change_classes.issubset(allowed_changes):
            raise AgentRuntimePolicyViolation("tool change class is not authorized")
        proposal_kinds = set(descriptor.get("proposal_kinds") or [])
        if not proposal_kinds.issubset(V1_RUNTIME_PROPOSAL_KINDS):
            raise AgentRuntimePolicyViolation(
                "tool proposal kind is outside the Runtime v1 closed set"
            )

        allowed_external = set(
            authorization.get("allowed_external_data_categories") or []
        )
        external_categories = set(
            descriptor.get("external_data_categories") or []
        )
        if not external_categories.issubset(allowed_external):
            raise AgentRuntimePolicyViolation("tool external-data category is not authorized")
        arguments = decision.arguments or {}
        if _contains_forbidden_location(arguments):
            raise AgentRuntimePolicyViolation(
                "arbitrary URI and filesystem paths are forbidden in Runtime v1"
            )

        return {
            "allowed": True,
            "reason_code": "authorized",
            "tool_name": decision.tool.name,
            "tool_version": decision.tool.version,
            "scope_kind": decision.scope.kind,
            "effect_class": effect_class,
            "approval_mode": "proposal_only",
        }

    def authorize_tool(
        self,
        *,
        authorization: Mapping[str, Any],
        decision: PlannerDecision,
        descriptor: RuntimeToolDescriptor,
    ) -> tuple[BaseModel, dict[str, Any]]:
        policy_decision = self.authorize_tool_snapshot(
            authorization=authorization,
            decision=decision,
            descriptor={
                "reference": descriptor.reference.model_dump(mode="json"),
                "scope_kinds": list(descriptor.scope_kinds),
                "effect_class": descriptor.effect_class,
                "proposal_kinds": list(descriptor.proposal_kinds),
                "change_classes": list(descriptor.change_classes),
                "external_data_categories": list(
                    descriptor.external_data_categories
                ),
            },
        )
        payload = descriptor.input_schema.model_validate(decision.arguments or {})
        return payload, policy_decision
