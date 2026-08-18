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
from typing import Any
from uuid import uuid4

from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.repositories.generation_job_repository import TokenBudgetExceeded
from backend.db.repositories.novel_repository import novel_repo
from backend.db.repositories.prose_run_repository import prose_run_repo
from backend.db.utils import to_object_id
from backend.llm.config import get_llm_config
from backend.llm.prompts.prompt_selector import (
    CHAPTER_OUTLINE_PROMPT_NAME,
    OUTLINE_ADHERENCE_PROMPT_NAME,
    PROSE_PROMPT_NAME,
    load_prompt_config,
)
from backend.llm.schemas.novel_pydantic import (
    ChapterOutlineAdherenceResultSchema,
    ChapterOutlineResultSchema,
)
from backend.llm.models import TokenUsage
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
from backend.services.generation.outline_adherence import (
    normalize_outline_adherence,
)
from backend.services.llm.context_builder import (
    assemble_context,
    assemble_outline_context,
    fetch_context_inputs,
    outline_selection_roster,
)
from backend.services.llm.generation_runtime import (
    AttemptScope,
    PromptPlan,
    WorkflowStepTarget,
    create_generation_runtime,
)
from backend.services.llm.agent_orchestrator import apply_agent_profile
from backend.services.llm.prose_runner import stream_prose
from backend.services.llm.workflow_service import get_llm_service_for_step
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
from backend.services.novel.state_completion import chapter_content_digest
from backend.services.novel.outline_validation import validate_outline_ids
from backend.services.novel.state_validation import (
    resolve_outline_character_references,
)
from backend.services.novel.style_controls import render_style_controls


CHAPTER_OUTLINE_WORKFLOW = "create_chapter_outline_by_ai"
CHAPTER_OUTLINE_STEP = "chapter_outline"
PROSE_WORKFLOW = "write_chapter_by_ai"
PROSE_STEP = "chapter_content"
STATE_WORKFLOW = "extract_chapter_state_by_ai"
STATE_STEP = "chapter_state"

CHAPTER_OUTLINE_STEPS: tuple[WorkflowStep, ...] = (
    WorkflowStep(
        key=CHAPTER_OUTLINE_STEP,
        schema=ChapterOutlineResultSchema,
        agent_id="chapter_planner",
        prompt_args=lambda ctx: {
            "context": ctx.params["context"],
            "chapter_order": ctx.params["chapter_order"],
            "chapter_title": ctx.params["chapter_title"],
            "style_controls": ctx.params["style_controls"],
            "words_per_chapter": ctx.params["words_per_chapter"],
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
        "system_prompt",
    }
)


class AcceptanceAuthority(str, Enum):
    """调用方获准执行的落库级别。"""

    PREVIEW = "preview"
    SYSTEM = "system"


class ChapterGenerationStage(str, Enum):
    OUTLINE = "outline"
    PROSE = "prose"
    OUTLINE_ADHERENCE = "outline_adherence"
    STATE = "state"


@dataclass(frozen=True)
class OutlineGenerationCommand:
    novel_id: str
    chapter_id: str
    authority: AcceptanceAuthority = AcceptanceAuthority.PREVIEW
    generation_params: Mapping[str, Any] = field(default_factory=dict)
    attempt_scope: AttemptScope | None = None
    request_id: str | None = None
    is_disconnected: Callable[[], Awaitable[bool]] | None = None


@dataclass(frozen=True)
class ProseGenerationCommand:
    novel_id: str
    chapter_id: str
    authority: AcceptanceAuthority = AcceptanceAuthority.PREVIEW
    owner_id: str | None = None
    generation_params: Mapping[str, Any] = field(default_factory=dict)
    attempt_scope: AttemptScope | None = None
    resume_run_id: str | None = None
    expected_run_revision: int | None = None
    confirm_uncertain_retry: bool = False
    continuation_policy: ProseContinuationPolicy | Mapping[str, Any] | None = None
    readiness_digest: str | None = None
    confirm_automatic_continuations: bool = False
    token_budget: int | None = None
    request_id: str | None = None
    is_disconnected: Callable[[], Awaitable[bool]] | None = None


@dataclass(frozen=True)
class OutlineAdherenceCommand:
    novel_id: str
    chapter_id: str
    generation_params: Mapping[str, Any] = field(default_factory=dict)
    attempt_scope: AttemptScope | None = None


@dataclass(frozen=True)
class ChapterGenerationResult:
    stage: ChapterGenerationStage
    value: Any
    usage: dict[str, Any]
    attempts: list[dict[str, Any]]
    truncation: dict[str, Any]
    dropped: dict[str, Any] = field(default_factory=dict)
    remapped: list[dict[str, Any]] = field(default_factory=list)
    completion: dict[str, Any] = field(default_factory=dict)
    accepted: bool = False

    @property
    def total_tokens(self) -> int:
        return int(self.usage.get("total_tokens") or 0)


@dataclass(frozen=True)
class ChapterGenerationEvent:
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
        )


@dataclass(frozen=True)
class _PreparedOutline:
    command: OutlineGenerationCommand
    params: dict[str, Any]
    gen_kwargs: dict[str, Any]
    roster: dict[str, Any]
    runtime: Any
    truncation: dict[str, Any]


@dataclass(frozen=True)
class _PreparedProse:
    command: ProseGenerationCommand
    novel: dict[str, Any]
    chapter: dict[str, Any]
    context: Any
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
        ),
    ) -> AsyncIterator[ChapterGenerationEvent]:
        """预检命令并返回事件流；预检错误发生在 HTTP 开流之前。"""

        if isinstance(command, OutlineGenerationCommand):
            prepared = await self._prepare_outline(command)
            return self._stream_outline(prepared)
        if isinstance(command, ProseGenerationCommand):
            prepared = await self._prepare_prose(command)
            return self._stream_prose(prepared)
        if isinstance(command, OutlineAdherenceCommand):
            prepared = await self._prepare_outline_adherence(command)
            return self._stream_outline_adherence(prepared)
        raise TypeError(f"unsupported chapter generation command: {type(command)!r}")

    async def collect(
        self,
        command: (
            OutlineGenerationCommand
            | ProseGenerationCommand
            | OutlineAdherenceCommand
        ),
    ) -> ChapterGenerationResult:
        """无头消费同一事件流，并把失败终帧恢复成带审计信息的异常。"""

        execution = await self.execute(command)
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
            )
        raise WorkflowFailed("chapter generation ended without a successful result")

    async def _prepare_outline(
        self,
        command: OutlineGenerationCommand,
    ) -> _PreparedOutline:
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
            yield ChapterGenerationEvent("context", prepared.truncation)

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
            deps=WorkflowDeps(runtime=prepared.runtime),
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
                yield ChapterGenerationEvent("keepalive", {})
                continue
            name, raw_data = parsed
            data = dict(raw_data)
            outline = _extract_outline(name, data)
            if outline is None:
                yield ChapterGenerationEvent(name, data)
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
                        "id_remapping",
                        {"remapped": final_remapped},
                    )
                    reported_remapped = True
            if dropped:
                final_dropped = dict(dropped)
                if not reported_dropped:
                    yield ChapterGenerationEvent(
                        "id_validation",
                        {"dropped": final_dropped},
                    )
                    reported_dropped = True
            cleaned_data = _replace_outline(name, data, cleaned)
            if name != "done":
                yield ChapterGenerationEvent(name, cleaned_data)
                continue

            accepted = False
            if command.authority is AcceptanceAuthority.SYSTEM:
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
            yield ChapterGenerationEvent(name, cleaned_data, result=result)

    async def _prepare_outline_adherence(
        self,
        command: OutlineAdherenceCommand,
    ) -> _PreparedOutlineAdherence:
        chapter = await self._deps.chapter_repo.get_chapter_by_id(
            command.chapter_id
        )
        if chapter.get("novel_id") != to_object_id(command.novel_id):
            raise ValueError("该章节不属于指定小说")
        content = str(chapter.get("content") or "").strip()
        if not content:
            raise ValueError("本章尚无可供细纲符合度检查的正文")
        if not chapter.get("outline"):
            raise ValueError("本章尚无可供细纲符合度检查的章节细纲")

        inputs = await self._deps.fetch_context_inputs(
            command.novel_id,
            command.chapter_id,
        )
        context = self._deps.assemble_context(inputs)
        prompts = self._deps.load_prompts().get(
            OUTLINE_ADHERENCE_PROMPT_NAME,
            {},
        )
        prompt_base = prompts["outline_adherence_prompt_base"].format(
            context=context.to_prompt_text(),
            chapter_order=int(chapter.get("order_index") or 0),
            chapter_title=str(chapter.get("title") or ""),
            chapter_content=content,
        )
        prompt_plan = PromptPlan(
            native_schema_prompt=apply_agent_profile(
                "continuity_editor",
                prompt_base
                + "\n"
                + prompts["outline_adherence_prompt_with_schema_suffix"],
            ),
            prompt_json_prompt=apply_agent_profile(
                "continuity_editor",
                prompt_base
                + "\n"
                + prompts["outline_adherence_prompt_without_schema_suffix"],
            ),
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
        plan = runtime.plan_structured(
            WorkflowStepTarget(STATE_WORKFLOW, STATE_STEP)
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
        )

    async def _stream_outline_adherence(
        self,
        prepared: _PreparedOutlineAdherence,
    ) -> AsyncIterator[ChapterGenerationEvent]:
        if any(prepared.truncation.values()):
            yield ChapterGenerationEvent("context", prepared.truncation)
        yield ChapterGenerationEvent(
            "step",
            {
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
                ChapterOutlineAdherenceResultSchema,
                prepared.prompt_plan,
                **prepared.gen_kwargs,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            usage = _runtime_usage(prepared.runtime)
            yield ChapterGenerationEvent(
                "done",
                {
                    "success": False,
                    "failed_step": "outline_adherence",
                    "error": str(exc),
                    "usage": usage,
                    "attempts": _serialize_attempts(prepared.runtime),
                },
            )
            return

        review = normalize_outline_adherence(generated.value.model_dump())
        usage = generated.usage.model_dump()
        attempts = _serialize_attempts(prepared.runtime)
        yield ChapterGenerationEvent(
            "step",
            {
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
            "done",
            {
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
            yield ChapterGenerationEvent("context", prepared.truncation)
        yield ChapterGenerationEvent("plan", prepared.execution_plan.to_dict())

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
                "done",
                {
                    "success": False,
                    "error": str(exc),
                    "completion_status": "stale",
                },
            )
            return

        if run_document is not None:
            yield ChapterGenerationEvent(
                "run",
                {
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
                async for frame in self._deps.stream_prose(
                    workflow_name=PROSE_WORKFLOW,
                    step_key=PROSE_STEP,
                    prompt=call_prompt,
                    service=service,
                    gen_kwargs=call_kwargs,
                    request_id=command.request_id or uuid4().hex[:8],
                    is_disconnected=command.is_disconnected,
                    runtime=runtime,
                    generation_plan=plan,
                ):
                    parsed = parse_sse_event(frame)
                    if parsed is None:
                        continue
                    name, data = parsed
                    if name == "delta" and data.get("text"):
                        yield str(data["text"])

            return consume()

        async def on_delta(chunk: str) -> None:
            await queue.put(ChapterGenerationEvent("delta", {"text": chunk}))

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

                accepted = False
                if (
                    command.authority is AcceptanceAuthority.SYSTEM
                    and generated.completion.can_write_formal_prose
                ):
                    if latest_run is None or owner_id is None:
                        raise ValueError("系统接受正文前缺少可审计的 ProseRun")
                    await self._deps.prose_runs.accept(
                        owner_id=owner_id,
                        run_id=str(latest_run["_id"]),
                        chapter_id=command.chapter_id,
                        expected_revision=int(latest_run.get("revision") or 0),
                        accept_partial=False,
                        partial_acknowledgement=False,
                    )
                    accepted = True

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
                    ChapterGenerationEvent("done", payload, result=result)
                )
            except asyncio.CancelledError:
                if latest_run is not None and owner_id is not None:
                    await asyncio.shield(
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
                budget_limited = isinstance(exc, TokenBudgetExceeded)
                usage = usage_reader().model_dump()
                await queue.put(
                    ChapterGenerationEvent(
                        "done",
                        {
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
                                        "token_budget_exceeded_before_dispatch"
                                        if budget_limited
                                        else "uncertain_provider_attempt"
                                    )
                                )
                            ],
                            "has_uncertain_attempt": not (
                                continuation_limited or budget_limited
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
