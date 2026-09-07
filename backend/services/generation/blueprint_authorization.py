"""Freeze one blueprint plan without invoking a Provider or creating a novel."""
from __future__ import annotations

from dataclasses import asdict
from copy import deepcopy
from typing import Any

from backend.db.repositories.blueprint_run_repository import content_digest
from backend.services.generation.blueprint_workflow import (
    AI_CREATE_STEPS, BLUEPRINT_WORKFLOW_PROTOCOL, WORKFLOW_NAME,
    BlueprintGenerationRequest, workflow_params,
)
from backend.services.llm.generation_params import build_gen_kwargs
from backend.services.llm.generation_runtime import (
    GenerationPlan, StructuredOutputMode, WorkflowStepTarget,
)


def plan_record(plan: GenerationPlan) -> dict:
    if not isinstance(plan.target, WorkflowStepTarget):
        raise ValueError("Blueprints require a registered workflow target")
    record = asdict(plan)
    record["mode"] = plan.mode.value
    return record


def plan_from_record(record: dict) -> GenerationPlan:
    fields = deepcopy(record)
    fields["target"] = WorkflowStepTarget(**fields["target"])
    fields["mode"] = StructuredOutputMode(fields["mode"])
    return GenerationPlan(**fields)


def request_record(request: BlueprintGenerationRequest) -> dict:
    return BlueprintGenerationRequest.model_validate({
        name: getattr(request, name) for name in BlueprintGenerationRequest.model_fields
    }).model_dump(mode="json")


def prepare_blueprint_authorization(
    request: BlueprintGenerationRequest, *, run_id: str, owner_id: str,
    runtime: Any, all_prompts: dict, candidates: dict, source_summary: dict | None,
) -> tuple[dict, dict]:
    """Return an immutable authorization and its content-free cost summary."""
    prompts = deepcopy(all_prompts[WORKFLOW_NAME])
    plans: dict[str, dict] = {}
    providers = []
    maximum_tokens = 0
    token_bound_known = True
    max_attempts_by_step = {}
    providers_by_step = {}
    for step in AI_CREATE_STEPS:
        if step.key in candidates:
            continue
        for suffix in ("prompt_base", "prompt_with_schema_suffix", "prompt_without_schema_suffix"):
            if not isinstance(prompts.get(f"{step.resolved_config_key}_{suffix}"), str):
                raise ValueError("Incomplete blueprint prompt definition")
        plan = runtime.plan_structured(WorkflowStepTarget(WORKFLOW_NAME, step.resolved_config_key))
        plans[step.key] = plan_record(plan)
        attempts = int(plan.max_semantic_attempts)
        if attempts < 1 or attempts > 16:
            raise ValueError("Unbounded blueprint attempt plan")
        output_bound = request.max_tokens or plan.max_output_tokens
        context_bound = plan.max_context_tokens
        if output_bound is None or context_bound is None or output_bound <= 0 or context_bound <= 0:
            token_bound_known = False
        else:
            maximum_tokens += attempts * (output_bound + context_bound)
        max_attempts_by_step[step.key] = attempts
        providers_by_step[step.key] = sorted({plan.provider_alias, *([plan.reviewer_alias] if plan.reviewer_alias else [])})
        providers.append({"step": step.key, "provider_alias": plan.provider_alias,
            "provider_model": plan.provider_model, "maximum_attempts": attempts})

    automatic_budget = request.token_budget is None and token_bound_known and maximum_tokens > 0
    token_budget = maximum_tokens if request.token_budget is None and token_bound_known else request.token_budget
    maximum_attempts = sum(max_attempts_by_step.values())
    uncertain_source = bool(source_summary and source_summary.get("has_uncertain"))
    issues = []
    if not token_bound_known:
        issues.append({"code": "blueprint_token_bound_unproven", "level": "blocked"})
    if automatic_budget:
        issues.append({"code": "automatic_token_budget_requires_confirmation", "level": "warning_requires_ack"})
    if uncertain_source:
        issues.append({"code": "blueprint_uncertain_source_requires_confirmation", "level": "warning_requires_ack"})
    authorization = {
        "schema_version": "blueprint_authorization.v1", "workflow_protocol": BLUEPRINT_WORKFLOW_PROTOCOL,
        "owner_id": owner_id, "draft_id": request.draft_id, "run_id": run_id,
        "request": request_record(request), "author_brief": workflow_params(request)["author_brief"],
        "workflow_name": WORKFLOW_NAME, "strategy": request.strategy,
        "step_order": [step.key for step in AI_CREATE_STEPS],
        "generation_params": build_gen_kwargs(request), "transport_retries": 0,
        "plans": plans, "prompts": prompts, "prompt_revision": content_digest(prompts),
        "limits": {"maximum_provider_attempts": maximum_attempts,
            "max_attempts_by_step": max_attempts_by_step, "providers_by_step": providers_by_step,
            "token_budget": token_budget},
        "prefix_sources": {key: {"digest": value["digest"], "source_run_id": value["source_run_id"],
            "authorization_digest": value["authorization_digest"]} for key, value in candidates.items()},
        "source_summary": source_summary,
    }
    report = {
        "version": 3, "run_id": run_id, "draft_id": request.draft_id,
        "status": "blocked" if not token_bound_known else "warning_requires_ack" if issues else "ready",
        "digest": content_digest(authorization),
        "author_brief_revision": authorization["author_brief"]["revision"],
        "prompt_revision": authorization["prompt_revision"], "strategy": request.strategy,
        "token_budget": token_budget, "uses_system_token_budget": automatic_budget,
        "maximum_provider_attempts": maximum_attempts, "maximum_tokens_total": maximum_tokens,
        "token_bound_known": token_bound_known,
        "budget_covers_conservative_maximum": bool(token_bound_known and token_budget is not None and token_budget >= maximum_tokens),
        "providers": providers, "issues": issues, "uncertain_source": uncertain_source,
        "reused_steps": list(candidates), "source_summary": source_summary,
    }
    return authorization, report
