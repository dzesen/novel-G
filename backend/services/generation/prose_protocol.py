"""Stable identities and compatibility predicates for prose execution protocols."""
from __future__ import annotations

import math
from typing import Any

from backend.llm.schemas.novel_pydantic import (
    MAX_CHAPTER_OUTLINE_SCENES,
    MAX_CHAPTER_OUTLINE_TARGET_WORDS,
)


SCENE_CONTINUATION_V3_PROTOCOL_REVISION = "scene-continuation-v3"
CURRENT_SCENE_CONTINUATION_PROTOCOL_REVISION = (
    f"{SCENE_CONTINUATION_V3_PROTOCOL_REVISION}.7"
)

# V2 scene contracts carry an explicit hard maximum for the whole scene, but a
# single Provider call can materially overshoot its approximate word target.
# Keep each planned base request small enough to make that overrun recoverable;
# the scene executor still enforces the unchanged contract min/target/max.
MAX_V2_SCENE_BASE_CALL_TARGET_WORDS = 600

# This constant is part of the v3 continuation protocol, not a user control.
# Keep it here so dispatch, readiness and protocol identity move together when
# the fixed seam window changes.
SEAM_TAIL_MIN_CHARACTERS = 2_000

# Automatic/manual continuation calls are appended after planned base-call
# sequences without renumbering persisted base segments.
AUTOMATIC_PROSE_SEQUENCE_FLOOR = 1_000_000


def v2_scene_base_call_safe_output_budget(safe_output_budget: Any) -> int:
    """Apply the V2 per-call word ceiling to a Provider-derived safe budget."""

    try:
        parsed = int(safe_output_budget)
    except (TypeError, ValueError):
        parsed = 1
    return min(max(1, parsed), MAX_V2_SCENE_BASE_CALL_TARGET_WORDS)


def maximum_v2_chapter_base_calls(
    *,
    safe_output_budget: Any,
    target_word_count: int = MAX_CHAPTER_OUTLINE_TARGET_WORDS,
    scene_count: int = MAX_CHAPTER_OUTLINE_SCENES,
) -> int:
    """Bound every legal positive V2 scene partition for one chapter.

    For positive scene targets with a fixed sum, the conservative bound is
    ``ceil(total / effective_call_budget) + scene_count - 1``.  The effective
    call budget must remain Provider-specific: a model configured below the
    600-word seam needs more calls than the default 103-call ceiling.
    """

    if (
        type(target_word_count) is not int
        or not 1 <= target_word_count <= MAX_CHAPTER_OUTLINE_TARGET_WORDS
    ):
        raise ValueError("V2 chapter target_word_count is outside its bound")
    if (
        type(scene_count) is not int
        or not 1 <= scene_count <= MAX_CHAPTER_OUTLINE_SCENES
        or scene_count > target_word_count
    ):
        raise ValueError("V2 chapter scene_count is outside its bound")
    effective_budget = v2_scene_base_call_safe_output_budget(
        safe_output_budget
    )
    return (
        math.ceil(target_word_count / effective_budget)
        + scene_count
        - 1
    )


def scene_continuation_seam_window_characters(scene_target_words: Any) -> int:
    """Return the fixed v3.7 tail window for every logical scene.

    ``scene_target_words`` remains part of this stable call boundary because
    readiness and the executor share it, but v3.7 intentionally does not
    derive model-visible context from the target.  The prior v3.2 expansion
    added cost without an observed benefit in the four-cell retest.
    """
    del scene_target_words
    return SEAM_TAIL_MIN_CHARACTERS


def is_scene_continuation_v3_family(revision: Any) -> bool:
    """Return whether a persisted revision uses the v3 per-scene executor."""
    normalized = str(revision or "").strip()
    return (
        normalized == SCENE_CONTINUATION_V3_PROTOCOL_REVISION
        or normalized.startswith(f"{SCENE_CONTINUATION_V3_PROTOCOL_REVISION}.")
    )
