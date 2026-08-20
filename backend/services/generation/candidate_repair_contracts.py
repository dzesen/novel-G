"""Small shared contracts for the bounded chapter-candidate repair tail."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


MAX_CHAPTER_CANDIDATE_REPAIR_CYCLES = 8


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
