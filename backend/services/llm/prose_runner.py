"""正文流式驱动器：单步、流式、逐 token 产出 SSE 帧。

**为什么不复用 `run_workflow`**（设计 §3.1）：它的 `_drive_with_keepalive` 是
"起一个 asyncio.Task、等它完成、其间发心跳"的形状，而流式是
`async for chunk in service.stream_text(...)`——无法通过一个"等 task 结束"的
函数逐 chunk 往外 yield。扩展它必然是一个从头写到尾、与现有路径几乎不重叠的分支，
而 2a 的全部工作流都跑在那个函数里。

本模块只从 workflow_runner 借 `sse_event` 一个纯格式化函数（"帧长什么样只有一个
模块知道"是那边立下的原则），不借用任何执行逻辑。

**没有 keepalive**：token 本身就是流量，流式不需要心跳。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, AsyncGenerator

from backend.llm.models import TokenUsage
from backend.services.llm.workflow_runner import sse_event
from backend.services.llm.generation_runtime import GenerationPlan, GenerationRuntime

logger = logging.getLogger(__name__)


async def stream_prose(
    *,
    workflow_name: str,
    step_key: str,
    prompt: str,
    service: Any | None,
    gen_kwargs: Mapping[str, Any],
    request_id: str,
    is_disconnected: Callable[[], Awaitable[bool]] | None = None,
    runtime: GenerationRuntime | None = None,
    generation_plan: GenerationPlan | None = None,
) -> AsyncGenerator[str, None]:
    """流式生成正文，产出 delta 帧与终局 done 帧。

    Args:
        workflow_name: 工作流名，仅用于日志。
        step_key: 步骤名，仅用于日志。
        prompt: 拼装完成的完整提示词。
        service: LLMService（或具备 stream_text / last_usage 的替身）。
        gen_kwargs: 透传给 LLMService 的生成参数覆盖。
        request_id: 便于串联日志的请求标识。
        is_disconnected: 查询客户端是否断开的回调；None 表示不检查。

    Yields:
        SSE 帧字符串：每个非空 chunk 一条 `delta`，末尾一条 `done`。
        客户端断开时**不发任何终局帧**——没人在听了。
    """
    started_at = time.perf_counter()
    chunks: list[str] = []

    logger.info(
        "[%s] request_id=%s step=%s status=running", workflow_name, request_id, step_key
    )

    attempt_offset = len(runtime.attempts) if runtime is not None else 0
    if runtime is not None:
        if generation_plan is None:
            raise ValueError("generation_plan is required when runtime is provided")
        stream = runtime.stream_text(generation_plan, prompt, **gen_kwargs)
    elif service is not None:
        stream = service.stream_text(prompt, **gen_kwargs)
    else:
        raise ValueError("service or runtime is required")
    try:
        async for chunk in stream:
            if is_disconnected is not None and await is_disconnected():
                # 用户关了页面：立刻关掉生成器，底层 HTTP 请求随之断开，不再计费。
                # 设计上这里的粒度是 chunk，而非 run_workflow 的 15 秒轮询。
                # 实测（设计 §10.3）：本部署下真正生效的是 ASGI 任务取消
                # （CancelledError 更早、绕过 except Exception），这条循环内
                # 检查在生产路径上从未被走到——留着是刻意的兜底（is_disconnected
                # 做成可选参数正是为此），不是应删除的死代码。
                try:
                    await stream.aclose()
                except Exception:
                    # 关闭本身失败（例如收尾时网络层出错）不能落到下面的
                    # except Exception 里补发一条 done 帧——断连路径的契约是
                    # 不发任何终局帧，没人在听了。这里只记日志，不再抛出。
                    logger.exception(
                        "[%s] request_id=%s step=%s aclose failed after disconnect",
                        workflow_name,
                        request_id,
                        step_key,
                    )
                logger.info(
                    "[%s] request_id=%s step=%s status=disconnected chunks=%d",
                    workflow_name,
                    request_id,
                    step_key,
                    len(chunks),
                )
                return
            if not chunk:
                continue
            chunks.append(chunk)
            yield sse_event("delta", {"text": chunk})
    except Exception as exc:
        runtime_attempts = runtime.attempts[attempt_offset:] if runtime is not None else ()
        usage_so_far = TokenUsage(
            input_tokens=sum(item.usage.input_tokens for item in runtime_attempts),
            output_tokens=sum(item.usage.output_tokens for item in runtime_attempts),
            total_tokens=sum(item.usage.total_tokens for item in runtime_attempts),
        )
        attempt_payload = [
            {
                "attempt_id": item.attempt_id,
                "provider_alias": item.provider_alias,
                "phase": item.phase,
                "state": item.state,
                "usage": item.usage.model_dump(),
            }
            for item in runtime_attempts
        ]
        logger.exception(
            "[%s] request_id=%s step=%s failed after %d chunks",
            workflow_name,
            request_id,
            step_key,
            len(chunks),
        )
        # 已发出的 delta 留在前端手上（设计 §7.3 已说明这半章无从对账）。
        yield sse_event("done", {
            "success": False,
            "error": str(exc),
            "usage_so_far": usage_so_far.model_dump(),
            "attempts": attempt_payload,
        })
        return

    text = "".join(chunks)
    # last_usage 只在生成器耗尽后才有效；中途取消或 provider 不报用量时为零值
    # （设计 §7.1）。这里不编造估算值。
    if runtime is not None:
        runtime_attempts = runtime.attempts[attempt_offset:]
        usage = TokenUsage(
            input_tokens=sum(item.usage.input_tokens for item in runtime_attempts),
            output_tokens=sum(item.usage.output_tokens for item in runtime_attempts),
            total_tokens=sum(item.usage.total_tokens for item in runtime_attempts),
        )
    else:
        usage = getattr(service, "last_usage", None) or TokenUsage()
    logger.info(
        "[%s] request_id=%s step=%s status=done elapsed_ms=%d chars=%d total_tokens=%s",
        workflow_name,
        request_id,
        step_key,
        int((time.perf_counter() - started_at) * 1000),
        len(text),
        usage.model_dump().get("total_tokens"),
    )
    yield sse_event("done", {"success": True, "text": text, "usage": usage.model_dump()})
