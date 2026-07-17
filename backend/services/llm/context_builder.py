"""上下文装配器：给定 (novel_id, chapter_id) 产出喂给 prompt 的上下文包。

严格分两层：
- fetch_context_inputs：唯一碰数据库的薄取数层，不含逻辑。
- assemble_context：纯函数，承载全部装配与截断逻辑，入参是普通 dict。

分层不是洁癖——装配逻辑（尤其截断分支）必须能脱离 MongoDB 全覆盖测试。
"""

from __future__ import annotations

import logging
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
from backend.db.repositories.worldbook_repository import worldbook_repo

# 默认上下文预算。写到第 87 章时，"最近 K 章 + 所有活跃伏笔 + 相关卡片"
# 必然撑爆窗口；没有预算控制，系统会在中后期以"莫名其妙的 API 报错"死掉。
DEFAULT_CONTEXT_TOKEN_BUDGET = 8000

# 最近 K 章摘要。K=5，硬编码；配置项留到有真实数据之后（设计已确认决策）。
RECENT_CHAPTER_COUNT = 5

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


def _volume_section(volume: dict) -> "Optional[ContextSection]":
    """装 volume 段（本卷摘要 + 弧线）；两者皆空时返回 None。两模式共用。"""
    if not (volume.get("summary") or volume.get("arc")):
        return None
    return _blob("volume", f"本卷摘要：{volume.get('summary', '')}\n本卷弧线：{volume.get('arc', '')}")


def _recent_chapters_section(recent: list) -> "Optional[ContextSection]":
    """装 recent 章摘要段，每章一条可独立丢弃的 item。两模式共用（§4.2 共享 helper）。

    drop_rank = order_index：最旧的 order_index 最小 → 逐项截断时最先丢。
    """
    items = [
        ContextItem(text=f"第 {c['order_index']} 章：{c['summary']}", drop_rank=int(c["order_index"]))
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

    @property
    def total_tokens(self) -> int:
        """全部段落的估算 token 合计。"""
        return sum(estimate_tokens(section.content) for section in self.sections)

    def to_prompt_text(self) -> str:
        """按顺序拼成可直接放入 prompt 的文本。"""
        return "\n\n".join(section.content for section in self.sections if section.content)


# 截断优先级：数字越小越先被丢。九档对应设计 §5.1：core_settings /
# chapter_outline / threads_to_resolve / permanent_facts / present_cards /
# volume / recent_chapters / other_threads / minor_cards。第 9 档
# minor_cards 见 §5.1 第 9 档，已实现。
SECTION_PRIORITY = {
    "core_settings": 100,      # 永不截断
    "chapter_outline": 100,    # 永不截断
    "threads_to_resolve": 100, # 永不截断
    "permanent_facts": 100,    # 永不截断——防止"死人复活"的唯一屏障
    "present_cards": 50,
    "volume": 40,
    "recent_chapters": 30,
    "other_threads": 20,
    "minor_cards": 10,         # 最先丢：地点/物品/规则卡
}


class ContextBudgetError(Exception):
    """永不截断档自身已超预算，无法在不牺牲安全信息的前提下装配上下文。"""


# 细纲模式的截断优先级：与正文模式不同。roster 永不截断——截了它 AI 就吐不出
# 合法 id，是失败而非降级。没有 chapter_outline/present_cards/threads_to_resolve
# 三段（它们是本工作流的输出）。
OUTLINE_SECTION_PRIORITY = {
    "core_settings": 100,     # 永不截断
    "permanent_facts": 100,   # 永不截断——死人复活屏障
    "roster": 100,            # 永不截断——AI 选人/选物/选伏笔的唯一来源
    "volume": 40,
    "recent_chapters": 30,
    "other_threads": 20,
}
OUTLINE_NEVER_TRUNCATE = {name for name, weight in OUTLINE_SECTION_PRIORITY.items() if weight >= 100}


def _facts_up_to(state: dict, chapter_order: int) -> list:
    """取 chapter_order 及之前确立的永久事实。

    过滤是回溯的关键：重写第 30 章时喂入第 80 章的事实就是剧透。
    """
    facts = state.get("permanent_facts") or []
    return [f for f in facts if int(f.get("chapter_order", 0)) <= chapter_order]


def _format_facts(name: str, facts: list) -> str:
    lines = [f"- {f['fact']}（第 {f['chapter_order']} 章确立，{f['kind']}）" for f in facts]
    return f"{name} 的既定事实：\n" + "\n".join(lines)


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

    sections.append(_core_settings_section(novel))

    if outline:
        sections.append(_blob("chapter_outline", f"本章细纲：{outline}"))

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
        sections.append(_blob("present_cards", "\n\n".join(present_blocks)))

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
        facts = _facts_up_to(state, chapter_order)
        if facts:
            fact_blocks.append(_format_facts(card["name"], facts))
    if fact_blocks:
        sections.append(_blob("permanent_facts", "\n\n".join(fact_blocks)))

    volume_section = _volume_section(volume)
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
        minor_blocks.append(f"{card['name']}：{card.get('description', '')}")
    if minor_blocks:
        sections.append(_blob("minor_cards", "相关设定：\n" + "\n".join(minor_blocks)))

    kept, dropped, partial = _truncate_to_budget(sections, budget, SECTION_PRIORITY)
    if dropped or partial:
        logger.warning(
            "上下文超预算，整段丢弃 %s，部分丢弃 %s（预算 %s tokens）。本章将在信息不全的情况下生成。",
            dropped,
            partial,
            budget,
        )
    return ChapterContext(sections=kept, truncated_sections=dropped, dropped_item_counts=partial)


def _roster_section(roster: dict) -> ContextSection:
    """把 roster 装成一个永不截断的段落，条目携带 id 供 AI 选中。"""
    lines: list = []
    for label, key in (("人物", "characters"), ("世界设定", "worldbook"), ("伏笔", "threads")):
        entries = roster.get(key) or []
        if not entries:
            continue
        lines.append(f"【{label}】可用 id 名单：")
        for e in entries:
            lines.append(f"- id={e['id']} {e['name']}：{e.get('brief', '')}")
    return _blob("roster", "\n".join(lines))


def assemble_outline_context(inputs: dict, budget: int = DEFAULT_CONTEXT_TOKEN_BUDGET) -> ChapterContext:
    """细纲模式装配。纯函数，不碰数据库。

    与 assemble_context（正文模式）的差异（设计 §4.2）：
    - 加 roster（AI 选人/选物/选伏笔的名单，带 id），进永不截断档；
    - 去掉 chapter_outline / present_cards / threads_to_resolve 三段——它们是本
      工作流的输出，生成细纲时尚不存在。

    Raises:
        ContextBudgetError: 永不截断档（尤其 roster）自身已超预算。roster 有界
            （见 §4.4），此为安全阀而非常态。
    """
    novel = inputs.get("novel") or {}
    volume = inputs.get("volume") or {}
    chapter = inputs.get("chapter") or {}
    cards = inputs.get("cards") or {}
    states = inputs.get("states") or {}
    roster = inputs.get("roster") or {}
    chapter_order = int(chapter.get("order_index") or 0)

    sections: List[ContextSection] = []

    # core_settings / recent / threads 段用与正文模式同一套共享 helper（§4.2），
    # 只有 roster 与"去掉三段输出"是细纲模式独有。
    sections.append(_core_settings_section(novel))

    sections.append(_roster_section(roster))

    # 主要角色的 permanent_facts 无条件装配（死人复活屏障，细纲阶段同样需要）。
    # 这一段与正文模式的三档装配逻辑不同（这里只取主要角色），故不共用 helper。
    fact_blocks = []
    for card_id, card in cards.items():
        if card.get("card_type") == "character" and card.get("importance") == "main":
            state = states.get(card_id)
            if not state:
                continue
            facts = _facts_up_to(state, chapter_order)
            if facts:
                fact_blocks.append(_format_facts(card["name"], facts))
    if fact_blocks:
        sections.append(_blob("permanent_facts", "\n\n".join(fact_blocks)))

    volume_section = _volume_section(volume)
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

    kept, dropped, partial = _truncate_to_budget(sections, budget, OUTLINE_SECTION_PRIORITY)
    if dropped or partial:
        logger.warning(
            "细纲上下文超预算，整段丢弃 %s，部分丢弃 %s（预算 %s tokens）。",
            dropped,
            partial,
            budget,
        )
    return ChapterContext(sections=kept, truncated_sections=dropped, dropped_item_counts=partial)


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
    recent = [
        {"order_index": int(c.get("order_index") or 0), "summary": c.get("summary", "")}
        for c in all_chapters
        if 0 < int(c.get("order_index") or 0) < order_index
    ]
    recent.sort(key=lambda c: c["order_index"])
    recent = recent[-RECENT_CHAPTER_COUNT:]

    card_docs = await character_repo.list_cards(novel_id, "character")
    cards = {
        str(card["_id"]): {
            "name": card.get("name", ""),
            "importance": card.get("importance", "sub"),
            "description": card.get("description", ""),
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
            }

    state_docs = await character_state_repo.list_states(novel_id)
    states = {
        str(state["card_id"]): {
            "current_state": state.get("current_state", ""),
            "as_of_chapter_order": state.get("as_of_chapter_order", 0),
            "permanent_facts": state.get("permanent_facts", []),
        }
        for state in state_docs
    }

    thread_docs = await plot_thread_repo.list_threads(
        novel_id, statuses=ACTIVE_THREAD_STATUSES
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
    roster = {
        "characters": [
            {"id": cid, "name": c["name"], "brief": c.get("description", "")}
            for cid, c in cards.items()
        ],
        "worldbook": [
            {"id": wid, "name": w["name"], "brief": w.get("description", "")}
            for wid, w in worldbook_cards.items()
        ],
        "threads": [
            {"id": t["_id"], "name": t["name"], "brief": t.get("description", "")}
            for t in threads
        ],
    }

    return {
        "novel": {
            "core_seed": novel.get("core_seed", ""),
            "worldview": novel.get("worldview", ""),
            "writing_style": novel.get("writing_style", ""),
            "narrative_pov": novel.get("narrative_pov", ""),
            "tone": novel.get("tone", ""),
            "era_background": novel.get("era_background", ""),
        },
        "volume": {"summary": volume.get("summary", ""), "arc": volume.get("arc", "")},
        "chapter": {"order_index": order_index, "outline": outline},
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
