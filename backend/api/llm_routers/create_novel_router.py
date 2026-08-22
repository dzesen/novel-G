"""AI 创建小说路由：通过 4 步 LLM 管道从用户创意生成完整小说设定（SSE 流式状态推送）。"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncGenerator
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.api.llm_routers._common import (
    GenerationParamsMixin,
    build_gen_kwargs,
    build_runtime_kwargs,
    safe_novel_text,
)
from backend.db.errors import InvalidIdError, NotFoundError
from backend.db.repositories.novel_repository import novel_repo
from backend.llm.config import get_llm_config
from backend.services.llm.workflow_runner import (
    WorkflowDeps,
    WorkflowStep,
    run_workflow,
)
from backend.services.llm.generation_runtime import (
    PromptPlan,
    WorkflowStepTarget,
    create_generation_runtime,
    create_workflow_runtime,
)
from backend.services.llm.llm_service import LLMService
from backend.services.llm.agent_orchestrator import CreativeDirectionSelection
from backend.services.novel.faction_service import FactionService
from backend.llm.prompts.prompt_selector import (
    CORE_FACTIONS_PROMPT_NAME,
    load_prompt_config,
)
from backend.llm.schemas.novel_pydantic import (
    ExpandIdeaSchema,
    ExtractIdeaSchema,
    CoreSeedSchema,
    CoreFactionsResultSchema,
    NovelMetaSchema,
)

from backend.api.default_routers.auth_router import require_owned_body_resource

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


AI_CREATE_STEPS: tuple[WorkflowStep, ...] = (
    WorkflowStep(
        key="expand_idea",
        # 唯一一个两套词汇不同名的步骤：另外三步恰好同名，这个区别极易被忽略。
        config_key="expand_idea_to_full_novel_story",
        schema=ExpandIdeaSchema,
        prompt_args=lambda ctx: {"user_idea": ctx.params["user_idea"]},
    ),
    WorkflowStep(
        key="extract_idea",
        schema=ExtractIdeaSchema,
        prompt_args=lambda ctx: {"plot": ctx.results["expand_idea"].plot},
    ),
    WorkflowStep(
        key="core_seed",
        schema=CoreSeedSchema,
        prompt_args=lambda ctx: {
            "plot": ctx.results["expand_idea"].plot,
            "genre": ctx.results["extract_idea"].genre,
            "tone": ctx.results["extract_idea"].tone,
            "target_audience": ctx.results["extract_idea"].target_audience,
            "core_idea": ctx.results["extract_idea"].core_idea,
            "number_of_chapters": ctx.params["number_of_chapters"],
            "words_per_chapter": ctx.params["words_per_chapter"],
        },
    ),
    WorkflowStep(
        key="novel_meta",
        schema=NovelMetaSchema,
        prompt_args=lambda ctx: {
            "plot": ctx.results["expand_idea"].plot,
            "genre": ctx.results["extract_idea"].genre,
            "tone": ctx.results["extract_idea"].tone,
            "target_audience": ctx.results["extract_idea"].target_audience,
            "core_idea": ctx.results["extract_idea"].core_idea,
            "number_of_chapters": ctx.params["number_of_chapters"],
            "words_per_chapter": ctx.params["words_per_chapter"],
            "core_seed": ctx.results["core_seed"].core_seed,
        },
    ),
)

AI_CREATE_STEP_ORDER: tuple[str, ...] = tuple(step.key for step in AI_CREATE_STEPS)


class AICreateCachedSteps(BaseModel):
    """AI 创建小说流程的可复用步骤缓存。

    Args:
        expand_idea: 已完成的扩写完整剧情结果。
        extract_idea: 已完成的提炼创意结果。
        core_seed: 已完成的故事核心结果。
        novel_meta: 已完成的小说设定结果。

    Returns:
        请求体中的缓存步骤会被 Pydantic 校验为对应 schema 实例。
    """

    expand_idea: ExpandIdeaSchema | None = None
    extract_idea: ExtractIdeaSchema | None = None
    core_seed: CoreSeedSchema | None = None
    novel_meta: NovelMetaSchema | None = None


def _get_contiguous_cached_steps(cached_steps: AICreateCachedSteps | None) -> dict[str, BaseModel]:
    """读取从第一步开始连续存在的缓存步骤。

    Args:
        cached_steps: 前端传入的可选缓存步骤。

    Returns:
        只包含连续前缀的缓存字典；中间断档后的缓存会被忽略，避免错误续跑。
    """
    if cached_steps is None:
        return {}

    prefix: dict[str, BaseModel] = {}
    for step_name in AI_CREATE_STEP_ORDER:
        cached_value = getattr(cached_steps, step_name)
        if cached_value is None:
            break
        # 只信任连续前缀，后续步骤即使传入也会从断点重新生成。
        prefix[step_name] = cached_value
    return prefix


class AICreateNovelRequest(GenerationParamsMixin):
    user_idea: str
    number_of_chapters: int = 100
    words_per_chapter: int = Field(default=3000, ge=500, le=50000)
    creative_direction: CreativeDirectionSelection | None = None
    cached_steps: AICreateCachedSteps | None = None


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
    return f"{prompt_base}\n{prompts[suffix_key]}".strip()


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


@router.post("/create-novel-by-ai")
async def create_novel_by_ai(req: AICreateNovelRequest, request: Request):
    """4 步 LLM 管道（SSE 流式）：expand_idea → extract_idea → core_seed → novel_meta。

    Args:
        req: AI 创建小说请求，允许携带从第一步开始连续完成的 cached_steps。
        request: 用于检测客户端断开，断开时会取消正在跑的 LLM 调用。

    Returns:
        SSE 响应；每一步通过 step 事件推送，结束时通过 done 事件返回结果或部分结果。
    """

    async def event_stream() -> AsyncGenerator[str, None]:
        # 依赖在此处装配而非模块级：这些名字在测试中会被 monkeypatch 到本模块上，
        # 调用时再取才能拿到替身。
        deps = WorkflowDeps(
            runtime=create_workflow_runtime(**build_runtime_kwargs(req)),
        )
        async for frame in run_workflow(
            workflow_name=WORKFLOW_NAME,
            steps=AI_CREATE_STEPS,
            prompts=_load_prompts().get(WORKFLOW_NAME, {}),
            params={
                "user_idea": _build_creation_idea(
                    req.user_idea,
                    req.creative_direction,
                ),
                "number_of_chapters": req.number_of_chapters,
                "words_per_chapter": req.words_per_chapter,
            },
            gen_kwargs=build_gen_kwargs(req),
            cached=_get_contiguous_cached_steps(req.cached_steps),
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
