"""Executable, typed capability seam shared by every runtime caller."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Awaitable, Callable, Literal, Mapping

from pydantic import BaseModel


CapabilitySource = Literal["http", "job_engine", "agent_runtime", "test"]


class SideEffectPolicy(StrEnum):
    PREVIEW_ONLY = "preview_only"
    ACCEPT_REQUIRED = "accept_required"
    SYSTEM_WRITE = "system_write"


class RevisionPolicy(StrEnum):
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
    stream: (
        Callable[
            [Any, Any, CapabilityCall],
            Awaitable[AsyncIterator[Any]],
        ]
        | None
    ) = None

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
    event_schema: type[BaseModel] | None = None

    def __post_init__(self) -> None:
        if not self.capability:
            raise ValueError("capability cannot be empty")
        if self.version < 1:
            raise ValueError("capability version must be positive")
        if not issubclass(self.input_schema, BaseModel):
            raise TypeError("input_schema must be a Pydantic model")
        if not issubclass(self.output_schema, BaseModel):
            raise TypeError("output_schema must be a Pydantic model")
        if self.handler.stream is not None and self.event_schema is None:
            raise ValueError("stream handlers require an event_schema")
        if len(set(self.allowed_tools)) != len(self.allowed_tools):
            raise ValueError("allowed tool ids must be unique")

    def public_view(self) -> dict[str, Any]:
        """Project display metadata from the executable runtime contract."""

        return {
            "capability": self.capability,
            "version": self.version,
            "label": self.label,
            "description": self.description,
            "customizable": self.customizable,
            "preview_only": (
                self.side_effect_policy is SideEffectPolicy.PREVIEW_ONLY
            ),
            "scope_options": list(self.scope_options),
            "input_contract": self.input_schema.__name__,
            "output_contract": self.output_schema.__name__,
            "context_policy": self.context_provider.policy_id,
            "side_effect_policy": self.side_effect_policy.value,
            "handler_id": self.handler.handler_id,
            "allowed_tools": list(self.allowed_tools),
            "revision_policy": self.revision_policy.value,
        }


@dataclass(frozen=True)
class CapabilityExecution:
    value: BaseModel
    budget: CapabilityBudget
    audit: Mapping[str, Any]
    context_policy: str
    handler_id: str


@dataclass(frozen=True)
class CapabilityStream:
    events: AsyncIterator[BaseModel]
    budget: CapabilityBudget
    context_policy: str
    handler_id: str


@dataclass(frozen=True)
class ToolReference:
    name: str
    version: int

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("tool name cannot be empty")
        if self.version < 1:
            raise ValueError("tool version must be positive")


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

    def list(self) -> tuple[CapabilityDefinition, ...]:
        return self._definitions

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

    async def stream(
        self,
        capability: str,
        payload: BaseModel | Mapping[str, Any],
        *,
        call: CapabilityCall,
    ) -> CapabilityStream:
        definition = self.get(capability)
        if definition.handler.stream is None or definition.event_schema is None:
            raise ValueError(f"capability is not streamable: {capability}")
        request = definition.input_schema.model_validate(payload)
        budget = definition.budget_estimator(request)
        if not isinstance(budget, CapabilityBudget):
            raise TypeError("budget_estimator must return CapabilityBudget")
        context = await definition.context_provider.provide(request, call)
        raw_events = await definition.handler.stream(request, context, call)

        async def validated_events() -> AsyncIterator[BaseModel]:
            async for event in raw_events:
                yield definition.event_schema.model_validate(event)

        return CapabilityStream(
            events=validated_events(),
            budget=budget,
            context_policy=definition.context_provider.policy_id,
            handler_id=definition.handler.handler_id,
        )


class ToolRegistry:
    """Agent Runtime adapter over an exact, frozen capability allowlist."""

    def __init__(
        self,
        capabilities: CapabilityRegistry,
        *,
        allowed: tuple[ToolReference, ...],
    ) -> None:
        if len(set(allowed)) != len(allowed):
            raise ValueError("authorized tool references must be unique")
        for reference in allowed:
            definition = capabilities.get(reference.name)
            if definition.version != reference.version:
                raise ValueError(
                    "authorized tool version does not match registry: "
                    f"{reference.name}@{reference.version}"
                )
        self._capabilities = capabilities
        self._allowed = frozenset(allowed)

    def list(self) -> tuple[CapabilityDefinition, ...]:
        return tuple(
            self._capabilities.get(reference.name)
            for reference in sorted(
                self._allowed,
                key=lambda item: (item.name, item.version),
            )
        )

    async def execute(
        self,
        reference: ToolReference,
        payload: BaseModel | Mapping[str, Any],
        *,
        call: CapabilityCall,
    ) -> CapabilityExecution:
        if call.source != "agent_runtime":
            raise ValueError("ToolRegistry only accepts agent_runtime calls")
        if reference not in self._allowed:
            raise ValueError(
                f"tool is not authorized: {reference.name}@{reference.version}"
            )
        definition = self._capabilities.get(reference.name)
        if definition.version != reference.version:
            raise ValueError(
                f"tool version is not authorized: "
                f"{reference.name}@{reference.version}"
            )
        return await self._capabilities.execute(
            reference.name,
            payload,
            call=call,
        )
