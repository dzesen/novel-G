"""AI 状态回填路由：正文 → 章摘要 + 伏笔推进 + 人物状态 + 一致性报告。

生成端只持久化租约与候选；叙事状态落库只发生在 accept 端点。
与 outline_router 一致，依赖以 WorkflowDeps 在 event_stream 内装配，
使测试可 monkeypatch 本模块的全局名。
"""

from __future__ import annotations

from dataclasses import replace
from typing import AsyncGenerator
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from backend.api.llm_routers._common import (
    GenerationParamsMixin,
    build_gen_kwargs,
)
from backend.db.errors import InvalidIdError, NotFoundError
from backend.db.mutation import MutationConflictError
from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.repositories.novel_repository import novel_repo
from backend.llm.config import get_llm_config, get_provider_config
from backend.llm.prompts.prompt_selector import load_prompt_config
from backend.services.generation.chapter_generation_application import (
    AcceptanceAuthority,
    ChapterGenerationApplicationDeps,
    ChapterGenerationApplicationService,
    CHAPTER_STATE_STEPS,
    PartialProseRequiresCompletion,
    StateGenerationCommand,
    STATE_STEP,
    STATE_WORKFLOW,
)
from backend.services.llm.context_builder import (
    ContextBudgetError,
    assemble_context,
    estimate_tokens,
    fetch_context_inputs,
)
from backend.services.llm.workflow_runner import (
    run_workflow,
    sse_comment,
    sse_event,
)
from backend.services.llm.generation_runtime import create_workflow_runtime
from backend.services.llm.workflow_service import (
    resolve_provider_for_step,
)
from backend.services.novel.state_proposal import (
    StaleStatePreview,
    state_proposal_module,
)

from backend.api.default_routers.auth_router import require_owned_body_resource

router = APIRouter(
    prefix="/api/llm",
    tags=["llm"],
    dependencies=[Depends(require_owned_body_resource)],
)
def _load_prompts() -> dict:
    return load_prompt_config()


def _chapter_generation_service() -> ChapterGenerationApplicationService:
    def create_runtime(*, attempt_scope=None, **kwargs):
        del attempt_scope
        return create_workflow_runtime(**kwargs)

    production = ChapterGenerationApplicationDeps.production()
    return ChapterGenerationApplicationService(
        replace(
            production,
            novel_repo=novel_repo,
            chapter_repo=chapter_repo,
            fetch_context_inputs=fetch_context_inputs,
            assemble_context=assemble_context,
            create_runtime=create_runtime,
            run_workflow=run_workflow,
            load_prompts=_load_prompts,
            state_proposals=state_proposal_module,
            resolve_provider=resolve_provider_for_step,
            get_provider_config=get_provider_config,
            estimate_tokens=estimate_tokens,
            log_partial_on_disconnect=(
                get_llm_config().log_partial_result_on_disconnect
            ),
        )
    )


class ChapterStateRequest(GenerationParamsMixin):
    novel_id: str = Field(..., min_length=1)
    chapter_id: str = Field(..., min_length=1)


@router.post("/extract-chapter-state-by-ai")
async def extract_chapter_state_by_ai(req: ChapterStateRequest, request: Request):
    """通过统一应用服务生成状态提案；本路由只授予预览权限。"""
    try:
        execution = await _chapter_generation_service().execute(
            StateGenerationCommand(
                novel_id=req.novel_id,
                chapter_id=req.chapter_id,
                authority=AcceptanceAuthority.PREVIEW,
                generation_params={
                    **build_gen_kwargs(req),
                    "allow_failure_retry": req.allow_failure_retry,
                },
                request_id=uuid4().hex[:8],
                is_disconnected=request.is_disconnected,
            )
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PartialProseRequiresCompletion as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (InvalidIdError, ContextBudgetError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    async def event_stream() -> AsyncGenerator[str, None]:
        async for event in execution:
            if event.name == "keepalive":
                yield sse_comment("keepalive")
            else:
                yield sse_event(event.name, event.data)

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
    proposal_id: str = Field(..., min_length=1)
    acceptance_token: str = Field(..., min_length=1)
    selected_fact_ids: list[str] = Field(default_factory=list)
    selected_thread_ids: list[str] = Field(default_factory=list)
    edits: dict = Field(default_factory=dict)


@router.post("/accept-chapter-state")
async def accept_chapter_state(req: AcceptChapterStateRequest):
    """接受状态回填：写章摘要、回填人物状态、推进伏笔状态。"""
    try:
        return await state_proposal_module.accept(
            chapter_id=req.chapter_id,
            proposal_id=req.proposal_id,
            acceptance_token=req.acceptance_token,
            selected_fact_ids=req.selected_fact_ids,
            selected_thread_ids=req.selected_thread_ids,
            edits=req.edits,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except InvalidIdError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except StaleStatePreview as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except MutationConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
