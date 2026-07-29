"""Select embedded Character Card avatars without changing card parsing."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from enum import Enum
import hashlib
import hmac
from typing import Any, Literal, Protocol

from bson import ObjectId

from backend.db import collections
from backend.db.mongo import get_database
from backend.db.utils import to_object_id
from backend.services.image.managed_assets import (
    ImageAssetRecord,
    ImageAssetWriterProtocol,
    ImportedImageAssetCreate,
    InvalidImageAssetError,
    ManagedImageAssetService,
)
from backend.services.interop.character_card_adapter import (
    MAX_PNG_BYTES,
    CharacterCardAsset,
    CharacterCardAdapter,
    ParsedCharacterCard,
)


# A PNG card and its avatar are the same byte sequence. A lower avatar-only
# limit would accept the card but silently make its image impossible to keep.
# Decode amplification remains bounded by the shared Pillow probe.
MAX_IMPORTED_AVATAR_BYTES = MAX_PNG_BYTES


class InvalidCharacterCardAvatar(ValueError):
    """The selected embedded avatar cannot cross the managed-image boundary."""


class CharacterCardAvatarNotFound(LookupError):
    """The owned, applied proposal or mapped character is not available."""


class AvatarSourceKind(str, Enum):
    PNG_CONTAINER = "png_container"
    DATA_URI = "data_uri"
    REMOTE_URL = "remote_url"
    EMBEDDED_REFERENCE = "embedded_reference"
    DEFAULT = "default"
    NONE = "none"


@dataclass(frozen=True)
class CharacterCardAvatarPreview:
    source_kind: AvatarSourceKind
    importable: bool
    asset_path: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "source_kind": self.source_kind.value,
            "importable": self.importable,
            "asset_path": self.asset_path,
        }


@dataclass(frozen=True)
class CharacterCardAvatarImportResult:
    status: Literal["imported", "not_imported", "skipped"]
    source_kind: AvatarSourceKind
    asset_id: str | None = None
    content_hash: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "status": self.status,
            "source_kind": self.source_kind.value,
            "asset_id": self.asset_id,
            "content_hash": self.content_hash,
        }


class AppliedAvatarProposalGatewayProtocol(Protocol):
    async def get_applied_owned(
        self,
        *,
        proposal_id: ObjectId,
        owner_id: ObjectId,
    ) -> dict[str, Any] | None: ...

    async def get_character(
        self,
        *,
        novel_id: ObjectId,
        card_id: ObjectId,
    ) -> dict[str, Any] | None: ...


class MongoAppliedAvatarProposalGateway:
    async def get_applied_owned(
        self,
        *,
        proposal_id: ObjectId,
        owner_id: ObjectId,
    ) -> dict[str, Any] | None:
        return await get_database()[collections.CARD_IMPORT_PROPOSALS].find_one(
            {
                "_id": proposal_id,
                "owner_id": owner_id,
                "status": "applied",
            }
        )

    async def get_character(
        self,
        *,
        novel_id: ObjectId,
        card_id: ObjectId,
    ) -> dict[str, Any] | None:
        return await get_database()[collections.CHARACTERS].find_one(
            {
                "_id": card_id,
                "novel_id": novel_id,
                "card_type": "character",
                "is_deleted": False,
            },
            projection={"_id": 1, "novel_id": 1},
        )


def _selected_icon(parsed: ParsedCharacterCard) -> CharacterCardAsset | None:
    icons = [asset for asset in parsed.assets if asset.type == "icon"]
    if not icons:
        return None
    if len(icons) == 1:
        return icons[0]
    # The adapter already rejects multiple icons unless exactly one is main.
    return next(asset for asset in icons if asset.name == "main")


def describe_avatar_source(
    parsed: ParsedCharacterCard,
) -> CharacterCardAvatarPreview:
    """Describe only what the card structure makes available for transfer."""

    if parsed.source_container == "png":
        return CharacterCardAvatarPreview(
            source_kind=AvatarSourceKind.PNG_CONTAINER,
            importable=True,
        )
    icon = _selected_icon(parsed)
    if icon is None:
        return CharacterCardAvatarPreview(
            source_kind=AvatarSourceKind.NONE,
            importable=False,
        )
    uri = icon.uri
    normalized_uri = uri.lower()
    if normalized_uri.startswith("data:"):
        return CharacterCardAvatarPreview(
            source_kind=AvatarSourceKind.DATA_URI,
            importable=True,
            asset_path=icon.path,
        )
    if normalized_uri.startswith(("http://", "https://")):
        kind = AvatarSourceKind.REMOTE_URL
    elif normalized_uri.startswith("embeded://"):
        kind = AvatarSourceKind.EMBEDDED_REFERENCE
    else:
        kind = AvatarSourceKind.DEFAULT
    return CharacterCardAvatarPreview(
        source_kind=kind,
        importable=False,
        asset_path=icon.path,
    )


def _decode_data_uri(uri: str) -> bytes:
    header, separator, encoded = uri.partition(",")
    if (
        not separator
        or not header.lower().endswith(";base64")
        or not encoded
    ):
        raise InvalidCharacterCardAvatar(
            "Embedded avatar data must be a base64 data URI"
        )
    # Reject before allocating the decoded result. The upper bound is at most
    # two bytes above the exact result because of base64 padding.
    decoded_upper_bound = ((len(encoded) + 3) // 4) * 3
    if decoded_upper_bound > MAX_IMPORTED_AVATAR_BYTES + 2:
        raise InvalidCharacterCardAvatar(
            "Embedded avatar exceeds the imported-avatar byte limit"
        )
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise InvalidCharacterCardAvatar(
            "Embedded avatar is not valid base64"
        ) from exc
    if len(decoded) > MAX_IMPORTED_AVATAR_BYTES:
        raise InvalidCharacterCardAvatar(
            "Embedded avatar exceeds the imported-avatar byte limit"
        )
    return decoded


def extract_avatar_bytes(
    parsed: ParsedCharacterCard,
    source_payload: bytes,
) -> bytes | None:
    """Return card-owned avatar bytes; never dereference an asset URI."""

    preview = describe_avatar_source(parsed)
    if preview.source_kind == AvatarSourceKind.PNG_CONTAINER:
        avatar = source_payload
    elif preview.source_kind == AvatarSourceKind.DATA_URI:
        icon = _selected_icon(parsed)
        assert icon is not None
        avatar = _decode_data_uri(icon.uri)
    else:
        return None
    if not avatar:
        raise InvalidCharacterCardAvatar("Embedded avatar is empty")
    if len(avatar) > MAX_IMPORTED_AVATAR_BYTES:
        raise InvalidCharacterCardAvatar(
            "Embedded avatar exceeds the imported-avatar byte limit"
        )
    return avatar


class CharacterCardAvatarImportService:
    """Move one reviewed card avatar into the existing managed-image store."""

    def __init__(
        self,
        *,
        proposals: AppliedAvatarProposalGatewayProtocol | None = None,
        writer: ImageAssetWriterProtocol | None = None,
    ) -> None:
        self.proposals = proposals or MongoAppliedAvatarProposalGateway()
        self.writer = writer or ManagedImageAssetService()

    @staticmethod
    def _parse_source(
        source_payload: bytes,
        *,
        declared_mime: str,
    ) -> ParsedCharacterCard:
        mime = declared_mime.partition(";")[0].strip().lower()
        if mime == "image/png":
            return CharacterCardAdapter.parse_png(
                source_payload,
                declared_mime=mime,
            )
        if mime == "application/json":
            return CharacterCardAdapter.parse_json(
                source_payload,
                declared_mime=mime,
            )
        raise InvalidCharacterCardAvatar(
            "Avatar source must be a Character Card PNG or JSON file"
        )

    async def import_applied_source(
        self,
        *,
        proposal_id: str,
        owner_id: str,
        declared_mime: str,
        source_payload: bytes,
    ) -> CharacterCardAvatarImportResult:
        try:
            proposal_object_id = to_object_id(proposal_id)
            owner_object_id = to_object_id(owner_id)
        except Exception as exc:
            raise CharacterCardAvatarNotFound(
                "Applied card import proposal not found"
            ) from exc
        proposal = await self.proposals.get_applied_owned(
            proposal_id=proposal_object_id,
            owner_id=owner_object_id,
        )
        if proposal is None:
            raise CharacterCardAvatarNotFound(
                "Applied card import proposal not found"
            )

        source_hash = hashlib.sha256(source_payload).hexdigest()
        if not hmac.compare_digest(
            source_hash,
            str(proposal.get("source_hash") or ""),
        ):
            raise InvalidCharacterCardAvatar(
                "Avatar bytes do not match the reviewed source file"
            )
        parsed = self._parse_source(
            source_payload,
            declared_mime=declared_mime,
        )
        if parsed.source_container != proposal.get("source_container"):
            raise InvalidCharacterCardAvatar(
                "Avatar source container does not match the reviewed proposal"
            )
        preview = describe_avatar_source(parsed)
        if proposal.get("avatar_preview") != preview.as_dict():
            raise InvalidCharacterCardAvatar(
                "Avatar transfer details changed; preview the card again"
            )

        mappings = (proposal.get("apply_result") or {}).get("mappings") or []
        mapping = next(
            (
                item
                for item in mappings
                if isinstance(item, dict)
                and item.get("candidate_id") == "character:0"
            ),
            None,
        )
        if mapping is None:
            raise InvalidCharacterCardAvatar(
                "Applied proposal has no character mapping"
            )
        if mapping.get("action") == "skip":
            return CharacterCardAvatarImportResult(
                status="skipped",
                source_kind=preview.source_kind,
            )
        card_id_text = str(mapping.get("card_id") or "")
        if not card_id_text:
            raise InvalidCharacterCardAvatar(
                "Applied character mapping has no formal card id"
            )
        if not preview.importable:
            return CharacterCardAvatarImportResult(
                status="not_imported",
                source_kind=preview.source_kind,
            )

        novel_id = proposal.get("novel_id")
        if novel_id is None:
            raise InvalidCharacterCardAvatar(
                "Applied proposal is not bound to a novel"
            )
        try:
            novel_object_id = to_object_id(novel_id)
            card_object_id = to_object_id(card_id_text)
        except Exception as exc:
            raise InvalidCharacterCardAvatar(
                "Applied character mapping contains an invalid id"
            ) from exc
        character = await self.proposals.get_character(
            novel_id=novel_object_id,
            card_id=card_object_id,
        )
        if character is None:
            raise CharacterCardAvatarNotFound(
                "Mapped character card not found"
            )

        avatar = extract_avatar_bytes(
            parsed,
            source_payload,
        )
        assert avatar is not None
        try:
            record: ImageAssetRecord = await self.writer.put(
                content=avatar,
                command=ImportedImageAssetCreate(
                    owner_id=str(owner_object_id),
                    novel_id=str(novel_object_id),
                    subject_kind="character_portrait",
                    subject_id=str(card_object_id),
                ),
            )
        except InvalidImageAssetError as exc:
            raise InvalidCharacterCardAvatar(
                "Embedded avatar is not a valid supported image"
            ) from exc
        return CharacterCardAvatarImportResult(
            status="imported",
            source_kind=preview.source_kind,
            asset_id=record.asset_id,
            content_hash=record.content_hash,
        )


character_card_avatar_import_service = CharacterCardAvatarImportService()


__all__ = [
    "AvatarSourceKind",
    "CharacterCardAvatarPreview",
    "CharacterCardAvatarImportResult",
    "CharacterCardAvatarImportService",
    "CharacterCardAvatarNotFound",
    "InvalidCharacterCardAvatar",
    "MAX_IMPORTED_AVATAR_BYTES",
    "describe_avatar_source",
    "extract_avatar_bytes",
    "character_card_avatar_import_service",
]
