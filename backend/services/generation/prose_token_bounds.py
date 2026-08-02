"""Pure v3 prose output-bound calculations shared by execution and readiness."""
from __future__ import annotations

import math
from typing import Any


TOKENS_PER_WORD_ESTIMATE = 0.65
MIN_DERIVED_OUTPUT_TOKENS = 256


def positive_token_limit(value: Any) -> int | None:
    """Normalize only explicit positive integer token limits."""
    if isinstance(value, (bool, float)):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def v3_output_token_bound(
    *,
    target_words: Any,
    inherited_max_tokens: Any,
) -> int:
    """Return the actual v3 output cap for one call without widening user intent."""
    try:
        normalized_target = max(1, int(target_words))
    except (TypeError, ValueError):
        normalized_target = 1
    derived = max(
        MIN_DERIVED_OUTPUT_TOKENS,
        math.ceil(normalized_target / TOKENS_PER_WORD_ESTIMATE),
    )
    inherited = positive_token_limit(inherited_max_tokens)
    return derived if inherited is None else min(inherited, derived)
