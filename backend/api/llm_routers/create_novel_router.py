"""AI 创建小说路由：通过 4 步 LLM 管道从用户创意生成完整小说设定（SSE 流式状态推送）。"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import Field

from backend.api.llm_routers._common import (
    GenerationParamsMixin,
    build_gen_kwargs,
    build_runtime_kwargs,
    safe_novel_text,
)
from backend.db.errors import InvalidIdError, NotFoundError
from backend.db.repositories.novel_repository import novel_repo
from backend.services.llm.generation_runtime import (
    PromptPlan,
    WorkflowStepTarget,
    create_generation_runtime,
    create_workflow_runtime,
)
from backend.services.llm.agent_orchestrator import CreativeDirectionSelection
from backend.services.novel.faction_service import FactionService
from backend.services.generation.author_brief import (
    novel_author_brief,
)
from backend.llm.prompts.prompt_selector import (
    CORE_FACTIONS_PROMPT_NAME,
    load_prompt_config,
)
from backend.llm.schemas.novel_pydantic import (
    CoreFactionsResultSchema,
)

from backend.api.default_routers.auth_router import require_owned_body_resource
from backend.db.repositories.blueprint_run_repository import BlueprintRunConflict
from backend.services.generation.blueprint_runs import BlueprintRunService
from backend.services.generation.blueprint_workflow import (
    AI_CREATE_STEPS, AICreateNovelRequest, BlueprintGenerationRequest,
    BlueprintGenerationStartRequest, BlueprintResumeRequest,
)

router = APIRouter(
    prefix="/api/llm",
    tags=["llm"],
    dependencies=[Depends(require_owned_body_resource)],
)

WORKFLOW_NAME = "create_novel_by_ai"
FACTIONS_WORKFLOW_NAME = "create_factions_by_ai"
CREATE_CORE_FACTIONS_STEP_NAME = "create_core_factions"
logger = logging.getLogger(__name__)

def _load_prompts() -> dict:
    """读取当前生效的 prompt 定义文件。"""
    return load_prompt_config()


def _build_creation_idea(
    user_idea: str,
    creative_direction: CreativeDirectionSelection | None,
) -> str:
    """Combine the original idea with a user-confirmed direction."""
    if creative_direction is None:
        return user_idea

    direction = creative_direction.direction
    must_keep = "；".join(direction.must_keep) or "无额外条目"
    risks = "；".join(direction.risks) or "无额外条目"
    adjustments = creative_direction.user_adjustments.strip() or "无"
    return f"""【用户原始创意】
{user_idea.strip()}

【用户已确认的创意总监方向——后续四步必须遵守】
- 方向标题：{direction.title}
- 核心提案：{direction.pitch}
- 核心冲突：{direction.core_conflict}
- 主角成长弧：{direction.protagonist_arc}
- 长篇故事引擎：{direction.story_engine}
- 世界观钩子：{direction.world_hook}
- 基调与风格：{direction.tone_and_style}
- 必须保留：{must_keep}
- 已知风险：{risks}
- 用户补充调整：{adjustments}

执行约束：不得在扩写、提炼、核心种子或小说设定步骤中擅自改换上述方向；
如细节存在空白，应在不违背原始创意和已确认方向的前提下补全。""".strip()


class GenerateCoreFactionsRequest(GenerationParamsMixin):
    """基于已保存小说生成核心阵营预览的请求。"""

    novel_id: str = Field(..., min_length=1)


def _build_core_factions_prompt(novel: dict[str, Any], *, use_json_schema: bool) -> str:
    """构造全书核心阵营生成提示词。

    Args:
        novel: 已落库小说文档。
        use_json_schema: 当前 Provider 是否支持结构化输出。

    Returns:
        发送给 LLM 的完整提示词。
    """
    prompts = _load_prompts().get(CORE_FACTIONS_PROMPT_NAME, {})
    suffix_key = (
        "create_core_factions_prompt_with_schema_suffix"
        if use_json_schema
        else "create_core_factions_prompt_without_schema_suffix"
    )
    tags = novel.get("tags") if isinstance(novel.get("tags"), list) else []
    prompt_base = prompts["create_core_factions_prompt_base"].format(
        plot=safe_novel_text(novel, "plot"),
        genre=safe_novel_text(novel, "genre", "未分类"),
        tone=safe_novel_text(novel, "tone"),
        target_audience=safe_novel_text(novel, "target_audience"),
        core_idea=safe_novel_text(novel, "core_idea"),
        number_of_chapters=novel.get("number_of_chapters") or 100,
        words_per_chapter=novel.get("words_per_chapter") or 3000,
        core_seed=safe_novel_text(novel, "core_seed"),
        title=safe_novel_text(novel, "title"),
        summary=safe_novel_text(novel, "summary"),
        worldview=safe_novel_text(novel, "worldview"),
        writing_style=safe_novel_text(novel, "writing_style"),
        narrative_pov=safe_novel_text(novel, "narrative_pov"),
        era_background=safe_novel_text(novel, "era_background"),
        tags_json=json.dumps(tags, ensure_ascii=False),
    )
    brief = novel_author_brief(novel)
    return f"{brief.to_prompt_text() if brief else ''}\n{prompt_base}\n{prompts[suffix_key]}".strip()


@router.post("/generate-core-factions")
async def generate_core_factions(req: GenerateCoreFactionsRequest):
    """基于已保存小说信息生成核心阵营预览，不写入数据库。

    Args:
        req: 生成核心阵营的请求参数。

    Returns:
        包含 core_factions 与 faction_relations 的预览结果。
    """
    request_id = uuid4().hex[:8]
    try:
        novel = await novel_repo.get_novel_by_id(req.novel_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except InvalidIdError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if await FactionService.has_core_factions_initialized(req.novel_id):
        raise HTTPException(status_code=409, detail="核心阵营已初始化，请改用手动新增或先清空核心势力与垃圾桶")

    step_name = CREATE_CORE_FACTIONS_STEP_NAME
    gen_kwargs = build_gen_kwargs(req)
    runtime = create_generation_runtime(**build_runtime_kwargs(req))
    plan = runtime.plan_structured(WorkflowStepTarget(FACTIONS_WORKFLOW_NAME, step_name))

    logger.info(
        "[generate_core_factions] request_id=%s novel_id=%s provider=%s json_schema=%s",
        request_id,
        req.novel_id,
        plan.provider_alias,
        plan.mode.value,
    )

    try:
        generated = await runtime.generate_structured(
            plan,
            CoreFactionsResultSchema,
            PromptPlan(
                native_schema_prompt=_build_core_factions_prompt(novel, use_json_schema=True),
                prompt_json_prompt=_build_core_factions_prompt(novel, use_json_schema=False),
            ),
            **gen_kwargs,
        )
        parsed_result = CoreFactionsResultSchema.model_validate(generated.value.model_dump())
        return parsed_result.model_dump()
    except ValueError as exc:
        logger.warning(
            "[generate_core_factions] request_id=%s invalid_result=%s",
            request_id,
            exc,
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception(
            "[generate_core_factions] request_id=%s failed provider=%s timeout=%s",
            request_id,
            plan.provider_alias,
            plan.timeout_seconds,
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/rewrite-novel-field")
async def rewrite_novel_field(req: dict[str, Any]):
    """保留一个兼容周期的退役端点，不再触发任何付费调用。

    Args:
        req: 前端提交的字段改写请求。

    Returns:
        此端点始终抛出 410。
    """
    del req
    raise HTTPException(
        status_code=410,
        detail={
            "code": "blueprint_field_rewrite_retired",
            "message": "逐字段 AI 改写已停用，请使用整份蓝图重新生成。",
        },
    )


def _blueprint_service() -> BlueprintRunService:
    return BlueprintRunService(runtime_factory=create_workflow_runtime, prompt_supplier=_load_prompts)


def _blueprint_http_error(error: Exception) -> HTTPException:
    if isinstance(error, InvalidIdError):
        return HTTPException(status_code=400, detail={"code": "blueprint_identity_invalid", "message": "蓝图运行 ID 无效。"})
    if isinstance(error, NotFoundError):
        return HTTPException(status_code=404, detail={"code": "blueprint_run_not_found", "message": "蓝图运行不存在。"})
    code = getattr(error, "code", "blueprint_plan_invalid")
    messages = {
        "blueprint_readiness_stale": "输入、Provider 或工作流计划已变化，请重新预检。",
        "blueprint_result_uncertain": "前次请求结果未能确认，已保留完成步骤。请核查运行记录后决定是否重新生成。",
        "blueprint_legacy_cache_not_authorized": "旧浏览器缓存没有绑定运行授权。请保留草稿并重新预检。",
        "blueprint_run_already_running": "这个蓝图运行正在执行，请回到原运行。",
        "blueprint_resume_required": "这个运行已经启动，请使用继续原运行。",
        "blueprint_new_readiness_required": "前次运行已结束，请基于保留的步骤重新预检。",
        "blueprint_source_run_must_stop": "请先停止来源运行，再使用它已完成的步骤。",
        "blueprint_source_input_changed": "原始要求或草稿归属已变化，不能复用该运行的步骤。",
        "automatic_token_budget_confirmation_required": "请先确认系统计算的 Token 消耗上界。",
        "blueprint_uncertain_source_confirmation_required": "请先确认前次未知请求可能已经产生费用。",
        "blueprint_token_bound_unproven": "当前 Provider 缺少可证明的 Token 上界。",
    }
    return HTTPException(status_code=409 if isinstance(error, BlueprintRunConflict) else 400,
        detail={"code": code, "message": messages.get(code, "蓝图预检或运行暂不可用，请检查运行记录后重试。")})


@router.post("/create-novel-by-ai/readiness")
@router.post("/regenerate-blueprint/readiness")
async def inspect_blueprint_regeneration_readiness(req: BlueprintGenerationRequest, request: Request) -> dict[str, Any]:
    """Both creation entries share zero-paid, owner/draft-scoped planning."""
    try:
        return await _blueprint_service().inspect(req, request.state.actor.id)
    except (ValueError, NotFoundError, InvalidIdError) as exc:
        raise _blueprint_http_error(exc) from exc


def _blueprint_stream(service: BlueprintRunService, run: dict, request: Request):
    return StreamingResponse(service.stream(run, is_disconnected=request.is_disconnected),
        media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/regenerate-blueprint")
@router.post("/create-novel-by-ai")
async def create_novel_by_ai(req: BlueprintGenerationStartRequest, request: Request):
    """Execute only the plan sealed by this owner/draft readiness."""
    service = _blueprint_service()
    try:
        run = await service.start(req, request.state.actor.id)
    except (ValueError, NotFoundError, InvalidIdError) as exc:
        raise _blueprint_http_error(exc) from exc
    return _blueprint_stream(service, run, request)


@router.get("/blueprint-runs")
async def list_blueprint_runs(request: Request, draft_id: str | None = None):
    service = _blueprint_service()
    runs = await service.repo.list_runs(request.state.actor.id, draft_id)
    return {"data": [service.public_view(run, summary=True) for run in runs]}


@router.get("/blueprint-runs/{run_id}")
async def get_blueprint_run(run_id: str, request: Request):
    service = _blueprint_service()
    try:
        return service.public_view(await service.repo.get_run(run_id, request.state.actor.id))
    except (ValueError, NotFoundError, InvalidIdError) as exc:
        raise _blueprint_http_error(exc) from exc


@router.post("/blueprint-runs/{run_id}/resume")
async def resume_blueprint_run(run_id: str, req: BlueprintResumeRequest, request: Request):
    service = _blueprint_service()
    try:
        run = await service.resume(run_id, request.state.actor.id, req.readiness_digest)
    except (ValueError, NotFoundError, InvalidIdError) as exc:
        raise _blueprint_http_error(exc) from exc
    return _blueprint_stream(service, run, request)


@router.post("/blueprint-runs/{run_id}/pause")
async def pause_blueprint_run(run_id: str, request: Request):
    service = _blueprint_service()
    try:
        return service.public_view(await service.repo.request_pause(run_id, request.state.actor.id))
    except (ValueError, NotFoundError, InvalidIdError) as exc:
        raise _blueprint_http_error(exc) from exc
