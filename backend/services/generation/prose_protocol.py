"""Stable identities and compatibility predicates for prose execution protocols."""
from __future__ import annotations

import math
from typing import Any


SCENE_CONTINUATION_V3_PROTOCOL_REVISION = "scene-continuation-v3"
CURRENT_SCENE_CONTINUATION_PROTOCOL_REVISION = (
    f"{SCENE_CONTINUATION_V3_PROTOCOL_REVISION}.2"
)

# These two constants are part of the v3 continuation protocol, not a user
# control. Keep their definition here so dispatch, readiness and protocol
# identity move together when the bounded seam window changes.
SEAM_TAIL_MIN_CHARACTERS = 2_000
SEAM_SCENE_COVERAGE_FACTOR = 1.5


def scene_continuation_seam_window_characters(scene_target_words: Any) -> int:
    """Return the bounded tail window for one logical scene.

    The window intentionally tracks the whole scene target rather than an
    individual base-part or continuation output target. That gives a resumed
    call enough local history to preserve continuity while retaining an
    explicit upper bound when a scene over-writes its target.
    """
    try:
        target_words = max(0, int(scene_target_words))
    except (TypeError, ValueError):
        target_words = 0
    return max(
        SEAM_TAIL_MIN_CHARACTERS,
        math.ceil(target_words * SEAM_SCENE_COVERAGE_FACTOR),
    )


def is_scene_continuation_v3_family(revision: Any) -> bool:
    """Return whether a persisted revision uses the v3 per-scene executor."""
    normalized = str(revision or "").strip()
    return (
        normalized == SCENE_CONTINUATION_V3_PROTOCOL_REVISION
        or normalized.startswith(f"{SCENE_CONTINUATION_V3_PROTOCOL_REVISION}.")
    )
