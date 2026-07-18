"""AI 大纲预览路由：分卷大纲（本链）与章节细纲（2a-2b）经单步 LLM 工作流产出预览。

与 create_novel_router 一致，依赖以 WorkflowDeps 在 event_stream 内装配，使测试可
monkeypatch 本模块的全局名。本路由从不写数据库——落库在域路由（volume_router）。
"""

from __future__ import annotations

import logging
from typing import Any, AsyncGenerator, Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.db.errors import InvalidIdError, NotFoundError
from backend.db.repositories.novel_repository import novel_repo
from backend.llm.config import get_llm_config, get_provider_config
from backend.llm.prompts.prompt_selector import VOLUME_OUTLINE_PROMPT_NAME, load_prompt_config
from backend.llm.schemas.novel_pydantic import VolumeOutlineResultSchema
from backend.services.llm.format_review_service import validate_and_fix_format
from backend.services.llm.workflow_runner import WorkflowDeps, WorkflowStep, run_workflow
from backend.services.llm.workflow_service import (
    get_llm_service_for_step,
    resolve_provider_for_step,
    resolve_timeout_for_step,
)

router = APIRouter(prefix="/api/llm", tags=["llm"])
logger = logging.getLogger(__name__)

VOLUME_OUTLINE_WORKFLOW = "create_volume_outline_by_ai"


def _load_prompts() -> dict:
    return load_prompt_config()


def _check_json_schema_support(step_name: str, workflow_name: str = VOLUME_OUTLINE_WORKFLOW) -> bool:
    provider = resolve_provider_for_step(workflow_name, step_name)
    if not provider:
        return False
    return get_provider_config(provider).supports_json_schema


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


class VolumeOutlineRequest(BaseModel):
    novel_id: str = Field(..., min_length=1)
    temperature: Optional[float] = Field(default=None, ge=0, le=2)
    top_p: Optional[float] = Field(default=None, ge=0, le=1)
    max_tokens: Optional[int] = Field(default=None, gt=0)
    presence_penalty: Optional[float] = Field(default=None, ge=-2, le=2)
    frequency_penalty: Optional[float] = Field(default=None, ge=-2, le=2)
    system_prompt: Optional[str] = Field(default=None)


def _build_gen_kwargs(req: Any) -> dict:
    kwargs: dict = {}
    for key in ("temperature", "top_p", "max_tokens", "presence_penalty", "frequency_penalty", "system_prompt"):
        val = getattr(req, key)
        if val is not None:
            kwargs[key] = val
    return kwargs


def _safe(novel: dict, field: str, fallback: str = "未提供") -> str:
    value = novel.get(field)
    if isinstance(value, list):
        return "、".join(str(v).strip() for v in value if str(v).strip()) or fallback
    return str(value or "").strip() or fallback


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
        "title": _safe(novel, "title"),
        "genre": _safe(novel, "genre", "未分类"),
        "tone": _safe(novel, "tone"),
        "core_idea": _safe(novel, "core_idea"),
        "core_seed": _safe(novel, "core_seed"),
        "summary": _safe(novel, "summary"),
        "worldview": _safe(novel, "worldview"),
        "plot": _safe(novel, "plot"),
    }

    async def event_stream() -> AsyncGenerator[str, None]:
        deps = WorkflowDeps(
            resolve_provider=resolve_provider_for_step,
            resolve_timeout=resolve_timeout_for_step,
            get_service=get_llm_service_for_step,
            supports_schema=_check_json_schema_support,
            fix_format=validate_and_fix_format,
        )
        async for frame in run_workflow(
            workflow_name=VOLUME_OUTLINE_WORKFLOW,
            steps=VOLUME_OUTLINE_STEPS,
            prompts=_load_prompts().get(VOLUME_OUTLINE_PROMPT_NAME, {}),
            params=params,
            gen_kwargs=_build_gen_kwargs(req),
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
