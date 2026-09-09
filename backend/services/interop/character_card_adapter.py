"""Strict, side-effect-free Character Card JSON and PNG parsing.

This module deliberately stops at a bounded preview representation.  It does
not write to MongoDB, fetch assets, execute regular expressions/decorators, or
send any imported value to an LLM. PNG image data is never decoded or stored.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
import zlib
from copy import deepcopy
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from urllib.parse import urlsplit


MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_STRING_CHARS = 200_000
MAX_ARRAY_ITEMS = 1_000
MAX_OBJECT_FIELDS = 2_000
MAX_NESTING_DEPTH = 32
MAX_TOTAL_VALUES = 20_000
MAX_PNG_BYTES = 10 * 1024 * 1024
# PNG 规范对 chunk 数量没有上限，图像数据本身就是切成多个 IDAT chunk 存的，
# 切多细取决于编码器。因此这个值【不能】按“正常图片没几个 chunk”来定：
# 基于 libpng 的工具按压缩缓冲区大小切 IDAT，一张压缩后 1 MB 的立绘就可能
# 超过一百个 IDAT，而这与文件是否可疑毫无关系。
#
# 真正的 DoS 防线是上面的 MAX_PNG_BYTES，它在 chunk 循环【之前】检查：
# 每个 chunk 至少 12 字节开销，10 MiB 文件最多约 87 万个 chunk，而循环体
# 只做几次切片和整数解析，构不成 DoS。本值只用于挡住病态输入，
# 取值必须高到不可能误杀合法文件——10 MiB 即使按 1 KB/IDAT 这种极端细的
# 切法也只有约 10,240 个 chunk。
MAX_PNG_CHUNKS = 16_384
MAX_PNG_CHUNK_JSON_BYTES = MAX_JSON_BYTES
MAX_PNG_DECODED_BYTES = 2 * MAX_PNG_CHUNK_JSON_BYTES
_MAX_PNG_CARD_ENCODED_BYTES = 4 * (
    (MAX_PNG_CHUNK_JSON_BYTES + 2) // 3
)

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_CARD_KEYWORDS = frozenset({"chara", "ccv3"})
_PNG_GENERATION_METADATA_KEYS = frozenset({"software", "source", "comment"})
_GENERATION_PARAMETER_KEYS = frozenset(
    {
        "prompt",
        "negative_prompt",
        "seed",
        "sampler",
        "steps",
        "scale",
        "cfg_scale",
        "width",
        "height",
    }
)
_GENERATION_PARAMETER_SIGNAL_KEYS = _GENERATION_PARAMETER_KEYS - {
    "width",
    "height",
}

_V1_REQUIRED_STRINGS = (
    "name",
    "description",
    "personality",
    "scenario",
    "first_mes",
    "mes_example",
)
_CARD_REQUIRED_STRINGS = _V1_REQUIRED_STRINGS + (
    "creator_notes",
    "system_prompt",
    "post_history_instructions",
    "creator",
    "character_version",
)
_SAFE_CHARACTER_EXTENSION_KEYS = frozenset({"talkativeness"})
_V3_DATA_FIELDS = frozenset(
    {
        *_V1_REQUIRED_STRINGS,
        "creator_notes",
        "alternate_greetings",
        "tags",
        "creator",
        "character_version",
        "extensions",
        "character_book",
        "assets",
        "nickname",
        "creator_notes_multilingual",
        "source",
        "group_only_greetings",
        "creation_date",
        "modification_date",
    }
)
_KNOWN_V3_DECORATORS = frozenset(
    {
        "activate_only_after",
        "activate_only_every",
        "keep_activate_after_match",
        "dont_activate_after_match",
        "depth",
        "instruct_depth",
        "reverse_depth",
        "reverse_instruct_depth",
        "role",
        "scan_depth",
        "instruct_scan_depth",
        "is_greeting",
        "position",
        "ignore_on_max_context",
        "additional_keys",
        "exclude_keys",
        "is_user_icon",
        "dont_activate",
        "activate",
        "disable_ui_prompt",
    }
)
_DECORATOR_LINE_RE = re.compile(
    r"^(?P<marker>@@@?)(?P<name>[a-z][a-z0-9_]*)(?:[ \t]+(?P<value>.*))?$"
)
_ASSET_EXTENSION_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class CharacterCardValidationError(ValueError):
    """A stable validation failure safe to show at an import boundary."""

    def __init__(
        self,
        code: str,
        path: str,
        message: str,
        *,
        missing_metadata_kind: (
            Literal[
                "ai_generation_metadata",
                "no_text_chunks",
                "other_text_chunks",
            ]
            | None
        ) = None,
        text_keywords: tuple[str, ...] = (),
    ):
        super().__init__(f"{path}: {message}")
        self.code = code
        self.path = path
        self.message = message
        self.missing_metadata_kind = missing_metadata_kind
        self.text_keywords = text_keywords


@dataclass(frozen=True)
class CharacterCardRiskField:
    """Imported content that is preserved for preview but remains disabled."""

    path: str
    kind: str
    value: Any
    enabled: Literal[False] = False


@dataclass(frozen=True)
class CharacterCardDecorator:
    """A V3 lorebook decorator recognized for inert preview."""

    path: str
    name: str
    value: str
    raw: str
    fallback: bool
    known: bool
    enabled: Literal[False] = False


@dataclass(frozen=True)
class CharacterCardAsset:
    """V3 asset metadata; the URI is never dereferenced by this adapter."""

    path: str
    type: str
    uri: str
    name: str
    ext: str
    retrieval_enabled: Literal[False] = False


@dataclass(frozen=True)
class ParsedCharacterCard:
    """Bounded Character Card preview with untrusted parts kept separate."""

    source_format: Literal["v1", "v2", "v3"]
    source_container: Literal["json", "png"]
    spec_version: str
    fields: dict[str, Any]
    raw_card: dict[str, Any]
    compatibility_warnings: tuple[str, ...]
    prompt_risk_fields: tuple[CharacterCardRiskField, ...]
    decorators: tuple[CharacterCardDecorator, ...]
    assets: tuple[CharacterCardAsset, ...]
    selected_png_chunk: Literal["chara", "ccv3"] | None = None
    png_chunk_classification: (
        Literal["v1", "v2", "v3", "pseudo_v3"] | None
    ) = None
    png_preview_label: str | None = None
    image_data_discarded: bool = False

    @property
    def risk_fields(self) -> tuple[CharacterCardRiskField, ...]:
        """Compatibility alias for callers using the shorter preview label."""

        return self.prompt_risk_fields

    @property
    def detected_warnings(self) -> tuple[str, ...]:
        """Proposal-layer name for compatibility warnings."""

        return self.compatibility_warnings


class _DuplicateKeyError(ValueError):
    pass


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(key)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite number {value}")


def _fail(code: str, path: str, message: str) -> None:
    raise CharacterCardValidationError(code, path, message)


def _decode_json_document(payload: bytes, *, path: str) -> Any:
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CharacterCardValidationError(
            "invalid_encoding", path, "JSON 必须使用 UTF-8 编码"
        ) from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except _DuplicateKeyError as exc:
        raise CharacterCardValidationError(
            "duplicate_key", path, f"JSON 含重复字段：{exc}"
        ) from exc
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise CharacterCardValidationError(
            "malformed_json", path, "不是有效的 JSON"
        ) from exc
    _validate_resource_limits(value)
    return value


def _validate_resource_limits(value: Any) -> None:
    stack: list[tuple[Any, str, int]] = [(value, "$", 1)]
    total_values = 0
    while stack:
        current, path, depth = stack.pop()
        total_values += 1
        if total_values > MAX_TOTAL_VALUES:
            _fail("too_many_values", path, f"JSON 值总数不得超过 {MAX_TOTAL_VALUES}")
        if isinstance(current, str):
            if len(current) > MAX_STRING_CHARS:
                _fail(
                    "string_too_long",
                    path,
                    f"字符串长度不得超过 {MAX_STRING_CHARS} 个字符",
                )
            continue
        if isinstance(current, list):
            if depth > MAX_NESTING_DEPTH:
                _fail(
                    "nesting_too_deep",
                    path,
                    f"JSON 嵌套深度不得超过 {MAX_NESTING_DEPTH}",
                )
            if len(current) > MAX_ARRAY_ITEMS:
                _fail(
                    "array_too_long",
                    path,
                    f"数组长度不得超过 {MAX_ARRAY_ITEMS}",
                )
            stack.extend(
                (item, f"{path}[{index}]", depth + 1)
                for index, item in enumerate(current)
            )
            continue
        if isinstance(current, dict):
            if depth > MAX_NESTING_DEPTH:
                _fail(
                    "nesting_too_deep",
                    path,
                    f"JSON 嵌套深度不得超过 {MAX_NESTING_DEPTH}",
                )
            if len(current) > MAX_OBJECT_FIELDS:
                _fail(
                    "object_too_wide",
                    path,
                    f"对象字段数不得超过 {MAX_OBJECT_FIELDS}",
                )
            for key, item in current.items():
                if not isinstance(key, str):
                    _fail("invalid_type", f"{path}.<key>", "JSON 对象键必须是字符串")
                if len(key) > MAX_STRING_CHARS:
                    _fail(
                        "string_too_long",
                        f"{path}.<key>",
                        f"字段名长度不得超过 {MAX_STRING_CHARS} 个字符",
                    )
                stack.append((item, f"{path}.{key}", depth + 1))
            continue
        if isinstance(current, float) and not math.isfinite(current):
            _fail("invalid_number", path, "数字必须是有限值")
        if current is not None and not isinstance(current, (bool, int, float)):
            _fail("invalid_type", path, "包含非 JSON 值")


def _require_object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail("invalid_type", path, "必须是对象")
    return value


def _require_string(mapping: dict[str, Any], key: str, path: str) -> str:
    if key not in mapping:
        _fail("missing_field", f"{path}.{key}", "缺少必填字段")
    value = mapping[key]
    if not isinstance(value, str):
        _fail("invalid_type", f"{path}.{key}", "必须是字符串")
    return value


def _require_array(mapping: dict[str, Any], key: str, path: str) -> list[Any]:
    if key not in mapping:
        _fail("missing_field", f"{path}.{key}", "缺少必填字段")
    value = mapping[key]
    if not isinstance(value, list):
        _fail("invalid_type", f"{path}.{key}", "必须是数组")
    return value


def _require_boolean(mapping: dict[str, Any], key: str, path: str) -> bool:
    if key not in mapping:
        _fail("missing_field", f"{path}.{key}", "缺少必填字段")
    value = mapping[key]
    if not isinstance(value, bool):
        _fail("invalid_type", f"{path}.{key}", "必须是 boolean")
    return value


def _require_number(mapping: dict[str, Any], key: str, path: str) -> int | float:
    if key not in mapping:
        _fail("missing_field", f"{path}.{key}", "缺少必填字段")
    value = mapping[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail("invalid_type", f"{path}.{key}", "必须是数字")
    return value


def _validate_optional_type(
    mapping: dict[str, Any],
    key: str,
    expected: type | tuple[type, ...],
    path: str,
    label: str,
) -> None:
    if key not in mapping:
        return
    value = mapping[key]
    if isinstance(value, bool) and expected in ((int, float), (int, float, str)):
        _fail("invalid_type", f"{path}.{key}", f"必须是{label}")
    if not isinstance(value, expected):
        _fail("invalid_type", f"{path}.{key}", f"必须是{label}")


def _validate_string_array(value: Any, path: str) -> list[str]:
    if not isinstance(value, list):
        _fail("invalid_type", path, "必须是字符串数组")
    for index, item in enumerate(value):
        if not isinstance(item, str):
            _fail("invalid_type", f"{path}[{index}]", "必须是字符串")
    return value


def _validate_extensions(mapping: dict[str, Any], path: str) -> dict[str, Any]:
    if "extensions" not in mapping:
        _fail("missing_field", f"{path}.extensions", "缺少必填字段")
    return _require_object(mapping["extensions"], f"{path}.extensions")


def _validate_lorebook(value: Any, path: str, *, v3: bool) -> tuple[str, ...]:
    book = _require_object(value, path)
    # SillyTavern's World Info converter omits the book-level extensions object.
    # Only absence is compatible; present values and entry extensions stay strict.
    if "extensions" in book:
        _validate_extensions(book, path)
    entries = _require_array(book, "entries", path)
    _validate_optional_type(book, "name", str, path, "字符串")
    _validate_optional_type(book, "description", str, path, "字符串")
    _validate_optional_type(book, "scan_depth", (int, float), path, "数字")
    _validate_optional_type(book, "token_budget", (int, float), path, "数字")
    _validate_optional_type(book, "recursive_scanning", bool, path, "boolean")
    for index, raw_entry in enumerate(entries):
        entry_path = f"{path}.entries[{index}]"
        entry = _require_object(raw_entry, entry_path)
        _validate_string_array(
            entry.get("keys") if "keys" in entry else _missing(entry_path, "keys"),
            f"{entry_path}.keys",
        )
        _require_string(entry, "content", entry_path)
        _validate_extensions(entry, entry_path)
        _require_boolean(entry, "enabled", entry_path)
        _require_number(entry, "insertion_order", entry_path)
        if v3:
            _require_boolean(entry, "use_regex", entry_path)
        _validate_optional_type(entry, "case_sensitive", bool, entry_path, "boolean")
        _validate_optional_type(entry, "name", str, entry_path, "字符串")
        _validate_optional_type(entry, "priority", (int, float), entry_path, "数字")
        id_types: type | tuple[type, ...] = (int, float, str) if v3 else (int, float)
        _validate_optional_type(entry, "id", id_types, entry_path, "数字或字符串" if v3 else "数字")
        _validate_optional_type(entry, "comment", str, entry_path, "字符串")
        _validate_optional_type(entry, "selective", bool, entry_path, "boolean")
        if "secondary_keys" in entry:
            _validate_string_array(
                entry["secondary_keys"], f"{entry_path}.secondary_keys"
            )
        _validate_optional_type(entry, "constant", bool, entry_path, "boolean")
        if "position" in entry:
            position = entry["position"]
            if not isinstance(position, str) or position not in {
                "before_char",
                "after_char",
            }:
                _fail(
                    "invalid_value",
                    f"{entry_path}.position",
                    "必须是 before_char 或 after_char",
                )
    return (
        ("内嵌世界书缺少 extensions，已按空对象兼容读取；原始数据保持不变",)
        if "extensions" not in book
        else ()
    )


def _project_lorebook(book: dict[str, Any], *, v3: bool) -> dict[str, Any]:
    book_fields = {
        "name",
        "description",
        "scan_depth",
        "token_budget",
        "recursive_scanning",
        "extensions",
        "entries",
    }
    entry_fields = {
        "keys",
        "content",
        "extensions",
        "enabled",
        "insertion_order",
        "case_sensitive",
        "name",
        "priority",
        "id",
        "comment",
        "selective",
        "secondary_keys",
        "constant",
        "position",
    }
    if v3:
        entry_fields.add("use_regex")
    projected = {
        key: deepcopy(value) for key, value in book.items() if key in book_fields
    }
    projected.setdefault("extensions", {})
    projected["entries"] = [
        {
            key: deepcopy(value)
            for key, value in entry.items()
            if key in entry_fields
        }
        for entry in book["entries"]
    ]
    return projected


def _missing(path: str, key: str) -> None:
    _fail("missing_field", f"{path}.{key}", "缺少必填字段")


def _isolated_extensions(
    extensions: dict[str, Any],
    *,
    path: str,
    risk_fields: list[CharacterCardRiskField],
    safe_keys: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in extensions.items():
        if key in safe_keys:
            safe[key] = deepcopy(value)
            continue
        if key in {"depth_prompt", "regex_scripts"}:
            kind = key
        elif key in {"fav", "world"}:
            kind = "local_extension"
        else:
            kind = "unknown_extension"
        risk_fields.append(
            CharacterCardRiskField(
                path=f"{path}.{key}",
                kind=kind,
                value=deepcopy(value),
            )
        )
    return safe


def _parse_v2(
    card: dict[str, Any], source_container: Literal["json", "png"]
) -> ParsedCharacterCard:
    version = _require_string(card, "spec_version", "$")
    if version != "2.0":
        _fail("unsupported_version", "$.spec_version", "V2 规范版本必须是 2.0")
    data = _require_object(
        card.get("data") if "data" in card else _missing("$", "data"), "$.data"
    )
    for key in _CARD_REQUIRED_STRINGS:
        _require_string(data, key, "$.data")
    _validate_string_array(
        data.get("alternate_greetings")
        if "alternate_greetings" in data
        else _missing("$.data", "alternate_greetings"),
        "$.data.alternate_greetings",
    )
    _validate_string_array(
        data.get("tags") if "tags" in data else _missing("$.data", "tags"),
        "$.data.tags",
    )
    extensions = _validate_extensions(data, "$.data")
    warnings: tuple[str, ...] = ()
    if "character_book" in data:
        warnings = _validate_lorebook(
            data["character_book"], "$.data.character_book", v3=False
        )

    risk_fields = [
        CharacterCardRiskField(
            path="$.data.system_prompt",
            kind="system_prompt",
            value=deepcopy(data["system_prompt"]),
        ),
        CharacterCardRiskField(
            path="$.data.post_history_instructions",
            kind="post_history_instructions",
            value=deepcopy(data["post_history_instructions"]),
        ),
    ]
    safe_extensions = _isolated_extensions(
        extensions,
        path="$.data.extensions",
        risk_fields=risk_fields,
        safe_keys=_SAFE_CHARACTER_EXTENSION_KEYS,
    )
    safe_fields = {
        key: deepcopy(value)
        for key, value in data.items()
        if key
        in {
            *_V1_REQUIRED_STRINGS,
            "creator_notes",
            "alternate_greetings",
            "tags",
            "creator",
            "character_version",
            "extensions",
            "character_book",
        }
    }
    safe_fields["extensions"] = safe_extensions
    if "character_book" in safe_fields:
        book = _project_lorebook(data["character_book"], v3=False)
        safe_fields["character_book"] = book
        book["extensions"] = _isolated_extensions(
            book["extensions"],
            path="$.data.character_book.extensions",
            risk_fields=risk_fields,
        )
        if book.get("recursive_scanning") is True:
            risk_fields.append(
                CharacterCardRiskField(
                    path="$.data.character_book.recursive_scanning",
                    kind="recursive_scanning",
                    value=True,
                )
            )
            book["recursive_scanning"] = False
        for index, entry in enumerate(book["entries"]):
            entry["extensions"] = _isolated_extensions(
                entry["extensions"],
                path=f"$.data.character_book.entries[{index}].extensions",
                risk_fields=risk_fields,
            )
    return ParsedCharacterCard(
        source_format="v2",
        source_container=source_container,
        spec_version=version,
        fields=safe_fields,
        raw_card=deepcopy(card),
        compatibility_warnings=warnings,
        prompt_risk_fields=tuple(risk_fields),
        decorators=(),
        assets=(),
    )


def _validate_v3_assets(
    value: Any,
    path: str,
) -> tuple[list[dict[str, str]], tuple[CharacterCardAsset, ...]]:
    assets = _require_array({"assets": value}, "assets", path.rsplit(".", 1)[0])
    normalized: list[dict[str, str]] = []
    previews: list[CharacterCardAsset] = []
    for index, raw_asset in enumerate(assets):
        asset_path = f"{path}[{index}]"
        asset = _require_object(raw_asset, asset_path)
        asset_type = _require_string(asset, "type", asset_path)
        uri = _require_string(asset, "uri", asset_path)
        name = _require_string(asset, "name", asset_path)
        ext = _require_string(asset, "ext", asset_path)
        if not _ASSET_EXTENSION_RE.fullmatch(ext):
            _fail(
                "invalid_value",
                f"{asset_path}.ext",
                "必须是不带点号的小写文件扩展名",
            )
        _validate_asset_uri(uri, f"{asset_path}.uri")
        normalized_asset = {
            "type": asset_type,
            "uri": uri,
            "name": name,
            "ext": ext,
        }
        normalized.append(normalized_asset)
        previews.append(
            CharacterCardAsset(path=asset_path, **deepcopy(normalized_asset))
        )

    icon_assets = [asset for asset in normalized if asset["type"] == "icon"]
    if len(icon_assets) > 1:
        main_count = sum(asset["name"] == "main" for asset in icon_assets)
        if main_count != 1:
            _fail(
                "invalid_assets",
                path,
                "多个 icon 素材时必须且只能有一个 name=main",
            )
    background_assets = [
        asset for asset in normalized if asset["type"] == "background"
    ]
    if sum(asset["name"] == "main" for asset in background_assets) > 1:
        _fail(
            "invalid_assets",
            path,
            "background 素材最多只能有一个 name=main",
        )
    return normalized, tuple(previews)


def _validate_asset_uri(uri: str, path: str) -> None:
    if any(ord(char) < 32 for char in uri):
        _fail("invalid_value", path, "素材 URI 不得包含控制字符")
    if uri == "ccdefault:":
        return
    if uri.startswith("embeded://"):
        if not uri.removeprefix("embeded://"):
            _fail("invalid_value", path, "embeded URI 必须包含素材路径")
        return
    if uri.startswith("data:"):
        header, separator, encoded = uri.partition(",")
        if not separator or not header.lower().endswith(";base64") or not encoded:
            _fail("invalid_value", path, "data URI 必须使用 base64 编码")
        return
    parsed = urlsplit(uri)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return
    _fail(
        "invalid_value",
        path,
        "只接受 http、https、base64 data、embeded 或 ccdefault 素材 URI",
    )


def _extract_v3_decorators(
    content: str,
    *,
    path: str,
    risk_fields: list[CharacterCardRiskField],
) -> tuple[str, list[CharacterCardDecorator]]:
    content_lines: list[str] = []
    previews: list[CharacterCardDecorator] = []
    for line_index, line in enumerate(content.splitlines()):
        match = _DECORATOR_LINE_RE.fullmatch(line)
        if match is None:
            content_lines.append(line)
            continue
        marker = match.group("marker")
        name = match.group("name")
        value = match.group("value") or ""
        decorator_path = f"{path}#decorator[{line_index}]"
        previews.append(
            CharacterCardDecorator(
                path=decorator_path,
                name=name,
                value=value,
                raw=line,
                fallback=marker == "@@@",
                known=name in _KNOWN_V3_DECORATORS,
            )
        )
        risk_fields.append(
            CharacterCardRiskField(
                path=decorator_path,
                kind="decorator",
                value=line,
            )
        )
    return "\n".join(content_lines).strip(), previews


def _parse_v3(
    card: dict[str, Any], source_container: Literal["json", "png"]
) -> ParsedCharacterCard:
    version = _require_string(card, "spec_version", "$")
    parsed_version = _parse_version(version, "$.spec_version")
    if parsed_version < Decimal("3.0"):
        _fail("unsupported_version", "$.spec_version", "V3 规范版本不得低于 3.0")
    if parsed_version == Decimal("3.0") and version != "3.0":
        _fail("invalid_version", "$.spec_version", "当前 V3 规范版本必须写作 3.0")
    warnings = (
        ("来自更新规范，部分功能可能未识别",)
        if parsed_version > Decimal("3.0")
        else ()
    )
    data = _require_object(
        card.get("data") if "data" in card else _missing("$", "data"), "$.data"
    )
    for key in _CARD_REQUIRED_STRINGS:
        _require_string(data, key, "$.data")
    for key in ("alternate_greetings", "tags", "group_only_greetings"):
        _validate_string_array(
            data.get(key) if key in data else _missing("$.data", key),
            f"$.data.{key}",
        )
    extensions = _validate_extensions(data, "$.data")
    if "character_book" in data:
        warnings += _validate_lorebook(
            data["character_book"], "$.data.character_book", v3=True
        )
    _validate_optional_type(data, "nickname", str, "$.data", "字符串")
    if "creator_notes_multilingual" in data:
        notes = _require_object(
            data["creator_notes_multilingual"],
            "$.data.creator_notes_multilingual",
        )
        for language_code, note in notes.items():
            language_path = f"$.data.creator_notes_multilingual.{language_code}"
            if not re.fullmatch(r"[a-z]{2}", language_code):
                _fail(
                    "invalid_value",
                    language_path,
                    "语言码必须是两位小写 ISO 639-1 代码",
                )
            if not isinstance(note, str):
                _fail("invalid_type", language_path, "必须是字符串")
    if "source" in data:
        _validate_string_array(data["source"], "$.data.source")
    _validate_optional_type(data, "creation_date", (int, float), "$.data", "数字")
    _validate_optional_type(
        data, "modification_date", (int, float), "$.data", "数字"
    )
    normalized_assets: list[dict[str, str]] | None = None
    asset_previews: tuple[CharacterCardAsset, ...] = ()
    if "assets" in data:
        normalized_assets, asset_previews = _validate_v3_assets(
            data["assets"], "$.data.assets"
        )

    risk_fields = [
        CharacterCardRiskField(
            path="$.data.system_prompt",
            kind="system_prompt",
            value=deepcopy(data["system_prompt"]),
        ),
        CharacterCardRiskField(
            path="$.data.post_history_instructions",
            kind="post_history_instructions",
            value=deepcopy(data["post_history_instructions"]),
        ),
    ]
    safe_fields = {
        key: deepcopy(value)
        for key, value in data.items()
        if key in _V3_DATA_FIELDS
    }
    safe_fields["extensions"] = _isolated_extensions(
        extensions,
        path="$.data.extensions",
        risk_fields=risk_fields,
        safe_keys=_SAFE_CHARACTER_EXTENSION_KEYS,
    )
    if normalized_assets is not None:
        safe_fields["assets"] = normalized_assets

    decorator_previews: list[CharacterCardDecorator] = []
    if "character_book" in safe_fields:
        book = _project_lorebook(data["character_book"], v3=True)
        safe_fields["character_book"] = book
        book["extensions"] = _isolated_extensions(
            book["extensions"],
            path="$.data.character_book.extensions",
            risk_fields=risk_fields,
        )
        if book.get("recursive_scanning") is True:
            risk_fields.append(
                CharacterCardRiskField(
                    path="$.data.character_book.recursive_scanning",
                    kind="recursive_scanning",
                    value=True,
                )
            )
            book["recursive_scanning"] = False
        for index, entry in enumerate(book["entries"]):
            entry_path = f"$.data.character_book.entries[{index}]"
            entry["extensions"] = _isolated_extensions(
                entry["extensions"],
                path=f"{entry_path}.extensions",
                risk_fields=risk_fields,
            )
            if entry["use_regex"] is True:
                risk_fields.append(
                    CharacterCardRiskField(
                        path=f"{entry_path}.use_regex",
                        kind="use_regex",
                        value=True,
                    )
                )
                entry["use_regex"] = False
            safe_content, entry_decorators = _extract_v3_decorators(
                entry["content"],
                path=f"{entry_path}.content",
                risk_fields=risk_fields,
            )
            entry["content"] = safe_content
            decorator_previews.extend(entry_decorators)

    return ParsedCharacterCard(
        source_format="v3",
        source_container=source_container,
        spec_version=version,
        fields=safe_fields,
        raw_card=deepcopy(card),
        compatibility_warnings=warnings,
        prompt_risk_fields=tuple(risk_fields),
        decorators=tuple(decorator_previews),
        assets=asset_previews,
    )


def _parse_version(value: str, path: str) -> Decimal:
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
        _fail("invalid_version", path, "必须是十进制版本字符串")
    try:
        return Decimal(value)
    except InvalidOperation:
        _fail("invalid_version", path, "不是有效版本")


@dataclass(frozen=True)
class _PNGCardTextChunk:
    encoded: bytes
    chunk_type: Literal["tEXt", "zTXt", "iTXt"]


def _png_text_path(chunk_type: str, keyword: str) -> str:
    return f"$.png.{chunk_type}.{keyword}"


def _bounded_decompress_png_text(
    compressed: bytes,
    *,
    path: str,
) -> bytes:
    inflater = zlib.decompressobj()
    try:
        decompressed = inflater.decompress(
            compressed,
            _MAX_PNG_CARD_ENCODED_BYTES + 1,
        )
    except zlib.error as exc:
        raise CharacterCardValidationError(
            "malformed_png_text",
            path,
            "PNG 压缩文本数据无效",
        ) from exc
    if (
        len(decompressed) > _MAX_PNG_CARD_ENCODED_BYTES
        or inflater.unconsumed_tail
    ):
        _fail(
            "decoded_metadata_too_large",
            path,
            (
                "PNG 角色卡文本解压后超过既有解码上限："
                f"单块解码后不得超过 {MAX_PNG_CHUNK_JSON_BYTES} bytes"
            ),
        )
    if not inflater.eof or inflater.unused_data:
        _fail("malformed_png_text", path, "PNG 压缩文本数据不完整或含多余数据")
    return decompressed


def _png_text_keyword(
    raw: bytes,
    *,
    chunk_type: Literal["tEXt", "zTXt", "iTXt"],
) -> tuple[str, int]:
    separator = raw.find(b"\x00")
    if separator < 1 or separator > 79:
        _fail(
            "malformed_png_text",
            "$",
            f"PNG {chunk_type} keyword 无效",
        )
    return raw[:separator].decode("latin-1"), separator


def _parse_png_text_chunk(
    data_view: memoryview,
    *,
    chunk_type: Literal["tEXt", "zTXt", "iTXt"],
    decode_text: bool,
) -> tuple[str, bytes | None]:
    raw = data_view.tobytes()
    keyword, separator = _png_text_keyword(raw, chunk_type=chunk_type)
    path = _png_text_path(chunk_type, keyword.lower())

    if chunk_type == "tEXt":
        text = raw[separator + 1 :]
    elif chunk_type == "zTXt":
        if len(raw) < separator + 2:
            _fail("malformed_png_text", path, "PNG zTXt 缺少压缩方法")
        if raw[separator + 1] != 0:
            _fail("malformed_png_text", path, "PNG zTXt 压缩方法必须为 0")
        if not decode_text:
            return keyword, None
        text = _bounded_decompress_png_text(
            raw[separator + 2 :],
            path=path,
        )
    else:
        if len(raw) < separator + 3:
            _fail("malformed_png_text", path, "PNG iTXt 头部不完整")
        compression_flag = raw[separator + 1]
        compression_method = raw[separator + 2]
        if compression_flag not in {0, 1}:
            _fail("malformed_png_text", path, "PNG iTXt 压缩标志必须为 0 或 1")
        if compression_method != 0:
            _fail("malformed_png_text", path, "PNG iTXt 压缩方法必须为 0")
        language_end = raw.find(b"\x00", separator + 3)
        if language_end < 0:
            _fail("malformed_png_text", path, "PNG iTXt 缺少语言标记分隔符")
        translated_end = raw.find(b"\x00", language_end + 1)
        if translated_end < 0:
            _fail("malformed_png_text", path, "PNG iTXt 缺少翻译关键字分隔符")
        if not decode_text:
            return keyword, None
        encoded_text = raw[translated_end + 1 :]
        text = (
            _bounded_decompress_png_text(encoded_text, path=path)
            if compression_flag == 1
            else encoded_text
        )
        try:
            text.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CharacterCardValidationError(
                "malformed_png_text",
                path,
                "PNG iTXt 文本必须是 UTF-8",
            ) from exc

    if len(text) > _MAX_PNG_CARD_ENCODED_BYTES:
        _fail(
            "decoded_metadata_too_large",
            path,
            (
                "PNG 角色卡文本超过既有解码上限："
                f"单块解码后不得超过 {MAX_PNG_CHUNK_JSON_BYTES} bytes"
            ),
        )
    return keyword, text


def _looks_like_generation_metadata(
    *,
    keyword_names: frozenset[str],
    comments: tuple[bytes, ...],
) -> bool:
    if not ({"software", "source"} & keyword_names) or not comments:
        return False

    for comment in comments:
        try:
            parsed = json.loads(comment.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            try:
                parsed = json.loads(comment.decode("latin-1"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
        if not isinstance(parsed, dict):
            continue
        parameter_keys = {
            str(key).lower() for key in parsed
        } & _GENERATION_PARAMETER_KEYS
        if (
            len(parameter_keys) >= 2
            and parameter_keys & _GENERATION_PARAMETER_SIGNAL_KEYS
        ):
            return True
    return False


def _raise_missing_character_metadata(
    *,
    text_keywords: tuple[str, ...],
    diagnostic_text: dict[str, list[bytes]],
) -> None:
    lowered_keywords = frozenset(keyword.lower() for keyword in text_keywords)
    if not text_keywords:
        message = (
            "PNG 完全没有文本块，元数据多半在转存或压缩时被剥离。"
            "请从卡站重新下载原始文件，不要使用截图或社交平台转发的版本。"
        )
        kind: Literal[
            "ai_generation_metadata",
            "no_text_chunks",
            "other_text_chunks",
        ] = "no_text_chunks"
    elif _looks_like_generation_metadata(
        keyword_names=lowered_keywords,
        comments=tuple(diagnostic_text.get("comment", [])),
    ):
        message = (
            "这是 AI 生成的插图；PNG 携带的是 prompt、seed、sampler、steps、"
            "scale 等生成参数，不是角色数据。"
        )
        kind = "ai_generation_metadata"
    else:
        keywords = "、".join(text_keywords)
        message = (
            "PNG 不含 chara 或 ccv3 角色卡文本块。"
            f"实际存在的文本关键字：{keywords}。"
            "请据此确认是否下载了正确的原始角色卡。"
        )
        kind = "other_text_chunks"
    raise CharacterCardValidationError(
        "missing_character_metadata",
        "$",
        message,
        missing_metadata_kind=kind,
        text_keywords=text_keywords,
    )


def _extract_png_card_chunks(payload: bytes) -> dict[str, _PNGCardTextChunk]:
    if len(payload) > MAX_PNG_BYTES:
        _fail(
            "file_too_large",
            "$",
            f"PNG 文件不得超过 {MAX_PNG_BYTES} bytes",
        )
    if not payload.startswith(_PNG_SIGNATURE):
        _fail("invalid_png_signature", "$", "不是有效的 PNG 文件")

    chunks: dict[str, _PNGCardTextChunk] = {}
    text_keywords: list[str] = []
    seen_text_keywords: set[str] = set()
    diagnostic_text: dict[str, list[bytes]] = {}
    chunk_count = 0
    offset = len(_PNG_SIGNATURE)
    saw_ihdr = False
    saw_iend = False

    while offset < len(payload):
        if len(payload) - offset < 12:
            _fail("truncated_png", "$", "PNG chunk 头或校验值不完整")
        chunk_count += 1
        if chunk_count > MAX_PNG_CHUNKS:
            _fail(
                "too_many_png_chunks",
                "$",
                f"PNG chunk 数量不得超过 {MAX_PNG_CHUNKS}",
            )

        data_length = int.from_bytes(payload[offset : offset + 4], "big")
        chunk_type = payload[offset + 4 : offset + 8]
        if len(chunk_type) != 4 or not all(
            (65 <= byte <= 90) or (97 <= byte <= 122) for byte in chunk_type
        ):
            _fail("invalid_png_chunk_type", "$", "PNG chunk 类型无效")
        data_start = offset + 8
        data_end = data_start + data_length
        crc_end = data_end + 4
        if data_end < data_start or crc_end > len(payload):
            _fail("truncated_png", "$", "PNG chunk 声明长度超出文件边界")

        data_view = memoryview(payload)[data_start:data_end]
        expected_crc = int.from_bytes(payload[data_end:crc_end], "big")
        actual_crc = zlib.crc32(data_view, zlib.crc32(chunk_type)) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            _fail("invalid_png_crc", "$", "PNG chunk CRC 校验失败")

        if chunk_count == 1:
            if chunk_type != b"IHDR" or data_length != 13:
                _fail("invalid_png_structure", "$", "PNG 首个 chunk 必须是 IHDR")
            saw_ihdr = True
        elif chunk_type == b"IHDR":
            _fail("invalid_png_structure", "$", "PNG 只能包含一个首位 IHDR")

        if chunk_type in {b"tEXt", b"zTXt", b"iTXt"}:
            text_chunk_type = chunk_type.decode("ascii")
            raw_keyword = data_view.tobytes().split(b"\x00", 1)[0]
            keyword_hint = raw_keyword.decode("latin-1").lower()
            decode_text = (
                keyword_hint in _PNG_CARD_KEYWORDS
                or keyword_hint in _PNG_GENERATION_METADATA_KEYS
            )
            display_keyword, text = _parse_png_text_chunk(
                data_view,
                chunk_type=text_chunk_type,
                decode_text=decode_text,
            )
            keyword = display_keyword.lower()
            if display_keyword not in seen_text_keywords:
                seen_text_keywords.add(display_keyword)
                text_keywords.append(display_keyword)
            if keyword in _PNG_CARD_KEYWORDS:
                if keyword in chunks:
                    _fail(
                        "duplicate_png_card_chunk",
                        _png_text_path(text_chunk_type, keyword),
                        f"PNG 含多个 {keyword} 角色卡块",
                    )
                if text is None:
                    _fail(
                        "malformed_png_text",
                        _png_text_path(text_chunk_type, keyword),
                        f"PNG {text_chunk_type} 角色卡文本缺失",
                    )
                chunks[keyword] = _PNGCardTextChunk(
                    encoded=text,
                    chunk_type=text_chunk_type,
                )
            elif (
                keyword in _PNG_GENERATION_METADATA_KEYS
                and text is not None
            ):
                diagnostic_text.setdefault(keyword, []).append(text)

        offset = crc_end
        if chunk_type == b"IEND":
            if data_length != 0:
                _fail("invalid_png_structure", "$", "IEND chunk 必须为空")
            saw_iend = True
            if offset != len(payload):
                _fail("invalid_png_structure", "$", "IEND 后不得有额外数据")
            break

    if not saw_ihdr or not saw_iend:
        _fail("truncated_png", "$", "PNG 缺少 IHDR 或 IEND")
    if not chunks:
        _raise_missing_character_metadata(
            text_keywords=tuple(text_keywords),
            diagnostic_text=diagnostic_text,
        )

    total_decoded_upper_bound = 0
    for keyword, chunk in chunks.items():
        encoded = chunk.encoded
        path = _png_text_path(chunk.chunk_type, keyword)
        # Base64 padding means this upper bound can exceed the actual size by
        # at most two bytes per candidate chunk.
        decoded_upper_bound = ((len(encoded) + 3) // 4) * 3
        if decoded_upper_bound > MAX_PNG_CHUNK_JSON_BYTES + 2:
            _fail(
                "decoded_metadata_too_large",
                path,
                f"单个 PNG 角色卡块解码后不得超过 {MAX_PNG_CHUNK_JSON_BYTES} bytes",
            )
        total_decoded_upper_bound += decoded_upper_bound
    if total_decoded_upper_bound > MAX_PNG_DECODED_BYTES + 4:
        _fail(
            "decoded_metadata_too_large",
            "$.png.text",
            f"PNG 角色卡块解码后合计不得超过 {MAX_PNG_DECODED_BYTES} bytes",
        )
    return chunks


def _decode_png_card_chunk(
    chunk: _PNGCardTextChunk,
    *,
    keyword: str,
) -> Any:
    path = _png_text_path(chunk.chunk_type, keyword)
    try:
        decoded = base64.b64decode(chunk.encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CharacterCardValidationError(
            "invalid_base64", path, f"{keyword} 不是有效的 Base64"
        ) from exc
    if len(decoded) > MAX_PNG_CHUNK_JSON_BYTES:
        _fail(
            "decoded_metadata_too_large",
            path,
            f"单个 PNG 角色卡块解码后不得超过 {MAX_PNG_CHUNK_JSON_BYTES} bytes",
        )
    return _decode_json_document(decoded, path=path)


def _equal_except_card_spec(left: Any, right: Any) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    ignored = {"spec", "spec_version"}
    left_content = {
        key: value for key, value in left.items() if key not in ignored
    }
    right_content = {
        key: value for key, value in right.items() if key not in ignored
    }
    return _json_values_equal(left_content, right_content)


def _v2_fallback_raw_card_with_v3_metadata(
    ccv3: Any,
    chara: Any,
) -> dict[str, Any] | None:
    """Return a V2 raw card only when bounded V3 metadata can be preserved."""

    if not isinstance(ccv3, dict) or not isinstance(chara, dict):
        return None
    if (
        ccv3.get("spec") != "chara_card_v3"
        or ccv3.get("spec_version") != "3.0"
    ):
        return None
    ccv3_data = ccv3.get("data")
    chara_data = chara.get("data")
    if not isinstance(ccv3_data, dict) or not isinstance(chara_data, dict):
        return None

    v2_data_keys = {
        *_CARD_REQUIRED_STRINGS,
        "alternate_greetings",
        "tags",
        "extensions",
        "character_book",
    }
    if not ccv3_data.keys() <= {
        *v2_data_keys,
        "group_only_greetings",
        "source",
    }:
        return None

    extra_keys = ccv3_data.keys() - chara_data.keys()
    if not extra_keys <= {"group_only_greetings", "source"}:
        return None
    if chara_data.keys() - ccv3_data.keys():
        return None
    if (
        "group_only_greetings" in ccv3_data
        and ccv3_data["group_only_greetings"] != []
    ):
        return None
    if "source" in ccv3_data and (
        not isinstance(ccv3_data["source"], list)
        or any(not isinstance(item, str) for item in ccv3_data["source"])
    ):
        return None

    normalized = deepcopy(ccv3)
    normalized["spec"] = "chara_card_v2"
    normalized["spec_version"] = "2.0"
    normalized_data = normalized["data"]
    for key in extra_keys:
        normalized_data.pop(key)
    if not _json_values_equal(normalized, chara):
        return None

    preserved = deepcopy(chara)
    preserved_data = preserved["data"]
    for key in ("source", "group_only_greetings"):
        if key in ccv3_data:
            preserved_data[key] = deepcopy(ccv3_data[key])
    _validate_resource_limits(preserved)
    return preserved


def _json_values_equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, (int, float)) or isinstance(right, (int, float)):
        return (
            isinstance(left, (int, float))
            and not isinstance(left, bool)
            and isinstance(right, (int, float))
            and not isinstance(right, bool)
            and left == right
        )
    if isinstance(left, dict) or isinstance(right, dict):
        if not isinstance(left, dict) or not isinstance(right, dict):
            return False
        return left.keys() == right.keys() and all(
            _json_values_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list) or isinstance(right, list):
        if not isinstance(left, list) or not isinstance(right, list):
            return False
        return len(left) == len(right) and all(
            _json_values_equal(left_item, right_item)
            for left_item, right_item in zip(left, right)
        )
    return type(left) is type(right) and left == right


def _with_png_preview(
    parsed: ParsedCharacterCard,
    *,
    selected_chunk: Literal["chara", "ccv3"],
    classification: Literal["v1", "v2", "v3", "pseudo_v3"],
    label: str,
    raw_card: dict[str, Any] | None = None,
) -> ParsedCharacterCard:
    return replace(
        parsed,
        raw_card=deepcopy(raw_card) if raw_card is not None else parsed.raw_card,
        selected_png_chunk=selected_chunk,
        png_chunk_classification=classification,
        png_preview_label=label,
        image_data_discarded=True,
    )


class CharacterCardAdapter:
    """Public in-memory parsing seam for Character Card JSON and PNG files."""

    @classmethod
    def parse_json(
        cls,
        payload: bytes,
        *,
        declared_mime: str,
        filename: str | None = None,
    ) -> ParsedCharacterCard:
        if not isinstance(payload, bytes):
            _fail("invalid_payload", "$", "JSON 文件内容必须是 bytes")
        if len(payload) > MAX_JSON_BYTES:
            _fail(
                "file_too_large",
                "$",
                f"JSON 文件不得超过 {MAX_JSON_BYTES} bytes",
            )
        mime = declared_mime.partition(";")[0].strip().lower()
        if mime != "application/json":
            _fail("mime_mismatch", "$", "声明的 MIME 必须是 application/json")
        if filename is not None and not filename.lower().endswith(".json"):
            _fail("extension_mismatch", "$", "JSON 角色卡文件名必须以 .json 结尾")
        value = _decode_json_document(payload, path="$")
        return cls.parse(value, source_container="json")

    @classmethod
    def parse_png(
        cls,
        payload: bytes,
        *,
        declared_mime: str,
        filename: str | None = None,
    ) -> ParsedCharacterCard:
        if not isinstance(payload, bytes):
            _fail("invalid_payload", "$", "PNG 文件内容必须是 bytes")
        mime = declared_mime.partition(";")[0].strip().lower()
        if mime != "image/png":
            _fail("mime_mismatch", "$", "声明的 MIME 必须是 image/png")
        if filename is not None and not filename.lower().endswith(".png"):
            _fail("extension_mismatch", "$", "PNG 角色卡文件名必须以 .png 结尾")

        chunks = _extract_png_card_chunks(payload)
        if "ccv3" not in chunks:
            chara = _decode_png_card_chunk(chunks["chara"], keyword="chara")
            parsed = cls.parse(chara, source_container="png")
            format_label = parsed.source_format.upper()
            return _with_png_preview(
                parsed,
                selected_chunk="chara",
                classification=parsed.source_format,
                label=f"采用 chara（{format_label}）",
            )

        ccv3 = _decode_png_card_chunk(chunks["ccv3"], keyword="ccv3")
        ccv3_path = _png_text_path(chunks["ccv3"].chunk_type, "ccv3")
        try:
            parsed_v3 = cls.parse(ccv3, source_container="png")
            if parsed_v3.source_format != "v3":
                _fail(
                    "invalid_ccv3_spec",
                    f"{ccv3_path}.spec",
                    "ccv3 必须声明 chara_card_v3",
                )
        except CharacterCardValidationError as v3_error:
            if "chara" not in chunks:
                raise CharacterCardValidationError(
                    "malformed_v3_chunk",
                    ccv3_path,
                    f"ccv3 未通过 V3 严格校验且没有 chara 可核对：{v3_error.message}",
                ) from v3_error

            chara = _decode_png_card_chunk(chunks["chara"], keyword="chara")
            chara_path = _png_text_path(chunks["chara"].chunk_type, "chara")
            try:
                parsed_chara = cls.parse(chara, source_container="png")
                if parsed_chara.source_format != "v2":
                    _fail(
                        "invalid_chara_spec",
                        f"{chara_path}.spec",
                        "伪 V3 的 chara 对照块必须是 V2",
                    )
            except CharacterCardValidationError as v2_error:
                raise CharacterCardValidationError(
                    "malformed_v3_chunk",
                    ccv3_path,
                    f"chara 对照块不是有效 V2：{v2_error.message}",
                ) from v2_error

            fallback_raw_card = _v2_fallback_raw_card_with_v3_metadata(
                ccv3,
                chara,
            )
            if (
                _equal_except_card_spec(ccv3, chara)
                and fallback_raw_card is not None
            ):
                v2_content = deepcopy(_require_object(ccv3, ccv3_path))
                v2_content["spec"] = "chara_card_v2"
                v2_content["spec_version"] = "2.0"
                parsed_v2 = cls.parse(v2_content, source_container="png")

                return _with_png_preview(
                    parsed_v2,
                    selected_chunk="ccv3",
                    classification="pseudo_v3",
                    label="采用 ccv3，但内容实为 V2（酒馆导出的 spec 改写块）",
                    raw_card=_require_object(ccv3, ccv3_path),
                )

            if fallback_raw_card is not None:
                warning = (
                    "ccv3 未通过 V3 严格校验；已核对共享字段一致并采用 chara（V2），"
                    "来源仅隔离保留，不参与安全字段映射"
                )
                parsed_chara = replace(
                    parsed_chara,
                    compatibility_warnings=(
                        *parsed_chara.compatibility_warnings,
                        warning,
                    ),
                )
                return _with_png_preview(
                    parsed_chara,
                    selected_chunk="chara",
                    classification="v2",
                    label=(
                        "ccv3 未通过 V3 严格校验；共享字段一致，"
                        "已安全采用 chara（V2）"
                    ),
                    raw_card=fallback_raw_card,
                )

            raise CharacterCardValidationError(
                "malformed_v3_chunk",
                ccv3_path,
                f"ccv3 未通过 V3 严格校验且与 chara 有实质差异：{v3_error.message}",
            ) from v3_error

        return _with_png_preview(
            parsed_v3,
            selected_chunk="ccv3",
            classification="v3",
            label="采用 ccv3（V3）",
        )

    @classmethod
    def parse(
        cls,
        card: Any,
        *,
        source_container: Literal["json", "png"],
    ) -> ParsedCharacterCard:
        if source_container not in {"json", "png"}:
            _fail("unsupported_container", "$", "只接受 JSON 或 PNG 容器")
        _validate_resource_limits(card)
        raw_card = deepcopy(_require_object(card, "$"))
        if "spec" in raw_card:
            spec = raw_card["spec"]
            if spec == "chara_card_v2":
                return _parse_v2(raw_card, source_container)
            if spec == "chara_card_v3":
                return _parse_v3(raw_card, source_container)
            _fail("unsupported_spec", "$.spec", "不支持该角色卡规范")
        fields = {key: _require_string(raw_card, key, "$") for key in _V1_REQUIRED_STRINGS}
        return ParsedCharacterCard(
            source_format="v1",
            source_container=source_container,
            spec_version="1.0",
            fields=deepcopy(fields),
            raw_card=raw_card,
            compatibility_warnings=(
                "V1 仅含基础角色字段，转换到 V2/V3 时无法恢复缺失字段",
            ),
            prompt_risk_fields=(),
            decorators=(),
            assets=(),
        )
