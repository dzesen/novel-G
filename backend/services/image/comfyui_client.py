"""只封装 V-1 实测端点的 ComfyUI HTTP 客户端。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import httpx

from backend.services.image.comfyui_failures import provider_unavailable_failure
from backend.services.image.contracts import ImageInputAsset, ImageProviderError


class ComfyUIProtocolError(ValueError):
    """ComfyUI 返回了与固定契约不兼容的响应。"""


@dataclass(frozen=True)
class QueueItem:
    number: int | float | None
    prompt_id: str


@dataclass(frozen=True)
class QueueSnapshot:
    running: tuple[QueueItem, ...]
    pending: tuple[QueueItem, ...]

    def running_item(self, prompt_id: str) -> QueueItem | None:
        return next((item for item in self.running if item.prompt_id == prompt_id), None)

    def pending_item(self, prompt_id: str) -> QueueItem | None:
        return next((item for item in self.pending if item.prompt_id == prompt_id), None)


def _queue_items(value: Any) -> tuple[QueueItem, ...]:
    if not isinstance(value, list):
        return ()
    items: list[QueueItem] = []
    for raw_item in value:
        if isinstance(raw_item, dict):
            prompt_id = str(raw_item.get("prompt_id") or "").strip()
            number = raw_item.get("number")
        elif isinstance(raw_item, list) and len(raw_item) >= 2:
            number = raw_item[0]
            prompt_id = str(raw_item[1] or "").strip()
        else:
            continue
        if prompt_id:
            items.append(QueueItem(number=number, prompt_id=prompt_id))
    return tuple(items)


class ComfyUIClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: int,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=base_url,
            timeout=float(timeout_seconds),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._client.request(method, path, **kwargs)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RequestError) as error:
            raise ImageProviderError(provider_unavailable_failure()) from error
        if response.status_code >= 500:
            raise ImageProviderError(provider_unavailable_failure())
        return response

    @staticmethod
    def _json_object(response: httpx.Response, *, endpoint: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as error:
            raise ComfyUIProtocolError(
                f"ComfyUI {endpoint} did not return a JSON object"
            ) from error
        if not isinstance(payload, dict):
            raise ComfyUIProtocolError(
                f"ComfyUI {endpoint} did not return a JSON object"
            )
        return payload

    @staticmethod
    def _json_string_list(
        response: httpx.Response,
        *,
        endpoint: str,
    ) -> tuple[str, ...]:
        try:
            payload = response.json()
        except ValueError as error:
            raise ComfyUIProtocolError(
                f"ComfyUI {endpoint} did not return a JSON string list"
            ) from error
        if not isinstance(payload, list) or any(
            not isinstance(item, str) for item in payload
        ):
            raise ComfyUIProtocolError(
                f"ComfyUI {endpoint} did not return a JSON string list"
            )
        return tuple(payload)

    async def get_queue(self) -> QueueSnapshot:
        response = await self._request("GET", "/queue")
        if response.status_code != 200:
            raise ComfyUIProtocolError("ComfyUI /queue request failed")
        payload = self._json_object(response, endpoint="/queue")
        return QueueSnapshot(
            running=_queue_items(
                payload.get("running", payload.get("queue_running"))
            ),
            pending=_queue_items(
                payload.get("pending", payload.get("queue_pending"))
            ),
        )

    async def get_history(self, prompt_id: str) -> dict[str, Any]:
        response = await self._request("GET", f"/history/{prompt_id}")
        if response.status_code != 200:
            raise ComfyUIProtocolError("ComfyUI history request failed")
        return self._json_object(response, endpoint="/history/{prompt_id}")

    async def get_system_stats(self) -> dict[str, Any]:
        response = await self._request("GET", "/system_stats")
        if response.status_code != 200:
            raise ComfyUIProtocolError("ComfyUI /system_stats request failed")
        return self._json_object(response, endpoint="/system_stats")

    async def get_object_info(self) -> dict[str, Any]:
        response = await self._request("GET", "/object_info")
        if response.status_code != 200:
            raise ComfyUIProtocolError("ComfyUI /object_info request failed")
        return self._json_object(response, endpoint="/object_info")

    async def get_model_names(
        self,
        model_kind: Literal["checkpoints", "loras"],
    ) -> tuple[str, ...]:
        if model_kind not in {"checkpoints", "loras"}:
            raise ValueError(f"Unsupported ComfyUI model kind: {model_kind}")
        endpoint = f"/models/{model_kind}"
        response = await self._request("GET", endpoint)
        if response.status_code != 200:
            raise ComfyUIProtocolError(
                f"ComfyUI {endpoint} request failed"
            )
        return self._json_string_list(
            response,
            endpoint=endpoint,
        )

    async def submit_prompt(
        self,
        *,
        workflow: dict[str, Any],
        client_id: str,
    ) -> tuple[int, dict[str, Any]]:
        response = await self._request(
            "POST",
            "/prompt",
            json={"prompt": workflow, "client_id": client_id},
        )
        return (
            response.status_code,
            self._json_object(response, endpoint="/prompt"),
        )

    async def upload_image(self, asset: ImageInputAsset) -> dict[str, Any]:
        response = await self._request(
            "POST",
            "/upload/image",
            data={"type": "input", "overwrite": "true"},
            files={
                "image": (
                    asset.filename,
                    asset.content,
                    asset.mime_type,
                )
            },
        )
        if response.status_code != 200:
            raise ComfyUIProtocolError("ComfyUI /upload/image request failed")
        return self._json_object(response, endpoint="/upload/image")

    async def get_view(
        self,
        *,
        filename: str,
        subfolder: str,
        storage_type: str,
    ) -> httpx.Response:
        return await self._request(
            "GET",
            "/view",
            params={
                "filename": filename,
                "subfolder": subfolder,
                "type": storage_type,
            },
        )

    async def cancel_job(self, prompt_id: str) -> httpx.Response:
        return await self._request(
            "POST",
            f"/api/jobs/{prompt_id}/cancel",
        )

    async def interrupt(self, prompt_id: str) -> httpx.Response:
        return await self._request(
            "POST",
            "/interrupt",
            json={"prompt_id": prompt_id},
        )

    async def delete_pending(self, prompt_id: str) -> httpx.Response:
        return await self._request(
            "POST",
            "/queue",
            json={"delete": [prompt_id]},
        )
