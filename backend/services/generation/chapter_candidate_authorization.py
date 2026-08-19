"""Read-only authorization planning for the candidate-first chapter tail."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from backend.services.agent_runtime.contracts import (
    PlannerDescriptor,
    RuntimeToolDescriptor,
    runtime_tool_descriptor_snapshot,
)
from backend.services.generation.chapter_finalization import (
    MAX_FINALIZATION_REPAIR_CYCLES,
)
from backend.services.llm.generation_runtime import (
    GenerationPlan,
    WorkflowStepTarget,
)


CANDIDATE_REPAIR_AUTHORIZATION_SCHEMA = (
    "chapter_candidate_repair_authorization.v1"
)
PROSE_REMEDIATION_SCOPE_KIND = "chapter_prose_candidate"
PROSE_REMEDIATION_MAX_STEPS = 3
PROSE_REMEDIATION_MAX_PLANNER_CALLS = 3
PROSE_REMEDIATION_MAX_TOOL_CALLS = 2
PROSE_REMEDIATION_DEADLINE_SECONDS = 300
PROSE_REMEDIATION_MAX_PREDISPATCH_RETRIES = 2
PROSE_REMEDIATION_MAX_PLANNER_REPAIRS = 1
PROSE_REMEDIATION_MAX_TOOL_RETRIES = 1


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
    paid_attempts = _strict_positive_int(
        plan.max_semantic_attempts,
        field="candidate repair paid-attempt bound",
    )
    raw_max_tokens = dict(generation_params or {}).get("max_tokens")
    output_tokens = _strict_positive_int(
        raw_max_tokens if raw_max_tokens is not None else plan.max_output_tokens,
        field="candidate repair output-token bound",
    )
    timeout_seconds = plan.timeout_seconds
    if timeout_seconds is not None:
        timeout_seconds = _strict_positive_int(
            timeout_seconds,
            field="candidate repair timeout",
        )
    context_tokens = plan.max_context_tokens
    if context_tokens is not None:
        context_tokens = _strict_positive_int(
            context_tokens,
            field="candidate repair context-token bound",
        )
    return {
        "schema_version": "candidate_structured_generation_plan.v1",
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
        "max_paid_attempts_per_call": paid_attempts,
        "max_output_tokens_per_attempt": output_tokens,
        "max_context_tokens": context_tokens,
    }


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
        return {
            **base,
            "maximum_provider_attempts_per_cycle": 0,
            "maximum_provider_attempts_total": 0,
            "prose_remediation": None,
            "adherence_review": None,
            "state_repair": None,
        }

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
        REMEDIATION_SCOPE_KIND,
        REWRITE_TOOL,
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

    maximum_tool_paid = max(
        descriptor.max_paid_attempts_per_call for descriptor in tools
    )
    maximum_tool_tokens = max(
        descriptor.max_tokens_per_call for descriptor in tools
    )
    prose_paid_attempts = (
        PROSE_REMEDIATION_MAX_PLANNER_CALLS
        * planner.max_paid_attempts_per_call
        + PROSE_REMEDIATION_MAX_TOOL_CALLS * maximum_tool_paid
    )
    prose_token_bound = (
        PROSE_REMEDIATION_MAX_PLANNER_CALLS * planner.max_tokens_per_call
        + PROSE_REMEDIATION_MAX_TOOL_CALLS * maximum_tool_tokens
    )
    from backend.services.generation.chapter_generation_application import (
        OUTLINE_ADHERENCE_STEP,
        PROSE_REMEDIATION_WORKFLOW,
        STATE_STEP,
        STATE_WORKFLOW,
    )
    adherence_projection = _structured_plan_projection(
        adherence_plan,
        workflow=PROSE_REMEDIATION_WORKFLOW,
        step=OUTLINE_ADHERENCE_STEP,
        generation_params=generation_params,
    )
    state_projection = _structured_plan_projection(
        state_plan,
        workflow=STATE_WORKFLOW,
        step=STATE_STEP,
        generation_params=generation_params,
    )
    # A prose repair is not trusted on the Agent's own review alone. The
    # deterministic candidate pipeline always performs one fresh, exact-source
    # adherence review before it may advance to state extraction.
    maximum_per_cycle = max(
        prose_paid_attempts
        + int(adherence_projection["max_paid_attempts_per_call"]),
        int(state_projection["max_paid_attempts_per_call"]),
    )
    maximum_total = len(eligible_ids) * cycles * maximum_per_cycle

    planner_external = tuple(planner.external_data_categories)
    tool_external = tuple(
        item
        for descriptor in tools
        for item in descriptor.external_data_categories
    )
    return {
        **base,
        "maximum_provider_attempts_per_cycle": maximum_per_cycle,
        "maximum_provider_attempts_total": maximum_total,
        "prose_remediation": {
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
        },
        "adherence_review": adherence_projection,
        "state_repair": state_projection,
    }


def authorized_candidate_repair_attempt_slots(
    readiness: Mapping[str, Any],
    *,
    chapter_id: str,
) -> int:
    """Return one chapter's frozen repair slots, rejecting worklist drift."""
    normalized_chapter_id = str(chapter_id or "")
    if not normalized_chapter_id:
        raise ValueError("candidate repair chapter id is required")
    planning = readiness.get("planning")
    if not isinstance(planning, Mapping):
        raise ValueError("generation readiness planning is missing")
    raw_authorization = planning.get(
        "chapter_candidate_repair_authorization"
    )
    if not isinstance(raw_authorization, Mapping):
        raise ValueError("generation readiness has no candidate repair authority")
    authorization = dict(raw_authorization)
    if (
        authorization.get("schema_version")
        != CANDIDATE_REPAIR_AUTHORIZATION_SCHEMA
    ):
        raise ValueError("candidate repair authorization schema is invalid")

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

    frozen_count = _strict_non_negative_int(
        authorization.get("eligible_chapter_count"),
        field="candidate repair eligible chapter count",
    )
    if (
        frozen_count != len(eligible_ids)
        or str(authorization.get("eligible_chapter_ids_digest") or "")
        != _chapter_ids_digest(tuple(eligible_ids))
    ):
        raise ValueError("candidate repair worklist digest changed")
    cycles = _strict_non_negative_int(
        authorization.get("max_repair_cycles_per_chapter"),
        field="candidate repair cycle bound",
    )
    per_cycle = _strict_non_negative_int(
        authorization.get("maximum_provider_attempts_per_cycle"),
        field="candidate repair per-cycle attempt bound",
    )
    total = _strict_non_negative_int(
        authorization.get("maximum_provider_attempts_total"),
        field="candidate repair total attempt bound",
    )
    if total != frozen_count * cycles * per_cycle:
        raise ValueError("candidate repair total attempt bound changed")
    if snapshots[normalized_chapter_id].get("has_content") is True:
        return 0
    return cycles * per_cycle
