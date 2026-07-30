"""图像 Provider 的协议分型配置模型。"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import math
import re
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


JsonScalar = Union[str, int, float, bool]
ReferenceMode = Literal[
    "none",
    "img2img",
    "controlnet",
    "style_reference",
    "edit_model",
]
ImageUsage = Literal["character_portrait", "cover", "scene_illustration"]


class _StrictConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkflowInputBinding(_StrictConfigModel):
    """把一个业务语义槽位绑定到 API-format workflow 的节点输入。"""

    node_id: str = Field(min_length=1)
    input: str = Field(min_length=1)
    required: bool = False
    upload: bool = False

    @field_validator("node_id", "input")
    @classmethod
    def strip_non_empty_value(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("workflow binding values must not be empty")
        return stripped


class WorkflowOutputBinding(_StrictConfigModel):
    """声明从 history.outputs 的哪个节点字段提取产物。"""

    node_id: str = Field(min_length=1)
    field: str = Field(default="images", min_length=1)

    @field_validator("node_id", "field")
    @classmethod
    def strip_non_empty_value(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("workflow output values must not be empty")
        return stripped


class WorkflowInputOverride(_StrictConfigModel):
    """Persisted scalar input difference applied without changing workflow topology."""

    node_id: str = Field(min_length=1, max_length=200)
    input: str = Field(min_length=1, max_length=200)
    value: JsonScalar

    @field_validator("value")
    @classmethod
    def validate_bounded_scalar(cls, value: JsonScalar) -> JsonScalar:
        if isinstance(value, str) and len(value) > 2_000:
            raise ValueError("workflow override string values are limited to 2000 characters")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("workflow override numeric values must be finite")
        return value

    @field_validator("node_id", "input")
    @classmethod
    def strip_non_empty_value(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("workflow override targets must not be empty")
        return stripped


class WorkflowDependencies(_StrictConfigModel):
    """readiness 使用的部署依赖；最终仍以 ComfyUI /prompt 校验为准。"""

    node_types: list[str] = Field(default_factory=list)
    checkpoints: list[str] = Field(default_factory=list)
    loras: list[str] = Field(default_factory=list)


class ComfyUIWorkflowConfig(_StrictConfigModel):
    """ComfyUI API-format 模板与语义槽位配置。"""

    template_path: str = ""
    template_revision: str = ""
    reference_mode: ReferenceMode = "none"
    bindings: dict[str, WorkflowInputBinding] = Field(default_factory=dict)
    overrides: list[WorkflowInputOverride] = Field(default_factory=list)
    outputs: list[WorkflowOutputBinding] = Field(default_factory=list)
    dependencies: WorkflowDependencies = Field(default_factory=WorkflowDependencies)

    @field_validator("template_path")
    @classmethod
    def strip_template_path(cls, value: str) -> str:
        return value.strip()

    @field_validator("template_revision")
    @classmethod
    def validate_template_revision(cls, value: str) -> str:
        stripped = value.strip()
        if stripped and re.fullmatch(r"sha256:[0-9a-f]{64}", stripped) is None:
            raise ValueError(
                "template_revision must be empty or sha256:<64 lowercase hex>"
            )
        return stripped

    @model_validator(mode="after")
    def validate_semantic_slot_names(self) -> "ComfyUIWorkflowConfig":
        if any(not str(slot).strip() for slot in self.bindings):
            raise ValueError("workflow semantic slot names must not be empty")
        targets: dict[tuple[str, str], str] = {}
        for slot, binding in self.bindings.items():
            target = (binding.node_id, binding.input)
            previous_slot = targets.get(target)
            if previous_slot is not None:
                raise ValueError(
                    f"语义槽位 {previous_slot} 与 {slot} 冲突："
                    f"都绑定到节点 {binding.node_id}.{binding.input}"
                )
            targets[target] = slot
        override_targets: dict[tuple[str, str], int] = {}
        for index, override in enumerate(self.overrides):
            target = (override.node_id, override.input)
            previous_index = override_targets.get(target)
            if previous_index is not None:
                raise ValueError(
                    "workflow overrides "
                    f"{previous_index} and {index} both target "
                    f"node {override.node_id}.{override.input}"
                )
            bound_slot = targets.get(target)
            if bound_slot is not None:
                raise ValueError(
                    f"workflow override for node {override.node_id}.{override.input} "
                    f"conflicts with semantic slot {bound_slot}"
                )
            override_targets[target] = index
        return self


class ComfyUIImageProviderConfig(_StrictConfigModel):
    """ComfyUI 协议配置；本协议没有 API Key 字段。"""

    type: Literal["comfyui"] = "comfyui"
    base_url: str = "http://127.0.0.1:8188"
    enabled: bool = False
    timeout_seconds: int = Field(default=600, gt=0)
    max_concurrency: int = Field(default=1, gt=0)
    workflow: ComfyUIWorkflowConfig = Field(default_factory=ComfyUIWorkflowConfig)

    @field_validator("base_url")
    @classmethod
    def normalize_base_url(cls, value: str) -> str:
        return value.strip().rstrip("/")


class FieldParameterMapping(_StrictConfigModel):
    """把一个语义槽位直接映射为 Provider 请求字段。"""

    kind: Literal["field"] = "field"
    field: str = Field(min_length=1)
    minimum: float | int | None = None
    maximum: float | int | None = None
    allowed_values: list[JsonScalar] | None = None
    default: JsonScalar | None = None

    @field_validator("field")
    @classmethod
    def strip_field(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("parameter field must not be empty")
        return stripped

    @model_validator(mode="after")
    def validate_bounds_and_default(self) -> "FieldParameterMapping":
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError("parameter mapping minimum must not exceed maximum")
        if (
            self.default is not None
            and self.allowed_values is not None
            and self.default not in self.allowed_values
        ):
            raise ValueError("parameter mapping default must be an allowed value")
        return self


class ValueMapParameterMapping(_StrictConfigModel):
    """把项目语义值映射为一个或多个实际请求参数。"""

    kind: Literal["value_map"] = "value_map"
    values: dict[str, dict[str, JsonScalar]]

    @model_validator(mode="after")
    def validate_value_map(self) -> "ValueMapParameterMapping":
        if not self.values:
            raise ValueError("parameter value map must not be empty")
        for semantic_value, request_fields in self.values.items():
            if not semantic_value.strip():
                raise ValueError("parameter semantic values must not be empty")
            if not request_fields:
                raise ValueError(
                    f"parameter value map entry must contain request fields: {semantic_value}"
                )
            if any(not field.strip() for field in request_fields):
                raise ValueError("provider request field names must not be empty")
        return self


ParameterMapping = Annotated[
    Union[FieldParameterMapping, ValueMapParameterMapping],
    Field(discriminator="kind"),
]


class OpenAICompatibleResultMapping(_StrictConfigModel):
    """OpenAI Images 兼容响应的字段映射。"""

    items_path: str = "data"
    url_field: str = "url"
    base64_field: str = "b64_json"
    mime_type_field: str = "mime_type"
    revised_prompt_field: str = "revised_prompt"


class OpenAICompatibleImageProviderConfig(_StrictConfigModel):
    """OpenAI Images 兼容协议配置；厂商差异只放在映射数据中。"""

    type: Literal["openai_compatible"] = "openai_compatible"
    base_url: str = ""
    api_key: str = ""
    default_model: str = ""
    enabled: bool = False
    timeout_seconds: int = Field(default=120, gt=0)
    max_retries: int = Field(default=2, ge=0)
    max_concurrency: int = Field(default=1, gt=0)
    parameters: dict[str, ParameterMapping | None] = Field(default_factory=dict)
    result: OpenAICompatibleResultMapping = Field(
        default_factory=OpenAICompatibleResultMapping
    )

    @field_validator("base_url")
    @classmethod
    def normalize_base_url(cls, value: str) -> str:
        # OpenAI SDK 的 base_url 可以包含 /v1；这里只去除空白和末尾斜杠。
        return value.strip().rstrip("/")

    @field_validator("default_model")
    @classmethod
    def strip_default_model(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_semantic_slot_names(self) -> "OpenAICompatibleImageProviderConfig":
        if any(not str(slot).strip() for slot in self.parameters):
            raise ValueError("parameter semantic slot names must not be empty")
        return self


ImageProviderConfig = Annotated[
    Union[ComfyUIImageProviderConfig, OpenAICompatibleImageProviderConfig],
    Field(discriminator="type"),
]


class _BaseIllustrationPipelineProfile(_StrictConfigModel):
    """所有场景插图 Pipeline 共用的最小接口。"""

    display_name: str = Field(default="", max_length=120)
    compose_provider: str = Field(min_length=1)

    @field_validator("display_name")
    @classmethod
    def strip_display_name(cls, value: str) -> str:
        return value.strip()

    @field_validator("compose_provider")
    @classmethod
    def require_compose_provider(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("compose_provider must not be empty")
        return stripped


class QuickIllustrationPipelineProfile(_BaseIllustrationPipelineProfile):
    """单阶段场景生成；extra=forbid 阻止偷加后续阶段。"""

    kind: Literal["quick"] = "quick"


class ConsistencyIllustrationPipelineProfile(_BaseIllustrationPipelineProfile):
    """构图、身份修正与可选精修的固定三段式。"""

    kind: Literal["consistency"] = "consistency"
    identity_edit_provider: str = Field(min_length=1)
    refine_provider: str | None = None

    @field_validator("identity_edit_provider")
    @classmethod
    def require_identity_provider(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("identity_edit_provider must not be empty")
        return stripped

    @field_validator("refine_provider", mode="before")
    @classmethod
    def normalize_optional_refine_provider(cls, value: Any) -> str | None:
        if value is None:
            return None
        stripped = str(value).strip()
        return stripped or None


IllustrationPipelineProfile = Annotated[
    Union[
        QuickIllustrationPipelineProfile,
        ConsistencyIllustrationPipelineProfile,
    ],
    Field(discriminator="kind"),
]


class ImagePipelineStageQualityEvidence(_StrictConfigModel):
    """Provider 适配器对有效定义与可见运行环境给出的质量证据。"""

    provider_alias: str = Field(min_length=1)
    provider_revision: str
    adapter_id: str = Field(min_length=1, max_length=80)
    adapter_revision: str = Field(min_length=1, max_length=120)
    effective_definition_hash: str
    runtime_fingerprint_hash: str

    @field_validator("provider_alias", "adapter_id", "adapter_revision")
    @classmethod
    def strip_non_empty_identifier(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("quality evidence identifiers must not be empty")
        return stripped

    @field_validator(
        "provider_revision",
        "effective_definition_hash",
        "runtime_fingerprint_hash",
    )
    @classmethod
    def validate_sha256_digest(cls, value: str) -> str:
        stripped = value.strip()
        if re.fullmatch(r"sha256:[0-9a-f]{64}", stripped) is None:
            raise ValueError(
                "quality evidence hashes must be sha256:<64 lowercase hex>"
            )
        return stripped


_PIPELINE_PROVIDER_FIELDS = (
    "compose_provider",
    "identity_edit_provider",
    "refine_provider",
)
_PIPELINE_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")


def image_provider_reference_paths(image_config: Any) -> dict[str, str]:
    """枚举 image_providers 内全部 Provider 引用；生命周期逻辑共用此 seam。"""
    if not isinstance(image_config, dict):
        return {}

    references: dict[str, str] = {}

    def add(path: str, value: Any) -> None:
        alias = str(value or "").strip()
        if alias:
            references[path] = alias

    add("image_providers.default_provider", image_config.get("default_provider"))
    usages = image_config.get("usages")
    if isinstance(usages, dict):
        for usage in ("character_portrait", "cover", "scene_illustration"):
            add(f"image_providers.usages.{usage}", usages.get(usage))

    pipelines = image_config.get("pipelines")
    if isinstance(pipelines, dict):
        for pipeline_alias, pipeline in pipelines.items():
            if not isinstance(pipeline, dict):
                continue
            for field in _PIPELINE_PROVIDER_FIELDS:
                add(
                    f"image_providers.pipelines.{pipeline_alias}.{field}",
                    pipeline.get(field),
                )
    return references


def rename_image_provider_references(
    image_config: dict[str, Any],
    from_alias: str,
    to_alias: str,
) -> None:
    """原地迁移 Provider 别名；兼容历史值两侧空白。"""
    source = from_alias.strip()
    target = to_alias.strip()

    def matches(value: Any) -> bool:
        return str(value or "").strip() == source

    if matches(image_config.get("default_provider")):
        image_config["default_provider"] = target
    usages = image_config.get("usages")
    if isinstance(usages, dict):
        for usage, alias in usages.items():
            if matches(alias):
                usages[usage] = target
    pipelines = image_config.get("pipelines")
    if not isinstance(pipelines, dict):
        return
    for pipeline in pipelines.values():
        if not isinstance(pipeline, dict):
            continue
        for field in _PIPELINE_PROVIDER_FIELDS:
            if matches(pipeline.get(field)):
                pipeline[field] = target


def image_pipeline_reference_paths(image_config: Any) -> dict[str, str]:
    """枚举 Pipeline alias 引用；目前只有全局默认场景 Pipeline。"""
    if not isinstance(image_config, dict):
        return {}
    alias = str(image_config.get("default_scene_pipeline") or "").strip()
    return {"image_providers.default_scene_pipeline": alias} if alias else {}


def _canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _pipeline_stage_aliases(
    profile: IllustrationPipelineProfile,
) -> list[tuple[str, str]]:
    stages = [("compose", profile.compose_provider)]
    if isinstance(profile, ConsistencyIllustrationPipelineProfile):
        stages.append(("identity_edit", profile.identity_edit_provider))
        if profile.refine_provider:
            stages.append(("refine", profile.refine_provider))
    return stages


def _provider_without_secrets(provider: ImageProviderConfig) -> dict[str, Any]:
    return provider.model_dump(mode="json", exclude={"api_key"})


def compute_image_provider_revision(provider: ImageProviderConfig) -> str:
    """计算单个 Provider 的 secret-free 配置 revision。"""
    return _canonical_sha256(_provider_without_secrets(provider))


def compute_image_pipeline_revision(
    config: "ImageProvidersConfig",
    pipeline_alias: str,
) -> str:
    """计算配置 revision；包含运维配置，但永不包含 API Key。"""
    alias = _require_canonical_pipeline_alias(pipeline_alias)
    profile = config.pipelines.get(alias)
    if profile is None:
        raise ValueError(f"未找到图像 Pipeline 配置: {alias}")
    stages: dict[str, Any] = {}
    for stage, provider_alias in _pipeline_stage_aliases(profile):
        provider = config.providers.get(provider_alias)
        if provider is None:
            raise ValueError(
                f"图像 Pipeline {alias} 的 {stage} Provider 不存在: {provider_alias}"
            )
        stages[stage] = {
            "provider_alias": provider_alias,
            "provider_revision": compute_image_provider_revision(provider),
        }
    return _canonical_sha256(
        {
            "pipeline_alias": alias,
            "profile": profile.model_dump(mode="json", exclude_none=True),
            "stages": stages,
        }
    )


def _quality_provider_payload(provider: ImageProviderConfig) -> dict[str, Any]:
    if isinstance(provider, ComfyUIImageProviderConfig):
        workflow = provider.workflow
        return {
            "type": provider.type,
            "base_url": provider.base_url,
            "workflow": {
                "template_path": workflow.template_path,
                "template_revision": workflow.template_revision,
                "reference_mode": workflow.reference_mode,
                "bindings": {
                    key: value.model_dump(mode="json")
                    for key, value in workflow.bindings.items()
                },
                "overrides": [
                    value.model_dump(mode="json") for value in workflow.overrides
                ],
                "dependencies": workflow.dependencies.model_dump(mode="json"),
                "artifact_selector": [
                    output.model_dump(mode="json") for output in workflow.outputs
                ],
            },
        }
    return {
        "type": provider.type,
        "base_url": provider.base_url,
        "default_model": provider.default_model,
        "parameters": {
            key: value.model_dump(mode="json") if value is not None else None
            for key, value in provider.parameters.items()
        },
        "artifact_selector": {
            "items_path": provider.result.items_path,
            "url_field": provider.result.url_field,
            "base64_field": provider.result.base64_field,
        },
    }


def _require_canonical_pipeline_alias(pipeline_alias: str) -> str:
    if (
        pipeline_alias != pipeline_alias.strip()
        or _PIPELINE_ALIAS_RE.fullmatch(pipeline_alias) is None
    ):
        raise ValueError(f"图像 Pipeline 别名不是规范标识符: {pipeline_alias!r}")
    return pipeline_alias


def compute_image_pipeline_quality_fingerprint(
    config: "ImageProvidersConfig",
    pipeline_alias: str,
    *,
    stage_evidence: Mapping[
        str,
        ImagePipelineStageQualityEvidence | dict[str, Any],
    ],
) -> str:
    """计算部署质量指纹；缺少有效定义或 runtime 证据时拒绝生成。"""
    alias = _require_canonical_pipeline_alias(pipeline_alias)
    profile = config.pipelines.get(alias)
    if profile is None:
        raise ValueError(f"未找到图像 Pipeline 配置: {alias}")

    stage_aliases = _pipeline_stage_aliases(profile)
    expected_stages = {stage for stage, _provider_alias in stage_aliases}
    submitted_stages = set(stage_evidence)
    if submitted_stages != expected_stages:
        missing = sorted(expected_stages - submitted_stages)
        extra = sorted(submitted_stages - expected_stages)
        details: list[str] = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if extra:
            details.append("extra=" + ",".join(extra))
        raise ValueError(
            "quality fingerprint evidence must match pipeline stages: "
            + "; ".join(details)
        )

    stages: dict[str, Any] = {}
    for stage, provider_alias in stage_aliases:
        provider = config.providers.get(provider_alias)
        if provider is None:
            raise ValueError(
                f"图像 Pipeline {alias} 的 {stage} Provider 不存在: {provider_alias}"
            )
        evidence = ImagePipelineStageQualityEvidence.model_validate(
            stage_evidence[stage]
        )
        if evidence.provider_alias != provider_alias:
            raise ValueError(
                f"quality evidence Provider alias mismatch at {stage}: "
                f"expected {provider_alias}, got {evidence.provider_alias}"
            )
        expected_provider_revision = compute_image_provider_revision(provider)
        if evidence.provider_revision != expected_provider_revision:
            raise ValueError(
                f"quality evidence Provider revision mismatch at {stage}"
            )
        stages[stage] = {
            "provider_alias": provider_alias,
            "provider": _quality_provider_payload(provider),
            "evidence": evidence.model_dump(
                mode="json",
                exclude={"provider_alias", "provider_revision"},
            ),
        }
    return _canonical_sha256(
        {
            "schema_version": 1,
            "pipeline_alias": alias,
            "kind": profile.kind,
            "stages": stages,
        }
    )


class ImageProviderUsages(_StrictConfigModel):
    """各图像用途的默认 Provider 别名。"""

    character_portrait: str = ""
    cover: str = ""
    scene_illustration: str = ""

    @field_validator("character_portrait", "cover", "scene_illustration")
    @classmethod
    def strip_usage_aliases(cls, value: str) -> str:
        return value.strip()


class ImageProvidersConfig(_StrictConfigModel):
    """顶层 image_providers 配置段。"""

    default_provider: str = ""
    default_scene_pipeline: str = ""
    providers: dict[str, ImageProviderConfig] = Field(default_factory=dict)
    pipelines: dict[str, IllustrationPipelineProfile] = Field(default_factory=dict)
    usages: ImageProviderUsages = Field(default_factory=ImageProviderUsages)

    @field_validator("default_provider")
    @classmethod
    def strip_default_provider_alias(cls, value: str) -> str:
        return value.strip()

    @field_validator("default_scene_pipeline")
    @classmethod
    def validate_default_scene_pipeline_alias(cls, value: str) -> str:
        if not value:
            return ""
        if value != value.strip() or _PIPELINE_ALIAS_RE.fullmatch(value) is None:
            raise ValueError(
                "default_scene_pipeline must be an exact canonical Pipeline alias"
            )
        return value

    @model_validator(mode="after")
    def validate_aliases(self) -> "ImageProvidersConfig":
        if any(not str(alias).strip() for alias in self.providers):
            raise ValueError("image provider aliases must not be empty")
        invalid_pipeline_aliases = [
            str(alias)
            for alias in self.pipelines
            if str(alias) != str(alias).strip()
            or _PIPELINE_ALIAS_RE.fullmatch(str(alias)) is None
        ]
        if invalid_pipeline_aliases:
            raise ValueError(
                "image pipeline aliases must be canonical ASCII identifiers "
                "(1-120 chars: letters, digits, dot, underscore, hyphen): "
                + ", ".join(sorted(invalid_pipeline_aliases))
            )
        return self


def normalize_image_providers_config(value: Any) -> dict[str, Any]:
    """校验并补齐图像 Provider 配置，不接触网络或磁盘。"""
    raw = value if isinstance(value, dict) else {}
    return ImageProvidersConfig.model_validate(raw).model_dump(mode="python")


def get_image_providers_config() -> ImageProvidersConfig:
    """读取并解析当前生效的顶层 image_providers 配置。"""
    from backend.config.config import get_config_value

    return ImageProvidersConfig.model_validate(
        get_config_value("image_providers", {})
    )


def get_image_provider_config(alias: str | None = None) -> ImageProviderConfig:
    """按别名读取图像 Provider；未指定时使用默认别名。"""
    config = get_image_providers_config()
    provider_alias = (alias or config.default_provider).strip()
    if provider_alias not in config.providers:
        raise ValueError(f"未找到图像 Provider 配置: {provider_alias}")
    return config.providers[provider_alias]
