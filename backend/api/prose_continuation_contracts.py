"""Shared HTTP contracts for bounded per-scene prose continuation."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from backend.services.generation.prose_continuation import ProseContinuationPolicy


class ProseContinuationPolicyRequest(BaseModel):
    """The same strict, user-facing controls apply to single and batch runs."""

    model_config = ConfigDict(extra="forbid")

    automatic_continuations_per_scene: int = Field(default=0, ge=0, le=15)
    continuation_target_words: int = Field(default=1_000, ge=400, le=5_000)

    def to_domain(self) -> ProseContinuationPolicy:
        return ProseContinuationPolicy(
            automatic_continuations_per_scene=(
                self.automatic_continuations_per_scene
            ),
            continuation_target_words=self.continuation_target_words,
        )
