"""从同一份配置快照解析工作流步骤的 Provider 与超时。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.config import get_config_value
from backend.llm.config import LLMConfig
from backend.services.llm.llm_service import LLMService


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _workflow_and_step(
    raw: dict[str, Any], workflow_name: str, step_name: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    workflow = _mapping(_mapping(raw.get("workflows")).get(workflow_name))
    step = _mapping(_mapping(workflow.get("steps")).get(step_name))
    return workflow, step


def _coerce_positive_timeout(value: object) -> int | None:
    """空值和非法值继承 Provider 超时；保持既有整数秒转换语义。"""
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        timeout_seconds = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return timeout_seconds if timeout_seconds > 0 else None


@dataclass(frozen=True)
class _StepSettings:
    provider: str | None
    timeout_seconds: int | None


def _resolve_step(workflow_name: str, step_name: str) -> _StepSettings:
    # get_config_value returns a detached snapshot. Do not reread it for each
    # fallback or the timeout: a settings save could otherwise mix revisions.
    raw = _mapping(get_config_value("llm", {}))
    llm_config = LLMConfig.model_validate(raw)
    workflow, step = _workflow_and_step(raw, workflow_name, step_name)
    candidates = (
        step.get("provider"),
        workflow.get("default_provider"),
        llm_config.default_provider,
    )
    provider = next(
        (
            name for name in candidates
            if isinstance(name, str)
            and name in llm_config.providers
            and llm_config.providers[name].enabled
        ),
        None,
    )
    return _StepSettings(provider, _coerce_positive_timeout(step.get("timeout_seconds")))


def resolve_provider_for_step(workflow_name: str, step_name: str) -> str | None:
    """按步骤、工作流、全局默认的顺序选择第一个已启用的 Provider。"""
    return _resolve_step(workflow_name, step_name).provider


def resolve_timeout_for_step(workflow_name: str, step_name: str) -> int | None:
    """解析步骤超时；不要求该步骤已配置可用 Provider。"""
    step = _get_workflow_step_config(workflow_name, step_name)
    return _coerce_positive_timeout(step.get("timeout_seconds"))


def _get_workflow_step_config(workflow_name: str, step_name: str) -> dict[str, Any]:
    """保留注册表守卫使用的步骤配置读取入口。"""
    raw = _mapping(get_config_value("llm", {}))
    _, step = _workflow_and_step(raw, workflow_name, step_name)
    return step


def get_llm_service_for_step(workflow_name: str, step_name: str) -> LLMService:
    """从一次解析结果创建服务，不发起模型请求。"""
    settings = _resolve_step(workflow_name, step_name)
    if not settings.provider:
        raise ValueError(
            f"无法为工作流 '{workflow_name}' 的步骤 '{step_name}' 找到可用的 Provider，"
            "请检查配置中是否有已启用的 Provider。"
        )
    return LLMService(
        provider_name=settings.provider,
        timeout_seconds=settings.timeout_seconds,
    )
