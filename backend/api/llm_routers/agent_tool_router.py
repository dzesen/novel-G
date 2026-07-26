"""Preview-only creative inspiration and continuity review Agent endpoints."""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ConfigDict, Field

from backend.api.default_routers.agent_router import get_agent_catalog
from backend.api.default_routers.auth_router import require_authenticated_request
from backend.api.llm_routers._common import GenerationParamsMixin, build_gen_kwargs
from backend.db.errors import InvalidIdError, NotFoundError
from backend.services.auth.identity_service import Actor
from backend.services.auth.novel_access_service import (
    NovelAccessService,
    get_novel_access_service,
)
from backend.services.llm.agent_catalog import AgentCatalog
from backend.services.llm.agent_context import (
    AgentScope,
    StaleAgentContext,
    build_agent_context,
    ensure_agent_context_current,
    is_valid_evidence_reference,
)
from backend.services.llm.agent_orchestrator import (
    AgentOrchestrator,
    ContinuityReviewResult,
    CreativeInspirationResult,
)
from backend.services.llm.agent_run import AgentRunStore, agent_run_store
from backend.services.llm.generation_runtime import (
    PromptPlan,
    WorkflowStepTarget,
    create_generation_runtime,
)


router = APIRouter(
    prefix="/api/llm",
    tags=["llm-agents"],
    dependencies=[Depends(require_authenticated_request)],
)

CREATIVE_WORKFLOW = "creative_inspiration_by_agent"
CREATIVE_STEP = "inspiration"
CONTINUITY_WORKFLOW = "continuity_review_by_agent"
CONTINUITY_STEP = "review"
logger = logging.getLogger(__name__)


class AgentScopeRequest(GenerationParamsMixin):
    model_config = ConfigDict(extra="forbid")

    novel_id: str = Field(min_length=1)
    scope: Literal["novel", "volume", "chapter"] = "novel"
    volume_id: str | None = None
    chapter_id: str | None = None
    agent_id: str = Field(min_length=1)
    instruction: str = Field(default="", max_length=2000)


class CreativeInspirationRequest(AgentScopeRequest):
    question: str = Field(min_length=2, max_length=2000)
    constraints: str = Field(default="", max_length=2000)
    idea_count: int = Field(default=4, ge=2, le=8)


class ContinuityReviewRequest(AgentScopeRequest):
    focus: str = Field(default="", max_length=2000)


def get_agent_run_store() -> AgentRunStore:
    return agent_run_store


def _creative_prompt(
    *,
    context: str,
    target_label: str,
    question: str,
    constraints: str,
    instruction: str,
    idea_count: int,
    json_only: bool,
) -> str:
    suffix = (
        "只输出合法 JSON 对象，不要使用 Markdown 代码块。"
        if json_only
        else "严格按照提供的 JSON Schema 输出。"
    )
    return f"""为“{target_label}”完成一次受约束的创意启发。

【当前小说证据】
{context}

【用户要解决的问题】
{question}

【必须保留或避免的事项】
{constraints or "无额外事项"}

【本次补充指令】
{instruction or "无"}

要求：
- 生成恰好 {idea_count} 个方向，方案之间必须在核心机制上不同，不能只是措辞变化。
- 每个方案说明为什么适合现有故事、会影响哪些元素、主要风险及建议修改位置。
- 不得把建议写成已经发生的事实；不得悄悄推翻卷纲、人物永久事实或世界规则。
- affected_elements、risks、suggested_changes 使用简短字符串数组。
- framing 先概括当前创作空间与最关键约束。
{suffix}""".strip()


def _continuity_prompt(
    *,
    context: str,
    target_label: str,
    coverage: str,
    focus: str,
    instruction: str,
    json_only: bool,
) -> str:
    suffix = (
        "只输出合法 JSON 对象，不要使用 Markdown 代码块。"
        if json_only
        else "严格按照提供的 JSON Schema 输出。"
    )
    return f"""审查“{target_label}”的前后一致性，只报告有证据支持的问题。

【证据覆盖范围】
{coverage}

【当前小说证据】
{context}

【用户关注点】
{focus or "全面检查人物状态、时间线、地点、世界规则、伏笔、势力和卷纲服从性"}

【本次补充指令】
{instruction or "无"}

要求：
- 每个 issue 必须给出 location 和至少一条 evidence；不得仅凭常识推测。
- 每个 issue 还必须给出至少一个 references 条目，并只使用证据包中真实存在的
  chapter_id、scene_index、fact_id 或 thread_id。
- references.kind 只能是 chapter、scene、fact、thread；scene_index 从 0 开始。
- severity 只能是 high、medium、low。
- category 只能是 character_state、timeline、location、world_rule、plot_thread、
  volume_outline、faction、other。
- confidence 是 0 到 1 的数字；证据不足时不创建 issue，并在 summary 中说明。
- suggestion 是人工可执行的修正建议，不得直接改写或声称已经修改数据库。
- coverage 必须如实概括本次实际检查到的材料。
{suffix}""".strip()


async def _resolve_context_and_agent(
    *,
    request: AgentScopeRequest,
    actor: Actor,
    access: NovelAccessService,
    catalog: AgentCatalog,
    capability: str,
):
    await access.require_owned_novel(actor, request.novel_id)
    profile = await catalog.resolve_profile(
        actor,
        agent_id=request.agent_id,
        capability=capability,
    )
    context = await build_agent_context(
        novel_id=request.novel_id,
        scope=request.scope,
        volume_id=request.volume_id,
        chapter_id=request.chapter_id,
    )
    return context, profile


def _response_metadata(
    *,
    generated: Any,
    profile: Any,
    context: Any,
    run_id: str,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "agent_id": profile.agent_id,
        "agent_version": profile.version,
        "provider_alias": generated.plan.provider_alias,
        "usage": generated.usage.model_dump(),
        "attempts": [
            {
                "attempt_id": item.attempt_id,
                "provider_alias": item.provider_alias,
                "phase": item.phase,
                "state": item.state,
                "usage": item.usage.model_dump(),
            }
            for item in generated.attempts
        ],
        "context_report": {
            "coverage": context.coverage,
            "truncated_sections": list(context.truncated_sections),
        },
        "context_snapshot": context.snapshot(),
        "write_policy": "preview_only",
    }


def _validate_continuity_references(
    result: ContinuityReviewResult,
    context: Any,
) -> None:
    for issue_index, issue in enumerate(result.issues):
        invalid = [
            reference.model_dump(exclude_none=True)
            for reference in issue.references
            if not is_valid_evidence_reference(
                context,
                reference.model_dump(exclude_none=True),
            )
        ]
        if invalid:
            raise ValueError(
                f"一致性 issue[{issue_index}] 返回了不属于本次上下文的证据引用"
            )


async def _record_run_failure(
    runs: AgentRunStore,
    run_id: str | None,
    *,
    error: BaseException,
    runtime: Any | None,
    stale: bool = False,
) -> None:
    if not run_id:
        return
    try:
        await runs.fail(
            run_id,
            error=error,
            runtime=runtime,
            stale=stale,
        )
    except Exception:
        # Preserve the original request failure. A secondary audit-store
        # outage is logged for operators but must not replace the Provider,
        # validation, or stale-context error the author needs to act on.
        logger.exception("Failed to persist Agent run failure for %s", run_id)


@router.post("/agent-inspiration")
async def generate_agent_inspiration(
    request: CreativeInspirationRequest,
    actor: Actor = Depends(require_authenticated_request),
    access: NovelAccessService = Depends(get_novel_access_service),
    catalog: AgentCatalog = Depends(get_agent_catalog),
    runs: AgentRunStore = Depends(get_agent_run_store),
) -> dict[str, Any]:
    run_id: str | None = None
    runtime: Any | None = None
    try:
        context, profile = await _resolve_context_and_agent(
            request=request,
            actor=actor,
            access=access,
            catalog=catalog,
            capability="creative_inspiration",
        )
        run_id = await runs.begin(
            actor_id=actor.id,
            novel_id=request.novel_id,
            capability="creative_inspiration",
            agent_id=profile.agent_id,
            agent_version=profile.version,
            request=request.model_dump(),
            context=context,
        )
        runtime = create_generation_runtime()
        orchestrator = AgentOrchestrator(runtime)
        generated = await orchestrator.generate_structured(
            profile=profile,
            target=WorkflowStepTarget(CREATIVE_WORKFLOW, CREATIVE_STEP),
            schema=CreativeInspirationResult,
            prompts=PromptPlan(
                native_schema_prompt=_creative_prompt(
                    context=context.text,
                    target_label=context.target_label,
                    question=request.question.strip(),
                    constraints=request.constraints.strip(),
                    instruction=request.instruction.strip(),
                    idea_count=request.idea_count,
                    json_only=False,
                ),
                prompt_json_prompt=_creative_prompt(
                    context=context.text,
                    target_label=context.target_label,
                    question=request.question.strip(),
                    constraints=request.constraints.strip(),
                    instruction=request.instruction.strip(),
                    idea_count=request.idea_count,
                    json_only=True,
                ),
            ),
            **build_gen_kwargs(request),
        )
        await ensure_agent_context_current(context)
        result = generated.value.model_dump()
        await runs.complete(run_id, generated=generated, result=result)
        return {
            "result": result,
            **_response_metadata(
                generated=generated,
                profile=profile,
                context=context,
                run_id=run_id,
            ),
        }
    except StaleAgentContext as exc:
        await _record_run_failure(
            runs,
            run_id,
            error=exc,
            runtime=runtime,
            stale=True,
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except NotFoundError as exc:
        await _record_run_failure(
            runs,
            run_id,
            error=exc,
            runtime=runtime,
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (InvalidIdError, ValueError) as exc:
        await _record_run_failure(
            runs,
            run_id,
            error=exc,
            runtime=runtime,
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        await _record_run_failure(
            runs,
            run_id,
            error=exc,
            runtime=runtime,
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/agent-continuity-review")
async def generate_agent_continuity_review(
    request: ContinuityReviewRequest,
    actor: Actor = Depends(require_authenticated_request),
    access: NovelAccessService = Depends(get_novel_access_service),
    catalog: AgentCatalog = Depends(get_agent_catalog),
    runs: AgentRunStore = Depends(get_agent_run_store),
) -> dict[str, Any]:
    run_id: str | None = None
    runtime: Any | None = None
    try:
        context, profile = await _resolve_context_and_agent(
            request=request,
            actor=actor,
            access=access,
            catalog=catalog,
            capability="continuity_review",
        )
        run_id = await runs.begin(
            actor_id=actor.id,
            novel_id=request.novel_id,
            capability="continuity_review",
            agent_id=profile.agent_id,
            agent_version=profile.version,
            request=request.model_dump(),
            context=context,
        )
        runtime = create_generation_runtime()
        orchestrator = AgentOrchestrator(runtime)
        generated = await orchestrator.generate_structured(
            profile=profile,
            target=WorkflowStepTarget(CONTINUITY_WORKFLOW, CONTINUITY_STEP),
            schema=ContinuityReviewResult,
            prompts=PromptPlan(
                native_schema_prompt=_continuity_prompt(
                    context=context.text,
                    target_label=context.target_label,
                    coverage=context.coverage,
                    focus=request.focus.strip(),
                    instruction=request.instruction.strip(),
                    json_only=False,
                ),
                prompt_json_prompt=_continuity_prompt(
                    context=context.text,
                    target_label=context.target_label,
                    coverage=context.coverage,
                    focus=request.focus.strip(),
                    instruction=request.instruction.strip(),
                    json_only=True,
                ),
            ),
            **build_gen_kwargs(request),
        )
        await ensure_agent_context_current(context)
        _validate_continuity_references(generated.value, context)
        result = generated.value.model_dump()
        await runs.complete(run_id, generated=generated, result=result)
        return {
            "result": result,
            **_response_metadata(
                generated=generated,
                profile=profile,
                context=context,
                run_id=run_id,
            ),
        }
    except StaleAgentContext as exc:
        await _record_run_failure(
            runs,
            run_id,
            error=exc,
            runtime=runtime,
            stale=True,
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except NotFoundError as exc:
        await _record_run_failure(
            runs,
            run_id,
            error=exc,
            runtime=runtime,
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (InvalidIdError, ValueError) as exc:
        await _record_run_failure(
            runs,
            run_id,
            error=exc,
            runtime=runtime,
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        await _record_run_failure(
            runs,
            run_id,
            error=exc,
            runtime=runtime,
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc
