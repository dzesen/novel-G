"""Executable registry definitions for the existing preview Agent tools."""

from __future__ import annotations

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
from backend.services.llm.agent_capability_application import (
    AGENT_WORKFLOW_TARGETS,
    AgentCapabilityApplication,
    PreparedAgentCapability,
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

_FALLBACK_STRUCTURED_ATTEMPTS = 4
_FALLBACK_OUTPUT_TOKENS = 20_000


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
        dependencies: PreparedAgentCapability,
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


def _application_handler(
    application: AgentCapabilityApplication,
    capability: str,
):
    async def execute(
        request: BaseModel,
        prepared: PreparedAgentCapability,
        call: CapabilityCall,
    ) -> BaseModel:
        return await application.execute(
            capability,
            request,
            prepared,
            call,
        )

    execute.__name__ = (
        "_execute_creative_direction"
        if capability == "novel_direction"
        else f"_execute_{capability}"
    )
    return execute


def build_agent_capability_registry(
    *,
    application: AgentCapabilityApplication | Any | None = None,
    access: Any | None = None,
    catalog: Any | None = None,
    runs: Any | None = None,
) -> CapabilityRegistry:
    """Build Agent definitions with production or HTTP-injected adapters."""

    resolved_application = application or AgentCapabilityApplication(
        access=access,
        catalog=catalog,
        runs=runs,
    )

    def prepare_context_for(capability: str):
        async def prepare_context(
            request: BaseModel,
            call: CapabilityCall,
        ) -> PreparedAgentCapability:
            return await resolved_application.prepare(
                capability,
                request,
                call,
            )

        return prepare_context

    specifications = (
        (
            "scene_rewrite",
            "场景改写",
            "保持场景功能和故事事实，生成可人工接受的场景候选。",
            ("scene",),
            RewriteChapterSceneRequest,
            SceneRewriteResponse,
            "chapter_context:scene_snapshot",
        ),
        (
            "novel_direction",
            "新书创意定向",
            "在正式建书前，把原始灵感发展为多个可比较的长篇方向供用户确认。",
            ("creation",),
            CreativeDirectorRequest,
            CreativeDirectionResponse,
            "creation_input:user_brief",
        ),
        (
            "creative_inspiration",
            "创意启发",
            "针对小说、卷或章节提出多个带影响分析的创意方向。",
            ("novel", "volume", "chapter"),
            CreativeInspirationRequest,
            CreativeInspirationResponse,
            "agent_context:bounded_evidence",
        ),
        (
            "continuity_review",
            "前后一致性检查",
            "按证据检查人物、时间、设定、伏笔与卷纲冲突。",
            ("novel", "volume", "chapter"),
            ContinuityReviewRequest,
            ContinuityReviewResponse,
            "agent_context:bounded_evidence",
        ),
        (
            "style_consistency",
            "文风与人物声音一致性",
            "以早期正文抽样和角色卡声音字段为基准，逐条定位可举证的风格漂移。",
            ("chapter", "volume"),
            StyleConsistencyRequest,
            StyleConsistencyResponse,
            "agent_context:bounded_evidence",
        ),
        (
            "illustration_prompt",
            "插图提示词",
            "把有界小说证据转译为可编辑的结构化文生图提示词。",
            ("character", "novel", "chapter"),
            IllustrationPromptRequest,
            IllustrationPromptResponse,
            "agent_context:bounded_evidence",
        ),
        (
            "volume_retrospective",
            "卷级复盘",
            "结合卷纲、卷内正文与确定性故事健康报告，复核承诺兑现、伏笔回收与节奏。",
            ("volume",),
            VolumeRetrospectiveRequest,
            VolumeRetrospectiveResponse,
            "agent_context:bounded_evidence",
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
                    provide=prepare_context_for(capability),
                ),
                handler=CapabilityHandler(
                    execute=_application_handler(
                        resolved_application,
                        capability,
                    )
                ),
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
            ) in specifications
        )
    )
