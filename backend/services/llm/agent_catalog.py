"""User-visible Agent catalog with built-in and persisted custom adapters."""

from __future__ import annotations

from typing import Any, Literal

from backend.config.config import get_config_value
from backend.db.errors import NotFoundError
from backend.db.repositories.agent_definition_repository import (
    AgentDefinitionRepository,
    agent_definition_repo,
)
from backend.services.auth.identity_service import Actor
from backend.services.llm.application_capability_registry import (
    list_public_capability_definitions,
)
from backend.services.llm.capability_registry import CapabilityDefinition
from backend.services.llm.agent_orchestrator import (
    AgentProfile,
    get_agent_profile,
    get_agent_profiles,
)
from backend.services.llm.agent_limits import (
    MAX_CUSTOM_AGENT_INSTRUCTION_CHARS,
    MAX_CUSTOM_AGENT_OUTPUT_TOKENS,
)


_GENERATION_PARAM_KEYS = {
    "temperature",
    "top_p",
    "max_tokens",
    "presence_penalty",
    "frequency_penalty",
}


class AgentCatalog:
    """Resolve all Agent visibility, validation and optimistic-version rules."""

    def __init__(
        self,
        repository: AgentDefinitionRepository = agent_definition_repo,
    ) -> None:
        self.repository = repository

    @staticmethod
    def list_capabilities() -> tuple[CapabilityDefinition, ...]:
        return list_public_capability_definitions()

    @staticmethod
    def get_capability(capability: str) -> CapabilityDefinition:
        for definition in list_public_capability_definitions():
            if definition.capability == capability:
                return definition
        raise ValueError(f"未知生成角色能力: {capability}")

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
                f"生成角色 '{agent_id}' 不支持能力 '{capability}'"
            )
        if not profile.enabled:
            raise ValueError(f"生成角色已停用: {agent_id}")
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
            raise ValueError(f"能力 '{capability}' 不允许创建自定义生成角色")
        return await self.create_profile(
            actor,
            label=(label or f"{source.public_label} 副本").strip(),
            description=source.description,
            capability=capability,
            instruction=source.public_instruction,
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
                raise NotFoundError(f"Editable generation role '{agent_id}' was not found")
            return
        raise ValueError("内置生成角色不能删除；可复制为自定义生成角色后编辑")

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
            raise ValueError(f"能力 '{capability}' 不允许创建自定义生成角色")
        if not 2 <= len(label.strip()) <= 64:
            raise ValueError("生成角色名称长度必须为 2 到 64 个字符")
        if len(description.strip()) > 500:
            raise ValueError("生成角色说明不能超过 500 个字符")
        if not 20 <= len(instruction.strip()) <= MAX_CUSTOM_AGENT_INSTRUCTION_CHARS:
            raise ValueError(
                "生成角色指令长度必须为 20 到 "
                f"{MAX_CUSTOM_AGENT_INSTRUCTION_CHARS} 个字符"
            )
        if visibility not in {"private", "shared"}:
            raise ValueError("生成角色可见性无效")
        if visibility == "shared" and not actor.is_admin:
            raise ValueError("只有管理员可以创建共享生成角色")
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
                f"不支持的生成角色参数: {', '.join(sorted(unknown))}"
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
        if max_tokens is not None and not (
            0 < int(max_tokens) <= MAX_CUSTOM_AGENT_OUTPUT_TOKENS
        ):
            raise ValueError(
                "max_tokens 必须在 1 到 "
                f"{MAX_CUSTOM_AGENT_OUTPUT_TOKENS} 之间"
            )
        for key in ("presence_penalty", "frequency_penalty"):
            value = cleaned.get(key)
            if value is not None and not -2 <= float(value) <= 2:
                raise ValueError(f"{key} 必须在 -2 到 2 之间")
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
