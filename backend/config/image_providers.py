"""图像 Provider 的协议分型配置模型。"""

from __future__ import annotations

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


class ImageProviderUsages(_StrictConfigModel):
    """各图像用途的默认 Provider 别名。"""

    character_portrait: str = ""
    cover: str = ""
    scene_illustration: str = ""


class ImageProvidersConfig(_StrictConfigModel):
    """顶层 image_providers 配置段。"""

    default_provider: str = ""
    providers: dict[str, ImageProviderConfig] = Field(default_factory=dict)
    usages: ImageProviderUsages = Field(default_factory=ImageProviderUsages)

    @model_validator(mode="after")
    def validate_provider_aliases(self) -> "ImageProvidersConfig":
        if any(not str(alias).strip() for alias in self.providers):
            raise ValueError("image provider aliases must not be empty")
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
