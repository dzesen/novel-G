"""User-visible Agent catalog with built-in and persisted custom adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from backend.config.config import get_config_value
from backend.db.errors import NotFoundError
from backend.db.repositories.agent_definition_repository import (
    AgentDefinitionRepository,
    agent_definition_repo,
)
from backend.services.auth.identity_service import Actor
from backend.services.llm.agent_orchestrator import (
    AgentProfile,
    get_agent_profile,
    get_agent_profiles,
)


@dataclass(frozen=True)
class CapabilityDefinition:
    capability: str
    version: int
    label: str
    description: str
    customizable: bool
    scope_options: tuple[str, ...]
    input_contract: str
    output_contract: str
    context_policy: str
    side_effect_policy: Literal[
        "preview_only",
        "accept_required",
        "system_write",
    ]
    handler_id: str

    def public_view(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "version": self.version,
            "label": self.label,
            "description": self.description,
            "customizable": self.customizable,
            "preview_only": self.side_effect_policy == "preview_only",
            "scope_options": list(self.scope_options),
            "input_contract": self.input_contract,
            "output_contract": self.output_contract,
            "context_policy": self.context_policy,
            "side_effect_policy": self.side_effect_policy,
            "handler_id": self.handler_id,
        }


_CAPABILITIES: tuple[CapabilityDefinition, ...] = (
    CapabilityDefinition(
        capability="chapter_outline",
        version=1,
        label="章节策划",
        description="生成受卷纲与故事状态约束的章节细纲。",
        customizable=False,
        scope_options=("chapter",),
        input_contract="ChapterOutlineGenerationRequest",
        output_contract="ChapterOutline",
        context_policy="chapter_context:hard_contracts",
        side_effect_policy="accept_required",
        handler_id="workflow:create_chapter_outline_by_ai.chapter_outline",
    ),
    CapabilityDefinition(
        capability="chapter_prose",
        version=1,
        label="章节执笔",
        description="依据已接受细纲生成章节正文。",
        customizable=False,
        scope_options=("chapter",),
        input_contract="WriteChapterRequest",
        output_contract="chapter_prose_stream",
        context_policy="chapter_context:hard_contracts",
        side_effect_policy="system_write",
        handler_id="workflow:write_chapter_by_ai.chapter_content",
    ),
    CapabilityDefinition(
        capability="chapter_state",
        version=1,
        label="状态提取",
        description="从正文提取人物状态、永久事实和伏笔变更。",
        customizable=False,
        scope_options=("chapter",),
        input_contract="ExtractChapterStateRequest",
        output_contract="ChapterStateProposal",
        context_policy="chapter_context:state_evidence",
        side_effect_policy="accept_required",
        handler_id="workflow:extract_chapter_state_by_ai.chapter_state",
    ),
    CapabilityDefinition(
        capability="scene_rewrite",
        version=1,
        label="场景改写",
        description="保持场景功能和故事事实，生成可人工接受的场景候选。",
        customizable=True,
        scope_options=("scene",),
        input_contract="RewriteChapterSceneRequest",
        output_contract="SceneRewriteResult",
        context_policy="chapter_context:scene_snapshot",
        side_effect_policy="preview_only",
        handler_id="router:rewrite_chapter_scene",
    ),
    CapabilityDefinition(
        capability="novel_direction",
        version=1,
        label="新书创意定向",
        description="在正式建书前，把原始灵感发展为多个可比较的长篇方向供用户确认。",
        customizable=True,
        scope_options=("creation",),
        input_contract="CreativeDirectorRequest",
        output_contract="CreativeDirectionResult",
        context_policy="creation_input:user_brief",
        side_effect_policy="preview_only",
        handler_id="router:generate_creative_direction",
    ),
    CapabilityDefinition(
        capability="creative_inspiration",
        version=1,
        label="创意启发",
        description="针对小说、卷或章节提出多个带影响分析的创意方向。",
        customizable=True,
        scope_options=("novel", "volume", "chapter"),
        input_contract="CreativeInspirationRequest",
        output_contract="CreativeInspirationResult",
        context_policy="agent_context:bounded_evidence",
        side_effect_policy="preview_only",
        handler_id="router:generate_agent_inspiration",
    ),
    CapabilityDefinition(
        capability="continuity_review",
        version=1,
        label="前后一致性检查",
        description="按证据检查人物、时间、设定、伏笔与卷纲冲突。",
        customizable=True,
        scope_options=("novel", "volume", "chapter"),
        input_contract="ContinuityReviewRequest",
        output_contract="ContinuityReviewResult",
        context_policy="agent_context:bounded_evidence",
        side_effect_policy="preview_only",
        handler_id="router:generate_agent_continuity_review",
    ),
    CapabilityDefinition(
        capability="style_consistency",
        version=1,
        label="文风与人物声音一致性",
        description="以早期正文抽样和角色卡声音字段为基准，逐条定位可举证的风格漂移。",
        customizable=True,
        scope_options=("chapter", "volume"),
        input_contract="StyleConsistencyRequest",
        output_contract="StyleConsistencyResult",
        context_policy="agent_context:bounded_evidence",
        side_effect_policy="preview_only",
        handler_id="router:generate_agent_style_consistency",
    ),
)
_CAPABILITY_BY_ID = {item.capability: item for item in _CAPABILITIES}
_GENERATION_PARAM_KEYS = {"temperature", "top_p", "max_tokens"}


class AgentCatalog:
    """Resolve all Agent visibility, validation and optimistic-version rules."""

    def __init__(
        self,
        repository: AgentDefinitionRepository = agent_definition_repo,
    ) -> None:
        self.repository = repository

    @staticmethod
    def list_capabilities() -> tuple[CapabilityDefinition, ...]:
        return _CAPABILITIES

    @staticmethod
    def get_capability(capability: str) -> CapabilityDefinition:
        try:
            return _CAPABILITY_BY_ID[capability]
        except KeyError as exc:
            raise ValueError(f"未知 Agent 能力: {capability}") from exc

    @staticmethod
    def list_provider_options() -> list[dict[str, str]]:
        llm = get_config_value("llm", {})
        providers = llm.get("providers", {}) if isinstance(llm, dict) else {}
        return [
            {
                "alias": str(alias),
                "type": str(provider.get("type") or ""),
                "model": str(provider.get("default_model") or ""),
            }
            for alias, provider in providers.items()
            if isinstance(provider, dict) and provider.get("enabled")
        ]

    async def list_profiles(
        self,
        actor: Actor,
        *,
        capability: str | None = None,
        include_disabled: bool = False,
    ) -> tuple[AgentProfile, ...]:
        if capability:
            self.get_capability(capability)
        builtins = get_agent_profiles(capability=capability)
        documents = await self.repository.list_visible(
            actor_id=actor.id,
            include_disabled=include_disabled,
            capability=capability,
        )
        return (*builtins, *(self._profile_from_document(item, actor) for item in documents))

    async def resolve_profile(
        self,
        actor: Actor,
        *,
        agent_id: str,
        capability: str | None = None,
    ) -> AgentProfile:
        try:
            profile = get_agent_profile(agent_id)
        except ValueError:
            document = await self.repository.get_visible(
                actor_id=actor.id,
                agent_id=agent_id,
            )
            profile = self._profile_from_document(document, actor)
        if capability and capability not in profile.capabilities:
            raise ValueError(
                f"Agent '{agent_id}' 不支持能力 '{capability}'"
            )
        if not profile.enabled:
            raise ValueError(f"Agent 已停用: {agent_id}")
        return profile

    async def create_profile(
        self,
        actor: Actor,
        *,
        label: str,
        description: str,
        capability: str,
        instruction: str,
        provider_alias: str | None,
        generation_params: dict[str, Any],
        visibility: Literal["private", "shared"],
        enabled: bool,
    ) -> AgentProfile:
        self._validate_editable_definition(
            actor=actor,
            label=label,
            description=description,
            capability=capability,
            instruction=instruction,
            provider_alias=provider_alias,
            generation_params=generation_params,
            visibility=visibility,
        )
        document = await self.repository.create_definition(
            owner_id=actor.id,
            data={
                "label": label.strip(),
                "description": description.strip(),
                "capability": capability,
                "instruction": instruction.strip(),
                "provider_alias": provider_alias.strip() if provider_alias else None,
                "generation_params": self._clean_generation_params(generation_params),
                "visibility": visibility,
                "enabled": bool(enabled),
            },
        )
        return self._profile_from_document(document, actor)

    async def update_profile(
        self,
        actor: Actor,
        *,
        agent_id: str,
        expected_version: int,
        label: str,
        description: str,
        capability: str,
        instruction: str,
        provider_alias: str | None,
        generation_params: dict[str, Any],
        visibility: Literal["private", "shared"],
        enabled: bool,
    ) -> AgentProfile:
        self._validate_editable_definition(
            actor=actor,
            label=label,
            description=description,
            capability=capability,
            instruction=instruction,
            provider_alias=provider_alias,
            generation_params=generation_params,
            visibility=visibility,
        )
        document = await self.repository.update_owned(
            owner_id=actor.id,
            agent_id=agent_id,
            expected_version=expected_version,
            updates={
                "label": label.strip(),
                "description": description.strip(),
                "capability": capability,
                "instruction": instruction.strip(),
                "provider_alias": provider_alias.strip() if provider_alias else None,
                "generation_params": self._clean_generation_params(generation_params),
                "visibility": visibility,
                "enabled": bool(enabled),
            },
        )
        return self._profile_from_document(document, actor)

    async def clone_profile(
        self,
        actor: Actor,
        *,
        source_agent_id: str,
        label: str | None = None,
    ) -> AgentProfile:
        source = await self.resolve_profile(actor, agent_id=source_agent_id)
        capability = source.capabilities[0]
        definition = self.get_capability(capability)
        if not definition.customizable:
            raise ValueError(f"能力 '{capability}' 不允许创建自定义 Agent")
        return await self.create_profile(
            actor,
            label=(label or f"{source.label} 副本").strip(),
            description=source.description,
            capability=capability,
            instruction=source.instruction,
            provider_alias=source.provider_alias,
            generation_params=dict(source.generation_params),
            visibility="private",
            enabled=True,
        )

    async def delete_profile(self, actor: Actor, *, agent_id: str) -> None:
        try:
            get_agent_profile(agent_id)
        except ValueError:
            await self.repository.get_owned(owner_id=actor.id, agent_id=agent_id)
            deleted = await self.repository.soft_delete_owned(
                owner_id=actor.id,
                agent_id=agent_id,
            )
            if not deleted:
                raise NotFoundError(f"Editable Agent '{agent_id}' was not found")
            return
        raise ValueError("内置 Agent 不能删除；可复制为自定义 Agent 后编辑")

    def _validate_editable_definition(
        self,
        *,
        actor: Actor,
        label: str,
        description: str,
        capability: str,
        instruction: str,
        provider_alias: str | None,
        generation_params: dict[str, Any],
        visibility: str,
    ) -> None:
        definition = self.get_capability(capability)
        if not definition.customizable:
            raise ValueError(f"能力 '{capability}' 不允许创建自定义 Agent")
        if not 2 <= len(label.strip()) <= 64:
            raise ValueError("Agent 名称长度必须为 2 到 64 个字符")
        if len(description.strip()) > 500:
            raise ValueError("Agent 说明不能超过 500 个字符")
        if not 20 <= len(instruction.strip()) <= 4000:
            raise ValueError("Agent 指令长度必须为 20 到 4000 个字符")
        if visibility not in {"private", "shared"}:
            raise ValueError("Agent 可见性无效")
        if visibility == "shared" and not actor.is_admin:
            raise ValueError("只有管理员可以创建共享 Agent")
        if provider_alias:
            aliases = {item["alias"] for item in self.list_provider_options()}
            if provider_alias.strip() not in aliases:
                raise ValueError(
                    f"Provider 不存在或未启用: {provider_alias.strip()}"
                )
        self._clean_generation_params(generation_params)

    @staticmethod
    def _clean_generation_params(values: dict[str, Any]) -> dict[str, Any]:
        unknown = set(values) - _GENERATION_PARAM_KEYS
        if unknown:
            raise ValueError(
                f"不支持的 Agent 生成参数: {', '.join(sorted(unknown))}"
            )
        cleaned = {
            key: value
            for key, value in values.items()
            if value is not None
        }
        temperature = cleaned.get("temperature")
        if temperature is not None and not 0 <= float(temperature) <= 2:
            raise ValueError("temperature 必须在 0 到 2 之间")
        top_p = cleaned.get("top_p")
        if top_p is not None and not 0 <= float(top_p) <= 1:
            raise ValueError("top_p 必须在 0 到 1 之间")
        max_tokens = cleaned.get("max_tokens")
        if max_tokens is not None and int(max_tokens) <= 0:
            raise ValueError("max_tokens 必须大于 0")
        return cleaned

    @staticmethod
    def _profile_from_document(
        document: dict[str, Any],
        actor: Actor,
    ) -> AgentProfile:
        owner_id = str(document["owner_id"])
        return AgentProfile(
            agent_id=str(document["agent_id"]),
            label=str(document.get("label") or ""),
            description=str(document.get("description") or ""),
            instruction=str(document.get("instruction") or ""),
            capabilities=(str(document.get("capability") or ""),),
            origin="custom",
            owner_id=owner_id,
            provider_alias=(
                str(document["provider_alias"])
                if document.get("provider_alias")
                else None
            ),
            generation_params=dict(document.get("generation_params") or {}),
            enabled=bool(document.get("enabled", True)),
            version=int(document.get("version") or 1),
            visibility=str(document.get("visibility") or "private"),
            editable=owner_id == actor.id,
        )


agent_catalog = AgentCatalog()
