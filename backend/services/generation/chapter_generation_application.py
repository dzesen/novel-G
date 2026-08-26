"""章节生成的统一应用层入口。

本模块拥有章节级生成的上下文、提示词、运行时、结果清洗与接受权限。HTTP 路由
和批量作业只负责把各自的输入翻译成命令，并消费同一组类型化事件；二者不得再
各自装配一套 LLM 工作流。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field as ModelField,
    StrictInt,
    field_validator,
)

from backend.db.repositories.chapter_repository import chapter_repo
from backend.services.llm.pre_dispatch_boundaries import (
    pre_dispatch_boundary_code,
    restore_pre_dispatch_boundary,
)
from backend.db.repositories.novel_repository import novel_repo
from backend.db.repositories.prose_run_repository import prose_run_repo
from backend.db.utils import to_object_id
from backend.llm.config import get_llm_config, get_provider_config
from backend.llm.prompts.prompt_selector import (
    CHAPTER_OUTLINE_PROMPT_NAME,
    CHAPTER_STATE_PROMPT_NAME,
    OUTLINE_ADHERENCE_PROMPT_NAME,
    PROSE_PROMPT_NAME,
    load_prompt_config,
)
from backend.llm.schemas.novel_pydantic import (
    ChapterOutlineAdherenceEvidenceV4Schema,
    ChapterOutlineAdherenceResultSchema,
    ChapterOutlineResultSchema,
    ChapterStateResultSchema,
)
from backend.llm.models import TokenUsage
from backend.llm.stream_terminal import INCOMPLETE_FINISH_REASONS
from backend.services.generation.candidate_repair_contracts import (
    JobMutationRecoveryBindingV1,
    project_state_context,
)
from backend.services.generation.cancellation_cleanup import (
    drain_cancellation_cleanup,
)
from backend.services.generation.prose_completion import prose_completion_module
from backend.services.generation.prose_continuation import (
    ProseContinuationPolicy,
    authorization_ruleset_requires_refresh,
)
from backend.services.generation.prose_generation import (
    ProseContinuationLimit,
    execute_prose_plan,
)
from backend.services.generation.prose_readiness import (
    build_prose_readiness,
    validate_prose_readiness,
)
from backend.services.generation.prose_run_attempt_scope import (
    ProseRunAttemptScope,
)
from backend.services.generation.prose_runs import prose_run_module
from backend.services.generation.protected_generation_params import (
    validate_protected_generation_params,
)
from backend.services.generation.outline_adherence import (
    OUTLINE_ADHERENCE_SYSTEM_PROMPT,
    assess_outline_adherence_evidence,
    normalize_outline_adherence,
)
from backend.scene_contract_versions import (
    MAX_V2_OUTLINE_RESPONSE_UTF8_BYTES,
    OUTLINE_ADHERENCE_EVIDENCE_VERSION,
    SCENE_TRANSITION_CONTRACT_VERSION,
    require_known_scene_contract_version,
)
from backend.services.llm.context_builder import (
    assemble_context,
    assemble_outline_context,
    bind_context_lineage,
    estimate_tokens,
    fetch_context_inputs,
    outline_selection_roster,
)
from backend.services.llm.generation_runtime import (
    AttemptScope,
    GenerationPlan,
    PromptPlan,
    WorkflowStepTarget,
    create_generation_runtime,
)
from backend.services.llm.agent_orchestrator import apply_agent_profile
from backend.services.llm.prose_runner import stream_prose
from backend.services.llm.workflow_service import (
    get_llm_service_for_step,
    resolve_provider_for_step,
)
from backend.services.llm.outline_generation import (
    chapter_outline_generation_kwargs,
)
from backend.services.llm.workflow_runner import (
    WorkflowDeps,
    WorkflowFailed,
    WorkflowStep,
    parse_sse_event,
    run_workflow,
)
from backend.services.novel.chapter_service import ChapterService
from backend.services.novel.legacy_chapter_completion import (
    LegacyChapterCompletionProof,
    verify_legacy_chapter_completion_for_state,
)
from backend.services.novel.state_completion import (
    chapter_content_digest,
    prose_is_eligible_for_state,
    prose_acceptance_state,
)
from backend.services.novel.state_proposal import (
    FactAccountingPolicy,
    state_proposal_module,
)
from backend.services.novel.outline_validation import validate_outline_ids
from backend.services.novel.state_validation import (
    resolve_outline_character_references,
)
from backend.services.generation.state_repair_contracts import (
    StateRepairDirective,
)
from backend.services.novel.style_controls import render_style_controls


CHAPTER_OUTLINE_WORKFLOW = "create_chapter_outline_by_ai"
CHAPTER_OUTLINE_STEP = "chapter_outline"
PROSE_WORKFLOW = "write_chapter_by_ai"
PROSE_STEP = "chapter_content"
STATE_WORKFLOW = "extract_chapter_state_by_ai"
STATE_STEP = "chapter_state"
PROSE_REMEDIATION_WORKFLOW = "remediate_chapter_prose_by_agent"
OUTLINE_ADHERENCE_STEP = "outline_adherence"

CHAPTER_OUTLINE_STEPS: tuple[WorkflowStep, ...] = (
    WorkflowStep(
        key=CHAPTER_OUTLINE_STEP,
        schema=ChapterOutlineResultSchema,
        agent_id="chapter_planner",
        max_structured_raw_output_bytes=(
            MAX_V2_OUTLINE_RESPONSE_UTF8_BYTES
        ),
        prompt_args=lambda ctx: {
            "context": ctx.params["context"],
            "chapter_order": ctx.params["chapter_order"],
            "chapter_title": ctx.params["chapter_title"],
            "style_controls": ctx.params["style_controls"],
            "words_per_chapter": ctx.params["words_per_chapter"],
        },
    ),
)

CHAPTER_STATE_STEPS: tuple[WorkflowStep, ...] = (
    WorkflowStep(
        key=STATE_STEP,
        schema=ChapterStateResultSchema,
        agent_id="continuity_editor",
        prompt_args=lambda ctx: {
            "context": ctx.params["context"],
            "chapter_id": ctx.params["chapter_id"],
            "chapter_order": ctx.params["chapter_order"],
            "chapter_title": ctx.params["chapter_title"],
            "chapter_content": ctx.params["chapter_content"],
        },
    ),
)

_GENERATION_OVERRIDE_KEYS = frozenset(
    {
        "temperature",
        "top_p",
        "max_tokens",
        "presence_penalty",
        "frequency_penalty",
    }
)


def _validate_frozen_structured_plan(
    plan: GenerationPlan,
    *,
    workflow: str,
    step: str,
) -> GenerationPlan:
    target = WorkflowStepTarget(workflow, step)
    if not isinstance(plan, GenerationPlan) or plan.target != target:
        raise ValueError("冻结的结构化生成计划与章节步骤不匹配")
    return plan


def _validate_frozen_text_plan(
    plan: GenerationPlan,
    *,
    workflow: str,
    step: str,
) -> GenerationPlan:
    target = WorkflowStepTarget(workflow, step)
    if (
        not isinstance(plan, GenerationPlan)
        or plan.target != target
        or plan.reviewer_alias is not None
    ):
        raise ValueError("冻结的文本生成计划与章节步骤不匹配")
    return plan


class AcceptanceAuthority(str, Enum):
    """调用方获准执行的落库级别。"""

    PREVIEW = "preview"
    SYSTEM = "system"


class AcceptanceTiming(str, Enum):
    """决定已通过生成闸门的候选何时进入正式数据。"""

    IMMEDIATE = "immediate"
    DEFERRED = "deferred"


class ProseCandidateSource(BaseModel):
    """供后续只读检查使用的、可追溯到 ProseRun 的正文候选。"""

    model_config = ConfigDict(frozen=True)

    text: str = ModelField(min_length=1)
    source_run_id: str = ModelField(min_length=1)
    source_run_revision: int = ModelField(ge=0)
    source_content_digest: str = ModelField(min_length=64, max_length=64)
    completion: Mapping[str, Any]


class StateRepairGuidance(StateRepairDirective):
    """Bounded, metadata-only guidance for one state-candidate regeneration."""

    schema_version: Literal["state_repair_guidance.v1"] = (
        "state_repair_guidance.v1"
    )
    prior_proposal_id: str = ModelField(min_length=1, max_length=128)


def _render_state_repair_guidance(guidance: StateRepairGuidance) -> str:
    return (
        "【本轮状态候选修复约束】\n"
        "仅重新提取候选，不写入正式状态；不得猜测或创建任何内部 ID。\n"
        f"原因：{','.join(guidance.reason_codes)}\n"
        f"冲突数量：{guidance.consistency_issue_count}\n"
        f"无效引用数量：{guidance.dropped_reference_count}\n"
        f"未核算正式事实数量：{guidance.unaccounted_canonical_fact_count}\n"
        f"非法内部 ID 数量：{guidance.invalid_internal_reference_count}\n"
        f"悬空动作引用数量：{guidance.dangling_reference_count}\n"
        f"抽取失败数量：{guidance.extraction_failure_count}\n"
        "需重点核对的已声明 card_id："
        + (",".join(guidance.affected_card_ids) or "无")
    )


class ChapterGenerationStage(str, Enum):
    OUTLINE = "outline"
    PROSE = "prose"
    OUTLINE_ADHERENCE = "outline_adherence"
    STATE = "state"


class PartialProseRequiresCompletion(ValueError):
    """正文只接受了部分 AI 结果，状态回填必须硬暂停。"""


class ProseCompletionProofRequired(ValueError):
    """正式正文缺少 manual、V2 certificate 或冻结 legacy 证明。"""


class _ChapterGenerationCommand(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)


_NarrativeRevision = Annotated[
    StrictInt,
    ModelField(ge=0, le=9_223_372_036_854_775_807),
]


class OutlineGenerationCommand(_ChapterGenerationCommand):
    novel_id: str
    chapter_id: str
    authority: AcceptanceAuthority = AcceptanceAuthority.PREVIEW
    generation_params: Mapping[str, Any] = ModelField(default_factory=dict)
    attempt_scope: Any | None = None
    generation_plan: GenerationPlan | None = None
    expected_narrative_revision: _NarrativeRevision | None = None
    mutation_idempotency_key: str | None = ModelField(
        default=None,
        min_length=1,
        max_length=240,
    )
    job_mutation_binding: JobMutationRecoveryBindingV1 | None = None
    request_id: str | None = None
    is_disconnected: Callable[[], Awaitable[bool]] | None = None

    @field_validator("mutation_idempotency_key")
    @classmethod
    def validate_mutation_idempotency_key(cls, value: str | None) -> str | None:
        if value is not None and value != value.strip():
            raise ValueError("outline mutation idempotency key is invalid")
        return value


class ProseGenerationCommand(_ChapterGenerationCommand):
    novel_id: str
    chapter_id: str
    authority: AcceptanceAuthority = AcceptanceAuthority.PREVIEW
    acceptance_timing: AcceptanceTiming = AcceptanceTiming.IMMEDIATE
    owner_id: str | None = None
    generation_params: Mapping[str, Any] = ModelField(default_factory=dict)
    attempt_scope: Any | None = None
    generation_plan: GenerationPlan | None = None
    generation_job_id: str | None = ModelField(
        default=None,
        pattern=r"^[0-9a-f]{24}$",
    )
    resume_run_id: str | None = None
    expected_run_revision: int | None = None
    confirm_uncertain_retry: bool = False
    continuation_policy: ProseContinuationPolicy | Mapping[str, Any] | None = None
    readiness_digest: str | None = None
    confirm_automatic_continuations: bool = False
    token_budget: int | None = None
    request_id: str | None = None
    is_disconnected: Callable[[], Awaitable[bool]] | None = None


class OutlineAdherenceCommand(_ChapterGenerationCommand):
    novel_id: str
    chapter_id: str
    generation_params: Mapping[str, Any] = ModelField(default_factory=dict)
    attempt_scope: Any | None = None
    prose_candidate: ProseCandidateSource | None = None
    generation_plan: GenerationPlan | None = None


class StateGenerationCommand(_ChapterGenerationCommand):
    novel_id: str
    chapter_id: str
    authority: AcceptanceAuthority = AcceptanceAuthority.PREVIEW
    acceptance_timing: AcceptanceTiming = AcceptanceTiming.IMMEDIATE
    generation_params: Mapping[str, Any] = ModelField(default_factory=dict)
    attempt_scope: Any | None = None
    prose_candidate: ProseCandidateSource | None = None
    generation_plan: GenerationPlan | None = None
    repair_guidance: StateRepairGuidance | None = None
    job_mutation_binding: JobMutationRecoveryBindingV1 | None = None
    request_id: str | None = None
    is_disconnected: Callable[[], Awaitable[bool]] | None = None


class ChapterGenerationResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    stage: ChapterGenerationStage
    value: Any
    usage: dict[str, Any]
    attempts: list[dict[str, Any]]
    truncation: dict[str, Any]
    dropped: dict[str, Any] = ModelField(default_factory=dict)
    remapped: list[dict[str, Any]] = ModelField(default_factory=list)
    completion: dict[str, Any] = ModelField(default_factory=dict)
    acceptance: dict[str, Any] = ModelField(default_factory=dict)
    accepted: bool = False

    @property
    def total_tokens(self) -> int:
        return int(self.usage.get("total_tokens") or 0)


class ChapterGenerationEvent(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    name: str
    data: dict[str, Any]
    result: ChapterGenerationResult | None = None


@dataclass(frozen=True)
class ChapterGenerationApplicationDeps:
    novel_repo: Any
    chapter_repo: Any
    fetch_context_inputs: Callable[[str, str], Awaitable[dict[str, Any]]]
    assemble_outline_context: Callable[[dict[str, Any]], Any]
    create_runtime: Callable[..., Any]
    run_workflow: Callable[..., AsyncIterator[str]]
    load_prompts: Callable[[], dict[str, Any]]
    accept_outline: Callable[[str, dict[str, Any]], Awaitable[Any]]
    log_partial_on_disconnect: bool = False
    assemble_context: Callable[[dict[str, Any]], Any] = assemble_context
    get_legacy_service: Callable[[str, str], Any] = get_llm_service_for_step
    stream_prose: Callable[..., AsyncIterator[str]] = stream_prose
    prose_completion: Any = prose_completion_module
    execute_prose_plan: Callable[..., Awaitable[Any]] = execute_prose_plan
    prose_runs: Any = prose_run_module
    prose_run_repo: Any = prose_run_repo
    create_prose_attempt_scope: Callable[..., Any] = ProseRunAttemptScope
    state_proposals: Any = state_proposal_module
    resolve_provider: Callable[[str, str], str] = resolve_provider_for_step
    get_provider_config: Callable[[str], Any] = get_provider_config
    estimate_tokens: Callable[[str], int] = estimate_tokens
    verify_legacy_completion: Callable[..., Awaitable[Any]] = (
        verify_legacy_chapter_completion_for_state
    )

    @classmethod
    def production(cls) -> "ChapterGenerationApplicationDeps":
        return cls(
            novel_repo=novel_repo,
            chapter_repo=chapter_repo,
            fetch_context_inputs=fetch_context_inputs,
            assemble_outline_context=assemble_outline_context,
            create_runtime=create_generation_runtime,
            run_workflow=run_workflow,
            load_prompts=load_prompt_config,
            accept_outline=ChapterService.accept_chapter_outline,
            log_partial_on_disconnect=(
                get_llm_config().log_partial_result_on_disconnect
            ),
            assemble_context=assemble_context,
            get_legacy_service=get_llm_service_for_step,
            stream_prose=stream_prose,
            prose_completion=prose_completion_module,
            execute_prose_plan=execute_prose_plan,
            prose_runs=prose_run_module,
            prose_run_repo=prose_run_repo,
            create_prose_attempt_scope=ProseRunAttemptScope,
            state_proposals=state_proposal_module,
            resolve_provider=resolve_provider_for_step,
            get_provider_config=get_provider_config,
            estimate_tokens=estimate_tokens,
            verify_legacy_completion=(
                verify_legacy_chapter_completion_for_state
            ),
        )


@dataclass(frozen=True)
class _PreparedOutline:
    command: OutlineGenerationCommand
    params: dict[str, Any]
    gen_kwargs: dict[str, Any]
    roster: dict[str, Any]
    runtime: Any
    plan: GenerationPlan | None
    truncation: dict[str, Any]


@dataclass(frozen=True)
class _PreparedProse:
    command: ProseGenerationCommand
    novel: dict[str, Any]
    chapter: dict[str, Any]
    context: Any
    context_lineage: dict[str, Any] | None
    prompt: str
    gen_kwargs: dict[str, Any]
    policy: ProseContinuationPolicy
    runtime: Any
    plan: Any
    legacy_service: Any
    execution_plan: Any
    authorization: dict[str, Any] | None
    owner_id: str | None
    resume_run_id: str | None
    expected_run_revision: int | None
    truncation: dict[str, Any]


@dataclass(frozen=True)
class _PreparedOutlineAdherence:
    command: OutlineAdherenceCommand
    chapter: dict[str, Any]
    context: Any
    prompt_plan: PromptPlan
    gen_kwargs: dict[str, Any]
    runtime: Any
    plan: Any
    truncation: dict[str, Any]
    prose: str
    outline: dict[str, Any]
    result_schema: type[BaseModel]


@dataclass(frozen=True)
class _PreparedState:
    command: StateGenerationCommand
    chapter: dict[str, Any]
    context: Any
    roster: dict[str, Any]
    lease: Any
    params: dict[str, Any]
    gen_kwargs: dict[str, Any]
    runtime: Any
    plan: GenerationPlan | None
    truncation: dict[str, Any]


ChapterGenerationPrepared = (
    _PreparedOutline
    | _PreparedProse
    | _PreparedOutlineAdherence
    | _PreparedState
)


def _has_zero_pre_dispatch_evidence(data: Mapping[str, Any]) -> bool:
    usage = data.get("usage_so_far")
    attempts = data.get("attempts")
    return bool(
        isinstance(usage, Mapping)
        and set(usage) == {
            "input_tokens",
            "output_tokens",
            "total_tokens",
        }
        and all(type(usage[key]) is int and usage[key] == 0 for key in usage)
        and isinstance(attempts, list)
        and not attempts
    )


def _is_reported_incomplete_terminal(data: Mapping[str, Any]) -> bool:
    """Distinguish an exhausted Provider stream from a raised stream error.

    ``stream_prose`` reports terminal finish reasons such as ``length`` or
    ``error`` with a complete usage receipt and ``completion_status``.  Those
    frames must reach the scene executor, which persists the draft and applies
    its continuation/pause policy.  Raised transport/provider exceptions use
    ``usage_so_far`` instead and still become ``WorkflowFailed`` below.
    """

    return bool(
        data.get("completion_status") == "incomplete"
        and data.get("finish_reason")
        in INCOMPLETE_FINISH_REASONS
        and isinstance(data.get("usage"), Mapping)
        and "usage_so_far" not in data
    )


async def _stream_prose_deltas(
    frames: AsyncIterator[str],
) -> AsyncIterator[str]:
    """Translate trusted prose SSE frames back into the typed application seam."""

    emitted_generated_text = False
    async for frame in frames:
        parsed = parse_sse_event(frame)
        if parsed is None:
            continue
        name, data = parsed
        if name == "delta" and data.get("text"):
            emitted_generated_text = True
            yield str(data["text"])
            continue
        if name != "done" or data.get("success"):
            continue
        if _is_reported_incomplete_terminal(data):
            continue
        boundary = (
            restore_pre_dispatch_boundary(
                data.get("error_code"),
                data.get("error"),
            )
            if not emitted_generated_text and _has_zero_pre_dispatch_evidence(data)
            else None
        )
        if boundary is not None:
            raise boundary
        raise WorkflowFailed(
            data.get("error") or "prose generation failed",
            usage=data.get("usage_so_far"),
            attempts=data.get("attempts"),
        )


class ChapterGenerationApplicationService:
    """所有章节生成消费者共用的深接口。"""

    def __init__(
        self,
        deps: ChapterGenerationApplicationDeps | None = None,
    ) -> None:
        self._deps = deps or ChapterGenerationApplicationDeps.production()

    async def execute(
        self,
        command: (
            OutlineGenerationCommand
            | ProseGenerationCommand
            | OutlineAdherenceCommand
            | StateGenerationCommand
        ),
    ) -> AsyncIterator[ChapterGenerationEvent]:
        """预检命令并返回事件流；预检错误发生在 HTTP 开流之前。"""

        prepared = await self.prepare(command)
        return await self.execute_prepared(prepared)

    async def prepare(
        self,
        command: (
            OutlineGenerationCommand
            | ProseGenerationCommand
            | OutlineAdherenceCommand
            | StateGenerationCommand
        ),
    ) -> ChapterGenerationPrepared:
        """Resolve the real chapter context before a capability is dispatched."""

        validate_protected_generation_params(command.generation_params)

        if isinstance(command, OutlineGenerationCommand):
            return await self._prepare_outline(command)
        if isinstance(command, ProseGenerationCommand):
            return await self._prepare_prose(command)
        if isinstance(command, OutlineAdherenceCommand):
            return await self._prepare_outline_adherence(command)
        if isinstance(command, StateGenerationCommand):
            return await self._prepare_state(command)
        raise TypeError(f"unsupported chapter generation command: {type(command)!r}")

    async def execute_prepared(
        self,
        prepared: ChapterGenerationPrepared,
    ) -> AsyncIterator[ChapterGenerationEvent]:
        """Execute an already resolved context without reading it again."""

        if isinstance(prepared, _PreparedOutline):
            return self._stream_outline(prepared)
        if isinstance(prepared, _PreparedProse):
            return self._stream_prose(prepared)
        if isinstance(prepared, _PreparedOutlineAdherence):
            return self._stream_outline_adherence(prepared)
        if isinstance(prepared, _PreparedState):
            return self._stream_state(prepared)
        raise TypeError(
            f"unsupported prepared chapter context: {type(prepared)!r}"
        )

    async def collect(
        self,
        command: (
            OutlineGenerationCommand
            | ProseGenerationCommand
            | OutlineAdherenceCommand
            | StateGenerationCommand
        ),
    ) -> ChapterGenerationResult:
        """无头消费同一事件流，并把失败终帧恢复成带审计信息的异常。"""

        prepared = await self.prepare(command)
        return await self.collect_prepared(prepared)

    async def collect_prepared(
        self,
        prepared: ChapterGenerationPrepared,
    ) -> ChapterGenerationResult:
        """Collect a prepared execution without rebuilding mutable context."""

        execution = await self.execute_prepared(prepared)
        failure: dict[str, Any] | None = None
        async for event in execution:
            if event.result is not None:
                return event.result
            if event.name == "done" and not event.data.get("success"):
                failure = event.data
        if failure is not None:
            raise WorkflowFailed(
                failure.get("error")
                or f"workflow failed at {failure.get('failed_step')}",
                usage=failure.get("usage") or failure.get("usage_so_far"),
                attempts=failure.get("attempts"),
                diagnostics=failure.get("diagnostics"),
            )
        raise WorkflowFailed("chapter generation ended without a successful result")

    async def _prepare_outline(
        self,
        command: OutlineGenerationCommand,
    ) -> _PreparedOutline:
        binding = command.job_mutation_binding
        if binding is not None:
            frozen_binding = JobMutationRecoveryBindingV1.model_validate(
                binding.model_dump(mode="python")
            )
            if (
                command.authority is not AcceptanceAuthority.SYSTEM
                or frozen_binding.operation != "accept_chapter_outline"
                or frozen_binding.novel_id != command.novel_id
                or frozen_binding.chapter_id != command.chapter_id
                or frozen_binding.expected_narrative_revision
                != command.expected_narrative_revision
                or frozen_binding.idempotency_key
                != command.mutation_idempotency_key
            ):
                raise ValueError("章纲 mutation 的 Job 授权绑定无效")
        novel = await self._deps.novel_repo.get_novel_by_id(command.novel_id)
        chapter = await self._deps.chapter_repo.get_chapter_by_id(
            command.chapter_id
        )
        if chapter.get("novel_id") != to_object_id(command.novel_id):
            raise ValueError("该章节不属于指定小说")

        inputs = await self._deps.fetch_context_inputs(
            command.novel_id,
            command.chapter_id,
        )
        context = self._deps.assemble_outline_context(inputs)
        roster = outline_selection_roster(
            inputs["roster"],
            context.selectable_worldbook_card_ids,
        )
        generation_values = dict(command.generation_params or {})
        gen_kwargs = {
            key: value
            for key, value in generation_values.items()
            if key in _GENERATION_OVERRIDE_KEYS and value is not None
        }
        runtime_kwargs = (
            {}
            if generation_values.get("allow_failure_retry", True)
            else {"max_provider_retries": 0}
        )
        runtime = self._deps.create_runtime(
            attempt_scope=command.attempt_scope,
            **runtime_kwargs,
        )
        plan = (
            _validate_frozen_structured_plan(
                command.generation_plan,
                workflow=CHAPTER_OUTLINE_WORKFLOW,
                step=CHAPTER_OUTLINE_STEP,
            )
            if command.generation_plan is not None
            else None
        )
        return _PreparedOutline(
            command=command,
            params={
                "context": context.to_prompt_text(),
                "chapter_order": int(chapter.get("order_index") or 0),
                "chapter_title": str(chapter.get("title") or ""),
                "style_controls": render_style_controls(
                    novel.get("style_controls")
                ),
                "words_per_chapter": novel.get("words_per_chapter") or 3000,
            },
            gen_kwargs=chapter_outline_generation_kwargs(gen_kwargs),
            roster=roster,
            runtime=runtime,
            plan=plan,
            truncation={
                "truncated_sections": list(context.truncated_sections),
                "dropped_item_counts": dict(context.dropped_item_counts),
            },
        )

    async def _stream_outline(
        self,
        prepared: _PreparedOutline,
    ) -> AsyncIterator[ChapterGenerationEvent]:
        if any(prepared.truncation.values()):
            yield ChapterGenerationEvent(
                name="context",
                data=prepared.truncation,
            )

        command = prepared.command
        frames = self._deps.run_workflow(
            workflow_name=CHAPTER_OUTLINE_WORKFLOW,
            steps=CHAPTER_OUTLINE_STEPS,
            prompts=self._deps.load_prompts().get(
                CHAPTER_OUTLINE_PROMPT_NAME,
                {},
            ),
            params=prepared.params,
            gen_kwargs=prepared.gen_kwargs,
            cached={},
            deps=WorkflowDeps(
                runtime=prepared.runtime,
                structured_plans=(
                    {CHAPTER_OUTLINE_STEP: prepared.plan}
                    if prepared.plan is not None
                    else {}
                ),
            ),
            request_id=command.request_id or uuid4().hex[:8],
            is_disconnected=command.is_disconnected,
            log_partial_on_disconnect=self._deps.log_partial_on_disconnect,
        )
        reported_dropped = False
        reported_remapped = False
        final_dropped: dict[str, Any] = {}
        final_remapped: list[dict[str, Any]] = []

        async for frame in frames:
            parsed = parse_sse_event(frame)
            if parsed is None:
                yield ChapterGenerationEvent(name="keepalive", data={})
                continue
            name, raw_data = parsed
            data = dict(raw_data)
            outline = _extract_outline(name, data)
            if outline is None:
                yield ChapterGenerationEvent(name=name, data=data)
                continue

            resolved, remapped = resolve_outline_character_references(
                outline,
                prepared.roster,
            )
            cleaned, dropped = validate_outline_ids(resolved, prepared.roster)
            if remapped:
                final_remapped = list(remapped)
                if not reported_remapped:
                    yield ChapterGenerationEvent(
                        name="id_remapping",
                        data={"remapped": final_remapped},
                    )
                    reported_remapped = True
            if dropped:
                final_dropped = dict(dropped)
                if not reported_dropped:
                    yield ChapterGenerationEvent(
                        name="id_validation",
                        data={"dropped": final_dropped},
                    )
                    reported_dropped = True
            cleaned_data = _replace_outline(name, data, cleaned)
            if name != "done":
                yield ChapterGenerationEvent(name=name, data=cleaned_data)
                continue

            accepted = False
            if command.authority is AcceptanceAuthority.SYSTEM:
                if (
                    command.expected_narrative_revision is not None
                    or command.mutation_idempotency_key is not None
                ):
                    mutation_options: dict[str, Any] = {
                        "expected_narrative_revision": (
                            command.expected_narrative_revision
                        ),
                        "idempotency_key": command.mutation_idempotency_key,
                    }
                    if command.job_mutation_binding is not None:
                        mutation_options["job_mutation_binding"] = (
                            command.job_mutation_binding
                        )
                    await self._deps.accept_outline(
                        command.chapter_id,
                        cleaned,
                        **mutation_options,
                    )
                else:
                    await self._deps.accept_outline(command.chapter_id, cleaned)
                accepted = True
            usage = dict(cleaned_data.get("usage") or {})
            attempts = list(cleaned_data.get("attempts") or [])
            if not attempts:
                attempts = _serialize_attempts(prepared.runtime)
            result = ChapterGenerationResult(
                stage=ChapterGenerationStage.OUTLINE,
                value=cleaned,
                usage=usage,
                attempts=attempts,
                truncation=prepared.truncation,
                dropped=final_dropped,
                remapped=final_remapped,
                accepted=accepted,
            )
            yield ChapterGenerationEvent(
                name=name,
                data=cleaned_data,
                result=result,
            )

    async def _prepare_state(
        self,
        command: StateGenerationCommand,
    ) -> _PreparedState:
        lease = None
        try:
            await self._deps.novel_repo.get_novel_by_id(command.novel_id)
            chapter = await self._deps.chapter_repo.get_chapter_by_id(
                command.chapter_id
            )
            if chapter.get("novel_id") != to_object_id(command.novel_id):
                raise ValueError("该章节不属于指定小说")
            candidate = command.prose_candidate
            if command.repair_guidance is not None and candidate is None:
                raise ValueError("状态修复必须绑定精确正文候选")
            content = (
                candidate.text
                if candidate is not None
                else str(chapter.get("content") or "")
            ).strip()
            if not content:
                raise ValueError("本章还没有已保存的正文，请先写好并保存正文")
            if candidate is not None:
                self._validate_prose_candidate(candidate)
            elif prose_acceptance_state(chapter) == "partial_manual_required":
                raise PartialProseRequiresCompletion(
                    "本章正文只接受了部分 AI 结果；请先补写并将章节状态设为完成，"
                    "再执行状态回填"
                )
            legacy_proof: LegacyChapterCompletionProof | bool | None = None
            if candidate is None and not prose_is_eligible_for_state(chapter):
                if prose_acceptance_state(chapter) == "ai_complete":
                    legacy_proof = await self._deps.verify_legacy_completion(
                        novel_id=command.novel_id,
                        chapter_id=command.chapter_id,
                        chapter=chapter,
                    )
                if not legacy_proof:
                    raise ProseCompletionProofRequired(
                        "本章正文没有可验证的人工完成、V2 完成证书或冻结的旧版"
                        "完成证明，不能生成新的正式状态候选"
                    )

            acceptance = chapter.get("prose_acceptance")
            acceptance = acceptance if isinstance(acceptance, Mapping) else {}
            legacy_source_run_id = (
                legacy_proof.source_prose_run_id
                if isinstance(legacy_proof, LegacyChapterCompletionProof)
                else str(acceptance.get("source_run_id") or "") or None
            )
            legacy_source_run_revision = (
                legacy_proof.source_prose_run_revision
                if isinstance(legacy_proof, LegacyChapterCompletionProof)
                else None
            )

            snapshot = await self._deps.state_proposals.capture(
                command.novel_id,
                command.chapter_id,
                chapter=chapter,
                source_content_digest=(
                    candidate.source_content_digest
                    if candidate is not None
                    else chapter_content_digest(content)
                ),
                source_prose_run_id=(
                    candidate.source_run_id
                    if candidate is not None
                    else legacy_source_run_id
                ),
                source_prose_run_revision=(
                    candidate.source_run_revision
                    if candidate is not None
                    else legacy_source_run_revision
                ),
                source_prose_acceptance_state=(
                    "ai_complete"
                    if candidate is not None
                    else prose_acceptance_state(chapter)
                ),
            )
            binding = command.job_mutation_binding
            if binding is not None and (
                command.authority is not AcceptanceAuthority.SYSTEM
                or command.acceptance_timing is not AcceptanceTiming.IMMEDIATE
                or binding.operation != "accept_chapter_state"
                or binding.novel_id != command.novel_id
                or binding.chapter_id != command.chapter_id
                or binding.expected_narrative_revision
                != snapshot.narrative_revision
            ):
                raise ValueError("状态 mutation 的 Job 授权绑定无效")
            provider_alias = self._deps.resolve_provider(
                STATE_WORKFLOW,
                STATE_STEP,
            )
            provider_config = self._deps.get_provider_config(provider_alias)
            lease = await self._deps.state_proposals.begin(
                command.novel_id,
                command.chapter_id,
                snapshot=snapshot,
                audit={
                    "workflow": STATE_WORKFLOW,
                    "step": STATE_STEP,
                    "provider_alias": provider_alias,
                    "provider_type": getattr(
                        provider_config,
                        "provider_type",
                        None,
                    ),
                    "model": getattr(provider_config, "model", None),
                    "mode": command.authority.value,
                    **(
                        {"request_id": command.request_id}
                        if command.request_id
                        else {}
                    ),
                    **(
                        {
                            "job_mutation_binding": binding.model_dump(
                                mode="json"
                            )
                        }
                        if binding is not None
                        else {}
                    ),
                },
            )
            inputs = await self._deps.fetch_context_inputs(
                command.novel_id,
                command.chapter_id,
            )
            context = self._deps.assemble_context(inputs)
            context_text = context.to_prompt_text()
            await self._deps.state_proposals.record_pre_dispatch_projection(
                lease,
                project_state_context(
                    truncated_sections=context.truncated_sections,
                    dropped_item_counts=context.dropped_item_counts,
                ),
            )
            guidance = command.repair_guidance
            if guidance is not None:
                outline = chapter.get("outline")
                raw_declared_ids = (
                    outline.get("present_character_card_ids")
                    if isinstance(outline, Mapping)
                    else None
                )
                declared_ids = {
                    item
                    for item in (
                        raw_declared_ids
                        if isinstance(raw_declared_ids, list)
                        else []
                    )
                    if isinstance(item, str) and item
                }
                if not set(guidance.affected_card_ids) <= declared_ids:
                    raise ValueError(
                        "状态修复包含章细纲未声明的资料卡 ID"
                    )
                context_text = (
                    context_text
                    + "\n\n"
                    + _render_state_repair_guidance(guidance)
                )
            generation_values = dict(command.generation_params or {})
            reserved_output = int(
                generation_values.get("max_tokens")
                or getattr(provider_config, "max_tokens", None)
                or 4096
            )
            estimated_input = self._deps.estimate_tokens(
                context_text
            ) + self._deps.estimate_tokens(content)
            max_context_tokens = int(
                getattr(provider_config, "max_context_tokens", 128000)
            )
            if estimated_input + reserved_output > max_context_tokens:
                raise ValueError(
                    "本章正文与上下文预计超过模型窗口："
                    f"输入约 {estimated_input} tokens，输出预留 {reserved_output}，"
                    f"窗口 {max_context_tokens}。"
                    "请精简上下文或选择更大窗口的模型。"
                )
            await self._deps.state_proposals.ensure_current(snapshot)

            gen_kwargs = {
                key: value
                for key, value in generation_values.items()
                if key in _GENERATION_OVERRIDE_KEYS and value is not None
            }
            runtime_kwargs = (
                {}
                if generation_values.get("allow_failure_retry", True)
                else {"max_provider_retries": 0}
            )
            runtime = self._deps.create_runtime(
                attempt_scope=command.attempt_scope,
                **runtime_kwargs,
            )
            plan = (
                _validate_frozen_structured_plan(
                    command.generation_plan,
                    workflow=STATE_WORKFLOW,
                    step=STATE_STEP,
                )
                if command.generation_plan is not None
                else None
            )
            return _PreparedState(
                command=command,
                chapter=chapter,
                context=context,
                roster=inputs["roster"],
                lease=lease,
                params={
                    "context": context_text,
                    "chapter_id": command.chapter_id,
                    "chapter_order": int(chapter.get("order_index") or 0),
                    "chapter_title": str(chapter.get("title") or ""),
                    "chapter_content": content,
                },
                gen_kwargs=gen_kwargs,
                runtime=runtime,
                plan=plan,
                truncation={
                    "truncated_sections": list(context.truncated_sections),
                    "dropped_item_counts": dict(context.dropped_item_counts),
                },
            )
        except BaseException as exc:
            if lease is not None:
                await self._deps.state_proposals.mark_failed(lease, exc)
            raise

    async def _stream_state(
        self,
        prepared: _PreparedState,
    ) -> AsyncIterator[ChapterGenerationEvent]:
        if any(prepared.truncation.values()):
            yield ChapterGenerationEvent(
                name="context",
                data=prepared.truncation,
            )
        command = prepared.command
        await self._deps.state_proposals.mark_dispatched(prepared.lease)
        frames = self._deps.run_workflow(
            workflow_name=STATE_WORKFLOW,
            steps=CHAPTER_STATE_STEPS,
            prompts=self._deps.load_prompts().get(
                CHAPTER_STATE_PROMPT_NAME,
                {},
            ),
            params=prepared.params,
            gen_kwargs=prepared.gen_kwargs,
            cached={},
            deps=WorkflowDeps(
                runtime=prepared.runtime,
                structured_plans=(
                    {STATE_STEP: prepared.plan}
                    if prepared.plan is not None
                    else {}
                ),
            ),
            request_id=command.request_id or uuid4().hex[:8],
            is_disconnected=command.is_disconnected,
            log_partial_on_disconnect=self._deps.log_partial_on_disconnect,
        )
        preview = self._deps.state_proposals.stream_preview(
            prepared.lease,
            frames,
            roster=prepared.roster,
            prose=str(prepared.params["chapter_content"]),
            state_step=STATE_STEP,
        )
        dropped: dict[str, Any] = {}
        remapped: list[dict[str, Any]] = []
        async for frame in preview:
            parsed = parse_sse_event(frame)
            if parsed is None:
                yield ChapterGenerationEvent(name="keepalive", data={})
                continue
            name, raw_data = parsed
            data = dict(raw_data)
            if name == "id_validation":
                dropped = dict(data.get("dropped") or {})
                yield ChapterGenerationEvent(name=name, data=data)
                continue
            if name == "id_remapping":
                remapped = list(data.get("remapped") or [])
                yield ChapterGenerationEvent(name=name, data=data)
                continue
            proposal = _extract_state_proposal(name, data)
            if proposal is None:
                if name == "done" and not data.get("success"):
                    data.setdefault(
                        "attempts",
                        _serialize_attempts(prepared.runtime),
                    )
                yield ChapterGenerationEvent(name=name, data=data)
                continue

            if name != "done":
                yield ChapterGenerationEvent(name=name, data=data)
                continue
            acceptance: dict[str, Any] = {}
            accepted = False
            if (
                command.authority is AcceptanceAuthority.SYSTEM
                and command.acceptance_timing is AcceptanceTiming.IMMEDIATE
            ):
                acceptance = await self._deps.state_proposals.run_auto(
                    chapter_id=command.chapter_id,
                    proposal=proposal,
                    policy=FactAccountingPolicy(),
                    job_mutation_binding=command.job_mutation_binding,
                )
                accepted = True
            usage = dict(data.get("usage") or {})
            result = ChapterGenerationResult(
                stage=ChapterGenerationStage.STATE,
                value=proposal,
                usage=usage,
                attempts=_serialize_attempts(prepared.runtime),
                truncation=prepared.truncation,
                dropped=dropped,
                remapped=remapped,
                acceptance=dict(acceptance or {}),
                accepted=accepted,
            )
            yield ChapterGenerationEvent(
                name=name,
                data=data,
                result=result,
            )

    async def _prepare_outline_adherence(
        self,
        command: OutlineAdherenceCommand,
    ) -> _PreparedOutlineAdherence:
        chapter = await self._deps.chapter_repo.get_chapter_by_id(
            command.chapter_id
        )
        if chapter.get("novel_id") != to_object_id(command.novel_id):
            raise ValueError("该章节不属于指定小说")
        candidate = command.prose_candidate
        content = (
            candidate.text
            if candidate is not None
            else str(chapter.get("content") or "")
        )
        if candidate is not None:
            self._validate_prose_candidate(candidate)
        if not content.strip():
            raise ValueError("本章尚无可供细纲符合度检查的正文")
        if not chapter.get("outline"):
            raise ValueError("本章尚无可供细纲符合度检查的章节细纲")
        outline = dict(chapter["outline"])
        uses_versioned_evidence = require_known_scene_contract_version(outline) == (
            SCENE_TRANSITION_CONTRACT_VERSION
        )
        if uses_versioned_evidence and candidate is None:
            raise ValueError("版本化 beat 证据必须绑定精确正文候选")

        inputs = await self._deps.fetch_context_inputs(
            command.novel_id,
            command.chapter_id,
        )
        context = self._deps.assemble_context(inputs)
        prompts = self._deps.load_prompts().get(
            OUTLINE_ADHERENCE_PROMPT_NAME,
            {},
        )
        if uses_versioned_evidence and prompts.get("contract_version") != (
            OUTLINE_ADHERENCE_EVIDENCE_VERSION
        ):
            raise ValueError("V4 证据化审核提示词合同版本无效")
        prompt_base = prompts["outline_adherence_prompt_base"].format(
            context=context.to_prompt_text(),
            chapter_order=int(chapter.get("order_index") or 0),
            chapter_title=str(chapter.get("title") or ""),
            chapter_content=content,
        )
        with_schema_suffix = (
            "outline_adherence_v4_prompt_with_schema_suffix"
            if uses_versioned_evidence
            else "outline_adherence_prompt_with_schema_suffix"
        )
        without_schema_suffix = (
            "outline_adherence_v4_prompt_without_schema_suffix"
            if uses_versioned_evidence
            else "outline_adherence_prompt_without_schema_suffix"
        )
        prompt_plan = PromptPlan(
            native_schema_prompt=apply_agent_profile(
                "continuity_editor",
                prompt_base
                + "\n"
                + prompts[with_schema_suffix],
            ),
            prompt_json_prompt=apply_agent_profile(
                "continuity_editor",
                prompt_base
                + "\n"
                + prompts[without_schema_suffix],
            ),
        )
        generation_values = dict(command.generation_params or {})
        gen_kwargs = {
            key: value
            for key, value in generation_values.items()
            if key in _GENERATION_OVERRIDE_KEYS and value is not None
        }
        gen_kwargs["system_prompt"] = OUTLINE_ADHERENCE_SYSTEM_PROMPT
        runtime_kwargs = (
            {}
            if generation_values.get("allow_failure_retry", True)
            else {"max_provider_retries": 0}
        )
        runtime = self._deps.create_runtime(
            attempt_scope=command.attempt_scope,
            **runtime_kwargs,
        )
        plan = (
            _validate_frozen_structured_plan(
                command.generation_plan,
                workflow=PROSE_REMEDIATION_WORKFLOW,
                step=OUTLINE_ADHERENCE_STEP,
            )
            if command.generation_plan is not None
            else runtime.plan_structured(
                WorkflowStepTarget(
                    PROSE_REMEDIATION_WORKFLOW,
                    OUTLINE_ADHERENCE_STEP,
                )
            )
        )
        return _PreparedOutlineAdherence(
            command=command,
            chapter=chapter,
            context=context,
            prompt_plan=prompt_plan,
            gen_kwargs=gen_kwargs,
            runtime=runtime,
            plan=plan,
            truncation={
                "truncated_sections": list(context.truncated_sections),
                "dropped_item_counts": dict(context.dropped_item_counts),
            },
            prose=content,
            outline=outline,
            result_schema=(
                ChapterOutlineAdherenceEvidenceV4Schema
                if uses_versioned_evidence
                else ChapterOutlineAdherenceResultSchema
            ),
        )

    @staticmethod
    def _validate_prose_candidate(candidate: ProseCandidateSource) -> None:
        if chapter_content_digest(candidate.text) != candidate.source_content_digest:
            raise ValueError("正文候选摘要与候选内容不一致")
        completion = dict(candidate.completion or {})
        if (
            completion.get("can_write_formal_prose") is not True
            or str(completion.get("status") or "") != "complete"
        ):
            raise PartialProseRequiresCompletion(
                "正文候选尚未通过完整性闸门，不能用于后续检查"
            )

    async def _stream_outline_adherence(
        self,
        prepared: _PreparedOutlineAdherence,
    ) -> AsyncIterator[ChapterGenerationEvent]:
        if any(prepared.truncation.values()):
            yield ChapterGenerationEvent(
                name="context",
                data=prepared.truncation,
            )
        yield ChapterGenerationEvent(
            name="step",
            data={
                "step": "outline_adherence",
                "status": "running",
                "provider": getattr(prepared.plan, "provider_alias", ""),
                "model": getattr(prepared.plan, "provider_model", ""),
                "agent": "continuity_editor",
            },
        )
        try:
            generated = await prepared.runtime.generate_structured(
                prepared.plan,
                prepared.result_schema,
                prepared.prompt_plan,
                **prepared.gen_kwargs,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            usage = _runtime_usage(prepared.runtime)
            yield ChapterGenerationEvent(
                name="done",
                data={
                    "success": False,
                    "failed_step": "outline_adherence",
                    "error": str(exc),
                    "usage": usage,
                    "attempts": _serialize_attempts(prepared.runtime),
                },
            )
            return

        try:
            candidate = prepared.command.prose_candidate
            if prepared.result_schema is ChapterOutlineAdherenceEvidenceV4Schema:
                if candidate is None:  # guarded in prepare; fail-closed proof
                    raise ValueError("V4 符合度证据缺少正文候选绑定")
                review = assess_outline_adherence_evidence(
                    generated.value.model_dump(),
                    outline=prepared.outline,
                    prose=prepared.prose,
                    source_prose_run_id=candidate.source_run_id,
                    source_prose_run_revision=candidate.source_run_revision,
                    source_content_digest=candidate.source_content_digest,
                )
            else:
                review = normalize_outline_adherence(
                    generated.value.model_dump()
                )
            if (
                candidate is not None
                and prepared.result_schema
                is not ChapterOutlineAdherenceEvidenceV4Schema
            ):
                review = {
                    **review,
                    "source_prose_run_id": candidate.source_run_id,
                    "source_prose_run_revision": candidate.source_run_revision,
                    "source_content_digest": candidate.source_content_digest,
                }
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            usage = generated.usage.model_dump()
            yield ChapterGenerationEvent(
                name="done",
                data={
                    "success": False,
                    "failed_step": "outline_adherence",
                    "error": str(exc),
                    "usage": usage,
                    "attempts": _serialize_attempts(prepared.runtime),
                },
            )
            return
        usage = generated.usage.model_dump()
        attempts = _serialize_attempts(prepared.runtime)
        yield ChapterGenerationEvent(
            name="step",
            data={
                "step": "outline_adherence",
                "status": "done",
                "data": review,
                "usage": usage,
            },
        )
        result = ChapterGenerationResult(
            stage=ChapterGenerationStage.OUTLINE_ADHERENCE,
            value=review,
            usage=usage,
            attempts=attempts,
            truncation=prepared.truncation,
        )
        yield ChapterGenerationEvent(
            name="done",
            data={
                "success": True,
                "result": {"outline_adherence": review},
                "usage": usage,
            },
            result=result,
        )

    async def _prepare_prose(
        self,
        command: ProseGenerationCommand,
    ) -> _PreparedProse:
        novel = await self._deps.novel_repo.get_novel_by_id(command.novel_id)
        chapter = await self._deps.chapter_repo.get_chapter_by_id(
            command.chapter_id
        )
        if chapter.get("novel_id") != to_object_id(command.novel_id):
            raise ValueError("该章节不属于指定小说")
        outline = dict(chapter.get("outline") or {})
        if not outline:
            raise ValueError("本章还没有已接受的细纲，请先生成并接受章节细纲")
        scene_contract_version = require_known_scene_contract_version(outline)
        resumes_frozen_legacy_run = bool(
            command.resume_run_id
            and type(command.expected_run_revision) is int
            and command.expected_run_revision >= 0
        )
        if (
            scene_contract_version != SCENE_TRANSITION_CONTRACT_VERSION
            and not resumes_frozen_legacy_run
        ):
            raise ValueError(
                "旧版章纲不能启动新的 AI 正文；请重新生成并接受 V2 场景合同章纲"
            )

        inputs = await self._deps.fetch_context_inputs(
            command.novel_id,
            command.chapter_id,
        )
        context = self._deps.assemble_context(inputs)
        words_per_chapter = int(
            outline.get("target_word_count")
            or novel.get("words_per_chapter")
            or 3000
        )
        prompts = self._deps.load_prompts().get(PROSE_PROMPT_NAME, {})
        prompt = apply_agent_profile(
            "chapter_writer",
            prompts[f"{PROSE_STEP}_prompt_base"].format(
                context=context.to_prompt_text(),
                chapter_order=int(chapter.get("order_index") or 0),
                chapter_title=str(chapter.get("title") or ""),
                style_controls=render_style_controls(
                    novel.get("style_controls")
                ),
                words_per_chapter=words_per_chapter,
            )
            + "\n"
            + prompts[f"{PROSE_STEP}_prompt_without_schema_suffix"],
        )
        generation_values = dict(command.generation_params or {})
        gen_kwargs = {
            key: value
            for key, value in generation_values.items()
            if key in _GENERATION_OVERRIDE_KEYS and value is not None
        }
        runtime_kwargs = (
            {}
            if generation_values.get("allow_failure_retry", True)
            else {"max_provider_retries": 0}
        )
        runtime = self._deps.create_runtime(
            attempt_scope=command.attempt_scope,
            **runtime_kwargs,
        )
        legacy_service = None
        if command.generation_plan is not None:
            plan = _validate_frozen_text_plan(
                command.generation_plan,
                workflow=PROSE_WORKFLOW,
                step=PROSE_STEP,
            )
        else:
            try:
                plan = runtime.plan_text(
                    WorkflowStepTarget(PROSE_WORKFLOW, PROSE_STEP)
                )
            except ValueError:
                legacy_service = self._deps.get_legacy_service(
                    PROSE_WORKFLOW,
                    PROSE_STEP,
                )
                runtime = None
                plan = None

        policy_value = command.continuation_policy
        if policy_value is None:
            policy_value = generation_values.get("prose_continuation_policy")
        policy = (
            policy_value
            if isinstance(policy_value, ProseContinuationPolicy)
            else ProseContinuationPolicy.from_mapping(policy_value)
        )
        provider_capability = _provider_capability(plan)
        execution_plan = self._deps.prose_completion.plan(
            outline=outline,
            target_word_count=words_per_chapter,
            provider_capability=provider_capability,
            request_overrides=gen_kwargs,
        )
        owner_id = command.owner_id
        if command.authority is AcceptanceAuthority.SYSTEM and not owner_id:
            owner_id = str(novel.get("owner_id") or "") or None
        if command.authority is AcceptanceAuthority.SYSTEM and owner_id is None:
            raise ValueError("小说缺少 owner_id，无法创建用户隔离的正文草稿")

        authorization: dict[str, Any] | None = None
        if (
            command.authority is AcceptanceAuthority.PREVIEW
            and policy.permits_automatic_continuation
        ):
            if plan is None:
                raise ValueError("当前 Provider 无法完成正文自动续写预检")
            authorization_revision = await self._prose_authorization_revision(
                command=command,
                owner_id=owner_id,
                policy=policy,
            )
            readiness = build_prose_readiness(
                execution_plan=execution_plan,
                generation_plan=plan,
                policy=policy,
                token_budget=command.token_budget,
                authorization_revision=authorization_revision,
                novel_id=command.novel_id,
                chapter_id=command.chapter_id,
                outline=outline,
                context_text=context.to_prompt_text(),
                base_prompt=prompt,
                generation_kwargs=gen_kwargs,
            )
            validate_prose_readiness(
                readiness,
                supplied_digest=command.readiness_digest,
                confirmed_automatic_continuations=(
                    command.confirm_automatic_continuations
                ),
            )
            authorization = readiness.authorization.to_dict()

        resume_run_id = command.resume_run_id
        expected_run_revision = command.expected_run_revision
        if (
            command.authority is AcceptanceAuthority.SYSTEM
            and owner_id is not None
            and resume_run_id is None
        ):
            active = await self._deps.prose_runs.inspect_active(
                owner_id=owner_id,
                chapter_id=command.chapter_id,
                outline=outline,
                context_text=context.to_prompt_text(),
            )
            if active is not None:
                stored_protocol = str(
                    ((active.get("plan") or {}).get("protocol_revision") or "")
                )
                if (
                    stored_protocol != execution_plan.protocol_revision
                    or active.get("status") == "stale"
                ):
                    await self._deps.prose_run_repo.mark_status(
                        run_id=str(active["_id"]),
                        owner_id=owner_id,
                        status="stale",
                    )
                else:
                    resume_run_id = str(active["_id"])
                    expected_run_revision = int(active.get("revision") or 0)

        return _PreparedProse(
            command=command,
            novel=novel,
            chapter=chapter,
            context=context,
            context_lineage=bind_context_lineage(inputs, context),
            prompt=prompt,
            gen_kwargs=gen_kwargs,
            policy=policy,
            runtime=runtime,
            plan=plan,
            legacy_service=legacy_service,
            execution_plan=execution_plan,
            authorization=authorization,
            owner_id=owner_id,
            resume_run_id=resume_run_id,
            expected_run_revision=expected_run_revision,
            truncation={
                "truncated_sections": list(context.truncated_sections),
                "dropped_item_counts": dict(context.dropped_item_counts),
            },
        )

    async def _prose_authorization_revision(
        self,
        *,
        command: ProseGenerationCommand,
        owner_id: str | None,
        policy: ProseContinuationPolicy,
    ) -> int:
        if owner_id is None or not command.resume_run_id:
            return 1
        existing = await self._deps.prose_run_repo.get_run(
            command.resume_run_id,
            owner_id,
        )
        stored = dict(existing.get("prose_continuation_authorization") or {})
        stored_revision = int(
            stored.get("authorization_revision")
            or existing.get("authorization_revision")
            or 0
        )
        if (
            dict(stored.get("policy") or {}) == policy.to_dict()
            and stored.get("token_budget") == command.token_budget
            and not authorization_ruleset_requires_refresh(
                stored,
                policy=policy,
            )
        ):
            return max(1, stored_revision)
        return max(1, stored_revision + 1)

    async def _stream_prose(
        self,
        prepared: _PreparedProse,
    ) -> AsyncIterator[ChapterGenerationEvent]:
        if any(prepared.truncation.values()):
            yield ChapterGenerationEvent(
                name="context",
                data=prepared.truncation,
            )
        yield ChapterGenerationEvent(
            name="plan",
            data=prepared.execution_plan.to_dict(),
        )

        command = prepared.command
        runtime = prepared.runtime
        plan = prepared.plan
        service = prepared.legacy_service
        owner_id = prepared.owner_id
        run_document: dict[str, Any] | None = None
        try:
            if owner_id is not None:
                run_document = await self._deps.prose_runs.begin(
                    owner_id=owner_id,
                    novel_id=command.novel_id,
                    chapter_id=command.chapter_id,
                    outline=prepared.chapter.get("outline") or {},
                    context_text=prepared.context.to_prompt_text(),
                    context_lineage=prepared.context_lineage,
                    plan=prepared.execution_plan,
                    provider_plan={
                        "provider_alias": (
                            getattr(plan, "provider_alias", "")
                            if plan is not None
                            else "legacy"
                        ),
                        "provider_model": _provider_capability(plan)["model"],
                        "config_revision": (
                            getattr(plan, "config_revision", "")
                            if plan is not None
                            else ""
                        ),
                        "thinking_mode": (
                            getattr(plan, "thinking_mode", None)
                            if plan is not None
                            else None
                        ),
                    },
                    generation_job_id=command.generation_job_id,
                    run_id=prepared.resume_run_id,
                    expected_revision=prepared.expected_run_revision,
                    confirm_uncertain_retry=(
                        command.confirm_uncertain_retry
                        or bool(
                            getattr(
                                command.attempt_scope,
                                "confirm_uncertain_retry",
                                False,
                            )
                        )
                    ),
                    replace_exhausted=(
                        command.authority is AcceptanceAuthority.SYSTEM
                    ),
                    authorization=prepared.authorization,
                )
        except Exception as exc:
            yield ChapterGenerationEvent(
                name="done",
                data={
                    "success": False,
                    "error": str(exc),
                    "completion_status": "stale",
                },
            )
            return

        if run_document is not None:
            yield ChapterGenerationEvent(
                name="run",
                data={
                    "run_id": str(run_document["_id"]),
                    "run_revision": int(run_document.get("revision") or 0),
                },
            )
        if (
            run_document is not None
            and runtime is not None
            and plan is not None
            and command.attempt_scope is None
        ):
            lease = run_document.get("lease") or {}
            runtime = self._deps.create_runtime(
                attempt_scope=self._deps.create_prose_attempt_scope(
                    run_id=str(run_document["_id"]),
                    owner_id=owner_id or "",
                    lease_token=str(lease.get("token") or ""),
                ),
                max_provider_retries=0,
            )

        queue: asyncio.Queue[ChapterGenerationEvent | None] = asyncio.Queue()
        latest_run = run_document

        def usage_reader() -> TokenUsage:
            if runtime is not None:
                attempts = list(getattr(runtime, "attempts", None) or [])
                return attempts[-1].usage if attempts else runtime.usage
            raw = getattr(service, "last_usage", None)
            if isinstance(raw, TokenUsage):
                return raw
            if hasattr(raw, "model_dump"):
                return TokenUsage.model_validate(raw.model_dump())
            return TokenUsage()

        def finish_reason_reader() -> Any:
            return getattr(
                runtime if runtime is not None else service,
                "last_finish_reason",
                None,
            )

        def raw_finish_reason_reader() -> Any:
            return getattr(
                runtime if runtime is not None else service,
                "last_raw_finish_reason",
                finish_reason_reader(),
            )

        def stream_call(
            call_prompt: str,
            call_kwargs: dict[str, Any],
        ) -> AsyncIterator[str]:
            async def consume() -> AsyncIterator[str]:
                frames = self._deps.stream_prose(
                    workflow_name=PROSE_WORKFLOW,
                    step_key=PROSE_STEP,
                    prompt=call_prompt,
                    service=service,
                    gen_kwargs=call_kwargs,
                    request_id=command.request_id or uuid4().hex[:8],
                    is_disconnected=command.is_disconnected,
                    runtime=runtime,
                    generation_plan=plan,
                )
                async for chunk in _stream_prose_deltas(frames):
                    yield chunk

            return consume()

        async def on_delta(chunk: str) -> None:
            await queue.put(
                ChapterGenerationEvent(
                    name="delta",
                    data={"text": chunk},
                )
            )

        async def on_segment(segment: dict[str, Any]) -> None:
            nonlocal latest_run
            if latest_run is None or owner_id is None:
                return
            lease = latest_run.get("lease") or {}
            latest_run = await self._deps.prose_run_repo.append_segment(
                run_id=str(latest_run["_id"]),
                owner_id=owner_id,
                lease_token=str(lease.get("token") or ""),
                segment=segment,
            )

        async def on_scene_progress(
            scene_progress: tuple[dict[str, Any], ...],
        ) -> None:
            nonlocal latest_run
            if latest_run is None or owner_id is None:
                return
            lease = latest_run.get("lease") or {}
            latest_run = await self._deps.prose_run_repo.update_scene_progress(
                run_id=str(latest_run["_id"]),
                owner_id=owner_id,
                lease_token=str(lease.get("token") or ""),
                scene_progress=[dict(item) for item in scene_progress],
            )

        async def produce() -> None:
            nonlocal latest_run
            try:
                generated = await self._deps.execute_prose_plan(
                    plan=prepared.execution_plan,
                    outline=prepared.chapter.get("outline") or {},
                    base_prompt=prepared.prompt,
                    stream_call=stream_call,
                    finish_reason_reader=finish_reason_reader,
                    usage_reader=usage_reader,
                    raw_finish_reason_reader=raw_finish_reason_reader,
                    outline_revision=str(
                        (run_document or {}).get("outline_revision")
                        or "ephemeral"
                    ),
                    gen_kwargs=prepared.gen_kwargs,
                    existing_segments=list(
                        (run_document or {}).get("segments") or []
                    ),
                    existing_scene_progress=list(
                        (run_document or {}).get("scene_progress") or []
                    ),
                    confirm_uncertain_retry=(
                        command.confirm_uncertain_retry
                        or bool(
                            getattr(
                                command.attempt_scope,
                                "confirm_uncertain_retry",
                                False,
                            )
                        )
                    ),
                    continuation_policy=prepared.policy,
                    manual_continuation=(
                        command.authority is AcceptanceAuthority.PREVIEW
                        and command.resume_run_id is not None
                    ),
                    on_delta=on_delta,
                    on_segment=on_segment,
                    on_scene_progress=on_scene_progress,
                )
                if latest_run is not None and owner_id is not None:
                    stored_completion = {
                        **generated.completion.to_dict(),
                        "scene_progress": [
                            dict(item) for item in generated.scene_progress
                        ],
                        "pause_reason": generated.pause_reason,
                    }
                    lease = latest_run.get("lease") or {}
                    latest_run = await self._deps.prose_run_repo.finish(
                        run_id=str(latest_run["_id"]),
                        owner_id=owner_id,
                        lease_token=str(lease.get("token") or ""),
                        status=(
                            "complete"
                            if generated.completion.can_write_formal_prose
                            else generated.completion.status
                        ),
                        completion=stored_completion,
                        assembled_text=generated.text,
                    )

                # SYSTEM authority may persist a complete ProseRun candidate, but
                # it no longer owns formal prose acceptance.  Every AI complete
                # write is issued later by the certificate finalizer together
                # with its semantic and state evidence.
                accepted = False

                completion = {
                    **generated.completion.to_dict(),
                    "source_run_id": (
                        str(latest_run["_id"]) if latest_run else None
                    ),
                    "scene_progress": [
                        dict(item) for item in generated.scene_progress
                    ],
                    "pause_reason": generated.pause_reason,
                    "source_run_revision": (
                        int(latest_run.get("revision") or 0)
                        if latest_run
                        else None
                    ),
                    "source_run_digest": chapter_content_digest(
                        generated.text
                    ),
                }
                usage = generated.usage.model_dump()
                payload = {
                    "success": generated.completion.can_write_formal_prose,
                    "text": generated.text,
                    "usage": usage,
                    "completion_status": generated.completion.status,
                    "finish_reason": generated.completion.finish_reason,
                    "requested_word_count": (
                        generated.completion.requested_word_count
                    ),
                    "actual_word_count": generated.completion.actual_word_count,
                    "raw_character_count": (
                        generated.completion.raw_character_count
                    ),
                    "scene_count": generated.completion.scene_count,
                    "completed_scene_count": (
                        generated.completion.completed_scene_count
                    ),
                    "mode": generated.completion.mode,
                    "scene_progress": completion["scene_progress"],
                    "pause_reason": generated.pause_reason,
                    "reason_codes": list(generated.completion.reason_codes),
                    "run_id": completion["source_run_id"],
                    "run_revision": completion["source_run_revision"],
                }
                if not generated.completion.can_write_formal_prose:
                    payload["error"] = (
                        "正文未满足完整性要求，已保留为可恢复草稿"
                    )
                result = ChapterGenerationResult(
                    stage=ChapterGenerationStage.PROSE,
                    value=generated.text,
                    usage=usage,
                    attempts=_serialize_attempts(runtime),
                    truncation=prepared.truncation,
                    completion=completion,
                    accepted=accepted,
                )
                await queue.put(
                    ChapterGenerationEvent(
                        name="done",
                        data=payload,
                        result=result,
                    )
                )
            except asyncio.CancelledError:
                if latest_run is not None and owner_id is not None:
                    await drain_cancellation_cleanup(
                        self._deps.prose_run_repo.mark_status(
                            run_id=str(latest_run["_id"]),
                            owner_id=owner_id,
                            status="incomplete",
                        )
                    )
                raise
            except Exception as exc:
                if latest_run is not None and owner_id is not None:
                    await self._deps.prose_run_repo.mark_status(
                        run_id=str(latest_run["_id"]),
                        owner_id=owner_id,
                        status="incomplete",
                    )
                continuation_limited = isinstance(exc, ProseContinuationLimit)
                boundary_code = pre_dispatch_boundary_code(exc)
                usage = usage_reader().model_dump()
                await queue.put(
                    ChapterGenerationEvent(
                        name="done",
                        data={
                            "success": False,
                            "error": str(exc),
                            "usage": usage,
                            "attempts": _serialize_attempts(runtime),
                            "completion_status": "incomplete",
                            "reason_codes": [
                                (
                                    "continuation_limit_reached"
                                    if continuation_limited
                                    else (
                                        boundary_code
                                        if boundary_code is not None
                                        else "uncertain_provider_attempt"
                                    )
                                )
                            ],
                            "has_uncertain_attempt": not (
                                continuation_limited
                                or boundary_code is not None
                            ),
                            "run_id": (
                                str(latest_run["_id"])
                                if latest_run
                                else None
                            ),
                            "run_revision": (
                                int(latest_run.get("revision") or 0)
                                if latest_run
                                else None
                            ),
                        },
                    )
                )
            finally:
                await queue.put(None)

        producer = asyncio.create_task(produce())
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield event
        finally:
            if not producer.done():
                producer.cancel()
            try:
                await producer
            except asyncio.CancelledError:
                pass


def _provider_capability(plan: Any) -> dict[str, Any]:
    return {
        "max_output_tokens": getattr(plan, "max_output_tokens", None),
        "model": getattr(plan, "provider_model", ""),
    }


def _extract_outline(name: str, data: Mapping[str, Any]) -> dict[str, Any] | None:
    if (
        name == "step"
        and data.get("step") == CHAPTER_OUTLINE_STEP
        and data.get("status") == "done"
        and isinstance(data.get("data"), dict)
    ):
        return dict(data["data"])
    result = data.get("result")
    if (
        name == "done"
        and data.get("success")
        and isinstance(result, dict)
        and isinstance(result.get(CHAPTER_OUTLINE_STEP), dict)
    ):
        return dict(result[CHAPTER_OUTLINE_STEP])
    return None


def _extract_state_proposal(
    name: str,
    data: Mapping[str, Any],
) -> dict[str, Any] | None:
    if (
        name == "step"
        and data.get("step") == STATE_STEP
        and data.get("status") == "done"
        and isinstance(data.get("data"), dict)
    ):
        return dict(data["data"])
    result = data.get("result")
    if (
        name == "done"
        and data.get("success")
        and isinstance(result, dict)
        and isinstance(result.get(STATE_STEP), dict)
    ):
        return dict(result[STATE_STEP])
    return None


def _replace_outline(
    name: str,
    data: Mapping[str, Any],
    cleaned: dict[str, Any],
) -> dict[str, Any]:
    replaced = dict(data)
    if name == "step":
        replaced["data"] = cleaned
    else:
        result = dict(replaced.get("result") or {})
        result[CHAPTER_OUTLINE_STEP] = cleaned
        replaced["result"] = result
    return replaced


def _serialize_attempts(runtime: Any) -> list[dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    for item in list(getattr(runtime, "attempts", None) or []):
        usage = getattr(item, "usage", None)
        if hasattr(usage, "model_dump"):
            usage = usage.model_dump()
        attempts.append(
            {
                "attempt_id": getattr(item, "attempt_id", ""),
                "provider_alias": getattr(item, "provider_alias", ""),
                "phase": getattr(item, "phase", ""),
                "state": getattr(item, "state", ""),
                "usage": dict(usage or {}),
            }
        )
    return attempts


def _runtime_usage(runtime: Any) -> dict[str, Any]:
    usage = getattr(runtime, "usage", None)
    if hasattr(usage, "model_dump"):
        return usage.model_dump()
    return dict(usage or {})
