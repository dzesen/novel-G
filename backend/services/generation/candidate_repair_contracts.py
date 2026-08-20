"""Small shared contracts for the bounded chapter-candidate repair tail."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


MAX_CHAPTER_CANDIDATE_REPAIR_CYCLES = 8


class PreDispatchFenceV1(BaseModel):
    """One bounded receipt lease mirrored into the GenerationJob ledger."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["state_repair_pre_dispatch_fence.v1"] = (
        "state_repair_pre_dispatch_fence.v1"
    )
    receipt_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    claim_token: str = Field(min_length=1, max_length=128)
    claim_epoch: int = Field(ge=1, le=1_000_000)
    cycle: int = Field(ge=1, le=MAX_CHAPTER_CANDIDATE_REPAIR_CYCLES)

    @property
    def step_id(self) -> str:
        return f"candidate-state-repair:{self.cycle}"


class StateContextProjection(BaseModel):
    """Metadata-only context truncation evidence persisted before dispatch."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["state_context_projection.v1"] = (
        "state_context_projection.v1"
    )
    truncated_section_count: int = Field(ge=0, le=100)
    dropped_item_count: int = Field(ge=0, le=10_000)


def project_state_context(
    *,
    truncated_sections: Sequence[Any],
    dropped_item_counts: Mapping[str, Any],
) -> StateContextProjection:
    """Project typed context metadata without retaining names or prose."""
    dropped = 0
    for value in list(dropped_item_counts.values())[:100]:
        if type(value) is not int or value < 0:
            raise ValueError("state context dropped-item evidence is invalid")
        dropped = min(10_000, dropped + value)
    return StateContextProjection(
        truncated_section_count=min(100, len(truncated_sections)),
        dropped_item_count=dropped,
    )
