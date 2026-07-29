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
from backend.services.image.contracts import (
    ImageFailure,
    ImageFailureCode,
    ImageProviderError,
)


class ImageProviderTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: str = Field(min_length=1)


class ImageProviderDependencyCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["node_types", "checkpoints", "loras"]
    required: tuple[str, ...] = ()
    available: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    status: Literal["passed", "failed"]


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
    dependency_checks: tuple[ImageProviderDependencyCheck, ...] = ()
    failure: ImageFailure | None = None


def _dependency_check(
    *,
    kind: Literal["node_types", "checkpoints", "loras"],
    required: list[str],
    discovered: tuple[str, ...],
) -> ImageProviderDependencyCheck:
    required_names = tuple(dict.fromkeys(required))
    discovered_names = set(discovered)
    available = tuple(
        name for name in required_names if name in discovered_names
    )
    missing = tuple(
        name for name in required_names if name not in discovered_names
    )
    return ImageProviderDependencyCheck(
        kind=kind,
        required=required_names,
        available=available,
        missing=missing,
        status="failed" if missing else "passed",
    )


def _dependency_failure(
    checks: tuple[ImageProviderDependencyCheck, ...],
) -> ImageFailure | None:
    missing = {
        check.kind: list(check.missing)
        for check in checks
        if check.missing
    }
    if not missing:
        return None
    return ImageFailure(
        code=ImageFailureCode.DEPENDENCY_MISSING,
        message="ComfyUI 已连接，但 workflow 声明的依赖未就绪",
        action=(
            "安装缺失的节点包、checkpoint 或 LoRA，"
            "或修正 workflow.dependencies 后重新测试"
        ),
        details={"missing": missing},
    )


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
        raise ValueError("当前版本只支持 ComfyUI 图像后端")
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
    template_revision = ""
    comfyui_version = ""
    queue_running = 0
    queue_pending = 0
    checkpoint_names: tuple[str, ...] = ()
    lora_names: tuple[str, ...] = ()
    dependency_checks: tuple[ImageProviderDependencyCheck, ...] = ()
    try:
        probe_workflow = provider.workflow.model_copy(
            update={"template_revision": ""}
        )
        loaded = load_comfyui_template(
            probe_workflow,
            template_root=template_root,
        )
        template_revision = loaded.revision
        checkpoint_names = loaded.checkpoint_names
        lora_names = loaded.lora_names

        system_stats = await active_client.get_system_stats()
        queue = await active_client.get_queue()
        system = system_stats.get("system")
        system = system if isinstance(system, dict) else {}
        comfyui_version = str(system.get("comfyui_version") or "").strip()
        if not comfyui_version:
            raise ComfyUIProtocolError(
                "ComfyUI /system_stats did not include comfyui_version"
            )
        queue_running = len(queue.running)
        queue_pending = len(queue.pending)

        available_node_types = await active_client.get_object_info()
        available_checkpoints = await active_client.get_model_names(
            "checkpoints"
        )
        available_loras = await active_client.get_model_names("loras")
        dependencies = provider.workflow.dependencies
        dependency_checks = (
            _dependency_check(
                kind="node_types",
                required=dependencies.node_types,
                discovered=available_node_types,
            ),
            _dependency_check(
                kind="checkpoints",
                required=dependencies.checkpoints,
                discovered=available_checkpoints,
            ),
            _dependency_check(
                kind="loras",
                required=dependencies.loras,
                discovered=available_loras,
            ),
        )
        dependency_failure = _dependency_failure(dependency_checks)
        if dependency_failure is not None:
            return ImageProviderTestResponse(
                alias=alias,
                provider_type="comfyui",
                status="failed",
                summary=dependency_failure.message,
                comfyui_version=comfyui_version,
                template_revision=template_revision,
                queue_running=queue_running,
                queue_pending=queue_pending,
                checkpoint_names=checkpoint_names,
                lora_names=lora_names,
                dependency_checks=dependency_checks,
                failure=dependency_failure,
            )

        return ImageProviderTestResponse(
            alias=alias,
            provider_type="comfyui",
            status="passed",
            summary=(
                "ComfyUI 连接、API-format 模板、队列接口与声明依赖均可用"
            ),
            comfyui_version=comfyui_version,
            template_revision=template_revision,
            queue_running=queue_running,
            queue_pending=queue_pending,
            checkpoint_names=checkpoint_names,
            lora_names=lora_names,
            dependency_checks=dependency_checks,
        )
    except ImageProviderError as error:
        return ImageProviderTestResponse(
            alias=alias,
            provider_type="comfyui",
            status="failed",
            summary=error.failure.message,
            comfyui_version=comfyui_version,
            template_revision=template_revision,
            queue_running=queue_running,
            queue_pending=queue_pending,
            checkpoint_names=checkpoint_names,
            lora_names=lora_names,
            dependency_checks=dependency_checks,
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
            comfyui_version=comfyui_version,
            template_revision=template_revision,
            queue_running=queue_running,
            queue_pending=queue_pending,
            checkpoint_names=checkpoint_names,
            lora_names=lora_names,
            dependency_checks=dependency_checks,
            failure=failure,
        )
    finally:
        if owns_client:
            await active_client.aclose()
