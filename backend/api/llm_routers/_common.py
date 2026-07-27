"""LLM 路由共用的请求模型、生成参数和安全文本读取工具。

结构化输出协议由 GenerationRuntime 统一规划；路由层不再维护 Provider 能力分支。
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

GEN_PARAM_KEYS: tuple[str, ...] = (
    "temperature",
    "top_p",
    "max_tokens",
    "presence_penalty",
    "frequency_penalty",
    "system_prompt",
)


class GenerationParamsMixin(BaseModel):
    """可选生成参数。前端不传的键整个不出现在请求体里，由配置默认值兜底。"""

    temperature: Optional[float] = Field(default=None, ge=0, le=2)
    top_p: Optional[float] = Field(default=None, ge=0, le=1)
    max_tokens: Optional[int] = Field(default=None, gt=0)
    presence_penalty: Optional[float] = Field(default=None, ge=-2, le=2)
    frequency_penalty: Optional[float] = Field(default=None, ge=-2, le=2)
    system_prompt: Optional[str] = Field(default=None)
    allow_failure_retry: bool = Field(
        default=True,
        description="是否允许沿用 Provider 配置进行传输层失败自动重试",
    )


def build_gen_kwargs(req: Any) -> dict:
    """从请求中提取非空的生成参数，用于传入 LLMService。

    Args:
        req: 任何带有这六个属性的请求对象。

    Returns:
        只含非 None 值的关键字参数字典。
    """
    kwargs: dict = {}
    for key in GEN_PARAM_KEYS:
        val = getattr(req, key, None)
        if val is not None:
            kwargs[key] = val
    return kwargs


def build_runtime_kwargs(req: Any) -> dict[str, int]:
    """根据请求决定是否禁用 Provider 传输层自动重试。

    ``allow_failure_retry=True`` 保持既有行为，由 Provider 的 ``max_retries``
    配置决定实际次数；显式关闭时只覆盖本次请求，不修改持久化配置。
    """
    if getattr(req, "allow_failure_retry", True):
        return {}
    return {"max_provider_retries": 0}


def safe_novel_text(novel: dict, field: str, fallback: str = "未提供") -> str:
    """读取小说字段并转换为适合提示词的文本。

    Args:
        novel: 已落库小说文档。
        field: 字段名。
        fallback: 字段为空时使用的占位文本。

    Returns:
        可放入提示词的字符串；列表字段以顿号连接。
    """
    value = novel.get(field)
    if isinstance(value, list):
        return "、".join(str(item).strip() for item in value if str(item).strip()) or fallback
    return str(value or "").strip() or fallback
