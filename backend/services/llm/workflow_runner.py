"""SSE 工作流执行器：按步骤表驱动多步 LLM 管道。

原先每个步骤都是一段约 50 行的复制粘贴（查缓存 → yield running → 解析 provider/timeout/schema
→ 打日志 → try → 拼 prompt → 调用 → yield done → except → yield error → yield partial → return）。
本模块把这段流程抽成唯一实现，新工作流只需声明一张步骤表。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator

from pydantic import BaseModel

from backend.llm.models import TokenUsage
from backend.services.llm.agent_orchestrator import apply_agent_profile
from backend.services.llm.generation_runtime import (
    GenerationPlan,
    GenerationRuntime,
    PromptPlan,
    StructuredOutputMode,
    WorkflowStepTarget,
)

logger = logging.getLogger(__name__)

# 心跳间隔。设定生成单步就要 60–120 秒静默，正文生成更长；
# 代理与负载均衡通常在 30–60 秒空闲后掐连接，故取远小于此的值。
KEEPALIVE_SECONDS = 15.0


@dataclass(frozen=True)
class StepContext:
    """步骤拼 prompt 时可见的上下文。

    Args:
        results: 已完成步骤的结果，按步骤 key 索引（含缓存命中的步骤）。
        params: 请求级参数，如 user_idea / number_of_chapters。
    """

    results: Mapping[str, BaseModel]
    params: Mapping[str, Any]


@dataclass(frozen=True)
class WorkflowStep:
    """一个工作流步骤的完整声明。

    key 与 config_key 是**两套词汇**：key 用于 SSE 事件与前端缓存，
    config_key 用于 llm.workflows 配置段与 prompt 模板名。二者历史上靠路由里的
    硬编码字符串对应，而四步中三步恰好同名，使这个区别极易被忽略。
    本表是二者唯一的映射来源；config_key 省略即表示与 key 同名。

    Args:
        key: SSE 事件与缓存使用的步骤名。
        schema: 该步骤产出的 Pydantic 模型。
        prompt_args: 由上下文算出 prompt 模板格式化参数的函数。
        config_key: 配置与 prompt 模板使用的步骤名；None 表示同 key。
    """

    key: str
    schema: type[BaseModel]
    prompt_args: Callable[[StepContext], dict[str, Any]]
    config_key: str | None = None
    agent_id: str | None = None
    max_structured_raw_output_bytes: int | None = None

    @property
    def resolved_config_key(self) -> str:
        """配置与 prompt 侧实际使用的步骤名。"""
        return self.config_key or self.key


@dataclass(frozen=True)
class WorkflowDeps:
    """执行器对外部能力的依赖。

    以注入方式提供，使执行器可脱离配置文件、HTTP 与真实 LLM 测试。
    """

    runtime: GenerationRuntime
    structured_plans: Mapping[str, GenerationPlan] = field(
        default_factory=dict
    )


class ClientDisconnected(Exception):
    """客户端已断开，工作流应立即中止。"""


def sse_event(event: str, data: dict) -> str:
    """格式化一条 SSE 事件。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def sse_comment(text: str) -> str:
    """格式化一条 SSE 注释帧。

    EventSource 会忽略注释，但它足以让代理认为连接仍活跃，是标准的 keepalive 做法。
    """
    return f": {text}\n\n"


def parse_sse_event(frame: str) -> "tuple[str, dict] | None":
    """把一帧 SSE 解析回 (事件名, 数据)——`sse_event` 的逆函数。

    与 sse_event 放在同一模块，是为了让"帧长什么样"只有一个模块知道。路由需要
    在 run_workflow 的输出上做后处理（如设计 §5.3 的 id 剔除要写回 done 帧）；
    若每个路由自己 split 字符串，帧格式一改就要满仓库找。执行器**没有**因此
    学到任何业务概念，它只是多了一个自己已有函数的逆。

    Args:
        frame: 一帧 SSE 文本。

    Returns:
        (事件名, 数据字典)；非事件帧（keepalive 注释、格式不符、data 非 JSON）返回 None。
    """
    if not frame.startswith("event: "):
        return None
    lines = frame.split("\n")
    event = lines[0][len("event: "):]
    for line in lines[1:]:
        if line.startswith("data: "):
            try:
                return event, json.loads(line[len("data: "):])
            except json.JSONDecodeError:
                return None
    return None


def _add_usage(left: TokenUsage, right: TokenUsage) -> TokenUsage:
    """累加两份用量。"""
    return TokenUsage(
        input_tokens=left.input_tokens + right.input_tokens,
        output_tokens=left.output_tokens + right.output_tokens,
        total_tokens=left.total_tokens + right.total_tokens,
    )


async def _drive_with_keepalive(
    task: asyncio.Task,
    is_disconnected: Callable[[], Awaitable[bool]] | None,
    keepalive_seconds: float,
) -> AsyncGenerator[str, None]:
    """等待 task 完成，其间周期性 yield 心跳帧并检查客户端是否断开。

    Args:
        task: 正在执行的 LLM 调用任务。
        is_disconnected: 查询客户端是否断开的回调；None 表示不检查。
        keepalive_seconds: 心跳间隔秒数。

    Yields:
        SSE 心跳注释帧。

    Raises:
        ClientDisconnected: 客户端断开时抛出，task 已被取消。
    """
    while True:
        done, _pending = await asyncio.wait({task}, timeout=keepalive_seconds)
        if task in done:
            return

        if is_disconnected is not None and await is_disconnected():
            # 用户关了页面，正在烧的 LLM 调用必须立刻掐掉，否则断开也照样计费。
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            raise ClientDisconnected()

        yield sse_comment("keepalive")


async def run_workflow(
    *,
    workflow_name: str,
    steps: Sequence[WorkflowStep],
    prompts: Mapping[str, str],
    params: Mapping[str, Any],
    gen_kwargs: Mapping[str, Any],
    cached: Mapping[str, BaseModel],
    deps: WorkflowDeps,
    request_id: str,
    is_disconnected: Callable[[], Awaitable[bool]] | None = None,
    keepalive_seconds: float | None = None,
    log_partial_on_disconnect: bool = False,
) -> AsyncGenerator[str, None]:
    """按步骤表驱动一条多步 LLM 管道，产出 SSE 帧。

    Args:
        workflow_name: 工作流名，用于解析配置与日志。
        steps: 步骤表，按顺序执行。
        prompts: 该工作流的 prompt 模板段。
        params: 请求级参数，供 prompt_args 取用。
        gen_kwargs: 透传给 LLMService 的生成参数覆盖。
        cached: 前端回传的已完成步骤（应为从第一步起的连续前缀）。
        deps: 外部能力依赖。
        request_id: 便于串联日志的请求标识。
        is_disconnected: 查询客户端是否断开的回调。
        keepalive_seconds: 心跳间隔秒数；None 表示取模块级 KEEPALIVE_SECONDS。
        log_partial_on_disconnect: 断开时是否把已完成步骤的完整结果打进日志。

    Yields:
        SSE 帧字符串（step / done 事件与 keepalive 注释）。
    """
    # 在此解析而非用默认参数：默认参数在 def 时绑定，测试就无法调快心跳。
    interval = KEEPALIVE_SECONDS if keepalive_seconds is None else keepalive_seconds
    workflow_start = time.perf_counter()
    results: dict[str, BaseModel] = {}
    total_usage = TokenUsage()

    def _log(step: str, status: str, **details: object) -> None:
        logger.info(
            "[%s] request_id=%s step=%s status=%s details=%s",
            workflow_name,
            request_id,
            step,
            status,
            details or {},
        )

    def _partial() -> dict[str, Any]:
        return {key: model.model_dump() for key, model in results.items()}

    _log(
        "workflow",
        "started",
        cached_steps=list(cached.keys()),
        overrides=sorted(gen_kwargs.keys()),
    )

    for step in steps:
        if (cached_value := cached.get(step.key)) is not None:
            results[step.key] = cached_value
            _log(step.key, "cached")
            yield sse_event(
                "step",
                {"step": step.key, "status": "done", "cached": True, "data": cached_value.model_dump()},
            )
            continue

        step_started_at = time.perf_counter()
        config_key = step.resolved_config_key
        runtime_attempt_offset = len(deps.runtime.attempts)
        try:
            target = WorkflowStepTarget(workflow_name, config_key)
            generation_plan = deps.structured_plans.get(step.key)
            if generation_plan is None:
                generation_plan = deps.runtime.plan_structured(target)
            elif (
                not isinstance(generation_plan, GenerationPlan)
                or generation_plan.target != target
            ):
                raise ValueError(
                    "frozen structured plan does not match the workflow step"
                )
            provider = generation_plan.provider_alias
            timeout_seconds = generation_plan.timeout_seconds
            use_schema = generation_plan.mode == StructuredOutputMode.SCHEMA_ENFORCED
        except Exception as exc:
            yield sse_event(
                "step",
                {"step": step.key, "status": "error", "error": str(exc)},
            )
            yield sse_event(
                "done",
                {
                    "success": False,
                    "failed_step": step.key,
                    "partial_result": _partial(),
                    "usage": total_usage.model_dump(),
                },
            )
            return
        yield sse_event(
            "step",
            {
                "step": step.key,
                "status": "running",
                "provider": provider,
                "structured_output": generation_plan.mode.value,
                "reviewer": generation_plan.reviewer_alias,
                "agent": step.agent_id,
            },
        )
        _log(
            step.key,
            "running",
            provider=provider or "unresolved",
            timeout_seconds=timeout_seconds,
            json_schema=use_schema,
        )

        try:
            prompt_base = prompts[f"{config_key}_prompt_base"].format(
                **step.prompt_args(StepContext(results=results, params=params))
            )
            native_prompt = prompt_base + "\n" + prompts[f"{config_key}_prompt_with_schema_suffix"]
            prompt_json = prompt_base + "\n" + prompts[f"{config_key}_prompt_without_schema_suffix"]
            if step.agent_id:
                native_prompt = apply_agent_profile(step.agent_id, native_prompt)
                prompt_json = apply_agent_profile(step.agent_id, prompt_json)

            structured_kwargs = dict(gen_kwargs)
            if step.max_structured_raw_output_bytes is not None:
                structured_kwargs["max_structured_raw_output_bytes"] = (
                    step.max_structured_raw_output_bytes
                )
            coro = deps.runtime.generate_structured(
                generation_plan,
                step.schema,
                PromptPlan(
                    native_schema_prompt=native_prompt,
                    prompt_json_prompt=prompt_json,
                ),
                **structured_kwargs,
            )

            task = asyncio.ensure_future(coro)
            async for frame in _drive_with_keepalive(task, is_disconnected, interval):
                yield frame
            generated = await task
            produced = generated.value
            step_usage = generated.usage

        except ClientDisconnected:
            _log(
                step.key,
                "disconnected",
                completed_steps=list(results.keys()),
                partial_result=_partial() if log_partial_on_disconnect else "<未记录>",
            )
            # 没人在听了，不再发任何事件；前端重连时会带 cached_steps 续跑。
            return

        except Exception as exc:
            logger.exception(
                "[%s] request_id=%s step=%s failed provider=%s",
                workflow_name,
                request_id,
                step.key,
                provider or "unresolved",
            )
            failed_attempts = deps.runtime.attempts[runtime_attempt_offset:]
            failed_usage = TokenUsage(
                input_tokens=sum(item.usage.input_tokens for item in failed_attempts),
                output_tokens=sum(item.usage.output_tokens for item in failed_attempts),
                total_tokens=sum(item.usage.total_tokens for item in failed_attempts),
            )
            total_usage = _add_usage(total_usage, failed_usage)
            attempt_payload = [
                {
                    "attempt_id": attempt.attempt_id,
                    "provider": attempt.provider_alias,
                    "phase": attempt.phase,
                    "usage": attempt.usage.model_dump(),
                }
                for attempt in failed_attempts
            ]
            yield sse_event(
                "step",
                {
                    "step": step.key,
                    "status": "error",
                    "error": str(exc),
                    "usage": failed_usage.model_dump(),
                    "attempts": attempt_payload,
                },
            )
            yield sse_event(
                "done",
                {
                    "success": False,
                    "failed_step": step.key,
                    "partial_result": _partial(),
                    "usage": total_usage.model_dump(),
                    "attempts": attempt_payload,
                },
            )
            return

        results[step.key] = produced
        total_usage = _add_usage(total_usage, step_usage)
        _log(
            step.key,
            "done",
            provider=provider or "unresolved",
            elapsed_ms=int((time.perf_counter() - step_started_at) * 1000),
            total_tokens=step_usage.total_tokens,
        )
        yield sse_event(
            "step",
            {
                "step": step.key,
                "status": "done",
                "data": produced.model_dump(),
                "usage": step_usage.model_dump(),
            },
        )

    _log(
        "workflow",
        "done",
        total_elapsed_ms=int((time.perf_counter() - workflow_start) * 1000),
        total_tokens=total_usage.total_tokens,
    )
    yield sse_event(
        "done",
        {"success": True, "result": _partial(), "usage": total_usage.model_dump()},
    )

class WorkflowFailed(Exception):
    """无头运行时工作流以 done{success:false} 结束。"""

    def __init__(
        self,
        message: str,
        *,
        usage: dict[str, Any] | None = None,
        attempts: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.usage = usage or {}
        self.attempts = attempts or []


async def run_workflow_to_result(step_key: str, frames: AsyncGenerator[str, None]) -> "tuple[dict, int]":
    """无头消费 run_workflow 的 SSE 帧，返回 (最终 result[step_key], total_tokens)。

    浏览器路径靠 SSE 逐帧渲染；批量引擎不需要帧，只要最终结构化结果。帧格式的知识
    仍只留在本模块（parse_sse_event 的逆用），执行器未学到任何业务概念。
    """
    async for frame in frames:
        parsed = parse_sse_event(frame)
        if parsed is None:
            continue
        event, data = parsed
        if event == "done":
            if not data.get("success"):
                raise WorkflowFailed(
                    data.get("error") or f"workflow failed at {data.get('failed_step')}",
                    usage=data.get("usage"),
                    attempts=data.get("attempts"),
                )
            result = data.get("result") or {}
            usage = data.get("usage") or {}
            return result.get(step_key, {}), int(usage.get("total_tokens") or 0)
    raise WorkflowFailed("workflow ended without a done event")
