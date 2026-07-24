"""AI 创建小说路由：通过 4 步 LLM 管道从用户创意生成完整小说设定（SSE 流式状态推送）。"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, AsyncGenerator, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.api.llm_routers._common import (
    GenerationParamsMixin,
    build_gen_kwargs,
    safe_novel_text,
)
from backend.db.errors import InvalidIdError, NotFoundError
from backend.db.repositories.novel_repository import novel_repo
from backend.llm.config import get_llm_config, get_provider_config
from backend.services.llm.workflow_runner import (
    WorkflowDeps,
    WorkflowStep,
    run_workflow,
)
from backend.services.llm.generation_runtime import (
    ExplicitProviderTarget,
    PromptPlan,
    WorkflowStepTarget,
    create_generation_runtime,
    create_workflow_runtime,
)
from backend.services.llm.llm_service import LLMService
from backend.services.novel.faction_service import FactionService
from backend.llm.prompts.prompt_selector import (
    CORE_FACTIONS_PROMPT_NAME,
    REWRITE_NOVEL_FIELD_PROMPT_NAME,
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

NovelRewriteFieldKey = Literal[
    "title",
    "subtitle",
    "genre",
    "tags",
    "plot",
    "core_idea",
    "tone",
    "target_audience",
    "introduction",
    "summary",
    "core_seed",
    "worldview",
    "writing_style",
    "narrative_pov",
    "era_background",
]

REWRITABLE_NOVEL_FIELDS: set[str] = {
    "title",
    "subtitle",
    "genre",
    "tags",
    "plot",
    "core_idea",
    "tone",
    "target_audience",
    "introduction",
    "summary",
    "core_seed",
    "worldview",
    "writing_style",
    "narrative_pov",
    "era_background",
}

REWRITE_CONTEXT_FIELDS: tuple[str, ...] = (
    "title",
    "subtitle",
    "genre",
    "tags",
    "tone",
    "target_audience",
    "core_idea",
    "core_seed",
    "writing_style",
    "narrative_pov",
    "era_background",
    "number_of_chapters",
    "words_per_chapter",
)

FIELD_LABELS: dict[str, str] = {
    "title": "标题",
    "subtitle": "副标题",
    "genre": "类型",
    "tags": "标签",
    "plot": "主线剧情",
    "core_idea": "核心创意",
    "tone": "基调",
    "target_audience": "目标读者",
    "introduction": "引言",
    "summary": "简介",
    "core_seed": "核心种子",
    "worldview": "世界观",
    "writing_style": "写作风格",
    "narrative_pov": "叙事视角",
    "era_background": "时代背景",
}

NARRATIVE_POV_VALUES: set[str] = {"第一人称", "第三人称有限视角", "全知视角"}
TAG_SPLIT_RE = re.compile(r"[\n,，、;；]+")


def _load_prompts() -> dict:
    """读取当前生效的 prompt 定义文件。"""
    return load_prompt_config()


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
    words_per_chapter: int = 3000
    cached_steps: AICreateCachedSteps | None = None


class NovelRewriteChatMessage(BaseModel):
    """单条创建态字段改写对话消息。"""

    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=8000)


class NovelFieldRewriteRequest(BaseModel):
    """创建态字段改写请求。"""

    provider: str = Field(..., min_length=1)
    target_field: NovelRewriteFieldKey
    instruction: str = Field(..., min_length=1, max_length=4000)
    current_value: str | list[str] = ""
    context: dict[str, Any] = Field(default_factory=dict)
    chat_history: list[NovelRewriteChatMessage] = Field(default_factory=list)


class NovelFieldRewriteResult(BaseModel):
    """创建态字段改写结果。"""

    target_field: NovelRewriteFieldKey
    value: str | list[str]


class GenerateCoreFactionsRequest(GenerationParamsMixin):
    """基于已保存小说生成核心阵营预览的请求。"""

    novel_id: str = Field(..., min_length=1)


def _validate_rewrite_provider(provider: str):
    """校验指定 Provider 能否用于创建态字段改写。

    Args:
        provider: 前端选择的 Provider 别名。

    Returns:
        已解析的 Provider 配置。

    Raises:
        HTTPException: Provider 不存在或未启用时抛出 400。
    """
    alias = provider.strip()
    llm_cfg = get_llm_config()
    if alias not in llm_cfg.providers:
        raise HTTPException(status_code=400, detail=f"Provider 不存在: {alias}")

    provider_config = get_provider_config(alias)
    if not provider_config.enabled:
        raise HTTPException(status_code=400, detail=f"Provider 未启用: {alias}")

    return provider_config


def _format_rewrite_value(value: str | list[str]) -> str:
    """将字段值格式化为提示词中的可读文本。

    Args:
        value: 当前字段值，标签字段可能是字符串列表。

    Returns:
        可直接放入提示词的文本。
    """
    if isinstance(value, list):
        return "、".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def _compact_rewrite_context(context: dict[str, Any], target_field: str) -> dict[str, Any]:
    """过滤创建草稿上下文，只保留用户可见且非目标字段的小说创建字段。

    Args:
        context: 前端提交的完整创建草稿上下文。
        target_field: 当前正在改写的目标字段。

    Returns:
        供 LLM 参考的上下文字典。
    """
    compact: dict[str, Any] = {}
    for field in REWRITE_CONTEXT_FIELDS:
        # 目标字段已经通过 current_value 独立传入，避免同一长文本在 prompt 中重复出现。
        if field != target_field and field in context:
            compact[field] = context[field]
    return compact


def _format_rewrite_history(history: list[NovelRewriteChatMessage]) -> str:
    """将字段历史对话压缩成提示词片段。

    Args:
        history: 当前目标字段的历史消息列表。

    Returns:
        可读的历史对话文本；无历史时返回占位说明。
    """
    if not history:
        return "无"

    lines: list[str] = []
    for message in history[-12:]:
        role_label = "用户" if message.role == "user" else "AI"
        lines.append(f"{role_label}: {message.content.strip()}")
    return "\n".join(lines)


def _build_rewrite_prompt(req: NovelFieldRewriteRequest, *, use_json_schema: bool) -> str:
    """构造创建态字段改写提示词。

    Args:
        req: 字段改写请求模型。
        use_json_schema: 当前 Provider 是否支持结构化输出。

    Returns:
        发送给 LLM 的完整提示词。
    """
    prompts = _load_prompts().get(REWRITE_NOVEL_FIELD_PROMPT_NAME, {})
    field_label = FIELD_LABELS[req.target_field]
    context_json = json.dumps(
        _compact_rewrite_context(req.context, req.target_field),
        ensure_ascii=False,
        indent=2,
    )
    current_value = _format_rewrite_value(req.current_value)
    history_text = _format_rewrite_history(req.chat_history)
    suffix_key = (
        "rewrite_novel_field_prompt_with_schema_suffix"
        if use_json_schema
        else "rewrite_novel_field_prompt_without_schema_suffix"
    )

    prompt_base = prompts["rewrite_novel_field_prompt_base"].format(
        target_field_label=field_label,
        target_field=req.target_field,
        instruction=req.instruction.strip(),
        current_value=current_value or "无",
        context_json=context_json,
        history_text=history_text,
    )
    prompt_suffix = prompts[suffix_key].format(target_field=req.target_field)
    return f"{prompt_base}\n{prompt_suffix}".strip()


def _normalize_rewrite_value(target_field: NovelRewriteFieldKey, value: str | list[str]) -> str | list[str]:
    """归一化 LLM 返回的字段值。

    Args:
        target_field: 当前改写目标字段。
        value: LLM 返回的原始字段值。

    Returns:
        可直接返回给前端并写入表单的字段值。

    Raises:
        ValueError: 返回值为空或不满足字段约束时抛出。
    """
    if target_field not in REWRITABLE_NOVEL_FIELDS:
        raise ValueError(f"不支持改写字段: {target_field}")

    if target_field == "tags":
        raw_items = value if isinstance(value, list) else TAG_SPLIT_RE.split(str(value))
        tags: list[str] = []
        for item in raw_items:
            tag = str(item).strip()
            if tag and tag not in tags:
                tags.append(tag)
        if not tags:
            raise ValueError("标签改写结果不能为空")
        return tags[:8]

    if isinstance(value, list):
        text = "\n".join(str(item).strip() for item in value if str(item).strip())
    else:
        text = str(value).strip()

    if not text:
        raise ValueError(f"{FIELD_LABELS[target_field]}改写结果不能为空")

    if target_field == "narrative_pov" and text not in NARRATIVE_POV_VALUES:
        allowed = "、".join(sorted(NARRATIVE_POV_VALUES))
        raise ValueError(f"叙事视角只能为: {allowed}")

    return text


def _normalize_rewrite_result(
    target_field: NovelRewriteFieldKey,
    result: NovelFieldRewriteResult,
) -> NovelFieldRewriteResult:
    """校验并归一化完整改写结果。

    Args:
        target_field: 请求中的目标字段。
        result: LLM 返回并解析后的改写结果。

    Returns:
        字段一致且值已归一化的改写结果。

    Raises:
        ValueError: 字段不一致或字段值非法时抛出。
    """
    if result.target_field != target_field:
        raise ValueError(f"AI 返回字段不一致: {result.target_field}")

    return NovelFieldRewriteResult(
        target_field=target_field,
        value=_normalize_rewrite_value(target_field, result.value),
    )


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
    runtime = create_generation_runtime()
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
async def rewrite_novel_field(req: NovelFieldRewriteRequest):
    """使用指定 Provider 改写创建态小说信息中的单个字段。

    Args:
        req: 前端提交的字段改写请求。

    Returns:
        包含目标字段和改写后字段值的响应字典。
    """
    _validate_rewrite_provider(req.provider)
    runtime = create_generation_runtime()
    plan = runtime.plan_structured(ExplicitProviderTarget(req.provider))
    request_id = uuid4().hex[:8]
    logger.info(
        "[rewrite_novel_field] request_id=%s provider=%s field=%s json_schema=%s",
        request_id,
        req.provider,
        req.target_field,
        plan.mode.value,
    )

    try:
        generated = await runtime.generate_structured(
            plan,
            NovelFieldRewriteResult,
            PromptPlan(
                native_schema_prompt=_build_rewrite_prompt(req, use_json_schema=True),
                prompt_json_prompt=_build_rewrite_prompt(req, use_json_schema=False),
            ),
        )
        parsed_result = NovelFieldRewriteResult.model_validate(generated.value.model_dump())
        normalized_result = _normalize_rewrite_result(req.target_field, parsed_result)
        return normalized_result.model_dump()
    except HTTPException:
        raise
    except ValueError as exc:
        logger.warning(
            "[rewrite_novel_field] request_id=%s invalid_result=%s",
            request_id,
            exc,
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception(
            "[rewrite_novel_field] request_id=%s failed provider=%s field=%s",
            request_id,
            req.provider,
            req.target_field,
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc


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
            runtime=create_workflow_runtime(),
        )
        async for frame in run_workflow(
            workflow_name=WORKFLOW_NAME,
            steps=AI_CREATE_STEPS,
            prompts=_load_prompts().get(WORKFLOW_NAME, {}),
            params={
                "user_idea": req.user_idea,
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

