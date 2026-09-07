"""Pure authorization envelope for the strict three-chapter successor gate.

The successor production Job starts from accepted formal outlines.  Issue #20,
however, must authorize the preceding outline calls and the derived root Job as
one bounded operation.  This module closes that gap without opening MongoDB,
constructing a Provider adapter, or retaining prompt/user material.

The envelope is a template, not execution authority.  A later execution seam
must re-create the live outline/root projections, match this exact digest and
collect the three external authorizations before the first side effect.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
import json
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.evaluation.batch_job_acceptance_sample import (
    BatchJobAcceptanceSample,
)
from backend.evaluation.required_book_successor_judge_probe import (
    RequiredJudgeCapabilityProbeReceipt,
    validate_required_judge_capability_probe_receipt,
)
from backend.evaluation.required_book_successor_judge_probe_store import (
    RequiredJudgeProbeTerminalEvidence,
    validate_required_judge_probe_terminal_evidence,
)
from backend.llm.prompts.prompt_selector import (
    CHAPTER_OUTLINE_PROMPT_NAME,
    load_prompt_config,
)
from backend.llm.schemas.novel_pydantic import ChapterOutlineResultSchema
from backend.scene_contract_versions import (
    MAX_V2_OUTLINE_RESPONSE_UTF8_BYTES,
    OUTLINE_RESPONSE_BYTE_BUDGET_REASON_CODE,
)
from backend.services.generation.required_adherence_capacity import (
    RequiredAdherenceCapacity,
)
from backend.services.generation.prose_token_bounds import (
    conservative_prompt_input_bound,
)
from backend.services.generation.required_book_successor import (
    REQUIRED_BOOK_SUCCESSOR_ACKNOWLEDGEMENT,
    REQUIRED_BOOK_SUCCESSOR_RECOVERY_CHECKPOINT_BEFORE_FIRST_CHILD,
    RequiredBookSuccessorAuthorization,
    RequiredBookSuccessorProviderUsageBound,
    prepare_required_book_successor_readiness,
    required_book_successor_digest,
    required_book_successor_provider_usage_bounds,
    validate_required_book_successor_readiness,
)
from backend.services.generation.required_book_successor_planning import (
    RequiredBookSuccessorPlanBundle,
    build_required_book_successor_plan_bundle,
)
from backend.services.generation.required_chapter_review_job import (
    REQUIRED_REVIEW_ACKNOWLEDGEMENT,
    RequiredGenerationPlanSnapshot,
    prepare_required_chapter_review_readiness,
)
from backend.services.generation.required_initial_prose_contracts import (
    RequiredInitialProseAuthorization,
)
from backend.services.generation.required_prose_rewrite_contracts import (
    RequiredRewriteAuthorization,
)
from backend.services.generation.readiness import generation_readiness_module
from backend.services.llm.agent_orchestrator import apply_agent_profile
from backend.services.llm.generation_runtime import (
    GenerationPlan,
    GenerationRuntime,
    STRUCTURED_BYTE_BUDGET_REGENERATION_PHASE,
    STRUCTURED_BYTE_BUDGET_REGENERATION_PROMPT_REVISION,
    StructuredOutputMode,
    WorkflowStepTarget,
    effective_provider_system_prompt,
    maximum_structured_validation_issues_projection,
    render_structured_byte_budget_regeneration_prompt,
    render_structured_repair_prompt,
)
from backend.services.llm.outline_generation import (
    CHAPTER_OUTLINE_MAX_OUTPUT_TOKENS,
)
from backend.services.novel.style_controls import render_style_controls


SUCCESSOR_ACCEPTANCE_SAMPLE_ID = "successor-representative-3000-v1"
SUCCESSOR_ACCEPTANCE_PROTOCOL_REVISION = (
    "required-book-successor-acceptance-r3"
)
OUTLINE_WORKFLOW = "create_chapter_outline_by_ai"
OUTLINE_STEP = "chapter_outline"
CHAPTER_COUNT = 3
TARGET_WORD_COUNT = 3_000
OUTLINE_TIMEOUT_SECONDS = 300
MAXIMUM_REAL_RUNS = 2
READONLY_REDACTED_CONTEXT_BYTES = 4_096
READONLY_OUTLINE_CONTEXT_BYTES = (
    READONLY_REDACTED_CONTEXT_BYTES
    + (CHAPTER_COUNT - 1) * MAX_V2_OUTLINE_RESPONSE_UTF8_BYTES * 2
    + MAX_V2_OUTLINE_RESPONSE_UTF8_BYTES
)
_MAX = 2**63 - 1
_SHA256 = r"^[0-9a-f]{64}$"
_PRICE_QUANTUM = Decimal("0.000001")

DATABASE_AUTHORIZATION_CODE = (
    "successor_acceptance_isolated_database_write_and_hard_delete"
)
COST_AUTHORIZATION_CODE = "successor_acceptance_cost_upper_bound"
DISCLOSURE_AUTHORIZATION_CODE = (
    "successor_acceptance_three_chapter_external_disclosure"
)
REQUIRED_AUTHORIZATION_CODES = (
    DATABASE_AUTHORIZATION_CODE,
    COST_AUTHORIZATION_CODE,
    DISCLOSURE_AUTHORIZATION_CODE,
)

# These keys are structurally absent from every model below.  The recursive
# check protects the surrounding dict report as it evolves.
FORBIDDEN_REPORT_KEYS = frozenset({
    "api_key",
    "content",
    "context",
    "messages",
    "prompt",
    "prose",
    "raw_output",
    "response_body",
    "system_prompt",
    "text",
})


class _Closed(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


class SuccessorSceneWordBudget(_Closed):
    minimum: Literal[1200] = 1200
    target: Literal[1500] = 1500
    maximum: Literal[1800] = 1800


class SuccessorAcceptanceSample(_Closed):
    schema_version: Literal["successor_acceptance_sample.v1"] = (
        "successor_acceptance_sample.v1"
    )
    sample_id: Literal["successor-representative-3000-v1"] = (
        SUCCESSOR_ACCEPTANCE_SAMPLE_ID
    )
    prior_sample_id: Literal["representative-3000-v1"] = (
        "representative-3000-v1"
    )
    prior_sample_runs_used: Literal[2] = 2
    prior_sample_maximum_runs: Literal[2] = 2
    prior_run_claims_inherited: Literal[False] = False
    chapter_count: Literal[3] = CHAPTER_COUNT
    target_word_count: Literal[3000] = TARGET_WORD_COUNT
    scene_word_budgets: tuple[
        SuccessorSceneWordBudget,
        SuccessorSceneWordBudget,
    ]
    maximum_real_runs: Literal[2] = MAXIMUM_REAL_RUNS
    real_runs_used_at_readiness: Literal[0] = 0
    real_runs_remaining_at_readiness: Literal[2] = MAXIMUM_REAL_RUNS
    fixture_blueprint_digest: str = Field(pattern=_SHA256)
    root_prompt_protocol_digest: str = Field(pattern=_SHA256)


class SuccessorProviderPricing(_Closed):
    """User-visible cache-miss prices for one exact Provider/model pair."""

    schema_version: Literal["successor_provider_pricing.v1"] = (
        "successor_provider_pricing.v1"
    )
    provider_alias: str = Field(min_length=1, max_length=160)
    provider_model: str = Field(min_length=1, max_length=240)
    currency: str = Field(min_length=1, max_length=16)
    input_cache_miss_per_million: Decimal = Field(ge=0)
    output_per_million: Decimal = Field(ge=0)
    basis: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def normalize_identity(self) -> "SuccessorProviderPricing":
        if (
            self.provider_alias != self.provider_alias.strip()
            or self.provider_model != self.provider_model.strip()
            or self.currency != self.currency.strip().upper()
            or self.basis != self.basis.strip()
        ):
            raise ValueError("successor_acceptance_pricing_not_canonical")
        return self


class SuccessorOutlineStageAuthorization(_Closed):
    schema_version: Literal["successor_outline_stage_authorization.v1"] = (
        "successor_outline_stage_authorization.v1"
    )
    protocol_revision: Literal["required-outline-prestage-r1"] = (
        "required-outline-prestage-r1"
    )
    generation: RequiredGenerationPlanSnapshot
    prompt_protocol_digest: str = Field(pattern=_SHA256)
    chapter_count: Literal[3] = CHAPTER_COUNT
    target_word_count: Literal[3000] = TARGET_WORD_COUNT
    maximum_response_bytes: Literal[16000] = MAX_V2_OUTLINE_RESPONSE_UTF8_BYTES
    overflow_action: Literal[
        "same_provider_concise_regeneration_without_source"
    ] = "same_provider_concise_regeneration_without_source"
    overflow_failure_reason_code: Literal[
        "outline_response_byte_budget_exceeded"
    ] = OUTLINE_RESPONSE_BYTE_BUDGET_REASON_CODE
    regeneration_prompt_revision: str = Field(min_length=1, max_length=160)
    regeneration_phase: str = Field(min_length=1, max_length=160)
    uses_existing_second_semantic_attempt: Literal[True] = True
    primary_input_tokens_per_chapter: int = Field(ge=1, le=_MAX)
    repair_input_tokens_per_chapter: int = Field(ge=1, le=_MAX)
    byte_regeneration_input_tokens_per_chapter: int = Field(ge=1, le=_MAX)
    maximum_input_tokens_per_attempt: int = Field(ge=1, le=_MAX)
    maximum_output_tokens_per_attempt: Literal[20000] = (
        CHAPTER_OUTLINE_MAX_OUTPUT_TOKENS
    )
    maximum_semantic_attempts_per_chapter: Literal[2] = 2
    maximum_provider_attempts_total: Literal[6] = 6
    maximum_input_tokens_total: int = Field(ge=1, le=_MAX)
    maximum_output_tokens_total: Literal[120000] = (
        CHAPTER_COUNT * 2 * CHAPTER_OUTLINE_MAX_OUTPUT_TOKENS
    )
    maximum_tokens_total: int = Field(ge=1, le=_MAX)
    maximum_serial_seconds_total: Literal[1800] = (
        CHAPTER_COUNT * 2 * OUTLINE_TIMEOUT_SECONDS
    )
    can_generate_outline_candidates: Literal[True] = True
    can_accept_formal_outlines: Literal[True] = True

    @model_validator(mode="after")
    def validate_projection(self) -> "SuccessorOutlineStageAuthorization":
        plan = self.generation
        secondary = max(
            self.repair_input_tokens_per_chapter,
            self.byte_regeneration_input_tokens_per_chapter,
        )
        expected_input = CHAPTER_COUNT * (
            self.primary_input_tokens_per_chapter + secondary
        )
        if (
            plan.call_kind != "structured"
            or (plan.workflow, plan.step) != (OUTLINE_WORKFLOW, OUTLINE_STEP)
            or plan.target_provider_alias is None
            or plan.provider_alias != plan.target_provider_alias
            or plan.structured_output_mode
            not in {
                StructuredOutputMode.PROMPT_JSON.value,
                StructuredOutputMode.JSON_OBJECT.value,
            }
            or plan.reviewer_alias is not None
            or plan.timeout_seconds != OUTLINE_TIMEOUT_SECONDS
            or plan.max_semantic_attempts != 2
            or plan.max_output_tokens != CHAPTER_OUTLINE_MAX_OUTPUT_TOKENS
            or self.maximum_input_tokens_per_attempt
            != max(
                self.primary_input_tokens_per_chapter,
                self.repair_input_tokens_per_chapter,
                self.byte_regeneration_input_tokens_per_chapter,
            )
            or self.maximum_input_tokens_total != expected_input
            or self.maximum_tokens_total
            != self.maximum_input_tokens_total + self.maximum_output_tokens_total
            or plan.max_context_tokens is None
            or self.maximum_input_tokens_per_attempt
            + self.maximum_output_tokens_per_attempt
            > plan.max_context_tokens
        ):
            raise ValueError("successor_acceptance_outline_projection_invalid")
        return self


class SuccessorRootProjection(_Closed):
    """Identifier-free projection of the live root authority to be derived."""

    schema_version: Literal["successor_root_projection.v1", "successor_root_projection.v2"] = (
        "successor_root_projection.v1"
    )
    projection_digest: str = Field(pattern=_SHA256)
    root_protocol_revision: str = Field(min_length=1, max_length=160)
    chapter_count: Literal[3] = CHAPTER_COUNT
    chapter_order_indexes: tuple[Literal[1], Literal[2], Literal[3]] = (1, 2, 3)
    target_word_count: Literal[3000] = TARGET_WORD_COUNT
    scene_word_budgets: tuple[
        SuccessorSceneWordBudget,
        SuccessorSceneWordBudget,
    ]
    initial_prose: RequiredInitialProseAuthorization
    initial_generation: RequiredGenerationPlanSnapshot
    rewrite_planner: RequiredGenerationPlanSnapshot | None
    rewrite_generation: RequiredGenerationPlanSnapshot
    independent_review: RequiredGenerationPlanSnapshot
    state_generation: RequiredGenerationPlanSnapshot
    review_writer_model: str = Field(min_length=1, max_length=240)
    review_input_token_bound: int = Field(ge=1, le=_MAX)
    review_max_response_bytes: int = Field(ge=1, le=_MAX)
    review_capacity: RequiredAdherenceCapacity
    rewrite: RequiredRewriteAuthorization
    provider_usage_bounds: tuple[
        RequiredBookSuccessorProviderUsageBound, ...
    ] = Field(min_length=1, max_length=32)
    maximum_provider_attempts_total: int = Field(ge=1, le=_MAX)
    maximum_input_tokens_total: int = Field(ge=1, le=_MAX)
    maximum_output_tokens_total: int = Field(ge=1, le=_MAX)
    maximum_tokens_total: int = Field(ge=1, le=_MAX)
    maximum_serial_seconds_total: int = Field(ge=1, le=_MAX)
    expected_narrative_revision_increment: Literal[3] = CHAPTER_COUNT
    formal_outline_write_count: Literal[3] = CHAPTER_COUNT
    formal_prose_write_count: Literal[3] = CHAPTER_COUNT
    formal_state_accept_count: Literal[3] = CHAPTER_COUNT
    final_audit_required: Literal[True] = True
    zero_partial_formal_writes_on_failure: Literal[True] = True
    recovery_checkpoint: Literal["before_first_child"] = (
        REQUIRED_BOOK_SUCCESSOR_RECOVERY_CHECKPOINT_BEFORE_FIRST_CHILD
    )

    @model_validator(mode="after")
    def validate_projection(self) -> "SuccessorRootProjection":
        expected = (
            ("successor_root_projection.v1", "required_prose_rewrite_authorization.v1")
            if self.rewrite_planner is not None else
            ("successor_root_projection.v2", "required_prose_rewrite_authorization.v2")
        )
        if (self.schema_version, self.rewrite.schema_version) != expected:
            raise ValueError("successor_acceptance_dispatch_version_changed")
        if (
            self.maximum_provider_attempts_total
            != sum(item.maximum_paid_attempts for item in self.provider_usage_bounds)
            or self.maximum_input_tokens_total
            != sum(item.maximum_input_tokens for item in self.provider_usage_bounds)
            or self.maximum_output_tokens_total
            != sum(item.maximum_output_tokens for item in self.provider_usage_bounds)
            or self.maximum_tokens_total
            != self.maximum_input_tokens_total + self.maximum_output_tokens_total
            or len({item.provider_alias for item in self.provider_usage_bounds})
            != len(self.provider_usage_bounds)
        ):
            raise ValueError("successor_acceptance_root_projection_invalid")
        identity = self.model_dump(mode="python", exclude={"projection_digest"})
        if required_book_successor_digest(identity) != self.projection_digest:
            raise ValueError("successor_acceptance_root_projection_changed")
        return self


class SuccessorPricedProviderUsage(_Closed):
    schema_version: Literal["successor_priced_provider_usage.v1"] = (
        "successor_priced_provider_usage.v1"
    )
    provider_alias: str = Field(min_length=1, max_length=160)
    provider_model: str = Field(min_length=1, max_length=240)
    maximum_paid_attempts: int = Field(ge=1, le=_MAX)
    maximum_input_tokens: int = Field(ge=1, le=_MAX)
    maximum_output_tokens: int = Field(ge=1, le=_MAX)
    maximum_total_tokens: int = Field(ge=1, le=_MAX)
    pricing: SuccessorProviderPricing
    cost_upper_bound: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def validate_price(self) -> "SuccessorPricedProviderUsage":
        expected_cost = _price_ceiling(
            input_tokens=self.maximum_input_tokens,
            output_tokens=self.maximum_output_tokens,
            pricing=self.pricing,
        )
        if (
            self.provider_alias != self.pricing.provider_alias
            or self.provider_model != self.pricing.provider_model
            or self.maximum_total_tokens
            != self.maximum_input_tokens + self.maximum_output_tokens
            or self.cost_upper_bound != expected_cost
        ):
            raise ValueError("successor_acceptance_provider_usage_invalid")
        return self


class RequiredBookSuccessorAcceptanceAuthorization(_Closed):
    schema_version: Literal[
        "required_book_successor_acceptance_authorization.v3"
    ] = "required_book_successor_acceptance_authorization.v3"
    protocol_revision: Literal[
        "required-book-successor-acceptance-r3"
    ] = SUCCESSOR_ACCEPTANCE_PROTOCOL_REVISION
    contract_digest: str = Field(pattern=_SHA256)
    authorization_revision: int = Field(ge=1, le=_MAX)
    created_at: datetime
    deadline_at: datetime
    sample: SuccessorAcceptanceSample
    outline_stage: SuccessorOutlineStageAuthorization
    root_projection: SuccessorRootProjection
    judge_probe_evidence: RequiredJudgeProbeTerminalEvidence
    provider_usage_bounds: tuple[SuccessorPricedProviderUsage, ...] = Field(
        min_length=1,
        max_length=32,
    )
    maximum_provider_attempts_total: int = Field(ge=1, le=_MAX)
    maximum_input_tokens_total: int = Field(ge=1, le=_MAX)
    maximum_output_tokens_total: int = Field(ge=1, le=_MAX)
    maximum_tokens_total: int = Field(ge=1, le=_MAX)
    maximum_serial_seconds_total: int = Field(ge=1, le=_MAX)
    currency: str = Field(min_length=1, max_length=16)
    aggregate_cost_upper_bound: Decimal = Field(ge=0)
    required_authorization_codes: tuple[str, str, str] = (
        REQUIRED_AUTHORIZATION_CODES
    )
    isolated_database_write_and_final_hard_delete_required: Literal[True] = True
    cost_upper_bound_authorization_required: Literal[True] = True
    external_disclosure_authorization_required: Literal[True] = True
    provider_dispatch_allowed_by_readiness_alone: Literal[False] = False

    @model_validator(mode="after")
    def validate_aggregate(self) -> "RequiredBookSuccessorAcceptanceAuthorization":
        usage = self.provider_usage_bounds
        outline = self.outline_stage
        root = self.root_projection
        probe_evidence = validate_required_judge_probe_terminal_evidence(
            self.judge_probe_evidence
        )
        probe = probe_evidence.receipt
        if (
            self.created_at.tzinfo is None
            or self.deadline_at.tzinfo is None
            or self.deadline_at
            < self.created_at
            + timedelta(seconds=self.maximum_serial_seconds_total)
            or len({item.provider_alias for item in usage}) != len(usage)
            or len({item.pricing.currency for item in usage}) != 1
            or self.currency != usage[0].pricing.currency
            or self.maximum_provider_attempts_total
            != sum(item.maximum_paid_attempts for item in usage)
            or self.maximum_input_tokens_total
            != sum(item.maximum_input_tokens for item in usage)
            or self.maximum_output_tokens_total
            != sum(item.maximum_output_tokens for item in usage)
            or self.maximum_tokens_total
            != self.maximum_input_tokens_total + self.maximum_output_tokens_total
            or self.maximum_serial_seconds_total
            != outline.maximum_serial_seconds_total
            + root.maximum_serial_seconds_total
            or self.aggregate_cost_upper_bound
            != sum((item.cost_upper_bound for item in usage), Decimal(0))
        ):
            raise ValueError("successor_acceptance_authorization_invalid")
        validate_required_judge_capability_probe_receipt(
            probe,
            generation_plan=root.independent_review,
            review_contract_digest=root.review_capacity.review_contract_digest,
            review_input_token_bound=root.review_input_token_bound,
            readiness_created_at=self.created_at,
            readiness_deadline_at=self.deadline_at,
        )
        identity = self.model_dump(mode="python", exclude={"contract_digest"})
        if required_book_successor_digest(identity) != self.contract_digest:
            raise ValueError("successor_acceptance_authorization_changed")
        return self

    @property
    def judge_probe_receipt(self) -> RequiredJudgeCapabilityProbeReceipt:
        return self.judge_probe_evidence.receipt


def _price_ceiling(
    *,
    input_tokens: int,
    output_tokens: int,
    pricing: SuccessorProviderPricing,
) -> Decimal:
    amount = (
        Decimal(input_tokens) * pricing.input_cache_miss_per_million
        + Decimal(output_tokens) * pricing.output_per_million
    ) / Decimal(1_000_000)
    return amount.quantize(_PRICE_QUANTUM, rounding=ROUND_CEILING)


def _readonly_runtime(config: Mapping[str, Any]) -> GenerationRuntime:
    frozen = deepcopy(dict(config))

    def forbidden_adapter(_alias: str, _timeout: int | None) -> Any:
        raise AssertionError(
            "successor acceptance readiness constructed a Provider adapter"
        )

    return GenerationRuntime(
        config_supplier=lambda: deepcopy(frozen),
        adapter_factory=forbidden_adapter,
    )


def _redacted_outline_context_text() -> str:
    prefix = "readonly successor acceptance outline context\n"
    padding = READONLY_OUTLINE_CONTEXT_BYTES - len(prefix.encode("utf-8"))
    if padding < 0:
        raise AssertionError("successor acceptance outline prefix is oversized")
    return prefix + ("x" * padding)


def _outline_prompt_bounds(
    *,
    system_prompt: str,
) -> tuple[int, int, int, str]:
    prompts = load_prompt_config(force_reload=True)[CHAPTER_OUTLINE_PROMPT_NAME]
    primary = apply_agent_profile(
        "chapter_planner",
        prompts["chapter_outline_prompt_base"].format(
            context=_redacted_outline_context_text(),
            chapter_order=1,
            chapter_title="readonly successor acceptance fixture",
            style_controls=render_style_controls(None),
            words_per_chapter=TARGET_WORD_COUNT,
        )
        + "\n"
        + prompts["chapter_outline_prompt_without_schema_suffix"],
    )
    repair = render_structured_repair_prompt(
        original_prompt=primary,
        schema=ChapterOutlineResultSchema,
        produced="x" * MAX_V2_OUTLINE_RESPONSE_UTF8_BYTES,
        validation_issues=maximum_structured_validation_issues_projection(),
    )
    regeneration = render_structured_byte_budget_regeneration_prompt(
        original_prompt=primary,
        max_bytes=MAX_V2_OUTLINE_RESPONSE_UTF8_BYTES,
    )
    protocol_digest = required_book_successor_digest({
        "primary": primary,
        "repair": repair,
        "byte_regeneration": regeneration,
        "schema": ChapterOutlineResultSchema.model_json_schema(),
        # The value is hashed into the protocol identity and never serialized
        # into readiness.  Provider-default prompt drift must still invalidate
        # the authorization even when its token length happens to be equal.
        "effective_system_instruction": system_prompt,
    })
    return (
        conservative_prompt_input_bound(
            prompt=primary,
            system_prompt=system_prompt,
        ),
        conservative_prompt_input_bound(
            prompt=repair,
            system_prompt=system_prompt,
        ),
        conservative_prompt_input_bound(
            prompt=regeneration,
            system_prompt=system_prompt,
        ),
        protocol_digest,
    )


def _representative_outline(chapter_order: int) -> dict[str, Any]:
    scenes = []
    for scene_index in (1, 2):
        stem = f"c{chapter_order}s{scene_index}"
        scenes.append({
            "contract_version": "scene_transition_contract.v2",
            "scene_id": stem,
            "summary": f"synthetic successor scene {stem}",
            "purpose": "advance the bounded synthetic acceptance task",
            "preconditions": [{
                "condition_id": f"{stem}.pre",
                "description": "the prior bounded state is available",
            }],
            "beats": [{
                "beat_id": f"{stem}.beat",
                "description": "perform one auditable synthetic transition",
                "expected_transition": "the chapter gains one new state",
                "required": True,
            }],
            "postconditions": [{
                "condition_id": f"{stem}.post",
                "description": "the bounded transition is complete",
            }],
            "forbidden_conditions": [],
            "narrative_delta": [{
                "delta_id": f"{stem}.delta",
                "dimension": "risk",
                "before": "unresolved",
                "after": "advanced",
            }],
            "event_key": f"successor.acceptance.{stem}",
            "repetition_policy": "forbid",
            "word_budget": {"min": 1200, "target": 1500, "max": 1800},
        })
    outline = {
        "scene_contract_version": "scene_transition_contract.v2",
        "pov_character_card_id": None,
        "present_character_card_ids": [],
        "mentioned_character_card_ids": [],
        "referenced_worldbook_card_ids": [],
        "scenes": scenes,
        "core_conflict": "complete one bounded synthetic chapter transition",
        "ending_hook": "continue to the next bounded chapter state",
        "target_word_count": TARGET_WORD_COUNT,
        "threads_resolved": [],
        "new_threads": [],
        "new_reference_card_candidates": [],
    }
    return ChapterOutlineResultSchema.model_validate(outline).model_dump(
        mode="python"
    )


def _synthetic_root_authority(
    *,
    bundle: RequiredBookSuccessorPlanBundle,
) -> RequiredBookSuccessorAuthorization:
    novel_id = "1" * 24
    owner_id = "2" * 24
    volume_id = "3" * 24
    chapters = [
        {
            "_id": str(order + 3) * 24,
            "novel_id": novel_id,
            "volume_id": volume_id,
            "order_index": order,
            "content": "",
            "outline": _representative_outline(order),
        }
        for order in range(1, CHAPTER_COUNT + 1)
    ]
    base = {
        "version": 2,
        "novel_id": novel_id,
        "scope": "book",
        "volume_id": None,
        "outline_deviation_policy": "pause_for_rewrite",
        "work": {
            "chapters": [
                {
                    "chapter_id": chapter["_id"],
                    "volume_id": volume_id,
                    "has_outline": True,
                    "has_content": False,
                }
                for chapter in chapters
            ],
        },
        "resources": {
            "owner_id": owner_id,
            "narrative_revision": 0,
        },
        "active_proposal": None,
        "planning": {},
        "issues": [],
    }
    created_at = datetime(2040, 1, 1, tzinfo=timezone.utc)
    review = prepare_required_chapter_review_readiness(
        base,
        chapters=chapters,
        plan=bundle.chapter_review,
        token_budget=_MAX,
        authorization_revision=1,
        created_at=created_at,
        deadline_at=created_at + timedelta(days=30),
    )
    accepted_review = generation_readiness_module.authorize(
        review,
        supplied_digest=review["digest"],
        acknowledged_warning_codes=(REQUIRED_REVIEW_ACKNOWLEDGEMENT,),
    )
    root = prepare_required_book_successor_readiness(
        accepted_review,
        state_plan=bundle.state_generation,
        token_budget=_MAX,
        recovery_checkpoint=(
            REQUIRED_BOOK_SUCCESSOR_RECOVERY_CHECKPOINT_BEFORE_FIRST_CHILD
        ),
    )
    return validate_required_book_successor_readiness(root)


def _root_projection(
    authority: RequiredBookSuccessorAuthorization,
) -> SuccessorRootProjection:
    initials = [item.initial_prose for item in authority.review_template.chapters]
    if (
        len(initials) != CHAPTER_COUNT
        or any(item != initials[0] for item in initials[1:])
        or [item.order_index for item in authority.review_template.chapters]
        != [1, 2, 3]
    ):
        raise ValueError("successor_acceptance_root_sample_shape_changed")
    usage = required_book_successor_provider_usage_bounds(authority)
    review = authority.review_template
    identity = {
        "schema_version": "successor_root_projection.v1" if review.rewrite_planner is not None else "successor_root_projection.v2",
        "root_protocol_revision": authority.protocol_revision,
        "chapter_count": CHAPTER_COUNT,
        "chapter_order_indexes": (1, 2, 3),
        "target_word_count": TARGET_WORD_COUNT,
        "scene_word_budgets": (
            SuccessorSceneWordBudget(),
            SuccessorSceneWordBudget(),
        ),
        "initial_prose": initials[0],
        "initial_generation": review.initial_generation,
        "rewrite_planner": review.rewrite_planner,
        "rewrite_generation": review.rewrite_generation,
        "independent_review": review.independent_review,
        "state_generation": authority.state_generation,
        "review_writer_model": review.review_writer_model,
        "review_input_token_bound": review.review_input_token_bound,
        "review_max_response_bytes": review.review_max_response_bytes,
        "review_capacity": review.review.capacity,
        "rewrite": review.rewrite,
        "provider_usage_bounds": usage,
        "maximum_provider_attempts_total": authority.maximum_provider_attempts_total,
        "maximum_input_tokens_total": sum(
            item.maximum_input_tokens for item in usage
        ),
        "maximum_output_tokens_total": sum(
            item.maximum_output_tokens for item in usage
        ),
        "maximum_tokens_total": authority.maximum_tokens_total,
        "maximum_serial_seconds_total": authority.maximum_serial_seconds_total,
        "expected_narrative_revision_increment": CHAPTER_COUNT,
        "formal_outline_write_count": CHAPTER_COUNT,
        "formal_prose_write_count": authority.formal_write_count,
        "formal_state_accept_count": authority.formal_write_count,
        "final_audit_required": authority.final_audit_required,
        "zero_partial_formal_writes_on_failure": True,
        "recovery_checkpoint": authority.recovery_checkpoint,
    }
    return SuccessorRootProjection(
        **identity,
        projection_digest=required_book_successor_digest(identity),
    )


def _outline_stage(
    *,
    config: Mapping[str, Any],
    runtime: GenerationRuntime,
    provider_alias: str,
) -> SuccessorOutlineStageAuthorization:
    alias = str(provider_alias or "").strip()
    if not alias:
        raise ValueError("successor_acceptance_outline_provider_missing")
    plan = runtime.plan_structured(
        WorkflowStepTarget(
            OUTLINE_WORKFLOW,
            OUTLINE_STEP,
            provider_alias=alias,
        )
    )
    if (
        plan.reviewer_alias is not None
        or plan.max_semantic_attempts != 2
        or plan.timeout_seconds != OUTLINE_TIMEOUT_SECONDS
        or plan.max_output_tokens is None
        or plan.max_output_tokens < CHAPTER_OUTLINE_MAX_OUTPUT_TOKENS
        or plan.mode
        not in {
            StructuredOutputMode.PROMPT_JSON,
            StructuredOutputMode.JSON_OBJECT,
        }
    ):
        raise ValueError("successor_acceptance_outline_plan_unsupported")
    dispatch_plan: GenerationPlan = replace(
        plan,
        max_output_tokens=CHAPTER_OUTLINE_MAX_OUTPUT_TOKENS,
    )
    snapshot = RequiredGenerationPlanSnapshot.freeze(
        dispatch_plan,
        call_kind="structured",
        expected_target=(OUTLINE_WORKFLOW, OUTLINE_STEP),
    )
    system_prompt = effective_provider_system_prompt(
        config,
        dispatch_plan.provider_alias,
    )
    primary, repair, regeneration, prompt_digest = _outline_prompt_bounds(
        system_prompt=system_prompt,
    )
    secondary = max(repair, regeneration)
    maximum_input = max(primary, repair, regeneration)
    return SuccessorOutlineStageAuthorization(
        generation=snapshot,
        prompt_protocol_digest=prompt_digest,
        regeneration_prompt_revision=(
            STRUCTURED_BYTE_BUDGET_REGENERATION_PROMPT_REVISION
        ),
        regeneration_phase=STRUCTURED_BYTE_BUDGET_REGENERATION_PHASE,
        primary_input_tokens_per_chapter=primary,
        repair_input_tokens_per_chapter=repair,
        byte_regeneration_input_tokens_per_chapter=regeneration,
        maximum_input_tokens_per_attempt=maximum_input,
        maximum_input_tokens_total=CHAPTER_COUNT * (primary + secondary),
        maximum_tokens_total=(
            CHAPTER_COUNT * (primary + secondary)
            + CHAPTER_COUNT * 2 * CHAPTER_OUTLINE_MAX_OUTPUT_TOKENS
        ),
    )


def _sample() -> SuccessorAcceptanceSample:
    old_sample = BatchJobAcceptanceSample.REPRESENTATIVE_3000
    old_contract = old_sample.contract()
    blueprint = deepcopy(old_sample.fixture_blueprint())
    blueprint["novel"]["title"] = (
        "__required_book_successor_acceptance_representative_3000_v1__"
    )
    return SuccessorAcceptanceSample(
        scene_word_budgets=(
            SuccessorSceneWordBudget(),
            SuccessorSceneWordBudget(),
        ),
        fixture_blueprint_digest=required_book_successor_digest(blueprint),
        root_prompt_protocol_digest=old_contract["prompt_protocol_digest"],
    )


def _provider_models(
    *,
    outline: SuccessorOutlineStageAuthorization,
    root: SuccessorRootProjection,
) -> dict[str, str]:
    pairs = [
        (outline.generation.provider_alias, outline.generation.provider_model),
        (root.initial_generation.provider_alias, root.initial_generation.provider_model),
        (root.rewrite_generation.provider_alias, root.rewrite_generation.provider_model),
        (root.independent_review.provider_alias, root.independent_review.provider_model),
        (root.state_generation.provider_alias, root.state_generation.provider_model),
    ]
    if root.rewrite_planner is not None:
        pairs.append((root.rewrite_planner.provider_alias, root.rewrite_planner.provider_model))
    models: dict[str, str] = {}
    for alias, model in pairs:
        previous = models.setdefault(alias, model)
        if previous != model:
            raise ValueError("successor_acceptance_provider_model_ambiguous")
    return models


def _priced_usage(
    *,
    outline: SuccessorOutlineStageAuthorization,
    root: SuccessorRootProjection,
    pricing: Sequence[SuccessorProviderPricing],
) -> tuple[SuccessorPricedProviderUsage, ...]:
    price_by_alias = {item.provider_alias: item for item in pricing}
    if len(price_by_alias) != len(pricing):
        raise ValueError("successor_acceptance_pricing_alias_duplicated")
    models = _provider_models(outline=outline, root=root)
    usage: dict[str, list[int]] = {
        item.provider_alias: [
            item.maximum_paid_attempts,
            item.maximum_input_tokens,
            item.maximum_output_tokens,
        ]
        for item in root.provider_usage_bounds
    }
    outline_values = usage.setdefault(
        outline.generation.provider_alias,
        [0, 0, 0],
    )
    outline_values[0] += outline.maximum_provider_attempts_total
    outline_values[1] += outline.maximum_input_tokens_total
    outline_values[2] += outline.maximum_output_tokens_total
    if set(price_by_alias) != set(usage):
        raise ValueError("successor_acceptance_pricing_coverage_changed")
    result = []
    for alias in sorted(usage):
        attempts, input_tokens, output_tokens = usage[alias]
        item_pricing = price_by_alias[alias]
        if item_pricing.provider_model != models[alias]:
            raise ValueError("successor_acceptance_pricing_model_changed")
        result.append(SuccessorPricedProviderUsage(
            provider_alias=alias,
            provider_model=models[alias],
            maximum_paid_attempts=attempts,
            maximum_input_tokens=input_tokens,
            maximum_output_tokens=output_tokens,
            maximum_total_tokens=input_tokens + output_tokens,
            pricing=item_pricing,
            cost_upper_bound=_price_ceiling(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                pricing=item_pricing,
            ),
        ))
    return tuple(result)


def build_required_book_successor_acceptance_authorization(
    *,
    config: Mapping[str, Any],
    outline_provider_alias: str,
    writer_provider_alias: str,
    judge_provider_alias: str,
    state_provider_alias: str,
    provider_pricing: Sequence[SuccessorProviderPricing],
    judge_probe_evidence: RequiredJudgeProbeTerminalEvidence | Mapping[str, Any],
    review_max_response_bytes: int,
    authorization_revision: int,
    created_at: datetime,
    deadline_at: datetime,
    review_input_token_bound: int | None = None,
    rewrite_dispatch: Literal["fixed", "planner"] = "fixed",
) -> RequiredBookSuccessorAcceptanceAuthorization:
    """Build one complete, no-I/O authorization template for issue #20."""

    if not isinstance(config, Mapping):
        raise ValueError("successor_acceptance_config_invalid")
    runtime = _readonly_runtime(config)
    outline = _outline_stage(
        config=config,
        runtime=runtime,
        provider_alias=outline_provider_alias,
    )
    bundle = build_required_book_successor_plan_bundle(
        runtime,
        writer_provider_alias=writer_provider_alias,
        judge_provider_alias=judge_provider_alias,
        state_provider_alias=state_provider_alias,
        review_input_token_bound=review_input_token_bound,
        review_max_response_bytes=review_max_response_bytes,
        rewrite_dispatch=rewrite_dispatch,
    )
    root = _root_projection(_synthetic_root_authority(bundle=bundle))
    evidence = validate_required_judge_probe_terminal_evidence(
        judge_probe_evidence
    )
    probe = validate_required_judge_capability_probe_receipt(
        evidence.receipt,
        generation_plan=root.independent_review,
        review_contract_digest=root.review_capacity.review_contract_digest,
        review_input_token_bound=root.review_input_token_bound,
        readiness_created_at=created_at,
        readiness_deadline_at=deadline_at,
    )
    usages = _priced_usage(
        outline=outline,
        root=root,
        pricing=provider_pricing,
    )
    identity = {
        "schema_version": (
            "required_book_successor_acceptance_authorization.v3"
        ),
        "protocol_revision": SUCCESSOR_ACCEPTANCE_PROTOCOL_REVISION,
        "authorization_revision": authorization_revision,
        "created_at": created_at,
        "deadline_at": deadline_at,
        "sample": _sample(),
        "outline_stage": outline,
        "root_projection": root,
        "judge_probe_evidence": evidence,
        "provider_usage_bounds": usages,
        "maximum_provider_attempts_total": sum(
            item.maximum_paid_attempts for item in usages
        ),
        "maximum_input_tokens_total": sum(
            item.maximum_input_tokens for item in usages
        ),
        "maximum_output_tokens_total": sum(
            item.maximum_output_tokens for item in usages
        ),
        "maximum_tokens_total": sum(item.maximum_total_tokens for item in usages),
        "maximum_serial_seconds_total": (
            outline.maximum_serial_seconds_total
            + root.maximum_serial_seconds_total
        ),
        "currency": usages[0].pricing.currency if usages else "",
        "aggregate_cost_upper_bound": sum(
            (item.cost_upper_bound for item in usages),
            Decimal(0),
        ),
        "required_authorization_codes": REQUIRED_AUTHORIZATION_CODES,
        "isolated_database_write_and_final_hard_delete_required": True,
        "cost_upper_bound_authorization_required": True,
        "external_disclosure_authorization_required": True,
        "provider_dispatch_allowed_by_readiness_alone": False,
    }
    return RequiredBookSuccessorAcceptanceAuthorization(
        **identity,
        contract_digest=required_book_successor_digest(identity),
    )


def _contains_forbidden_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            str(key).strip().lower() in FORBIDDEN_REPORT_KEYS
            or _contains_forbidden_key(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_forbidden_key(item) for item in value)
    return False


def prepare_required_book_successor_acceptance_readiness(
    **kwargs: Any,
) -> dict[str, Any]:
    """Return redacted approval material; it grants no dispatch by itself."""

    authorization = build_required_book_successor_acceptance_authorization(
        **kwargs
    )
    issues = [
        {
            "code": DATABASE_AUTHORIZATION_CODE,
            "level": "warning_requires_ack",
            "details": {
                "scope": "one_new_isolated_test_novel_and_final_hard_delete",
            },
        },
        {
            "code": COST_AUTHORIZATION_CODE,
            "level": "warning_requires_ack",
            "details": {
                "currency": authorization.currency,
                "amount": str(authorization.aggregate_cost_upper_bound),
                "maximum_provider_attempts": (
                    authorization.maximum_provider_attempts_total
                ),
                "maximum_tokens": authorization.maximum_tokens_total,
            },
        },
        {
            "code": DISCLOSURE_AUTHORIZATION_CODE,
            "level": "warning_requires_ack",
            "details": {
                "sample_id": authorization.sample.sample_id,
                "chapter_count": CHAPTER_COUNT,
                "target_word_count_per_chapter": TARGET_WORD_COUNT,
                "providers": [
                    item.provider_alias
                    for item in authorization.provider_usage_bounds
                ],
            },
        },
    ]
    snapshot = {
        "schema_version": (
            "required_book_successor_acceptance_readiness.v3"
        ),
        # Keep the approval artifact JSON-native so a write/read round trip
        # cannot turn tuples, datetimes or Decimal values into a stale shape.
        "authorization": authorization.model_dump(mode="json"),
        "issues": issues,
        "safety": {
            "provider_calls": 0,
            "database_reads": False,
            "database_writes": False,
            "adapter_constructions": 0,
            "contains_raw_user_material": False,
            "grants_execution_authority": False,
        },
    }
    if _contains_forbidden_key(snapshot):
        raise ValueError("successor_acceptance_readiness_contains_forbidden_key")
    readiness = {
        **snapshot,
        "status": "warning_requires_ack",
        "digest": required_book_successor_digest(snapshot),
    }
    validate_required_book_successor_acceptance_readiness(readiness)
    return readiness


def validate_required_book_successor_acceptance_readiness(
    readiness: Mapping[str, Any],
) -> RequiredBookSuccessorAcceptanceAuthorization:
    if not isinstance(readiness, Mapping):
        raise ValueError("successor_acceptance_readiness_invalid")
    try:
        authorization = (
            RequiredBookSuccessorAcceptanceAuthorization.model_validate_json(
                json.dumps(
                    readiness.get("authorization"),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                )
            )
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("successor_acceptance_readiness_invalid") from exc
    issues = readiness.get("issues")
    safety = readiness.get("safety")
    snapshot = {
        "schema_version": readiness.get("schema_version"),
        "authorization": deepcopy(readiness.get("authorization")),
        "issues": deepcopy(issues),
        "safety": deepcopy(safety),
    }
    if (
        readiness.get("schema_version")
        != "required_book_successor_acceptance_readiness.v3"
        or readiness.get("status") != "warning_requires_ack"
        or not isinstance(issues, list)
        or tuple(item.get("code") for item in issues if isinstance(item, Mapping))
        != REQUIRED_AUTHORIZATION_CODES
        or any(
            not isinstance(item, Mapping)
            or item.get("level") != "warning_requires_ack"
            for item in issues
        )
        or safety
        != {
            "provider_calls": 0,
            "database_reads": False,
            "database_writes": False,
            "adapter_constructions": 0,
            "contains_raw_user_material": False,
            "grants_execution_authority": False,
        }
        or _contains_forbidden_key(snapshot)
        or readiness.get("digest")
        != required_book_successor_digest(snapshot)
    ):
        raise ValueError("successor_acceptance_readiness_changed")
    return authorization


def validate_required_book_successor_acceptance_outline(
    authorization: RequiredBookSuccessorAcceptanceAuthorization,
    outline: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate one live Provider outline without retaining its semantic text.

    Semantic scene text is intentionally free to vary.  The sample freezes the
    V2 contract version, exact scene count/allocation and Provider byte ceiling.
    """

    if not isinstance(
        authorization,
        RequiredBookSuccessorAcceptanceAuthorization,
    ):
        raise ValueError("successor_acceptance_authorization_required")
    expected_budgets = [
        {
            "min": item.minimum,
            "target": item.target,
            "max": item.maximum,
        }
        for item in authorization.sample.scene_word_budgets
    ]
    raw_scenes = outline.get("scenes") if isinstance(outline, Mapping) else None
    empty_fixture_dependencies = (
        outline.get("pov_character_card_id") is None
        and outline.get("present_character_card_ids") == []
        and outline.get("mentioned_character_card_ids") == []
        and outline.get("referenced_worldbook_card_ids") == []
        and outline.get("threads_resolved") == []
        and outline.get("new_threads") == []
        and outline.get("new_reference_card_candidates") == []
    ) if isinstance(outline, Mapping) else False
    if (
        not isinstance(outline, Mapping)
        or outline.get("scene_contract_version")
        != "scene_transition_contract.v2"
        or outline.get("target_word_count")
        != authorization.sample.target_word_count
        or not isinstance(raw_scenes, list)
        or len(raw_scenes) != len(expected_budgets)
        or any(not isinstance(scene, Mapping) for scene in raw_scenes)
        or [scene.get("word_budget") for scene in raw_scenes]
        != expected_budgets
        or not empty_fixture_dependencies
    ):
        raise ValueError("successor_acceptance_outline_shape_changed")
    try:
        parsed = ChapterOutlineResultSchema.model_validate(outline)
    except (TypeError, ValueError) as exc:
        raise ValueError("successor_acceptance_outline_invalid") from exc
    return parsed.model_dump(mode="python")


def validate_required_book_successor_acceptance_outlines(
    authorization: RequiredBookSuccessorAcceptanceAuthorization,
    outlines: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Validate all three ordered Provider outline results."""

    if len(outlines) != authorization.sample.chapter_count:
        raise ValueError("successor_acceptance_outline_count_changed")
    return tuple(
        validate_required_book_successor_acceptance_outline(
            authorization,
            outline,
        )
        for outline in outlines
    )


def validate_required_book_successor_acceptance_root_readiness(
    authorization: RequiredBookSuccessorAcceptanceAuthorization,
    root_readiness: Mapping[str, Any],
) -> RequiredBookSuccessorAuthorization:
    """Prove a live identifier-bearing root matches the frozen projection."""

    if not isinstance(
        authorization,
        RequiredBookSuccessorAcceptanceAuthorization,
    ):
        raise ValueError("successor_acceptance_authorization_required")
    live = validate_required_book_successor_readiness(root_readiness)
    if _root_projection(live) != authorization.root_projection:
        raise ValueError("successor_acceptance_live_root_changed")
    return live


def validate_current_required_book_successor_acceptance_authorization(
    authorization: RequiredBookSuccessorAcceptanceAuthorization,
    *,
    config: Mapping[str, Any],
) -> RequiredBookSuccessorAcceptanceAuthorization:
    """Re-plan against current config/source without crossing dispatch."""

    if not isinstance(
        authorization,
        RequiredBookSuccessorAcceptanceAuthorization,
    ):
        raise ValueError("successor_acceptance_authorization_required")
    outline_alias = authorization.outline_stage.generation.target_provider_alias
    root = authorization.root_projection
    writer_alias = root.initial_generation.target_provider_alias
    judge_alias = root.independent_review.target_provider_alias
    state_alias = root.state_generation.target_provider_alias
    if None in {outline_alias, writer_alias, judge_alias, state_alias}:
        raise ValueError("successor_acceptance_explicit_routes_changed")
    current = build_required_book_successor_acceptance_authorization(
        config=config,
        outline_provider_alias=str(outline_alias),
        writer_provider_alias=str(writer_alias),
        judge_provider_alias=str(judge_alias),
        state_provider_alias=str(state_alias),
        provider_pricing=tuple(
            item.pricing for item in authorization.provider_usage_bounds
        ),
        judge_probe_evidence=authorization.judge_probe_evidence,
        review_input_token_bound=root.review_input_token_bound,
        review_max_response_bytes=root.review_max_response_bytes,
        authorization_revision=authorization.authorization_revision,
        created_at=authorization.created_at,
        deadline_at=authorization.deadline_at,
        rewrite_dispatch="planner" if root.rewrite_planner is not None else "fixed",
    )
    if current != authorization:
        raise ValueError("successor_acceptance_authorization_stale")
    return authorization


def derive_required_book_successor_acceptance_root_readiness(
    authorization: RequiredBookSuccessorAcceptanceAuthorization,
    *,
    config: Mapping[str, Any],
    base_readiness: Mapping[str, Any],
    chapters: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Derive the accepted root readiness from three live formal outlines.

    This is still a pure planning operation.  It neither accepts the outlines
    in storage nor starts a Job.  The root acknowledgement is consumed under
    the already-frozen parent envelope so execution never asks for a second,
    wider grant after outline generation.
    """

    validate_current_required_book_successor_acceptance_authorization(
        authorization,
        config=config,
    )
    if (
        len(chapters) != authorization.sample.chapter_count
        or any(not isinstance(chapter, Mapping) for chapter in chapters)
    ):
        raise ValueError("successor_acceptance_chapter_set_changed")
    validate_required_book_successor_acceptance_outlines(
        authorization,
        tuple(chapter.get("outline") for chapter in chapters),
    )
    root = authorization.root_projection
    runtime = _readonly_runtime(config)
    bundle = build_required_book_successor_plan_bundle(
        runtime,
        writer_provider_alias=str(root.initial_generation.target_provider_alias),
        judge_provider_alias=str(root.independent_review.target_provider_alias),
        state_provider_alias=str(root.state_generation.target_provider_alias),
        review_input_token_bound=root.review_input_token_bound,
        review_max_response_bytes=root.review_max_response_bytes,
        rewrite_dispatch="planner" if root.rewrite_planner is not None else "fixed",
    )
    review = prepare_required_chapter_review_readiness(
        base_readiness,
        chapters=chapters,
        plan=bundle.chapter_review,
        token_budget=root.maximum_tokens_total,
        authorization_revision=authorization.authorization_revision,
        created_at=authorization.created_at,
        deadline_at=authorization.deadline_at,
    )
    accepted_review = generation_readiness_module.authorize(
        review,
        supplied_digest=review["digest"],
        acknowledged_warning_codes=(REQUIRED_REVIEW_ACKNOWLEDGEMENT,),
    )
    derived = prepare_required_book_successor_readiness(
        accepted_review,
        state_plan=bundle.state_generation,
        token_budget=root.maximum_tokens_total,
        recovery_checkpoint=(
            REQUIRED_BOOK_SUCCESSOR_RECOVERY_CHECKPOINT_BEFORE_FIRST_CHILD
        ),
    )
    validate_required_book_successor_acceptance_root_readiness(
        authorization,
        derived,
    )
    accepted_root = generation_readiness_module.authorize(
        derived,
        supplied_digest=derived["digest"],
        acknowledged_warning_codes=(
            REQUIRED_BOOK_SUCCESSOR_ACKNOWLEDGEMENT,
        ),
    )
    validate_required_book_successor_readiness(accepted_root)
    return accepted_root


__all__ = [
    "COST_AUTHORIZATION_CODE",
    "DATABASE_AUTHORIZATION_CODE",
    "DISCLOSURE_AUTHORIZATION_CODE",
    "REQUIRED_AUTHORIZATION_CODES",
    "SUCCESSOR_ACCEPTANCE_SAMPLE_ID",
    "RequiredBookSuccessorAcceptanceAuthorization",
    "SuccessorProviderPricing",
    "build_required_book_successor_acceptance_authorization",
    "derive_required_book_successor_acceptance_root_readiness",
    "prepare_required_book_successor_acceptance_readiness",
    "validate_current_required_book_successor_acceptance_authorization",
    "validate_required_book_successor_acceptance_outline",
    "validate_required_book_successor_acceptance_outlines",
    "validate_required_book_successor_acceptance_readiness",
    "validate_required_book_successor_acceptance_root_readiness",
]
