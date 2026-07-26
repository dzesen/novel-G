"""AI 正文生成路由：单步流式工作流，产出 token 增量。**从不写数据库**。

接受动作由前端写进编辑器草稿，再由既有自动保存落库（设计 §2）——本阶段
刻意没有 accept 端点，因此也没有 2a 那套四层防御、409 与级联担忧。

与 outline_router 一致，依赖在 event_stream 内取用，使测试可 monkeypatch
本模块的全局名。
"""

from __future__ import annotations

import logging
from typing import AsyncGenerator
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import Field

from backend.api.llm_routers._common import GenerationParamsMixin, build_gen_kwargs
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
from backend.services.llm.workflow_runner import sse_event
from backend.services.llm.workflow_service import get_llm_service_for_step
from backend.services.llm.generation_runtime import WorkflowStepTarget, create_generation_runtime

from backend.api.default_routers.auth_router import require_owned_body_resource

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


@router.post("/write-chapter-by-ai")
async def write_chapter_by_ai(req: ProseRequest, request: Request):
    """基于已接受的章细纲与正文模式上下文包，流式生成本章正文。不写数据库。"""
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
            runtime = create_generation_runtime()
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

        async for frame in stream_prose(
            workflow_name=PROSE_WORKFLOW,
            step_key=PROSE_STEP,
            prompt=prompt,
            service=service,
            gen_kwargs=gen_kwargs,
            request_id=request_id,
            is_disconnected=request.is_disconnected,
            runtime=runtime,
            generation_plan=plan,
        ):
            yield frame

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
