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
    RuntimeProposalKind,
    RuntimeToolDescriptor,
    RuntimeToolReference,
    runtime_tool_descriptor_snapshot,
)
from backend.services.generation.chapter_finalization import (
    MAX_FINALIZATION_REPAIR_CYCLES,
)
from backend.services.generation.provider_budget import (
    ProviderBudgetBound,
    max_provider_bounds,
    merge_provider_bounds,
    scale_provider_bounds,
    structured_call_budget,
    structured_provider_bounds,
)
from backend.services.llm.generation_runtime import (
    GenerationPlan,
    WorkflowStepTarget,
)


CANDIDATE_REPAIR_AUTHORIZATION_SCHEMA = (
    "chapter_candidate_repair_authorization.v2"
)
CANDIDATE_PIPELINE_REVISION = 2
CANDIDATE_STRUCTURED_PLAN_SCHEMA = "candidate_structured_generation_plan.v2"
PROSE_REMEDIATION_SCOPE_KIND = "chapter_prose_candidate"
PROSE_REMEDIATION_MAX_STEPS = 3
PROSE_REMEDIATION_MAX_PLANNER_CALLS = 3
PROSE_REMEDIATION_MAX_TOOL_CALLS = 2
PROSE_REMEDIATION_DEADLINE_SECONDS = 300
PROSE_REMEDIATION_MAX_PREDISPATCH_RETRIES = 2
PROSE_REMEDIATION_MAX_PLANNER_REPAIRS = 1
PROSE_REMEDIATION_MAX_TOOL_RETRIES = 1

_PositiveInt = Annotated[StrictInt, Field(ge=1)]
_NonNegativeInt = Annotated[StrictInt, Field(ge=0)]
_Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


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


class CandidateStructuredGenerationPlan(_ClosedAuthorizationModel):
    schema_version: Literal["candidate_structured_generation_plan.v2"]
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
        bounds = _prose_remediation_bounds(self)
        expected_limits = AgentRuntimeLimits(
            max_steps=PROSE_REMEDIATION_MAX_STEPS,
            max_planner_calls=PROSE_REMEDIATION_MAX_PLANNER_CALLS,
            max_tool_calls=PROSE_REMEDIATION_MAX_TOOL_CALLS,
            max_paid_attempts=bounds.paid_attempts,
            token_budget=bounds.tokens,
            deadline_seconds=PROSE_REMEDIATION_DEADLINE_SECONDS,
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
    schema_version: Literal["chapter_candidate_repair_authorization.v2"]
    authorization_revision: _PositiveInt
    eligible_chapter_count: _NonNegativeInt
    eligible_chapter_ids_digest: _Sha256
    max_repair_cycles_per_chapter: Annotated[
        StrictInt,
        Field(ge=0, le=MAX_FINALIZATION_REPAIR_CYCLES),
    ]
    maximum_provider_attempts_per_cycle: _NonNegativeInt
    maximum_provider_attempts_total: _NonNegativeInt
    maximum_tokens_per_cycle: _NonNegativeInt
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
            if any(item is not None for item in adapters) or any(
                (
                    self.maximum_provider_attempts_per_cycle,
                    self.maximum_provider_attempts_total,
                    self.maximum_tokens_per_cycle,
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
        cycle_bounds = _candidate_cycle_bounds(
            self.prose_remediation,
            self.adherence_review,
            self.state_repair,
        )
        multiplier = (
            self.eligible_chapter_count * self.max_repair_cycles_per_chapter
        )
        if (
            self.maximum_provider_attempts_per_cycle
            != cycle_bounds.paid_attempts
            or self.maximum_provider_attempts_total
            != multiplier * cycle_bounds.paid_attempts
            or self.maximum_tokens_per_cycle != cycle_bounds.tokens
            or self.maximum_tokens_total != multiplier * cycle_bounds.tokens
        ):
            raise ValueError("candidate repair aggregate bounds changed")
        expected_provider_bounds = tuple(
            CandidateProviderBudgetBound(
                provider_alias=bound.provider_alias,
                maximum_paid_attempts_per_cycle=bound.paid_attempts,
                maximum_paid_attempts_total=bound.paid_attempts * multiplier,
                maximum_tokens_per_cycle=bound.tokens,
                maximum_tokens_total=bound.tokens * multiplier,
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


def _candidate_cycle_bounds(
    prose_remediation: ProseRemediationAuthorization,
    adherence_review: CandidateStructuredGenerationPlan,
    state_repair: CandidateStructuredGenerationPlan,
) -> _BudgetBounds:
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
        "workflow": workflow,
        "step": step,
        "provider_alias": provider_alias,
        "provider_model": provider_model,
        "structured_output_mode": mode,
        "reviewer_alias": (
            str(plan.reviewer_alias) if plan.reviewer_alias else None
        ),
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
    if cycles > MAX_FINALIZATION_REPAIR_CYCLES:
        raise ValueError("candidate repair cycle bound exceeds V1")
    eligible_ids = _eligible_chapter_ids(chapters)
    base = {
        "schema_version": CANDIDATE_REPAIR_AUTHORIZATION_SCHEMA,
        "authorization_revision": revision,
        "eligible_chapter_count": len(eligible_ids),
        "eligible_chapter_ids_digest": _chapter_ids_digest(eligible_ids),
        "max_repair_cycles_per_chapter": cycles,
    }
    if not eligible_ids or cycles == 0:
        return CandidateRepairAuthorization.model_validate({
            **base,
            "maximum_provider_attempts_per_cycle": 0,
            "maximum_provider_attempts_total": 0,
            "maximum_tokens_per_cycle": 0,
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
    planner_generation = _runtime_call_projection(
        remediation_bundle.planner_call,
        planner,
        workflow=PROSE_REMEDIATION_WORKFLOW,
        step=REMEDIATION_PLANNER_STEP,
    )
    rewrite_generation = _runtime_call_projection(
        remediation_bundle.rewrite_call,
        tools[0],
        workflow=PROSE_REMEDIATION_WORKFLOW,
        step=PROSE_CANDIDATE_REWRITE_STEP,
    )
    adherence_generation = _runtime_call_projection(
        remediation_bundle.adherence_call,
        tools[1],
        workflow=PROSE_REMEDIATION_WORKFLOW,
        step=OUTLINE_ADHERENCE_STEP,
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
            "limits": {
                "max_steps": PROSE_REMEDIATION_MAX_STEPS,
                "max_planner_calls": PROSE_REMEDIATION_MAX_PLANNER_CALLS,
                "max_tool_calls": PROSE_REMEDIATION_MAX_TOOL_CALLS,
                "max_paid_attempts": prose_paid_attempts,
                "token_budget": prose_token_bound,
                "deadline_seconds": PROSE_REMEDIATION_DEADLINE_SECONDS,
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
    cycle_bounds = _candidate_cycle_bounds(
        prose_authorization,
        adherence_projection,
        state_projection,
    )
    multiplier = len(eligible_ids) * cycles
    return CandidateRepairAuthorization.model_validate({
        **base,
        "maximum_provider_attempts_per_cycle": cycle_bounds.paid_attempts,
        "maximum_provider_attempts_total": (
            multiplier * cycle_bounds.paid_attempts
        ),
        "maximum_tokens_per_cycle": cycle_bounds.tokens,
        "maximum_tokens_total": multiplier * cycle_bounds.tokens,
        "provider_bounds": [
            {
                "provider_alias": bound.provider_alias,
                "maximum_paid_attempts_per_cycle": bound.paid_attempts,
                "maximum_paid_attempts_total": (
                    multiplier * bound.paid_attempts
                ),
                "maximum_tokens_per_cycle": bound.tokens,
                "maximum_tokens_total": multiplier * bound.tokens,
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
        max_repair_cycles=authorization.max_repair_cycles_per_chapter,
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
    cycles = authorization.max_repair_cycles_per_chapter
    per_cycle = authorization.maximum_provider_attempts_per_cycle
    if snapshots[normalized_chapter_id].get("has_content") is True:
        return 0
    return cycles * per_cycle
