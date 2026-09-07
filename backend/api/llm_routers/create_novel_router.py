"""AI 创建小说路由：通过 4 步 LLM 管道从用户创意生成完整小说设定（SSE 流式状态推送）。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any, AsyncGenerator, Literal, Mapping
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator
from backend.novel_scale import ChapterCount, CreationIdea, WordsPerChapter

from backend.api.llm_routers._common import (
    GenerationParamsMixin,
    build_gen_kwargs,
    build_runtime_kwargs,
    safe_novel_text,
)
from backend.db.errors import InvalidIdError, NotFoundError
from backend.db.repositories.novel_repository import novel_repo
from backend.llm.config import get_llm_config
from backend.llm.models import TokenUsage
from backend.services.llm.workflow_runner import (
    WorkflowDeps,
    WorkflowStep,
    run_workflow,
)
from backend.services.llm.generation_runtime import (
    PromptPlan,
    AttemptUsage,
    GenerationPlan,
    WorkflowStepTarget,
    create_generation_runtime,
    create_workflow_runtime,
)
from backend.services.llm.llm_service import LLMService
from backend.services.llm.agent_orchestrator import CreativeDirectionSelection
from backend.services.novel.faction_service import FactionService
from backend.services.generation.author_brief import (
    AuthorConstraints, creation_author_brief, novel_author_brief, render_author_brief_record,
)
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
        prompt_context=lambda ctx: render_author_brief_record(ctx.params["author_brief"]),
        prompt_args=lambda ctx: {"user_idea": ctx.params["user_idea"]},
    ),
    WorkflowStep(
        key="extract_idea",
        schema=ExtractIdeaSchema,
        prompt_context=lambda ctx: render_author_brief_record(ctx.params["author_brief"]),
        prompt_args=lambda ctx: {"plot": ctx.results["expand_idea"].plot},
    ),
    WorkflowStep(
        key="core_seed",
        schema=CoreSeedSchema,
        prompt_context=lambda ctx: render_author_brief_record(ctx.params["author_brief"]),
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
        prompt_context=lambda ctx: render_author_brief_record(ctx.params["author_brief"]),
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
    user_idea: CreationIdea
    number_of_chapters: ChapterCount = 100
    words_per_chapter: WordsPerChapter = 3000
    creative_direction: CreativeDirectionSelection | None = None
    author_constraints: AuthorConstraints = Field(default_factory=AuthorConstraints)
    cached_steps: AICreateCachedSteps | None = None

    @model_validator(mode="after")
    def validate_author_input(self):
        creation_author_brief(self)
        return self


class BlueprintRegenerationRequest(AICreateNovelRequest):
    """Whole-blueprint rerun with a fixed, explicitly budgeted authority."""

    cached_steps: None = Field(default=None)
    system_prompt: None = Field(default=None)
    allow_failure_retry: Literal[False] = False
    max_tokens: int = Field(default=16_384, ge=1, le=200_000)
    token_budget: int | None = Field(default=None, ge=1, le=2**63 - 1)


class BlueprintRegenerationStartRequest(BlueprintRegenerationRequest):
    readiness_digest: str = Field(min_length=64, max_length=64)
    acknowledge_automatic_token_budget: bool = False


class _BlueprintBudgetBoundary(ValueError):
    provider_request_not_dispatched = True


class _BlueprintAttemptScope:
    """Atomically reserve every Provider request inside one fixed workflow."""

    def __init__(self, *, maximum_attempts: int, token_budget: int) -> None:
        self.maximum_attempts = int(maximum_attempts)
        self.token_budget = int(token_budget)
        self._lock = asyncio.Lock()
        self._claims: dict[str, tuple[str, str]] = {}
        self._reservations: dict[str, int] = {}
        self._attempts: dict[str, AttemptUsage] = {}
        self._uncertain: set[str] = set()
        self._consumed_tokens = 0

    @property
    def attempts(self) -> tuple[AttemptUsage, ...]:
        return tuple(self._attempts.values())

    @property
    def claimed_attempt_ids(self) -> tuple[str, ...]:
        return tuple(self._claims)

    @property
    def uncertain_attempt_ids(self) -> tuple[str, ...]:
        return tuple(self._uncertain)

    async def claim(self, provider_alias: str, phase: str) -> str:
        return await self.claim_with_budget(provider_alias, phase, None)

    async def claim_with_budget(
        self,
        provider_alias: str,
        phase: str,
        conservative_tokens: int | None,
    ) -> str:
        if (
            conservative_tokens is None
            or isinstance(conservative_tokens, bool)
            or int(conservative_tokens) <= 0
        ):
            raise _BlueprintBudgetBoundary(
                "blueprint generation has no conservative token bound"
            )
        bound = int(conservative_tokens)
        async with self._lock:
            if len(self._claims) >= self.maximum_attempts:
                raise _BlueprintBudgetBoundary(
                    "blueprint generation attempt capacity exhausted"
                )
            reserved = sum(self._reservations.values())
            if self._consumed_tokens + reserved + bound > self.token_budget:
                raise _BlueprintBudgetBoundary(
                    "blueprint generation token budget exhausted before dispatch"
                )
            attempt_id = uuid4().hex
            self._claims[attempt_id] = (str(provider_alias), str(phase))
            self._reservations[attempt_id] = bound
            return attempt_id

    async def account(self, attempt_id: str, usage: TokenUsage) -> None:
        async with self._lock:
            if attempt_id in self._attempts:
                return
            provider_alias, phase = self._claims[attempt_id]
            reserved = self._reservations.pop(attempt_id, 0)
            actual = max(
                int(usage.total_tokens or 0),
                int(usage.input_tokens or 0) + int(usage.output_tokens or 0),
            )
            accounted = actual if actual > 0 else reserved
            self._consumed_tokens += accounted
            self._attempts[attempt_id] = AttemptUsage(
                attempt_id=attempt_id,
                provider_alias=provider_alias,
                phase=phase,
                usage=TokenUsage(
                    input_tokens=max(0, int(usage.input_tokens or 0)),
                    output_tokens=max(0, int(usage.output_tokens or 0)),
                    total_tokens=accounted,
                ),
            )

    async def mark_uncertain(self, attempt_id: str, reason: str) -> None:
        del reason
        async with self._lock:
            if attempt_id in self._uncertain:
                return
            provider_alias, phase = self._claims[attempt_id]
            reserved = self._reservations.pop(attempt_id, 0)
            self._consumed_tokens += reserved
            self._uncertain.add(attempt_id)
            self._attempts[attempt_id] = AttemptUsage(
                attempt_id=attempt_id,
                provider_alias=provider_alias,
                phase=phase,
                usage=TokenUsage(total_tokens=reserved),
                state="uncertain",
            )

    async def release_pre_dispatch(self, attempt_id: str, reason: str) -> None:
        del reason
        async with self._lock:
            self._reservations.pop(attempt_id, None)
            self._claims.pop(attempt_id, None)


def _blueprint_regeneration_snapshot(
    req: BlueprintRegenerationRequest,
    *,
    runtime,
) -> tuple[
    dict[str, Any],
    dict[str, GenerationPlan],
    dict[str, Any],
]:
    plans = {
        step.key: runtime.plan_structured(
            WorkflowStepTarget(WORKFLOW_NAME, step.resolved_config_key)
        )
        for step in AI_CREATE_STEPS
    }
    plan_items: list[dict[str, Any]] = []
    maximum_provider_attempts = 0
    maximum_tokens_total = 0
    token_bound_known = True
    for step in AI_CREATE_STEPS:
        plan = plans[step.key]
        output_bound = req.max_tokens or plan.max_output_tokens
        context_bound = plan.max_context_tokens
        attempts = int(plan.max_semantic_attempts)
        maximum_provider_attempts += attempts
        if output_bound is None or context_bound is None:
            token_bound_known = False
        else:
            maximum_tokens_total += attempts * (
                int(output_bound) + int(context_bound)
            )
        plan_items.append({
            "step": step.key,
            "provider_alias": plan.provider_alias,
            "provider_model": plan.provider_model,
            "mode": plan.mode.value,
            "reviewer_alias": plan.reviewer_alias,
            "config_revision": plan.config_revision,
            "capability_snapshot": plan.capability_snapshot,
            "maximum_attempts": attempts,
            "max_output_tokens": output_bound,
            "max_context_tokens": context_bound,
        })
    uses_system_token_budget = bool(
        req.token_budget is None
        and token_bound_known
        and maximum_tokens_total > 0
    )
    effective_token_budget = (
        maximum_tokens_total
        if uses_system_token_budget
        else req.token_budget
    )
    source = {
        "author_brief": creation_author_brief(req).to_record(),
        "user_idea": req.user_idea,
        "number_of_chapters": req.number_of_chapters,
        "words_per_chapter": req.words_per_chapter,
        "creative_direction": (
            req.creative_direction.model_dump(mode="json")
            if req.creative_direction is not None
            else None
        ),
    }
    prompts = dict(_load_prompts().get(WORKFLOW_NAME, {}))
    authorization_snapshot = {
        "version": 2,
        "workflow": "blueprint_regeneration",
        "source": source,
        "token_budget": effective_token_budget,
        "uses_system_token_budget": uses_system_token_budget,
        "generation_params": {
            **build_gen_kwargs(req),
            "allow_failure_retry": False,
        },
        "maximum_provider_attempts": maximum_provider_attempts,
        "maximum_tokens_total": maximum_tokens_total,
        "token_bound_known": token_bound_known,
        "plans": plan_items,
        "prompt_revision": hashlib.sha256(
            json.dumps(
                prompts,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }
    digest = hashlib.sha256(
        json.dumps(
            authorization_snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    report = {
        "version": 2,
        "status": (
            "blocked"
            if not token_bound_known
            else "warning_requires_ack"
            if uses_system_token_budget
            else "ready"
        ),
        "digest": digest,
        "token_budget": effective_token_budget,
        "uses_system_token_budget": uses_system_token_budget,
        "maximum_provider_attempts": maximum_provider_attempts,
        "maximum_tokens_total": maximum_tokens_total,
        "token_bound_known": token_bound_known,
        "budget_covers_conservative_maximum": bool(
            token_bound_known
            and effective_token_budget is not None
            and effective_token_budget >= maximum_tokens_total
        ),
        "providers": [
            {
                "step": item["step"],
                "provider_alias": item["provider_alias"],
                "provider_model": item["provider_model"],
                "maximum_attempts": item["maximum_attempts"],
            }
            for item in plan_items
        ],
        "issues": (
            [{
                "code": "blueprint_token_bound_unproven",
                "level": "blocked",
            }]
            if not token_bound_known
            else [{
                "code": "automatic_token_budget_requires_confirmation",
                "level": "warning_requires_ack",
            }]
            if uses_system_token_budget
            else []
        ),
    }
    return report, plans, prompts


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


@router.post("/regenerate-blueprint/readiness")
async def inspect_blueprint_regeneration_readiness(
    req: BlueprintRegenerationRequest,
) -> dict[str, Any]:
    """Plan the fixed four-step rerun without dispatching a Provider request."""
    try:
        runtime = create_workflow_runtime(max_provider_retries=0)
        report, _plans, _prompts = _blueprint_regeneration_snapshot(
            req,
            runtime=runtime,
        )
        return report
    except Exception as exc:
        logger.warning(
            "[blueprint_regeneration] readiness planning failed type=%s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=400,
            detail={
                "code": "blueprint_regeneration_plan_invalid",
                "message": "当前 Provider 或工作流计划无法完成蓝图预检。",
            },
        ) from exc


@router.post("/regenerate-blueprint")
async def regenerate_blueprint(
    req: BlueprintRegenerationStartRequest,
    request: Request,
):
    """Execute the exact zero-cost preview inside hard attempt/token ceilings."""
    planning_runtime = create_workflow_runtime(max_provider_retries=0)
    try:
        report, plans, frozen_prompts = _blueprint_regeneration_snapshot(
            req,
            runtime=planning_runtime,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "blueprint_regeneration_readiness_stale",
                "message": "Provider 或工作流计划已变化，请重新预检。",
            },
        ) from exc
    if report["status"] == "blocked":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "blueprint_regeneration_token_bound_unproven",
                "message": "当前 Provider 缺少可证明的 token 上界。",
            },
        )
    if (
        report["uses_system_token_budget"]
        and not req.acknowledge_automatic_token_budget
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "automatic_token_budget_confirmation_required",
                "message": "请先确认系统计算的 Token 消耗上界。",
            },
        )
    if req.readiness_digest != report["digest"]:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "blueprint_regeneration_readiness_stale",
                "message": "蓝图重新生成预检已过期，请重新检查后再启动。",
            },
        )

    scope = _BlueprintAttemptScope(
        maximum_attempts=int(report["maximum_provider_attempts"]),
        token_budget=int(report["token_budget"]),
    )
    execution_runtime = create_workflow_runtime(
        attempt_scope=scope,
        max_provider_retries=0,
    )

    async def event_stream() -> AsyncGenerator[str, None]:
        async for frame in run_workflow(
            workflow_name=WORKFLOW_NAME,
            steps=AI_CREATE_STEPS,
            prompts=frozen_prompts,
            params={
                "author_brief": creation_author_brief(req).to_record(),
                "user_idea": req.user_idea,
                "number_of_chapters": req.number_of_chapters,
                "words_per_chapter": req.words_per_chapter,
            },
            gen_kwargs=build_gen_kwargs(req),
            cached={},
            deps=WorkflowDeps(
                runtime=execution_runtime,
                structured_plans=plans,
            ),
            request_id=uuid4().hex[:8],
            is_disconnected=request.is_disconnected,
            log_partial_on_disconnect=False,
        ):
            yield frame

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
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
                "author_brief": creation_author_brief(req).to_record(),
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
