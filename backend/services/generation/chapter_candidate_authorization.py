"""Read-only authorization planning for the candidate-first chapter tail."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationError,
    model_validator,
)

from backend.services.agent_runtime.contracts import (
    AgentRuntimeLimits,
    PlannerDescriptor,
    RuntimeChangeClass,
    RuntimeEffectClass,
    RuntimeFailureReasonCodes,
    RuntimeProposalKind,
    RuntimeToolDescriptor,
    RuntimeToolReference,
    runtime_tool_descriptor_snapshot,
)
from backend.services.generation.chapter_finalization import (
    MAX_FINALIZATION_REPAIR_CYCLES,
)
from backend.services.generation.candidate_repair_contracts import (
    MAX_CHAPTER_CANDIDATE_COMPONENT_REPAIRS,
)
from backend.services.generation.chapter_repair_policy import (
    RepairBudgetLimitsV1,
)
from backend.services.generation.provider_budget import (
    ProviderBudgetBound,
    max_provider_bounds,
    merge_provider_bounds,
    scale_provider_bounds,
    structured_call_budget,
    structured_provider_bounds,
)
from backend.services.generation.narrative_quality_authorization import (
    build_narrative_quality_signal_authorization,
    narrative_quality_signal_authorization_digest,
    validate_readiness_narrative_quality_signal_authorization,
)
from backend.services.llm.generation_runtime import (
    GenerationPlan,
    STRUCTURED_REQUEST_BUDGET_PROTOCOL,
    StructuredOutputMode,
    WorkflowStepTarget,
)


CANDIDATE_REPAIR_AUTHORIZATION_SCHEMA = (
    "chapter_candidate_repair_authorization.v14"
)
CANDIDATE_PIPELINE_REVISION = 47
CANDIDATE_STRUCTURED_PLAN_SCHEMA = "candidate_structured_generation_plan.v4"
CANDIDATE_JOB_EXECUTION_AUTHORIZATION_SCHEMA = (
    "chapter_candidate_job_execution_authorization.v2"
)
CANDIDATE_JOB_GENERATION_PLAN_SCHEMA = "candidate_job_generation_plan.v1"
PROSE_REMEDIATION_SCOPE_KIND = "chapter_prose_candidate"
PROSE_REMEDIATION_MAX_STEPS = 3
PROSE_REMEDIATION_MAX_PLANNER_CALLS = 3
PROSE_REMEDIATION_MAX_TOOL_CALLS = 2
# Provider windows come from the frozen plans; this margin covers only local
# scheduling, ledger settlement, and terminal projection work between calls.
PROSE_REMEDIATION_LOCAL_COMPLETION_MARGIN_SECONDS = 60
PROSE_REMEDIATION_MAX_PREDISPATCH_RETRIES = 2
PROSE_REMEDIATION_MAX_PLANNER_REPAIRS = 1
PROSE_REMEDIATION_MAX_TOOL_RETRIES = 1

_PositiveInt = Annotated[StrictInt, Field(ge=1)]
_NonNegativeInt = Annotated[StrictInt, Field(ge=0)]
_Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def readiness_uses_candidate_pipeline(readiness: Any) -> bool:
    """Select the execution protocol without allowing damaged v2 to downgrade."""

    if readiness is None:
        return False
    if not isinstance(readiness, Mapping):
        raise ValueError("generation readiness is invalid")
    version = readiness.get("version")
    planning = readiness.get("planning")
    if version is not None and (
        isinstance(version, bool) or not isinstance(version, int)
    ):
        raise ValueError("generation readiness version is invalid")
    if version not in (None, 1, 2):
        raise ValueError("generation readiness version is invalid")
    if version in (None, 1) and not isinstance(planning, Mapping):
        return False
    if not isinstance(planning, Mapping):
        raise ValueError("generation readiness planning is missing")
    revision = planning.get("chapter_candidate_pipeline_revision")
    authorization = planning.get(
        "chapter_candidate_repair_authorization"
    )
    if version in (None, 1) and revision is None and authorization is None:
        return False
    if version != 2:
        raise ValueError("candidate repair readiness version is invalid")
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision != CANDIDATE_PIPELINE_REVISION
    ):
        raise ValueError("candidate pipeline authorization revision is invalid")
    if not isinstance(authorization, Mapping):
        raise ValueError("generation readiness has no candidate repair authority")
    return True


def readiness_chapter_uses_candidate_pipeline(
    readiness: Any,
    *,
    chapter_id: str,
) -> bool:
    """Select candidate execution for one frozen chapter snapshot."""

    if not readiness_uses_candidate_pipeline(readiness):
        return False
    assert isinstance(readiness, Mapping)
    work = readiness.get("work")
    raw_chapters = work.get("chapters") if isinstance(work, Mapping) else None
    if not isinstance(raw_chapters, list):
        raise ValueError("generation readiness worklist is invalid")
    normalized_chapter_id = str(chapter_id or "")
    matches: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for raw_chapter in raw_chapters:
        if not isinstance(raw_chapter, Mapping):
            raise ValueError("generation readiness chapter snapshot is invalid")
        snapshot_id = str(raw_chapter.get("chapter_id") or "")
        if not snapshot_id or snapshot_id in seen:
            raise ValueError("generation readiness chapter identity is invalid")
        seen.add(snapshot_id)
        has_content = raw_chapter.get("has_content")
        has_outline = raw_chapter.get("has_outline")
        if type(has_content) is not bool or type(has_outline) is not bool:
            raise ValueError("generation readiness chapter mode is invalid")
        if snapshot_id == normalized_chapter_id:
            matches.append(raw_chapter)
    if len(matches) != 1:
        raise ValueError("chapter is outside the frozen generation worklist")
    selected = matches[0]
    if selected.get("has_content") is True and selected.get("has_outline") is False:
        raise ValueError(
            "existing prose without outline cannot enter the state-only pipeline"
        )
    validate_readiness_narrative_quality_signal_authorization(readiness)
    return selected.get("has_content") is False


class _ClosedAuthorizationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RuntimeToolDescriptorSnapshot(_ClosedAuthorizationModel):
    schema_version: Literal["agent_runtime_tool_descriptor.v1"]
    reference: RuntimeToolReference
    label: str = Field(min_length=1, max_length=240)
    input_schema_digest: _Sha256
    output_schema_digest: _Sha256
    scope_kinds: tuple[str, ...] = Field(min_length=1, max_length=16)
    effect_class: RuntimeEffectClass
    proposal_kinds: tuple[RuntimeProposalKind, ...] = ()
    change_classes: tuple[RuntimeChangeClass, ...] = ()
    max_paid_attempts_per_call: _NonNegativeInt
    max_tokens_per_call: _NonNegativeInt
    implementation_revision: str = Field(min_length=1, max_length=240)
    context_policy_revision: str = Field(min_length=1, max_length=240)
    external_data_categories: tuple[str, ...] = Field(max_length=32)
    idempotent: StrictBool
    retryable_failure_reason_codes: RuntimeFailureReasonCodes = ()


class CandidateStructuredGenerationPlan(_ClosedAuthorizationModel):
    schema_version: Literal["candidate_structured_generation_plan.v4"]
    runtime_budget_protocol: Literal["structured_request_budget.v2"]
    workflow: str = Field(min_length=1, max_length=160)
    step: str = Field(min_length=1, max_length=160)
    provider_alias: str = Field(min_length=1, max_length=160)
    provider_model: str = Field(min_length=1, max_length=240)
    structured_output_mode: Literal[
        "prompt_json",
        "json_object",
        "schema_enforced",
    ]
    reviewer_alias: str | None = Field(default=None, min_length=1, max_length=160)
    thinking_mode: Literal["enabled", "disabled"] | None
    timeout_seconds: _PositiveInt | None = None
    config_revision: str = Field(min_length=1, max_length=240)
    capability_snapshot: str = Field(min_length=1, max_length=240)
    generation_params_digest: _Sha256
    max_paid_attempts_per_call: _PositiveInt
    max_output_tokens_per_attempt: _PositiveInt
    max_context_tokens: _PositiveInt | None
    max_input_tokens_per_attempt: _PositiveInt
    max_tokens_per_call: _PositiveInt

    @model_validator(mode="after")
    def validate_token_bound(self) -> "CandidateStructuredGenerationPlan":
        expected = self.max_paid_attempts_per_call * (
            self.max_input_tokens_per_attempt
            + self.max_output_tokens_per_attempt
        )
        if self.max_tokens_per_call != expected:
            raise ValueError("structured generation token bound changed")
        return self


class CandidateRemediationDeadlineBudget(_ClosedAuthorizationModel):
    schema_version: Literal["candidate_remediation_deadline_budget.v1"]
    max_planner_calls: _PositiveInt
    planner_timeout_seconds: _PositiveInt
    max_tool_calls: _PositiveInt
    maximum_tool_timeout_seconds: _PositiveInt
    serial_logical_call_window_seconds: _PositiveInt
    local_completion_margin_seconds: _PositiveInt
    deadline_seconds: _PositiveInt

    @model_validator(mode="after")
    def validate_derived_deadline(self) -> "CandidateRemediationDeadlineBudget":
        serial_window = (
            self.max_planner_calls * self.planner_timeout_seconds
            + self.max_tool_calls * self.maximum_tool_timeout_seconds
        )
        if self.serial_logical_call_window_seconds != serial_window:
            raise ValueError("candidate remediation serial window changed")
        if self.deadline_seconds != (
            serial_window + self.local_completion_margin_seconds
        ):
            raise ValueError("candidate remediation deadline changed")
        return self


def _required_generation_timeout(
    plan: CandidateStructuredGenerationPlan,
    *,
    label: str,
) -> int:
    timeout_seconds = plan.timeout_seconds
    if timeout_seconds is None:
        raise ValueError(f"candidate remediation {label} timeout is required")
    return timeout_seconds


def _prose_remediation_deadline_budget(
    planner_generation: CandidateStructuredGenerationPlan,
    rewrite_generation: CandidateStructuredGenerationPlan,
    adherence_generation: CandidateStructuredGenerationPlan,
) -> CandidateRemediationDeadlineBudget:
    planner_timeout_seconds = _required_generation_timeout(
        planner_generation,
        label="Planner",
    )
    maximum_tool_timeout_seconds = max(
        _required_generation_timeout(
            rewrite_generation,
            label="rewrite Tool",
        ),
        _required_generation_timeout(
            adherence_generation,
            label="adherence Tool",
        ),
    )
    serial_window = (
        PROSE_REMEDIATION_MAX_PLANNER_CALLS * planner_timeout_seconds
        + PROSE_REMEDIATION_MAX_TOOL_CALLS * maximum_tool_timeout_seconds
    )
    return CandidateRemediationDeadlineBudget(
        schema_version="candidate_remediation_deadline_budget.v1",
        max_planner_calls=PROSE_REMEDIATION_MAX_PLANNER_CALLS,
        planner_timeout_seconds=planner_timeout_seconds,
        max_tool_calls=PROSE_REMEDIATION_MAX_TOOL_CALLS,
        maximum_tool_timeout_seconds=maximum_tool_timeout_seconds,
        serial_logical_call_window_seconds=serial_window,
        local_completion_margin_seconds=(
            PROSE_REMEDIATION_LOCAL_COMPLETION_MARGIN_SECONDS
        ),
        deadline_seconds=(
            serial_window + PROSE_REMEDIATION_LOCAL_COMPLETION_MARGIN_SECONDS
        ),
    )


class CandidateJobGenerationPlan(_ClosedAuthorizationModel):
    """Closed, reconstructable identity for one initial candidate Job call."""

    schema_version: Literal["candidate_job_generation_plan.v1"]
    runtime_budget_protocol: Literal["structured_request_budget.v2"]
    call_kind: Literal["structured", "text"]
    workflow: str = Field(min_length=1, max_length=160)
    step: str = Field(min_length=1, max_length=160)
    provider_alias: str = Field(min_length=1, max_length=160)
    provider_model: str = Field(min_length=1, max_length=240)
    structured_output_mode: Literal[
        "prompt_json",
        "json_object",
        "schema_enforced",
    ]
    reviewer_alias: str | None = Field(default=None, min_length=1, max_length=160)
    timeout_seconds: _PositiveInt | None = None
    config_revision: str = Field(min_length=1, max_length=240)
    capability_snapshot: str = Field(min_length=1, max_length=240)
    max_semantic_attempts: _PositiveInt
    max_output_tokens: _PositiveInt | None = None
    max_context_tokens: _PositiveInt | None = None
    thinking_mode: Literal["enabled", "disabled"] | None = None

    @model_validator(mode="after")
    def validate_call_kind(self) -> "CandidateJobGenerationPlan":
        if self.call_kind == "text":
            if self.reviewer_alias is not None:
                raise ValueError("text generation plan cannot use a reviewer")
        return self


class CandidateJobExecutionAuthorization(_ClosedAuthorizationModel):
    """Frozen Provider plans for the initial candidate-only chapter chain."""

    schema_version: Literal[
        "chapter_candidate_job_execution_authorization.v2"
    ]
    narrative_quality_signal_authorization_digest: _Sha256
    generation_params_digest: _Sha256
    eligible_chapter_count: _NonNegativeInt
    eligible_chapter_ids_digest: _Sha256
    missing_outline_chapter_count: _NonNegativeInt
    outline: CandidateJobGenerationPlan | None
    prose: CandidateJobGenerationPlan | None
    adherence: CandidateJobGenerationPlan | None
    state: CandidateJobGenerationPlan | None

    @model_validator(mode="after")
    def validate_required_plans(self) -> "CandidateJobExecutionAuthorization":
        from backend.services.generation.chapter_generation_application import (
            CHAPTER_OUTLINE_STEP,
            CHAPTER_OUTLINE_WORKFLOW,
            OUTLINE_ADHERENCE_STEP,
            PROSE_REMEDIATION_WORKFLOW,
            PROSE_STEP,
            PROSE_WORKFLOW,
            STATE_STEP,
            STATE_WORKFLOW,
        )

        if self.missing_outline_chapter_count > self.eligible_chapter_count:
            raise ValueError("candidate outline count exceeds eligible chapters")
        required = (self.prose, self.adherence, self.state)
        if self.eligible_chapter_count == 0:
            if self.missing_outline_chapter_count or self.outline is not None or any(
                item is not None for item in required
            ):
                raise ValueError("inactive candidate execution authority is not empty")
            return self
        if any(item is None for item in required):
            raise ValueError("candidate execution authority is incomplete")
        if (self.outline is not None) != bool(self.missing_outline_chapter_count):
            raise ValueError("candidate outline authority changed")
        expected = (
            (
                self.outline,
                "structured",
                CHAPTER_OUTLINE_WORKFLOW,
                CHAPTER_OUTLINE_STEP,
            ),
            (self.prose, "text", PROSE_WORKFLOW, PROSE_STEP),
            (
                self.adherence,
                "structured",
                PROSE_REMEDIATION_WORKFLOW,
                OUTLINE_ADHERENCE_STEP,
            ),
            (self.state, "structured", STATE_WORKFLOW, STATE_STEP),
        )
        for plan, kind, workflow, step in expected:
            if plan is None:
                continue
            if (
                plan.call_kind != kind
                or plan.workflow != workflow
                or plan.step != step
            ):
                raise ValueError("candidate execution plan target changed")
        return self


class ProseRemediationAuthorization(_ClosedAuthorizationModel):
    scope_kind: Literal["chapter_prose_candidate"]
    registry_revision: str = Field(min_length=1, max_length=240)
    allowed_tools: tuple[RuntimeToolReference, ...] = Field(min_length=2, max_length=2)
    allowed_effects: tuple[RuntimeEffectClass, ...] = Field(min_length=2, max_length=2)
    allowed_change_classes: tuple[RuntimeChangeClass, ...] = Field(
        min_length=1,
        max_length=1,
    )
    allowed_external_data_categories: tuple[str, ...] = Field(max_length=32)
    deadline_budget: CandidateRemediationDeadlineBudget
    limits: AgentRuntimeLimits
    planner: PlannerDescriptor
    tools: tuple[RuntimeToolDescriptorSnapshot, ...] = Field(
        min_length=2,
        max_length=2,
    )
    planner_generation: CandidateStructuredGenerationPlan
    rewrite_generation: CandidateStructuredGenerationPlan
    adherence_generation: CandidateStructuredGenerationPlan

    @model_validator(mode="after")
    def validate_runtime_contract(self) -> "ProseRemediationAuthorization":
        from backend.services.generation.prose_remediation_runtime import (
            ADHERENCE_TOOL,
            REWRITE_TOOL,
        )

        expected_references = (REWRITE_TOOL, ADHERENCE_TOOL)
        if self.allowed_tools != expected_references:
            raise ValueError("candidate remediation tool allowlist changed")
        if tuple(item.reference for item in self.tools) != expected_references:
            raise ValueError("candidate remediation Tool snapshots changed")
        if any(self.scope_kind not in item.scope_kinds for item in self.tools):
            raise ValueError("candidate remediation Tool scope changed")
        expected_effects = tuple(
            _ordered_union(tuple(item.effect_class for item in self.tools))
        )
        expected_changes = tuple(
            _ordered_union(*tuple(item.change_classes for item in self.tools))
        )
        expected_external = tuple(
            _ordered_union(
                self.planner.external_data_categories,
                tuple(
                    category
                    for item in self.tools
                    for category in item.external_data_categories
                ),
            )
        )
        if self.allowed_effects != expected_effects:
            raise ValueError("candidate remediation effect allowlist changed")
        if self.allowed_change_classes != expected_changes:
            raise ValueError("candidate remediation change allowlist changed")
        if self.allowed_external_data_categories != expected_external:
            raise ValueError("candidate remediation external-data allowlist changed")
        _validate_remediation_generation_plans(self)
        expected_deadline_budget = _prose_remediation_deadline_budget(
            self.planner_generation,
            self.rewrite_generation,
            self.adherence_generation,
        )
        if self.deadline_budget != expected_deadline_budget:
            raise ValueError("candidate remediation deadline budget changed")
        bounds = _prose_remediation_bounds(self)
        expected_limits = AgentRuntimeLimits(
            max_steps=PROSE_REMEDIATION_MAX_STEPS,
            max_planner_calls=PROSE_REMEDIATION_MAX_PLANNER_CALLS,
            max_tool_calls=PROSE_REMEDIATION_MAX_TOOL_CALLS,
            max_paid_attempts=bounds.paid_attempts,
            token_budget=bounds.tokens,
            deadline_seconds=expected_deadline_budget.deadline_seconds,
            max_predispatch_retries=PROSE_REMEDIATION_MAX_PREDISPATCH_RETRIES,
            max_planner_repairs=PROSE_REMEDIATION_MAX_PLANNER_REPAIRS,
            max_tool_retries=PROSE_REMEDIATION_MAX_TOOL_RETRIES,
        )
        if self.limits != expected_limits:
            raise ValueError("candidate remediation Runtime limits changed")
        return self


class CandidateProviderBudgetBound(_ClosedAuthorizationModel):
    provider_alias: str = Field(min_length=1, max_length=160)
    maximum_paid_attempts_per_cycle: _NonNegativeInt
    maximum_paid_attempts_total: _NonNegativeInt
    maximum_tokens_per_cycle: _NonNegativeInt
    maximum_tokens_total: _NonNegativeInt


class CandidateRepairAuthorization(_ClosedAuthorizationModel):
    schema_version: Literal["chapter_candidate_repair_authorization.v14"]
    narrative_quality_signal_authorization_digest: _Sha256
    authorization_revision: _PositiveInt
    eligible_chapter_count: _NonNegativeInt
    eligible_chapter_ids_digest: _Sha256
    max_repair_cycles_per_chapter: Annotated[
        StrictInt,
        Field(ge=0, le=MAX_CHAPTER_CANDIDATE_COMPONENT_REPAIRS),
    ]
    maximum_repair_events_per_chapter: Annotated[
        StrictInt,
        Field(ge=0, le=MAX_FINALIZATION_REPAIR_CYCLES),
    ]
    component_limits: RepairBudgetLimitsV1
    maximum_provider_attempts_per_cycle: _NonNegativeInt
    maximum_provider_attempts_per_chapter: _NonNegativeInt
    maximum_provider_attempts_total: _NonNegativeInt
    maximum_tokens_per_cycle: _NonNegativeInt
    maximum_tokens_per_chapter: _NonNegativeInt
    maximum_tokens_total: _NonNegativeInt
    provider_bounds: tuple[CandidateProviderBudgetBound, ...]
    prose_remediation: ProseRemediationAuthorization | None
    adherence_review: CandidateStructuredGenerationPlan | None
    state_repair: CandidateStructuredGenerationPlan | None

    @model_validator(mode="after")
    def validate_derived_bounds(self) -> "CandidateRepairAuthorization":
        inactive = (
            self.eligible_chapter_count == 0
            or self.max_repair_cycles_per_chapter == 0
        )
        adapters = (
            self.prose_remediation,
            self.adherence_review,
            self.state_repair,
        )
        if inactive:
            if self.component_limits != _zero_component_limits():
                raise ValueError("inactive candidate component limits are not empty")
            if any(item is not None for item in adapters) or any(
                (
                    self.maximum_repair_events_per_chapter,
                    self.maximum_provider_attempts_per_cycle,
                    self.maximum_provider_attempts_per_chapter,
                    self.maximum_provider_attempts_total,
                    self.maximum_tokens_per_cycle,
                    self.maximum_tokens_per_chapter,
                    self.maximum_tokens_total,
                )
            ) or self.provider_bounds:
                raise ValueError("inactive candidate repair authority is not empty")
            return self
        if any(item is None for item in adapters):
            raise ValueError("candidate repair authority is incomplete")
        assert self.prose_remediation is not None
        assert self.adherence_review is not None
        assert self.state_repair is not None
        component_limits = _candidate_component_limits(
            self.max_repair_cycles_per_chapter,
            self.prose_remediation,
            self.adherence_review,
            self.state_repair,
        )
        if self.component_limits != component_limits:
            raise ValueError("candidate repair component limits changed")
        expected_events = _candidate_repair_event_count(component_limits)
        if self.maximum_repair_events_per_chapter != expected_events:
            raise ValueError("candidate repair event bound changed")
        cycle_bounds, chapter_bounds = _candidate_authorized_bounds(
            self.prose_remediation,
            self.adherence_review,
            self.state_repair,
            component_limits,
        )
        if (
            self.maximum_provider_attempts_per_cycle
            != cycle_bounds.paid_attempts
            or self.maximum_provider_attempts_per_chapter
            != chapter_bounds.paid_attempts
            or self.maximum_provider_attempts_total
            != self.eligible_chapter_count * chapter_bounds.paid_attempts
            or self.maximum_tokens_per_cycle != cycle_bounds.tokens
            or self.maximum_tokens_per_chapter != chapter_bounds.tokens
            or self.maximum_tokens_total
            != self.eligible_chapter_count * chapter_bounds.tokens
        ):
            raise ValueError("candidate repair aggregate bounds changed")
        chapter_provider_bounds = {
            bound.provider_alias: bound
            for bound in chapter_bounds.provider_bounds
        }
        expected_provider_bounds = tuple(
            CandidateProviderBudgetBound(
                provider_alias=bound.provider_alias,
                maximum_paid_attempts_per_cycle=bound.paid_attempts,
                maximum_paid_attempts_total=(
                    chapter_provider_bounds[bound.provider_alias].paid_attempts
                    * self.eligible_chapter_count
                ),
                maximum_tokens_per_cycle=bound.tokens,
                maximum_tokens_total=(
                    chapter_provider_bounds[bound.provider_alias].tokens
                    * self.eligible_chapter_count
                ),
            )
            for bound in cycle_bounds.provider_bounds
        )
        if self.provider_bounds != expected_provider_bounds:
            raise ValueError("candidate repair Provider bounds changed")
        return self


@dataclass(frozen=True)
class _BudgetBounds:
    paid_attempts: int
    tokens: int
    provider_bounds: tuple[ProviderBudgetBound, ...]


def _projection_provider_bounds(
    plan: CandidateStructuredGenerationPlan,
) -> tuple[ProviderBudgetBound, ...]:
    return structured_provider_bounds(
        provider_alias=plan.provider_alias,
        reviewer_alias=plan.reviewer_alias,
        max_paid_attempts=plan.max_paid_attempts_per_call,
        input_tokens_per_attempt=plan.max_input_tokens_per_attempt,
        output_tokens_per_attempt=plan.max_output_tokens_per_attempt,
    )


def _prose_remediation_bounds(
    authorization: ProseRemediationAuthorization,
) -> _BudgetBounds:
    paid_attempts, tokens = _prose_descriptor_totals(
        authorization.planner,
        authorization.tools,
    )
    planner_bounds = scale_provider_bounds(
        _projection_provider_bounds(authorization.planner_generation),
        PROSE_REMEDIATION_MAX_PLANNER_CALLS,
    )
    rewrite_bounds = scale_provider_bounds(
        _projection_provider_bounds(authorization.rewrite_generation),
        PROSE_REMEDIATION_MAX_TOOL_CALLS,
    )
    adherence_bounds = scale_provider_bounds(
        _projection_provider_bounds(authorization.adherence_generation),
        PROSE_REMEDIATION_MAX_TOOL_CALLS,
    )
    return _BudgetBounds(
        paid_attempts=paid_attempts,
        tokens=tokens,
        provider_bounds=merge_provider_bounds(
            planner_bounds,
            max_provider_bounds(rewrite_bounds, adherence_bounds),
        ),
    )


def _prose_descriptor_totals(
    planner: PlannerDescriptor,
    tools: Sequence[RuntimeToolDescriptor | RuntimeToolDescriptorSnapshot],
) -> tuple[int, int]:
    maximum_tool_paid = max(
        item.max_paid_attempts_per_call for item in tools
    )
    maximum_tool_tokens = max(item.max_tokens_per_call for item in tools)
    return (
        PROSE_REMEDIATION_MAX_PLANNER_CALLS
        * planner.max_paid_attempts_per_call
        + PROSE_REMEDIATION_MAX_TOOL_CALLS * maximum_tool_paid,
        PROSE_REMEDIATION_MAX_PLANNER_CALLS * planner.max_tokens_per_call
        + PROSE_REMEDIATION_MAX_TOOL_CALLS * maximum_tool_tokens,
    )


def _candidate_branch_bounds(
    prose_remediation: ProseRemediationAuthorization,
    adherence_review: CandidateStructuredGenerationPlan,
    state_repair: CandidateStructuredGenerationPlan,
) -> tuple[_BudgetBounds, _BudgetBounds]:
    prose = _prose_remediation_bounds(prose_remediation)
    adherence_provider = _projection_provider_bounds(adherence_review)
    state_provider = _projection_provider_bounds(state_repair)
    prose_branch = _BudgetBounds(
        paid_attempts=(
            prose.paid_attempts
            + adherence_review.max_paid_attempts_per_call
        ),
        tokens=(
            prose.tokens + adherence_review.max_tokens_per_call
        ),
        provider_bounds=merge_provider_bounds(
            prose.provider_bounds,
            adherence_provider,
        ),
    )
    state_branch = _BudgetBounds(
        paid_attempts=state_repair.max_paid_attempts_per_call,
        tokens=state_repair.max_tokens_per_call,
        provider_bounds=state_provider,
    )
    return prose_branch, state_branch


def _candidate_cycle_bounds(
    prose_remediation: ProseRemediationAuthorization,
    adherence_review: CandidateStructuredGenerationPlan,
    state_repair: CandidateStructuredGenerationPlan,
) -> _BudgetBounds:
    prose_branch, state_branch = _candidate_branch_bounds(
        prose_remediation,
        adherence_review,
        state_repair,
    )
    return _BudgetBounds(
        paid_attempts=max(
            prose_branch.paid_attempts,
            state_branch.paid_attempts,
        ),
        tokens=max(prose_branch.tokens, state_branch.tokens),
        provider_bounds=max_provider_bounds(
            prose_branch.provider_bounds,
            state_branch.provider_bounds,
        ),
    )


def _zero_component_limits() -> RepairBudgetLimitsV1:
    return RepairBudgetLimitsV1(
        provider_technical_retry=0,
        adherence_judge_retry=0,
        state_reextraction=0,
        local_prose_repair=0,
        scene_regeneration=0,
        outline_rollback=0,
    )


def _candidate_component_limits(
    max_component_repairs: int,
    prose_remediation: ProseRemediationAuthorization,
    adherence_review: CandidateStructuredGenerationPlan,
    state_repair: CandidateStructuredGenerationPlan,
) -> RepairBudgetLimitsV1:
    prose = _prose_remediation_bounds(prose_remediation)
    prose_logical_calls = (
        PROSE_REMEDIATION_MAX_PLANNER_CALLS
        + PROSE_REMEDIATION_MAX_TOOL_CALLS
    )
    prose_schema_retries = prose.paid_attempts - prose_logical_calls
    adherence_schema_retries = adherence_review.max_paid_attempts_per_call - 1
    state_schema_retries = state_repair.max_paid_attempts_per_call - 1
    content_events = max_component_repairs * 2
    return RepairBudgetLimitsV1(
        # Candidate runtimes are constructed with max_provider_retries=0.
        provider_technical_retry=0,
        adherence_judge_retry=(
            content_events
            * (prose_schema_retries + adherence_schema_retries)
            + max_component_repairs * state_schema_retries
        ),
        state_reextraction=max_component_repairs,
        local_prose_repair=max_component_repairs,
        scene_regeneration=max_component_repairs,
        outline_rollback=0,
    )


def _candidate_repair_event_count(limits: RepairBudgetLimitsV1) -> int:
    return (
        limits.state_reextraction
        + limits.local_prose_repair
        + limits.scene_regeneration
        + limits.outline_rollback
    )


def _candidate_authorized_bounds(
    prose_remediation: ProseRemediationAuthorization,
    adherence_review: CandidateStructuredGenerationPlan,
    state_repair: CandidateStructuredGenerationPlan,
    limits: RepairBudgetLimitsV1,
) -> tuple[_BudgetBounds, _BudgetBounds]:
    prose_branch, state_branch = _candidate_branch_bounds(
        prose_remediation,
        adherence_review,
        state_repair,
    )
    cycle_bounds = _candidate_cycle_bounds(
        prose_remediation,
        adherence_review,
        state_repair,
    )
    content_events = limits.local_prose_repair + limits.scene_regeneration
    chapter_provider_bounds = merge_provider_bounds(
        scale_provider_bounds(prose_branch.provider_bounds, content_events),
        scale_provider_bounds(
            state_branch.provider_bounds,
            limits.state_reextraction,
        ),
    )
    chapter_bounds = _BudgetBounds(
        paid_attempts=(
            prose_branch.paid_attempts * content_events
            + state_branch.paid_attempts * limits.state_reextraction
        ),
        tokens=(
            prose_branch.tokens * content_events
            + state_branch.tokens * limits.state_reextraction
        ),
        provider_bounds=chapter_provider_bounds,
    )
    return cycle_bounds, chapter_bounds


def _validate_remediation_generation_plans(
    authorization: ProseRemediationAuthorization,
) -> None:
    from backend.services.generation.prose_remediation_runtime import (
        OUTLINE_ADHERENCE_STEP,
        PROSE_CANDIDATE_REWRITE_STEP,
        PROSE_REMEDIATION_WORKFLOW,
        REMEDIATION_PLANNER_STEP,
    )

    plans = (
        authorization.planner_generation,
        authorization.rewrite_generation,
        authorization.adherence_generation,
    )
    targets = (
        (PROSE_REMEDIATION_WORKFLOW, REMEDIATION_PLANNER_STEP),
        (PROSE_REMEDIATION_WORKFLOW, PROSE_CANDIDATE_REWRITE_STEP),
        (PROSE_REMEDIATION_WORKFLOW, OUTLINE_ADHERENCE_STEP),
    )
    if tuple((item.workflow, item.step) for item in plans) != targets:
        raise ValueError("candidate remediation generation targets changed")
    if any(item.generation_params_digest != _mapping_digest(None) for item in plans):
        raise ValueError("candidate remediation internal parameters changed")
    planner_plan = authorization.planner_generation
    if (
        planner_plan.provider_alias != authorization.planner.provider_alias
        or planner_plan.provider_model != authorization.planner.provider_model
        or planner_plan.max_paid_attempts_per_call
        != authorization.planner.max_paid_attempts_per_call
        or planner_plan.max_tokens_per_call
        != authorization.planner.max_tokens_per_call
    ):
        raise ValueError("candidate remediation Planner plan changed")
    for descriptor, plan in zip(
        authorization.tools,
        (authorization.rewrite_generation, authorization.adherence_generation),
        strict=True,
    ):
        if (
            plan.max_paid_attempts_per_call
            != descriptor.max_paid_attempts_per_call
            or plan.max_tokens_per_call != descriptor.max_tokens_per_call
        ):
            raise ValueError("candidate remediation Tool Provider plan changed")


def _canonical_json_projection(value: Any, *, field: str) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} is not valid JSON") from exc


def parse_candidate_repair_authorization(
    value: Mapping[str, Any],
) -> CandidateRepairAuthorization:
    try:
        parsed = CandidateRepairAuthorization.model_validate(value)
    except ValidationError as exc:
        raise ValueError("candidate repair authorization is invalid") from exc
    raw_json = _canonical_json_projection(
        dict(value),
        field="candidate repair authorization",
    )
    canonical_json = _canonical_json_projection(
        parsed.model_dump(mode="json"),
        field="canonical candidate repair authorization",
    )
    if raw_json != canonical_json:
        raise ValueError(
            "candidate repair authorization requires exact JSON types"
        )
    return parsed


def _strict_positive_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _strict_non_negative_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _eligible_chapter_ids(
    chapters: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    identifiers: list[str] = []
    for chapter in chapters:
        if str(chapter.get("content") or "").strip():
            continue
        chapter_id = str(chapter.get("_id") or "")
        if not chapter_id:
            raise ValueError("candidate repair authorization requires chapter ids")
        identifiers.append(chapter_id)
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("candidate repair authorization has duplicate chapter ids")
    return tuple(identifiers)


def _chapter_ids_digest(chapter_ids: tuple[str, ...]) -> str:
    encoded = json.dumps(
        list(chapter_ids),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _mapping_digest(value: Mapping[str, Any] | None) -> str:
    try:
        encoded = json.dumps(
            dict(value or {}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("candidate repair generation params are invalid") from exc
    return hashlib.sha256(encoded).hexdigest()


def _candidate_job_generation_params_digest(
    value: Mapping[str, Any] | None,
) -> str:
    public = dict(value or {})
    public.pop("_internal_readiness_prose_prompt_input_bounds", None)
    return _mapping_digest(public)


@dataclass(frozen=True)
class CandidateJobGenerationPlans:
    outline: GenerationPlan | None
    prose: GenerationPlan | None
    adherence: GenerationPlan | None
    state: GenerationPlan | None


@dataclass(frozen=True)
class CandidateJobGenerationRequirements:
    """Provider-plan requirements derived from the entire frozen worklist."""

    active: bool
    needs_outline: bool


def _candidate_job_plan_projection(
    plan: GenerationPlan,
    *,
    call_kind: Literal["structured", "text"],
) -> CandidateJobGenerationPlan:
    if not isinstance(plan, GenerationPlan) or not isinstance(
        plan.target, WorkflowStepTarget
    ):
        raise ValueError("candidate Job GenerationPlan is invalid")
    return CandidateJobGenerationPlan.model_validate({
        "schema_version": CANDIDATE_JOB_GENERATION_PLAN_SCHEMA,
        "runtime_budget_protocol": STRUCTURED_REQUEST_BUDGET_PROTOCOL,
        "call_kind": call_kind,
        "workflow": plan.target.workflow_name,
        "step": plan.target.step_name,
        "provider_alias": plan.provider_alias,
        "provider_model": plan.provider_model,
        "structured_output_mode": plan.mode.value,
        "reviewer_alias": plan.reviewer_alias,
        "timeout_seconds": plan.timeout_seconds,
        "config_revision": plan.config_revision,
        "capability_snapshot": plan.capability_snapshot,
        "max_semantic_attempts": plan.max_semantic_attempts,
        "max_output_tokens": plan.max_output_tokens,
        "max_context_tokens": plan.max_context_tokens,
        "thinking_mode": plan.thinking_mode,
    })


def candidate_job_generation_plan_snapshot(
    plan: GenerationPlan,
    *,
    call_kind: Literal["structured", "text"],
) -> CandidateJobGenerationPlan:
    """Freeze one runtime plan through the shared closed plan contract."""

    return _candidate_job_plan_projection(plan, call_kind=call_kind)


def generation_plan_from_candidate_snapshot(
    value: CandidateJobGenerationPlan,
) -> GenerationPlan:
    """Rebuild the immutable runtime plan without consulting live config."""

    if not isinstance(value, CandidateJobGenerationPlan):
        raise ValueError("candidate Job plan snapshot is invalid")
    return GenerationPlan(
        target=WorkflowStepTarget(value.workflow, value.step),
        provider_alias=value.provider_alias,
        timeout_seconds=value.timeout_seconds,
        mode=StructuredOutputMode(value.structured_output_mode),
        reviewer_alias=value.reviewer_alias,
        config_revision=value.config_revision,
        capability_snapshot=value.capability_snapshot,
        max_semantic_attempts=value.max_semantic_attempts,
        provider_model=value.provider_model,
        max_output_tokens=value.max_output_tokens,
        max_context_tokens=value.max_context_tokens,
        thinking_mode=value.thinking_mode,
    )


def parse_candidate_job_execution_authorization(
    value: Any,
) -> CandidateJobExecutionAuthorization:
    try:
        parsed = CandidateJobExecutionAuthorization.model_validate(value)
    except ValidationError as exc:
        raise ValueError("candidate Job execution authorization is invalid") from exc
    raw_json = _canonical_json_projection(
        dict(value) if isinstance(value, Mapping) else value,
        field="candidate Job execution authorization",
    )
    canonical_json = _canonical_json_projection(
        parsed.model_dump(mode="json"),
        field="canonical candidate Job execution authorization",
    )
    if raw_json != canonical_json:
        raise ValueError(
            "candidate Job execution authorization requires exact JSON types"
        )
    return parsed


def candidate_job_generation_requirements(
    readiness: Mapping[str, Any],
) -> CandidateJobGenerationRequirements:
    """Resolve plan requirements without narrowing them to the current chapter."""

    if not readiness_uses_candidate_pipeline(readiness):
        raise ValueError("readiness has no candidate Job execution authority")
    planning = readiness.get("planning")
    if not isinstance(planning, Mapping):
        raise ValueError("candidate Job readiness planning is invalid")
    authorization = parse_candidate_job_execution_authorization(
        planning.get("chapter_candidate_job_execution_authorization")
    )
    quality_authorization = (
        validate_readiness_narrative_quality_signal_authorization(readiness)
    )
    if authorization.narrative_quality_signal_authorization_digest != (
        narrative_quality_signal_authorization_digest(quality_authorization)
    ):
        raise ValueError("candidate Job quality-signal authority changed")
    return CandidateJobGenerationRequirements(
        active=authorization.eligible_chapter_count > 0,
        needs_outline=authorization.missing_outline_chapter_count > 0,
    )


def plan_candidate_job_generation(
    *,
    needs_outline: bool,
    active: bool,
) -> CandidateJobGenerationPlans:
    """Resolve every initial candidate Provider plan from one runtime seam."""

    if not active:
        return CandidateJobGenerationPlans(None, None, None, None)
    from backend.services.generation.chapter_generation_application import (
        CHAPTER_OUTLINE_STEP,
        CHAPTER_OUTLINE_WORKFLOW,
        OUTLINE_ADHERENCE_STEP,
        PROSE_REMEDIATION_WORKFLOW,
        PROSE_STEP,
        PROSE_WORKFLOW,
        STATE_STEP,
        STATE_WORKFLOW,
    )
    from backend.services.llm.generation_runtime import create_generation_runtime

    runtime = create_generation_runtime(max_provider_retries=0)
    return CandidateJobGenerationPlans(
        outline=(
            runtime.plan_structured(
                WorkflowStepTarget(
                    CHAPTER_OUTLINE_WORKFLOW,
                    CHAPTER_OUTLINE_STEP,
                )
            )
            if needs_outline
            else None
        ),
        prose=runtime.plan_text(WorkflowStepTarget(PROSE_WORKFLOW, PROSE_STEP)),
        adherence=runtime.plan_structured(
            WorkflowStepTarget(
                PROSE_REMEDIATION_WORKFLOW,
                OUTLINE_ADHERENCE_STEP,
            )
        ),
        state=runtime.plan_structured(
            WorkflowStepTarget(STATE_WORKFLOW, STATE_STEP)
        ),
    )


def build_candidate_job_execution_authorization(
    chapters: Sequence[Mapping[str, Any]],
    generation_params: Mapping[str, Any] | None = None,
    *,
    plans: CandidateJobGenerationPlans | None = None,
) -> dict[str, Any]:
    eligible_ids = _eligible_chapter_ids(chapters)
    quality_authorization = build_narrative_quality_signal_authorization(
        chapters
    )
    missing_outline_count = sum(
        not bool(chapter.get("outline"))
        for chapter in chapters
        if not str(chapter.get("content") or "").strip()
    )
    resolved = plans or plan_candidate_job_generation(
        needs_outline=bool(missing_outline_count),
        active=bool(eligible_ids),
    )
    projection = CandidateJobExecutionAuthorization.model_validate({
        "schema_version": CANDIDATE_JOB_EXECUTION_AUTHORIZATION_SCHEMA,
        "narrative_quality_signal_authorization_digest": (
            narrative_quality_signal_authorization_digest(
                quality_authorization
            )
        ),
        "generation_params_digest": _candidate_job_generation_params_digest(
            generation_params
        ),
        "eligible_chapter_count": len(eligible_ids),
        "eligible_chapter_ids_digest": _chapter_ids_digest(eligible_ids),
        "missing_outline_chapter_count": missing_outline_count,
        "outline": (
            _candidate_job_plan_projection(
                resolved.outline,
                call_kind="structured",
            ).model_dump(mode="json")
            if resolved.outline is not None
            else None
        ),
        "prose": (
            _candidate_job_plan_projection(
                resolved.prose,
                call_kind="text",
            ).model_dump(mode="json")
            if resolved.prose is not None
            else None
        ),
        "adherence": (
            _candidate_job_plan_projection(
                resolved.adherence,
                call_kind="structured",
            ).model_dump(mode="json")
            if resolved.adherence is not None
            else None
        ),
        "state": (
            _candidate_job_plan_projection(
                resolved.state,
                call_kind="structured",
            ).model_dump(mode="json")
            if resolved.state is not None
            else None
        ),
    })
    return projection.model_dump(mode="json")


def validate_candidate_job_execution_authorization(
    readiness: Mapping[str, Any],
    *,
    chapter_id: str,
    generation_params: Mapping[str, Any] | None,
    live_plans: CandidateJobGenerationPlans,
) -> CandidateJobExecutionAuthorization:
    """Match live Provider plans to one signed readiness before reservation."""

    if not readiness_chapter_uses_candidate_pipeline(
        readiness,
        chapter_id=chapter_id,
    ):
        raise ValueError("chapter has no candidate Job execution authority")
    planning = readiness.get("planning")
    if not isinstance(planning, Mapping):
        raise ValueError("candidate Job readiness planning is invalid")
    authorization = parse_candidate_job_execution_authorization(
        planning.get("chapter_candidate_job_execution_authorization")
    )
    work = readiness.get("work")
    raw_chapters = work.get("chapters") if isinstance(work, Mapping) else None
    if not isinstance(raw_chapters, list):
        raise ValueError("candidate Job readiness worklist is invalid")
    eligible = tuple(
        str(item.get("chapter_id") or "")
        for item in raw_chapters
        if isinstance(item, Mapping) and item.get("has_content") is False
    )
    if (
        not all(eligible)
        or len(eligible) != len(set(eligible))
        or authorization.eligible_chapter_count != len(eligible)
        or authorization.eligible_chapter_ids_digest
        != _chapter_ids_digest(eligible)
        or authorization.generation_params_digest
        != _candidate_job_generation_params_digest(generation_params)
    ):
        raise ValueError("candidate Job execution scope changed")
    expected = build_candidate_job_execution_authorization(
        [
            {
                "_id": str(item.get("chapter_id") or ""),
                "content": "" if item.get("has_content") is False else "present",
                "outline": (
                    {
                        "scenes": [
                            {}
                            for _index in range(int(item.get("scene_count") or 0))
                        ]
                    }
                    if item.get("has_outline") is True
                    else None
                ),
            }
            for item in raw_chapters
            if isinstance(item, Mapping)
        ],
        generation_params,
        plans=live_plans,
    )
    if authorization.model_dump(mode="json") != expected:
        raise ValueError("candidate Job Provider plan changed after readiness")
    return authorization


def _ordered_union(*values: Sequence[str]) -> list[str]:
    result: list[str] = []
    for items in values:
        for item in items:
            normalized = str(item)
            if normalized and normalized not in result:
                result.append(normalized)
    return result


def _planner_projection(descriptor: PlannerDescriptor) -> dict[str, Any]:
    return descriptor.model_dump(mode="json")


def _tool_projection(descriptor: RuntimeToolDescriptor) -> dict[str, Any]:
    _strict_non_negative_int(
        descriptor.max_paid_attempts_per_call,
        field="tool paid-attempt bound",
    )
    _strict_non_negative_int(
        descriptor.max_tokens_per_call,
        field="tool token bound",
    )
    return runtime_tool_descriptor_snapshot(descriptor)


def _structured_plan_projection(
    plan: GenerationPlan,
    *,
    workflow: str,
    step: str,
    generation_params: Mapping[str, Any] | None,
    input_token_bound: int | None = None,
    output_token_bound: int | None = None,
) -> dict[str, Any]:
    if not isinstance(plan, GenerationPlan):
        raise ValueError("candidate repair GenerationPlan is invalid")
    if (
        not isinstance(plan.target, WorkflowStepTarget)
        or plan.target.workflow_name != workflow
        or plan.target.step_name != step
    ):
        raise ValueError("candidate repair GenerationPlan target changed")
    provider_alias = str(plan.provider_alias or "")
    provider_model = str(plan.provider_model or "")
    config_revision = str(plan.config_revision or "")
    capability_snapshot = str(plan.capability_snapshot or "")
    mode = str(getattr(plan.mode, "value", plan.mode) or "")
    if not all(
        (
            provider_alias,
            provider_model,
            config_revision,
            capability_snapshot,
            mode,
        )
    ):
        raise ValueError("candidate repair GenerationPlan identity is incomplete")
    raw_max_tokens = dict(generation_params or {}).get("max_tokens")
    budget = structured_call_budget(
        plan,
        input_token_bound=input_token_bound,
        output_token_bound=(
            output_token_bound
            if output_token_bound is not None
            else raw_max_tokens
        ),
    )
    timeout_seconds = plan.timeout_seconds
    if timeout_seconds is not None:
        timeout_seconds = _strict_positive_int(
            timeout_seconds,
            field="candidate repair timeout",
        )
    return {
        "schema_version": CANDIDATE_STRUCTURED_PLAN_SCHEMA,
        "runtime_budget_protocol": STRUCTURED_REQUEST_BUDGET_PROTOCOL,
        "workflow": workflow,
        "step": step,
        "provider_alias": provider_alias,
        "provider_model": provider_model,
        "structured_output_mode": mode,
        "reviewer_alias": (
            str(plan.reviewer_alias) if plan.reviewer_alias else None
        ),
        "thinking_mode": plan.thinking_mode,
        "timeout_seconds": timeout_seconds,
        "config_revision": config_revision,
        "capability_snapshot": capability_snapshot,
        "generation_params_digest": _mapping_digest(generation_params),
        "max_paid_attempts_per_call": budget.max_paid_attempts,
        "max_output_tokens_per_attempt": (
            budget.max_output_tokens_per_attempt
        ),
        "max_context_tokens": budget.max_context_tokens,
        "max_input_tokens_per_attempt": (
            budget.max_input_tokens_per_attempt
        ),
        "max_tokens_per_call": budget.max_tokens_per_call,
    }


def _runtime_call_projection(
    call: Any,
    descriptor: PlannerDescriptor | RuntimeToolDescriptor,
    *,
    workflow: str,
    step: str,
) -> dict[str, Any]:
    plan = getattr(call, "plan", None)
    if not isinstance(plan, GenerationPlan):
        raise ValueError("candidate remediation Provider plan is missing")
    attempts = _strict_positive_int(
        getattr(call, "max_paid_attempts", None),
        field="candidate remediation paid-attempt bound",
    )
    output_tokens = _strict_positive_int(
        getattr(call, "output_token_bound", None),
        field="candidate remediation output-token bound",
    )
    descriptor_attempts = _strict_positive_int(
        descriptor.max_paid_attempts_per_call,
        field="candidate remediation descriptor paid-attempt bound",
    )
    descriptor_tokens = _strict_positive_int(
        descriptor.max_tokens_per_call,
        field="candidate remediation descriptor token bound",
    )
    if attempts != descriptor_attempts or descriptor_tokens % attempts:
        raise ValueError("candidate remediation descriptor budget changed")
    input_tokens = descriptor_tokens // attempts - output_tokens
    projection = _structured_plan_projection(
        plan,
        workflow=workflow,
        step=step,
        generation_params=None,
        input_token_bound=input_tokens,
        output_token_bound=output_tokens,
    )
    if projection["max_tokens_per_call"] != descriptor_tokens:
        raise ValueError("candidate remediation descriptor token bound changed")
    return projection


def _production_remediation_inputs() -> tuple[Any, GenerationPlan, GenerationPlan]:
    from backend.services.generation.chapter_generation_application import (
        OUTLINE_ADHERENCE_STEP,
        PROSE_REMEDIATION_WORKFLOW,
        STATE_STEP,
        STATE_WORKFLOW,
    )
    from backend.services.generation.prose_remediation_runtime import (
        build_prose_remediation_runtime,
    )
    from backend.services.llm.generation_runtime import (
        WorkflowStepTarget,
        create_generation_runtime,
    )

    bundle = build_prose_remediation_runtime()
    runtime = create_generation_runtime(max_provider_retries=0)
    adherence_plan = runtime.plan_structured(
        WorkflowStepTarget(
            PROSE_REMEDIATION_WORKFLOW,
            OUTLINE_ADHERENCE_STEP,
        )
    )
    state_plan = runtime.plan_structured(
        WorkflowStepTarget(STATE_WORKFLOW, STATE_STEP)
    )
    if PROSE_REMEDIATION_WORKFLOW != "remediate_chapter_prose_by_agent":
        raise ValueError("prose remediation workflow identity changed")
    return bundle, adherence_plan, state_plan


def build_chapter_candidate_repair_authorization(
    chapters: Sequence[Mapping[str, Any]],
    authorization_revision: int,
    max_repair_cycles: int,
    generation_params: Mapping[str, Any] | None = None,
    *,
    remediation_bundle: Any | None = None,
    adherence_plan: GenerationPlan | None = None,
    state_plan: GenerationPlan | None = None,
) -> dict[str, Any]:
    """Freeze every production repair adapter before a batch job is started.

    The returned projection contains identities and worst-case paid-attempt
    bounds only. It never contains prompts, prose, credentials, or schemas.
    """
    revision = _strict_positive_int(
        authorization_revision,
        field="candidate repair authorization revision",
    )
    cycles = _strict_non_negative_int(
        max_repair_cycles,
        field="candidate repair cycle bound",
    )
    if cycles > MAX_CHAPTER_CANDIDATE_COMPONENT_REPAIRS:
        raise ValueError("candidate repair cycle bound exceeds V1")
    eligible_ids = _eligible_chapter_ids(chapters)
    quality_authorization = build_narrative_quality_signal_authorization(
        chapters
    )
    base = {
        "schema_version": CANDIDATE_REPAIR_AUTHORIZATION_SCHEMA,
        "narrative_quality_signal_authorization_digest": (
            narrative_quality_signal_authorization_digest(
                quality_authorization
            )
        ),
        "authorization_revision": revision,
        "eligible_chapter_count": len(eligible_ids),
        "eligible_chapter_ids_digest": _chapter_ids_digest(eligible_ids),
        "max_repair_cycles_per_chapter": cycles,
    }
    if not eligible_ids or cycles == 0:
        return CandidateRepairAuthorization.model_validate({
            **base,
            "maximum_repair_events_per_chapter": 0,
            "component_limits": _zero_component_limits(),
            "maximum_provider_attempts_per_cycle": 0,
            "maximum_provider_attempts_per_chapter": 0,
            "maximum_provider_attempts_total": 0,
            "maximum_tokens_per_cycle": 0,
            "maximum_tokens_per_chapter": 0,
            "maximum_tokens_total": 0,
            "provider_bounds": [],
            "prose_remediation": None,
            "adherence_review": None,
            "state_repair": None,
        }).model_dump(mode="json")

    if (
        remediation_bundle is None
        or adherence_plan is None
        or state_plan is None
    ):
        production_bundle, production_adherence_plan, production_state_plan = (
            _production_remediation_inputs()
        )
        if remediation_bundle is None:
            remediation_bundle = production_bundle
        if adherence_plan is None:
            adherence_plan = production_adherence_plan
        if state_plan is None:
            state_plan = production_state_plan

    planner = remediation_bundle.planner.descriptor
    if not isinstance(planner, PlannerDescriptor):
        raise ValueError("candidate remediation planner descriptor is invalid")

    from backend.services.generation.prose_remediation_runtime import (
        ADHERENCE_TOOL,
        PROSE_CANDIDATE_REWRITE_STEP,
        REMEDIATION_SCOPE_KIND,
        REMEDIATION_PLANNER_STEP,
        REWRITE_TOOL,
    )
    from backend.services.generation.chapter_generation_application import (
        OUTLINE_ADHERENCE_STEP,
        PROSE_REMEDIATION_WORKFLOW,
        STATE_STEP,
        STATE_WORKFLOW,
    )

    if REMEDIATION_SCOPE_KIND != PROSE_REMEDIATION_SCOPE_KIND:
        raise ValueError("candidate remediation scope identity changed")
    references = (REWRITE_TOOL, ADHERENCE_TOOL)
    tools = tuple(
        remediation_bundle.tools.describe(reference)
        for reference in references
    )
    if any(
        not isinstance(descriptor, RuntimeToolDescriptor)
        or descriptor.reference != reference
        for descriptor, reference in zip(tools, references, strict=True)
    ):
        raise ValueError("candidate remediation tool registry drifted")

    prose_paid_attempts, prose_token_bound = _prose_descriptor_totals(
        planner,
        tools,
    )
    planner_generation = CandidateStructuredGenerationPlan.model_validate(
        _runtime_call_projection(
            remediation_bundle.planner_call,
            planner,
            workflow=PROSE_REMEDIATION_WORKFLOW,
            step=REMEDIATION_PLANNER_STEP,
        )
    )
    rewrite_generation = CandidateStructuredGenerationPlan.model_validate(
        _runtime_call_projection(
            remediation_bundle.rewrite_call,
            tools[0],
            workflow=PROSE_REMEDIATION_WORKFLOW,
            step=PROSE_CANDIDATE_REWRITE_STEP,
        )
    )
    adherence_generation = CandidateStructuredGenerationPlan.model_validate(
        _runtime_call_projection(
            remediation_bundle.adherence_call,
            tools[1],
            workflow=PROSE_REMEDIATION_WORKFLOW,
            step=OUTLINE_ADHERENCE_STEP,
        )
    )
    deadline_budget = _prose_remediation_deadline_budget(
        planner_generation,
        rewrite_generation,
        adherence_generation,
    )
    adherence_projection = CandidateStructuredGenerationPlan.model_validate(
        _structured_plan_projection(
            adherence_plan,
            workflow=PROSE_REMEDIATION_WORKFLOW,
            step=OUTLINE_ADHERENCE_STEP,
            generation_params=generation_params,
        )
    )
    state_projection = CandidateStructuredGenerationPlan.model_validate(
        _structured_plan_projection(
            state_plan,
            workflow=STATE_WORKFLOW,
            step=STATE_STEP,
            generation_params=generation_params,
        )
    )

    planner_external = tuple(planner.external_data_categories)
    tool_external = tuple(
        item
        for descriptor in tools
        for item in descriptor.external_data_categories
    )
    prose_authorization = ProseRemediationAuthorization.model_validate(
        {
            "scope_kind": PROSE_REMEDIATION_SCOPE_KIND,
            "registry_revision": str(
                remediation_bundle.tools.registry_revision
            ),
            "allowed_tools": [
                reference.model_dump(mode="json") for reference in references
            ],
            "allowed_effects": _ordered_union(
                tuple(descriptor.effect_class for descriptor in tools)
            ),
            "allowed_change_classes": _ordered_union(
                *tuple(descriptor.change_classes for descriptor in tools)
            ),
            "allowed_external_data_categories": _ordered_union(
                planner_external,
                tool_external,
            ),
            "deadline_budget": deadline_budget,
            "limits": {
                "max_steps": PROSE_REMEDIATION_MAX_STEPS,
                "max_planner_calls": PROSE_REMEDIATION_MAX_PLANNER_CALLS,
                "max_tool_calls": PROSE_REMEDIATION_MAX_TOOL_CALLS,
                "max_paid_attempts": prose_paid_attempts,
                "token_budget": prose_token_bound,
                "deadline_seconds": deadline_budget.deadline_seconds,
                "max_predispatch_retries": (
                    PROSE_REMEDIATION_MAX_PREDISPATCH_RETRIES
                ),
                "max_planner_repairs": PROSE_REMEDIATION_MAX_PLANNER_REPAIRS,
                "max_tool_retries": PROSE_REMEDIATION_MAX_TOOL_RETRIES,
            },
            "planner": _planner_projection(planner),
            "tools": [_tool_projection(descriptor) for descriptor in tools],
            "planner_generation": planner_generation,
            "rewrite_generation": rewrite_generation,
            "adherence_generation": adherence_generation,
        }
    )
    # A prose repair is not trusted on the Agent's own review alone. The
    # deterministic candidate pipeline always performs one fresh, exact-source
    # adherence review before it may advance to state extraction.
    component_limits = _candidate_component_limits(
        cycles,
        prose_authorization,
        adherence_projection,
        state_projection,
    )
    cycle_bounds, chapter_bounds = _candidate_authorized_bounds(
        prose_authorization,
        adherence_projection,
        state_projection,
        component_limits,
    )
    chapter_provider_bounds = {
        bound.provider_alias: bound
        for bound in chapter_bounds.provider_bounds
    }
    return CandidateRepairAuthorization.model_validate({
        **base,
        "maximum_repair_events_per_chapter": (
            _candidate_repair_event_count(component_limits)
        ),
        "component_limits": component_limits,
        "maximum_provider_attempts_per_cycle": cycle_bounds.paid_attempts,
        "maximum_provider_attempts_per_chapter": (
            chapter_bounds.paid_attempts
        ),
        "maximum_provider_attempts_total": (
            len(eligible_ids) * chapter_bounds.paid_attempts
        ),
        "maximum_tokens_per_cycle": cycle_bounds.tokens,
        "maximum_tokens_per_chapter": chapter_bounds.tokens,
        "maximum_tokens_total": len(eligible_ids) * chapter_bounds.tokens,
        "provider_bounds": [
            {
                "provider_alias": bound.provider_alias,
                "maximum_paid_attempts_per_cycle": bound.paid_attempts,
                "maximum_paid_attempts_total": (
                    len(eligible_ids)
                    * chapter_provider_bounds[
                        bound.provider_alias
                    ].paid_attempts
                ),
                "maximum_tokens_per_cycle": bound.tokens,
                "maximum_tokens_total": (
                    len(eligible_ids)
                    * chapter_provider_bounds[bound.provider_alias].tokens
                ),
            }
            for bound in cycle_bounds.provider_bounds
        ],
        "prose_remediation": prose_authorization,
        "adherence_review": adherence_projection,
        "state_repair": state_projection,
    }).model_dump(mode="json")


def authorized_candidate_repair_attempt_slots(
    readiness: Mapping[str, Any],
    *,
    chapter_id: str,
    generation_params: Mapping[str, Any] | None = None,
) -> int:
    """Return one chapter's frozen repair slots, rejecting worklist drift."""
    normalized_chapter_id = str(chapter_id or "")
    if not normalized_chapter_id:
        raise ValueError("candidate repair chapter id is required")
    readiness_version = readiness.get("version")
    if (
        isinstance(readiness_version, bool)
        or not isinstance(readiness_version, int)
        or readiness_version != 2
    ):
        raise ValueError("candidate repair readiness version is invalid")
    planning = readiness.get("planning")
    if not isinstance(planning, Mapping):
        raise ValueError("generation readiness planning is missing")
    pipeline_revision = planning.get(
        "chapter_candidate_pipeline_revision"
    )
    if (
        isinstance(pipeline_revision, bool)
        or not isinstance(pipeline_revision, int)
        or pipeline_revision != CANDIDATE_PIPELINE_REVISION
    ):
        raise ValueError("candidate pipeline authorization revision is invalid")
    raw_authorization = planning.get(
        "chapter_candidate_repair_authorization"
    )
    if not isinstance(raw_authorization, Mapping):
        raise ValueError("generation readiness has no candidate repair authority")
    authorization = parse_candidate_repair_authorization(raw_authorization)
    from backend.services.generation.chapter_finalization import (
        build_chapter_finalization_authorization,
    )
    from backend.services.generation.chapter_generation_application import (
        OUTLINE_ADHERENCE_STEP,
        PROSE_REMEDIATION_WORKFLOW,
        STATE_STEP,
        STATE_WORKFLOW,
    )

    expected_finalization = build_chapter_finalization_authorization(
        authorization_revision=authorization.authorization_revision,
        max_repair_cycles=(
            authorization.maximum_repair_events_per_chapter
        ),
    )
    raw_finalization = planning.get("chapter_finalization_authorization")
    finalization_matches = False
    if isinstance(raw_finalization, Mapping):
        try:
            finalization_matches = _canonical_json_projection(
                dict(raw_finalization),
                field="chapter finalization authorization",
            ) == _canonical_json_projection(
                expected_finalization,
                field="expected chapter finalization authorization",
            )
        except ValueError:
            pass
    if not finalization_matches:
        raise ValueError("candidate repair and finalization authority diverged")
    if authorization.adherence_review is not None and (
        authorization.adherence_review.workflow != PROSE_REMEDIATION_WORKFLOW
        or authorization.adherence_review.step != OUTLINE_ADHERENCE_STEP
    ):
        raise ValueError("candidate adherence workflow identity changed")
    if authorization.state_repair is not None and (
        authorization.state_repair.workflow != STATE_WORKFLOW
        or authorization.state_repair.step != STATE_STEP
    ):
        raise ValueError("candidate state workflow identity changed")
    generation_digest = _mapping_digest(generation_params)
    for plan in (
        authorization.adherence_review,
        authorization.state_repair,
    ):
        if plan is not None and plan.generation_params_digest != generation_digest:
            raise ValueError("candidate repair generation parameters changed")

    work = readiness.get("work")
    raw_snapshots = work.get("chapters") if isinstance(work, Mapping) else None
    if not isinstance(raw_snapshots, list):
        raise ValueError("generation readiness worklist is invalid")
    snapshots: dict[str, Mapping[str, Any]] = {}
    eligible_ids: list[str] = []
    for raw_snapshot in raw_snapshots:
        if not isinstance(raw_snapshot, Mapping):
            raise ValueError("generation readiness chapter snapshot is invalid")
        snapshot_id = str(raw_snapshot.get("chapter_id") or "")
        if not snapshot_id or snapshot_id in snapshots:
            raise ValueError("generation readiness chapter identity is invalid")
        snapshots[snapshot_id] = raw_snapshot
        if raw_snapshot.get("has_content") is False:
            eligible_ids.append(snapshot_id)
        elif raw_snapshot.get("has_content") is not True:
            raise ValueError("generation readiness prose state is invalid")
    if normalized_chapter_id not in snapshots:
        raise ValueError("chapter is outside the frozen generation worklist")

    frozen_count = authorization.eligible_chapter_count
    if (
        frozen_count != len(eligible_ids)
        or authorization.eligible_chapter_ids_digest
        != _chapter_ids_digest(tuple(eligible_ids))
    ):
        raise ValueError("candidate repair worklist digest changed")
    quality_authorization = (
        validate_readiness_narrative_quality_signal_authorization(readiness)
    )
    if authorization.narrative_quality_signal_authorization_digest != (
        narrative_quality_signal_authorization_digest(quality_authorization)
    ):
        raise ValueError("candidate repair quality-signal authority changed")
    if snapshots[normalized_chapter_id].get("has_content") is True:
        return 0
    return authorization.maximum_provider_attempts_per_chapter


def validate_candidate_repair_execution_authorization(
    readiness: Mapping[str, Any],
    *,
    chapter_id: str,
    remediation_bundle: Any,
    adherence_plan: GenerationPlan,
    state_plan: GenerationPlan,
    generation_params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Match the live production adapters to one frozen chapter authority.

    Slot accounting alone proves only that a chapter is in the signed
    worklist. Execution also has to prove that the Planner, Tool Registry and
    the two deterministic structured workflows are still the exact adapters
    whose identities and worst-case budgets were included in that digest.
    """
    slots = authorized_candidate_repair_attempt_slots(
        readiness,
        chapter_id=chapter_id,
        generation_params=generation_params,
    )
    if slots <= 0:
        raise ValueError("chapter has no authorized candidate repair slots")

    planning = readiness.get("planning")
    work = readiness.get("work")
    if not isinstance(planning, Mapping) or not isinstance(work, Mapping):
        raise ValueError("candidate repair readiness projection is invalid")
    raw_authorization = planning.get(
        "chapter_candidate_repair_authorization"
    )
    raw_snapshots = work.get("chapters")
    if (
        not isinstance(raw_authorization, Mapping)
        or not isinstance(raw_snapshots, list)
    ):
        raise ValueError("candidate repair readiness projection is invalid")
    authorization = parse_candidate_repair_authorization(raw_authorization)
    chapters = [
        {
            "_id": str(snapshot["chapter_id"]),
            "content": (
                "already formal"
                if snapshot.get("has_content") is True
                else ""
            ),
            "outline": (
                {
                    "scenes": [
                        {}
                        for _index in range(
                            int(snapshot.get("scene_count") or 0)
                        )
                    ]
                }
                if snapshot.get("has_outline") is True
                else {}
            ),
        }
        for snapshot in raw_snapshots
    ]
    expected = build_chapter_candidate_repair_authorization(
        chapters=chapters,
        authorization_revision=authorization.authorization_revision,
        max_repair_cycles=(
            authorization.max_repair_cycles_per_chapter
        ),
        generation_params=generation_params,
        remediation_bundle=remediation_bundle,
        adherence_plan=adherence_plan,
        state_plan=state_plan,
    )
    if _canonical_json_projection(
        dict(raw_authorization),
        field="frozen candidate repair authorization",
    ) != _canonical_json_projection(
        expected,
        field="current candidate repair runtime snapshot",
    ):
        raise ValueError("candidate repair runtime snapshot drifted")
    return expected
