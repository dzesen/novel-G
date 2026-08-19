"""LLM 路由共用的请求模型、生成参数和安全文本读取工具。

结构化输出协议由 GenerationRuntime 统一规划；路由层不再维护 Provider 能力分支。
"""

from __future__ import annotations

from typing import Any

from backend.services.llm.generation_params import (
    GEN_PARAM_KEYS,
    GenerationParamsMixin,
    build_gen_kwargs,
    build_runtime_kwargs,
)

__all__ = [
    "GEN_PARAM_KEYS",
    "GenerationParamsMixin",
    "build_gen_kwargs",
    "build_runtime_kwargs",
    "safe_novel_text",
]


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
