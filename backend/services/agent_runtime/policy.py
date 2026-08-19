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
)


_URL_RE = re.compile(r"https?://", re.IGNORECASE)


class AgentRuntimePolicyViolation(ValueError):
    """A planner decision exceeded the immutable readiness authorization."""


def _contains_url(value: Any) -> bool:
    if isinstance(value, str):
        return bool(_URL_RE.search(value))
    if isinstance(value, Mapping):
        return any(_contains_url(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_url(item) for item in value)
    return False


class RuntimePolicyGate:
    """Authorize an exact tool/version/scope/effect tuple and validate its input."""

    def authorize_tool(
        self,
        *,
        authorization: Mapping[str, Any],
        decision: PlannerDecision,
        descriptor: RuntimeToolDescriptor,
    ) -> tuple[BaseModel, dict[str, Any]]:
        if decision.kind != "call_tool" or decision.tool is None or decision.scope is None:
            raise AgentRuntimePolicyViolation("a tool decision is required")
        allowed_tools = {
            RuntimeToolReference.model_validate(item)
            for item in authorization.get("allowed_tools") or []
        }
        if decision.tool not in allowed_tools:
            raise AgentRuntimePolicyViolation("tool/version is not authorized")
        if descriptor.reference != decision.tool:
            raise AgentRuntimePolicyViolation("tool descriptor identity drifted")

        authorized_scope = AgentScope.model_validate(authorization.get("scope") or {})
        if decision.scope != authorized_scope:
            raise AgentRuntimePolicyViolation("tool scope is outside the authorized target")
        if decision.scope.kind not in descriptor.scope_kinds:
            raise AgentRuntimePolicyViolation("tool does not support the authorized scope kind")

        allowed_effects = set(authorization.get("allowed_effects") or [])
        if descriptor.effect_class not in allowed_effects:
            raise AgentRuntimePolicyViolation("tool effect class is not authorized")
        if descriptor.effect_class == "system_write":
            raise AgentRuntimePolicyViolation("formal system writes are forbidden in Runtime v1")

        allowed_changes = set(authorization.get("allowed_change_classes") or [])
        if not set(descriptor.change_classes).issubset(allowed_changes):
            raise AgentRuntimePolicyViolation("tool change class is not authorized")

        allowed_external = set(
            authorization.get("allowed_external_data_categories") or []
        )
        if not set(descriptor.external_data_categories).issubset(allowed_external):
            raise AgentRuntimePolicyViolation("tool external-data category is not authorized")
        arguments = decision.arguments or {}
        if _contains_url(arguments) and not descriptor.external_data_categories:
            raise AgentRuntimePolicyViolation("unclassified external URL is forbidden")

        payload = descriptor.input_schema.model_validate(arguments)
        return payload, {
            "allowed": True,
            "reason_code": "authorized",
            "tool_name": decision.tool.name,
            "tool_version": decision.tool.version,
            "scope_kind": decision.scope.kind,
            "effect_class": descriptor.effect_class,
        }
