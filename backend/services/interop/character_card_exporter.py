"""Loss-aware Character Card V2 JSON export.

The exporter treats an imported card's retained raw JSON as opaque transport
data. Known fields are refreshed from the reviewed formal cards, while unknown
fields remain untouched so an import/export round trip does not erase newer or
vendor-specific extensions.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable

from backend.db.errors import NotFoundError
from backend.db.repositories.worldbook_repository import worldbook_repo
from backend.db.utils import to_object_id
from backend.services.novel.reference_card_service import ReferenceCardService


EXPORT_SCHEMA_VERSION = "1"
_NOVEL_G_EXTENSION = {"export_schema_version": EXPORT_SCHEMA_VERSION}
_CHAT_FIELDS = (
    "scenario",
    "first_mes",
    "mes_example",
    "system_prompt",
    "post_history_instructions",
)
_V1_LOSS_NOTE = (
    "由 novel-G 导出。源格式为 Character Card V1；V1 不包含 "
    "creator_notes、system_prompt、post_history_instructions、"
    "alternate_greetings、tags、creator、character_version 与 extensions，"
    "这些字段无法从源卡恢复。未映射：小说时间线、章节状态、永久事实。"
)
_V3_LOSS_NOTE = (
    "由 novel-G 转换并导出为 Character Card V2。未映射：V3 专有资产与"
    "行为语义、小说时间线、章节状态、永久事实；未知字段按不透明 JSON 保留。"
)
_NATIVE_EXPORT_NOTE = (
    "由 novel-G 导出。未映射：小说时间线、章节状态、永久事实。"
)


def _source_format(card: dict[str, Any]) -> str:
    interop = card.get("interop")
    source = interop.get("source") if isinstance(interop, dict) else None
    value = source.get("format") if isinstance(source, dict) else None
    return value if isinstance(value, str) else ""


def _raw_spec(card: dict[str, Any]) -> dict[str, Any]:
    interop = card.get("interop")
    value = interop.get("raw_spec") if isinstance(interop, dict) else None
    return deepcopy(value) if isinstance(value, dict) else {}


def _raw_data(raw: dict[str, Any], source_format: str) -> dict[str, Any]:
    if source_format in {"v2", "v3"} and isinstance(raw.get("data"), dict):
        return raw["data"]
    if source_format == "v1":
        return raw
    return {}


def _string_source(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    return value if isinstance(value, str) else ""


def _array_source(data: dict[str, Any], key: str) -> list[str]:
    value = data.get(key)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return deepcopy(value)
    return []


def _append_note(existing: str, note: str) -> str:
    if not existing:
        return note
    return f"{existing}\n\n{note}"


def _portable_extensions(
    raw_data: dict[str, Any],
    *,
    preserve_pristine_v2: bool,
) -> dict[str, Any]:
    raw_extensions = raw_data.get("extensions")
    extensions = (
        deepcopy(raw_extensions) if isinstance(raw_extensions, dict) else {}
    )
    if not preserve_pristine_v2:
        extensions["novel-g"] = deepcopy(_NOVEL_G_EXTENSION)
    return extensions


def _confirmed_keywords(card: dict[str, Any]) -> list[str]:
    values: list[str] = [str(card.get("name") or "").strip()]
    interop = card.get("interop")
    display = (
        interop.get("display_metadata") if isinstance(interop, dict) else None
    )
    keys = display.get("keys") if isinstance(display, dict) else None
    if isinstance(keys, list):
        values.extend(item for item in keys if isinstance(item, str))
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _worldbook_entry(card: dict[str, Any], index: int) -> dict[str, Any]:
    interop = card.get("interop")
    raw_entry = interop.get("raw_entry") if isinstance(interop, dict) else None
    if isinstance(raw_entry, dict):
        entry = deepcopy(raw_entry)
        entry["name"] = str(card.get("name") or "")
        entry["content"] = str(card.get("description") or "")
        display = (
            interop.get("display_metadata")
            if isinstance(interop, dict)
            else None
        )
        confirmed = display.get("keys") if isinstance(display, dict) else None
        if isinstance(confirmed, list) and all(
            isinstance(item, str) for item in confirmed
        ):
            entry["keys"] = deepcopy(confirmed)
        elif not isinstance(entry.get("keys"), list):
            entry["keys"] = _confirmed_keywords(card)
        return entry

    return {
        "keys": _confirmed_keywords(card),
        "secondary_keys": [],
        "content": str(card.get("description") or ""),
        "extensions": {"novel-g": {
            **deepcopy(_NOVEL_G_EXTENSION),
            "card_type": str(card.get("card_type") or "lore"),
        }},
        "enabled": True,
        "insertion_order": (index + 1) * 100,
        "name": str(card.get("name") or ""),
        "comment": str(card.get("subtitle") or ""),
        "constant": False,
        "position": "before_char",
        "use_regex": False,
    }


def _character_book(
    raw_data: dict[str, Any],
    worldbook_cards: Iterable[dict[str, Any]],
) -> dict[str, Any] | None:
    cards = list(worldbook_cards)
    if not cards:
        return None
    raw_book = raw_data.get("character_book")
    book = deepcopy(raw_book) if isinstance(raw_book, dict) else {}
    book.setdefault("name", "")
    book.setdefault("description", "")
    book.setdefault("scan_depth", 0)
    book.setdefault("token_budget", 0)
    book.setdefault("recursive_scanning", False)
    book.setdefault("extensions", {})
    book["entries"] = [
        _worldbook_entry(card, index) for index, card in enumerate(cards)
    ]
    return book


def build_character_card_v2(
    character: dict[str, Any],
    worldbook_cards: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Build portable V2 JSON without exposing persistence metadata."""

    source_format = _source_format(character)
    raw = _raw_spec(character)
    raw_data = _raw_data(raw, source_format)
    preserve_pristine_v2 = source_format == "v2"

    if source_format in {"v2", "v3"}:
        result = deepcopy(raw)
    else:
        result = {}
    result["spec"] = "chara_card_v2"
    result["spec_version"] = "2.0"

    data = (
        deepcopy(raw_data)
        if source_format in {"v2", "v3"} and isinstance(raw_data, dict)
        else {}
    )
    data["name"] = str(character.get("name") or "")
    data["description"] = str(character.get("description") or "")
    details = character.get("details")
    personality = (
        details.get("personality") if isinstance(details, dict) else None
    )
    data["personality"] = (
        personality
        if isinstance(personality, str)
        else _string_source(raw_data, "personality")
    )
    for field in _CHAT_FIELDS:
        data[field] = _string_source(raw_data, field)
    data["alternate_greetings"] = _array_source(
        raw_data, "alternate_greetings"
    )
    data["tags"] = [
        item for item in character.get("tags", []) if isinstance(item, str)
    ]
    data["creator"] = (
        _string_source(raw_data, "creator")
        if preserve_pristine_v2
        else (_string_source(raw_data, "creator") or "novel-G")
    )
    data["character_version"] = (
        _string_source(raw_data, "character_version")
        if preserve_pristine_v2
        else (_string_source(raw_data, "character_version") or "1.0")
    )
    original_notes = _string_source(raw_data, "creator_notes")
    if preserve_pristine_v2:
        data["creator_notes"] = original_notes
    elif source_format == "v1":
        data["creator_notes"] = _V1_LOSS_NOTE
    elif source_format == "v3":
        data["creator_notes"] = _append_note(original_notes, _V3_LOSS_NOTE)
    else:
        data["creator_notes"] = _NATIVE_EXPORT_NOTE
    data["extensions"] = _portable_extensions(
        raw_data,
        preserve_pristine_v2=preserve_pristine_v2,
    )

    book = _character_book(raw_data, worldbook_cards)
    if book is None:
        data.pop("character_book", None)
    else:
        data["character_book"] = book
    result["data"] = data
    return result


class CharacterCardExportService:
    """Load owned formal cards and export their portable representation."""

    @staticmethod
    async def _get_worldbook_card(
        novel_id: str,
        card_id: str,
    ) -> dict[str, Any]:
        card = await worldbook_repo.find_one(
            {
                "_id": to_object_id(card_id),
                "novel_id": to_object_id(novel_id),
                "card_type": {"$in": ["location", "item", "rule", "lore"]},
            }
        )
        if card is None:
            raise NotFoundError(
                f"World-book reference card '{card_id}' was not found"
            )
        return card

    @staticmethod
    async def export_v2(
        novel_id: str,
        character_card_id: str,
        worldbook_card_ids: Iterable[str] = (),
    ) -> dict[str, Any]:
        character = await ReferenceCardService.get(
            novel_id,
            "character",
            character_card_id,
        )
        ids = list(worldbook_card_ids)
        if len(ids) != len(set(ids)):
            raise ValueError("worldbook_card_ids cannot contain duplicates")
        worldbook_cards = [
            await CharacterCardExportService._get_worldbook_card(
                novel_id, card_id
            )
            for card_id in ids
        ]
        return build_character_card_v2(character, worldbook_cards)


character_card_export_service = CharacterCardExportService()
