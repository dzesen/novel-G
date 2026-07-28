"""AI 正文生成路由：流式生成并持久化可恢复的 ``ProseRun`` 草稿。

生成阶段不会直接覆盖正式章节正文；每次 Provider 调用在派发前保存 uncertain
检查点，片段完成后再更新终态。只有独立 accept 端点在 owner、revision 与
完整性校验通过后，才以可恢复 mutation 写入 ``chapter.content``。

与 outline_router 一致，依赖在 event_stream 内取用，使测试可 monkeypatch
本模块的全局名。
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncGenerator
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import Field

from backend.api.llm_routers._common import (
    GenerationParamsMixin,
    build_gen_kwargs,
    build_runtime_kwargs,
)
from backend.db.errors import InvalidIdError, NotFoundError
from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.repositories.novel_repository import novel_repo
from backend.db.utils import to_object_id
from backend.llm.prompts.prompt_selector import PROSE_PROMPT_NAME, load_prompt_config
from backend.services.llm.context_builder import (
    ContextBudgetError,
    assemble_context,
    fetch_context_inputs,
)
from backend.services.llm.agent_orchestrator import apply_agent_profile
from backend.services.llm.prose_runner import stream_prose
from backend.services.llm.workflow_runner import parse_sse_event, sse_event
from backend.services.llm.workflow_service import get_llm_service_for_step
from backend.services.llm.generation_runtime import WorkflowStepTarget, create_generation_runtime
from backend.llm.models import TokenUsage
from backend.services.generation.prose_completion import prose_completion_module
from backend.services.generation.prose_generation import (
    ProseContinuationLimit,
    execute_prose_plan,
)
from backend.services.generation.prose_runs import (
    prose_run_module,
    serialize_prose_run,
)
from backend.services.novel.style_controls import render_style_controls
from backend.db.repositories.prose_run_repository import prose_run_repo

from backend.api.default_routers.auth_router import (
    require_owned_body_resource,
    require_owned_path_resource,
)

router = APIRouter(
    prefix="/api/llm",
    tags=["llm"],
    dependencies=[Depends(require_owned_body_resource)],
)
logger = logging.getLogger(__name__)

PROSE_WORKFLOW = "write_chapter_by_ai"
PROSE_STEP = "chapter_content"


def _load_prompts() -> dict:
    return load_prompt_config()


class ProseRequest(GenerationParamsMixin):
    novel_id: str = Field(..., min_length=1)
    chapter_id: str = Field(..., min_length=1)
    resume_run_id: str | None = Field(default=None, min_length=1)
    expected_run_revision: int | None = Field(default=None, ge=1)
    confirm_uncertain_retry: bool = False


class AcceptProseRunRequest(ProseRequest):
    expected_run_revision: int = Field(ge=1)
    accept_partial: bool = False
    partial_acknowledgement: bool = False


class DiscardProseRunRequest(ProseRequest):
    expected_run_revision: int = Field(ge=1)


@router.get(
    "/prose-runs/novel/{novel_id}/leftovers",
    dependencies=[Depends(require_owned_path_resource)],
)
async def list_leftover_prose_runs(novel_id: str, request: Request):
    actor = getattr(request.state, "actor", None)
    if actor is None:
        raise HTTPException(status_code=401, detail="需要登录")
    try:
        return await prose_run_module.list_leftovers(
            owner_id=str(actor.id),
            novel_id=novel_id,
        )
    except InvalidIdError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/prose-runs/chapter/{chapter_id}",
    dependencies=[Depends(require_owned_path_resource)],
)
async def inspect_active_prose_run(chapter_id: str, request: Request):
    try:
        chapter = await chapter_repo.get_chapter_by_id(chapter_id)
        inputs = await fetch_context_inputs(str(chapter["novel_id"]), chapter_id)
        context = assemble_context(inputs)
        actor = getattr(request.state, "actor", None)
        if actor is None:
            raise HTTPException(status_code=401, detail="需要登录")
        run = await prose_run_module.inspect_active(
            owner_id=str(actor.id),
            chapter_id=chapter_id,
            outline=chapter.get("outline") or {},
            context_text=context.to_prompt_text(),
        )
        return serialize_prose_run(run)
    except HTTPException:
        raise
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (InvalidIdError, ContextBudgetError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/prose-runs/{run_id}/accept")
async def accept_prose_run(
    run_id: str,
    req: AcceptProseRunRequest,
    request: Request,
):
    actor = getattr(request.state, "actor", None)
    if actor is None:
        raise HTTPException(status_code=401, detail="需要登录")
    try:
        return await prose_run_module.accept(
            owner_id=str(actor.id),
            run_id=run_id,
            chapter_id=req.chapter_id,
            expected_revision=req.expected_run_revision,
            accept_partial=req.accept_partial,
            partial_acknowledgement=req.partial_acknowledgement,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (InvalidIdError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/prose-runs/{run_id}/discard")
async def discard_prose_run(
    run_id: str,
    req: DiscardProseRunRequest,
    request: Request,
):
    actor = getattr(request.state, "actor", None)
    if actor is None:
        raise HTTPException(status_code=401, detail="需要登录")
    try:
        await prose_run_module.discard(
            owner_id=str(actor.id),
            novel_id=req.novel_id,
            chapter_id=req.chapter_id,
            run_id=run_id,
            expected_revision=req.expected_run_revision,
        )
        return {"discarded": True}
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (InvalidIdError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/write-chapter-by-ai")
async def write_chapter_by_ai(req: ProseRequest, request: Request):
    """流式生成正文并保存可恢复草稿，不直接覆盖正式章节正文。"""
    # 全部前置校验在开流**之前**完成：一旦开始 streaming，状态码已经发出，
    # 这些错误就只能降级成流里的一条帧（沿用 2a-2b 的既定偏离）。
    try:
        novel = await novel_repo.get_novel_by_id(req.novel_id)
        chapter = await chapter_repo.get_chapter_by_id(req.chapter_id)
        if chapter.get("novel_id") != to_object_id(req.novel_id):
            raise HTTPException(status_code=400, detail="该章节不属于指定小说")
        if not chapter.get("outline"):
            # 没有 outline，assemble_context 推导不出出场人物、待回收伏笔与 POV，
            # 上下文包退化成"核心设定 + 最近几章摘要"——一致性地基没了（设计 §6）。
            raise HTTPException(
                status_code=400, detail="本章还没有已接受的细纲，请先生成并接受章节细纲"
            )
        inputs = await fetch_context_inputs(req.novel_id, req.chapter_id)
        context = assemble_context(inputs)
    except HTTPException:
        # 故意抛出的 400 必须先于下面的宽泛 handler，否则会被降级成别的码。
        raise
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except InvalidIdError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ContextBudgetError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    prompts = _load_prompts().get(PROSE_PROMPT_NAME, {})
    # 字数目标优先取本章细纲的 target_word_count：那是 2a 细纲链写入、人可能在
    # 细纲面板里手动改过的**本章**决定，比小说级的 words_per_chapter 更具体。
    # 若只信小说级默认值，一章被人为改成 5000 字的细纲会和"约 3000 字"的指令
    # 同时喂给模型——同一次调用里两个矛盾的字数目标，且人的显式选择被静默吞掉。
    words_per_chapter = (
        (chapter.get("outline") or {}).get("target_word_count")
        or novel.get("words_per_chapter")
        or 3000
    )
    # 正文是纯文本，固定走 without_schema 后缀；本工作流从不请求 JSON Schema。
    prompt = apply_agent_profile(
        "chapter_writer",
        prompts[f"{PROSE_STEP}_prompt_base"].format(
            context=context.to_prompt_text(),
            chapter_order=int(chapter.get("order_index") or 0),
            chapter_title=str(chapter.get("title") or ""),
            style_controls=render_style_controls(novel.get("style_controls")),
            words_per_chapter=words_per_chapter,
        )
        + "\n"
        + prompts[f"{PROSE_STEP}_prompt_without_schema_suffix"]
    )
    gen_kwargs = build_gen_kwargs(req)
    request_id = uuid4().hex[:8]

    async def event_stream() -> AsyncGenerator[str, None]:
        if context.truncated_sections or context.dropped_item_counts:
            # 截断在 LLM 调用之前就已知，故立刻告知前端而不是等到结束——
            # 那正是用户该考虑提前中止的时刻（设计 §4，沿用 2a 设计 §6）。
            yield sse_event(
                "context",
                {
                    "truncated_sections": context.truncated_sections,
                    "dropped_item_counts": context.dropped_item_counts,
                },
            )

        try:
            runtime = create_generation_runtime(**build_runtime_kwargs(req))
            try:
                plan = runtime.plan_text(WorkflowStepTarget(PROSE_WORKFLOW, PROSE_STEP))
                service = None
            except ValueError:
                # 迁移兼容：测试/嵌入方可能仍通过旧 seam 注入临时 service。
                service = get_llm_service_for_step(PROSE_WORKFLOW, PROSE_STEP)
                runtime = None
                plan = None
        except Exception as exc:
            logger.exception(
                "[%s] request_id=%s failed to resolve service", PROSE_WORKFLOW, request_id
            )
            yield sse_event("done", {"success": False, "error": str(exc)})
            return

        provider_capability = {
            "max_output_tokens": (
                getattr(plan, "max_output_tokens", None)
                if plan is not None
                else None
            ),
            "model": getattr(plan, "provider_model", "") if plan is not None else "",
        }
        execution_plan = prose_completion_module.plan(
            outline=chapter.get("outline") or {},
            target_word_count=int(words_per_chapter),
            provider_capability=provider_capability,
            request_overrides=gen_kwargs,
        )
        yield sse_event("plan", execution_plan.to_dict())

        actor = getattr(request.state, "actor", None)
        owner_id = str(actor.id) if actor is not None else None
        run_document = None
        if owner_id:
            try:
                run_document = await prose_run_module.begin(
                    owner_id=owner_id,
                    novel_id=req.novel_id,
                    chapter_id=req.chapter_id,
                    outline=chapter.get("outline") or {},
                    context_text=context.to_prompt_text(),
                    plan=execution_plan,
                    provider_plan={
                        "provider_alias": (
                            getattr(plan, "provider_alias", "")
                            if plan is not None
                            else "legacy"
                        ),
                        "provider_model": provider_capability["model"],
                        "config_revision": (
                            getattr(plan, "config_revision", "")
                            if plan is not None
                            else ""
                        ),
                    },
                    run_id=req.resume_run_id,
                    expected_revision=req.expected_run_revision,
                    confirm_uncertain_retry=req.confirm_uncertain_retry,
                )
            except Exception as exc:
                yield sse_event("done", {
                    "success": False,
                    "error": str(exc),
                    "completion_status": "stale",
                })
                return
        if run_document is not None:
            yield sse_event("run", {
                "run_id": str(run_document["_id"]),
                "run_revision": int(run_document.get("revision") or 0),
            })

        queue: asyncio.Queue[str | None] = asyncio.Queue()
        latest_run = run_document

        def usage_reader():
            if runtime is not None:
                attempts = runtime.attempts
                return attempts[-1].usage if attempts else runtime.usage
            raw = getattr(service, "last_usage", None)
            if isinstance(raw, TokenUsage):
                return raw
            if hasattr(raw, "model_dump"):
                return TokenUsage.model_validate(raw.model_dump())
            return TokenUsage()

        def finish_reason_reader():
            return getattr(
                runtime if runtime is not None else service,
                "last_finish_reason",
                None,
            )

        def stream_call(call_prompt: str, call_kwargs: dict):
            async def consume():
                async for frame in stream_prose(
                    workflow_name=PROSE_WORKFLOW,
                    step_key=PROSE_STEP,
                    prompt=call_prompt,
                    service=service,
                    gen_kwargs=call_kwargs,
                    request_id=request_id,
                    is_disconnected=request.is_disconnected,
                    runtime=runtime,
                    generation_plan=plan,
                ):
                    parsed = parse_sse_event(frame)
                    if parsed is None:
                        continue
                    event, data = parsed
                    if event == "delta" and data.get("text"):
                        yield str(data["text"])
            return consume()

        async def on_delta(chunk: str) -> None:
            await queue.put(sse_event("delta", {"text": chunk}))

        async def on_segment(segment: dict) -> None:
            nonlocal latest_run
            if latest_run is None or owner_id is None:
                return
            lease = latest_run.get("lease") or {}
            latest_run = await prose_run_repo.append_segment(
                run_id=str(latest_run["_id"]),
                owner_id=owner_id,
                lease_token=str(lease.get("token") or ""),
                segment=segment,
            )

        async def produce() -> None:
            nonlocal latest_run
            try:
                result = await execute_prose_plan(
                    plan=execution_plan,
                    outline=chapter.get("outline") or {},
                    base_prompt=prompt,
                    stream_call=stream_call,
                    finish_reason_reader=finish_reason_reader,
                    usage_reader=usage_reader,
                    outline_revision=(
                        str((run_document or {}).get("outline_revision") or "ephemeral")
                    ),
                    gen_kwargs=gen_kwargs,
                    existing_segments=list((run_document or {}).get("segments") or []),
                    confirm_uncertain_retry=req.confirm_uncertain_retry,
                    on_delta=on_delta,
                    on_segment=on_segment,
                )
                if latest_run is not None and owner_id is not None:
                    lease = latest_run.get("lease") or {}
                    latest_run = await prose_run_repo.finish(
                        run_id=str(latest_run["_id"]),
                        owner_id=owner_id,
                        lease_token=str(lease.get("token") or ""),
                        status=(
                            "complete"
                            if result.completion.can_write_formal_prose
                            else result.completion.status
                        ),
                        completion=result.completion.to_dict(),
                        assembled_text=result.text,
                    )
                completion = result.completion.to_dict()
                payload = {
                    "success": result.completion.can_write_formal_prose,
                    "text": result.text,
                    "usage": result.usage.model_dump(),
                    "completion_status": result.completion.status,
                    "finish_reason": result.completion.finish_reason,
                    "requested_word_count": result.completion.requested_word_count,
                    "actual_word_count": result.completion.actual_word_count,
                    "raw_character_count": result.completion.raw_character_count,
                    "scene_count": result.completion.scene_count,
                    "completed_scene_count": result.completion.completed_scene_count,
                    "mode": result.completion.mode,
                    "reason_codes": list(result.completion.reason_codes),
                    "run_id": str(latest_run["_id"]) if latest_run else None,
                    "run_revision": int(latest_run.get("revision") or 0) if latest_run else None,
                }
                if not result.completion.can_write_formal_prose:
                    payload["error"] = "正文未满足完整性要求，已保留为可恢复草稿"
                await queue.put(sse_event("done", payload))
            except asyncio.CancelledError:
                if latest_run is not None and owner_id is not None:
                    await asyncio.shield(
                        prose_run_repo.mark_status(
                            run_id=str(latest_run["_id"]),
                            owner_id=owner_id,
                            status="incomplete",
                        )
                    )
                raise
            except Exception as exc:
                logger.exception(
                    "[%s] request_id=%s segmented prose failed",
                    PROSE_WORKFLOW,
                    request_id,
                )
                if latest_run is not None and owner_id is not None:
                    await prose_run_repo.mark_status(
                        run_id=str(latest_run["_id"]),
                        owner_id=owner_id,
                        status="incomplete",
                    )
                continuation_limited = isinstance(
                    exc,
                    ProseContinuationLimit,
                )
                await queue.put(sse_event("done", {
                    "success": False,
                    "error": str(exc),
                    "completion_status": "incomplete",
                    "reason_codes": [
                        (
                            "continuation_limit_reached"
                            if continuation_limited
                            else "uncertain_provider_attempt"
                        )
                    ],
                    "has_uncertain_attempt": not continuation_limited,
                    "run_id": str(latest_run["_id"]) if latest_run else None,
                    "run_revision": (
                        int(latest_run.get("revision") or 0)
                        if latest_run
                        else None
                    ),
                }))
            finally:
                await queue.put(None)

        producer = asyncio.create_task(produce())
        try:
            while True:
                frame = await queue.get()
                if frame is None:
                    break
                yield frame
        finally:
            if not producer.done():
                producer.cancel()
            try:
                await producer
            except asyncio.CancelledError:
                pass

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
