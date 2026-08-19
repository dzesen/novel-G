"""Executable registry definitions for the existing preview Agent tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from backend.services.llm.agent_capability_contracts import (
    ContinuityReviewRequest,
    ContinuityReviewResponse,
    CreativeDirectionResponse,
    CreativeDirectorRequest,
    CreativeInspirationRequest,
    CreativeInspirationResponse,
    IllustrationPromptRequest,
    IllustrationPromptResponse,
    RewriteChapterSceneRequest,
    SceneRewriteResponse,
    StyleConsistencyRequest,
    StyleConsistencyResponse,
    VolumeRetrospectiveRequest,
    VolumeRetrospectiveResponse,
)
from backend.services.llm.capability_registry import (
    CapabilityBudget,
    CapabilityCall,
    CapabilityDefinition,
    CapabilityHandler,
    CapabilityRegistry,
    ContextProvider,
    RevisionPolicy,
    SideEffectPolicy,
)
from backend.services.llm.generation_runtime import (
    ExplicitProviderTarget,
    WorkflowStepTarget,
    create_generation_runtime,
)


AGENT_WORKFLOW_TARGETS: dict[str, WorkflowStepTarget] = {
    "scene_rewrite": WorkflowStepTarget(
        "rewrite_chapter_scene_by_agent",
        "scene_rewrite",
    ),
    "novel_direction": WorkflowStepTarget(
        "creative_direction_by_agent",
        "direction",
    ),
    "creative_inspiration": WorkflowStepTarget(
        "creative_inspiration_by_agent",
        "inspiration",
    ),
    "continuity_review": WorkflowStepTarget(
        "continuity_review_by_agent",
        "review",
    ),
    "style_consistency": WorkflowStepTarget(
        "style_consistency_by_agent",
        "review",
    ),
    "illustration_prompt": WorkflowStepTarget(
        "illustration_prompt_by_agent",
        "illustration_prompt",
    ),
    "volume_retrospective": WorkflowStepTarget(
        "volume_retrospective_by_agent",
        "review",
    ),
}

_FALLBACK_STRUCTURED_ATTEMPTS = 4
_FALLBACK_OUTPUT_TOKENS = 20_000


@dataclass(frozen=True)
class AgentCapabilityDependencies:
    actor: Any
    access: Any
    catalog: Any
    runs: Any
    profile: Any


class _PinnedAgentCatalog:
    """Keep handler execution on the profile resolved for its budget plan."""

    def __init__(self, profile: Any) -> None:
        self._profile = profile

    async def resolve_profile(
        self,
        _actor: Any,
        *,
        agent_id: str,
        capability: str | None = None,
    ) -> Any:
        if agent_id != self._profile.agent_id:
            raise ValueError("Agent profile changed after capability planning")
        if capability and capability not in self._profile.capabilities:
            raise ValueError("Agent capability changed after capability planning")
        return self._profile


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _budget_estimator(target: WorkflowStepTarget):
    def estimate(
        request: BaseModel,
        dependencies: AgentCapabilityDependencies,
        _call: CapabilityCall,
    ) -> CapabilityBudget:
        allow_retry = bool(
            getattr(request, "allow_failure_retry", True)
        )
        profile = dependencies.profile
        resolved_target = (
            ExplicitProviderTarget(profile.provider_alias)
            if profile.provider_alias
            else target
        )
        try:
            runtime = create_generation_runtime(
                **({} if allow_retry else {"max_provider_retries": 0})
            )
            plan = runtime.plan_structured(resolved_target)
        except ValueError:
            plan = None
        requested = _positive_int(getattr(request, "max_tokens", None))
        role_default = _positive_int(
            dict(profile.generation_params).get("max_tokens")
        )
        return CapabilityBudget(
            max_paid_attempts=(
                int(plan.max_semantic_attempts)
                if plan is not None
                else _FALLBACK_STRUCTURED_ATTEMPTS
            ),
            max_output_tokens=(
                requested
                or role_default
                or (
                    _positive_int(plan.max_output_tokens)
                    if plan is not None
                    else None
                )
                or _FALLBACK_OUTPUT_TOKENS
            ),
            provider_alias=(
                str(plan.provider_alias)
                if plan is not None
                else profile.provider_alias
            ),
            provider_model=(
                str(plan.provider_model) if plan is not None else None
            ),
        )

    return estimate


def _preview_audit(result: BaseModel) -> dict[str, Any]:
    data = result.model_dump()
    return {
        "result_type": type(result).__name__,
        "run_id": data.get("run_id"),
        "agent_id": data.get("agent_id"),
        "provider_alias": data.get("provider_alias"),
        "usage": dict(data.get("usage") or {}),
        "write_policy": data.get("write_policy", "preview_only"),
    }


async def _execute_scene_rewrite(
    request: RewriteChapterSceneRequest,
    dependencies: AgentCapabilityDependencies,
    _call: CapabilityCall,
) -> SceneRewriteResponse:
    from backend.api.llm_routers.scene_agent_router import (
        _execute_scene_rewrite_capability,
    )

    result = await _execute_scene_rewrite_capability(
        request,
        actor=dependencies.actor,
        access=dependencies.access,
        catalog=dependencies.catalog,
    )
    return SceneRewriteResponse.model_validate(result)


async def _execute_creative_direction(
    request: CreativeDirectorRequest,
    dependencies: AgentCapabilityDependencies,
    _call: CapabilityCall,
) -> CreativeDirectionResponse:
    from backend.api.llm_routers.agent_tool_router import (
        _execute_creative_direction_capability,
    )

    result = await _execute_creative_direction_capability(
        request,
        actor=dependencies.actor,
        catalog=dependencies.catalog,
    )
    return CreativeDirectionResponse.model_validate(result)


async def _execute_creative_inspiration(
    request: CreativeInspirationRequest,
    dependencies: AgentCapabilityDependencies,
    _call: CapabilityCall,
) -> CreativeInspirationResponse:
    from backend.api.llm_routers.agent_tool_router import (
        _execute_creative_inspiration_capability,
    )

    result = await _execute_creative_inspiration_capability(
        request,
        actor=dependencies.actor,
        access=dependencies.access,
        catalog=dependencies.catalog,
        runs=dependencies.runs,
    )
    return CreativeInspirationResponse.model_validate(result)


async def _execute_continuity_review(
    request: ContinuityReviewRequest,
    dependencies: AgentCapabilityDependencies,
    _call: CapabilityCall,
) -> ContinuityReviewResponse:
    from backend.api.llm_routers.agent_tool_router import (
        _execute_continuity_review_capability,
    )

    result = await _execute_continuity_review_capability(
        request,
        actor=dependencies.actor,
        access=dependencies.access,
        catalog=dependencies.catalog,
        runs=dependencies.runs,
    )
    return ContinuityReviewResponse.model_validate(result)


async def _execute_style_consistency(
    request: StyleConsistencyRequest,
    dependencies: AgentCapabilityDependencies,
    _call: CapabilityCall,
) -> StyleConsistencyResponse:
    from backend.api.llm_routers.agent_tool_router import (
        _execute_style_consistency_capability,
    )

    result = await _execute_style_consistency_capability(
        request,
        actor=dependencies.actor,
        access=dependencies.access,
        catalog=dependencies.catalog,
        runs=dependencies.runs,
    )
    return StyleConsistencyResponse.model_validate(result)


async def _execute_illustration_prompt(
    request: IllustrationPromptRequest,
    dependencies: AgentCapabilityDependencies,
    _call: CapabilityCall,
) -> IllustrationPromptResponse:
    from backend.api.llm_routers.agent_tool_router import (
        _execute_illustration_prompt_capability,
    )

    result = await _execute_illustration_prompt_capability(
        request,
        actor=dependencies.actor,
        access=dependencies.access,
        catalog=dependencies.catalog,
        runs=dependencies.runs,
    )
    return IllustrationPromptResponse.model_validate(result)


async def _execute_volume_retrospective(
    request: VolumeRetrospectiveRequest,
    dependencies: AgentCapabilityDependencies,
    _call: CapabilityCall,
) -> VolumeRetrospectiveResponse:
    from backend.api.llm_routers.agent_tool_router import (
        _execute_volume_retrospective_capability,
    )

    result = await _execute_volume_retrospective_capability(
        request,
        actor=dependencies.actor,
        access=dependencies.access,
        catalog=dependencies.catalog,
        runs=dependencies.runs,
    )
    return VolumeRetrospectiveResponse.model_validate(result)


def build_agent_capability_registry(
    *,
    access: Any | None = None,
    catalog: Any | None = None,
    runs: Any | None = None,
) -> CapabilityRegistry:
    """Build Agent definitions with production or HTTP-injected adapters."""

    def provide_dependencies_for(capability: str):
        async def provide_dependencies(
            request: BaseModel,
            call: CapabilityCall,
        ) -> AgentCapabilityDependencies:
            if call.actor is None:
                raise ValueError(
                    "Agent capabilities require an authenticated actor"
                )
            resolved_access = access
            resolved_catalog = catalog
            resolved_runs = runs
            if resolved_access is None:
                from backend.services.auth.novel_access_service import (
                    get_novel_access_service,
                )

                resolved_access = get_novel_access_service()
            if resolved_catalog is None:
                from backend.services.llm.agent_catalog import agent_catalog

                resolved_catalog = agent_catalog
            if resolved_runs is None:
                from backend.services.llm.agent_run import agent_run_store

                resolved_runs = agent_run_store
            profile = await resolved_catalog.resolve_profile(
                call.actor,
                agent_id=str(getattr(request, "agent_id", "")),
                capability=capability,
            )
            return AgentCapabilityDependencies(
                actor=call.actor,
                access=resolved_access,
                catalog=_PinnedAgentCatalog(profile),
                runs=resolved_runs,
                profile=profile,
            )

        return provide_dependencies

    specifications = (
        (
            "scene_rewrite",
            "场景改写",
            "保持场景功能和故事事实，生成可人工接受的场景候选。",
            ("scene",),
            RewriteChapterSceneRequest,
            SceneRewriteResponse,
            "chapter_context:scene_snapshot",
            _execute_scene_rewrite,
        ),
        (
            "novel_direction",
            "新书创意定向",
            "在正式建书前，把原始灵感发展为多个可比较的长篇方向供用户确认。",
            ("creation",),
            CreativeDirectorRequest,
            CreativeDirectionResponse,
            "creation_input:user_brief",
            _execute_creative_direction,
        ),
        (
            "creative_inspiration",
            "创意启发",
            "针对小说、卷或章节提出多个带影响分析的创意方向。",
            ("novel", "volume", "chapter"),
            CreativeInspirationRequest,
            CreativeInspirationResponse,
            "agent_context:bounded_evidence",
            _execute_creative_inspiration,
        ),
        (
            "continuity_review",
            "前后一致性检查",
            "按证据检查人物、时间、设定、伏笔与卷纲冲突。",
            ("novel", "volume", "chapter"),
            ContinuityReviewRequest,
            ContinuityReviewResponse,
            "agent_context:bounded_evidence",
            _execute_continuity_review,
        ),
        (
            "style_consistency",
            "文风与人物声音一致性",
            "以早期正文抽样和角色卡声音字段为基准，逐条定位可举证的风格漂移。",
            ("chapter", "volume"),
            StyleConsistencyRequest,
            StyleConsistencyResponse,
            "agent_context:bounded_evidence",
            _execute_style_consistency,
        ),
        (
            "illustration_prompt",
            "插图提示词",
            "把有界小说证据转译为可编辑的结构化文生图提示词。",
            ("character", "novel", "chapter"),
            IllustrationPromptRequest,
            IllustrationPromptResponse,
            "agent_context:bounded_evidence",
            _execute_illustration_prompt,
        ),
        (
            "volume_retrospective",
            "卷级复盘",
            "结合卷纲、卷内正文与确定性故事健康报告，复核承诺兑现、伏笔回收与节奏。",
            ("volume",),
            VolumeRetrospectiveRequest,
            VolumeRetrospectiveResponse,
            "agent_context:bounded_evidence",
            _execute_volume_retrospective,
        ),
    )
    return CapabilityRegistry(
        tuple(
            CapabilityDefinition(
                capability=capability,
                version=1,
                label=label,
                description=description,
                customizable=True,
                scope_options=scope_options,
                input_schema=input_schema,
                output_schema=output_schema,
                context_provider=ContextProvider(
                    policy_id=context_policy,
                    provide=provide_dependencies_for(capability),
                ),
                handler=CapabilityHandler(execute=handler),
                side_effect_policy=SideEffectPolicy.PREVIEW_ONLY,
                allowed_tools=(),
                budget_estimator=_budget_estimator(
                    AGENT_WORKFLOW_TARGETS[capability]
                ),
                revision_policy=RevisionPolicy.READ_ONLY,
                audit_projector=_preview_audit,
            )
            for (
                capability,
                label,
                description,
                scope_options,
                input_schema,
                output_schema,
                context_policy,
                handler,
            ) in specifications
        )
    )
