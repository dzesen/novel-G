"""Pure v3 prose output-bound calculations shared by execution and readiness."""
from __future__ import annotations

import json
import math
from typing import Any


TOKENS_PER_WORD_ESTIMATE = 0.65
MIN_DERIVED_OUTPUT_TOKENS = 256
# V26 reserves a bounded tail after the nominal word target so the Provider
# can emit its natural stop instead of being cut off exactly at the estimate.
# The scene word maximum remains a separate deterministic local gate.
NATURAL_STOP_HEADROOM_RATIO = 0.25
MIN_NATURAL_STOP_HEADROOM_TOKENS = 256
# The rendered prompt is counted in UTF-8 bytes, while this fixed allowance
# covers Provider framing that is not present in the rendered text.  Keeping
# it here makes the readiness calculation and the dispatch-time reservation
# share one source of truth.
PROVIDER_SYSTEM_FRAMING_TOKEN_ALLOWANCE = 1_024


def structured_schema_request_payload(schema: Any) -> str:
    """Serialize the exact response-schema projection sent beside a prompt."""
    projection = schema.model_json_schema()
    return json.dumps(
        projection,
        ensure_ascii=True,
        sort_keys=True,
    )


def positive_token_limit(value: Any) -> int | None:
    """Normalize only explicit positive integer token limits."""
    if isinstance(value, (bool, float)):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def conservative_prompt_input_bound(
    *,
    prompt: str,
    system_prompt: str = "",
    additional_request_payload: str = "",
) -> int:
    """Return the input portion of one conservative Provider reservation."""
    return max(
        1,
        len(str(prompt or "").encode("utf-8"))
        + len(str(system_prompt or "").encode("utf-8"))
        + len(str(additional_request_payload or "").encode("utf-8"))
        + PROVIDER_SYSTEM_FRAMING_TOKEN_ALLOWANCE,
    )


def conservative_runtime_token_bound(
    *,
    output_token_bound: Any,
    prompt: str,
    system_prompt: str = "",
    additional_request_payload: str = "",
) -> int | None:
    """Return the exact conservative formula used before Provider dispatch."""
    output_limit = positive_token_limit(output_token_bound)
    if output_limit is None:
        return None
    return max(
        1,
        int(output_limit)
        + conservative_prompt_input_bound(
            prompt=prompt,
            system_prompt=system_prompt,
            additional_request_payload=additional_request_payload,
        ),
    )


def v3_output_token_bound(
    *,
    target_words: Any,
    inherited_max_tokens: Any,
) -> int:
    """Return the v3.6 output cap with bounded natural-stop headroom."""
    try:
        normalized_target = max(1, int(target_words))
    except (TypeError, ValueError):
        normalized_target = 1
    nominal = max(
        MIN_DERIVED_OUTPUT_TOKENS,
        math.ceil(normalized_target / TOKENS_PER_WORD_ESTIMATE),
    )
    headroom = max(
        MIN_NATURAL_STOP_HEADROOM_TOKENS,
        math.ceil(nominal * NATURAL_STOP_HEADROOM_RATIO),
    )
    derived = nominal + headroom
    inherited = positive_token_limit(inherited_max_tokens)
    return derived if inherited is None else min(inherited, derived)
