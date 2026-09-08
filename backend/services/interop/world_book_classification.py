"""Deterministic proposal classification; never execute or inject book content."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from backend.services.interop.world_book_adapter import LorebookEntry


IMPORT_CARD_TYPES = frozenset({"character", "location", "item", "rule", "lore"})
MAX_CLASSIFICATION_CONTENT_CHARS = 24_000
_MAX_FIELDS = 512
_TYPE_LABELS = {
    "character": "character", "char": "character", "npc": "character",
    "角色": "character", "人物": "character", "角色卡": "character",
    "location": "location", "place": "location", "地点": "location",
    "item": "item", "物品": "item", "道具": "item", "装备": "item",
    "rule": "rule", "规则": "rule", "法则": "rule",
    "lore": "lore", "世界设定": "lore", "世界观": "lore",
}
_TITLE_MARKER = re.compile(r"^\s*[\[【(（]\s*([^\]】)）]{1,24})\s*[\]】)）]\s*(.*)$")
_FIELD_LINE = re.compile(r"^([ \t]*)([^\r\n:：<>#{}\[\]]{1,64})\s*[:：](.*)$")
_BASIC_INFO = {"基本信息", "基础信息", "基本资料", "基础资料", "basic_info", "basic_information"}
_APPEARANCE = {"外貌", "外貌特征", "外形", "外观", "appearance"}
_PERSONALITY = {"性格", "性格特点", "性格特征", "personality"}


@dataclass(frozen=True)
class EntryClassification:
    card_type: str
    reason_code: str
    name: str


@dataclass
class _Field:
    label: str
    children: list[_Field] = field(default_factory=list)


def _label(value: str) -> str:
    return value.strip().strip("\"'*").strip().lower().replace(" ", "_")


def _record_fields(content: str) -> list[_Field]:
    """Read field labels within bounded JSON or indented key/value records."""
    if len(content) > MAX_CLASSIFICATION_CONTENT_CHARS:
        return []
    text = content.strip()
    if text.startswith("{"):
        try:
            value = json.loads(text)
        except (ValueError, RecursionError):
            return []
        count = 0

        def fields(source: Any, depth: int = 0) -> list[_Field]:
            nonlocal count
            if not isinstance(source, dict) or depth > 8:
                raise ValueError("not a single bounded record")
            result = []
            for key, child in source.items():
                count += 1
                if count > _MAX_FIELDS or not isinstance(key, str) or len(key) > 64:
                    raise ValueError("field limit")
                if isinstance(child, list) and any(isinstance(item, (dict, list)) for item in child):
                    raise ValueError("multiple records")
                result.append(_Field(_label(key), fields(child, depth + 1) if isinstance(child, dict) else []))
            return result

        try:
            return fields(value)
        except ValueError:
            return []

    roots: list[_Field] = []
    stack: list[tuple[int, _Field]] = []
    count = 0
    for line in text.splitlines():
        # A list of records cannot be treated as one character or place.
        if re.match(r"^\s*[-*]\s+[^:：]+[:：]", line):
            indent = len(line) - len(line.lstrip())
            if not stack or indent <= stack[0][0]:
                return []
            continue
        match = _FIELD_LINE.fullmatch(line)
        if match is None:
            continue
        count += 1
        if count > _MAX_FIELDS:
            return []
        indent = len(match[1].expandtabs(4))
        node = _Field(_label(match[2]))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if len(stack) > 8:
            return []
        (stack[-1][1].children if stack else roots).append(node)
        stack.append((indent, node))
    return roots


def _structured_types(content: str) -> set[str]:
    nodes = _record_fields(content)
    # A named record may wrap its fields once or twice. Do not combine siblings
    # from a cast list, an itinerary, or a world overview.
    for _ in range(2):
        if len(nodes) == 1 and nodes[0].children and nodes[0].label not in _BASIC_INFO:
            nodes = nodes[0].children
        else:
            break
    labels = {node.label for node in nodes}
    if len(labels) != len(nodes):
        return set()
    identity = labels | {
        child.label for node in nodes if node.label in _BASIC_INFO
        for child in node.children
    }
    kinds = set()
    has_person_identity = bool(identity & {"姓名", "角色名", "name", "character_name", "full_name"}) or (
        bool(identity & {"年龄", "age"}) and bool(identity & {"身份", "职业", "occupation"})
    )
    if has_person_identity and labels & _APPEARANCE and labels & _PERSONALITY:
        kinds.add("character")
    if labels & {"地点名称", "地点名", "场所名称", "location_name"} and labels & {
        "地理位置", "所在区域", "位置", "环境", "出入口", "功能", "region", "geography", "environment",
    }:
        kinds.add("location")
    if labels & {"物品名称", "物品名", "道具名称", "装备名称", "item_name"} and labels & {
        "用途", "功能", "作用", "材质", "外观", "能力", "使用方式", "usage", "function", "material",
    }:
        kinds.add("item")
    if labels & {"规则名称", "法则名称", "rule_name"} and labels & {
        "约束", "限制", "规则内容", "生效条件", "触发条件", "适用范围", "后果", "规则类型", "规则定位",
        "effect", "conditions", "constraint",
    }:
        kinds.add("rule")
    return kinds


def classify_worldbook_entry(entry: LorebookEntry) -> EntryClassification:
    """Select a review destination from explicit markers or coherent fields."""
    raw = entry.raw_entry
    extensions = raw.get("extensions")
    namespace = extensions.get("novel-g") if isinstance(extensions, dict) else None
    metadata = [raw.get("card_type"), raw.get("category")]
    if isinstance(namespace, dict):
        metadata.append(namespace.get("card_type"))
    explicit = {
        _TYPE_LABELS[value.strip().lower()] for value in metadata
        if isinstance(value, str) and value.strip().lower() in _TYPE_LABELS
    }
    metadata_present = bool(explicit)
    name = entry.name
    title = _TITLE_MARKER.fullmatch(entry.name)
    if title and title[1].strip().lower() in _TYPE_LABELS:
        explicit.add(_TYPE_LABELS[title[1].strip().lower()])
        name = title[2].strip() or entry.name
    if len(explicit) > 1:
        return EntryClassification("lore", "conflicting_markers", entry.name)
    if explicit:
        return EntryClassification(
            next(iter(explicit)), "explicit_metadata" if metadata_present else "explicit_title", name,
        )
    inferred = _structured_types(entry.content)
    if len(inferred) == 1:
        return EntryClassification(next(iter(inferred)), "structured_fields", entry.name)
    return EntryClassification(
        "lore", "conflicting_markers" if inferred else "unclassified", entry.name,
    )
