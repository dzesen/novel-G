"""Closed contracts shared by the bounded Agent Runtime and its adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


RuntimeEffectClass = Literal["read_only", "paid_read", "proposal_only"]
RuntimeProposalKind = Literal["chapter_prose_candidate"]
RuntimeChangeClass = Literal["temporary_candidate"]
RuntimeObservationStatus = Literal[
    "ok",
    "retryable_error",
    "blocked",
    "uncertain",
    "permanent_error",
]

V1_RUNTIME_EFFECT_CLASSES = frozenset({"read_only", "paid_read", "proposal_only"})
V1_RUNTIME_PROPOSAL_KINDS = frozenset({"chapter_prose_candidate"})
V1_RUNTIME_CHANGE_CLASSES = frozenset({"temporary_candidate"})
MAX_PLANNER_VIEW_BYTES = 16_384
MAX_RUNTIME_RESULT_PROJECTION_BYTES = 262_144


def _json_size(value: Any, *, label: str) -> int:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be JSON serializable") from exc
    return len(encoded)


def _validate_bounded_projection(
    *,
    planner_view: dict[str, Any],
    result_projection: dict[str, Any],
) -> None:
    if _json_size(planner_view, label="planner_view") > MAX_PLANNER_VIEW_BYTES:
        raise ValueError("planner_view exceeds the Runtime v1 byte limit")
    if (
        _json_size(result_projection, label="result projection")
        > MAX_RUNTIME_RESULT_PROJECTION_BYTES
    ):
        raise ValueError("result projection exceeds the Runtime v1 byte limit")


def _validate_runtime_result_projection(value: Any, *, label: str) -> None:
    if _json_size(value, label=label) > MAX_RUNTIME_RESULT_PROJECTION_BYTES:
        raise ValueError(f"{label} exceeds the Runtime v1 byte limit")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RuntimeToolReference(_StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(
        min_length=1,
        max_length=120,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
    )
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


class RuntimeAdapterKnownFailure(RuntimeError):
    """A dispatched adapter failed with a known, fully accounted outcome."""

    def __init__(
        self,
        *,
        reason_code: str,
        usage: RuntimeCallUsage,
    ) -> None:
        normalized = str(reason_code or "").strip()
        if (
            len(normalized) > 160
            or re.fullmatch(r"[a-z][a-z0-9_.-]*", normalized) is None
        ):
            raise ValueError("known adapter failure reason code is invalid")
        super().__init__(normalized)
        self.reason_code = normalized
        self.usage = RuntimeCallUsage.model_validate(usage)


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
    schema_version: Literal["agent_runtime_planner_descriptor.v1"] = (
        "agent_runtime_planner_descriptor.v1"
    )


@dataclass(frozen=True, slots=True)
class RuntimeToolDescriptor:
    """A typed executable tool contract; schemas stay in process, not in MongoDB."""

    reference: RuntimeToolReference
    label: str
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]
    scope_kinds: tuple[str, ...]
    effect_class: RuntimeEffectClass
    proposal_kinds: tuple[RuntimeProposalKind, ...]
    change_classes: tuple[RuntimeChangeClass, ...]
    max_paid_attempts_per_call: int
    max_tokens_per_call: int
    implementation_revision: str
    context_policy_revision: str
    external_data_categories: tuple[str, ...]
    idempotent: bool
    schema_version: Literal["agent_runtime_tool_descriptor.v1"] = (
        "agent_runtime_tool_descriptor.v1"
    )

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
        if self.schema_version != "agent_runtime_tool_descriptor.v1":
            raise ValueError("unknown tool descriptor schema_version")
        if self.effect_class not in V1_RUNTIME_EFFECT_CLASSES:
            raise ValueError("tool effect_class is outside the Runtime v1 closed set")
        unknown_proposals = set(self.proposal_kinds) - V1_RUNTIME_PROPOSAL_KINDS
        if unknown_proposals:
            raise ValueError("tool proposal kind is outside the Runtime v1 closed set")
        unknown_changes = set(self.change_classes) - V1_RUNTIME_CHANGE_CLASSES
        if unknown_changes:
            raise ValueError("tool change class is outside the Runtime v1 closed set")
        if self.effect_class != "proposal_only" and (
            self.proposal_kinds or self.change_classes
        ):
            raise ValueError("read tools cannot declare proposal or change classes")


class PlannerDecision(_StrictModel):
    schema_version: Literal["agent_runtime_planner_decision.v1"] = (
        "agent_runtime_planner_decision.v1"
    )
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
    schema_version: Literal["agent_runtime_planner_input.v1"] = (
        "agent_runtime_planner_input.v1"
    )
    goal: str
    scope: AgentScope
    ordinal: int = Field(ge=0)
    allowed_tools: tuple[dict[str, Any], ...] = ()
    observations: tuple[dict[str, Any], ...] = ()
    authorization_digest: str


class PlannerResult(_StrictModel):
    schema_version: Literal["agent_runtime_planner_result.v1"] = (
        "agent_runtime_planner_result.v1"
    )
    decision: PlannerDecision
    usage: RuntimeCallUsage = Field(default_factory=RuntimeCallUsage)

    @model_validator(mode="after")
    def validate_projection_size(self) -> "PlannerResult":
        _validate_runtime_result_projection(
            self.model_dump(mode="json"),
            label="planner result projection",
        )
        return self


class RuntimeToolContext(_StrictModel):
    schema_version: Literal["agent_runtime_tool_context.v1"] = (
        "agent_runtime_tool_context.v1"
    )
    owner_id: str
    novel_id: str
    run_id: str
    step_id: str
    scope: AgentScope
    authorization_digest: str


class RuntimeToolResult(_StrictModel):
    schema_version: Literal["agent_runtime_tool_result.v1"] = (
        "agent_runtime_tool_result.v1"
    )
    status: RuntimeObservationStatus
    code: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[a-z][a-z0-9_.-]*$",
    )
    data: dict[str, Any] = Field(default_factory=dict)
    planner_view: dict[str, Any] = Field(default_factory=dict)
    audit_view: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    resource_revision: str | None = None
    resource_digest: str | None = Field(default=None, min_length=1, max_length=160)
    usage: RuntimeCallUsage = Field(default_factory=RuntimeCallUsage)
    error_summary: str | None = Field(default=None, max_length=1_000)

    @model_validator(mode="after")
    def validate_projection_size(self) -> "RuntimeToolResult":
        _validate_bounded_projection(
            planner_view=self.planner_view,
            result_projection=self.model_dump(
                mode="json",
                exclude={"planner_view"},
            ),
        )
        return self


class RuntimeObservation(_StrictModel):
    schema_version: Literal["agent_runtime_observation.v1"] = (
        "agent_runtime_observation.v1"
    )
    observation_id: str = Field(min_length=1, max_length=160)
    step_id: str = Field(min_length=1, max_length=160)
    tool: RuntimeToolReference
    status: RuntimeObservationStatus
    code: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[a-z][a-z0-9_.-]*$",
    )
    data: dict[str, Any] = Field(default_factory=dict)
    planner_view: dict[str, Any] = Field(default_factory=dict)
    audit_view: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    resource_revision: str | None = None
    resource_digest: str | None = Field(default=None, min_length=1, max_length=160)
    usage: RuntimeCallUsage = Field(default_factory=RuntimeCallUsage)
    error_summary: str | None = Field(default=None, max_length=1_000)

    @model_validator(mode="after")
    def validate_projection_size(self) -> "RuntimeObservation":
        _validate_bounded_projection(
            planner_view=self.planner_view,
            result_projection=self.model_dump(
                mode="json",
                exclude={"planner_view"},
            ),
        )
        return self


class CompletionDecision(_StrictModel):
    schema_version: Literal["agent_runtime_completion_decision.v1"] = (
        "agent_runtime_completion_decision.v1"
    )
    satisfied: bool
    reason_code: str = Field(min_length=1, max_length=160)
    planner_view: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_planner_view_size(self) -> "CompletionDecision":
        if (
            _json_size(self.planner_view, label="planner_view")
            > MAX_PLANNER_VIEW_BYTES
        ):
            raise ValueError("planner_view exceeds the Runtime v1 byte limit")
        return self


class AgentRuntimeLimits(_StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_steps: int = Field(ge=1, le=1_000)
    max_planner_calls: int = Field(ge=1, le=1_000)
    max_tool_calls: int = Field(ge=0, le=1_000)
    max_paid_attempts: int = Field(ge=0, le=10_000)
    token_budget: int = Field(ge=0, le=1_000_000_000)
    deadline_seconds: int = Field(ge=1, le=86_400)
    max_predispatch_retries: int = Field(default=2, ge=0, le=20)
    max_planner_repairs: int = Field(default=1, ge=0, le=20)
    max_tool_retries: int = Field(default=1, ge=0, le=20)

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
    allowed_effects: tuple[RuntimeEffectClass, ...] = ()
    allowed_change_classes: tuple[RuntimeChangeClass, ...] = ()
    allowed_external_data_categories: tuple[str, ...] = ()
    approval_mode: Literal["proposal_only"] = "proposal_only"
    limits: AgentRuntimeLimits
    predecessor_run_id: str | None = Field(default=None, min_length=1)
    replay_of_run_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_unique_tools(self) -> "AgentReadinessRequest":
        if len(set(self.allowed_tools)) != len(self.allowed_tools):
            raise ValueError("allowed_tools cannot contain duplicates")
        if self.predecessor_run_id and self.replay_of_run_id:
            raise ValueError("predecessor_run_id and replay_of_run_id are exclusive")
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
    category: Literal["success", "pause", "failure", "cancelled", "superseded"]
    reason_code: str
    resumable: bool
    occurred_at: datetime
    step_id: str | None = None
    detail_code: str | None = None


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
    predecessor_run_id: str | None = None
    replay_of_run_id: str | None = None
    successor_run_id: str | None = None
    lineage_root_run_id: str | None = None


class AgentReplayView(_StrictModel):
    run_id: str
    consistent: bool
    violations: tuple[str, ...] = ()
    derived_status: str
    derived_usage: AgentRuntimeUsage
    source_input_digest: str
