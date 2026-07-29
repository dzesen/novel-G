"""ComfyUI API-format 模板加载、校验与只替换节点输入。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

from backend.config.image_providers import ComfyUIWorkflowConfig
from backend.services.image.contracts import (
    ImageFailure,
    ImageFailureCode,
    ImageGenerationRequest,
    ImageInputAsset,
    ImageProviderError,
    ImageSlotValue,
)


JsonScalar = str | int | float | bool | None


@dataclass(frozen=True)
class LoadedComfyUITemplate:
    workflow: dict[str, dict[str, Any]]
    revision: str
    checkpoint_names: tuple[str, ...]
    lora_names: tuple[str, ...]


@dataclass(frozen=True)
class PreparedComfyUITemplate:
    loaded: LoadedComfyUITemplate
    accepted_slots: dict[str, ImageSlotValue]
    ignored_slots: tuple[str, ...]


@dataclass(frozen=True)
class RenderedComfyUIWorkflow:
    workflow: dict[str, dict[str, Any]]
    graph_hash: str


def _template_failure(message: str, action: str, **details: Any) -> ImageProviderError:
    return ImageProviderError(
        ImageFailure(
            code=ImageFailureCode.WORKFLOW_VALIDATION_FAILED,
            message=message,
            action=action,
            details=details,
        )
    )


def _resolve_template_path(template_root: Path, configured_path: str) -> Path:
    if not configured_path.strip():
        raise _template_failure(
            "ComfyUI workflow 模板路径为空",
            "在图像 Provider 配置中选择一份 API-format workflow JSON 模板",
        )
    root = Path(template_root).resolve()
    candidate = Path(configured_path)
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (root / candidate).resolve()
        try:
            resolved.relative_to(root)
        except ValueError as error:
            raise _template_failure(
                "ComfyUI workflow 模板的相对路径越出配置目录",
                "改用配置目录内的相对路径，或直接填写模板的绝对路径",
            ) from error
    if not resolved.exists():
        raise _template_failure(
            "ComfyUI workflow 模板不存在",
            "确认模板路径正确，并重新选择 API-format workflow JSON 文件",
        )
    if not resolved.is_file():
        raise _template_failure(
            "ComfyUI workflow 模板路径不是常规文件",
            "选择一份可读的 API-format workflow JSON 文件，而不是目录",
        )
    return resolved


def _is_connection(value: Any) -> bool:
    return bool(
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], str)
        and isinstance(value[1], int)
        and not isinstance(value[1], bool)
    )


def _is_json_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _validate_api_format(workflow: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(workflow, dict) or not workflow:
        raise _template_failure(
            "ComfyUI workflow 模板不是非空 API-format 节点图",
            "从 ComfyUI 导出 API format，而不是 UI workflow format",
        )
    if "nodes" in workflow or "links" in workflow:
        raise _template_failure(
            "检测到 UI workflow format，不能提交给 /prompt",
            "从 ComfyUI 重新导出 API format 模板",
        )
    normalized: dict[str, dict[str, Any]] = {}
    for raw_node_id, raw_node in workflow.items():
        node_id = str(raw_node_id)
        if not isinstance(raw_node, dict):
            raise _template_failure(
                f"workflow 节点 {node_id} 不是对象",
                "修正模板中的节点结构后重试",
                node_id=node_id,
            )
        class_type = raw_node.get("class_type")
        inputs = raw_node.get("inputs")
        if not isinstance(class_type, str) or not class_type.strip():
            raise _template_failure(
                f"workflow 节点 {node_id} 缺少 class_type",
                "重新导出或修正 API-format 模板",
                node_id=node_id,
            )
        if not isinstance(inputs, dict):
            raise _template_failure(
                f"workflow 节点 {node_id} 缺少 inputs 对象",
                "重新导出或修正 API-format 模板",
                node_id=node_id,
            )
        normalized[node_id] = deepcopy(raw_node)
    return normalized


def _dependency_names(
    workflow: dict[str, dict[str, Any]],
    *,
    input_name: str,
) -> tuple[str, ...]:
    values = {
        str(node["inputs"][input_name])
        for node in workflow.values()
        if input_name in node["inputs"]
        and isinstance(node["inputs"][input_name], str)
        and str(node["inputs"][input_name]).strip()
    }
    return tuple(sorted(values))


def load_comfyui_template(
    workflow_config: ComfyUIWorkflowConfig,
    *,
    template_root: Path,
) -> LoadedComfyUITemplate:
    """加载模板并核对由后端计算的内容 revision。"""

    path = _resolve_template_path(template_root, workflow_config.template_path)
    try:
        content = path.read_bytes()
    except OSError as error:
        raise _template_failure(
            "无法读取 ComfyUI workflow 模板",
            "确认模板仍存在且当前进程有读取权限",
        ) from error
    revision = f"sha256:{hashlib.sha256(content).hexdigest()}"
    configured_revision = workflow_config.template_revision
    if configured_revision and configured_revision != revision:
        raise _template_failure(
            "ComfyUI workflow 模板内容已变化",
            "重新运行图像 Provider 连通性测试，使用后端计算的新 template_revision 更新配置",
            configured_revision=configured_revision,
            actual_revision=revision,
        )
    try:
        raw_workflow = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _template_failure(
            "ComfyUI workflow 模板不是有效的 UTF-8 JSON",
            "重新导出 API-format workflow JSON",
        ) from error
    workflow = _validate_api_format(raw_workflow)

    for slot, binding in workflow_config.bindings.items():
        node = workflow.get(binding.node_id)
        if node is None:
            raise _template_failure(
                f"语义槽位 {slot} 指向不存在的节点 {binding.node_id}",
                "在配置中改正节点槽位映射，或重新导出模板",
                slot=slot,
                node_id=binding.node_id,
            )
        inputs = node["inputs"]
        if binding.input not in inputs:
            raise _template_failure(
                f"语义槽位 {slot} 指向节点中不存在的输入 {binding.input}",
                "改正节点输入名；适配器不会替模板新增输入",
                slot=slot,
                node_id=binding.node_id,
                input_name=binding.input,
            )
        current_value = inputs[binding.input]
        if _is_connection(current_value):
            raise _template_failure(
                f"语义槽位 {slot} 指向连线输入，拒绝替换",
                "把槽位绑定到模板中已存在的标量输入，保留节点连线",
                slot=slot,
                node_id=binding.node_id,
                input_name=binding.input,
            )
        if not _is_json_scalar(current_value):
            raise _template_failure(
                f"语义槽位 {slot} 指向的输入不是标量",
                "把槽位绑定到字符串、数字、布尔值或 null 标量输入",
                slot=slot,
                node_id=binding.node_id,
                input_name=binding.input,
            )

    for output in workflow_config.outputs:
        if output.node_id not in workflow:
            raise _template_failure(
                f"产物映射指向不存在的节点 {output.node_id}",
                "把 outputs.node_id 改成模板中的实际产物节点",
                node_id=output.node_id,
                field=output.field,
            )

    return LoadedComfyUITemplate(
        workflow=workflow,
        revision=revision,
        checkpoint_names=_dependency_names(workflow, input_name="ckpt_name"),
        lora_names=_dependency_names(workflow, input_name="lora_name"),
    )


def prepare_comfyui_template(
    workflow_config: ComfyUIWorkflowConfig,
    request: ImageGenerationRequest,
    *,
    template_root: Path,
) -> PreparedComfyUITemplate:
    """确定可发送、必须拒绝及要回报上层的语义槽位。"""

    loaded = load_comfyui_template(workflow_config, template_root=template_root)
    bindings = workflow_config.bindings
    undeclared_required = sorted(set(request.required_slots) - set(bindings))
    if undeclared_required:
        missing = ", ".join(undeclared_required)
        raise _template_failure(
            f"当前 workflow 模板未声明用途所需槽位：{missing}",
            "选择声明这些槽位的模板，或补齐模板的节点槽位映射",
            missing_slots=undeclared_required,
            usage=request.usage,
        )
    if (
        "reference_image" in request.required_slots
        and workflow_config.reference_mode == "none"
    ):
        raise _template_failure(
            "当前 workflow 模板未声明参考图条件化模式",
            "选择 reference_mode 非 none 且声明 reference_image 槽位的模板",
            missing_slots=["reference_mode"],
            usage=request.usage,
        )

    required_values = set(request.required_slots)
    required_values.update(
        slot for slot, binding in bindings.items() if binding.required
    )
    missing_values = sorted(
        slot
        for slot in required_values
        if slot not in request.slot_values or request.slot_values[slot] is None
    )
    if missing_values:
        missing = ", ".join(missing_values)
        raise _template_failure(
            f"请求缺少 workflow 必填槽位值：{missing}",
            "补齐这些语义槽位的值后重新发起",
            missing_slots=missing_values,
            usage=request.usage,
        )

    ignored_slots = tuple(
        sorted(
            slot
            for slot, value in request.slot_values.items()
            if value is not None and slot not in bindings
        )
    )
    accepted_slots: dict[str, ImageSlotValue] = {}
    for slot, value in request.slot_values.items():
        if value is None or slot not in bindings:
            continue
        binding = bindings[slot]
        if binding.upload and not isinstance(value, ImageInputAsset):
            raise _template_failure(
                f"语义槽位 {slot} 必须提供待上传的图像素材",
                "为该槽位传入内存图像素材，而不是路径或远程 URL",
                slot=slot,
            )
        if not binding.upload and isinstance(value, ImageInputAsset):
            raise _template_failure(
                f"语义槽位 {slot} 未声明 upload，不能接收图像素材",
                "把模板槽位声明为 upload，或改为传入标量值",
                slot=slot,
            )
        if not isinstance(value, ImageInputAsset) and not _is_json_scalar(value):
            raise _template_failure(
                f"语义槽位 {slot} 的值不是可提交标量",
                "改为字符串、数字或布尔值",
                slot=slot,
            )
        accepted_slots[slot] = value
    return PreparedComfyUITemplate(
        loaded=loaded,
        accepted_slots=accepted_slots,
        ignored_slots=ignored_slots,
    )


def _topology_snapshot(
    workflow: dict[str, dict[str, Any]],
) -> tuple[
    tuple[str, ...],
    tuple[tuple[str, str], ...],
    tuple[tuple[str, str, str, int], ...],
]:
    node_ids = tuple(sorted(workflow))
    class_types = tuple(
        (node_id, str(workflow[node_id]["class_type"]))
        for node_id in node_ids
    )
    connections = tuple(
        sorted(
            (
                node_id,
                input_name,
                str(value[0]),
                int(value[1]),
            )
            for node_id in node_ids
            for input_name, value in workflow[node_id]["inputs"].items()
            if _is_connection(value)
        )
    )
    return node_ids, class_types, connections


def render_comfyui_template(
    workflow_config: ComfyUIWorkflowConfig,
    prepared: PreparedComfyUITemplate,
    resolved_values: dict[str, JsonScalar],
) -> RenderedComfyUIWorkflow:
    """只替换已验证的标量输入，并在返回前断言拓扑逐项不变。"""

    workflow = deepcopy(prepared.loaded.workflow)
    before_topology = _topology_snapshot(prepared.loaded.workflow)
    if set(resolved_values) != set(prepared.accepted_slots):
        raise _template_failure(
            "内部槽位解析结果与已接受槽位不一致",
            "重新发起任务；若持续出现请检查 Provider 适配器版本",
        )
    for slot, value in resolved_values.items():
        if not _is_json_scalar(value):
            raise _template_failure(
                f"语义槽位 {slot} 上传后没有得到标量值",
                "检查 ComfyUI 上传响应并重新发起",
                slot=slot,
            )
        binding = workflow_config.bindings[slot]
        workflow[binding.node_id]["inputs"][binding.input] = value

    after_topology = _topology_snapshot(workflow)
    if after_topology != before_topology:
        raise _template_failure(
            "渲染 workflow 时检测到拓扑变化",
            "停止使用当前适配器并检查节点槽位映射；程序只允许替换标量输入",
        )

    encoded = json.dumps(
        workflow,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return RenderedComfyUIWorkflow(
        workflow=workflow,
        graph_hash=f"sha256:{hashlib.sha256(encoded).hexdigest()}",
    )
