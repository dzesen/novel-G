"""LLM 高层服务封装，提供简洁的文本/结构化/流式生成接口。"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from threading import Lock
from typing import Any, AsyncGenerator
from weakref import WeakKeyDictionary

from pydantic import BaseModel

from backend.llm.factory import create_llm_client
from backend.llm.models import LLMRequest, LLMResponse


_limiter_lock = Lock()
_loop_limiters: WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    dict[str, tuple[int, asyncio.Semaphore]],
] = WeakKeyDictionary()


def _get_provider_semaphore(provider_name: str, limit: int) -> asyncio.Semaphore:
    """获取当前事件循环内按 Provider 共享的并发信号量。"""
    loop = asyncio.get_running_loop()
    with _limiter_lock:
        providers = _loop_limiters.setdefault(loop, {})
        configured = providers.get(provider_name)
        if configured is None or configured[0] != limit:
            configured = (limit, asyncio.Semaphore(limit))
            providers[provider_name] = configured
        return configured[1]


@asynccontextmanager
async def _provider_request_slot(provider_name: str, limit: int):
    """限制同一 Provider 的并发请求数；0 表示不限制。"""
    if limit <= 0:
        yield
        return

    semaphore = _get_provider_semaphore(provider_name, limit)
    async with semaphore:
        yield


class LLMService:
    """面向业务层的 LLM 能力封装。

    将底层客户端的请求构造、日志记录等细节隐藏，
    对外暴露 generate_text / generate_structured / stream_text 三个简洁方法。
    """

    def __init__(self, provider_name: str | None = None, timeout_seconds: int | None = None) -> None:
        self._client = create_llm_client(provider_name, timeout_seconds=timeout_seconds)
        self._provider_name = getattr(self._client, "provider_name", provider_name or "default")
        client_config = getattr(self._client, "config", None)
        self._max_concurrency = int(getattr(client_config, "max_concurrency", 0))

    def _make_request(
        self,
        prompt: str,
        system_prompt: str = "",
        **kwargs: Any,
    ) -> LLMRequest:
        """将简单参数组装为 LLMRequest。"""
        messages = [{"role": "user", "content": prompt}]
        return LLMRequest(
            messages=messages,
            system_prompt=system_prompt,
            **kwargs,
        )

    async def generate_text(
        self,
        prompt: str,
        system_prompt: str = "",
        **kwargs: Any,
    ) -> str:
        """普通文本生成，返回纯文本内容。"""
        request = self._make_request(prompt, system_prompt, **kwargs)
        async with _provider_request_slot(self._provider_name, self._max_concurrency):
            response: LLMResponse = await self._client.text_generate(request)
        return response.content

    async def generate_structured(
        self,
        prompt: str,
        schema: type[BaseModel],
        system_prompt: str = "",
        **kwargs: Any,
    ) -> BaseModel:
        """结构化生成，返回解析后的 Pydantic 模型实例。"""
        request = self._make_request(prompt, system_prompt, **kwargs)
        async with _provider_request_slot(self._provider_name, self._max_concurrency):
            response: LLMResponse = await self._client.schema_generate(request, schema)
        return schema.model_validate(json.loads(response.content))

    async def stream_text(
        self,
        prompt: str,
        system_prompt: str = "",
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        """流式文本生成，逐块 yield 文本片段。"""
        request = self._make_request(prompt, system_prompt, **kwargs)
        async with _provider_request_slot(self._provider_name, self._max_concurrency):
            async for chunk in self._client.stream_text(request):
                yield chunk
