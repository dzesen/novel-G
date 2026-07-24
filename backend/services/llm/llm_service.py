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
from backend.llm.exceptions import LLMStructuredValidationError
from backend.llm.models import LLMRequest, LLMResponse, TokenUsage


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

    三个方法均记录 token 用量，调用后可经 last_usage / total_usage 读取。
    正文生成是烧钱大户，用量若不透传，用户跑完一本书也不知道花了多少。

    **并发约束**：用量记在实例上，因此单个实例不可并发复用——两个并发调用会
    互相覆盖 last_usage。当前每个工作流步骤各建一个实例，天然满足该约束。
    """

    def __init__(
        self,
        provider_name: str | None = None,
        timeout_seconds: int | None = None,
        max_retries: int | None = None,
    ) -> None:
        self._client = create_llm_client(
            provider_name,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
        )
        self._provider_name = getattr(self._client, "provider_name", provider_name or "default")
        client_config = getattr(self._client, "config", None)
        self._max_concurrency = int(getattr(client_config, "max_concurrency", 0))
        self._last_usage = TokenUsage()
        self._total_usage = TokenUsage()

    @property
    def last_usage(self) -> TokenUsage:
        """最近一次调用的 token 用量。

        provider 不报用量时为零值——**零用量与"真的没花钱"不可区分**，这是已知取舍。
        客户端层的 log_llm_response 仍记录真实用量，必要时可交叉核对。
        """
        return self._last_usage

    @property
    def total_usage(self) -> TokenUsage:
        """本实例全部调用的累计 token 用量。"""
        return self._total_usage

    def _record_usage(self, usage: TokenUsage) -> None:
        """记录单次用量并累加到总量。"""
        self._last_usage = usage
        self._total_usage = TokenUsage(
            input_tokens=self._total_usage.input_tokens + usage.input_tokens,
            output_tokens=self._total_usage.output_tokens + usage.output_tokens,
            total_tokens=self._total_usage.total_tokens + usage.total_tokens,
        )

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
        """普通文本生成，返回纯文本内容；用量见 last_usage。"""
        request = self._make_request(prompt, system_prompt, **kwargs)
        async with _provider_request_slot(self._provider_name, self._max_concurrency):
            response: LLMResponse = await self._client.text_generate(request)
        self._record_usage(response.usage)
        return response.content

    async def generate_structured(
        self,
        prompt: str,
        schema: type[BaseModel],
        system_prompt: str = "",
        **kwargs: Any,
    ) -> BaseModel:
        """结构化生成，返回解析后的 Pydantic 模型实例；用量见 last_usage。"""
        request = self._make_request(prompt, system_prompt, **kwargs)
        async with _provider_request_slot(self._provider_name, self._max_concurrency):
            response: LLMResponse = await self._client.schema_generate(request, schema)
        self._record_usage(response.usage)
        try:
            return schema.model_validate(json.loads(response.content))
        except Exception as exc:
            raise LLMStructuredValidationError(
                f"Provider 返回内容未通过 {schema.__name__} 校验: {exc}",
                raw_output=response.content,
                provider=self._provider_name,
                model=response.model,
            ) from exc

    async def generate_json_object(
        self,
        prompt: str,
        system_prompt: str = "",
        **kwargs: Any,
    ) -> str:
        """请求 Provider 的 JSON Object 模式并返回原始 JSON 文本。"""
        request = self._make_request(
            prompt,
            system_prompt,
            metadata={"structured_output": "json_object"},
            **kwargs,
        )
        async with _provider_request_slot(self._provider_name, self._max_concurrency):
            response: LLMResponse = await self._client.text_generate(request)
        self._record_usage(response.usage)
        return response.content

    async def stream_text(
        self,
        prompt: str,
        system_prompt: str = "",
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        """流式文本生成，逐块 yield 文本片段。

        用量只在流末尾到达，故 last_usage 需在生成器耗尽后读取；
        中途 break 或 provider 不报用量时，它保持零值。
        """
        request = self._make_request(prompt, system_prompt, **kwargs)
        async with _provider_request_slot(self._provider_name, self._max_concurrency):
            async for chunk in self._client.stream_text(request, usage_sink=self._record_usage):
                yield chunk
