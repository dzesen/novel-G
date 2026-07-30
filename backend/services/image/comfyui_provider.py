"""ComfyUI 的异步 submit / poll / cancel Provider 适配器。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time
from typing import Any, Callable, Literal
import uuid

from backend.config.image_providers import ComfyUIImageProviderConfig
from backend.services.image.comfyui_client import (
    ComfyUIClient,
    ComfyUIProtocolError,
    QueueSnapshot,
)
from backend.services.image.comfyui_failures import (
    asset_expired_failure,
    cancelled_failure,
    classify_execution_error,
    classify_prompt_response,
    execution_failed_failure,
    job_lost_failure,
    queue_full_failure,
    sanitize_comfyui_text,
)
from backend.services.image.comfyui_template import (
    prepare_comfyui_template,
    render_comfyui_template,
)
from backend.services.image.contracts import (
    ImageArtifact,
    ImageCancelResult,
    ImageGenerationRequest,
    ImageInputAsset,
    ImageJobAudit,
    ImageJobHandle,
    ImageOutputBindingSnapshot,
    ImagePollResult,
    ImageProviderError,
)
from backend.services.novel.appearance_anchor import (
    RuntimeFingerprintSchema,
    RuntimePackageVersionSchema,
)


TerminalEvent = Literal[
    "execution_success",
    "execution_error",
    "execution_interrupted",
]


def _history_entry(
    payload: dict[str, Any],
    prompt_id: str,
) -> dict[str, Any] | None:
    wrapped = payload.get(prompt_id)
    if isinstance(wrapped, dict):
        return wrapped
    if "status" in payload or "outputs" in payload:
        return payload
    return None


def _terminal_event(
    history_entry: dict[str, Any] | None,
    prompt_id: str,
) -> tuple[TerminalEvent, dict[str, Any]] | None:
    if history_entry is None:
        return None
    status = history_entry.get("status")
    if not isinstance(status, dict) and isinstance(
        history_entry.get("messages"), list
    ):
        status = history_entry
    if not isinstance(status, dict):
        return None
    messages = status.get("messages")
    if not isinstance(messages, list):
        return None
    terminal_names = {
        "execution_success",
        "execution_error",
        "execution_interrupted",
    }
    for raw_message in reversed(messages):
        if (
            not isinstance(raw_message, list)
            or len(raw_message) != 2
            or raw_message[0] not in terminal_names
            or not isinstance(raw_message[1], dict)
        ):
            continue
        data = raw_message[1]
        event_prompt_id = str(data.get("prompt_id") or "")
        if event_prompt_id and event_prompt_id != prompt_id:
            continue
        return raw_message[0], data
    return None


def _comfyui_version(system_stats: dict[str, Any]) -> str:
    system = system_stats.get("system")
    if not isinstance(system, dict):
        return ""
    return str(system.get("comfyui_version") or "").strip()


_PRECISION_FLAG_PREFIXES = (
    "--force-fp",
    "--fp16-",
    "--fp32-",
    "--bf16-",
    "--fp8-",
    "--fp8_",
)
_RUNTIME_LIST_CHARACTER_BUDGET = 2_500


def _digest_runtime_values(
    values: list[str],
    *,
    label: str,
    max_items: int,
) -> list[str]:
    normalized = sorted(dict.fromkeys(values))
    if (
        len(normalized) <= max_items
        and sum(len(value) for value in normalized)
        <= _RUNTIME_LIST_CHARACTER_BUDGET
    ):
        return normalized
    digest = hashlib.sha256(
        json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return [f"{label}:sha256:{digest};count={len(normalized)}"]


def build_comfyui_runtime_fingerprint(
    system_stats: dict[str, Any],
    *,
    checkpoint_names: tuple[str, ...],
    lora_names: tuple[str, ...],
    workflow_graph_hash: str = "",
) -> RuntimeFingerprintSchema:
    """Keep only bounded deployment facts; never persist argv or local paths."""

    system = system_stats.get("system")
    system = system if isinstance(system, dict) else {}
    raw_packages = system.get("comfy_package_versions")
    raw_normalized_packages: list[tuple[str, str]] = []
    if isinstance(raw_packages, list):
        for raw_package in raw_packages:
            if not isinstance(raw_package, dict):
                continue
            name = sanitize_comfyui_text(
                raw_package.get("name"),
                limit=200,
            )
            if not name:
                continue
            version = sanitize_comfyui_text(
                raw_package.get("installed") or "unknown",
                limit=120,
            )
            raw_normalized_packages.append(
                (name, version or "unknown")
            )
    normalized_packages = sorted(dict.fromkeys(raw_normalized_packages))
    package_characters = sum(
        len(name) + len(version)
        for name, version in normalized_packages
    )
    if (
        len(normalized_packages) > 128
        or package_characters > _RUNTIME_LIST_CHARACTER_BUDGET
    ):
        package_digest = hashlib.sha256(
            json.dumps(
                normalized_packages,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        packages = [
            RuntimePackageVersionSchema(
                name=f"packages:sha256:{package_digest}",
                version=f"count:{len(normalized_packages)}",
            )
        ]
    else:
        packages = [
            RuntimePackageVersionSchema(name=name, version=version)
            for name, version in normalized_packages
        ]

    raw_devices = system_stats.get("devices")
    devices: list[str] = []
    if isinstance(raw_devices, list):
        for raw_device in raw_devices:
            if not isinstance(raw_device, dict):
                continue
            device_type = sanitize_comfyui_text(
                raw_device.get("type"),
                limit=40,
            )
            device_name = sanitize_comfyui_text(
                raw_device.get("name"),
                limit=250,
            )
            rendered = ": ".join(
                value for value in (device_type, device_name) if value
            )
            if rendered:
                devices.append(rendered[:300])

    raw_argv = system.get("argv")
    precision_flags: list[str] = []
    if isinstance(raw_argv, list):
        for value in raw_argv:
            flag = str(value or "").strip()
            if flag.startswith(_PRECISION_FLAG_PREFIXES):
                precision_flags.append(
                    sanitize_comfyui_text(flag, limit=80)
                )
    precision = ", ".join(sorted(set(precision_flags))) or "default"

    checkpoint_values = [
        sanitize_comfyui_text(name, limit=500)
        for name in checkpoint_names
        if str(name).strip()
    ]
    lora_values = [
        sanitize_comfyui_text(name, limit=500)
        for name in lora_names
        if str(name).strip()
    ]
    return RuntimeFingerprintSchema(
        comfyui_version=sanitize_comfyui_text(
            system.get("comfyui_version"),
            limit=120,
        ),
        pytorch_version=sanitize_comfyui_text(
            system.get("pytorch_version"),
            limit=120,
        ),
        package_versions=packages,
        devices=_digest_runtime_values(
            devices,
            label="devices",
            max_items=8,
        ),
        precision=precision[:200],
        checkpoint_names=_digest_runtime_values(
            checkpoint_values,
            label="checkpoints",
            max_items=32,
        ),
        lora_names=_digest_runtime_values(
            lora_values,
            label="loras",
            max_items=64,
        ),
        workflow_graph_hash=workflow_graph_hash,
    )


class ComfyUIProvider:
    def __init__(
        self,
        *,
        alias: str,
        config: ComfyUIImageProviderConfig,
        template_root: Path,
        client: ComfyUIClient | None = None,
        now_epoch: Callable[[], float] = time.time,
        client_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.alias = alias
        self.config = config
        self.template_root = Path(template_root)
        self.client = client or ComfyUIClient(
            base_url=config.base_url,
            timeout_seconds=config.timeout_seconds,
        )
        self._now_epoch = now_epoch
        self._client_id_factory = client_id_factory or (
            lambda: str(uuid.uuid4())
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    @staticmethod
    def _protocol_failure(action: str) -> ImageProviderError:
        return ImageProviderError(
            execution_failed_failure(
                message="ComfyUI 返回了与当前适配器不兼容的响应",
                action=action,
            )
        )

    async def submit(self, request: ImageGenerationRequest) -> ImageJobHandle:
        """只提交一次；排队护栏、上传和模板渲染都发生在 `/prompt` 之前。"""

        prepared = prepare_comfyui_template(
            self.config.workflow,
            request,
            template_root=self.template_root,
        )
        try:
            queue = await self.client.get_queue()
            active_count = len(queue.running) + len(queue.pending)
            if active_count >= self.config.max_concurrency:
                raise ImageProviderError(
                    queue_full_failure(
                        running=len(queue.running),
                        pending=len(queue.pending),
                        limit=self.config.max_concurrency,
                    )
                )
            system_stats = await self.client.get_system_stats()

            resolved_values: dict[str, str | int | float | bool | None] = {}
            input_asset_hashes: dict[str, str] = {}
            for slot, value in prepared.accepted_slots.items():
                if isinstance(value, ImageInputAsset):
                    input_asset_hashes[slot] = (
                        f"sha256:{hashlib.sha256(value.content).hexdigest()}"
                    )
                    upload = await self.client.upload_image(value)
                    uploaded_name = str(upload.get("name") or "").strip()
                    if not uploaded_name:
                        raise self._protocol_failure(
                            "检查 ComfyUI /upload/image 响应与版本"
                        )
                    resolved_values[slot] = uploaded_name
                else:
                    resolved_values[slot] = value

            rendered = render_comfyui_template(
                self.config.workflow,
                prepared,
                resolved_values,
            )
            status_code, payload = await self.client.submit_prompt(
                workflow=rendered.workflow,
                client_id=self._client_id_factory(),
            )
        except ComfyUIProtocolError as error:
            raise self._protocol_failure(
                "确认 ComfyUI 版本与适配器契约一致后重新测试连接"
            ) from error

        failure = classify_prompt_response(
            status_code=status_code,
            payload=payload,
        )
        if failure is not None:
            raise ImageProviderError(failure)
        prompt_id = str(payload.get("prompt_id") or "").strip()
        if not prompt_id:
            raise self._protocol_failure(
                "确认 /prompt 响应包含 prompt_id 后重新发起"
            )
        seed_value = request.slot_values.get("seed")
        seed = (
            int(seed_value)
            if isinstance(seed_value, int) and not isinstance(seed_value, bool)
            else None
        )
        return ImageJobHandle(
            provider_alias=self.alias,
            prompt_id=prompt_id,
            submitted_at_epoch=self._now_epoch(),
            timeout_seconds=self.config.timeout_seconds,
            ignored_slots=prepared.ignored_slots,
            outputs=tuple(
                ImageOutputBindingSnapshot(
                    node_id=output.node_id,
                    field=output.field,
                )
                for output in self.config.workflow.outputs
            ),
            audit=ImageJobAudit(
                template_revision=prepared.loaded.revision,
                submitted_graph_hash=rendered.graph_hash,
                comfyui_version=_comfyui_version(system_stats),
                seed=seed,
                checkpoint_names=prepared.loaded.checkpoint_names,
                lora_names=prepared.loaded.lora_names,
                input_asset_hashes=input_asset_hashes,
                runtime_fingerprint=build_comfyui_runtime_fingerprint(
                    system_stats,
                    checkpoint_names=prepared.loaded.checkpoint_names,
                    lora_names=prepared.loaded.lora_names,
                    workflow_graph_hash=prepared.loaded.effective_graph_hash,
                ),
            ),
        )

    def _validate_handle(self, handle: ImageJobHandle) -> None:
        if handle.provider_alias != self.alias:
            raise ValueError("Image job handle belongs to another Provider")

    async def _artifacts_from_history(
        self,
        handle: ImageJobHandle,
        history_entry: dict[str, Any],
    ) -> ImagePollResult:
        outputs = history_entry.get("outputs")
        outputs = outputs if isinstance(outputs, dict) else {}
        artifacts: list[ImageArtifact] = []
        for output_binding in handle.outputs:
            raw_output = outputs.get(output_binding.node_id)
            if not isinstance(raw_output, dict):
                continue
            descriptors = raw_output.get(output_binding.field)
            if not isinstance(descriptors, list):
                continue
            for descriptor in descriptors:
                if not isinstance(descriptor, dict):
                    continue
                filename = str(descriptor.get("filename") or "")
                subfolder = str(descriptor.get("subfolder") or "")
                storage_type = str(descriptor.get("type") or "")
                if not filename or not storage_type:
                    continue
                response = await self.client.get_view(
                    filename=filename,
                    subfolder=subfolder,
                    storage_type=storage_type,
                )
                if response.status_code == 404:
                    return ImagePollResult(
                        status="failed",
                        ignored_slots=handle.ignored_slots,
                        failure=asset_expired_failure(),
                    )
                if response.status_code != 200:
                    return ImagePollResult(
                        status="failed",
                        ignored_slots=handle.ignored_slots,
                        failure=execution_failed_failure(
                            message="ComfyUI /view 无法取回产物",
                            action="检查 history 产物描述符和 ComfyUI 输出目录后手工处理",
                        ),
                    )
                content = response.content
                artifacts.append(
                    ImageArtifact(
                        content=content,
                        content_hash=hashlib.sha256(content).hexdigest(),
                        mime_type=(
                            response.headers.get(
                                "content-type",
                                "application/octet-stream",
                            ).split(";", 1)[0]
                        ),
                        filename=filename,
                        subfolder=subfolder,
                        provider_storage_type=storage_type,
                    )
                )
        if not artifacts:
            return ImagePollResult(
                status="failed",
                ignored_slots=handle.ignored_slots,
                failure=execution_failed_failure(
                    message="ComfyUI 报告成功，但配置的产物节点没有返回图片",
                    action="检查 workflow.outputs 的节点 ID 与字段名后重新发起",
                ),
            )
        return ImagePollResult(
            status="succeeded",
            ignored_slots=handle.ignored_slots,
            artifacts=tuple(artifacts),
        )

    async def poll(self, handle: ImageJobHandle) -> ImagePollResult:
        """只查询 history/queue；任何分支都不会调用 `/prompt`。"""

        self._validate_handle(handle)
        try:
            history_payload = await self.client.get_history(handle.prompt_id)
            history_entry = _history_entry(history_payload, handle.prompt_id)
            terminal = _terminal_event(history_entry, handle.prompt_id)
            if terminal is not None:
                event_name, event_data = terminal
                if event_name == "execution_interrupted":
                    return ImagePollResult(
                        status="cancelled",
                        ignored_slots=handle.ignored_slots,
                        failure=cancelled_failure(),
                    )
                if event_name == "execution_error":
                    return ImagePollResult(
                        status="failed",
                        ignored_slots=handle.ignored_slots,
                        failure=classify_execution_error(event_data),
                    )
                return await self._artifacts_from_history(
                    handle,
                    history_entry or {},
                )

            queue = await self.client.get_queue()
        except ImageProviderError as error:
            return ImagePollResult(
                status="failed",
                ignored_slots=handle.ignored_slots,
                failure=error.failure,
            )
        except ComfyUIProtocolError:
            return ImagePollResult(
                status="failed",
                ignored_slots=handle.ignored_slots,
                failure=execution_failed_failure(
                    message="ComfyUI 返回了无法识别的轮询响应",
                    action="确认 ComfyUI 版本与适配器契约一致",
                ),
            )

        if queue.running_item(handle.prompt_id) is not None:
            return ImagePollResult(
                status="running",
                ignored_slots=handle.ignored_slots,
            )
        pending_item = queue.pending_item(handle.prompt_id)
        if pending_item is not None:
            position = next(
                index
                for index, item in enumerate(queue.pending, start=1)
                if item.prompt_id == handle.prompt_id
            )
            return ImagePollResult(
                status="queued",
                queue_position=position,
                ignored_slots=handle.ignored_slots,
            )
        if (
            history_entry is None
            and self._now_epoch() - handle.submitted_at_epoch
            >= handle.timeout_seconds
        ):
            return ImagePollResult(
                status="failed",
                ignored_slots=handle.ignored_slots,
                failure=job_lost_failure(handle.prompt_id),
            )
        return ImagePollResult(
            status="pending",
            ignored_slots=handle.ignored_slots,
        )

    @staticmethod
    def _cancel_terminal_result(
        history_entry: dict[str, Any] | None,
        prompt_id: str,
    ) -> ImageCancelResult | None:
        terminal = _terminal_event(history_entry, prompt_id)
        if terminal is None:
            return None
        if terminal[0] == "execution_interrupted":
            return ImageCancelResult(
                status="cancelled",
                failure=cancelled_failure(),
            )
        return ImageCancelResult(status="already_finished")

    async def _cancel_confirmation(
        self,
        handle: ImageJobHandle,
    ) -> tuple[
        ImageCancelResult | None,
        QueueSnapshot,
        dict[str, Any] | None,
    ]:
        history_payload = await self.client.get_history(handle.prompt_id)
        history_entry = _history_entry(history_payload, handle.prompt_id)
        terminal_result = self._cancel_terminal_result(
            history_entry,
            handle.prompt_id,
        )
        queue = await self.client.get_queue()
        return terminal_result, queue, history_entry

    async def cancel(self, handle: ImageJobHandle) -> ImageCancelResult:
        """优先按 ID 取消，确认后才按 queue 状态选择旧接口。"""

        self._validate_handle(handle)
        try:
            modern_response = await self.client.cancel_job(handle.prompt_id)
            terminal, queue, history_entry = await self._cancel_confirmation(
                handle
            )
            if terminal is not None:
                return terminal

            running = queue.running_item(handle.prompt_id)
            pending = queue.pending_item(handle.prompt_id)
            modern_accepted = 200 <= modern_response.status_code < 300
            if running is None and pending is None:
                if modern_accepted:
                    return ImageCancelResult(
                        status="cancelled",
                        failure=cancelled_failure(),
                    )
                return ImageCancelResult(
                    status="failed",
                    failure=job_lost_failure(handle.prompt_id),
                )

            if running is not None:
                await self.client.interrupt(handle.prompt_id)
            elif pending is not None:
                await self.client.delete_pending(handle.prompt_id)

            terminal, queue, history_entry = await self._cancel_confirmation(
                handle
            )
            if terminal is not None:
                return terminal
            if (
                queue.running_item(handle.prompt_id) is None
                and queue.pending_item(handle.prompt_id) is None
            ):
                return ImageCancelResult(
                    status="cancelled",
                    failure=cancelled_failure(),
                )
            return ImageCancelResult(
                status="failed",
                failure=execution_failed_failure(
                    message="ComfyUI 未确认任务取消",
                    action="在 ComfyUI 队列中确认目标任务后手工取消，避免中断其他任务",
                    details={"prompt_id": handle.prompt_id},
                ),
            )
        except ImageProviderError as error:
            return ImageCancelResult(status="failed", failure=error.failure)
        except ComfyUIProtocolError:
            return ImageCancelResult(
                status="failed",
                failure=execution_failed_failure(
                    message="ComfyUI 返回了无法识别的取消响应",
                    action="在 ComfyUI 队列中确认目标任务后手工取消",
                ),
            )
