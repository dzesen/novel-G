"""Approach A：无头驱动现有 LLM 工作流，产出结构化结果供批量引擎用（设计 §4.2）。

不重构已上线工作流：直接调 run_workflow/stream_prose，内部消费其 SSE 帧取结果，
再调服务层 accept。装配（fetch_context_inputs / assemble_* / WorkflowDeps）复制自
outline_router / prose_router / state_router 的开流前设置——那几处是模块级、可复用。
"""
from __future__ import annotations

from typing import Any, AsyncGenerator, Dict, Tuple
from uuid import uuid4

from backend.llm.config import get_provider_config
from backend.llm.prompts.prompt_selector import (
    CHAPTER_OUTLINE_PROMPT_NAME, CHAPTER_STATE_PROMPT_NAME, PROSE_PROMPT_NAME, load_prompt_config,
)
from backend.services.llm.context_builder import (
    assemble_context, assemble_outline_context, fetch_context_inputs,
)
from backend.services.llm.format_review_service import validate_and_fix_format
from backend.services.llm.prose_runner import stream_prose
from backend.services.llm.workflow_runner import (
    WorkflowDeps, WorkflowFailed, parse_sse_event, run_workflow, run_workflow_to_result,
)
from backend.services.llm.workflow_service import (
    get_llm_service_for_step, resolve_provider_for_step, resolve_timeout_for_step,
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
from backend.services.novel.chapter_state_service import ChapterStateService

CHAPTER_OUTLINE_STEP = CHAPTER_OUTLINE_STEPS[0].key


def _supports_schema(workflow_name: str):
    def check(step_name: str) -> bool:
        provider = resolve_provider_for_step(workflow_name, step_name)
        return bool(provider) and get_provider_config(provider).supports_json_schema
    return check


def _deps_for(workflow_name: str) -> WorkflowDeps:
    return WorkflowDeps(
        resolve_provider=resolve_provider_for_step,
        resolve_timeout=resolve_timeout_for_step,
        get_service=get_llm_service_for_step,
        supports_schema=_supports_schema(workflow_name),
        fix_format=validate_and_fix_format,
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
                raise WorkflowFailed(data.get("error") or "prose generation failed")
            return str(data.get("text") or ""), int((data.get("usage") or {}).get("total_tokens") or 0)
    raise WorkflowFailed("prose stream ended without a done event")


async def generate_outline(novel_id: str, chapter: Dict[str, Any]) -> Tuple[dict, dict, int, dict]:
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
    frames = run_workflow(
        workflow_name=CHAPTER_OUTLINE_WORKFLOW, steps=CHAPTER_OUTLINE_STEPS,
        prompts=load_prompt_config().get(CHAPTER_OUTLINE_PROMPT_NAME, {}),
        params=params, gen_kwargs={}, cached={}, deps=_deps_for(CHAPTER_OUTLINE_WORKFLOW),
        request_id=uuid4().hex[:8],
    )
    result, tokens = await run_workflow_to_result(CHAPTER_OUTLINE_STEP, frames)
    cleaned, dropped = validate_outline_ids(result, roster)
    truncation = {
        "truncated_sections": list(context.truncated_sections),
        "dropped_item_counts": dict(context.dropped_item_counts),
    }
    return cleaned, dropped, tokens, truncation


async def generate_prose(novel_id: str, chapter: Dict[str, Any]) -> Tuple[str, int, dict]:
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
    prompt = (
        prompts[f"{PROSE_STEP}_prompt_base"].format(
            context=context.to_prompt_text(),
            chapter_order=int(chapter.get("order_index") or 0),
            chapter_title=str(chapter.get("title") or ""),
            words_per_chapter=words,
        )
        + "\n" + prompts[f"{PROSE_STEP}_prompt_without_schema_suffix"]
    )
    service = get_llm_service_for_step(PROSE_WORKFLOW, PROSE_STEP)
    frames = stream_prose(
        workflow_name=PROSE_WORKFLOW, step_key=PROSE_STEP, prompt=prompt,
        service=service, gen_kwargs={}, request_id=uuid4().hex[:8],
    )
    text, tokens = await _consume_prose_frames(frames)
    truncation = {
        "truncated_sections": list(context.truncated_sections),
        "dropped_item_counts": dict(context.dropped_item_counts),
    }
    return text, tokens, truncation


async def generate_state(novel_id: str, chapter: Dict[str, Any]) -> Tuple[dict, dict, int, dict]:
    chapter_id = str(chapter["_id"])
    inputs = await fetch_context_inputs(novel_id, chapter_id)
    context = assemble_context(inputs)
    roster = inputs["roster"]
    # 正文必须重新查库取：调用方传入的 chapter 是 chapter_pipeline.run_chapter
    # 的入口快照，只用于 skip-existing 判断。同一轮内，本步之前的
    # generate_prose/write_prose 很可能刚把正文写进了库，而入口快照仍是写入前
    # 的空正文——不重新查库会把空文本喂给状态回填工作流，让摘要/人物状态/伏笔
    # 推进全部基于"没有正文"生成（state_router.extract_chapter_state_by_ai 同样
    # 是先查库拿 content，从不信任调用方快照）。fetch_context_inputs 不产出
    # content 字段（它只为 assemble_context 服务），故这里单独查一次章节。
    fresh_chapter = await chapter_repo.get_chapter_by_id(chapter_id)
    params = {
        "context": context.to_prompt_text(),
        "chapter_order": int(chapter.get("order_index") or 0),
        "chapter_title": str(chapter.get("title") or ""),
        "chapter_content": str(fresh_chapter.get("content") or "").strip(),
    }
    frames = run_workflow(
        workflow_name=STATE_WORKFLOW, steps=CHAPTER_STATE_STEPS,
        prompts=load_prompt_config().get(CHAPTER_STATE_PROMPT_NAME, {}),
        params=params, gen_kwargs={}, cached={}, deps=_deps_for(STATE_WORKFLOW),
        request_id=uuid4().hex[:8],
    )
    result, tokens = await run_workflow_to_result(STATE_STEP, frames)
    cleaned, dropped = validate_state_ids(result, roster)
    truncation = {
        "truncated_sections": list(context.truncated_sections),
        "dropped_item_counts": dict(context.dropped_item_counts),
    }
    return cleaned, dropped, tokens, truncation


async def _accept_outline(chapter_id: str, result: dict) -> None:
    await ChapterService.accept_chapter_outline(chapter_id, result)


async def _write_prose(chapter_id: str, text: str) -> None:
    await ChapterService.update_chapter(chapter_id, {"content": text})


async def _accept_state(chapter_id: str, accept_payload: dict) -> dict:
    return await ChapterStateService.accept_chapter_state(chapter_id, accept_payload)


def build_chapter_pipeline_deps() -> ChapterPipelineDeps:
    return ChapterPipelineDeps(
        generate_outline=generate_outline, generate_prose=generate_prose, generate_state=generate_state,
        accept_outline=_accept_outline, write_prose=_write_prose, accept_state=_accept_state,
    )
