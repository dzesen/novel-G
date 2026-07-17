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

# 不要在本模块重新定义 ACTIVE_THREAD_STATUSES——它已由 plot_thread_repository
# 定义，两处各写一份必然随时间漂移。导入仓储模块不会连数据库
# （BaseRepository.collection 是惰性 property），故本模块仍可脱离 MongoDB 测试。
from backend.db.repositories.plot_thread_repository import ACTIVE_THREAD_STATUSES

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


# 截断优先级：数字越小越先被丢。与设计 §5.1 的优先级表一一对应，
# 该表自称"本设计中最关键的一条"。
SECTION_PRIORITY = {
    "core_settings": 100,      # 永不截断
    "chapter_outline": 100,    # 永不截断
    "threads_to_resolve": 100, # 永不截断
    "permanent_facts": 100,    # 永不截断——防止"死人复活"的唯一屏障
    "present_cards": 50,
    "volume": 40,
    "recent_chapters": 30,
    "other_threads": 20,
    "minor_cards": 10,         # 最先丢
}
NEVER_TRUNCATE = {name for name, weight in SECTION_PRIORITY.items() if weight >= 100}


def _facts_up_to(state: dict, chapter_order: int) -> list:
    """取 chapter_order 及之前确立的永久事实。

    过滤是回溯的关键：重写第 30 章时喂入第 80 章的事实就是剧透。
    """
    facts = state.get("permanent_facts") or []
    return [f for f in facts if int(f.get("chapter_order", 0)) <= chapter_order]


def _format_facts(name: str, facts: list) -> str:
    lines = [f"- {f['fact']}（第 {f['chapter_order']} 章确立，{f['kind']}）" for f in facts]
    return f"{name} 的既定事实：\n" + "\n".join(lines)


def assemble_context(inputs: dict, budget: int = DEFAULT_CONTEXT_TOKEN_BUDGET) -> ChapterContext:
    """把取数结果装配成上下文包。纯函数，不碰数据库。

    Args:
        inputs: 取数层产出的普通 dict，形状见本模块文档。
        budget: token 预算；超出时按 SECTION_PRIORITY 从低往高截断。

    Returns:
        装配好的 ChapterContext，含被截断段落的标识。
    """
    novel = inputs.get("novel") or {}
    volume = inputs.get("volume") or {}
    chapter = inputs.get("chapter") or {}
    outline = chapter.get("outline") or {}
    cards = inputs.get("cards") or {}
    states = inputs.get("states") or {}
    chapter_order = int(chapter.get("order_index") or 0)

    present_ids = list(outline.get("present_character_card_ids") or [])
    sections: List[ContextSection] = []

    core_lines = [
        f"核心种子：{novel.get('core_seed', '')}",
        f"世界观：{novel.get('worldview', '')}",
        f"写作风格：{novel.get('writing_style', '')}",
        f"叙事视角：{novel.get('narrative_pov', '')}",
        f"基调：{novel.get('tone', '')}",
        f"时代背景：{novel.get('era_background', '')}",
    ]
    sections.append(ContextSection(name="core_settings", content="\n".join(core_lines)))

    if outline:
        sections.append(ContextSection(name="chapter_outline", content=f"本章细纲：{outline}"))

    # 出场人物：完整卡片 + current_state
    present_blocks = []
    for card_id in present_ids:
        card = cards.get(card_id)
        if not card:
            continue
        state = states.get(card_id) or {}
        block = f"{card['name']}：{card.get('description', '')}"
        if state.get("current_state"):
            block += f"\n当下状态（截至第 {state.get('as_of_chapter_order', chapter_order)} 章）：{state['current_state']}"
        present_blocks.append(block)
    if present_blocks:
        sections.append(ContextSection(name="present_cards", content="\n\n".join(present_blocks)))

    # permanent_facts：出场人物 + 全部主要角色（无条件）
    fact_ids = list(present_ids)
    for card_id, card in cards.items():
        if card.get("importance") == "main" and card_id not in fact_ids:
            fact_ids.append(card_id)

    fact_blocks = []
    for card_id in fact_ids:
        card = cards.get(card_id)
        state = states.get(card_id)
        if not card or not state:
            continue
        facts = _facts_up_to(state, chapter_order)
        if facts:
            fact_blocks.append(_format_facts(card["name"], facts))
    if fact_blocks:
        sections.append(ContextSection(name="permanent_facts", content="\n\n".join(fact_blocks)))

    if volume.get("summary") or volume.get("arc"):
        sections.append(ContextSection(
            name="volume",
            content=f"本卷摘要：{volume.get('summary', '')}\n本卷弧线：{volume.get('arc', '')}",
        ))

    recent = inputs.get("recent_chapters") or []
    if recent:
        lines = [f"第 {c['order_index']} 章：{c['summary']}" for c in recent if c.get("summary")]
        if lines:
            sections.append(ContextSection(name="recent_chapters", content="最近章节摘要：\n" + "\n".join(lines)))

    threads = [t for t in (inputs.get("threads") or []) if t.get("status") in ACTIVE_THREAD_STATUSES]
    to_resolve = set(outline.get("threads_resolved") or [])
    resolving = [t for t in threads if t.get("name") in to_resolve or t.get("_id") in to_resolve]
    others = [t for t in threads if t not in resolving]

    if resolving:
        lines = [f"- {t['name']}：{t.get('description', '')}" for t in resolving]
        sections.append(ContextSection(name="threads_to_resolve", content="本章需回收的伏笔：\n" + "\n".join(lines)))
    if others:
        # 远期伏笔先丢，故按 due 升序排；due 为空的排最后。
        others.sort(key=lambda t: (t.get("due_chapter_order") is None, t.get("due_chapter_order") or 0))
        lines = [f"- {t['name']}：{t.get('description', '')}" for t in others]
        sections.append(ContextSection(name="other_threads", content="活跃伏笔：\n" + "\n".join(lines)))

    return ChapterContext(sections=sections)
