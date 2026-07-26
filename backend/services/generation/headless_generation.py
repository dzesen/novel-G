"""Approach A：无头驱动现有 LLM 工作流，产出结构化结果供批量引擎用（设计 §4.2）。

不重构已上线工作流：直接调 run_workflow/stream_prose，内部消费其 SSE 帧取结果，
再调服务层 accept。装配（fetch_context_inputs / assemble_* / WorkflowDeps）复制自
outline_router / prose_router / state_router 的开流前设置——那几处是模块级、可复用。
"""
from __future__ import annotations

from typing import Any, AsyncGenerator, Callable, Dict, Tuple
from uuid import uuid4

from backend.llm.prompts.prompt_selector import (
    CHAPTER_OUTLINE_PROMPT_NAME, CHAPTER_STATE_PROMPT_NAME, PROSE_PROMPT_NAME, load_prompt_config,
)
from backend.services.llm.context_builder import (
    assemble_context, assemble_outline_context, fetch_context_inputs,
)
from backend.services.llm.agent_orchestrator import apply_agent_profile
from backend.services.llm.prose_runner import stream_prose
from backend.services.llm.workflow_runner import (
    WorkflowDeps, WorkflowFailed, parse_sse_event, run_workflow, run_workflow_to_result,
)
from backend.services.llm.generation_runtime import (
    AttemptScope,
    WorkflowStepTarget,
    create_generation_runtime,
)
from backend.services.novel.state_validation import validate_state_ids
from backend.services.novel.outline_validation import validate_outline_ids
from backend.db.repositories.novel_repository import novel_repo
from backend.db.repositories.chapter_repository import chapter_repo
# 步骤表与工作流常量直接借用路由模块（模块级、复用非重构）。
# 注意：outline_router / state_router 都没有导出标量的 "...STEP" 常量——
# outline 侧只有步骤表 CHAPTER_OUTLINE_STEPS（单步表，key 用 CHAPTER_OUTLINE_STEPS[0].key
# 取，不硬编码字面量）；state 侧的标量步骤名叫 STATE_STEP（不是 CHAPTER_STATE_STEP）。
from backend.api.llm_routers.outline_router import (
    CHAPTER_OUTLINE_STEPS, CHAPTER_OUTLINE_WORKFLOW,
)
from backend.api.llm_routers.prose_router import PROSE_STEP, PROSE_WORKFLOW
from backend.api.llm_routers.state_router import (
    CHAPTER_STATE_STEPS, STATE_STEP, STATE_WORKFLOW,
)
from backend.services.generation.chapter_pipeline import ChapterPipelineDeps
from backend.services.novel.chapter_service import ChapterService
from backend.services.novel.state_proposal import (
    SelectAllPolicy,
    state_proposal_module,
)

CHAPTER_OUTLINE_STEP = CHAPTER_OUTLINE_STEPS[0].key


def estimate_chapter_attempt_slots(chapter: Dict[str, Any]) -> int:
    """按当前不可变 GenerationPlan 计算一章的最大语义调用数。"""
    runtime = create_generation_runtime()
    slots = 0
    if not chapter.get("outline"):
        slots += runtime.plan_structured(
            WorkflowStepTarget(CHAPTER_OUTLINE_WORKFLOW, CHAPTER_OUTLINE_STEP)
        ).max_semantic_attempts
    if not str(chapter.get("content") or "").strip():
        slots += runtime.plan_text(
            WorkflowStepTarget(PROSE_WORKFLOW, PROSE_STEP)
        ).max_semantic_attempts
    if not str(chapter.get("summary") or "").strip():
        slots += runtime.plan_structured(
            WorkflowStepTarget(STATE_WORKFLOW, STATE_STEP)
        ).max_semantic_attempts
    return slots


def estimate_worklist_attempt_capacity(chapters: list[Dict[str, Any]]) -> int:
    """固定总容量等于当前工作清单各章计划上限之和，最少保留一槽。"""
    return max(1, sum(estimate_chapter_attempt_slots(chapter) for chapter in chapters))


def _deps_for(workflow_name: str, attempt_scope: AttemptScope | None = None) -> WorkflowDeps:
    del workflow_name
    return WorkflowDeps(
        runtime=create_generation_runtime(attempt_scope=attempt_scope),
    )


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
) -> tuple[dict, dict, int, dict, list[dict[str, Any]]]:
    inputs = await fetch_context_inputs(novel_id, str(chapter["_id"]))
    context = assemble_outline_context(inputs)
    roster = inputs["roster"]
    # words_per_chapter 是小说级字段（chapter 文档上不存在这一字段，见
    # backend/api/default_routers/novel_router.py 的 NovelBase），故须另取 novel
    # 文档；与 outline_router.create_chapter_outline_by_ai 的取值方式一致
    # （novel.get("words_per_chapter") or 3000），不能读 chapter.get(...)——
    # chapter 上该字段恒为 None，会让批量生成对所有小说都悄悄按 3000 字生成。
    novel = await novel_repo.get_novel_by_id(novel_id)
    params = {
        "context": context.to_prompt_text(),
        "chapter_order": int(chapter.get("order_index") or 0),
        "chapter_title": str(chapter.get("title") or ""),
        "words_per_chapter": novel.get("words_per_chapter") or 3000,
    }
    deps = _deps_for(CHAPTER_OUTLINE_WORKFLOW, attempt_scope)
    frames = run_workflow(
        workflow_name=CHAPTER_OUTLINE_WORKFLOW, steps=CHAPTER_OUTLINE_STEPS,
        prompts=load_prompt_config().get(CHAPTER_OUTLINE_PROMPT_NAME, {}),
        params=params, gen_kwargs={}, cached={}, deps=deps,
        request_id=uuid4().hex[:8],
    )
    result, tokens = await run_workflow_to_result(CHAPTER_OUTLINE_STEP, frames)
    cleaned, dropped = validate_outline_ids(result, roster)
    truncation = {
        "truncated_sections": list(context.truncated_sections),
        "dropped_item_counts": dict(context.dropped_item_counts),
    }
    return cleaned, dropped, tokens, truncation, _serialize_attempts(deps.runtime)


async def generate_prose(
    novel_id: str,
    chapter: Dict[str, Any],
    attempt_scope: AttemptScope | None = None,
) -> tuple[str, int, dict, list[dict[str, Any]]]:
    inputs = await fetch_context_inputs(novel_id, str(chapter["_id"]))
    context = assemble_context(inputs)
    # outline 取 fetch_context_inputs 内部刚刚重新查库得到的版本，不用调用方传入
    # 的 chapter 参数：chapter_pipeline.run_chapter 的入口快照只用于 skip-existing
    # 判断，同一轮里若本步之前的 generate_outline/accept_outline 刚写入了 outline，
    # 调用方那份快照仍是写入前的旧值（该函数文档明确写了"生成函数内部读库看得到
    # 本轮先前写入"这一契约）。用旧快照会让刚生成、刚接受的细纲的 target_word_count
    # 被悄悄忽略，退化成小说级默认字数。
    outline = (inputs.get("chapter") or {}).get("outline") or {}
    # 与 prose_router.write_chapter_by_ai 的取值方式一致（本章细纲的
    # target_word_count 优先，其次小说级 words_per_chapter，最后 3000）；
    # words_per_chapter 是小说级字段，chapter 文档上不存在，见 generate_outline
    # 同一处注释。
    novel = await novel_repo.get_novel_by_id(novel_id)
    words = outline.get("target_word_count") or novel.get("words_per_chapter") or 3000
    prompts = load_prompt_config().get(PROSE_PROMPT_NAME, {})
    prompt = apply_agent_profile(
        "chapter_writer",
        prompts[f"{PROSE_STEP}_prompt_base"].format(
            context=context.to_prompt_text(),
            chapter_order=int(chapter.get("order_index") or 0),
            chapter_title=str(chapter.get("title") or ""),
            words_per_chapter=words,
        )
        + "\n" + prompts[f"{PROSE_STEP}_prompt_without_schema_suffix"]
    )
    runtime = create_generation_runtime(attempt_scope=attempt_scope)
    plan = runtime.plan_text(WorkflowStepTarget(PROSE_WORKFLOW, PROSE_STEP))
    frames = stream_prose(
        workflow_name=PROSE_WORKFLOW, step_key=PROSE_STEP, prompt=prompt,
        service=None, gen_kwargs={}, request_id=uuid4().hex[:8],
        runtime=runtime, generation_plan=plan,
    )
    text, tokens = await _consume_prose_frames(frames)
    truncation = {
        "truncated_sections": list(context.truncated_sections),
        "dropped_item_counts": dict(context.dropped_item_counts),
    }
    return text, tokens, truncation, _serialize_attempts(runtime)


async def generate_state(
    novel_id: str,
    chapter: Dict[str, Any],
    attempt_scope: AttemptScope | None = None,
) -> tuple[dict, dict, int, dict, list[dict[str, Any]]]:
    chapter_id = str(chapter["_id"])
    fresh_chapter = await chapter_repo.get_chapter_by_id(chapter_id)
    generation_snapshot = await state_proposal_module.capture(
        novel_id,
        chapter_id,
        chapter=fresh_chapter,
    )
    generation_lease = await state_proposal_module.begin(
        novel_id,
        chapter_id,
        snapshot=generation_snapshot,
        audit={"workflow": STATE_WORKFLOW, "step": STATE_STEP, "mode": "headless"},
    )
    try:
        inputs = await fetch_context_inputs(novel_id, chapter_id)
        context = assemble_context(inputs)
        roster = inputs["roster"]
        # 正文必须重新查库取：调用方传入的 chapter 是管线入口快照，只用于
        # skip-existing；同轮先前步骤可能刚写入正文。
        params = {
            "context": context.to_prompt_text(),
            "chapter_order": int(chapter.get("order_index") or 0),
            "chapter_title": str(chapter.get("title") or ""),
            "chapter_content": str(fresh_chapter.get("content") or "").strip(),
        }
        deps = _deps_for(STATE_WORKFLOW, attempt_scope)
        await state_proposal_module.ensure_current(generation_snapshot)
        frames = run_workflow(
            workflow_name=STATE_WORKFLOW, steps=CHAPTER_STATE_STEPS,
            prompts=load_prompt_config().get(CHAPTER_STATE_PROMPT_NAME, {}),
            params=params, gen_kwargs={}, cached={}, deps=deps,
            request_id=uuid4().hex[:8],
        )
        result, tokens = await run_workflow_to_result(STATE_STEP, frames)
        await state_proposal_module.ensure_current(generation_snapshot)
        cleaned, dropped = validate_state_ids(result, roster)
        attempts = _serialize_attempts(deps.runtime)
        proposal = await state_proposal_module.publish(
            generation_lease,
            cleaned,
            audit={
                "usage": {"total_tokens": tokens},
                "attempts": attempts,
            },
        )
        truncation = {
            "truncated_sections": list(context.truncated_sections),
            "dropped_item_counts": dict(context.dropped_item_counts),
        }
        return proposal, dropped, tokens, truncation, attempts
    except BaseException as exc:
        await state_proposal_module.mark_failed(
            generation_lease,
            exc,
            audit={
                "usage": getattr(exc, "usage", None) or {},
                "attempts": list(getattr(exc, "attempts", None) or []),
            },
        )
        raise


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


async def _accept_outline(chapter_id: str, result: dict) -> None:
    await ChapterService.accept_chapter_outline(chapter_id, result)


async def _write_prose(chapter_id: str, text: str) -> None:
    await ChapterService.update_chapter(chapter_id, {"content": text})


async def _accept_state(chapter_id: str, proposal: dict) -> dict:
    return await state_proposal_module.run_auto(
        chapter_id=chapter_id,
        proposal=proposal,
        policy=SelectAllPolicy(),
    )


def build_chapter_pipeline_deps(
    attempt_scope_factory: Callable[[str], AttemptScope] | None = None,
) -> ChapterPipelineDeps:
    def scope(step: str) -> AttemptScope | None:
        return attempt_scope_factory(step) if attempt_scope_factory is not None else None

    return ChapterPipelineDeps(
        generate_outline=lambda novel_id, chapter: generate_outline(
            novel_id, chapter, scope("outline")
        ),
        generate_prose=lambda novel_id, chapter: generate_prose(
            novel_id, chapter, scope("prose")
        ),
        generate_state=lambda novel_id, chapter: generate_state(
            novel_id, chapter, scope("state")
        ),
        accept_outline=_accept_outline, write_prose=_write_prose, accept_state=_accept_state,
    )
