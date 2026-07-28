"""Persist bounded Character Card previews for explicit human review.

This layer only writes ``card_import_proposals``. It never creates or mutates
formal reference cards, executes imported behavior, or downloads assets.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from bson import ObjectId
from pymongo import DESCENDING
from pymongo.asynchronous.database import AsyncDatabase

from backend.config.config import get_config_value
from backend.db import collections
from backend.db.errors import NotFoundError
from backend.db.mongo import get_database
from backend.db.utils import get_utc_now, to_object_id
from backend.services.interop.character_card_adapter import (
    MAX_JSON_BYTES,
    ParsedCharacterCard,
)


MAX_RAW_PAYLOAD_BYTES = MAX_JSON_BYTES
PROPOSAL_LIFETIME = timedelta(days=7)
PROPOSAL_RETENTION = timedelta(days=30)
_SOURCE_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_NOVEL_SNAPSHOT_FIELDS = (
    "title",
    "subtitle",
    "genre",
    "tags",
    "plot",
    "core_idea",
    "tone",
    "target_audience",
    "introduction",
    "summary",
    "core_seed",
    "worldview",
    "writing_style",
    "narrative_pov",
    "era_background",
    "number_of_chapters",
    "words_per_chapter",
    "narrative_revision",
    "updated_at",
)


class RawPayloadTooLargeError(ValueError):
    """The lossless payload cannot be stored within the proposal limit."""

    def __init__(self, *, current_bytes: int, max_bytes: int):
        self.current_bytes = current_bytes
        self.max_bytes = max_bytes
        super().__init__(
            "raw_payload exceeds proposal storage limit: "
            f"current_bytes={current_bytes}, max_bytes={max_bytes}; "
            "import rejected without truncation"
        )


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _jsonable(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        normalized = (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )
        return normalized.isoformat()
    return value


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _default_config_snapshot() -> dict[str, Any]:
    configured = get_config_value("card_import", {})
    return {
        "implementation": {
            "mapping_version": 1,
            "raw_payload_max_bytes": MAX_RAW_PAYLOAD_BYTES,
            "proposal_lifetime_seconds": int(PROPOSAL_LIFETIME.total_seconds()),
            "proposal_retention_seconds": int(PROPOSAL_RETENTION.total_seconds()),
        },
        "runtime": deepcopy(configured) if isinstance(configured, dict) else {},
    }


def _serialize_proposal(proposal: dict[str, Any]) -> dict[str, Any]:
    result = _jsonable(deepcopy(proposal))
    result["proposal_id"] = result.pop("_id")
    return result


class CardImportProposalService:
    """Owner-scoped staging and staleness checks for parsed card imports."""

    def __init__(
        self,
        database: AsyncDatabase | None = None,
        *,
        config_provider: Callable[[], dict[str, Any]] | None = None,
    ):
        self._database = database
        self._config_provider = config_provider or _default_config_snapshot

    @property
    def db(self) -> AsyncDatabase:
        return self._database if self._database is not None else get_database()

    async def _novel_snapshot(
        self,
        novel_id: ObjectId | None,
        *,
        owner_id: ObjectId,
    ) -> dict[str, Any] | None:
        if novel_id is None:
            return None
        novel = await self.db[collections.NOVELS].find_one(
            {
                "_id": novel_id,
                "owner_id": owner_id,
            }
        )
        if novel is None:
            raise NotFoundError(f"Novel with id {novel_id} not found")
        return {
            field: deepcopy(novel.get(field))
            for field in _NOVEL_SNAPSHOT_FIELDS
        }

    def _configuration_snapshot(self) -> dict[str, Any]:
        value = self._config_provider()
        if not isinstance(value, dict):
            raise ValueError("card import configuration snapshot must be an object")
        return deepcopy(value)

    @staticmethod
    def _character_draft(
        parsed: ParsedCharacterCard,
    ) -> dict[str, Any]:
        fields = parsed.fields
        nickname = fields.get("nickname")
        aliases = [nickname] if isinstance(nickname, str) and nickname else []
        return {
            "name": fields.get("name", ""),
            "description": fields.get("description", ""),
            "tags": deepcopy(fields.get("tags", [])),
            "character_profile": {
                "aliases": aliases,
                "portrayal_context": fields.get("personality", ""),
                "dialogue_examples": [],
                "scene_opening_examples": [],
                "portrayal_notes": "",
            },
            "interop": {
                "source_format": parsed.source_format,
                "scenario": fields.get("scenario", ""),
                "first_mes": fields.get("first_mes", ""),
                "mes_example": fields.get("mes_example", ""),
                "creator_notes": fields.get("creator_notes", ""),
                "alternate_greetings": deepcopy(
                    fields.get("alternate_greetings", [])
                ),
            },
        }

    async def _character_conflicts(
        self,
        *,
        novel_id: ObjectId | None,
        draft: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if novel_id is None:
            return []
        imported_name = draft["name"]
        cursor = self.db[collections.CHARACTERS].find(
            {
                "novel_id": novel_id,
                "card_type": "character",
                "$or": [
                    {"name": imported_name},
                    {"character_profile.aliases": imported_name},
                ],
            }
        )
        existing_cards = await cursor.to_list(length=None)
        conflicts: list[dict[str, Any]] = []
        for card in existing_cards:
            match_kind = (
                "exact_name"
                if card.get("name") == imported_name
                else "confirmed_alias"
            )
            field_diffs: dict[str, dict[str, Any]] = {}
            for field, default in (
                ("name", ""),
                ("description", ""),
                ("tags", []),
                ("character_profile", {}),
            ):
                existing_value = deepcopy(card.get(field, default))
                imported_value = deepcopy(draft.get(field, default))
                if existing_value != imported_value:
                    field_diffs[field] = {
                        "existing": existing_value,
                        "imported": imported_value,
                    }
            conflicts.append(
                {
                    "target_card_id": str(card["_id"]),
                    "match_kind": match_kind,
                    "is_deleted": bool(card.get("is_deleted")),
                    "field_diffs": field_diffs,
                }
            )
        conflicts.sort(key=lambda item: item["target_card_id"])
        return conflicts

    @staticmethod
    def _proposed_cards(
        draft: dict[str, Any],
        *,
        duplicate: bool,
        conflicts: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if duplicate or len(conflicts) > 1:
            recommended_action = "skip"
        elif conflicts:
            recommended_action = (
                "restore_merge" if conflicts[0]["is_deleted"] else "merge"
            )
        else:
            recommended_action = "create"
        return [
            {
                "candidate_id": "character:0",
                "target_type": "character",
                "fields": draft,
                "conflicts": conflicts,
                "recommended_action": recommended_action,
            }
        ]

    async def stage(
        self,
        parsed: ParsedCharacterCard,
        *,
        owner_id: str | ObjectId,
        novel_id: str | ObjectId | None,
        source_hash: str,
        source_name: str | None = None,
    ) -> dict[str, Any]:
        """Persist one review proposal without touching formal card collections."""
        if not isinstance(parsed, ParsedCharacterCard):
            raise TypeError("parsed must be a ParsedCharacterCard")
        owner_object_id = to_object_id(owner_id)
        # Deliberately guard None before to_object_id: ObjectId(None) creates an ID.
        novel_object_id = (
            to_object_id(novel_id) if novel_id is not None else None
        )
        if not isinstance(source_hash, str) or not _SOURCE_HASH_RE.fullmatch(
            source_hash
        ):
            raise ValueError("source_hash must be a lowercase SHA-256 hex digest")
        if source_name is not None:
            if not isinstance(source_name, str) or len(source_name) > 255:
                raise ValueError("source_name must be a string of at most 255 characters")

        raw_payload = deepcopy(parsed.raw_card)
        raw_payload_bytes = len(_canonical_json_bytes(raw_payload))
        if raw_payload_bytes > MAX_RAW_PAYLOAD_BYTES:
            raise RawPayloadTooLargeError(
                current_bytes=raw_payload_bytes,
                max_bytes=MAX_RAW_PAYLOAD_BYTES,
            )

        novel_snapshot = await self._novel_snapshot(
            novel_object_id,
            owner_id=owner_object_id,
        )
        config_snapshot = self._configuration_snapshot()
        novel_snapshot_digest = _digest(novel_snapshot)
        config_digest = _digest(config_snapshot)

        collection = self.db[collections.CARD_IMPORT_PROPOSALS]
        duplicate = await collection.find_one(
            {
                "owner_id": owner_object_id,
                "source_hash": source_hash,
            },
            sort=[("imported_at", DESCENDING)],
        )
        duplicate_source = None
        if duplicate is not None:
            duplicate_source = {
                "proposal_id": str(duplicate["_id"]),
                "imported_at": duplicate.get("imported_at"),
            }
        draft = self._character_draft(parsed)
        conflicts = await self._character_conflicts(
            novel_id=novel_object_id,
            draft=draft,
        )
        target_cards_digest = _digest(conflicts)
        combined_digest = _digest(
            {
                "novel_snapshot_digest": novel_snapshot_digest,
                "config_digest": config_digest,
                "target_cards_digest": target_cards_digest,
            }
        )

        now = get_utc_now()
        proposal: dict[str, Any] = {
            "schema_version": 1,
            "novel_id": novel_object_id,
            "owner_id": owner_object_id,
            "source_format": parsed.source_format,
            "source_container": parsed.source_container,
            "source_name": source_name,
            "source_hash": source_hash,
            "raw_payload": raw_payload,
            "raw_payload_bytes": raw_payload_bytes,
            "detected_warnings": list(parsed.detected_warnings),
            "prompt_risk_fields": [
                asdict(item) for item in parsed.prompt_risk_fields
            ],
            "decorators": [asdict(item) for item in parsed.decorators],
            "assets": [asdict(item) for item in parsed.assets],
            "container_preview": {
                "selected_png_chunk": parsed.selected_png_chunk,
                "png_chunk_classification": parsed.png_chunk_classification,
                "png_preview_label": parsed.png_preview_label,
                "image_data_discarded": parsed.image_data_discarded,
            },
            "proposed_cards": self._proposed_cards(
                draft,
                duplicate=duplicate is not None,
                conflicts=conflicts,
            ),
            "decisions": [],
            "duplicate_source": duplicate_source,
            "novel_snapshot_digest": novel_snapshot_digest,
            "config_digest": config_digest,
            "target_cards_digest": target_cards_digest,
            "digest": combined_digest,
            "status": "pending_review",
            "stale_reasons": [],
            "imported_by": owner_object_id,
            "imported_at": now,
            "updated_at": now,
            "applied_at": None,
            "expires_at": now + PROPOSAL_LIFETIME,
            "purge_after": now + PROPOSAL_RETENTION,
        }
        inserted = await collection.insert_one(proposal)
        proposal["_id"] = inserted.inserted_id
        return _serialize_proposal(proposal)

    async def inspect(
        self,
        proposal_id: str | ObjectId,
        *,
        owner_id: str | ObjectId,
    ) -> dict[str, Any]:
        """Read an owned proposal and mark proposal-only state stale if needed."""
        proposal_object_id = to_object_id(proposal_id)
        owner_object_id = to_object_id(owner_id)
        collection = self.db[collections.CARD_IMPORT_PROPOSALS]
        proposal = await collection.find_one(
            {
                "_id": proposal_object_id,
                "owner_id": owner_object_id,
            }
        )
        if proposal is None:
            raise NotFoundError(
                f"Card import proposal with id {proposal_id} not found"
            )

        stale_reasons: list[str] = []
        current_config_digest = _digest(self._configuration_snapshot())
        if current_config_digest != proposal.get("config_digest"):
            stale_reasons.append("configuration_changed")

        novel_id = proposal.get("novel_id")
        try:
            current_novel_snapshot = await self._novel_snapshot(
                novel_id,
                owner_id=owner_object_id,
            )
        except NotFoundError:
            current_novel_snapshot = None
            stale_reasons.append("novel_missing_or_reassigned")
        else:
            if _digest(current_novel_snapshot) != proposal.get(
                "novel_snapshot_digest"
            ):
                stale_reasons.append("novel_snapshot_changed")

        proposed_cards = proposal.get("proposed_cards")
        if isinstance(proposed_cards, list) and proposed_cards:
            draft = proposed_cards[0].get("fields")
            if isinstance(draft, dict):
                current_conflicts = await self._character_conflicts(
                    novel_id=novel_id,
                    draft=draft,
                )
                if _digest(current_conflicts) != proposal.get(
                    "target_cards_digest"
                ):
                    stale_reasons.append("target_cards_changed")

        expires_at = proposal.get("expires_at")
        if isinstance(expires_at, datetime):
            normalized_expiry = (
                expires_at.replace(tzinfo=timezone.utc)
                if expires_at.tzinfo is None
                else expires_at.astimezone(timezone.utc)
            )
            if normalized_expiry <= datetime.now(timezone.utc):
                stale_reasons.append("proposal_expired")

        if stale_reasons:
            now = get_utc_now()
            await collection.update_one(
                {
                    "_id": proposal_object_id,
                    "owner_id": owner_object_id,
                },
                {
                    "$set": {
                        "status": "stale",
                        "stale_reasons": stale_reasons,
                        "stale_at": now,
                        "updated_at": now,
                    }
                },
            )
            proposal["status"] = "stale"
            proposal["stale_reasons"] = stale_reasons
            proposal["stale_at"] = now
            proposal["updated_at"] = now

        result = _serialize_proposal(proposal)
        result["is_stale"] = result.get("status") == "stale"
        return result


card_import_proposal_service = CardImportProposalService()
