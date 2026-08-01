"""Closed lineage contracts for staged scene-illustration work."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from backend.db.utils import to_object_id


IllustrationPipelineStage = Literal[
    "compose",
    "identity_edit",
    "refine",
    "external_import",
]
IllustrationCandidateState = Literal[
    "available",
    "selected",
    "discarded",
    "finalized",
]

_CONTENT_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _canonical_required_object_id(value: Any, *, field: str) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError(f"{field} is required")
    try:
        return str(to_object_id(value))
    except Exception as exc:
        raise ValueError(f"{field} must be an existing ObjectId") from exc


def _canonical_optional_object_id(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        raise ValueError(f"{field} must not be blank")
    try:
        return str(to_object_id(value))
    except Exception as exc:
        raise ValueError(f"{field} must be an existing ObjectId") from exc


def _canonical_optional_hash(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized.startswith("sha256:"):
        normalized = normalized.removeprefix("sha256:")
    if not _CONTENT_HASH_PATTERN.fullmatch(normalized):
        raise ValueError(f"{field} must be a SHA-256 content hash")
    return normalized


class IllustrationJobLineage(BaseModel):
    """Immutable stage identity plus the material required for idempotency."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    illustration_brief_id: str
    illustration_run_id: str
    pipeline_stage: IllustrationPipelineStage
    parent_asset_id: str | None = None
    reference_asset_hash: str | None = None
    base_asset_hash: str | None = None
    profile_revision: str = Field(min_length=1, max_length=500)
    prompt_revision: str = Field(min_length=1, max_length=500)

    @field_validator(
        "illustration_brief_id",
        "illustration_run_id",
        mode="before",
    )
    @classmethod
    def validate_required_ids(cls, value: Any, info: ValidationInfo) -> str:
        return _canonical_required_object_id(value, field=info.field_name)

    @field_validator("parent_asset_id", mode="before")
    @classmethod
    def validate_parent_asset_id(cls, value: Any) -> str | None:
        return _canonical_optional_object_id(value, field="parent_asset_id")

    @field_validator(
        "reference_asset_hash",
        "base_asset_hash",
        mode="before",
    )
    @classmethod
    def validate_hashes(cls, value: Any, info: ValidationInfo) -> str | None:
        return _canonical_optional_hash(value, field=info.field_name)

    @field_validator("profile_revision", "prompt_revision", mode="before")
    @classmethod
    def normalize_revisions(cls, value: Any) -> str:
        return str(value or "").strip()

    @model_validator(mode="after")
    def validate_stage_inputs(self) -> "IllustrationJobLineage":
        if self.pipeline_stage == "compose":
            if (
                self.parent_asset_id is not None
                or self.base_asset_hash is not None
            ):
                raise ValueError("compose must not declare a base asset")
            if self.reference_asset_hash is None:
                raise ValueError("compose requires reference_asset_hash")
            return self
        if self.parent_asset_id is None:
            raise ValueError(
                f"{self.pipeline_stage} requires parent_asset_id"
            )
        if self.base_asset_hash is None:
            raise ValueError(
                f"{self.pipeline_stage} requires base_asset_hash"
            )
        if (
            self.pipeline_stage == "identity_edit"
            and self.reference_asset_hash is None
        ):
            raise ValueError("identity_edit requires reference_asset_hash")
        return self

    def persisted_fields(self) -> dict[str, str]:
        fields = {
            "illustration_brief_id": self.illustration_brief_id,
            "illustration_run_id": self.illustration_run_id,
            "pipeline_stage": self.pipeline_stage,
        }
        if self.parent_asset_id is not None:
            fields["parent_asset_id"] = self.parent_asset_id
        return fields

    def idempotency_fields(self) -> dict[str, str | None]:
        return {
            **self.persisted_fields(),
            "parent_asset_id": self.parent_asset_id,
            "reference_asset_hash": self.reference_asset_hash,
            "base_asset_hash": self.base_asset_hash,
            "profile_revision": self.profile_revision,
            "prompt_revision": self.prompt_revision,
        }


class IllustrationAssetLineage(BaseModel):
    """Immutable business lineage attached when a staged asset is created."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    illustration_brief_id: str
    illustration_run_id: str
    pipeline_stage: IllustrationPipelineStage
    derived_from_asset_id: str | None = None

    @field_validator(
        "illustration_brief_id",
        "illustration_run_id",
        mode="before",
    )
    @classmethod
    def validate_required_ids(cls, value: Any, info: ValidationInfo) -> str:
        return _canonical_required_object_id(value, field=info.field_name)

    @field_validator("derived_from_asset_id", mode="before")
    @classmethod
    def validate_parent_asset_id(cls, value: Any) -> str | None:
        return _canonical_optional_object_id(
            value,
            field="derived_from_asset_id",
        )

    @model_validator(mode="after")
    def validate_stage_parent(self) -> "IllustrationAssetLineage":
        if self.pipeline_stage == "compose":
            if self.derived_from_asset_id is not None:
                raise ValueError("compose asset must not declare a parent")
            return self
        if self.derived_from_asset_id is None:
            raise ValueError(
                f"{self.pipeline_stage} requires derived_from_asset_id"
            )
        return self

    def persisted_fields(self) -> dict[str, str]:
        fields = {
            "illustration_brief_id": self.illustration_brief_id,
            "illustration_run_id": self.illustration_run_id,
            "pipeline_stage": self.pipeline_stage,
        }
        if self.derived_from_asset_id is not None:
            fields["derived_from_asset_id"] = self.derived_from_asset_id
        return fields
