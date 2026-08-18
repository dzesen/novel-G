"""章节生成的统一应用层入口。

本模块拥有章节级生成的上下文、提示词、运行时、结果清洗与接受权限。HTTP 路由
和批量作业只负责把各自的输入翻译成命令，并消费同一组类型化事件；二者不得再
各自装配一套 LLM 工作流。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import uuid4

from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.repositories.novel_repository import novel_repo
from backend.db.utils import to_object_id
from backend.llm.config import get_llm_config
from backend.llm.prompts.prompt_selector import (
    CHAPTER_OUTLINE_PROMPT_NAME,
    load_prompt_config,
)
from backend.llm.schemas.novel_pydantic import ChapterOutlineResultSchema
from backend.services.llm.context_builder import (
    assemble_outline_context,
    fetch_context_inputs,
    outline_selection_roster,
)
from backend.services.llm.generation_runtime import (
    AttemptScope,
    create_generation_runtime,
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
from backend.services.novel.outline_validation import validate_outline_ids
from backend.services.novel.state_validation import (
    resolve_outline_character_references,
)
from backend.services.novel.style_controls import render_style_controls


CHAPTER_OUTLINE_WORKFLOW = "create_chapter_outline_by_ai"
CHAPTER_OUTLINE_STEP = "chapter_outline"

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
class ChapterGenerationResult:
    stage: ChapterGenerationStage
    value: dict[str, Any]
    usage: dict[str, Any]
    attempts: list[dict[str, Any]]
    truncation: dict[str, Any]
    dropped: dict[str, Any] = field(default_factory=dict)
    remapped: list[dict[str, Any]] = field(default_factory=list)
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
        )


@dataclass(frozen=True)
class _PreparedOutline:
    command: OutlineGenerationCommand
    params: dict[str, Any]
    gen_kwargs: dict[str, Any]
    roster: dict[str, Any]
    runtime: Any
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
        command: OutlineGenerationCommand,
    ) -> AsyncIterator[ChapterGenerationEvent]:
        """预检命令并返回事件流；预检错误发生在 HTTP 开流之前。"""

        if not isinstance(command, OutlineGenerationCommand):
            raise TypeError(f"unsupported chapter generation command: {type(command)!r}")
        prepared = await self._prepare_outline(command)
        return self._stream_outline(prepared)

    async def collect(
        self,
        command: OutlineGenerationCommand,
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

