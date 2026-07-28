"""Approach A：无头驱动现有 LLM 工作流，产出结构化结果供批量引擎用（设计 §4.2）。

不重构已上线工作流：直接调 run_workflow/stream_prose，内部消费其 SSE 帧取结果，
再调服务层 accept。装配（fetch_context_inputs / assemble_* / WorkflowDeps）复制自
outline_router / prose_router / state_router 的开流前设置——那几处是模块级、可复用。
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, AsyncGenerator, Callable, Dict, Tuple
from uuid import uuid4

from backend.llm.prompts.prompt_selector import (
    CHAPTER_OUTLINE_PROMPT_NAME,
    CHAPTER_STATE_PROMPT_NAME,
    OUTLINE_ADHERENCE_PROMPT_NAME,
    PROSE_PROMPT_NAME,
    load_prompt_config,
)
from backend.llm.schemas.novel_pydantic import (
    ChapterOutlineAdherenceResultSchema,
)
from backend.services.llm.context_builder import (
    assemble_context,
    assemble_outline_context,
    fetch_context_inputs,
    outline_selection_roster,
)
from backend.services.llm.agent_orchestrator import apply_agent_profile
from backend.services.llm.prose_runner import stream_prose
from backend.services.llm.workflow_runner import (
    WorkflowDeps, WorkflowFailed, parse_sse_event, run_workflow, run_workflow_to_result,
)
from backend.services.llm.generation_runtime import (
    AttemptScope,
    PromptPlan,
    WorkflowStepTarget,
    create_generation_runtime,
)
from backend.services.novel.state_validation import (
    resolve_outline_character_references,
    resolve_state_character_references,
    state_reference_resolution,
    validate_state_ids,
)
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
from backend.services.generation.outline_adherence import (
    normalize_outline_adherence,
)
from backend.services.generation.job_planner import (
    REUSABLE_STATE_COMPLETION_STATUSES,
)
from backend.services.generation.prose_completion import prose_completion_module
from backend.services.generation.prose_generation import execute_prose_plan
from backend.services.generation.prose_runs import prose_run_module
from backend.db.repositories.prose_run_repository import prose_run_repo
from backend.services.novel.chapter_service import ChapterService
from backend.services.novel.state_proposal import (
    SelectAllPolicy,
    state_proposal_module,
)
from backend.services.novel.state_completion import prose_is_eligible_for_state
from backend.services.novel.state_completion import chapter_content_digest
from backend.services.novel.style_controls import render_style_controls
from backend.db.utils import get_utc_now

CHAPTER_OUTLINE_STEP = CHAPTER_OUTLINE_STEPS[0].key


_GENERATION_OVERRIDE_KEYS = frozenset({
    "temperature",
    "top_p",
    "max_tokens",
    "presence_penalty",
    "frequency_penalty",
    "system_prompt",
})


def _generation_options(
    generation_params: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, int]]:
    values = dict(generation_params or {})
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


def estimate_chapter_attempt_slots(
    chapter: Dict[str, Any],
    generation_params: Mapping[str, Any] | None = None,
) -> int:
    """按当前不可变 GenerationPlan 计算一章的最大语义调用数。"""
    overrides, runtime_kwargs = _generation_options(generation_params)
    runtime = create_generation_runtime(**runtime_kwargs)
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
            slots += prose_plan.call_count
        else:
            # 细纲尚未生成，场景数和逐场景预算未知。预留有界的保守容量，
            # 生成出细纲后实际调用仍受每章 reservation 约束，不可无限扩张。
            slots += 32
    if (
        str(
            (chapter.get("state_completion") or {}).get("status")
            or "missing"
        )
        not in REUSABLE_STATE_COMPLETION_STATUSES
    ):
        # 细纲符合度审查与状态回填共用 continuity Provider，但各自是一次
        # 独立的结构化语义调用，容量必须分别预留。
        slots += runtime.plan_structured(
            WorkflowStepTarget(STATE_WORKFLOW, STATE_STEP)
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
        WorkflowStepTarget(STATE_WORKFLOW, STATE_STEP)
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


def _deps_for(
    workflow_name: str,
    attempt_scope: AttemptScope | None = None,
    generation_params: Mapping[str, Any] | None = None,
) -> WorkflowDeps:
    del workflow_name
    _overrides, runtime_kwargs = _generation_options(generation_params)
    return WorkflowDeps(
        runtime=create_generation_runtime(
            attempt_scope=attempt_scope,
            **runtime_kwargs,
        ),
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
    generation_params: Mapping[str, Any] | None = None,
) -> tuple[dict, dict, int, dict, list[dict[str, Any]]]:
    inputs = await fetch_context_inputs(novel_id, str(chapter["_id"]))
    context = assemble_outline_context(inputs)
    roster = outline_selection_roster(
        inputs["roster"],
        context.selectable_worldbook_card_ids,
    )
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
        "style_controls": render_style_controls(novel.get("style_controls")),
        "words_per_chapter": novel.get("words_per_chapter") or 3000,
    }
    gen_kwargs, _runtime_kwargs = _generation_options(generation_params)
    deps = _deps_for(
        CHAPTER_OUTLINE_WORKFLOW,
        attempt_scope,
        generation_params,
    )
    frames = run_workflow(
        workflow_name=CHAPTER_OUTLINE_WORKFLOW, steps=CHAPTER_OUTLINE_STEPS,
        prompts=load_prompt_config().get(CHAPTER_OUTLINE_PROMPT_NAME, {}),
        params=params, gen_kwargs=gen_kwargs, cached={}, deps=deps,
        request_id=uuid4().hex[:8],
    )
    result, tokens = await run_workflow_to_result(CHAPTER_OUTLINE_STEP, frames)
    resolved, remapped = resolve_outline_character_references(result, roster)
    cleaned, dropped = validate_outline_ids(resolved, roster)
    truncation = {
        "truncated_sections": list(context.truncated_sections),
        "dropped_item_counts": dict(context.dropped_item_counts),
    }
    return (
        cleaned,
        dropped,
        tokens,
        truncation,
        _serialize_attempts(deps.runtime),
        remapped,
    )


async def generate_prose(
    novel_id: str,
    chapter: Dict[str, Any],
    attempt_scope: AttemptScope | None = None,
    generation_params: Mapping[str, Any] | None = None,
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
            style_controls=render_style_controls(novel.get("style_controls")),
            words_per_chapter=words,
        )
        + "\n" + prompts[f"{PROSE_STEP}_prompt_without_schema_suffix"]
    )
    gen_kwargs, runtime_kwargs = _generation_options(generation_params)
    runtime = create_generation_runtime(
        attempt_scope=attempt_scope,
        **runtime_kwargs,
    )
    plan = runtime.plan_text(WorkflowStepTarget(PROSE_WORKFLOW, PROSE_STEP))
    execution_plan = prose_completion_module.plan(
        outline=outline,
        target_word_count=int(words),
        provider_capability={
            "max_output_tokens": plan.max_output_tokens,
            "model": plan.provider_model,
        },
        request_overrides=gen_kwargs,
    )
    owner_id = str(novel.get("owner_id") or "")
    if not owner_id:
        raise ValueError("小说缺少 owner_id，无法创建用户隔离的正文草稿")
    active = await prose_run_module.inspect_active(
        owner_id=owner_id,
        chapter_id=str(chapter["_id"]),
        outline=outline,
        context_text=context.to_prompt_text(),
    )
    if active is not None and active.get("status") == "stale":
        active = None
    run_document = await prose_run_module.begin(
        owner_id=owner_id,
        novel_id=novel_id,
        chapter_id=str(chapter["_id"]),
        outline=outline,
        context_text=context.to_prompt_text(),
        plan=execution_plan,
        provider_plan={
            "provider_alias": plan.provider_alias,
            "provider_model": plan.provider_model,
            "config_revision": plan.config_revision,
        },
        run_id=str(active["_id"]) if active is not None else None,
        expected_revision=int(active.get("revision") or 0) if active is not None else None,
        confirm_uncertain_retry=bool(
            getattr(attempt_scope, "confirm_uncertain_retry", False)
        ),
        replace_exhausted=True,
    )
    latest_run = run_document

    def stream_call(call_prompt: str, call_kwargs: dict):
        return runtime.stream_text(plan, call_prompt, **call_kwargs)

    def finish_reason_reader():
        return runtime.last_finish_reason

    def usage_reader():
        attempts = runtime.attempts
        return attempts[-1].usage if attempts else runtime.usage

    async def on_segment(segment: dict) -> None:
        nonlocal latest_run
        lease = latest_run.get("lease") or {}
        latest_run = await prose_run_repo.append_segment(
            run_id=str(latest_run["_id"]),
            owner_id=owner_id,
            lease_token=str(lease.get("token") or ""),
            segment=segment,
        )

    try:
        generated = await execute_prose_plan(
            plan=execution_plan,
            outline=outline,
            base_prompt=prompt,
            stream_call=stream_call,
            finish_reason_reader=finish_reason_reader,
            usage_reader=usage_reader,
            outline_revision=str(run_document["outline_revision"]),
            gen_kwargs=gen_kwargs,
            existing_segments=list(run_document.get("segments") or []),
            confirm_uncertain_retry=bool(
                getattr(attempt_scope, "confirm_uncertain_retry", False)
            ),
            on_segment=on_segment,
        )
        lease = latest_run.get("lease") or {}
        latest_run = await prose_run_repo.finish(
            run_id=str(latest_run["_id"]),
            owner_id=owner_id,
            lease_token=str(lease.get("token") or ""),
            status=(
                "complete"
                if generated.completion.can_write_formal_prose
                else generated.completion.status
            ),
            completion=generated.completion.to_dict(),
            assembled_text=generated.text,
        )
    except BaseException:
        await prose_run_repo.mark_status(
            run_id=str(latest_run["_id"]),
            owner_id=owner_id,
            status="incomplete",
        )
        raise
    truncation = {
        "truncated_sections": list(context.truncated_sections),
        "dropped_item_counts": dict(context.dropped_item_counts),
    }
    completion = {
        **generated.completion.to_dict(),
        "source_run_id": str(latest_run["_id"]),
        "source_run_revision": int(latest_run.get("revision") or 0),
        "source_run_digest": chapter_content_digest(generated.text),
    }
    return (
        generated.text,
        generated.usage.total_tokens,
        truncation,
        _serialize_attempts(runtime),
        completion,
    )


async def generate_state(
    novel_id: str,
    chapter: Dict[str, Any],
    attempt_scope: AttemptScope | None = None,
    generation_params: Mapping[str, Any] | None = None,
) -> tuple[dict, dict, int, dict, list[dict[str, Any]]]:
    chapter_id = str(chapter["_id"])
    fresh_chapter = await chapter_repo.get_chapter_by_id(chapter_id)
    if not prose_is_eligible_for_state(fresh_chapter):
        raise ValueError(
            "本章正文尚未完整接受；请先补写并标记完成，不能执行状态回填"
        )
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
        gen_kwargs, _runtime_kwargs = _generation_options(generation_params)
        deps = _deps_for(
            STATE_WORKFLOW,
            attempt_scope,
            generation_params,
        )
        await state_proposal_module.ensure_current(generation_snapshot)
        frames = run_workflow(
            workflow_name=STATE_WORKFLOW, steps=CHAPTER_STATE_STEPS,
            prompts=load_prompt_config().get(CHAPTER_STATE_PROMPT_NAME, {}),
            params=params, gen_kwargs=gen_kwargs, cached={}, deps=deps,
            request_id=uuid4().hex[:8],
        )
        result, tokens = await run_workflow_to_result(STATE_STEP, frames)
        await state_proposal_module.ensure_current(generation_snapshot)
        resolved, remapped = resolve_state_character_references(result, roster)
        cleaned, dropped = validate_state_ids(resolved, roster)
        attempts = _serialize_attempts(deps.runtime)
        reference_resolution = state_reference_resolution(
            result,
            cleaned,
            dropped,
            remapped,
        )
        proposal = await state_proposal_module.publish(
            generation_lease,
            cleaned,
            audit={
                "usage": {"total_tokens": tokens},
                "attempts": attempts,
                "reference_resolution": reference_resolution,
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


async def generate_outline_adherence(
    novel_id: str,
    chapter: Dict[str, Any],
    attempt_scope: AttemptScope | None = None,
    generation_params: Mapping[str, Any] | None = None,
) -> tuple[dict, int, dict, list[dict[str, Any]]]:
    """在状态回填前审查正文是否落实细纲与当前卷弧线。"""

    chapter_id = str(chapter["_id"])
    fresh_chapter = await chapter_repo.get_chapter_by_id(chapter_id)
    content = str(fresh_chapter.get("content") or "").strip()
    if not content:
        raise ValueError("本章尚无可供细纲符合度检查的正文")
    if not fresh_chapter.get("outline"):
        raise ValueError("本章尚无可供细纲符合度检查的章节细纲")

    inputs = await fetch_context_inputs(novel_id, chapter_id)
    context = assemble_context(inputs)
    prompts = load_prompt_config().get(OUTLINE_ADHERENCE_PROMPT_NAME, {})
    prompt_args = {
        "context": context.to_prompt_text(),
        "chapter_order": int(fresh_chapter.get("order_index") or 0),
        "chapter_title": str(fresh_chapter.get("title") or ""),
        "chapter_content": content,
    }
    prompt_base = prompts["outline_adherence_prompt_base"].format(
        **prompt_args
    )
    native_prompt = apply_agent_profile(
        "continuity_editor",
        prompt_base
        + "\n"
        + prompts["outline_adherence_prompt_with_schema_suffix"],
    )
    prompt_json = apply_agent_profile(
        "continuity_editor",
        prompt_base
        + "\n"
        + prompts["outline_adherence_prompt_without_schema_suffix"],
    )
    gen_kwargs, runtime_kwargs = _generation_options(generation_params)
    runtime = create_generation_runtime(
        attempt_scope=attempt_scope,
        **runtime_kwargs,
    )
    plan = runtime.plan_structured(
        WorkflowStepTarget(STATE_WORKFLOW, STATE_STEP)
    )
    try:
        generated = await runtime.generate_structured(
            plan,
            ChapterOutlineAdherenceResultSchema,
            PromptPlan(
                native_schema_prompt=native_prompt,
                prompt_json_prompt=prompt_json,
            ),
            **gen_kwargs,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise WorkflowFailed(
            str(exc),
            usage=runtime.usage.model_dump(),
            attempts=_serialize_attempts(runtime),
        ) from exc
    result = normalize_outline_adherence(generated.value.model_dump())
    truncation = {
        "truncated_sections": list(context.truncated_sections),
        "dropped_item_counts": dict(context.dropped_item_counts),
    }
    return (
        result,
        generated.usage.total_tokens,
        truncation,
        _serialize_attempts(runtime),
    )


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


async def _write_prose(
    chapter_id: str,
    text: str,
    completion: dict[str, Any],
) -> None:
    await ChapterService.update_chapter(
        chapter_id,
        {
            "content": text,
            "prose_acceptance": {
                "state": "ai_complete",
                "content_digest": chapter_content_digest(text),
                "accepted_at": get_utc_now(),
                "source": "batch_generation",
                "source_run_id": completion.get("source_run_id"),
                "source_run_revision": completion.get(
                    "source_run_revision"
                ),
                "source_run_digest": completion.get("source_run_digest"),
                "completion_status": completion.get("status"),
                "finish_reason": completion.get("finish_reason"),
            },
        },
    )


async def _accept_state(chapter_id: str, proposal: dict) -> dict:
    return await state_proposal_module.run_auto(
        chapter_id=chapter_id,
        proposal=proposal,
        policy=SelectAllPolicy(),
    )


def build_chapter_pipeline_deps(
    attempt_scope_factory: Callable[[str], AttemptScope] | None = None,
    *,
    generation_params: Mapping[str, Any] | None = None,
) -> ChapterPipelineDeps:
    def scope(step: str) -> AttemptScope | None:
        return attempt_scope_factory(step) if attempt_scope_factory is not None else None

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
        generate_state=lambda novel_id, chapter: generate_state(
            novel_id,
            chapter,
            scope("state"),
            generation_params,
        ),
        accept_outline=_accept_outline, write_prose=_write_prose, accept_state=_accept_state,
    )
