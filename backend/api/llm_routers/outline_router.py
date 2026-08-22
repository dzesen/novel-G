"""AI 大纲预览路由。

分卷大纲仍是本路由内的单步预览；章节细纲由统一章节生成应用服务执行，本路由
只负责 HTTP/SSE 适配，且只授予预览权限。
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
    build_runtime_kwargs,
)
from backend.db.errors import InvalidIdError, NotFoundError
from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.repositories.novel_repository import novel_repo
from backend.llm.config import get_llm_config
from backend.llm.prompts.prompt_selector import (
    VOLUME_OUTLINE_PROMPT_NAME,
    load_prompt_config,
)
from backend.services.generation.chapter_generation_application import (
    AcceptanceAuthority,
    ChapterGenerationApplicationDeps,
    ChapterGenerationApplicationService,
    CHAPTER_OUTLINE_STEPS,
    CHAPTER_OUTLINE_WORKFLOW,
    OutlineGenerationCommand,
)
from backend.services.generation.chapter_capability_registry import (
    build_chapter_capability_registry,
)
from backend.services.llm.capability_registry import CapabilityCall
from backend.services.llm.context_builder import (
    ContextBudgetError,
    assemble_outline_context,
    fetch_context_inputs,
)
from backend.services.llm.workflow_runner import (
    WorkflowDeps,
    run_workflow,
    sse_comment,
    sse_event,
)
from backend.services.llm.generation_runtime import create_workflow_runtime
from backend.services.llm.outline_generation import CHAPTER_OUTLINE_MAX_OUTPUT_TOKENS
from backend.services.novel.chapter_service import ChapterService
from backend.services.generation.volume_outline_generation import (
    VOLUME_OUTLINE_STEPS,
    VOLUME_OUTLINE_WORKFLOW,
    volume_outline_params,
)

from backend.api.default_routers.auth_router import require_owned_body_resource

router = APIRouter(
    prefix="/api/llm",
    tags=["llm"],
    dependencies=[Depends(require_owned_body_resource)],
)
logger = logging.getLogger(__name__)

def _load_prompts() -> dict:
    return load_prompt_config()

class VolumeOutlineRequest(GenerationParamsMixin):
    novel_id: str = Field(..., min_length=1)


class ChapterOutlineRequest(GenerationParamsMixin):
    novel_id: str = Field(..., min_length=1)
    chapter_id: str = Field(..., min_length=1)
    max_tokens: Optional[int] = Field(
        default=None,
        gt=0,
        le=CHAPTER_OUTLINE_MAX_OUTPUT_TOKENS,
    )


def _chapter_generation_service() -> ChapterGenerationApplicationService:
    """应用装配根；路由仅注入基础设施，不拥有章节生成规则。"""

    def create_runtime(*, attempt_scope=None, **kwargs):
        del attempt_scope
        return create_workflow_runtime(**kwargs)

    return ChapterGenerationApplicationService(
        ChapterGenerationApplicationDeps(
            novel_repo=novel_repo,
            chapter_repo=chapter_repo,
            fetch_context_inputs=fetch_context_inputs,
            assemble_outline_context=assemble_outline_context,
            create_runtime=create_runtime,
            run_workflow=run_workflow,
            load_prompts=_load_prompts,
            accept_outline=ChapterService.accept_chapter_outline,
            log_partial_on_disconnect=(
                get_llm_config().log_partial_result_on_disconnect
            ),
        )
    )


def _chapter_capability_registry():
    return build_chapter_capability_registry(
        service_factory=_chapter_generation_service,
    )


@router.post("/create-volume-outline-by-ai")
async def create_volume_outline_by_ai(req: VolumeOutlineRequest, request: Request):
    """基于已保存小说设定生成分卷大纲预览（SSE）。不写数据库。"""
    try:
        novel = await novel_repo.get_novel_by_id(req.novel_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except InvalidIdError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    params = volume_outline_params(novel)

    async def event_stream() -> AsyncGenerator[str, None]:
        deps = WorkflowDeps(
            runtime=create_workflow_runtime(**build_runtime_kwargs(req)),
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
    """通过统一应用服务生成章节细纲预览（SSE）；本路由无落库权限。"""
    request_id = uuid4().hex[:8]
    try:
        capability_stream = await _chapter_capability_registry().stream(
            "chapter_outline",
            OutlineGenerationCommand(
                novel_id=req.novel_id,
                chapter_id=req.chapter_id,
                authority=AcceptanceAuthority.PREVIEW,
                generation_params={
                    **build_gen_kwargs(req),
                    "allow_failure_retry": req.allow_failure_retry,
                },
                request_id=request_id,
                is_disconnected=request.is_disconnected,
            ),
            call=CapabilityCall(
                source="http",
                request_id=request_id,
                actor=getattr(request.state, "actor", None),
            ),
        )
    except HTTPException:
        raise
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except InvalidIdError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ContextBudgetError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    async def event_stream() -> AsyncGenerator[str, None]:
        async for event in capability_stream.events:
            if event.name == "keepalive":
                yield sse_comment("keepalive")
            else:
                yield sse_event(event.name, event.data)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
