"""AI 大纲预览路由：分卷大纲与章节细纲经单步 LLM 工作流产出预览。

与 create_novel_router 一致，依赖以 WorkflowDeps 在 event_stream 内装配，使测试可
monkeypatch 本模块的全局名。本路由从不写数据库——落库在域路由
（volume_router / chapter_router）。
"""

from __future__ import annotations

import logging
from typing import AsyncGenerator, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import Field

from backend.api.llm_routers._common import (
    GenerationParamsMixin,
    build_gen_kwargs,
    safe_novel_text,
)
from backend.db.errors import InvalidIdError, NotFoundError
from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.repositories.novel_repository import novel_repo
from backend.db.utils import to_object_id
from backend.llm.config import get_llm_config
from backend.llm.prompts.prompt_selector import (
    CHAPTER_OUTLINE_PROMPT_NAME,
    VOLUME_OUTLINE_PROMPT_NAME,
    load_prompt_config,
)
from backend.llm.schemas.novel_pydantic import ChapterOutlineResultSchema, VolumeOutlineResultSchema
from backend.services.llm.context_builder import (
    ContextBudgetError,
    assemble_outline_context,
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
from backend.services.novel.outline_validation import validate_outline_ids
from backend.services.novel.state_validation import (
    resolve_outline_character_references,
)

from backend.api.default_routers.auth_router import require_owned_body_resource

router = APIRouter(
    prefix="/api/llm",
    tags=["llm"],
    dependencies=[Depends(require_owned_body_resource)],
)
logger = logging.getLogger(__name__)

VOLUME_OUTLINE_WORKFLOW = "create_volume_outline_by_ai"
CHAPTER_OUTLINE_WORKFLOW = "create_chapter_outline_by_ai"


def _load_prompts() -> dict:
    return load_prompt_config()


VOLUME_OUTLINE_STEPS: tuple[WorkflowStep, ...] = (
    WorkflowStep(
        key="volume_outline",
        schema=VolumeOutlineResultSchema,
        prompt_args=lambda ctx: {
            "number_of_chapters": ctx.params["number_of_chapters"],
            "title": ctx.params["title"],
            "genre": ctx.params["genre"],
            "tone": ctx.params["tone"],
            "core_idea": ctx.params["core_idea"],
            "core_seed": ctx.params["core_seed"],
            "summary": ctx.params["summary"],
            "worldview": ctx.params["worldview"],
            "plot": ctx.params["plot"],
        },
    ),
)

CHAPTER_OUTLINE_STEPS: tuple[WorkflowStep, ...] = (
    WorkflowStep(
        key="chapter_outline",
        schema=ChapterOutlineResultSchema,
        agent_id="chapter_planner",
        prompt_args=lambda ctx: {
            "context": ctx.params["context"],
            "chapter_order": ctx.params["chapter_order"],
            "chapter_title": ctx.params["chapter_title"],
            "words_per_chapter": ctx.params["words_per_chapter"],
        },
    ),
)


class VolumeOutlineRequest(GenerationParamsMixin):
    novel_id: str = Field(..., min_length=1)


class ChapterOutlineRequest(GenerationParamsMixin):
    novel_id: str = Field(..., min_length=1)
    chapter_id: str = Field(..., min_length=1)


def _extract_chapter_outline(parsed) -> Optional[dict]:
    """从一帧解析结果里取出细纲数据；该帧不携带细纲则返回 None。

    **两类**帧携带它：单步的 step done（data 直接是细纲）与工作流 done
    （result.chapter_outline）。两处都要清洗——只清 done 的话，前端按 step 帧
    缓存/续跑时用的仍是未校验的原始 id。
    """
    if parsed is None:
        return None
    event, data = parsed
    if event == "step" and data.get("step") == "chapter_outline" and data.get("status") == "done":
        outline = data.get("data")
        return outline if isinstance(outline, dict) else None
    if event == "done" and data.get("success") and isinstance(data.get("result"), dict):
        outline = data["result"].get("chapter_outline")
        return outline if isinstance(outline, dict) else None
    return None


def _replace_chapter_outline(parsed, cleaned: dict) -> str:
    """把清洗后的细纲写回帧并重新序列化。只在 _extract_chapter_outline 命中时调用。"""
    event, data = parsed
    data = dict(data)
    if event == "step":
        data["data"] = cleaned
    else:
        result = dict(data["result"])
        result["chapter_outline"] = cleaned
        data["result"] = result
    return sse_event(event, data)


@router.post("/create-volume-outline-by-ai")
async def create_volume_outline_by_ai(req: VolumeOutlineRequest, request: Request):
    """基于已保存小说设定生成分卷大纲预览（SSE）。不写数据库。"""
    try:
        novel = await novel_repo.get_novel_by_id(req.novel_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except InvalidIdError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    params = {
        "number_of_chapters": novel.get("number_of_chapters") or 100,
        "title": safe_novel_text(novel, "title"),
        "genre": safe_novel_text(novel, "genre", "未分类"),
        "tone": safe_novel_text(novel, "tone"),
        "core_idea": safe_novel_text(novel, "core_idea"),
        "core_seed": safe_novel_text(novel, "core_seed"),
        "summary": safe_novel_text(novel, "summary"),
        "worldview": safe_novel_text(novel, "worldview"),
        "plot": safe_novel_text(novel, "plot"),
    }

    async def event_stream() -> AsyncGenerator[str, None]:
        deps = WorkflowDeps(
            runtime=create_workflow_runtime(),
        )
        async for frame in run_workflow(
            workflow_name=VOLUME_OUTLINE_WORKFLOW,
            steps=VOLUME_OUTLINE_STEPS,
            prompts=_load_prompts().get(VOLUME_OUTLINE_PROMPT_NAME, {}),
            params=params,
            gen_kwargs=build_gen_kwargs(req),
            cached={},
            deps=deps,
            request_id=uuid4().hex[:8],
            is_disconnected=request.is_disconnected,
            log_partial_on_disconnect=get_llm_config().log_partial_result_on_disconnect,
        ):
            yield frame

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/create-chapter-outline-by-ai")
async def create_chapter_outline_by_ai(req: ChapterOutlineRequest, request: Request):
    """基于细纲上下文包（含 roster）生成章节细纲预览（SSE）。不写数据库。"""
    try:
        novel = await novel_repo.get_novel_by_id(req.novel_id)
        chapter = await chapter_repo.get_chapter_by_id(req.chapter_id)
        if chapter.get("novel_id") != to_object_id(req.novel_id):
            raise HTTPException(status_code=400, detail="该章节不属于指定小说")
        # 取数与装配在开流**之前**完成：一旦开始 streaming，状态码已经发出，
        # ContextBudgetError 就只能变成流里的一条错误帧。设计 §6 的示意代码把
        # build_context 画在 event_stream 内，本实现前移一步，理由如上；§6 真正
        # 要求的"截断在 LLM 调用之前上报"仍然成立——context 帧是流的第一帧。
        #
        # 这里不用 build_outline_context，是因为还需要同一次取数里的 roster
        # （§5.3 的 id 校验用）。build_outline_context 保留为便捷入口。
        inputs = await fetch_context_inputs(req.novel_id, req.chapter_id)
        context = assemble_outline_context(inputs)
    except HTTPException:
        raise
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except InvalidIdError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ContextBudgetError as exc:
        # roster 有界（设计 §4.4）：永不截断档自身超预算时明确失败并指名原因，
        # 不静默截断 roster——截了它 AI 就吐不出合法 id。
        raise HTTPException(status_code=400, detail=str(exc))

    params = {
        "context": context.to_prompt_text(),
        "chapter_order": int(chapter.get("order_index") or 0),
        "chapter_title": str(chapter.get("title") or ""),
        "words_per_chapter": novel.get("words_per_chapter") or 3000,
    }

    roster = inputs["roster"]

    async def event_stream() -> AsyncGenerator[str, None]:
        if context.truncated_sections or context.dropped_item_counts:
            # 欠账 #1 / 设计 §6：截断在 LLM 调用之前就已知，故在这里立刻告知前端，
            # 而不是挂到 step done 事件上（那是 usage 的路——用量只有调用完才知道）。
            # 挂到 done 上等于让用户白等 90 秒才被告知"这一章是在信息不全的情况下
            # 写的"，而那正是该提前中止的时刻。执行器因此不必学会"上下文包"这个概念。
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
        reported_remapped = False
        async for frame in run_workflow(
            workflow_name=CHAPTER_OUTLINE_WORKFLOW,
            steps=CHAPTER_OUTLINE_STEPS,
            prompts=_load_prompts().get(CHAPTER_OUTLINE_PROMPT_NAME, {}),
            params=params,
            gen_kwargs=build_gen_kwargs(req),
            cached={},
            deps=deps,
            request_id=uuid4().hex[:8],
            is_disconnected=request.is_disconnected,
            log_partial_on_disconnect=get_llm_config().log_partial_result_on_disconnect,
        ):
            parsed = parse_sse_event(frame)
            outline = _extract_chapter_outline(parsed)
            if outline is None:
                yield frame
                continue
            # 设计 §5.3：AI 返回的每个 id 必须在 roster 内，不在则剔除并**明确上报**。
            # 不上报的话，"AI 漏了个人物"会以"预览里少一行"的形式无声通过，而
            # preview-then-accept 的全部意义就是让人拿最后一道关。
            resolved, remapped = resolve_outline_character_references(
                outline,
                roster,
            )
            cleaned, dropped = validate_outline_ids(resolved, roster)
            if remapped and not reported_remapped:
                yield sse_event("id_remapping", {"remapped": remapped})
                reported_remapped = True
            if dropped and not reported:
                yield sse_event("id_validation", {"dropped": dropped})
                reported = True
            yield _replace_chapter_outline(parsed, cleaned)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
