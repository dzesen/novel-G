"""上下文装配器：给定 (novel_id, chapter_id) 产出喂给 prompt 的上下文包。

严格分两层：
- fetch_context_inputs：唯一碰数据库的薄取数层，不含逻辑。
- assemble_context：纯函数，承载全部装配与截断逻辑，入参是普通 dict。

分层不是洁癖——装配逻辑（尤其截断分支）必须能脱离 MongoDB 全覆盖测试。
"""

from __future__ import annotations

import json
import logging
import math
import unicodedata
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# 不要在本模块重新定义 ACTIVE_THREAD_STATUSES——它已由 plot_thread_repository
# 定义，两处各写一份必然随时间漂移。导入仓储模块不会连数据库
# （BaseRepository.collection 是惰性 property），故本模块仍可脱离 MongoDB 测试。
from backend.db.repositories.character_repository import character_repo
from backend.db.repositories.character_state_repository import character_state_repo
from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.repositories.novel_repository import novel_repo
from backend.db.repositories.plot_thread_repository import ACTIVE_THREAD_STATUSES, plot_thread_repo
from backend.db.repositories.volume_repository import volume_repo
from backend.services.novel.chapter_timeline import ChapterTimeline
from backend.services.novel.narrative_timeline import narrative_timeline
from backend.db.repositories.worldbook_repository import worldbook_repo

# 默认上下文预算。写到第 87 章时，"最近 K 章 + 所有活跃伏笔 + 相关卡片"
# 必然撑爆窗口；没有预算控制，系统会在中后期以"莫名其妙的 API 报错"死掉。
DEFAULT_CONTEXT_TOKEN_BUDGET = 8000

# 最近 K 章摘要。K=5，硬编码；配置项留到有真实数据之后（设计已确认决策）。
RECENT_CHAPTER_COUNT = 5

# A profile may retain more examples for editing/export, while prose generation only
# receives a small sample. The remaining examples are reference material, not facts.
DIALOGUE_EXAMPLES_PER_CHARACTER = 4

# 世界书可以有数百条，细纲阶段只给 Agent 一份有界目录，不给条目正文。
# 这里按 Python 字符数计数而不是 UTF-8 字节数；中文也严格是一字符，避免同一份
# 目录仅因语言不同产生三倍容量偏差。完整条目正文仍只由 assemble_context 按细纲
# referenced_worldbook_card_ids 装配。
WORLD_ENTRY_INDEX_MAX_CHARACTERS = 12_000
WORLD_ENTRY_INDEX_SUMMARY_MAX_CHARACTERS = 160
WORLD_ENTRY_INDEX_SECTION = "world_entry_index"
_WORLD_ENTRY_INDEX_HEADER = (
    "【世界条目紧凑索引（仅用于细纲选择，不含条目正文）】\n"
    "关键词只是检索信号，不会自动激活条目。需要使用某条设定时，必须把其 card_id "
    "显式写入 referenced_worldbook_card_ids；完整正文只会在声明后由系统按正式 ID 装配。"
)

# other_threads 中 due 为空的 drop_rank：无截止期即无紧迫性，视作无限远，最先丢。
_NO_DUE_DROP_RANK = -(10 ** 9)


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


class ContextItem(BaseModel):
    """段落内的一个可独立丢弃的条目。"""

    text: str = Field(description="条目正文")
    drop_rank: int = Field(default=0, description="截断排序键：同段落内越小越先丢")
    selection_id: Optional[str] = Field(
        default=None,
        description="细纲选择目录对应的正式 card_id；普通上下文条目为空",
    )


class ContextSection(BaseModel):
    """上下文中的一段，按装配优先级排列。

    内容由 header + items 派生：header 是段落固定前缀（如"最近章节摘要："），
    items 是可逐条丢弃的条目。单块段落用一个 item 承载、header 留空。
    """

    name: str = Field(description="段落标识，与截断优先级表对应")
    header: str = Field(default="", description="段落固定前缀，随存活条目一起呈现")
    items: List[ContextItem] = Field(default_factory=list, description="段落条目，按呈现顺序排列")

    @property
    def content(self) -> str:
        """由 header 与存活 items 派生的段落正文。"""
        parts = [self.header] if self.header else []
        parts.extend(item.text for item in self.items)
        return "\n".join(parts)


def _blob(name: str, text: str) -> "ContextSection":
    """造一个单条 item、无 header 的段落。"""
    return ContextSection(name=name, items=[ContextItem(text=text)])


def _core_settings_section(novel: dict) -> "ContextSection":
    """装 core_settings 段。正文模式与细纲模式共用（§4.2 共享 helper），避免两处漂移。"""
    core_lines = [
        f"核心种子：{novel.get('core_seed', '')}",
        f"世界观：{novel.get('worldview', '')}",
        f"写作风格：{novel.get('writing_style', '')}",
        f"叙事视角：{novel.get('narrative_pov', '')}",
        f"基调：{novel.get('tone', '')}",
        f"时代背景：{novel.get('era_background', '')}",
    ]
    return _blob("core_settings", "\n".join(core_lines))


def _volume_section(volume: dict, chapter: dict) -> "Optional[ContextSection]":
    """装配不可静默丢弃的当前卷结构契约；正文与细纲模式共用。"""
    if not any(
        volume.get(key)
        for key in ("title", "summary", "arc", "order_index", "chapter_range")
    ):
        return None

    lines = [
        "【当前卷大纲（必须遵守）】",
        "本章细纲与正文必须服务于本卷大纲，不得提前完成后续卷目标，也不得偏离本卷弧线。",
    ]
    title = str(volume.get("title") or "").strip()
    volume_order = int(volume.get("order_index") or 0)
    if title:
        prefix = f"第 {volume_order} 卷" if volume_order else "当前卷"
        lines.append(f"{prefix}：《{title}》")

    chapter_range = volume.get("chapter_range") or {}
    start = chapter_range.get("start")
    end = chapter_range.get("end")
    if start is not None and end is not None:
        lines.append(f"规划章节范围：全书第 {start}-{end} 章")

    volume_chapter_index = chapter.get("volume_chapter_index")
    volume_chapter_count = chapter.get("volume_chapter_count")
    book_ordinal = chapter.get("book_ordinal")
    if volume_chapter_index and volume_chapter_count:
        progress = f"本卷第 {volume_chapter_index}/{volume_chapter_count} 章"
        if book_ordinal:
            progress += f"（全书叙事序第 {book_ordinal} 章）"
        lines.append(f"当前进度：{progress}")

    if volume.get("summary"):
        lines.append(f"本卷剧情摘要：{volume['summary']}")
    if volume.get("arc"):
        lines.append(f"本卷弧线：{volume['arc']}")
    return _blob("volume", "\n".join(lines))


def _recent_chapters_section(recent: list) -> "Optional[ContextSection]":
    """装 recent 章摘要段，每章一条可独立丢弃的 item。两模式共用（§4.2 共享 helper）。

    drop_rank = book_ordinal：最旧的全书位置最小 → 逐项截断时最先丢。
    """
    items = [
        ContextItem(
            text=f"{c.get('display_label') or ('第 %s 章' % c['order_index'])}：{c['summary']}",
            drop_rank=int(c.get("book_ordinal") or c["order_index"]),
        )
        for c in recent if c.get("summary")
    ]
    if not items:
        return None
    return ContextSection(name="recent_chapters", header="最近章节摘要：", items=items)


def _thread_items(threads: list) -> "List[ContextItem]":
    """把活跃伏笔装成 ContextItem 列表。两模式共用。

    呈现顺序：due 升序、None 最后（近的先呈现给 AI）。
    drop_rank：-(due)，None 取极小值——due 越远越先丢，None 无截止期最先丢，与呈现顺序相反。
    返回排好序的新列表，不修改入参。
    """
    ordered = sorted(threads, key=lambda t: (t.get("due_chapter_order") is None, t.get("due_chapter_order") or 0))
    return [
        ContextItem(
            text=f"- {t['name']}：{t.get('description', '')}",
            drop_rank=(-(t["due_chapter_order"]) if t.get("due_chapter_order") is not None else _NO_DUE_DROP_RANK),
        )
        for t in ordered
    ]


class ChapterContext(BaseModel):
    """喂给 prompt 的上下文包。"""

    sections: List[ContextSection] = Field(default_factory=list, description="按顺序排列的上下文段落")
    truncated_sections: List[str] = Field(
        default_factory=list,
        description="因超预算被整段丢空的段落标识；截断必须可观测",
    )
    dropped_item_counts: Dict[str, int] = Field(
        default_factory=dict,
        description="部分被丢（段落未清空）的段落 -> 丢弃条目数；与 truncated_sections 互补",
    )
    selectable_worldbook_card_ids: List[str] = Field(
        default_factory=list,
        description="本次实际展示给细纲 Agent 的世界资料卡正式 ID，按索引呈现顺序排列",
    )

    @property
    def total_tokens(self) -> int:
        """全部段落的估算 token 合计。"""
        return sum(estimate_tokens(section.content) for section in self.sections)

    def to_prompt_text(self) -> str:
        """按顺序拼成可直接放入 prompt 的文本。"""
        return "\n\n".join(section.content for section in self.sections if section.content)


# 截断优先级：数字越小越先被丢。九档对应设计 §5.1：core_settings /
# chapter_outline / threads_to_resolve / permanent_facts / present_cards /
# volume / recent_chapters / other_threads / minor_cards。volume 现作为整卷/整本
# 生成的结构契约进入永不截断档；第 9 档
# minor_cards 见 §5.1 第 9 档，已实现。
SECTION_PRIORITY = {
    "core_settings": 100,      # 永不截断
    "chapter_outline": 100,    # 永不截断
    "threads_to_resolve": 100, # 永不截断
    "permanent_facts": 100,    # 永不截断——防止"死人复活"的唯一屏障
    "present_cards": 50,
    "present_states": 90,
    "portrayal_context": 15,
    "dialogue_examples": 5,
    "volume": 100,             # 永不截断——整卷/整本生成的结构契约
    "recent_chapters": 30,
    "other_threads": 20,
    "minor_cards": 10,         # 最先丢：地点/物品/规则卡
}
PROSE_NEVER_TRUNCATE = {name for name, weight in SECTION_PRIORITY.items() if weight >= 100}


class ContextBudgetError(Exception):
    """永不截断档自身已超预算，无法在不牺牲安全信息的前提下装配上下文。"""


# 细纲模式的截断优先级：与正文模式不同。roster 永不截断——截了它 AI 就吐不出
# 合法人物/伏笔 id，是失败而非降级。世界卡不再塞进 roster，而由有独立字符
# 上限的 world_entry_index 承载；该目录允许显式截断并复用 context_truncated
# 通知。没有 chapter_outline/present_cards/threads_to_resolve 三段（它们是本
# 工作流的输出）。
OUTLINE_SECTION_PRIORITY = {
    "core_settings": 100,     # 永不截断
    "permanent_facts": 100,   # 永不截断——死人复活屏障
    "roster": 100,            # 永不截断——AI 选人物/伏笔的唯一来源
    "volume": 100,            # 永不截断——章节细纲必须服从本卷结构
    "recent_chapters": 30,
    "other_threads": 20,
    WORLD_ENTRY_INDEX_SECTION: 10,
}
OUTLINE_NEVER_TRUNCATE = {name for name, weight in OUTLINE_SECTION_PRIORITY.items() if weight >= 100}


def _facts_up_to(state: dict, chapter_order: int, book_ordinal: int | None = None) -> list:
    """取目标章节及之前确立的永久事实。

    新数据按稳定 source_chapter_id 派生的全书序过滤；历史裸章号只有在整本
    唯一匹配时才会由取数层补 `_source_book_ordinal`，歧义数据保守排除。
    """
    facts = state.get("permanent_facts") or []
    if book_ordinal is not None:
        return [
            fact for fact in facts
            if isinstance(fact.get("_source_book_ordinal"), int)
            and fact["_source_book_ordinal"] <= book_ordinal
        ]
    return [f for f in facts if int(f.get("chapter_order", 0)) <= chapter_order]


def _format_facts(name: str, facts: list) -> str:
    lines = [
        f"- {fact['fact']}（{fact.get('_source_label') or ('第 %s 章' % fact.get('chapter_order', '?'))}确立，{fact['kind']}）"
        for fact in facts
    ]
    return f"{name} 的既定事实：\n" + "\n".join(lines)


def _format_worldbook_card(card: dict) -> str:
    name = str(card.get("name") or "")
    description = str(card.get("description") or "")
    card_type = str(card.get("card_type") or "")
    if card_type == "location":
        return f"地点「{name}」：{description}"
    if card_type == "item":
        return f"物品「{name}」：{description}"
    if card_type == "rule":
        return f"世界规则「{name}」：{description}"
    if card_type == "lore":
        return (
            f"世界设定条目「{name}」"
            f"（按一条世界设定使用，不预设为地点、物品或世界规则）："
            f"{description}"
        )
    return f"设定资料「{name}」：{description}"


def _truncate_to_budget(sections: list, budget: int, priority: dict) -> tuple:
    """超预算时按 (段落优先级, item.drop_rank) 一次全局排序，逐条丢弃。

    段落优先级为主键、条目 drop_rank 为次键，段落级与条目级由此统一成一次排序。
    优先级 >= 100 的段落永不截断（其条目不进候选）：宁可请求失败，也不能让 AI
    在缺失既定事实的情况下写出死人复活。

    Args:
        sections: 已装配的段落列表。
        budget: token 预算。
        priority: {段落名: 权重} 表；权重越小越先丢，>= 100 为永不截断。

    Returns:
        (保留段落（保持原顺序、原 header）, 整段丢空的段名列表, {部分丢弃段名: 丢弃条目数})。
    """
    never = {name for name, weight in priority.items() if weight >= 100}
    total = sum(estimate_tokens(s.content) for s in sections)
    if total <= budget:
        return sections, [], {}

    candidates = [
        (priority.get(s.name, 0), item.drop_rank, id(item), item)
        for s in sections
        if s.name not in never
        for item in s.items
    ]
    candidates.sort(key=lambda c: (c[0], c[1]))

    dropped_ids: set = set()
    per_section: dict = {}
    id_to_section = {id(item): s.name for s in sections for item in s.items}
    for _prio, _rank, item_id, item in candidates:
        if total <= budget:
            break
        total -= estimate_tokens(item.text)
        dropped_ids.add(item_id)
        sname = id_to_section[item_id]
        per_section[sname] = per_section.get(sname, 0) + 1

    kept: list = []
    fully_dropped: list = []
    for s in sections:
        survivors = [item for item in s.items if id(item) not in dropped_ids]
        if s.items and not survivors and s.name not in never:
            fully_dropped.append(s.name)
            continue
        kept.append(ContextSection(name=s.name, header=s.header, items=survivors))

    partial = {name: count for name, count in per_section.items() if name not in fully_dropped}
    return kept, fully_dropped, partial


def assemble_context(inputs: dict, budget: int = DEFAULT_CONTEXT_TOKEN_BUDGET) -> ChapterContext:
    """把取数结果装配成上下文包。纯函数，不碰数据库。

    Args:
        inputs: 取数层产出的普通 dict，形状见本模块文档。
        budget: token 预算；超出时按 SECTION_PRIORITY 从低往高截断。

    Returns:
        装配好的 ChapterContext，含被截断段落的标识。

    Raises:
        ContextBudgetError: 永不截断档（core_settings / chapter_outline / volume /
            threads_to_resolve / permanent_facts）自身已超预算。permanent_facts
            随全书主要角色数量线性增长、无上界（§4.4 的 roster 有界假设在这里不
            成立），此为安全阀而非常态——写到中后期这一档迟早会撑爆窗口。
    """
    novel = inputs.get("novel") or {}
    volume = inputs.get("volume") or {}
    chapter = inputs.get("chapter") or {}
    outline = chapter.get("outline") or {}
    cards = inputs.get("cards") or {}
    states = inputs.get("states") or {}
    chapter_order = int(chapter.get("order_index") or 0)
    book_ordinal = chapter.get("book_ordinal")

    present_ids = list(outline.get("present_character_card_ids") or [])
    sections: List[ContextSection] = []

    sections.append(_core_settings_section(novel))

    if outline:
        sections.append(_blob("chapter_outline", f"本章细纲：{outline}"))

    # 出场人物的基本介绍和塑造约束：中优先级，可在极端预算下显式截断。
    present_blocks = []
    state_blocks = []
    portrayal_context_items: list[ContextItem] = []
    dialogue_items: list[ContextItem] = []
    for card_id in present_ids:
        card = cards.get(card_id)
        if not card:
            continue
        state = states.get(card_id) or {}
        profile = card.get("character_profile") or {}
        details = card.get("details") or {}
        block_lines = [f"{card['name']}：{card.get('description', '')}"]
        if details.get("personality"):
            block_lines.append(f"性格：{details['personality']}")
        if profile.get("portrayal_notes"):
            block_lines.append(f"人物塑造约束：{profile['portrayal_notes']}")
        present_blocks.append("\n".join(block_lines))

        state_ordinal = state.get("_as_of_book_ordinal")
        if state.get("current_state") and (
            book_ordinal is None
            or (isinstance(state_ordinal, int) and state_ordinal <= book_ordinal)
        ):
            state_blocks.append(
                f"{card['name']}的当下状态"
                f"（截至第 {state.get('as_of_chapter_order', chapter_order)} 章）："
                f"{state['current_state']}"
            )
        if profile.get("portrayal_context"):
            portrayal_context_items.append(
                ContextItem(
                    text=f"{card['name']}的表现场景参考：{profile['portrayal_context']}"
                )
            )
        for index, example in enumerate(
            (profile.get("dialogue_examples") or [])[
                :DIALOGUE_EXAMPLES_PER_CHARACTER
            ]
        ):
            dialogue_items.append(
                ContextItem(
                    text=f"{card['name']}的对白风格示例：{example}",
                    # Keep the earliest examples when only part of the section fits.
                    drop_rank=-index,
                )
            )
    if present_blocks:
        sections.append(_blob("present_cards", "\n\n".join(present_blocks)))
    if state_blocks:
        sections.append(_blob("present_states", "\n\n".join(state_blocks)))
    if portrayal_context_items:
        sections.append(
            ContextSection(
                name="portrayal_context",
                header="人物表现场景参考（不是已经发生的剧情）：",
                items=portrayal_context_items,
            )
        )
    if dialogue_items:
        sections.append(
            ContextSection(
                name="dialogue_examples",
                header="对白风格示例（只模仿语气，不视为剧情事实）：",
                items=dialogue_items,
            )
        )

    # permanent_facts 装配范围（设计 §5.2）分三档：
    # 1）本章出场人物（present_ids）；
    # 2）全书主要角色，无条件装配，不看是否出场；
    # 3）被本章细纲 mentioned 的次要人物（§4.1 mentioned_character_card_ids）。
    #
    # 第三档是【减灾非屏障】：只在 AI 真的把某人填进 mentioned_character_card_ids
    # 时才生效，AI 漏填则该人的既定事实静默缺席——正是本档要防的 bug。灾难性
    # 情形（主要角色死后开口）已由第二档无条件兜住，本档只窄化到"次要死者在后文
    # 被提及"。人可在预览里补 mentioned 名单。
    #
    # importance/card_type 是资料卡通用字段，第二档必须显式限定 card_type=="character"，
    # 否则会把 importance="main" 的地点/物品卡也拖进来。
    mentioned_ids = list(outline.get("mentioned_character_card_ids") or [])
    fact_ids = list(present_ids)
    for card_id in mentioned_ids:
        if card_id not in fact_ids:
            fact_ids.append(card_id)
    for card_id, card in cards.items():
        if (
            card.get("card_type") == "character"
            and card.get("importance") == "main"
            and card_id not in fact_ids
        ):
            fact_ids.append(card_id)

    fact_blocks = []
    for card_id in fact_ids:
        card = cards.get(card_id)
        state = states.get(card_id)
        if not card or not state:
            continue
        facts = _facts_up_to(state, chapter_order, book_ordinal)
        if facts:
            fact_blocks.append(_format_facts(card["name"], facts))
    if fact_blocks:
        sections.append(_blob("permanent_facts", "\n\n".join(fact_blocks)))

    volume_section = _volume_section(volume, chapter)
    if volume_section is not None:
        sections.append(volume_section)

    recent_section = _recent_chapters_section(inputs.get("recent_chapters") or [])
    if recent_section is not None:
        sections.append(recent_section)

    threads = [t for t in (inputs.get("threads") or []) if t.get("status") in ACTIVE_THREAD_STATUSES]
    # threads_resolved 存的是 ObjectId（设计 §4.1），只能按 _id 匹配。不能退回
    # 按 name 匹配：plot_threads 的 name 没有唯一索引（见 db/indexes.py），
    # 两条同名伏笔会被一起错误地拖进 threads_to_resolve 这一"永不截断"档。
    to_resolve = set(outline.get("threads_resolved") or [])
    resolving = [t for t in threads if t.get("_id") in to_resolve]
    others = [t for t in threads if t not in resolving]

    if resolving:
        lines = [f"- {t['name']}：{t.get('description', '')}" for t in resolving]
        sections.append(_blob("threads_to_resolve", "本章需回收的伏笔：\n" + "\n".join(lines)))
    if others:
        sections.append(ContextSection(name="other_threads", header="活跃伏笔：", items=_thread_items(others)))

    worldbook_cards = inputs.get("worldbook_cards") or {}
    referenced_ids = list(outline.get("referenced_worldbook_card_ids") or [])
    minor_blocks = []
    for card_id in referenced_ids:
        card = worldbook_cards.get(card_id)
        if not card:
            continue
        minor_blocks.append(_format_worldbook_card(card))
    if minor_blocks:
        sections.append(_blob("minor_cards", "相关设定：\n" + "\n".join(minor_blocks)))

    # permanent_facts 无上界（随主要角色数量线性增长，§4.4 的"roster 有界"假设
    # 在正文模式不成立），本档也可能像细纲模式的 roster 一样单独超预算：宁可
    # 报错也不能悄悄丢弃"死人复活"屏障——没有这道守卫，_truncate_to_budget
    # 只从可丢弃段落里丢，永不截断档超预算时会静默地丢无可丢、返回一个仍然
    # 超预算的包，且因为 truncated_sections/dropped_item_counts 皆空，
    # prose_router 也不会发 context 帧告知前端（见该模块 §6 契约）。
    never_tokens = sum(
        estimate_tokens(s.content) for s in sections if s.name in PROSE_NEVER_TRUNCATE
    )
    if never_tokens > budget:
        raise ContextBudgetError(
            f"正文上下文的永不截断档已达 {never_tokens} tokens，超出预算 {budget}；"
            f"很可能是 permanent_facts 过多（主要角色事实随全书线性增长）。请精简后重试。"
        )

    kept, dropped, partial = _truncate_to_budget(sections, budget, SECTION_PRIORITY)
    if dropped or partial:
        logger.warning(
            "上下文超预算，整段丢弃 %s，部分丢弃 %s（预算 %s tokens）。本章将在信息不全的情况下生成。",
            dropped,
            partial,
            budget,
        )
    return ChapterContext(sections=kept, truncated_sections=dropped, dropped_item_counts=partial)


def build_roster(cards: dict, worldbook_cards: dict, threads: list) -> dict:
    """由已取到的卡片/伏笔构造 roster（AI 可选中的 id 名单）。纯函数，无 IO。

    抽成共享函数是因为它有**两个**调用方：预览侧的 fetch_context_inputs（用它
    已经查到的数据，不额外查库）与 accept 侧的 fetch_roster（自己查库）。
    两处各写一份必然漂移，而漂移的表现是"预览通过的 payload 在 accept 被拒"——
    一个用户完全无法自救的失败。

    Args:
        cards: {id 字符串: {"name", "description", ...}} 人物卡。
        worldbook_cards: 同上，世界卡（地点/物品/规则/通用世界设定）。
        threads: [{"_id" 字符串, "name", "description", ...}] 活跃伏笔。

    Returns:
        {"characters": [...], "worldbook": [...], "threads": [...]}，每项 {id, name, brief}。
    """
    return {
        "characters": [
            {
                "id": cid,
                "name": card["name"],
                "aliases": list(
                    (card.get("character_profile") or {}).get("aliases") or []
                ),
                "brief": card.get("description", ""),
            }
            for cid, card in cards.items()
        ],
        "worldbook": [
            {"id": wid, "name": card["name"], "brief": card.get("description", "")}
            for wid, card in worldbook_cards.items()
        ],
        "threads": [
            {"id": thread["_id"], "name": thread["name"], "brief": thread.get("description", "")}
            for thread in threads
        ],
    }


async def fetch_roster(novel_id: str) -> dict:
    """只取 roster 所需的数据并构造 roster（accept 侧的 id 存在性校验用）。

    伏笔只取 ACTIVE_THREAD_STATUSES，与 fetch_context_inputs 一致——accept 侧
    看到的名单必须与预览侧**完全相同**。若一条伏笔在预览之后被回收，accept
    会因此报错：那是正确行为（可见的失败），不是应该放宽的地方。
    """
    card_docs = await character_repo.list_cards(novel_id, "character")
    cards = {
        str(card["_id"]): {
            "name": card.get("name", ""),
            "description": card.get("description", ""),
            "character_profile": dict(card.get("character_profile") or {}),
        }
        for card in card_docs
    }

    worldbook_cards: dict = {}
    for card_type in worldbook_repo.supported_types:
        for card in await worldbook_repo.list_cards(novel_id, card_type):
            worldbook_cards[str(card["_id"])] = {
                "name": card.get("name", ""),
                "description": card.get("description", ""),
            }

    thread_docs = await plot_thread_repo.list_threads(novel_id, statuses=ACTIVE_THREAD_STATUSES)
    threads = [
        {
            "_id": str(thread["_id"]),
            "name": thread.get("name", ""),
            "description": thread.get("description", ""),
        }
        for thread in thread_docs
    ]

    return build_roster(cards, worldbook_cards, threads)


def _roster_section(roster: dict) -> ContextSection:
    """把人物/伏笔 roster 装成永不截断段落。

    世界资料卡刻意不在这里呈现：其完整 description 可能是数千字条目正文，数百
    条会把 roster 撑爆。它们由 _world_entry_index_section 以关键词 + 一句话摘要
    的形式单独、有界呈现。
    """
    lines: list = []
    for label, key in (("人物", "characters"), ("伏笔", "threads")):
        entries = roster.get(key) or []
        if not entries:
            continue
        lines.append(f"【{label}】可用 id 名单：")
        for e in entries:
            aliases = " / ".join(e.get("aliases") or [])
            alias_text = f"（别名：{aliases}）" if aliases else ""
            lines.append(
                f"- id={e['id']} {e['name']}{alias_text}：{e.get('brief', '')}"
            )
    return _blob("roster", "\n".join(lines))


def _normalized_inline_text(value: object) -> str:
    return " ".join(str(value or "").split())


def _one_sentence_summary(value: object) -> str:
    """从正式卡描述派生有界的一句话摘要，不把完整条目正文复制进索引。"""
    text = _normalized_inline_text(value)
    if not text:
        return ""
    sentence_end = len(text)
    for index, char in enumerate(text):
        if char in "。！？!?；;.":
            sentence_end = index + 1
            break
    summary = text[:sentence_end]
    if len(summary) <= WORLD_ENTRY_INDEX_SUMMARY_MAX_CHARACTERS:
        return summary
    return summary[: WORLD_ENTRY_INDEX_SUMMARY_MAX_CHARACTERS - 1] + "…"


def _world_entry_metadata(card: dict) -> tuple[list[str], bool, float]:
    interop = card.get("interop")
    display = (
        interop.get("display_metadata")
        if isinstance(interop, dict)
        else None
    )
    display = display if isinstance(display, dict) else {}
    raw_keys = display.get("keys")
    keys = (
        [str(item) for item in raw_keys if isinstance(item, str)]
        if isinstance(raw_keys, list)
        else []
    )
    regex_indexes: set[int] = set()
    raw_regex_fields = display.get("regex_fields")
    if isinstance(raw_regex_fields, list):
        for raw_field in raw_regex_fields:
            field = str(raw_field)
            if field.startswith("key[") and field.endswith("]"):
                try:
                    regex_indexes.add(int(field[4:-1]))
                except ValueError:
                    continue
    preview_notices = display.get("preview_notices")
    entry_level_regex = (
        isinstance(preview_notices, list)
        and any(
            isinstance(item, dict) and item.get("code") == "regex_present"
            for item in preview_notices
        )
        and not regex_indexes
    )
    if entry_level_regex:
        # V3 use_regex=true 是条目级开关；安全投影固定关闭，因此这些 keys 不得
        # 伪装成普通关键词影响细纲选择。其存在仍留在导入预览。
        keys = []
    elif regex_indexes:
        # 独立 World Info 可在同一 keys 数组混合普通词和 /.../flags；只保留
        # 普通词，正则字面量不执行、也不作为普通关键词送给 Agent。
        keys = [key for index, key in enumerate(keys) if index not in regex_indexes]
    constant = display.get("constant") is True
    raw_order = display.get("insertion_order")
    insertion_order = (
        float(raw_order)
        if isinstance(raw_order, (int, float))
        and not isinstance(raw_order, bool)
        and math.isfinite(float(raw_order))
        else 0.0
    )
    return keys, constant, insertion_order


def _world_entry_interop_projection(card: dict) -> dict:
    """取索引所需白名单元数据，隔离 raw_entry/脚本/decorators 等其余内容。"""
    raw_interop = card.get("interop")
    raw_display = (
        raw_interop.get("display_metadata")
        if isinstance(raw_interop, dict)
        else None
    )
    raw_display = raw_display if isinstance(raw_display, dict) else {}
    display: dict = {}
    for key in (
        "keys",
        "constant",
        "insertion_order",
        "regex_fields",
        "preview_notices",
    ):
        value = raw_display.get(key)
        if isinstance(value, list):
            display[key] = [
                dict(item) if isinstance(item, dict) else item
                for item in value
            ]
        elif value is not None:
            display[key] = value
    return {"display_metadata": display}


def _world_entry_index_section(
    worldbook_cards: dict,
) -> tuple[Optional[ContextSection], int]:
    """构造硬字符上限内的世界条目目录。

    保留优先级严格为 constant=true → importance=main → insertion_order 较高；
    card_id 只作完全相同优先级下的稳定 tie-breaker。条目必须整行进入，不切断
    card_id 或 keys；放不下就明确计入截断数。
    """
    ranked: list[tuple[bool, bool, float, str, dict]] = []
    for raw_card_id, raw_card in worldbook_cards.items():
        if not isinstance(raw_card, dict):
            continue
        card_id = str(raw_card_id)
        _keys, constant, insertion_order = _world_entry_metadata(raw_card)
        ranked.append(
            (
                constant,
                str(raw_card.get("importance") or "sub") == "main",
                insertion_order,
                card_id,
                raw_card,
            )
        )
    ranked.sort(key=lambda item: (-int(item[0]), -int(item[1]), -item[2], item[3]))

    items: list[ContextItem] = []
    used_characters = len(_WORLD_ENTRY_INDEX_HEADER)
    dropped = 0
    for priority_index, (_constant, _main, _order, card_id, card) in enumerate(ranked):
        keys, _constant_value, _order_value = _world_entry_metadata(card)
        line = (
            f"- card_id={card_id} | name={_normalized_inline_text(card.get('name'))}"
            f" | keys={json.dumps(keys, ensure_ascii=False, separators=(',', ':'))}"
            f" | summary={_one_sentence_summary(card.get('description'))}"
        )
        added_characters = len(line) + 1  # ContextSection.content 中 header/item 间的换行
        if used_characters + added_characters > WORLD_ENTRY_INDEX_MAX_CHARACTERS:
            # 严格保留排序后的前缀；不能因为后面的低优先级条目更短，就越过一个
            # 放不下的高优先级条目把它塞进来，否则实际截断顺序不再是规格声明的
            # constant → importance → insertion_order。
            dropped += len(ranked) - priority_index
            break
        items.append(
            ContextItem(
                text=line,
                # ranked 最前的是最高保留优先级；预算截断时反向从末尾开始丢。
                drop_rank=-priority_index,
                selection_id=card_id,
            )
        )
        used_characters += added_characters

    if not items:
        return None, dropped
    return (
        ContextSection(
            name=WORLD_ENTRY_INDEX_SECTION,
            header=_WORLD_ENTRY_INDEX_HEADER,
            items=items,
        ),
        dropped,
    )


def outline_selection_roster(
    roster: dict,
    selectable_worldbook_card_ids: list[str],
) -> dict:
    """把 AI 结果校验的世界卡名单收窄为本次索引实际展示过的正式 ID。"""
    allowed = set(selectable_worldbook_card_ids)
    return {
        **roster,
        "characters": list(roster.get("characters") or []),
        "worldbook": [
            dict(item)
            for item in (roster.get("worldbook") or [])
            if str(item.get("id") or "") in allowed
        ],
        "threads": list(roster.get("threads") or []),
    }


def assemble_outline_context(inputs: dict, budget: int = DEFAULT_CONTEXT_TOKEN_BUDGET) -> ChapterContext:
    """细纲模式装配。纯函数，不碰数据库。

    与 assemble_context（正文模式）的差异（设计 §4.2）：
    - 加人物/伏笔 roster（带 id），进永不截断档；
    - 加世界条目紧凑索引（card_id/name/keys/一句话摘要），先过字符硬上限，
      再服从总 token 预算；
    - 去掉 chapter_outline / present_cards / threads_to_resolve 三段——它们是本
      工作流的输出，生成细纲时尚不存在。

    Raises:
        ContextBudgetError: 永不截断档（尤其 roster 与 volume）自身已超预算。roster 有界
            （见 §4.4），此为安全阀而非常态。
    """
    novel = inputs.get("novel") or {}
    volume = inputs.get("volume") or {}
    chapter = inputs.get("chapter") or {}
    cards = inputs.get("cards") or {}
    states = inputs.get("states") or {}
    roster = inputs.get("roster") or {}
    chapter_order = int(chapter.get("order_index") or 0)
    book_ordinal = chapter.get("book_ordinal")

    sections: List[ContextSection] = []

    # core_settings / recent / threads 段用与正文模式同一套共享 helper（§4.2），
    # 只有 roster 与"去掉三段输出"是细纲模式独有。
    sections.append(_core_settings_section(novel))

    sections.append(_roster_section(roster))

    world_entry_index, hard_index_dropped = _world_entry_index_section(
        inputs.get("worldbook_cards") or {}
    )
    if world_entry_index is not None:
        sections.append(world_entry_index)

    # 主要角色的 permanent_facts 无条件装配（死人复活屏障，细纲阶段同样需要）。
    # 这一段与正文模式的三档装配逻辑不同（这里只取主要角色），故不共用 helper。
    fact_blocks = []
    for card_id, card in cards.items():
        if card.get("card_type") == "character" and card.get("importance") == "main":
            state = states.get(card_id)
            if not state:
                continue
            facts = _facts_up_to(state, chapter_order, book_ordinal)
            if facts:
                fact_blocks.append(_format_facts(card["name"], facts))
    if fact_blocks:
        sections.append(_blob("permanent_facts", "\n\n".join(fact_blocks)))

    volume_section = _volume_section(volume, chapter)
    if volume_section is not None:
        sections.append(volume_section)

    recent_section = _recent_chapters_section(inputs.get("recent_chapters") or [])
    if recent_section is not None:
        sections.append(recent_section)

    threads = [t for t in (inputs.get("threads") or []) if t.get("status") in ACTIVE_THREAD_STATUSES]
    if threads:
        sections.append(ContextSection(name="other_threads", header="活跃伏笔：", items=_thread_items(threads)))

    # roster 有界（§4.4）：永不截断档自身超预算时报错，不静默截断。
    never_tokens = sum(
        estimate_tokens(s.content) for s in sections if s.name in OUTLINE_NEVER_TRUNCATE
    )
    if never_tokens > budget:
        raise ContextBudgetError(
            f"细纲上下文的永不截断档已达 {never_tokens} tokens，超出预算 {budget}；"
            f"很可能是 roster 过大（卡片/伏笔过多）。请精简后重试。"
        )

    kept, dropped, partial = _truncate_to_budget(
        sections,
        budget,
        OUTLINE_SECTION_PRIORITY,
    )
    if hard_index_dropped and WORLD_ENTRY_INDEX_SECTION not in dropped:
        partial[WORLD_ENTRY_INDEX_SECTION] = (
            partial.get(WORLD_ENTRY_INDEX_SECTION, 0) + hard_index_dropped
        )
    selectable_worldbook_card_ids = [
        str(item.selection_id)
        for section in kept
        if section.name == WORLD_ENTRY_INDEX_SECTION
        for item in section.items
        if item.selection_id
    ]
    if dropped or partial:
        logger.warning(
            "细纲上下文超预算，整段丢弃 %s，部分丢弃 %s（预算 %s tokens）。",
            dropped,
            partial,
            budget,
        )
    return ChapterContext(
        sections=kept,
        truncated_sections=dropped,
        dropped_item_counts=partial,
        selectable_worldbook_card_ids=selectable_worldbook_card_ids,
    )


async def fetch_context_inputs(novel_id: str, chapter_id: str) -> dict:
    """取出装配上下文所需的全部数据。唯一碰数据库的一层，不含逻辑。

    Args:
        novel_id: 小说 ObjectId 字符串。
        chapter_id: 目标章节 ObjectId 字符串。

    Returns:
        供 assemble_context 消费的普通 dict，形状见本模块文档。
    """
    novel = await novel_repo.get_novel_by_id(novel_id)
    chapter = await chapter_repo.get_chapter_by_id(chapter_id)
    volume = await volume_repo.get_volume_by_id(str(chapter["volume_id"]))

    order_index = int(chapter.get("order_index") or 0)
    all_chapters = await chapter_repo.get_chapters_by_novel(novel_id)
    volumes = await volume_repo.get_volumes_by_novel(novel_id)
    timeline = ChapterTimeline(volumes, all_chapters)
    target_position = timeline.position(chapter_id)
    volume_positions = [
        position
        for position in timeline.positions
        if position.volume_id == target_position.volume_id
    ]
    volume_chapter_index = next(
        index
        for index, position in enumerate(volume_positions, start=1)
        if position.chapter_id == chapter_id
    )
    chapter_by_id = {str(item["_id"]): item for item in all_chapters}
    recent = [
        {
            "order_index": position.chapter_order,
            "book_ordinal": position.book_ordinal,
            "display_label": (
                f"第 {position.chapter_order} 章"
                if position.volume_order == target_position.volume_order
                else position.label
            ),
            "summary": chapter_by_id[position.chapter_id].get("summary", ""),
        }
        for position in timeline.recent_before(chapter_id, RECENT_CHAPTER_COUNT)
    ]

    card_docs = await character_repo.list_cards(novel_id, "character")
    cards = {
        str(card["_id"]): {
            "name": card.get("name", ""),
            "importance": card.get("importance", "sub"),
            "description": card.get("description", ""),
            "details": dict(card.get("details") or {}),
            "character_profile": dict(card.get("character_profile") or {}),
            "card_type": card.get("card_type", "character"),
        }
        for card in card_docs
    }

    worldbook_cards: dict = {}
    for card_type in worldbook_repo.supported_types:
        for card in await worldbook_repo.list_cards(novel_id, card_type):
            worldbook_cards[str(card["_id"])] = {
                "name": card.get("name", ""),
                "description": card.get("description", ""),
                "card_type": card.get("card_type", card_type),
                "importance": card.get("importance", "sub"),
                "sort_order": card.get("sort_order", 0),
                # 严格白名单投影；raw_entry、decorators、regex_scripts 与其他隔离
                # 内容连取数结果都不进入，更不会意外出现在 Prompt。
                "interop": _world_entry_interop_projection(card),
            }

    projection = await narrative_timeline.context_before(novel_id, chapter_id)
    state_docs = (
        projection.state_documents()
        if projection.states_tracked
        else await character_state_repo.list_states(novel_id)
    )
    states = {}
    for state in state_docs:
        facts = []
        for raw_fact in state.get("permanent_facts") or []:
            fact = dict(raw_fact)
            source_id = fact.get("source_chapter_id")
            try:
                source_position = (
                    timeline.position(str(source_id))
                    if source_id
                    else timeline.unique_legacy_order(int(fact.get("chapter_order") or 0))
                )
            except ValueError:
                source_position = None
            if source_position is not None:
                fact["_source_book_ordinal"] = source_position.book_ordinal
                fact["_source_label"] = source_position.label
            facts.append(fact)
        as_of_id = state.get("as_of_chapter_id")
        try:
            as_of_position = (
                timeline.position(str(as_of_id))
                if as_of_id
                else timeline.unique_legacy_order(int(state.get("as_of_chapter_order") or 0))
            )
        except ValueError:
            as_of_position = None
        states[str(state["card_id"])] = {
            "current_state": state.get("current_state", ""),
            "as_of_chapter_order": state.get("as_of_chapter_order", 0),
            "as_of_chapter_id": str(as_of_id) if as_of_id else None,
            "_as_of_book_ordinal": as_of_position.book_ordinal if as_of_position else None,
            "permanent_facts": facts,
        }

    thread_docs = (
        projection.active_threads
        if projection.threads_tracked
        else await plot_thread_repo.list_threads(
            novel_id, statuses=ACTIVE_THREAD_STATUSES
        )
    )
    threads = [
        {
            "_id": str(t["_id"]),
            "name": t.get("name", ""),
            "description": t.get("description", ""),
            "status": t.get("status", ""),
            "due_chapter_order": t.get("due_chapter_order"),
        }
        for t in thread_docs
    ]

    # outline 子文档里的 id 字段是真实 BSON ObjectId（设计 §4.1），而
    # assemble_context 拿它们去匹配上面已 str() 化的 cards/threads 主键——
    # ObjectId 与 str 用 == 恒不相等，不转换的话 present_cards、
    # threads_to_resolve（后者还是永不截断档）会静默地永远装不进内容。
    # 阶段 2 之前没有代码会写 chapter.outline，所以这里必须兜住 None；
    # 复制成新 dict 再改，不动 chapter 里读出来的原始子文档。
    raw_outline = chapter.get("outline")
    outline = None
    if raw_outline:
        outline = dict(raw_outline)
        outline["present_character_card_ids"] = [
            str(cid) for cid in (raw_outline.get("present_character_card_ids") or [])
        ]
        outline["mentioned_character_card_ids"] = [
            str(cid) for cid in (raw_outline.get("mentioned_character_card_ids") or [])
        ]
        outline["threads_resolved"] = [
            str(tid) for tid in (raw_outline.get("threads_resolved") or [])
        ]
        outline["referenced_worldbook_card_ids"] = [
            str(cid) for cid in (raw_outline.get("referenced_worldbook_card_ids") or [])
        ]
        pov_id = raw_outline.get("pov_character_card_id")
        outline["pov_character_card_id"] = str(pov_id) if pov_id is not None else None
        # threads_planted（设计 §4.1，同为 [ObjectId]）故意不在此处 str() 化：
        # assemble_context 目前不读这个字段，转换了也是死代码。但它和上面三个
        # 字段是同一种 BSON ObjectId，将来谁把它接进装配逻辑，必须照此处的写法
        # 先 str() 化，否则就是重新引入这段注释本身要防的那个 bug——
        # ObjectId != str，匹配不上任何东西，不报错，只是悄悄地永远装不进上下文。

    # roster：细纲模式喂给 AI 的可选名单，复用上面已取到的
    # cards/worldbook_cards/threads，不额外查库（见 assemble_outline_context）。
    # 形状由 build_roster 统一定义，accept 侧的 fetch_roster 用同一个函数。
    roster = build_roster(cards, worldbook_cards, threads)

    return {
        "novel": {
            "core_seed": novel.get("core_seed", ""),
            "worldview": novel.get("worldview", ""),
            "writing_style": novel.get("writing_style", ""),
            "narrative_pov": novel.get("narrative_pov", ""),
            "tone": novel.get("tone", ""),
            "era_background": novel.get("era_background", ""),
        },
        "volume": {
            "title": volume.get("title", ""),
            "summary": volume.get("summary", ""),
            "arc": volume.get("arc", ""),
            "order_index": volume.get("order_index"),
            "chapter_range": volume.get("chapter_range") or {},
        },
        "chapter": {
            "order_index": order_index,
            "book_ordinal": target_position.book_ordinal,
            "volume_chapter_index": volume_chapter_index,
            "volume_chapter_count": len(volume_positions),
            "chapter_id": chapter_id,
            "outline": outline,
        },
        "recent_chapters": recent,
        "cards": cards,
        "worldbook_cards": worldbook_cards,
        "states": states,
        "threads": threads,
        "roster": roster,
    }


async def build_context(
    novel_id: str,
    chapter_id: str,
    budget: int = DEFAULT_CONTEXT_TOKEN_BUDGET,
) -> ChapterContext:
    """取数 + 装配的组合入口。

    Args:
        novel_id: 小说 ObjectId 字符串。
        chapter_id: 目标章节 ObjectId 字符串。
        budget: token 预算。

    Returns:
        装配好的 ChapterContext。
    """
    return assemble_context(await fetch_context_inputs(novel_id, chapter_id), budget=budget)


async def build_outline_context(
    novel_id: str,
    chapter_id: str,
    budget: int = DEFAULT_CONTEXT_TOKEN_BUDGET,
) -> ChapterContext:
    """细纲模式的取数 + 装配组合入口。"""
    return assemble_outline_context(await fetch_context_inputs(novel_id, chapter_id), budget=budget)
