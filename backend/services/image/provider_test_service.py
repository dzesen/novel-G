"""图像 Provider 连通性与本地模板就绪度检查。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.config.image_providers import (
    ComfyUIImageProviderConfig,
    ImageProvidersConfig,
)
from backend.services.image.comfyui_client import (
    ComfyUIClient,
    ComfyUIProtocolError,
)
from backend.services.image.comfyui_failures import execution_failed_failure
from backend.services.image.comfyui_template import load_comfyui_template
from backend.services.image.contracts import ImageFailure, ImageProviderError


class ImageProviderTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: str = Field(min_length=1)


class ImageProviderTestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: str
    provider_type: str
    status: Literal["passed", "failed"]
    summary: str
    comfyui_version: str = ""
    template_revision: str = ""
    queue_running: int = 0
    queue_pending: int = 0
    checkpoint_names: tuple[str, ...] = ()
    lora_names: tuple[str, ...] = ()
    failure: ImageFailure | None = None


def resolve_image_provider_for_test(
    request: ImageProviderTestRequest,
    raw_config: dict,
) -> tuple[str, ComfyUIImageProviderConfig]:
    image_config = ImageProvidersConfig.model_validate(
        raw_config.get("image_providers") or {}
    )
    alias = request.alias.strip()
    provider = image_config.providers.get(alias)
    if provider is None:
        raise ValueError(f"图像 Provider 不存在：{alias}")
    if provider.type == "openai_compatible":
        raise ValueError("openai_compatible 图像适配器将在切片 10 接入")
    return alias, provider


async def test_image_provider_connection(
    request: ImageProviderTestRequest,
    raw_config: dict,
    *,
    template_root: Path,
    client: ComfyUIClient | None = None,
) -> ImageProviderTestResponse:
    alias, provider = resolve_image_provider_for_test(request, raw_config)
    active_client = client or ComfyUIClient(
        base_url=provider.base_url,
        timeout_seconds=provider.timeout_seconds,
    )
    owns_client = client is None
    try:
        loaded = load_comfyui_template(
            provider.workflow,
            template_root=template_root,
        )
        system_stats = await active_client.get_system_stats()
        queue = await active_client.get_queue()
        system = system_stats.get("system")
        system = system if isinstance(system, dict) else {}
        version = str(system.get("comfyui_version") or "").strip()
        if not version:
            raise ComfyUIProtocolError(
                "ComfyUI /system_stats did not include comfyui_version"
            )
        return ImageProviderTestResponse(
            alias=alias,
            provider_type="comfyui",
            status="passed",
            summary="ComfyUI 连接、API-format 模板与队列接口均可用",
            comfyui_version=version,
            template_revision=loaded.revision,
            queue_running=len(queue.running),
            queue_pending=len(queue.pending),
            checkpoint_names=loaded.checkpoint_names,
            lora_names=loaded.lora_names,
        )
    except ImageProviderError as error:
        return ImageProviderTestResponse(
            alias=alias,
            provider_type="comfyui",
            status="failed",
            summary=error.failure.message,
            failure=error.failure,
        )
    except ComfyUIProtocolError:
        failure = execution_failed_failure(
            message="ComfyUI 返回了与当前适配器不兼容的连通性响应",
            action="确认 ComfyUI 版本与地址，并重新运行连通性测试",
        )
        return ImageProviderTestResponse(
            alias=alias,
            provider_type="comfyui",
            status="failed",
            summary=failure.message,
            failure=failure,
        )
    finally:
        if owns_client:
            await active_client.aclose()
