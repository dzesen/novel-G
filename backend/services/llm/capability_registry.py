"""Executable, typed capability seam shared by every runtime caller."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, Mapping

from pydantic import BaseModel


CapabilitySource = Literal["http", "job_engine", "agent_runtime", "test"]


class SideEffectPolicy(str):
    PREVIEW_ONLY = "preview_only"
    ACCEPT_REQUIRED = "accept_required"
    SYSTEM_WRITE = "system_write"


class RevisionPolicy(str):
    READ_ONLY = "read_only"
    RECHECK_BEFORE_ACCEPT = "recheck_before_accept"
    SYSTEM_WRITE_ADVANCES = "system_write_advances"


@dataclass(frozen=True)
class CapabilityCall:
    source: CapabilitySource
    request_id: str | None = None
    actor: Any | None = None


@dataclass(frozen=True)
class CapabilityBudget:
    max_paid_attempts: int
    max_output_tokens: int

    def __post_init__(self) -> None:
        if self.max_paid_attempts < 0:
            raise ValueError("max_paid_attempts cannot be negative")
        if self.max_output_tokens < 0:
            raise ValueError("max_output_tokens cannot be negative")


@dataclass(frozen=True)
class ContextProvider:
    policy_id: str
    provide: Callable[[Any, CapabilityCall], Awaitable[Any]]


@dataclass(frozen=True)
class CapabilityHandler:
    execute: Callable[[Any, Any, CapabilityCall], Awaitable[Any]]

    @property
    def handler_id(self) -> str:
        return (
            f"{self.execute.__module__}:"
            f"{getattr(self.execute, '__name__', type(self.execute).__name__)}"
        )


@dataclass(frozen=True)
class CapabilityDefinition:
    capability: str
    version: int
    label: str
    description: str
    customizable: bool
    scope_options: tuple[str, ...]
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]
    context_provider: ContextProvider
    handler: CapabilityHandler
    side_effect_policy: SideEffectPolicy
    allowed_tools: tuple[str, ...]
    budget_estimator: Callable[[BaseModel], CapabilityBudget]
    revision_policy: RevisionPolicy
    audit_projector: Callable[[BaseModel], Mapping[str, Any]]

    def __post_init__(self) -> None:
        if not self.capability:
            raise ValueError("capability cannot be empty")
        if self.version < 1:
            raise ValueError("capability version must be positive")
        if not issubclass(self.input_schema, BaseModel):
            raise TypeError("input_schema must be a Pydantic model")
        if not issubclass(self.output_schema, BaseModel):
            raise TypeError("output_schema must be a Pydantic model")


@dataclass(frozen=True)
class CapabilityExecution:
    value: BaseModel
    budget: CapabilityBudget
    audit: Mapping[str, Any]
    context_policy: str
    handler_id: str


class CapabilityRegistry:
    """Validate and execute capabilities without transport-specific dispatch."""

    def __init__(
        self,
        definitions: tuple[CapabilityDefinition, ...],
    ) -> None:
        by_id = {item.capability: item for item in definitions}
        if len(by_id) != len(definitions):
            raise ValueError("capability ids must be unique")
        self._definitions = definitions
        self._by_id = by_id

    def get(self, capability: str) -> CapabilityDefinition:
        try:
            return self._by_id[capability]
        except KeyError as exc:
            raise ValueError(f"unknown capability: {capability}") from exc

    async def execute(
        self,
        capability: str,
        payload: BaseModel | Mapping[str, Any],
        *,
        call: CapabilityCall,
    ) -> CapabilityExecution:
        definition = self.get(capability)
        request = definition.input_schema.model_validate(payload)
        budget = definition.budget_estimator(request)
        if not isinstance(budget, CapabilityBudget):
            raise TypeError("budget_estimator must return CapabilityBudget")
        context = await definition.context_provider.provide(request, call)
        raw_result = await definition.handler.execute(request, context, call)
        result = definition.output_schema.model_validate(raw_result)
        return CapabilityExecution(
            value=result,
            budget=budget,
            audit=dict(definition.audit_projector(result)),
            context_policy=definition.context_provider.policy_id,
            handler_id=definition.handler.handler_id,
        )
