"""Strict, side-effect-free normalization for embedded and standalone world books.

The adapter implements only the mappings verified in the interop specification.
Unknown and unsupported behavior fields are preserved for preview/export, but
none of them are executed.
"""

from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

from backend.services.interop.character_card_adapter import (
    MAX_ARRAY_ITEMS,
    MAX_NESTING_DEPTH,
    MAX_OBJECT_FIELDS,
    MAX_STRING_CHARS,
    MAX_TOTAL_VALUES,
    ParsedCharacterCard,
)


# Sized for 500 entries * 2,000 Chinese characters * 3 UTF-8 bytes, with
# headroom for JSON structure and escaping. This limit is deliberately separate
# from CharacterCardAdapter.MAX_JSON_BYTES (2 MiB).
MAX_WORLD_BOOK_JSON_BYTES = 5 * 1024 * 1024
MAX_WORLD_BOOK_ARRAY_ITEMS = 2_000
MAX_WORLD_BOOK_ENTRIES = MAX_WORLD_BOOK_ARRAY_ITEMS

# These defenses intentionally remain identical to slice 1.
MAX_WORLD_BOOK_STRING_CHARS = MAX_STRING_CHARS
MAX_WORLD_BOOK_OBJECT_FIELDS = MAX_OBJECT_FIELDS
MAX_WORLD_BOOK_NESTING_DEPTH = MAX_NESTING_DEPTH
MAX_WORLD_BOOK_TOTAL_VALUES = MAX_TOTAL_VALUES

_POSITION_MAP: dict[int, str] = {
    0: "before_char",
    1: "after_char",
    2: "an_top",
    3: "an_bottom",
    4: "at_depth",
    5: "before_example_messages",
    6: "after_example_messages",
    7: "outlet",
}
_STANDALONE_MAPPED_FIELDS = frozenset(
    {
        "uid",
        "comment",
        "content",
        "key",
        "keysecondary",
        "disable",
        "constant",
        "order",
        "position",
    }
)
_EMBEDDED_RECOGNIZED_FIELDS = frozenset(
    {
        "id",
        "uid",
        "keys",
        "content",
        "extensions",
        "enabled",
        "insertion_order",
        "case_sensitive",
        "name",
        "priority",
        "comment",
        "selective",
        "secondary_keys",
        "constant",
        "position",
        "use_regex",
    }
)
_EMBEDDED_UNSUPPORTED_FIELDS = {
    "extensions": "extension_behavior",
    "case_sensitive": "matching",
    "priority": "priority",
    "selective": "selective",
}

# Exact field names from SillyTavern's verified newWorldInfoEntryDefinition.
_STANDALONE_UNSUPPORTED_FIELDS = {
    "vectorized": "vectorized",
    "selective": "selective",
    "selectiveLogic": "selective",
    "probability": "probability",
    "useProbability": "probability",
    "excludeRecursion": "recursion_control",
    "preventRecursion": "recursion_control",
    "delayUntilRecursion": "recursion_control",
    "depth": "depth_role",
    "role": "depth_role",
    "outletName": "depth_role",
    "group": "grouping",
    "groupOverride": "grouping",
    "groupWeight": "grouping",
    "useGroupScoring": "grouping",
    "scanDepth": "matching_source",
    "caseSensitive": "matching_source",
    "matchWholeWords": "matching_source",
    "matchPersonaDescription": "matching_source",
    "matchCharacterDescription": "matching_source",
    "matchCharacterPersonality": "matching_source",
    "matchCharacterDepthPrompt": "matching_source",
    "matchScenario": "matching_source",
    "matchCreatorNotes": "matching_source",
    "characterFilterNames": "character_filter",
    "characterFilterTags": "character_filter",
    "characterFilterExclude": "character_filter",
    "triggers": "trigger_type",
    "automationId": "automation_id",
    "sticky": "timed_effects",
    "cooldown": "timed_effects",
    "delay": "timed_effects",
}
_DECORATOR_LINE_RE = re.compile(
    r"^@@@?[a-z][a-z0-9_]*(?:[ \t]+.*)?$"
)
_REGEX_FLAGS_RE = re.compile(r"^[A-Za-z]*$")
_NUMERIC_UID_KEY_RE = re.compile(r"^[0-9]+$")
_BSON_INT64_MIN = -(2**63)
_BSON_INT64_MAX = 2**63 - 1


class WorldBookValidationError(ValueError):
    """A stable validation failure safe to expose at an import boundary."""

    def __init__(
        self,
        code: str,
        path: str,
        message: str,
        *,
        limit_name: str | None = None,
        current_value: int | None = None,
        max_value: int | None = None,
    ):
        super().__init__(f"{path}: {message}")
        self.code = code
        self.path = path
        self.message = message
        self.limit_name = limit_name
        self.current_value = current_value
        self.max_value = max_value


@dataclass(frozen=True)
class WorldBookPreviewNotice:
    code: str
    path: str
    message: str


@dataclass(frozen=True)
class WorldBookUnsupportedFeature:
    field: str
    category: str
    enabled: Literal[False] = False


@dataclass(frozen=True)
class LorebookEntry:
    name: str
    content: str
    keys: tuple[str, ...]
    secondary_keys: tuple[str, ...]
    enabled: bool
    constant: bool
    insertion_order: int | float
    position: str | int | float
    use_regex: Literal[False]
    raw_entry: dict[str, Any]
    external_uid: int | float | str | None
    source_locator: str
    regex_fields: tuple[str, ...]
    unrecognized_fields: tuple[str, ...]
    unsupported_features: tuple[WorldBookUnsupportedFeature, ...]
    preview_notices: tuple[WorldBookPreviewNotice, ...]


@dataclass(frozen=True)
class ParsedWorldBook:
    source_format: Literal["v2", "v3", "worldbook_standalone"]
    source_container: Literal["json", "png"]
    source_kind: Literal["embedded", "standalone"]
    entries: tuple[LorebookEntry, ...]
    raw_book: dict[str, Any]
    detected_warnings: tuple[str, ...]
    unrecognized_top_level_fields: tuple[str, ...] = ()


class _DuplicateKeyError(ValueError):
    pass


def _object_without_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(key)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite number {value}")


def _fail(code: str, path: str, message: str) -> None:
    raise WorldBookValidationError(code, path, message)


def _fail_limit(
    code: str,
    path: str,
    label: str,
    *,
    current: int,
    maximum: int,
) -> None:
    raise WorldBookValidationError(
        code,
        path,
        f"{label}超限：当前值 {current}，上限 {maximum}",
        limit_name=label,
        current_value=current,
        max_value=maximum,
    )


def _require_object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail("invalid_type", path, "必须是对象")
    return value


def _optional_string(
    mapping: dict[str, Any],
    key: str,
    path: str,
    default: str,
) -> str:
    value = mapping.get(key, default)
    if not isinstance(value, str):
        _fail("invalid_type", f"{path}.{key}", "必须是字符串")
    return value


def _optional_boolean(
    mapping: dict[str, Any],
    key: str,
    path: str,
    default: bool,
) -> bool:
    value = mapping.get(key, default)
    if not isinstance(value, bool):
        _fail("invalid_type", f"{path}.{key}", "必须是 boolean")
    return value


def _optional_number(
    mapping: dict[str, Any],
    key: str,
    path: str,
    default: int | float,
) -> int | float:
    value = mapping.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail("invalid_type", f"{path}.{key}", "必须是数字")
    if isinstance(value, float) and not math.isfinite(value):
        _fail("invalid_number", f"{path}.{key}", "数字必须是有限值")
    return value


def _optional_string_array(
    mapping: dict[str, Any],
    key: str,
    path: str,
    default: list[str],
) -> list[str]:
    value = mapping.get(key, default)
    if not isinstance(value, list):
        _fail("invalid_type", f"{path}.{key}", "必须是字符串数组")
    for index, item in enumerate(value):
        if not isinstance(item, str):
            _fail("invalid_type", f"{path}.{key}[{index}]", "必须是字符串")
    return value


def _validate_resource_limits(value: Any, *, max_array_items: int) -> None:
    stack: list[tuple[Any, str, int]] = [(value, "$", 1)]
    total_values = 0
    while stack:
        current, path, depth = stack.pop()
        total_values += 1
        if total_values > MAX_WORLD_BOOK_TOTAL_VALUES:
            _fail_limit(
                "too_many_values",
                path,
                "JSON 值总数",
                current=total_values,
                maximum=MAX_WORLD_BOOK_TOTAL_VALUES,
            )
        if isinstance(current, str):
            if len(current) > MAX_WORLD_BOOK_STRING_CHARS:
                _fail_limit(
                    "string_too_long",
                    path,
                    "字符串字符数",
                    current=len(current),
                    maximum=MAX_WORLD_BOOK_STRING_CHARS,
                )
            continue
        if isinstance(current, list):
            if depth > MAX_WORLD_BOOK_NESTING_DEPTH:
                _fail_limit(
                    "nesting_too_deep",
                    path,
                    "JSON 嵌套深度",
                    current=depth,
                    maximum=MAX_WORLD_BOOK_NESTING_DEPTH,
                )
            if len(current) > max_array_items:
                _fail_limit(
                    "array_too_long",
                    path,
                    "数组项数",
                    current=len(current),
                    maximum=max_array_items,
                )
            stack.extend(
                (item, f"{path}[{index}]", depth + 1)
                for index, item in enumerate(current)
            )
            continue
        if isinstance(current, dict):
            if depth > MAX_WORLD_BOOK_NESTING_DEPTH:
                _fail_limit(
                    "nesting_too_deep",
                    path,
                    "JSON 嵌套深度",
                    current=depth,
                    maximum=MAX_WORLD_BOOK_NESTING_DEPTH,
                )
            object_limit = (
                MAX_WORLD_BOOK_ENTRIES
                if path == "$.entries"
                else MAX_WORLD_BOOK_OBJECT_FIELDS
            )
            if len(current) > object_limit:
                _fail_limit(
                    "too_many_entries"
                    if path == "$.entries"
                    else "object_too_wide",
                    path,
                    "世界书条目数"
                    if path == "$.entries"
                    else "对象字段数",
                    current=len(current),
                    maximum=object_limit,
                )
            for key, item in current.items():
                if not isinstance(key, str):
                    _fail("invalid_type", f"{path}.<key>", "JSON 对象键必须是字符串")
                if len(key) > MAX_WORLD_BOOK_STRING_CHARS:
                    _fail_limit(
                        "string_too_long",
                        f"{path}.<key>",
                        "字段名字符数",
                        current=len(key),
                        maximum=MAX_WORLD_BOOK_STRING_CHARS,
                    )
                stack.append((item, f"{path}.{key}", depth + 1))
            continue
        if isinstance(current, float) and not math.isfinite(current):
            _fail("invalid_number", path, "数字必须是有限值")
        if (
            isinstance(current, int)
            and not isinstance(current, bool)
            and not _BSON_INT64_MIN <= current <= _BSON_INT64_MAX
        ):
            _fail(
                "number_out_of_range",
                path,
                "整数超出可安全保存的 signed 64-bit 范围",
            )
        if current is not None and not isinstance(current, (bool, int, float)):
            _fail("invalid_type", path, "包含非 JSON 值")


def _decode_json_document(payload: bytes) -> Any:
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise WorldBookValidationError(
            "invalid_encoding", "$", "JSON 必须使用 UTF-8 编码"
        ) from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except _DuplicateKeyError as exc:
        raise WorldBookValidationError(
            "duplicate_key", "$", f"JSON 含重复字段：{exc}"
        ) from exc
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise WorldBookValidationError(
            "malformed_json", "$", "不是有效的 JSON"
        ) from exc
    _validate_resource_limits(
        value,
        max_array_items=MAX_WORLD_BOOK_ARRAY_ITEMS,
    )
    return value


def _looks_like_regex_literal(value: str) -> bool:
    if len(value) < 2 or not value.startswith("/"):
        return False
    for index in range(len(value) - 1, 0, -1):
        if value[index] != "/":
            continue
        backslashes = 0
        cursor = index - 1
        while cursor >= 0 and value[cursor] == "\\":
            backslashes += 1
            cursor -= 1
        if backslashes % 2:
            continue
        return _REGEX_FLAGS_RE.fullmatch(value[index + 1 :]) is not None
    return False


def _derived_name(
    raw: dict[str, Any],
    *,
    keys: list[str],
    source_locator: str,
    embedded: bool,
) -> str:
    candidates: list[Any] = []
    if embedded:
        candidates.append(raw.get("name"))
    candidates.append(raw.get("comment"))
    candidates.extend(keys)
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return f"未命名世界书条目（{source_locator}）"


def _regex_fields(keys: list[str], secondary_keys: list[str]) -> tuple[str, ...]:
    detected: list[str] = []
    for field, values in (("key", keys), ("keysecondary", secondary_keys)):
        for index, value in enumerate(values):
            if _looks_like_regex_literal(value):
                detected.append(f"{field}[{index}]")
    return tuple(detected)


def _standalone_entry(
    raw_value: Any,
    *,
    object_key: str,
) -> LorebookEntry:
    path = f"$.entries.{object_key}"
    if _NUMERIC_UID_KEY_RE.fullmatch(object_key) is None:
        _fail(
            "invalid_entry_uid_key",
            path,
            "entries 的对象键必须是数字 UID 的字符串形式",
        )
    raw = deepcopy(_require_object(raw_value, path))
    object_uid = int(object_key)
    if "uid" in raw:
        uid = _optional_number(raw, "uid", path, object_uid)
        if (
            isinstance(uid, float)
            and not uid.is_integer()
        ) or int(uid) != object_uid:
            _fail(
                "conflicting_entry_uid",
                f"{path}.uid",
                f"uid={uid} 与 entries 对象键 {object_key} 冲突，停止预览",
            )
        external_uid: int | float | str | None = deepcopy(uid)
    else:
        external_uid = object_key

    comment = _optional_string(raw, "comment", path, "")
    content = _optional_string(raw, "content", path, "")
    keys = _optional_string_array(raw, "key", path, [])
    secondary_keys = _optional_string_array(raw, "keysecondary", path, [])
    disabled = _optional_boolean(raw, "disable", path, False)
    constant = _optional_boolean(raw, "constant", path, False)
    insertion_order = _optional_number(raw, "order", path, 100)
    raw_position = _optional_number(raw, "position", path, 0)
    normalized_position: str | int | float = _POSITION_MAP.get(
        raw_position,
        raw_position,
    )

    unrecognized = tuple(
        key for key in raw if key not in _STANDALONE_MAPPED_FIELDS
    )
    unsupported = tuple(
        WorldBookUnsupportedFeature(
            field=key,
            category=_STANDALONE_UNSUPPORTED_FIELDS[key],
        )
        for key in unrecognized
        if key in _STANDALONE_UNSUPPORTED_FIELDS
    )
    regex_fields = _regex_fields(keys, secondary_keys)
    notices = [
        WorldBookPreviewNotice(
            code="unrecognized_field",
            path=f"{path}.{key}",
            message=f"未识别字段：{key}；原值已保留但首版不执行",
        )
        for key in unrecognized
    ]
    if normalized_position == raw_position and raw_position not in _POSITION_MAP:
        notices.append(
            WorldBookPreviewNotice(
                code="unknown_position",
                path=f"{path}.position",
                message=f"未知位置：{raw_position}；已保留原值且未猜测映射",
            )
        )
    if regex_fields:
        notices.append(
            WorldBookPreviewNotice(
                code="regex_present",
                path=path,
                message=(
                    "关键词中存在 /.../flags 正则；安全投影 use_regex=false，"
                    "原字符串保留"
                ),
            )
        )

    return LorebookEntry(
        name=_derived_name(
            {"comment": comment},
            keys=keys,
            source_locator=f"UID {object_key}",
            embedded=False,
        ),
        content=content,
        keys=tuple(deepcopy(keys)),
        secondary_keys=tuple(deepcopy(secondary_keys)),
        enabled=not disabled,
        constant=constant,
        insertion_order=insertion_order,
        position=normalized_position,
        use_regex=False,
        raw_entry=raw,
        external_uid=external_uid,
        source_locator=object_key,
        regex_fields=regex_fields,
        unrecognized_fields=unrecognized,
        unsupported_features=unsupported,
        preview_notices=tuple(notices),
    )


def _embedded_entry(
    raw_value: Any,
    *,
    index: int,
    source_format: Literal["v2", "v3"],
) -> LorebookEntry:
    path = f"$.entries[{index}]"
    raw = deepcopy(_require_object(raw_value, path))
    for required in ("keys", "content", "enabled", "insertion_order"):
        if required not in raw:
            _fail("missing_field", f"{path}.{required}", "缺少必填字段")
    keys = _optional_string_array(raw, "keys", path, [])
    secondary_keys = _optional_string_array(raw, "secondary_keys", path, [])
    content = _optional_string(raw, "content", path, "")
    enabled = _optional_boolean(raw, "enabled", path, True)
    constant = _optional_boolean(raw, "constant", path, False)
    insertion_order = _optional_number(raw, "insertion_order", path, 100)
    position_value = raw.get("position", "before_char")
    if not isinstance(position_value, str) or position_value not in {
        "before_char",
        "after_char",
    }:
        _fail(
            "invalid_value",
            f"{path}.position",
            "必须是 before_char 或 after_char",
        )
    if source_format == "v3":
        if "use_regex" not in raw:
            _fail("missing_field", f"{path}.use_regex", "V3 缺少必填字段")
        raw_use_regex = _optional_boolean(raw, "use_regex", path, False)
    else:
        raw_use_regex = False

    notices: list[WorldBookPreviewNotice] = []
    regex_fields: tuple[str, ...] = ()
    if raw_use_regex:
        notices.append(
            WorldBookPreviewNotice(
                code="regex_present",
                path=f"{path}.use_regex",
                message="条目级正则已识别；安全投影 use_regex=false，首版不执行",
            )
        )
    content_lines: list[str] = []
    for line_index, line in enumerate(content.splitlines()):
        if source_format == "v3" and _DECORATOR_LINE_RE.fullmatch(line):
            notices.append(
                WorldBookPreviewNotice(
                    code="decorator_present",
                    path=f"{path}.content#decorator[{line_index}]",
                    message="V3 decorator 已保留在 raw_entry，安全正文投影中不执行",
                )
            )
            continue
        content_lines.append(line)
    safe_content = "\n".join(content_lines).strip()

    unrecognized = tuple(
        key for key in raw if key not in _EMBEDDED_RECOGNIZED_FIELDS
    )
    unsupported_fields = tuple(
        key
        for key in raw
        if key in _EMBEDDED_UNSUPPORTED_FIELDS
        and (
            key != "extensions"
            or bool(raw.get("extensions"))
        )
    )
    unsupported = tuple(
        WorldBookUnsupportedFeature(
            field=key,
            category=_EMBEDDED_UNSUPPORTED_FIELDS[key],
        )
        for key in unsupported_fields
    )
    notices.extend(
        WorldBookPreviewNotice(
            code="unrecognized_field",
            path=f"{path}.{key}",
            message=f"未识别字段：{key}；原值已保留但首版不执行",
        )
        for key in unrecognized
    )
    notices.extend(
        WorldBookPreviewNotice(
            code="unsupported_feature",
            path=f"{path}.{key}",
            message=f"已知但首版不执行的世界书行为字段：{key}",
        )
        for key in unsupported_fields
    )
    external_uid = deepcopy(raw.get("id", raw.get("uid")))
    return LorebookEntry(
        name=_derived_name(
            raw,
            keys=keys,
            source_locator=f"条目 {index + 1}",
            embedded=True,
        ),
        content=safe_content,
        keys=tuple(deepcopy(keys)),
        secondary_keys=tuple(deepcopy(secondary_keys)),
        enabled=enabled,
        constant=constant,
        insertion_order=insertion_order,
        position=position_value,
        use_regex=False,
        raw_entry=raw,
        external_uid=external_uid,
        source_locator=str(index),
        regex_fields=regex_fields,
        unrecognized_fields=unrecognized,
        unsupported_features=unsupported,
        preview_notices=tuple(notices),
    )


class WorldBookAdapter:
    """Public normalization seam for both verified world-book sources."""

    @classmethod
    def parse_json(
        cls,
        payload: bytes,
        *,
        declared_mime: str,
        filename: str | None = None,
    ) -> ParsedWorldBook:
        if not isinstance(payload, bytes):
            _fail("invalid_payload", "$", "世界书 JSON 文件内容必须是 bytes")
        if len(payload) > MAX_WORLD_BOOK_JSON_BYTES:
            _fail_limit(
                "file_too_large",
                "$",
                "独立世界书 JSON 文件字节数",
                current=len(payload),
                maximum=MAX_WORLD_BOOK_JSON_BYTES,
            )
        mime = declared_mime.partition(";")[0].strip().lower()
        if mime != "application/json":
            _fail("mime_mismatch", "$", "声明的 MIME 必须是 application/json")
        if filename is not None and not filename.lower().endswith(".json"):
            _fail(
                "extension_mismatch",
                "$",
                "独立世界书文件名必须以 .json 结尾",
            )
        value = _decode_json_document(payload)
        raw_book = deepcopy(_require_object(value, "$"))
        if "entries" not in raw_book:
            _fail(
                "not_standalone_worldbook",
                "$.entries",
                "独立世界书顶层必须包含 entries 对象",
            )
        if not isinstance(raw_book["entries"], dict):
            _fail(
                "invalid_type",
                "$.entries",
                "独立世界书 entries 必须是对象",
            )
        entries = tuple(
            _standalone_entry(raw_entry, object_key=object_key)
            for object_key, raw_entry in raw_book["entries"].items()
        )
        top_level_unknown = tuple(
            key for key in raw_book if key != "entries"
        )
        warnings = tuple(
            f"未识别顶层字段：{key}；原值已保留但首版不执行"
            for key in top_level_unknown
        )
        return ParsedWorldBook(
            source_format="worldbook_standalone",
            source_container="json",
            source_kind="standalone",
            entries=entries,
            raw_book=raw_book,
            detected_warnings=warnings,
            unrecognized_top_level_fields=top_level_unknown,
        )

    @classmethod
    def parse_embedded(
        cls,
        book: Any,
        *,
        source_format: Literal["v2", "v3"],
        source_container: Literal["json", "png"] = "json",
    ) -> ParsedWorldBook:
        if source_format not in {"v2", "v3"}:
            _fail(
                "unsupported_embedded_format",
                "$",
                "内嵌 character_book 只接受 V2 或 V3",
            )
        if source_container not in {"json", "png"}:
            _fail("unsupported_container", "$", "只接受 JSON 或 PNG 容器")
        _validate_resource_limits(book, max_array_items=MAX_ARRAY_ITEMS)
        raw_book = deepcopy(_require_object(book, "$"))
        entries_value = raw_book.get("entries")
        if not isinstance(entries_value, list):
            _fail("invalid_type", "$.entries", "内嵌 character_book.entries 必须是数组")
        entries = tuple(
            _embedded_entry(
                raw_entry,
                index=index,
                source_format=source_format,
            )
            for index, raw_entry in enumerate(entries_value)
        )
        top_level_unknown = tuple(
            key
            for key in raw_book
            if key
            not in {
                "name",
                "description",
                "scan_depth",
                "token_budget",
                "recursive_scanning",
                "extensions",
                "entries",
            }
        )
        warnings: list[str] = [
            f"未识别 character_book 顶层字段：{key}；原值已保留但首版不执行"
            for key in top_level_unknown
        ]
        if raw_book.get("recursive_scanning") is True:
            warnings.append("递归扫描已保留但首版不执行")
        if raw_book.get("extensions"):
            warnings.append("character_book extensions 已保留但首版不执行")
        return ParsedWorldBook(
            source_format=source_format,
            source_container=source_container,
            source_kind="embedded",
            entries=entries,
            raw_book=raw_book,
            detected_warnings=tuple(warnings),
            unrecognized_top_level_fields=top_level_unknown,
        )

    @classmethod
    def from_character_card(
        cls,
        parsed: ParsedCharacterCard,
    ) -> ParsedWorldBook | None:
        if not isinstance(parsed, ParsedCharacterCard):
            raise TypeError("parsed must be a ParsedCharacterCard")
        if parsed.source_format not in {"v2", "v3"}:
            return None
        raw_data = parsed.raw_card.get("data")
        if not isinstance(raw_data, dict):
            return None
        raw_book = raw_data.get("character_book")
        if raw_book is None:
            return None
        return cls.parse_embedded(
            raw_book,
            source_format=parsed.source_format,
            source_container=parsed.source_container,
        )
