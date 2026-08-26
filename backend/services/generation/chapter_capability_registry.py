"""Executable capability adapters for the shared chapter application seam."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from backend.services.generation.chapter_generation_application import (
    CHAPTER_OUTLINE_STEP,
    CHAPTER_OUTLINE_WORKFLOW,
    ChapterGenerationApplicationService,
    ChapterGenerationEvent,
    ChapterGenerationResult,
    OutlineAdherenceCommand,
    OutlineGenerationCommand,
    OUTLINE_ADHERENCE_STEP,
    PROSE_REMEDIATION_WORKFLOW,
    PROSE_STEP,
    PROSE_WORKFLOW,
    ProseGenerationCommand,
    STATE_STEP,
    STATE_WORKFLOW,
    StateGenerationCommand,
)
from backend.llm.schemas.novel_pydantic import (
    MAX_CHAPTER_OUTLINE_SCENES,
    MAX_CHAPTER_OUTLINE_TARGET_WORDS,
)
from backend.services.generation.prose_continuation import (
    ProseContinuationPolicy,
)
from backend.services.generation.prose_protocol import (
    maximum_v2_chapter_base_calls,
)
from backend.services.generation.prose_completion import prose_completion_module
from backend.services.llm.outline_generation import (
    CHAPTER_OUTLINE_MAX_OUTPUT_TOKENS,
)
from backend.services.llm.capability_registry import (
    CapabilityBudget,
    CapabilityCall,
    CapabilityDefinition,
    CapabilityHandler,
    CapabilityRegistry,
    ContextProvider,
    RevisionPolicy,
    SideEffectPolicy,
)
from backend.services.llm.generation_runtime import (
    WorkflowStepTarget,
    create_generation_runtime,
)


_STRUCTURED_FALLBACK_ATTEMPTS = 4
_FALLBACK_OUTPUT_TOKENS = 20_000


@dataclass(frozen=True)
class PreparedChapterCapability:
    service: ChapterGenerationApplicationService
    value: Any


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _try_structured_plan(command, target: WorkflowStepTarget):
    try:
        runtime = create_generation_runtime(
            **(
                {}
                if command.generation_params.get(
                    "allow_failure_retry",
                    True,
                )
                else {"max_provider_retries": 0}
            )
        )
        return runtime.plan_structured(target)
    except ValueError:
        return None


def _try_text_plan(command, target: WorkflowStepTarget):
    try:
        runtime = create_generation_runtime(
            **(
                {}
                if command.generation_params.get(
                    "allow_failure_retry",
                    True,
                )
                else {"max_provider_retries": 0}
            )
        )
        return runtime.plan_text(target)
    except ValueError:
        return None


async def _execute_chapter_generation(
    command: (
        OutlineGenerationCommand
        | OutlineAdherenceCommand
        | ProseGenerationCommand
        | StateGenerationCommand
    ),
    prepared: PreparedChapterCapability,
    _call: CapabilityCall,
) -> ChapterGenerationResult:
    return await prepared.service.collect_prepared(prepared.value)


async def _stream_chapter_generation(
    command: (
        OutlineGenerationCommand
        | OutlineAdherenceCommand
        | ProseGenerationCommand
        | StateGenerationCommand
    ),
    prepared: PreparedChapterCapability,
    _call: CapabilityCall,
):
    return await prepared.service.execute_prepared(prepared.value)


def _outline_budget(command: OutlineGenerationCommand) -> CapabilityBudget:
    plan = _try_structured_plan(
        command,
        WorkflowStepTarget(CHAPTER_OUTLINE_WORKFLOW, CHAPTER_OUTLINE_STEP)
    )
    return CapabilityBudget(
        max_paid_attempts=(
            int(plan.max_semantic_attempts)
            if plan is not None
            else _STRUCTURED_FALLBACK_ATTEMPTS
        ),
        max_output_tokens=min(
            _positive_int(command.generation_params.get("max_tokens"))
            or (
                _positive_int(plan.max_output_tokens)
                if plan is not None
                else None
            )
            or CHAPTER_OUTLINE_MAX_OUTPUT_TOKENS,
            CHAPTER_OUTLINE_MAX_OUTPUT_TOKENS,
        ),
    )


def _prose_budget(command: ProseGenerationCommand) -> CapabilityBudget:
    plan = _try_text_plan(
        command,
        WorkflowStepTarget(PROSE_WORKFLOW, PROSE_STEP),
    )
    policy = ProseContinuationPolicy.from_mapping(
        dict(command.generation_params).get("prose_continuation_policy")
    )
    per_call_output = int(
        _positive_int(command.generation_params.get("max_tokens"))
        or (
            _positive_int(plan.max_output_tokens)
            if plan is not None
            else None
        )
        or _FALLBACK_OUTPUT_TOKENS
    )
    capability_plan = prose_completion_module.plan(
        outline={"scenes": [{}]},
        target_word_count=MAX_CHAPTER_OUTLINE_TARGET_WORDS,
        provider_capability={
            "max_output_tokens": per_call_output,
            "model": getattr(plan, "provider_model", None),
        },
        request_overrides={},
    )
    maximum_calls = maximum_v2_chapter_base_calls(
        safe_output_budget=capability_plan.safe_output_budget,
    ) + (
        MAX_CHAPTER_OUTLINE_SCENES
        * policy.automatic_continuations_per_scene
    )
    return CapabilityBudget(
        max_paid_attempts=maximum_calls,
        max_output_tokens=int(
            command.token_budget
            or maximum_calls * per_call_output
        ),
    )


def _state_budget(command: StateGenerationCommand) -> CapabilityBudget:
    plan = _try_structured_plan(
        command,
        WorkflowStepTarget(STATE_WORKFLOW, STATE_STEP)
    )
    return CapabilityBudget(
        max_paid_attempts=(
            int(plan.max_semantic_attempts)
            if plan is not None
            else _STRUCTURED_FALLBACK_ATTEMPTS
        ),
        max_output_tokens=(
            _positive_int(command.generation_params.get("max_tokens"))
            or (
                _positive_int(plan.max_output_tokens)
                if plan is not None
                else None
            )
            or _FALLBACK_OUTPUT_TOKENS
        ),
    )


def _adherence_budget(
    command: OutlineAdherenceCommand,
) -> CapabilityBudget:
    plan = _try_structured_plan(
        command,
        WorkflowStepTarget(
            PROSE_REMEDIATION_WORKFLOW,
            OUTLINE_ADHERENCE_STEP,
        )
    )
    return CapabilityBudget(
        max_paid_attempts=(
            int(plan.max_semantic_attempts)
            if plan is not None
            else _STRUCTURED_FALLBACK_ATTEMPTS
        ),
        max_output_tokens=(
            _positive_int(command.generation_params.get("max_tokens"))
            or (
                _positive_int(plan.max_output_tokens)
                if plan is not None
                else None
            )
            or _FALLBACK_OUTPUT_TOKENS
        ),
    )


def _chapter_audit(result: ChapterGenerationResult) -> dict[str, Any]:
    return {
        "stage": result.stage.value,
        "accepted": result.accepted,
        "usage": dict(result.usage),
        "attempt_count": len(result.attempts),
    }


def _contextual_budget(estimator):
    def estimate(command, _context, _call):
        return estimator(command)

    return estimate


def build_chapter_capability_registry(
    *,
    service_factory: Callable[[], ChapterGenerationApplicationService],
    outline_budget_estimator: Callable[
        [OutlineGenerationCommand], CapabilityBudget
    ] = _outline_budget,
    prose_budget_estimator: Callable[
        [ProseGenerationCommand], CapabilityBudget
    ] = _prose_budget,
    state_budget_estimator: Callable[
        [StateGenerationCommand], CapabilityBudget
    ] = _state_budget,
    adherence_budget_estimator: Callable[
        [OutlineAdherenceCommand], CapabilityBudget
    ] = _adherence_budget,
) -> CapabilityRegistry:
    """Bind production or fake chapter adapters to the canonical contract."""

    async def prepare_context(
        command: (
            OutlineGenerationCommand
            | OutlineAdherenceCommand
            | ProseGenerationCommand
            | StateGenerationCommand
        ),
        _call: CapabilityCall,
    ) -> PreparedChapterCapability:
        service = service_factory()
        return PreparedChapterCapability(
            service=service,
            value=await service.prepare(command),
        )

    return CapabilityRegistry(
        (
            CapabilityDefinition(
                capability="chapter_outline",
                version=2,
                label="章节策划",
                description="生成受卷纲与故事状态约束的章节细纲。",
                customizable=False,
                scope_options=("chapter",),
                input_schema=OutlineGenerationCommand,
                output_schema=ChapterGenerationResult,
                context_provider=ContextProvider(
                    policy_id="chapter_context:hard_contracts",
                    provide=prepare_context,
                ),
                handler=CapabilityHandler(
                    execute=_execute_chapter_generation,
                    stream=_stream_chapter_generation,
                ),
                side_effect_policy=SideEffectPolicy.ACCEPT_REQUIRED,
                allowed_tools=(),
                budget_estimator=_contextual_budget(
                    outline_budget_estimator
                ),
                revision_policy=RevisionPolicy.RECHECK_BEFORE_ACCEPT,
                audit_projector=_chapter_audit,
                event_schema=ChapterGenerationEvent,
            ),
            CapabilityDefinition(
                capability="chapter_prose",
                version=1,
                label="章节执笔",
                description="依据已接受细纲生成章节正文。",
                customizable=False,
                scope_options=("chapter",),
                input_schema=ProseGenerationCommand,
                output_schema=ChapterGenerationResult,
                context_provider=ContextProvider(
                    policy_id="chapter_context:hard_contracts",
                    provide=prepare_context,
                ),
                handler=CapabilityHandler(
                    execute=_execute_chapter_generation,
                    stream=_stream_chapter_generation,
                ),
                side_effect_policy=SideEffectPolicy.SYSTEM_WRITE,
                allowed_tools=(),
                budget_estimator=_contextual_budget(
                    prose_budget_estimator
                ),
                revision_policy=RevisionPolicy.SYSTEM_WRITE_ADVANCES,
                audit_projector=_chapter_audit,
                event_schema=ChapterGenerationEvent,
            ),
            CapabilityDefinition(
                capability="chapter_state",
                version=1,
                label="状态提取",
                description="从正文提取人物状态、永久事实和伏笔变更。",
                customizable=False,
                scope_options=("chapter",),
                input_schema=StateGenerationCommand,
                output_schema=ChapterGenerationResult,
                context_provider=ContextProvider(
                    policy_id="chapter_context:state_evidence",
                    provide=prepare_context,
                ),
                handler=CapabilityHandler(
                    execute=_execute_chapter_generation,
                    stream=_stream_chapter_generation,
                ),
                side_effect_policy=SideEffectPolicy.ACCEPT_REQUIRED,
                allowed_tools=(),
                budget_estimator=_contextual_budget(
                    state_budget_estimator
                ),
                revision_policy=RevisionPolicy.RECHECK_BEFORE_ACCEPT,
                audit_projector=_chapter_audit,
                event_schema=ChapterGenerationEvent,
            ),
            CapabilityDefinition(
                capability="chapter_outline_adherence",
                version=4,
                label="章节细纲符合度",
                description="检查正文候选是否兑现已接受的章节细纲。",
                customizable=False,
                scope_options=("chapter",),
                input_schema=OutlineAdherenceCommand,
                output_schema=ChapterGenerationResult,
                context_provider=ContextProvider(
                    policy_id="chapter_context:outline_adherence",
                    provide=prepare_context,
                ),
                handler=CapabilityHandler(
                    execute=_execute_chapter_generation,
                    stream=_stream_chapter_generation,
                ),
                side_effect_policy=SideEffectPolicy.PREVIEW_ONLY,
                allowed_tools=(),
                budget_estimator=_contextual_budget(
                    adherence_budget_estimator
                ),
                revision_policy=RevisionPolicy.READ_ONLY,
                audit_projector=_chapter_audit,
                event_schema=ChapterGenerationEvent,
            ),
        )
    )
