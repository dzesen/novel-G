"""Bounded generation options for the chapter-outline workflow."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


# Reasoning providers count thinking and visible JSON against one output limit.
# Keep the visible outline/context limits separate from this bounded call budget.
CHAPTER_OUTLINE_MAX_OUTPUT_TOKENS = 64_000
CHAPTER_OUTLINE_CONTEXT_TOKEN_BUDGET = 20_000


def chapter_outline_generation_kwargs(
    generation_kwargs: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Apply the chapter-outline output ceiling without widening smaller limits."""

    result = {
        key: value
        for key, value in dict(generation_kwargs or {}).items()
        if value is not None
    }
    requested = result.get("max_tokens")
    if isinstance(requested, bool) or not isinstance(requested, int):
        requested = None
    result["max_tokens"] = (
        min(requested, CHAPTER_OUTLINE_MAX_OUTPUT_TOKENS)
        if requested is not None and requested > 0
        else CHAPTER_OUTLINE_MAX_OUTPUT_TOKENS
    )
    return result
