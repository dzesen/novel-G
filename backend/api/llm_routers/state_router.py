"""AI 状态回填路由：正文 → 章摘要 + 伏笔推进 + 人物状态 + 一致性报告。

**预览端从不写数据库**（落库在本模块的 accept 端点，走 ChapterStateService）。
与 outline_router 一致，依赖以 WorkflowDeps 在 event_stream 内装配，
使测试可 monkeypatch 本模块的全局名。
"""

from __future__ import annotations

import logging
from typing import AsyncGenerator, Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from backend.api.llm_routers._common import GenerationParamsMixin, build_gen_kwargs
from backend.db.errors import InvalidIdError, NotFoundError
from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.repositories.novel_repository import novel_repo
from backend.db.utils import to_object_id
from backend.llm.config import get_llm_config, get_provider_config
from backend.llm.prompts.prompt_selector import CHAPTER_STATE_PROMPT_NAME, load_prompt_config
from backend.llm.schemas.novel_pydantic import ChapterStateAcceptSchema, ChapterStateResultSchema
from backend.services.llm.context_builder import (
    ContextBudgetError,
    assemble_context,
    estimate_tokens,
    fetch_context_inputs,
)
from backend.services.llm.workflow_runner import (
    WorkflowDeps,
    WorkflowStep,
    parse_sse_event,
    run_workflow,
    sse_event,
)
from backend.services.llm.generation_runtime import create_workflow_runtime
from backend.services.llm.workflow_service import (
    resolve_provider_for_step,
)
from backend.services.novel.chapter_state_service import ChapterStateService
from backend.services.novel.state_validation import validate_state_ids
from backend.services.novel.state_timeline import (
    StaleStatePreview,
    state_preview_store,
)

router = APIRouter(prefix="/api/llm", tags=["llm"])
logger = logging.getLogger(__name__)

STATE_WORKFLOW = "extract_chapter_state_by_ai"
STATE_STEP = "chapter_state"


def _load_prompts() -> dict:
    return load_prompt_config()


CHAPTER_STATE_STEPS: tuple[WorkflowStep, ...] = (
    WorkflowStep(
        key=STATE_STEP,
        schema=ChapterStateResultSchema,
        prompt_args=lambda ctx: {
            "context": ctx.params["context"],
            "chapter_order": ctx.params["chapter_order"],
            "chapter_title": ctx.params["chapter_title"],
            "chapter_content": ctx.params["chapter_content"],
        },
    ),
)


class ChapterStateRequest(GenerationParamsMixin):
    novel_id: str = Field(..., min_length=1)
    chapter_id: str = Field(..., min_length=1)


def _extract_chapter_state(parsed) -> Optional[dict]:
    """从一帧解析结果里取出回填数据；该帧不携带它则返回 None。

    **两类**帧携带它：单步的 step done（data 直接是回填数据）与工作流 done
    （result.chapter_state）。两处都要清洗——只清 done 的话，前端按 step 帧
    渲染时用的仍是未校验的原始 id。
    """
    if parsed is None:
        return None
    event, data = parsed
    if event == "step" and data.get("step") == STATE_STEP and data.get("status") == "done":
        payload = data.get("data")
        return payload if isinstance(payload, dict) else None
    if event == "done" and data.get("success") and isinstance(data.get("result"), dict):
        payload = data["result"].get(STATE_STEP)
        return payload if isinstance(payload, dict) else None
    return None


def _replace_chapter_state(parsed, cleaned: dict) -> str:
    """把清洗后的数据写回帧并重新序列化。只在 _extract_chapter_state 命中时调用。"""
    event, data = parsed
    data = dict(data)
    if event == "step":
        data["data"] = cleaned
    else:
        result = dict(data["result"])
        result[STATE_STEP] = cleaned
        data["result"] = result
    return sse_event(event, data)


@router.post("/extract-chapter-state-by-ai")
async def extract_chapter_state_by_ai(req: ChapterStateRequest, request: Request):
    """读本章正文与正文模式上下文包，生成状态回填预览（SSE）。不写数据库。"""
    # 全部前置校验在开流**之前**完成：一旦开始 streaming，状态码已经发出，
    # 这些错误就只能降级成流里的一条帧（沿用 2a-2b / 2b-1 的既定做法）。
    try:
        await novel_repo.get_novel_by_id(req.novel_id)
        chapter = await chapter_repo.get_chapter_by_id(req.chapter_id)
        if chapter.get("novel_id") != to_object_id(req.novel_id):
            raise HTTPException(status_code=400, detail="该章节不属于指定小说")
        content = str(chapter.get("content") or "").strip()
        if not content:
            # 库里没有正文就没有可回填的东西。前端在打开面板前会先 flush 草稿
            # （设计 §4.1），走到这里说明确实还没写或还没保存。
            raise HTTPException(
                status_code=400, detail="本章还没有已保存的正文，请先写好并保存正文"
            )
        inputs = await fetch_context_inputs(req.novel_id, req.chapter_id)
        # 用正文模式而非细纲模式：既有 permanent_facts 在正文模式的永不截断档里，
        # 而那正是一致性校验的判据基础，截掉它校验就变成瞎猜（设计 §4.2）。
        context = assemble_context(inputs)
        provider_alias = resolve_provider_for_step(STATE_WORKFLOW, STATE_STEP)
        provider_config = get_provider_config(provider_alias)
        estimated_input = estimate_tokens(context.to_prompt_text()) + estimate_tokens(content)
        reserved_output = int(req.max_tokens or getattr(provider_config, "max_tokens", None) or 4096)
        max_context_tokens = int(getattr(provider_config, "max_context_tokens", 128000))
        if estimated_input + reserved_output > max_context_tokens:
            raise HTTPException(
                status_code=400,
                detail=(
                    "本章正文与上下文预计超过模型窗口："
                    f"输入约 {estimated_input} tokens，输出预留 {reserved_output}，"
                    f"窗口 {max_context_tokens}。"
                    "请精简上下文或选择更大窗口的模型。"
                ),
            )
    except HTTPException:
        # 故意抛出的 400 必须先于下面的宽泛 handler，否则会被降级成别的码。
        raise
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except InvalidIdError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ContextBudgetError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    params = {
        "context": context.to_prompt_text(),
        "chapter_order": int(chapter.get("order_index") or 0),
        "chapter_title": str(chapter.get("title") or ""),
        # 正文不进 context_builder，也不会被截断；上方已把完整正文纳入模型窗口预检。
        "chapter_content": content,
    }
    roster = inputs["roster"]

    async def event_stream() -> AsyncGenerator[str, None]:
        preview_payload: dict | None = None
        if context.truncated_sections or context.dropped_item_counts:
            # 截断在 LLM 调用之前就已知，故立刻告知前端而不是挂到 step done 上
            # （那是 usage 的路）。沿用 2a 设计 §6。
            yield sse_event(
                "context",
                {
                    "truncated_sections": context.truncated_sections,
                    "dropped_item_counts": context.dropped_item_counts,
                },
            )

        deps = WorkflowDeps(
            runtime=create_workflow_runtime(),
        )
        reported = False
        async for frame in run_workflow(
            workflow_name=STATE_WORKFLOW,
            steps=CHAPTER_STATE_STEPS,
            prompts=_load_prompts().get(CHAPTER_STATE_PROMPT_NAME, {}),
            params=params,
            gen_kwargs=build_gen_kwargs(req),
            cached={},
            deps=deps,
            request_id=uuid4().hex[:8],
            is_disconnected=request.is_disconnected,
            log_partial_on_disconnect=get_llm_config().log_partial_result_on_disconnect,
        ):
            parsed = parse_sse_event(frame)
            payload = _extract_chapter_state(parsed)
            if payload is None:
                yield frame
                continue
            # AI 返回的每个 id 必须在 roster 内，不在则剔除并**明确上报**。
            # 不上报的话，"AI 认错了人"会以"预览里少一行"的形式无声通过，而
            # preview-then-accept 的全部意义就是让人拿最后一道关。
            cleaned, dropped = validate_state_ids(payload, roster)
            if dropped and not reported:
                yield sse_event("id_validation", {"dropped": dropped})
                reported = True
            if preview_payload is None:
                preview_payload = await state_preview_store.create(
                    req.novel_id,
                    req.chapter_id,
                    cleaned,
                )
            yield _replace_chapter_state(parsed, preview_payload)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class AcceptChapterStateRequest(BaseModel):
    """accept 端点入参：在 payload 之外多带一个 chapter_id。

    继承 ChapterStateAcceptSchema 而非重复其字段，故 extra="forbid" 一并继承——
    多余字段仍会被拒。传给服务层时要**去掉 chapter_id**，那是定位参数不是数据。
    """

    model_config = ConfigDict(extra="forbid")

    chapter_id: str = Field(..., min_length=1)
    preview_id: str | None = None
    acceptance_token: str | None = None
    selected_fact_ids: list[str] = Field(default_factory=list)
    selected_thread_ids: list[str] = Field(default_factory=list)
    edits: dict = Field(default_factory=dict)
    # 旧客户端兼容输入；新 UI 只发送 preview + selection IDs。
    summary: str | None = None
    character_updates: list[dict] = Field(default_factory=list)
    accepted_thread_updates: list[dict] = Field(default_factory=list)


@router.post("/accept-chapter-state")
async def accept_chapter_state(req: AcceptChapterStateRequest):
    """接受状态回填：写章摘要、回填人物状态、推进伏笔状态。"""
    try:
        if req.preview_id or req.acceptance_token:
            if not req.preview_id or not req.acceptance_token:
                raise StaleStatePreview("preview_id and acceptance_token are both required")
            payload, preview_meta = await state_preview_store.consume(
                chapter_id=req.chapter_id,
                preview_id=req.preview_id,
                acceptance_token=req.acceptance_token,
                selected_fact_ids=req.selected_fact_ids,
                selected_thread_ids=req.selected_thread_ids,
                edits=req.edits,
            )
        else:
            if req.summary is None:
                raise ValueError("summary is required for legacy state acceptance")
            payload = {
                "summary": req.summary,
                "character_updates": req.character_updates,
                "accepted_thread_updates": req.accepted_thread_updates,
            }
            preview_meta = {"source": "legacy_accept", "confidence": "unrated"}
        return await ChapterStateService.accept_chapter_state(
            req.chapter_id,
            payload,
            acceptance_metadata=preview_meta,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except InvalidIdError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except StaleStatePreview as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
