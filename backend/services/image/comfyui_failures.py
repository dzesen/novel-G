"""按 ComfyUI 实测结构分类失败，并清洗所有可展示文本。"""

from __future__ import annotations

import re
from typing import Any

from backend.services.image.contracts import ImageFailure, ImageFailureCode


_WINDOWS_PATH_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])[A-Z]:[\\/][^\r\n'\"<>]*"
)
_UNIX_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_])/(?:[^/\s'\"<>]+/)+[^/\s'\"<>]*"
)
_WHITESPACE_RE = re.compile(r"\s+")
_OOM_MARKERS = (
    "outofmemory",
    "out of memory",
    "cuda error: out of memory",
    "allocation on device",
    "not enough memory",
)


def sanitize_comfyui_text(value: Any, *, limit: int = 300) -> str:
    """移除绝对路径、换行与超长尾部；traceback 从不传入本函数。"""

    text = str(value or "")
    text = _WINDOWS_PATH_RE.sub("<本机路径>", text)
    text = _UNIX_PATH_RE.sub("<本机路径>", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    if len(text) > limit:
        return f"{text[: limit - 1]}…"
    return text


def provider_unavailable_failure() -> ImageFailure:
    return ImageFailure(
        code=ImageFailureCode.PROVIDER_UNAVAILABLE,
        message="无法连接 ComfyUI",
        action="确认 ComfyUI 已启动，并检查 Provider 的 base_url 与端口",
    )


def queue_full_failure(*, running: int, pending: int, limit: int) -> ImageFailure:
    return ImageFailure(
        code=ImageFailureCode.QUEUE_FULL,
        message="ComfyUI 队列已达到当前 Provider 的并发上限",
        action="等待当前任务结束后再发起，不要重复提交",
        details={"running": running, "pending": pending, "limit": limit},
    )


def job_lost_failure(prompt_id: str) -> ImageFailure:
    return ImageFailure(
        code=ImageFailureCode.JOB_LOST,
        message="ComfyUI 已找不到这个任务",
        action="确认任务确实不在 ComfyUI 中后，由用户手工重新发起；系统不会自动重跑",
        details={"prompt_id": prompt_id},
    )


def asset_expired_failure() -> ImageFailure:
    return ImageFailure(
        code=ImageFailureCode.ASSET_EXPIRED,
        message="ComfyUI 产物已经过期，无法从 /view 取回",
        action="如仍需要这张图，请由用户手工重新发起；系统不会自动重跑 workflow",
    )


def cancelled_failure() -> ImageFailure:
    return ImageFailure(
        code=ImageFailureCode.CANCELLED,
        message="图像任务已取消",
        action="如仍需要图片，请重新确认输入后手工发起",
    )


def execution_failed_failure(
    *,
    message: str,
    action: str,
    details: dict[str, Any] | None = None,
) -> ImageFailure:
    return ImageFailure(
        code=ImageFailureCode.EXECUTION_FAILED,
        message=sanitize_comfyui_text(message),
        action=action,
        details=details or {},
    )


def _iter_node_errors(payload: dict[str, Any]):
    node_errors = payload.get("node_errors")
    if not isinstance(node_errors, dict):
        return
    for raw_node_id, raw_node_error in node_errors.items():
        if not isinstance(raw_node_error, dict):
            continue
        node_id = str(raw_node_id)
        class_type = sanitize_comfyui_text(raw_node_error.get("class_type"))
        errors = raw_node_error.get("errors")
        if not isinstance(errors, list):
            continue
        for raw_error in errors:
            if isinstance(raw_error, dict):
                yield node_id, class_type, raw_error


def classify_prompt_response(
    *,
    status_code: int,
    payload: dict[str, Any],
) -> ImageFailure | None:
    """分类 `/prompt` 同步响应；成功且 node_errors 为空时返回 None。"""

    node_errors = payload.get("node_errors")
    has_node_errors = isinstance(node_errors, dict) and bool(node_errors)
    if 200 <= status_code < 300 and not has_node_errors:
        return None
    if 200 <= status_code < 300:
        return ImageFailure(
            code=ImageFailureCode.PARTIAL_WORKFLOW_VALIDATION,
            message="ComfyUI 只接受了 workflow 的部分输出分支",
            action="修正 node_errors 指出的无效分支后再提交；本项目不会静默执行剩余分支",
            details={"node_ids": sorted(str(node_id) for node_id in node_errors)},
        )

    for node_id, class_type, error in _iter_node_errors(payload):
        extra_info = error.get("extra_info")
        extra_info = extra_info if isinstance(extra_info, dict) else {}
        input_name = sanitize_comfyui_text(extra_info.get("input_name"))
        error_type = sanitize_comfyui_text(error.get("type"))
        if error_type == "value_not_in_list" and input_name in {
            "ckpt_name",
            "lora_name",
        }:
            dependency_kind = "checkpoint" if input_name == "ckpt_name" else "LoRA"
            received = sanitize_comfyui_text(extra_info.get("received_value"))
            return ImageFailure(
                code=ImageFailureCode.DEPENDENCY_MISSING,
                message=f"ComfyUI 未加载模板要求的 {dependency_kind}：{received}",
                action=f"安装该 {dependency_kind}，或把模板改为当前部署已有的名称",
                details={
                    "node_id": node_id,
                    "node_type": class_type,
                    "error_type": error_type,
                    "input_name": input_name,
                    "received_value": received,
                },
            )

    top_error = payload.get("error")
    top_error = top_error if isinstance(top_error, dict) else {}
    first_node_error = next(_iter_node_errors(payload), None)
    details: dict[str, Any] = {
        "error_type": sanitize_comfyui_text(top_error.get("type")),
    }
    if first_node_error is not None:
        node_id, class_type, error = first_node_error
        extra_info = error.get("extra_info")
        extra_info = extra_info if isinstance(extra_info, dict) else {}
        details.update(
            {
                "node_id": node_id,
                "node_type": class_type,
                "node_error_type": sanitize_comfyui_text(error.get("type")),
                "input_name": sanitize_comfyui_text(extra_info.get("input_name")),
            }
        )
    summary = sanitize_comfyui_text(
        top_error.get("message") or top_error.get("details")
    )
    return ImageFailure(
        code=ImageFailureCode.WORKFLOW_VALIDATION_FAILED,
        message=summary or "ComfyUI 拒绝了 workflow",
        action="按节点 ID、节点类型和输入名修正 API-format 模板后重新提交",
        details=details,
    )


def classify_execution_error(event_data: dict[str, Any]) -> ImageFailure:
    """分类 history 的 execution_error；只保留允许回传的字段。"""

    exception_type = sanitize_comfyui_text(event_data.get("exception_type"))
    exception_message = sanitize_comfyui_text(event_data.get("exception_message"))
    searchable = f"{exception_type} {exception_message}".lower().replace("_", "")
    details = {
        "node_id": sanitize_comfyui_text(event_data.get("node_id")),
        "node_type": sanitize_comfyui_text(event_data.get("node_type")),
        "exception_type": exception_type,
    }
    if any(marker in searchable for marker in _OOM_MARKERS):
        return ImageFailure(
            code=ImageFailureCode.OUT_OF_MEMORY,
            message="ComfyUI 执行时显存不足",
            action="降低分辨率或批次，或改用更小的模型后手工重新发起",
            details=details,
        )
    return ImageFailure(
        code=ImageFailureCode.EXECUTION_FAILED,
        message=exception_message or "ComfyUI 节点执行失败",
        action="检查该节点的输入素材和模板配置，修正后手工重新发起",
        details=details,
    )
