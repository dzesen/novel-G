"""状态回填 payload 的纯校验函数：不碰数据库、不抛异常。

与 outline_validation 同源的分层理由：校验必须能脱离 MongoDB 单测，且预览端
（report 模式）与 accept 服务（raise 模式）共用同一实现——两处各写一份规则必然漂移。

**为什么不复用 validate_outline_ids**：细纲的 id 字段是平铺的
（present_character_card_ids 等），本模块的 id 嵌套在列表项里
（character_updates[].card_id），_OUTLINE_ID_FIELDS 那张 (字段, 分类, 是否列表)
的表套不上。共用的只有 roster 提取那一段，已抽为 known_id_sets。
"""

from __future__ import annotations

import re
import unicodedata
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
_PLACEHOLDER_CHARACTER_ID = re.compile(
    r"^(?:character|char|role)[\s_-]*\d+$",
    re.IGNORECASE,
)


def _normalize_reference_label(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(normalized.split()).casefold()


def _character_reference_index(
    roster: Dict[str, Any],
) -> Tuple[set[str], Dict[str, List[Tuple[str, str]]]]:
    index: Dict[str, List[Tuple[str, str]]] = {}
    known_ids = {
        str(item.get("id") or "")
        for item in roster.get("characters") or []
        if str(item.get("id") or "")
    }
    for character in roster.get("characters") or []:
        card_id = str(character.get("id") or "")
        if not card_id:
            continue
        labels = [
            ("name", character.get("name")),
            *[("alias", alias) for alias in character.get("aliases") or []],
        ]
        for matched_by, label in labels:
            normalized = _normalize_reference_label(label)
            if normalized:
                index.setdefault(normalized, []).append((card_id, matched_by))
    return known_ids, index


def _resolve_character_reference(
    raw_value: Any,
    *,
    known_ids: set[str],
    index: Dict[str, List[Tuple[str, str]]],
) -> Tuple[str, str] | None:
    raw = str(raw_value or "")
    normalized = _normalize_reference_label(raw)
    if raw in known_ids or _PLACEHOLDER_CHARACTER_ID.fullmatch(normalized):
        return None
    matches = index.get(normalized, [])
    matched_ids = {card_id for card_id, _kind in matches}
    if len(matched_ids) != 1:
        return None
    card_id = next(iter(matched_ids))
    match_kinds = {
        kind for candidate_id, kind in matches if candidate_id == card_id
    }
    return card_id, "name" if "name" in match_kinds else "alias"


def resolve_state_character_references(
    payload: Dict[str, Any],
    roster: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    """Map only unique formal names/explicit aliases to stable character IDs.

    Placeholder-like values and ambiguous labels deliberately remain unchanged so
    the ordinary ID validator will report and remove them.
    """

    known_ids, index = _character_reference_index(roster)

    resolved = dict(payload)
    remapped: List[Dict[str, str]] = []
    updates = [dict(item) for item in payload.get("character_updates") or []]
    for update in updates:
        raw = str(update.get("card_id") or "")
        match = _resolve_character_reference(
            raw,
            known_ids=known_ids,
            index=index,
        )
        if match is None:
            continue
        card_id, matched_by = match
        update["card_id"] = card_id
        remapped.append(
            {
                "field": "character_updates",
                "from": raw,
                "to": card_id,
                "matched_by": matched_by,
            }
        )
    if "character_updates" in payload:
        resolved["character_updates"] = updates
    return resolved, remapped


def resolve_outline_character_references(
    payload: Dict[str, Any],
    roster: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    """Resolve character names/aliases in the flat chapter-outline ID fields."""

    known_ids, index = _character_reference_index(roster)
    resolved = dict(payload)
    remapped: List[Dict[str, str]] = []
    fields = (
        ("pov_character_card_id", False),
        ("present_character_card_ids", True),
        ("mentioned_character_card_ids", True),
    )
    for field, is_list in fields:
        if field not in payload:
            continue
        raw_values = (
            list(payload.get(field) or [])
            if is_list
            else [payload.get(field)]
        )
        next_values = []
        for raw_value in raw_values:
            match = _resolve_character_reference(
                raw_value,
                known_ids=known_ids,
                index=index,
            )
            if match is None:
                next_values.append(raw_value)
                continue
            card_id, matched_by = match
            next_values.append(card_id)
            remapped.append(
                {
                    "field": field,
                    "from": str(raw_value or ""),
                    "to": card_id,
                    "matched_by": matched_by,
                }
            )
        resolved[field] = next_values if is_list else next_values[0]
    return resolved, remapped


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


def state_reference_resolution(
    original: Dict[str, Any],
    cleaned: Dict[str, Any],
    dropped: Dict[str, List[str]],
    remapped: List[Dict[str, str]] | None = None,
) -> Dict[str, Any]:
    """Describe reference loss without relying on UI-only validation frames."""
    proposed_characters = len(original.get("character_updates") or [])
    accepted_characters = len(cleaned.get("character_updates") or [])
    proposed_threads = len(original.get("thread_updates") or [])
    accepted_threads = len(cleaned.get("thread_updates") or [])
    accepted_character_ids = [
        str(item.get("card_id") or "")
        for item in cleaned.get("character_updates") or []
        if str(item.get("card_id") or "")
    ]
    cleaned_threads = (
        cleaned.get("thread_updates")
        if "thread_updates" in cleaned
        else cleaned.get("accepted_thread_updates")
    ) or []
    accepted_thread_ids = [
        str(item.get("thread_id") or "")
        for item in cleaned_threads
        if str(item.get("thread_id") or "")
    ]
    return {
        "proposed_character_update_count": proposed_characters,
        "accepted_character_update_count": accepted_characters,
        "dropped_character_update_count": max(
            len(dropped.get("character_updates") or []),
            proposed_characters - accepted_characters,
        ),
        "proposed_thread_update_count": proposed_threads,
        "accepted_thread_update_count": accepted_threads,
        "dropped_thread_update_count": max(
            len(dropped.get("thread_updates") or []),
            proposed_threads - accepted_threads,
        ),
        "dropped": {
            str(field): [str(value) for value in values]
            for field, values in dropped.items()
        },
        "accepted": {
            "character_updates": accepted_character_ids,
            "thread_updates": accepted_thread_ids,
        },
        "remapped": [dict(item) for item in remapped or []],
    }
