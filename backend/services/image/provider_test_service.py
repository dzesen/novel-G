"""ComfyUI connectivity, draft workflow inspection, and derived readiness checks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.config.image_providers import (
    ComfyUIImageProviderConfig,
    ImageProviderConfig,
    ImageProvidersConfig,
)
from backend.services.image.comfyui_client import (
    ComfyUIClient,
    ComfyUIProtocolError,
)
from backend.services.image.comfyui_failures import execution_failed_failure
from backend.services.image.comfyui_template import (
    LoadedComfyUITemplate,
    load_comfyui_template,
)
from backend.services.image.contracts import (
    ImageFailure,
    ImageFailureCode,
    ImageProviderError,
)


ControlScalar = str | int | float | bool | None
DependencyKind = Literal[
    "node_types",
    "checkpoints",
    "loras",
    "workflow_inputs",
]


class ImageProviderTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: str = Field(min_length=1)
    provider: ImageProviderConfig | None = None


class ImageProviderWorkflowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    class_type: str
    node_title: str
    input: str
    group: Literal["common", "advanced"]
    value_type: Literal["string", "integer", "number", "boolean", "choice", "unknown"]
    template_value: ControlScalar
    effective_value: ControlScalar
    overridden: bool
    choices: tuple[ControlScalar, ...] = ()
    minimum: float | int | None = None
    maximum: float | int | None = None
    step: float | int | None = None
    status: Literal["ready", "missing", "unknown"] = "unknown"
    issue: str = ""


class ImageProviderDependencyCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: DependencyKind
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
    effective_graph_hash: str = ""
    queue_running: int = 0
    queue_pending: int = 0
    checkpoint_names: tuple[str, ...] = ()
    lora_names: tuple[str, ...] = ()
    workflow_inputs: tuple[ImageProviderWorkflowInput, ...] = ()
    dependency_checks: tuple[ImageProviderDependencyCheck, ...] = ()
    failure: ImageFailure | None = None


_COMMON_INPUT_NAMES = {
    "ckpt_name",
    "unet_name",
    "model_name",
    "clip_name",
    "vae_name",
    "lora_name",
    "enable",
    "enabled",
}


def _dependency_check(
    *,
    kind: DependencyKind,
    required: list[str] | tuple[str, ...],
    discovered: tuple[str, ...],
) -> ImageProviderDependencyCheck:
    required_names = tuple(dict.fromkeys(required))
    discovered_names = set(discovered)
    available = tuple(name for name in required_names if name in discovered_names)
    missing = tuple(name for name in required_names if name not in discovered_names)
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
    missing = {check.kind: list(check.missing) for check in checks if check.missing}
    if not missing:
        return None
    return ImageFailure(
        code=ImageFailureCode.DEPENDENCY_MISSING,
        message="ComfyUI 已连接，但当前工作流所需的节点、资源或输入值尚未就绪",
        action="安装缺失资源，或把标出的工作流输入恢复为 ComfyUI 当前可用的值后重新检测",
        details={"missing": missing},
    )


def resolve_image_provider_for_test(
    request: ImageProviderTestRequest,
    raw_config: dict,
) -> tuple[str, ComfyUIImageProviderConfig]:
    alias = request.alias.strip()
    provider = request.provider
    if provider is None:
        image_config = ImageProvidersConfig.model_validate(
            raw_config.get("image_providers") or {}
        )
        provider = image_config.providers.get(alias)
        if provider is None:
            raise ValueError(f"图像 Provider 不存在：{alias}")
    if provider.type == "openai_compatible":
        raise ValueError("当前版本只支持 ComfyUI 图像后端")
    return alias, provider


def _object_input_spec(
    object_info: dict[str, Any],
    class_type: str,
    input_name: str,
) -> tuple[Any, dict[str, Any]] | None:
    node_info = object_info.get(class_type)
    if not isinstance(node_info, dict):
        return None
    input_info = node_info.get("input")
    if not isinstance(input_info, dict):
        return None
    for section_name in ("required", "optional"):
        section = input_info.get(section_name)
        if not isinstance(section, dict) or input_name not in section:
            continue
        raw_spec = section[input_name]
        if not isinstance(raw_spec, (list, tuple)) or not raw_spec:
            return None
        options = raw_spec[1] if len(raw_spec) > 1 and isinstance(raw_spec[1], dict) else {}
        return raw_spec[0], options
    return None


def _scalar_choices(raw_type: Any) -> tuple[ControlScalar, ...]:
    if not isinstance(raw_type, (list, tuple)):
        return ()
    return tuple(
        value
        for value in raw_type
        if value is None or isinstance(value, (str, int, float, bool))
    )


def _value_type(raw_type: Any, template_value: ControlScalar) -> Literal[
    "string", "integer", "number", "boolean", "choice", "unknown"
]:
    if isinstance(raw_type, (list, tuple)):
        return "choice"
    normalized = str(raw_type or "").upper()
    if normalized == "INT":
        return "integer"
    if normalized == "FLOAT":
        return "number"
    if normalized in {"BOOLEAN", "BOOL"}:
        return "boolean"
    if normalized == "STRING":
        return "string"
    if isinstance(template_value, bool):
        return "boolean"
    if isinstance(template_value, int):
        return "integer"
    if isinstance(template_value, float):
        return "number"
    if isinstance(template_value, str):
        return "string"
    return "unknown"


def _control_group(
    _class_type: str,
    input_name: str,
    template_value: ControlScalar,
) -> Literal["common", "advanced"]:
    if input_name.lower() in _COMMON_INPUT_NAMES or isinstance(template_value, bool):
        return "common"
    return "advanced"


def _control_status(
    *,
    value_type: str,
    effective_value: ControlScalar,
    choices: tuple[ControlScalar, ...],
    minimum: float | int | None,
    maximum: float | int | None,
    has_spec: bool,
) -> tuple[Literal["ready", "missing", "unknown"], str]:
    if not has_spec:
        return "unknown", "ComfyUI 未公开这个输入的可选范围；保存前请自行确认值有效"
    if choices and effective_value not in choices:
        return "missing", "当前值不在 ComfyUI 返回的可用选项中"
    if value_type == "integer" and (
        not isinstance(effective_value, int) or isinstance(effective_value, bool)
    ):
        return "missing", "当前值不是整数"
    if value_type == "number" and (
        not isinstance(effective_value, (int, float)) or isinstance(effective_value, bool)
    ):
        return "missing", "当前值不是数字"
    if value_type == "boolean" and not isinstance(effective_value, bool):
        return "missing", "当前值不是布尔值"
    if value_type == "string" and not isinstance(effective_value, str):
        return "missing", "当前值不是字符串"
    if (
        minimum is not None
        and isinstance(effective_value, (int, float))
        and not isinstance(effective_value, bool)
        and effective_value < minimum
    ):
        return "missing", f"当前值小于 ComfyUI 允许的最小值 {minimum}"
    if (
        maximum is not None
        and isinstance(effective_value, (int, float))
        and not isinstance(effective_value, bool)
        and effective_value > maximum
    ):
        return "missing", f"当前值大于 ComfyUI 允许的最大值 {maximum}"
    return "ready", ""


def _workflow_input_controls(
    loaded: LoadedComfyUITemplate,
    provider: ComfyUIImageProviderConfig,
    object_info: dict[str, Any],
) -> tuple[ImageProviderWorkflowInput, ...]:
    bound_targets = {
        (binding.node_id, binding.input)
        for binding in provider.workflow.bindings.values()
    }
    override_targets = {
        (override.node_id, override.input)
        for override in provider.workflow.overrides
    }
    controls: list[ImageProviderWorkflowInput] = []
    for node_id in sorted(loaded.workflow):
        node = loaded.workflow[node_id]
        template_node = loaded.template_workflow[node_id]
        class_type = str(node["class_type"])
        raw_meta = node.get("_meta")
        node_title = (
            str(raw_meta.get("title") or class_type)
            if isinstance(raw_meta, dict)
            else class_type
        )
        for input_name in sorted(node["inputs"]):
            target = (node_id, input_name)
            template_value = template_node["inputs"][input_name]
            effective_value = node["inputs"][input_name]
            if target in bound_targets or not (
                effective_value is None
                or isinstance(effective_value, (str, int, float, bool))
            ):
                continue
            spec = _object_input_spec(object_info, class_type, input_name)
            raw_type, options = spec if spec is not None else (None, {})
            choices = _scalar_choices(raw_type)
            minimum = options.get("min") if isinstance(options.get("min"), (int, float)) else None
            maximum = options.get("max") if isinstance(options.get("max"), (int, float)) else None
            step = options.get("step") if isinstance(options.get("step"), (int, float)) else None
            value_type = _value_type(raw_type, template_value)
            status, issue = _control_status(
                value_type=value_type,
                effective_value=effective_value,
                choices=choices,
                minimum=minimum,
                maximum=maximum,
                has_spec=spec is not None,
            )
            controls.append(
                ImageProviderWorkflowInput(
                    node_id=node_id,
                    class_type=class_type,
                    node_title=node_title,
                    input=input_name,
                    group=_control_group(class_type, input_name, template_value),
                    value_type=value_type,
                    template_value=template_value,
                    effective_value=effective_value,
                    overridden=target in override_targets,
                    choices=choices,
                    minimum=minimum,
                    maximum=maximum,
                    step=step,
                    status=status,
                    issue=issue,
                )
            )
    return tuple(controls)


def _workflow_input_check(
    controls: tuple[ImageProviderWorkflowInput, ...],
) -> ImageProviderDependencyCheck:
    labeled_controls = tuple(
        (
            f"{control.node_id}.{control.input}="
            f"{json.dumps(control.effective_value, ensure_ascii=False)}",
            control,
        )
        for control in controls
        if control.status != "unknown"
    )
    checkable = tuple(label for label, _control in labeled_controls)
    available = tuple(
        label
        for label, control in labeled_controls
        if control.status == "ready"
    )
    return _dependency_check(
        kind="workflow_inputs",
        required=checkable,
        discovered=available,
    )


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
    effective_graph_hash = ""
    comfyui_version = ""
    queue_running = 0
    queue_pending = 0
    checkpoint_names: tuple[str, ...] = ()
    lora_names: tuple[str, ...] = ()
    workflow_inputs: tuple[ImageProviderWorkflowInput, ...] = ()
    dependency_checks: tuple[ImageProviderDependencyCheck, ...] = ()
    try:
        probe_workflow = provider.workflow.model_copy(update={"template_revision": ""})
        probe_provider = provider.model_copy(update={"workflow": probe_workflow})
        loaded = load_comfyui_template(probe_workflow, template_root=template_root)
        template_revision = loaded.revision
        effective_graph_hash = loaded.effective_graph_hash
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

        object_info = await active_client.get_object_info()
        available_checkpoints = await active_client.get_model_names("checkpoints")
        available_loras = await active_client.get_model_names("loras")
        workflow_inputs = _workflow_input_controls(loaded, probe_provider, object_info)
        dependencies = provider.workflow.dependencies
        required_node_types = [
            str(node["class_type"])
            for node in loaded.workflow.values()
        ] + dependencies.node_types
        dependency_checks = (
            _dependency_check(
                kind="node_types",
                required=required_node_types,
                discovered=tuple(object_info),
            ),
            _dependency_check(
                kind="checkpoints",
                required=[*checkpoint_names, *dependencies.checkpoints],
                discovered=available_checkpoints,
            ),
            _dependency_check(
                kind="loras",
                required=[*lora_names, *dependencies.loras],
                discovered=available_loras,
            ),
            _workflow_input_check(workflow_inputs),
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
                effective_graph_hash=effective_graph_hash,
                queue_running=queue_running,
                queue_pending=queue_pending,
                checkpoint_names=checkpoint_names,
                lora_names=lora_names,
                workflow_inputs=workflow_inputs,
                dependency_checks=dependency_checks,
                failure=dependency_failure,
            )

        return ImageProviderTestResponse(
            alias=alias,
            provider_type="comfyui",
            status="passed",
            summary="ComfyUI 连接、API-format 模板、队列接口与自动推导的工作流依赖均可用",
            comfyui_version=comfyui_version,
            template_revision=template_revision,
            effective_graph_hash=effective_graph_hash,
            queue_running=queue_running,
            queue_pending=queue_pending,
            checkpoint_names=checkpoint_names,
            lora_names=lora_names,
            workflow_inputs=workflow_inputs,
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
            effective_graph_hash=effective_graph_hash,
            queue_running=queue_running,
            queue_pending=queue_pending,
            checkpoint_names=checkpoint_names,
            lora_names=lora_names,
            workflow_inputs=workflow_inputs,
            dependency_checks=dependency_checks,
            failure=error.failure,
        )
    except ComfyUIProtocolError:
        failure = execution_failed_failure(
            message="ComfyUI 返回了与当前适配器不兼容的连通性响应",
            action="确认 ComfyUI 版本与地址，并重新检测当前草稿",
        )
        return ImageProviderTestResponse(
            alias=alias,
            provider_type="comfyui",
            status="failed",
            summary=failure.message,
            comfyui_version=comfyui_version,
            template_revision=template_revision,
            effective_graph_hash=effective_graph_hash,
            queue_running=queue_running,
            queue_pending=queue_pending,
            checkpoint_names=checkpoint_names,
            lora_names=lora_names,
            workflow_inputs=workflow_inputs,
            dependency_checks=dependency_checks,
            failure=failure,
        )
    finally:
        if owns_client:
            await active_client.aclose()
