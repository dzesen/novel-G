"""Closed contracts shared by the bounded Agent Runtime and its adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RuntimeToolReference(_StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=120)
    version: int = Field(ge=1)


class AgentScope(_StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(min_length=1, max_length=80)
    object_id: str = Field(min_length=1, max_length=160)


class RuntimeCallUsage(_StrictModel):
    paid_attempts: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)


class PlannerDescriptor(_StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=120)
    version: int = Field(ge=1)
    implementation_revision: str = Field(min_length=1, max_length=160)
    provider_alias: str = Field(min_length=1, max_length=160)
    provider_model: str = Field(min_length=1, max_length=240)
    max_paid_attempts_per_call: int = Field(ge=0)
    max_tokens_per_call: int = Field(ge=0)
    external_data_categories: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RuntimeToolDescriptor:
    """A typed executable tool contract; schemas stay in process, not in MongoDB."""

    reference: RuntimeToolReference
    label: str
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]
    scope_kinds: tuple[str, ...]
    effect_class: str
    proposal_kinds: tuple[str, ...]
    change_classes: tuple[str, ...]
    max_paid_attempts_per_call: int
    max_tokens_per_call: int
    implementation_revision: str
    context_policy_revision: str
    external_data_categories: tuple[str, ...]
    idempotent: bool

    def __post_init__(self) -> None:
        if not self.label.strip():
            raise ValueError("tool label is required")
        if not self.scope_kinds:
            raise ValueError("tool scope_kinds cannot be empty")
        if self.max_paid_attempts_per_call < 0:
            raise ValueError("tool paid-attempt bound cannot be negative")
        if self.max_tokens_per_call < 0:
            raise ValueError("tool token bound cannot be negative")
        if not self.implementation_revision.strip():
            raise ValueError("tool implementation_revision is required")
        if not self.context_policy_revision.strip():
            raise ValueError("tool context_policy_revision is required")


class PlannerDecision(_StrictModel):
    kind: Literal["call_tool", "propose_finish"]
    tool: RuntimeToolReference | None = None
    scope: AgentScope | None = None
    arguments: dict[str, Any] | None = None
    finish_code: str | None = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def validate_decision_shape(self) -> "PlannerDecision":
        if self.kind == "call_tool":
            if self.tool is None or self.scope is None or self.arguments is None:
                raise ValueError("call_tool requires tool, scope, and arguments")
            if self.finish_code is not None:
                raise ValueError("call_tool cannot carry finish_code")
        else:
            if not str(self.finish_code or "").strip():
                raise ValueError("propose_finish requires finish_code")
            if self.tool is not None or self.scope is not None or self.arguments is not None:
                raise ValueError("propose_finish cannot carry a tool invocation")
        return self


class PlannerInput(_StrictModel):
    goal: str
    scope: AgentScope
    ordinal: int = Field(ge=0)
    allowed_tools: tuple[dict[str, Any], ...] = ()
    observations: tuple[dict[str, Any], ...] = ()
    authorization_digest: str


class PlannerResult(_StrictModel):
    decision: PlannerDecision
    usage: RuntimeCallUsage = Field(default_factory=RuntimeCallUsage)


class RuntimeToolContext(_StrictModel):
    owner_id: str
    novel_id: str
    run_id: str
    step_id: str
    scope: AgentScope
    authorization_digest: str


class RuntimeToolResult(_StrictModel):
    status: Literal["ok", "rejected", "failed"]
    code: str = Field(min_length=1, max_length=160)
    data: dict[str, Any] = Field(default_factory=dict)
    planner_view: dict[str, Any] = Field(default_factory=dict)
    audit_view: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    resource_revision: str | None = None
    usage: RuntimeCallUsage = Field(default_factory=RuntimeCallUsage)


class CompletionDecision(_StrictModel):
    satisfied: bool
    reason_code: str = Field(min_length=1, max_length=160)
    planner_view: dict[str, Any] = Field(default_factory=dict)


class AgentRuntimeLimits(_StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_steps: int = Field(ge=1, le=1_000)
    max_planner_calls: int = Field(ge=1, le=1_000)
    max_tool_calls: int = Field(ge=0, le=1_000)
    max_paid_attempts: int = Field(ge=0, le=10_000)
    token_budget: int = Field(ge=0, le=1_000_000_000)
    deadline_seconds: int = Field(ge=1, le=86_400)

    @model_validator(mode="after")
    def validate_step_capacity(self) -> "AgentRuntimeLimits":
        if self.max_planner_calls < self.max_steps:
            raise ValueError("max_planner_calls must cover every permitted step")
        return self


class AgentReadinessRequest(_StrictModel):
    novel_id: str = Field(min_length=1)
    goal: str = Field(min_length=1, max_length=20_000)
    scope: AgentScope
    allowed_tools: tuple[RuntimeToolReference, ...] = ()
    allowed_effects: tuple[str, ...] = ()
    allowed_change_classes: tuple[str, ...] = ()
    allowed_external_data_categories: tuple[str, ...] = ()
    approval_mode: Literal["proposal_only"] = "proposal_only"
    limits: AgentRuntimeLimits

    @model_validator(mode="after")
    def validate_unique_tools(self) -> "AgentReadinessRequest":
        if len(set(self.allowed_tools)) != len(self.allowed_tools):
            raise ValueError("allowed_tools cannot contain duplicates")
        return self


class AgentReadinessView(_StrictModel):
    readiness_id: str
    digest: str
    expires_at: datetime
    deadline_at: datetime
    baseline_narrative_revision: int
    authorization: dict[str, Any]


class AgentRuntimeUsage(_StrictModel):
    planner_calls: int = 0
    tool_calls: int = 0
    paid_attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class AgentTermination(_StrictModel):
    status: str
    reason_code: str
    occurred_at: datetime
    step_id: str | None = None


class AgentStepView(_StrictModel):
    step_id: str
    ordinal: int
    status: str
    planner_decision: dict[str, Any] | None = None
    policy_decision: dict[str, Any] | None = None
    tool_invocation: dict[str, Any] | None = None
    observation: dict[str, Any] | None = None


class AgentEventView(_StrictModel):
    event_id: str
    sequence: int
    type: str
    step_id: str | None = None
    payload: dict[str, Any]
    created_at: datetime


class AgentRunView(_StrictModel):
    run_id: str
    status: str
    authorization_digest: str
    usage: AgentRuntimeUsage
    termination: AgentTermination | None = None
    steps: tuple[AgentStepView, ...] = ()
    events: tuple[AgentEventView, ...] = ()
    has_uncertain_attempts: bool = False
