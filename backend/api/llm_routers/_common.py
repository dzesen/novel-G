"""三个 LLM 路由（create_novel / outline / prose）共用的请求件。

这些字段与函数此前在 create_novel_router 与 outline_router 各有一份逐字相同的拷贝。
本模块是它们唯一的来源。

**刻意不收纳 `_check_json_schema_support`**：它的模块级身份是测试
monkeypatch 策略的一部分（tests/test_outline_router.py 直接打路由模块上的
那个名字），挪进本模块会让 monkeypatch 静默失效；且正文工作流是纯文本、
从不走 JSON Schema，并不需要它。
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
