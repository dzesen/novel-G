"""Shared, bounded contracts for one state-candidate repair directive."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.services.generation.candidate_repair_contracts import (
    MAX_CHAPTER_CANDIDATE_REPAIR_CYCLES,
)


MAX_STATE_REPAIR_CARD_IDS = 20
MAX_STATE_REPAIR_CARD_ID_LENGTH = 64
MAX_STATE_REPAIR_DROPPED_REFERENCES = 1_000

StateRepairReason = Literal[
    "consistency_conflict",
    "invalid_internal_reference",
]


class StateRepairDirective(BaseModel):
    """The single source of truth for bounded state-repair guidance."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    cycle: int = Field(ge=1, le=MAX_CHAPTER_CANDIDATE_REPAIR_CYCLES)
    reason_codes: tuple[StateRepairReason, ...] = Field(
        min_length=1,
        max_length=2,
    )
    consistency_issue_count: int = Field(
        ge=0,
        le=MAX_STATE_REPAIR_CARD_IDS,
    )
    affected_card_ids: tuple[str, ...] = Field(
        max_length=MAX_STATE_REPAIR_CARD_IDS,
    )
    dropped_reference_count: int = Field(
        ge=0,
        le=MAX_STATE_REPAIR_DROPPED_REFERENCES,
    )

    @field_validator("reason_codes", "affected_card_ids")
    @classmethod
    def validate_unique_items(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("state repair guidance cannot contain duplicates")
        return value

    @field_validator("affected_card_ids")
    @classmethod
    def validate_card_id_shape(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            not item or len(item) > MAX_STATE_REPAIR_CARD_ID_LENGTH
            for item in value
        ):
            raise ValueError("state repair card ids exceed the V1 bound")
        return value
