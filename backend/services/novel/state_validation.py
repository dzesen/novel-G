"""状态回填 payload 的纯校验函数：不碰数据库、不抛异常。

与 outline_validation 同源的分层理由：校验必须能脱离 MongoDB 单测，且预览端
（report 模式）与 accept 服务（raise 模式）共用同一实现——两处各写一份规则必然漂移。

**为什么不复用 validate_outline_ids**：细纲的 id 字段是平铺的
（present_character_card_ids 等），本模块的 id 嵌套在列表项里
（character_updates[].card_id），_OUTLINE_ID_FIELDS 那张 (字段, 分类, 是否列表)
的表套不上。共用的只有 roster 提取那一段，已抽为 known_id_sets。
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from backend.services.novel.outline_validation import known_id_sets

# (payload 字段, 列表项里的 id 键, roster 分类键)。分类必须逐字段指定：
# characters 与 threads 是两个不同的集合，混成一个 id 集合会让
# "伏笔 id 填进角色字段"悄悄通过。
_STATE_ID_FIELDS: Tuple[Tuple[str, str, str], ...] = (
    ("character_updates", "card_id", "characters"),
    ("thread_updates", "thread_id", "threads"),
    # accept 入参用的字段名与 LLM 输出不同，但校验规则相同。
    ("accepted_thread_updates", "thread_id", "threads"),
)


def validate_state_ids(
    payload: Dict[str, Any],
    roster: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, List[str]]]:
    """剔除状态回填 payload 中不在 roster 内的 id 并报告剔除内容。

    Args:
        payload: LLM 输出或 accept 入参。缺失字段一律跳过，不凭空创造。
        roster: `context_builder.build_roster` 的产物。

    Returns:
        (cleaned, dropped)。cleaned 是**新 dict**（不修改入参，列表项亦浅拷贝）；
        dropped 形如 {"character_updates": ["<id>"]}，空 dict 表示全部合法。

    与 validate_outline_ids 同一约定：**不抛异常**。预览端按 dropped 上报
    （人能看见），accept 端按 dropped 拒绝整份 payload（那里没有预览可上报，
    静默剔除会变成无声降级）。
    """
    known = known_id_sets(roster)

    cleaned = dict(payload)
    dropped: Dict[str, List[str]] = {}

    for field, id_key, roster_key in _STATE_ID_FIELDS:
        if field not in cleaned:
            continue
        valid = known[roster_key]
        items = [dict(item) for item in (payload.get(field) or [])]
        bad = [str(item.get(id_key)) for item in items if str(item.get(id_key)) not in valid]
        cleaned[field] = [item for item in items if str(item.get(id_key)) in valid]
        if bad:
            dropped[field] = bad

    return cleaned, dropped
