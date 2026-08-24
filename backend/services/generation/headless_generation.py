"""批量章节生成的无头适配器。

各步骤逐步迁入统一章节生成应用服务；本模块只把批量任务的授权、尝试作用域与
结果元组翻译给现有章节管线。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, AsyncGenerator, Callable, Dict, Tuple

from backend.llm.prompts.prompt_selector import PROSE_PROMPT_NAME, load_prompt_config
from backend.llm.schemas.novel_pydantic import (
    MAX_CHAPTER_OUTLINE_SCENES,
    chapter_outline_response_utf8_bytes,
)
from backend.scene_contract_versions import MAX_V2_OUTLINE_RESPONSE_UTF8_BYTES
from backend.services.llm.context_builder import (
    DEFAULT_CONTEXT_TOKEN_BUDGET,
    assemble_context,
    fetch_context_inputs,
)
from backend.services.llm.agent_orchestrator import apply_agent_profile
from backend.services.llm.workflow_runner import (
    WorkflowFailed,
    parse_sse_event,
    run_workflow,
)
from backend.services.llm.generation_runtime import (
    AttemptScope,
    GenerationPlan,
    WorkflowStepTarget,
    create_generation_runtime,
)
from backend.services.llm.pre_dispatch_boundaries import (
    restore_pre_dispatch_boundary,
)
from backend.db.repositories.novel_repository import novel_repo
from backend.db.repositories.chapter_repository import chapter_repo
from backend.services.generation.chapter_generation_application import (
    AcceptanceAuthority,
    AcceptanceTiming,
    ChapterGenerationApplicationDeps,
    ChapterGenerationApplicationService,
    ChapterGenerationResult,
    CHAPTER_OUTLINE_STEP,
    CHAPTER_OUTLINE_WORKFLOW,
    OutlineAdherenceCommand,
    OutlineGenerationCommand,
    OUTLINE_ADHERENCE_STEP,
    PROSE_REMEDIATION_WORKFLOW,
    ProseCandidateSource,
    ProseGenerationCommand,
    STATE_STEP,
    STATE_WORKFLOW,
    StateGenerationCommand,
    StateRepairGuidance,
)
from backend.services.generation.chapter_capability_registry import (
    build_chapter_capability_registry,
)
from backend.services.llm.capability_registry import CapabilityCall
from backend.api.llm_routers.prose_router import PROSE_STEP, PROSE_WORKFLOW
from backend.services.generation.chapter_pipeline import ChapterPipelineDeps
from backend.services.generation.candidate_repair_contracts import (
    JobMutationRecoveryBindingV1,
)
from backend.services.generation.job_planner import (
    REUSABLE_STATE_COMPLETION_STATUSES,
)
from backend.services.generation.prose_completion import prose_completion_module
from backend.services.generation.prose_continuation import (
    ProseContinuationPolicy,
)
from backend.services.generation.prose_runs import chapter_content_digest
from backend.services.generation.protected_generation_params import (
    validate_protected_generation_params,
)
from backend.services.novel.style_controls import render_style_controls

_GENERATION_OVERRIDE_KEYS = frozenset({
    "temperature",
    "top_p",
    "max_tokens",
    "presence_penalty",
    "frequency_penalty",
})

# ``_build_prompts`` serializes the source outline with the default JSON
# separators, which add one ASCII space after every comma and colon.  Every
# such separator already occupies at least one byte in the compact Provider
# response, so an additional full response-cap worth of bytes is a proven
# upper bound for *all* legal scene/array-count combinations.  This avoids
# mistaking a handful of structural endpoint samples for the true maximum.
UNKNOWN_V2_OUTLINE_JSON_SEPARATOR_MARGIN = (
    MAX_V2_OUTLINE_RESPONSE_UTF8_BYTES
)


@dataclass(frozen=True)
class GeneratedProseCandidate:
    """A deferred generation result plus its exact, validated ProseRun identity."""

    generation: ChapterGenerationResult
    source: ProseCandidateSource


def _generation_options(
    generation_params: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, int]]:
    values = validate_protected_generation_params(generation_params)
    overrides = {
        key: value
        for key, value in values.items()
        if key in _GENERATION_OVERRIDE_KEYS and value is not None
    }
    runtime_kwargs = (
        {}
        if values.get("allow_failure_retry", True)
        else {"max_provider_retries": 0}
    )
    return overrides, runtime_kwargs


def _continuation_policy(
    generation_params: Mapping[str, Any] | None,
) -> ProseContinuationPolicy:
    return ProseContinuationPolicy.from_mapping(
        dict(generation_params or {}).get("prose_continuation_policy")
    )


def _chapter_generation_service() -> ChapterGenerationApplicationService:
    production = ChapterGenerationApplicationDeps.production()
    return ChapterGenerationApplicationService(
        replace(
            production,
            create_runtime=create_generation_runtime,
            run_workflow=run_workflow,
        )
    )


def _chapter_capability_registry():
    return build_chapter_capability_registry(
        service_factory=_chapter_generation_service,
    )


def build_prose_base_prompt(
    *,
    context_text: str,
    chapter_order: int,
    chapter_title: str,
    style_controls: Mapping[str, Any] | None,
    words_per_chapter: int,
) -> str:
    """Render the exact stable base text shared by prose execution/readiness.

    This is deliberately pure: preflight can use it to measure the same
    request shape without constructing an LLM runtime or crossing the Provider
    boundary.
    """
    prompts = load_prompt_config().get(PROSE_PROMPT_NAME, {})
    return apply_agent_profile(
        "chapter_writer",
        prompts[f"{PROSE_STEP}_prompt_base"].format(
            context=str(context_text or ""),
            chapter_order=int(chapter_order or 0),
            chapter_title=str(chapter_title or ""),
            style_controls=render_style_controls(style_controls),
            words_per_chapter=max(1, int(words_per_chapter or 1)),
        )
        + "\n" + prompts[f"{PROSE_STEP}_prompt_without_schema_suffix"],
    )


def _unknown_outline_prompt_envelope() -> dict[str, Any]:
    """Return a valid V2 outline that saturates its Provider response bound.

    An outline is not available until its own accepted write has occurred.
    This one-scene shape maximizes the part repeated as ``current_scene`` while
    also filling every bounded nested array.  The 16KB response Gate and 20KB
    stored-context Gate jointly bound any later accepted outline; call-count
    planning separately retains the 20-scene ceiling.
    """
    widest = "\U0001f600"
    semantic = widest * 16
    scene = {
        "contract_version": "scene_transition_contract.v2",
        "scene_id": "scene-authorization-maximum",
        "summary": (widest * 209) + "xx",
        "purpose": semantic,
        "preconditions": [
            {
                "condition_id": f"pre-{index}",
                "description": semantic,
            }
            for index in range(20)
        ],
        "beats": [
            {
                "beat_id": f"beat-{index}",
                "description": semantic,
                "expected_transition": semantic,
                "required": True,
            }
            for index in range(20)
        ],
        "postconditions": [
            {
                "condition_id": f"post-{index}",
                "description": semantic,
            }
            for index in range(20)
        ],
        "forbidden_conditions": [
            {
                "condition_id": f"forbid-{index}",
                "description": semantic,
            }
            for index in range(20)
        ],
        "narrative_delta": [
            {
                "delta_id": f"delta-{index}",
                "dimension": "risk",
                "before": semantic,
                "after": f"{semantic[:-1]}x",
            }
            for index in range(20)
        ],
        "event_key": "authorization.maximum-scene",
        "repetition_policy": "allow",
        "word_budget": {"min": 1, "target": 50_000, "max": 50_000},
    }
    return {
        "scene_contract_version": "scene_transition_contract.v2",
        "pov_character_card_id": None,
        "present_character_card_ids": [],
        "mentioned_character_card_ids": [],
        "referenced_worldbook_card_ids": [],
        "scenes": [scene],
        "core_conflict": "x",
        "ending_hook": "x",
        "target_word_count": 50_000,
        "threads_resolved": [],
        "new_threads": [],
        "new_reference_card_candidates": [],
    }


def _unknown_dense_outline_prompt_envelope() -> dict[str, Any]:
    """Saturate the response cap with the densest legal scene structure.

    Pretty/sorted JSON used by downstream repair prompts adds separators and
    repeated keys that compact response bytes do not contain.  A one-scene
    semantic maximum and this twenty-scene structural maximum therefore form
    distinct conservative authorization cases.
    """

    scene_target = 50_000 // MAX_CHAPTER_OUTLINE_SCENES
    scenes = []
    for index in range(1, MAX_CHAPTER_OUTLINE_SCENES + 1):
        scenes.append({
            "contract_version": "scene_transition_contract.v2",
            "scene_id": f"s{index}",
            "summary": "x",
            "purpose": "x",
            "preconditions": [
                {"condition_id": f"p{index}", "description": "x"}
            ],
            "beats": [{
                "beat_id": f"b{index}",
                "description": "x",
                "expected_transition": "x",
                "required": True,
            }],
            "postconditions": [
                {"condition_id": f"o{index}", "description": "x"}
            ],
            "forbidden_conditions": [],
            "narrative_delta": [{
                "delta_id": f"d{index}",
                "dimension": "risk",
                "before": "a",
                "after": "b",
            }],
            "event_key": f"e.{index}",
            "repetition_policy": "allow",
            "word_budget": {
                "min": 1,
                "target": scene_target,
                "max": scene_target,
            },
        })
    outline = {
        "scene_contract_version": "scene_transition_contract.v2",
        "pov_character_card_id": None,
        "present_character_card_ids": [],
        "mentioned_character_card_ids": [],
        "referenced_worldbook_card_ids": [],
        "scenes": scenes,
        "core_conflict": "x",
        "ending_hook": "x",
        "target_word_count": 50_000,
        "threads_resolved": [],
        "new_threads": [],
        "new_reference_card_candidates": [],
    }
    remaining = (
        MAX_V2_OUTLINE_RESPONSE_UTF8_BYTES
        - chapter_outline_response_utf8_bytes(outline)
    )
    for scene in scenes:
        if remaining <= 0:
            break
        addition = min(500 - len(scene["summary"]), remaining)
        scene["summary"] += "x" * addition
        remaining -= addition
    if (
        remaining != 0
        or chapter_outline_response_utf8_bytes(outline)
        != MAX_V2_OUTLINE_RESPONSE_UTF8_BYTES
    ):
        raise AssertionError("dense V2 outline envelope no longer saturates its cap")
    return outline


def _unknown_mixed_outline_prompt_envelope() -> dict[str, Any]:
    """Return the legal three-scene/eighteen-entry mixed-structure case.

    The maximum default-JSON whitespace overhead is not necessarily at the
    one- or twenty-scene endpoints.  This regression envelope captures the
    counterexample that exposed that gap; authorization additionally applies
    ``UNKNOWN_V2_OUTLINE_JSON_SEPARATOR_MARGIN`` as the proof for the entire
    legal structure space.
    """

    scene_count = 3
    nested_count = 18
    targets = [16_667, 16_667, 16_666]
    scenes: list[dict[str, Any]] = []
    expandable_fields: list[tuple[dict[str, Any], str, int]] = []
    for scene_index in range(1, scene_count + 1):
        scene = {
            "contract_version": "scene_transition_contract.v2",
            "scene_id": f"s{scene_index}",
            "summary": "x",
            "purpose": "x",
            "preconditions": [
                {
                    "condition_id": f"p{scene_index}-{item_index}",
                    "description": "x",
                }
                for item_index in range(1, nested_count + 1)
            ],
            "beats": [
                {
                    "beat_id": f"b{scene_index}-{item_index}",
                    "description": "x",
                    "expected_transition": "x",
                    "required": True,
                }
                for item_index in range(1, nested_count + 1)
            ],
            "postconditions": [
                {
                    "condition_id": f"o{scene_index}-{item_index}",
                    "description": "x",
                }
                for item_index in range(1, nested_count + 1)
            ],
            "forbidden_conditions": [
                {
                    "condition_id": f"f{scene_index}-{item_index}",
                    "description": "x",
                }
                for item_index in range(1, nested_count + 1)
            ],
            "narrative_delta": [
                {
                    "delta_id": f"d{scene_index}-{item_index}",
                    "dimension": "risk",
                    "before": "a",
                    "after": "b",
                }
                for item_index in range(1, nested_count + 1)
            ],
            "event_key": f"e.{scene_index}",
            "repetition_policy": "allow",
            "word_budget": {
                "min": 1,
                "target": targets[scene_index - 1],
                "max": targets[scene_index - 1],
            },
        }
        scenes.append(scene)
        expandable_fields.extend([
            (scene, "summary", 500),
            (scene, "purpose", 200),
        ])
        for condition_group in (
            scene["preconditions"],
            scene["postconditions"],
            scene["forbidden_conditions"],
        ):
            expandable_fields.extend(
                (condition, "description", 500)
                for condition in condition_group
            )
        for beat in scene["beats"]:
            expandable_fields.extend([
                (beat, "description", 500),
                (beat, "expected_transition", 500),
            ])
        for delta in scene["narrative_delta"]:
            expandable_fields.extend([
                (delta, "before", 500),
                (delta, "after", 500),
            ])

    outline = {
        "scene_contract_version": "scene_transition_contract.v2",
        "pov_character_card_id": None,
        "present_character_card_ids": [],
        "mentioned_character_card_ids": [],
        "referenced_worldbook_card_ids": [],
        "scenes": scenes,
        "core_conflict": "x",
        "ending_hook": "x",
        "target_word_count": 50_000,
        "threads_resolved": [],
        "new_threads": [],
        "new_reference_card_candidates": [],
    }
    remaining = (
        MAX_V2_OUTLINE_RESPONSE_UTF8_BYTES
        - chapter_outline_response_utf8_bytes(outline)
    )
    for owner, field, maximum_length in expandable_fields:
        if remaining <= 0:
            break
        addition = min(maximum_length - len(owner[field]), remaining)
        owner[field] += "x" * addition
        remaining -= addition
    if (
        remaining != 0
        or chapter_outline_response_utf8_bytes(outline)
        != MAX_V2_OUTLINE_RESPONSE_UTF8_BYTES
    ):
        raise AssertionError("mixed V2 outline envelope no longer saturates its cap")
    return outline

def _unknown_outline_context_prompt_envelope() -> str:
    """Reserve the complete prose-context budget at worst-case UTF-8 width."""
    return (
        "\U0001f600"
        * DEFAULT_CONTEXT_TOKEN_BUDGET
    )


async def build_batch_prose_prompt_input_bounds(
    *,
    novel_id: str,
    chapters: list[dict[str, Any]],
    policy: ProseContinuationPolicy,
    generation_params: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Measure the largest rendered v3 prompt shape without Provider calls.

    Only scalar upper bounds leave this function.  It never returns prompt,
    prose, credentials, or a Provider adapter.  Known outlines use the exact
    context and scene data that dispatch will render; missing outlines use the
    bounded schema envelope until the post-acceptance reconciliation can make
    the authorization narrower.
    """
    from backend.services.generation.prose_readiness import (
        runtime_scene_prompt_input_bounds,
    )

    values = dict(generation_params or {})
    overrides, runtime_kwargs = _generation_options(values)
    runtime = create_generation_runtime(**runtime_kwargs)
    prose_plan = runtime.plan_text(WorkflowStepTarget(PROSE_WORKFLOW, PROSE_STEP))
    novel = await novel_repo.get_novel_by_id(novel_id)

    base_inputs: list[int] = []
    continuation_inputs: list[int] = []
    unknown_outline_chapters = 0
    for chapter in chapters:
        if str(chapter.get("content") or "").strip():
            continue
        chapter_id = str(chapter.get("_id") or "")
        if not chapter_id:
            raise ValueError("readiness cannot measure a chapter without an id")
        context_inputs = await fetch_context_inputs(novel_id, chapter_id)
        actual_outline = dict((context_inputs.get("chapter") or {}).get("outline") or {})
        if actual_outline:
            outline_for_context = actual_outline
            outline_for_execution = actual_outline
            context = assemble_context(context_inputs)
            context_text = context.to_prompt_text()
        else:
            unknown_outline_chapters += 1
            outline_for_context = _unknown_outline_prompt_envelope()
            # The base-call output cap must cover a one-scene, max-word outline;
            # the separate planning layer still retains the 20-scene call count.
            outline_for_execution = {
                **outline_for_context,
                "scenes": [dict(outline_for_context["scenes"][0])],
            }
            # Validate and truncate the real context under the runtime budget.
            # The synthetic maximum outline is an authorization envelope, not
            # persisted content, so append it only to the measured prompt. If
            # it were inserted into assemble_context it would trip the 8k
            # runtime guard before a real outline even exists.
            assemble_context(context_inputs)
            context_text = "\n\n".join(filter(None, (
                _unknown_outline_context_prompt_envelope(),
                f"本章细纲：{outline_for_context}",
            )))
        target_words = int(
            outline_for_execution.get("target_word_count")
            or novel.get("words_per_chapter")
            or 3_000
        )
        base_prompt = build_prose_base_prompt(
            context_text=context_text,
            chapter_order=int(chapter.get("order_index") or 0),
            chapter_title=str(chapter.get("title") or ""),
            style_controls=novel.get("style_controls"),
            words_per_chapter=target_words,
        )
        execution_plan = prose_completion_module.plan(
            outline=outline_for_execution,
            target_word_count=target_words,
            provider_capability={
                "max_output_tokens": prose_plan.max_output_tokens,
                "model": prose_plan.provider_model,
            },
            request_overrides=overrides,
        )
        base_input, continuation_input = runtime_scene_prompt_input_bounds(
            execution_plan=execution_plan,
            policy=policy,
            base_prompt=base_prompt,
            outline=outline_for_context,
            generation_kwargs=overrides,
        )
        base_inputs.append(base_input)
        continuation_inputs.append(continuation_input)

    return {
        "basis": "v3_rendered_prompt_utf8_plus_provider_framing",
        "base_input_token_bound": max(base_inputs or [0]),
        "continuation_input_token_bound": max(continuation_inputs or [0]),
        "measured_prose_chapter_count": len(base_inputs),
        "unknown_outline_chapter_count": unknown_outline_chapters,
    }


def estimate_chapter_attempt_slots(
    chapter: Dict[str, Any],
    generation_params: Mapping[str, Any] | None = None,
) -> int:
    """按当前不可变 GenerationPlan 计算一章的最大语义调用数。"""
    overrides, runtime_kwargs = _generation_options(generation_params)
    runtime = create_generation_runtime(**runtime_kwargs)
    continuation_policy = _continuation_policy(generation_params)
    slots = 0
    if not chapter.get("outline"):
        slots += runtime.plan_structured(
            WorkflowStepTarget(CHAPTER_OUTLINE_WORKFLOW, CHAPTER_OUTLINE_STEP)
        ).max_semantic_attempts
    if not str(chapter.get("content") or "").strip():
        text_plan = runtime.plan_text(
            WorkflowStepTarget(PROSE_WORKFLOW, PROSE_STEP)
        )
        outline = chapter.get("outline") or {}
        if outline:
            prose_plan = prose_completion_module.plan(
                outline=outline,
                target_word_count=int(
                    outline.get("target_word_count")
                    or chapter.get("words_per_chapter")
                    or 3_000
                ),
                provider_capability={
                    "max_output_tokens": text_plan.max_output_tokens,
                    "model": text_plan.provider_model,
                },
                request_overrides=overrides,
            )
            maximum_logical_call_count = getattr(
                prose_plan,
                "maximum_logical_call_count",
                None,
            )
            if callable(maximum_logical_call_count):
                slots += maximum_logical_call_count(continuation_policy)
            else:
                # Compatibility for narrow test/embedding fakes which expose
                # only the old base-call count surface.
                base_calls = int(
                    getattr(prose_plan, "scheduled_base_call_count", 0)
                    or getattr(prose_plan, "call_count", 0)
                )
                slots += base_calls + (
                    max(1, len(outline.get("scenes") or []))
                    * continuation_policy.automatic_continuations_per_scene
                )
        else:
            # 细纲尚未生成，场景数和逐场景预算未知。预留有界的保守容量，
            # 生成出细纲后实际调用仍受每章 reservation 约束，不可无限扩张。
            from backend.llm.schemas.novel_pydantic import MAX_CHAPTER_OUTLINE_SCENES

            slots += 32 + (
                MAX_CHAPTER_OUTLINE_SCENES
                * continuation_policy.automatic_continuations_per_scene
            )
    if (
        str(
            (chapter.get("state_completion") or {}).get("status")
            or "missing"
        )
        not in REUSABLE_STATE_COMPLETION_STATUSES
    ):
        # 细纲符合度审查与状态回填是两个独立 workflow；即便当前恰好
        # 选择同一 Provider，也必须按各自冻结 plan 预留。
        slots += runtime.plan_structured(
            WorkflowStepTarget(
                PROSE_REMEDIATION_WORKFLOW,
                OUTLINE_ADHERENCE_STEP,
            )
        ).max_semantic_attempts
        slots += runtime.plan_structured(
            WorkflowStepTarget(STATE_WORKFLOW, STATE_STEP)
        ).max_semantic_attempts
    return slots


def estimate_worklist_attempt_capacity(
    chapters: list[Dict[str, Any]],
    generation_params: Mapping[str, Any] | None = None,
) -> int:
    """固定总容量包含首次运行和每章至多一次偏离修订后的重新检查。"""

    _overrides, runtime_kwargs = _generation_options(generation_params)
    runtime = create_generation_runtime(**runtime_kwargs)
    adherence_recheck_slots = runtime.plan_structured(
        WorkflowStepTarget(
            PROSE_REMEDIATION_WORKFLOW,
            OUTLINE_ADHERENCE_STEP,
        )
    ).max_semantic_attempts
    capacity = sum(
        estimate_chapter_attempt_slots(chapter, generation_params)
        + (
            adherence_recheck_slots
            if str(
                (chapter.get("state_completion") or {}).get("status")
                or "missing"
            )
            not in REUSABLE_STATE_COMPLETION_STATUSES
            else 0
        )
        for chapter in chapters
    )
    return max(1, capacity)


async def _consume_prose_frames(frames: AsyncGenerator[str, None]) -> Tuple[str, int]:
    """消费 stream_prose 帧 → (完整正文, total_tokens)；done{success:false} 抛 WorkflowFailed。"""
    async for frame in frames:
        parsed = parse_sse_event(frame)
        if parsed is None:
            continue
        event, data = parsed
        if event == "done":
            if not data.get("success"):
                raise WorkflowFailed(
                    data.get("error") or "prose generation failed",
                    usage=data.get("usage_so_far"),
                    attempts=data.get("attempts"),
                )
            return str(data.get("text") or ""), int((data.get("usage") or {}).get("total_tokens") or 0)
    raise WorkflowFailed("prose stream ended without a done event")


async def generate_outline(
    novel_id: str,
    chapter: Dict[str, Any],
    attempt_scope: AttemptScope | None = None,
    generation_params: Mapping[str, Any] | None = None,
    *,
    generation_plan: GenerationPlan | None = None,
    expected_narrative_revision: int | None = None,
    mutation_idempotency_key: str | None = None,
    job_mutation_binding: JobMutationRecoveryBindingV1 | None = None,
) -> tuple[dict, dict, int, dict, list[dict[str, Any]], list[dict[str, Any]]]:
    execution = await _chapter_capability_registry().execute(
        "chapter_outline",
        OutlineGenerationCommand(
            novel_id=novel_id,
            chapter_id=str(chapter["_id"]),
            authority=AcceptanceAuthority.SYSTEM,
            generation_params=dict(generation_params or {}),
            attempt_scope=attempt_scope,
            generation_plan=generation_plan,
            expected_narrative_revision=expected_narrative_revision,
            mutation_idempotency_key=mutation_idempotency_key,
            job_mutation_binding=job_mutation_binding,
        ),
        call=CapabilityCall(source="job_engine"),
    )
    result = execution.value
    return (
        result.value,
        result.dropped,
        result.total_tokens,
        result.truncation,
        result.attempts,
        result.remapped,
    )


async def generate_outline_preview(
    novel_id: str,
    chapter: Dict[str, Any],
    attempt_scope: AttemptScope | None = None,
    generation_params: Mapping[str, Any] | None = None,
    *,
    generation_plan: GenerationPlan | None = None,
) -> tuple[dict, dict, int, dict, list[dict[str, Any]], list[dict[str, Any]]]:
    """Generate an outline candidate without writing the formal outline."""

    execution = await _chapter_capability_registry().execute(
        "chapter_outline",
        OutlineGenerationCommand(
            novel_id=novel_id,
            chapter_id=str(chapter["_id"]),
            authority=AcceptanceAuthority.PREVIEW,
            generation_params=dict(generation_params or {}),
            attempt_scope=attempt_scope,
            generation_plan=generation_plan,
        ),
        call=CapabilityCall(source="job_engine"),
    )
    result = execution.value
    if result.accepted:
        raise RuntimeError("outline preview unexpectedly entered formal storage")
    return (
        result.value,
        result.dropped,
        result.total_tokens,
        result.truncation,
        result.attempts,
        result.remapped,
    )


async def generate_prose(
    novel_id: str,
    chapter: Dict[str, Any],
    attempt_scope: AttemptScope | None = None,
    generation_params: Mapping[str, Any] | None = None,
) -> tuple[str, int, dict, list[dict[str, Any]], dict[str, Any]]:
    execution = await _chapter_capability_registry().execute(
        "chapter_prose",
        ProseGenerationCommand(
            novel_id=novel_id,
            chapter_id=str(chapter["_id"]),
            authority=AcceptanceAuthority.SYSTEM,
            generation_params=dict(generation_params or {}),
            attempt_scope=attempt_scope,
        ),
        call=CapabilityCall(source="job_engine"),
    )
    result = execution.value
    return (
        str(result.value or ""),
        result.total_tokens,
        result.truncation,
        result.attempts,
        result.completion,
    )


async def generate_prose_candidate(
    novel_id: str,
    chapter: Dict[str, Any],
    attempt_scope: AttemptScope | None = None,
    generation_params: Mapping[str, Any] | None = None,
    *,
    generation_plan: GenerationPlan | None = None,
    generation_job_id: str | None = None,
) -> GeneratedProseCandidate:
    """Generate a persisted ProseRun candidate without accepting formal prose."""
    execution = await _chapter_capability_registry().execute(
        "chapter_prose",
        ProseGenerationCommand(
            novel_id=novel_id,
            chapter_id=str(chapter["_id"]),
            authority=AcceptanceAuthority.SYSTEM,
            acceptance_timing=AcceptanceTiming.DEFERRED,
            generation_params=dict(generation_params or {}),
            attempt_scope=attempt_scope,
            generation_plan=generation_plan,
            generation_job_id=generation_job_id,
        ),
        call=CapabilityCall(source="job_engine"),
    )
    result = execution.value
    if result.accepted:
        raise RuntimeError("deferred prose generation accepted formal content")
    completion = dict(result.completion or {})
    boundary = restore_pre_dispatch_boundary(
        completion.get("pause_reason"),
    )
    if boundary is not None:
        raise boundary
    revision = completion.get("source_run_revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise RuntimeError("deferred prose candidate has no valid run revision")
    text = str(result.value or "")
    run_id = str(completion.get("source_run_id") or "")
    if not run_id:
        raise RuntimeError("deferred prose candidate has no run id")
    digest = str(completion.get("source_run_digest") or "")
    if chapter_content_digest(text) != digest:
        raise RuntimeError("deferred prose candidate digest does not match its text")
    source = ProseCandidateSource(
        text=text,
        source_run_id=run_id,
        source_run_revision=revision,
        source_content_digest=digest,
        completion=completion,
    )
    return GeneratedProseCandidate(generation=result, source=source)

async def generate_state(
    novel_id: str,
    chapter: Dict[str, Any],
    attempt_scope: AttemptScope | None = None,
    generation_params: Mapping[str, Any] | None = None,
    *,
    job_mutation_binding: JobMutationRecoveryBindingV1 | None = None,
) -> tuple[dict, dict, int, dict, list[dict[str, Any]], dict[str, Any]]:
    execution = await _chapter_capability_registry().execute(
        "chapter_state",
        StateGenerationCommand(
            novel_id=novel_id,
            chapter_id=str(chapter["_id"]),
            authority=AcceptanceAuthority.SYSTEM,
            generation_params=dict(generation_params or {}),
            attempt_scope=attempt_scope,
            job_mutation_binding=job_mutation_binding,
        ),
        call=CapabilityCall(source="job_engine"),
    )
    result = execution.value
    return (
        dict(result.value or {}),
        result.dropped,
        result.total_tokens,
        result.truncation,
        result.attempts,
        result.acceptance,
    )


async def generate_state_candidate(
    novel_id: str,
    chapter: Dict[str, Any],
    prose_candidate: ProseCandidateSource,
    attempt_scope: AttemptScope | None = None,
    generation_params: Mapping[str, Any] | None = None,
    *,
    generation_plan: GenerationPlan | None = None,
    repair_guidance: StateRepairGuidance | None = None,
    request_id: str | None = None,
) -> ChapterGenerationResult:
    """Extract a persisted state proposal without accepting chapter state."""
    execution = await _chapter_capability_registry().execute(
        "chapter_state",
        StateGenerationCommand(
            novel_id=novel_id,
            chapter_id=str(chapter["_id"]),
            authority=AcceptanceAuthority.SYSTEM,
            acceptance_timing=AcceptanceTiming.DEFERRED,
            generation_params=dict(generation_params or {}),
            attempt_scope=attempt_scope,
            prose_candidate=prose_candidate,
            generation_plan=generation_plan,
            repair_guidance=repair_guidance,
            request_id=request_id,
        ),
        call=CapabilityCall(source="job_engine"),
    )
    result = execution.value
    if result.accepted:
        raise RuntimeError("deferred state generation accepted formal state")
    proposal = dict(result.value or {})
    if not proposal.get("proposal_id") or not proposal.get("acceptance_token"):
        raise RuntimeError("deferred state generation returned no proposal receipt")
    return result

async def generate_outline_adherence(
    novel_id: str,
    chapter: Dict[str, Any],
    attempt_scope: AttemptScope | None = None,
    generation_params: Mapping[str, Any] | None = None,
) -> tuple[dict, int, dict, list[dict[str, Any]]]:
    execution = await _chapter_capability_registry().execute(
        "chapter_outline_adherence",
        OutlineAdherenceCommand(
            novel_id=novel_id,
            chapter_id=str(chapter["_id"]),
            generation_params=dict(generation_params or {}),
            attempt_scope=attempt_scope,
        ),
        call=CapabilityCall(source="job_engine"),
    )
    result = execution.value
    return (
        dict(result.value or {}),
        result.total_tokens,
        result.truncation,
        result.attempts,
    )


async def review_prose_candidate(
    novel_id: str,
    chapter: Dict[str, Any],
    prose_candidate: ProseCandidateSource,
    attempt_scope: AttemptScope | None = None,
    generation_params: Mapping[str, Any] | None = None,
    *,
    generation_plan: GenerationPlan | None = None,
) -> ChapterGenerationResult:
    """Review one exact deferred prose candidate without reading formal prose."""
    execution = await _chapter_capability_registry().execute(
        "chapter_outline_adherence",
        OutlineAdherenceCommand(
            novel_id=novel_id,
            chapter_id=str(chapter["_id"]),
            generation_params=dict(generation_params or {}),
            attempt_scope=attempt_scope,
            prose_candidate=prose_candidate,
            generation_plan=generation_plan,
        ),
        call=CapabilityCall(source="job_engine"),
    )
    result = execution.value
    review = dict(result.value or {})
    if (
        str(review.get("source_prose_run_id") or "")
        != prose_candidate.source_run_id
        or int(review.get("source_prose_run_revision") or -1)
        != prose_candidate.source_run_revision
        or str(review.get("source_content_digest") or "")
        != prose_candidate.source_content_digest
    ):
        raise RuntimeError("candidate review lost its exact prose identity")
    return result

def _serialize_attempts(runtime) -> list[dict[str, Any]]:
    if runtime is None:
        return []
    return [
        {
            "attempt_id": item.attempt_id,
            "provider_alias": item.provider_alias,
            "phase": item.phase,
            "state": item.state,
            "usage": item.usage.model_dump(),
        }
        for item in runtime.attempts
    ]


async def _outline_already_accepted(chapter_id: str, result: dict) -> None:
    """统一应用服务已在 SYSTEM 权限下接受章纲；兼容旧管线回调形状。"""

    del chapter_id, result


async def _prose_already_accepted(
    chapter_id: str,
    text: str,
    completion: dict[str, Any],
) -> None:
    """统一应用服务已通过 ProseRun mutation 接受正文。"""

    del chapter_id, text, completion


async def _state_already_accepted(chapter_id: str, proposal: dict) -> dict:
    """统一应用服务已通过同一 Proposal 接受路径回填状态。"""

    del chapter_id, proposal
    return {}


def build_chapter_pipeline_deps(
    attempt_scope_factory: Callable[[str], AttemptScope] | None = None,
    *,
    generation_params: Mapping[str, Any] | None = None,
    recalculate_prose_authorization: (
        Callable[[str, dict[str, Any]], Any] | None
    ) = None,
    state_job_mutation_binding: JobMutationRecoveryBindingV1 | None = None,
) -> ChapterPipelineDeps:
    def scope(step: str) -> AttemptScope | None:
        return attempt_scope_factory(step) if attempt_scope_factory is not None else None

    async def generate_state_step(
        novel_id: str,
        chapter: Dict[str, Any],
    ):
        if state_job_mutation_binding is None:
            return await generate_state(
                novel_id,
                chapter,
                scope("state"),
                generation_params,
            )
        return await generate_state(
            novel_id,
            chapter,
            scope("state"),
            generation_params,
            job_mutation_binding=state_job_mutation_binding,
        )

    return ChapterPipelineDeps(
        generate_outline=lambda novel_id, chapter: generate_outline(
            novel_id,
            chapter,
            scope("outline"),
            generation_params,
        ),
        generate_prose=lambda novel_id, chapter: generate_prose(
            novel_id,
            chapter,
            scope("prose"),
            generation_params,
        ),
        review_outline_adherence=lambda novel_id, chapter: generate_outline_adherence(
            novel_id,
            chapter,
            scope("outline_adherence"),
            generation_params,
        ),
        generate_state=generate_state_step,
        accept_outline=_outline_already_accepted,
        write_prose=_prose_already_accepted,
        accept_state=_state_already_accepted,
        recalculate_prose_authorization=recalculate_prose_authorization,
    )
