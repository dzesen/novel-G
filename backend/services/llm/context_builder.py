"""上下文装配器：给定 (novel_id, chapter_id) 产出喂给 prompt 的上下文包。

严格分两层：
- fetch_context_inputs：唯一碰数据库的薄取数层，不含逻辑。
- assemble_context：纯函数，承载全部装配与截断逻辑，入参是普通 dict。

分层不是洁癖——装配逻辑（尤其截断分支）必须能脱离 MongoDB 全覆盖测试。
"""

from __future__ import annotations

import unicodedata
from typing import List

from pydantic import BaseModel, Field

# 默认上下文预算。写到第 87 章时，"最近 K 章 + 所有活跃伏笔 + 相关卡片"
# 必然撑爆窗口；没有预算控制，系统会在中后期以"莫名其妙的 API 报错"死掉。
DEFAULT_CONTEXT_TOKEN_BUDGET = 8000


def _is_cjk(char: str) -> bool:
    """判断字符是否属于中日韩表意文字区。"""
    return unicodedata.east_asian_width(char) in {"W", "F"}


def estimate_tokens(text: str) -> int:
    """估算文本 token 数。

    中文约 1 字 1 token，拉丁文约 4 字符 1 token。这是刻意的近似：
    三家 provider 分词器各不相同，精确计数对任何一家都只能准一家，
    而本函数的用途是"何时开始截断"的触发器，量级正确即可。

    Args:
        text: 待估算文本。

    Returns:
        估算的 token 数。
    """
    if not text:
        return 0
    cjk = sum(1 for char in text if _is_cjk(char))
    rest = len(text) - cjk
    return cjk + (rest + 3) // 4


class ContextSection(BaseModel):
    """上下文中的一段，按装配优先级排列。"""

    name: str = Field(description="段落标识，与截断优先级表对应")
    content: str = Field(description="段落正文")


class ChapterContext(BaseModel):
    """喂给 prompt 的上下文包。"""

    sections: List[ContextSection] = Field(default_factory=list, description="按顺序排列的上下文段落")
    truncated_sections: List[str] = Field(
        default_factory=list,
        description="因超预算被丢弃的段落标识；截断必须可观测",
    )

    @property
    def total_tokens(self) -> int:
        """全部段落的估算 token 合计。"""
        return sum(estimate_tokens(section.content) for section in self.sections)

    def to_prompt_text(self) -> str:
        """按顺序拼成可直接放入 prompt 的文本。"""
        return "\n\n".join(section.content for section in self.sections if section.content)
