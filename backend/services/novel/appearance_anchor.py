"""Bounded, image-only appearance anchors for character reference cards."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)


APPEARANCE_ANCHOR_DESCRIPTOR_CHARACTER_LIMIT = 1_200
APPEARANCE_ANCHOR_TOTAL_CHARACTER_LIMIT = 16_000
APPEARANCE_ANCHOR_RESET_WARNING = "这会让后续插图与已有插图不一致"

RUNTIME_PACKAGE_LIMIT = 128
RUNTIME_DEVICE_LIMIT = 8
RUNTIME_CHECKPOINT_LIMIT = 32
RUNTIME_LORA_LIMIT = 64

RuntimeDevice = Annotated[str, Field(max_length=300)]
RuntimeCheckpointName = Annotated[str, Field(max_length=500)]
RuntimeLoraName = Annotated[str, Field(max_length=500)]
ReferenceMode = Literal[
    "none",
    "img2img",
    "controlnet",
    "style_reference",
    "edit_model",
]

_CONTENT_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_WORKFLOW_REVISION_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


class AppearanceAnchorResetConfirmationRequired(ValueError):
    """An existing visual identity may only be replaced after user confirmation."""


class AppearanceAnchorConflictError(RuntimeError):
    """The stored anchor changed after the caller captured its reset baseline."""


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_text_list(value: Any) -> Any:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        return value
    return list(
        dict.fromkeys(
            text
            for item in value
            if (text := _normalize_text(item))
        )
    )


class RuntimePackageVersionSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    version: str = Field(default="unknown", min_length=1, max_length=120)

    @field_validator("name", "version", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return _normalize_text(value)


class RuntimeFingerprintSchema(BaseModel):
    """Sanitized deployment facts used only to warn about visual drift."""

    model_config = ConfigDict(extra="forbid")

    comfyui_version: str = Field(default="", max_length=120)
    pytorch_version: str = Field(default="", max_length=120)
    package_versions: list[RuntimePackageVersionSchema] = Field(
        default_factory=list,
        max_length=RUNTIME_PACKAGE_LIMIT,
    )
    devices: list[RuntimeDevice] = Field(
        default_factory=list,
        max_length=RUNTIME_DEVICE_LIMIT,
    )
    precision: str = Field(default="unknown", min_length=1, max_length=200)
    checkpoint_names: list[RuntimeCheckpointName] = Field(
        default_factory=list,
        max_length=RUNTIME_CHECKPOINT_LIMIT,
    )
    lora_names: list[RuntimeLoraName] = Field(
        default_factory=list,
        max_length=RUNTIME_LORA_LIMIT,
    )
    workflow_graph_hash: str = Field(default="", max_length=71)

    @field_validator(
        "comfyui_version",
        "pytorch_version",
        "precision",
        "workflow_graph_hash",
        mode="before",
    )
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return _normalize_text(value)

    @field_validator("workflow_graph_hash", mode="after")
    @classmethod
    def validate_workflow_graph_hash(cls, value: str) -> str:
        if value and _WORKFLOW_REVISION_PATTERN.fullmatch(value) is None:
            raise ValueError(
                "workflow_graph_hash must be empty or sha256:<64 lowercase hex>"
            )
        return value

    @field_validator(
        "devices",
        "checkpoint_names",
        "lora_names",
        mode="before",
    )
    @classmethod
    def normalize_text_lists(cls, value: Any) -> Any:
        return _normalize_text_list(value)

    @field_validator(
        "devices",
        "checkpoint_names",
        "lora_names",
        mode="before",
    )
    @classmethod
    def enforce_raw_list_limits(
        cls,
        value: Any,
        info: ValidationInfo,
    ) -> Any:
        limits = {
            "devices": RUNTIME_DEVICE_LIMIT,
            "checkpoint_names": RUNTIME_CHECKPOINT_LIMIT,
            "lora_names": RUNTIME_LORA_LIMIT,
        }
        if isinstance(value, (list, tuple)) and len(value) > limits[info.field_name]:
            raise ValueError(
                f"{info.field_name} contains too many runtime fingerprint entries"
            )
        return _normalize_text_list(value)

    @model_validator(mode="after")
    def deduplicate_package_versions(self) -> "RuntimeFingerprintSchema":
        unique: list[RuntimePackageVersionSchema] = []
        seen: set[tuple[str, str]] = set()
        for item in self.package_versions:
            key = (item.name, item.version)
            if key not in seen:
                seen.add(key)
                unique.append(item)
        self.package_versions = unique
        return self


def _string_character_total(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, BaseModel):
        return _string_character_total(value.model_dump(mode="python"))
    if isinstance(value, dict):
        return sum(_string_character_total(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return sum(_string_character_total(item) for item in value)
    return 0


class AppearanceAnchorSchema(BaseModel):
    """Frozen image identity; deliberately separate from prose context fields."""

    model_config = ConfigDict(extra="forbid")

    descriptor: str = Field(
        min_length=1,
        max_length=APPEARANCE_ANCHOR_DESCRIPTOR_CHARACTER_LIMIT,
    )
    # BSON integers are signed int64. Keep the full uint64 seed exact as its
    # canonical decimal representation in every persisted anchor.
    seed: str
    reference_asset: str
    established_at: datetime
    provider: str = Field(min_length=1, max_length=200)
    model: str = Field(min_length=1, max_length=500)
    workflow_revision: str
    reference_mode: ReferenceMode
    runtime_fingerprint: RuntimeFingerprintSchema

    @field_validator("descriptor", "provider", "model", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return _normalize_text(value)

    @field_validator("seed", mode="before")
    @classmethod
    def validate_seed(cls, value: Any) -> str:
        if isinstance(value, bool):
            raise ValueError("seed must be an integer from 0 through 2^64-1")
        if isinstance(value, int):
            parsed = value
        elif (
            isinstance(value, str)
            and value
            and value.isascii()
            and value.isdecimal()
            and (value == "0" or not value.startswith("0"))
        ):
            parsed = int(value)
        else:
            raise ValueError("seed must be an integer from 0 through 2^64-1")
        if not 0 <= parsed <= (2**64 - 1):
            raise ValueError("seed must be an integer from 0 through 2^64-1")
        return str(parsed)

    @field_validator("established_at", mode="after")
    @classmethod
    def normalize_established_at(cls, value: datetime) -> datetime:
        normalized = value
        if normalized.tzinfo is not None:
            normalized = normalized.astimezone(timezone.utc).replace(tzinfo=None)
        # BSON datetimes round-trip at millisecond precision and are decoded
        # without tzinfo by the project's Mongo client. Normalize before CAS.
        return normalized.replace(
            microsecond=(normalized.microsecond // 1000) * 1000
        )

    @field_validator("reference_asset", mode="before")
    @classmethod
    def validate_reference_asset(cls, value: Any) -> str:
        normalized = _normalize_text(value)
        if _CONTENT_HASH_PATTERN.fullmatch(normalized) is None:
            raise ValueError(
                "reference_asset must be a 64-character lowercase SHA-256 hash"
            )
        return normalized

    @field_validator("workflow_revision", mode="before")
    @classmethod
    def validate_workflow_revision(cls, value: Any) -> str:
        normalized = _normalize_text(value)
        if _WORKFLOW_REVISION_PATTERN.fullmatch(normalized) is None:
            raise ValueError(
                "workflow_revision must use sha256:<64 lowercase hex>"
            )
        return normalized

    @model_validator(mode="after")
    def enforce_total_limit(self) -> "AppearanceAnchorSchema":
        if _string_character_total(self) > APPEARANCE_ANCHOR_TOTAL_CHARACTER_LIMIT:
            raise ValueError(
                "Appearance anchor exceeds the total character limit"
            )
        return self


def normalize_appearance_anchor(value: Any) -> dict[str, Any]:
    """Validate and return the stable persistence representation."""

    return AppearanceAnchorSchema.model_validate(value).model_dump(mode="python")


def require_appearance_anchor_reset_confirmation(
    current_anchor: Any,
    *,
    confirmed: bool,
) -> None:
    """Require an explicit acknowledgement before replacing an existing anchor."""

    if current_anchor is not None and not confirmed:
        raise AppearanceAnchorResetConfirmationRequired(
            APPEARANCE_ANCHOR_RESET_WARNING
        )
