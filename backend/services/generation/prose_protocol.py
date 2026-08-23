"""Stable identities and compatibility predicates for prose execution protocols."""
from __future__ import annotations

from typing import Any


SCENE_CONTINUATION_V3_PROTOCOL_REVISION = "scene-continuation-v3"
CURRENT_SCENE_CONTINUATION_PROTOCOL_REVISION = (
    f"{SCENE_CONTINUATION_V3_PROTOCOL_REVISION}.3"
)

# This constant is part of the v3 continuation protocol, not a user control.
# Keep it here so dispatch, readiness and protocol identity move together when
# the fixed seam window changes.
SEAM_TAIL_MIN_CHARACTERS = 2_000

# Automatic/manual continuation calls are appended after planned base-call
# sequences without renumbering persisted base segments.
AUTOMATIC_PROSE_SEQUENCE_FLOOR = 1_000_000


def scene_continuation_seam_window_characters(scene_target_words: Any) -> int:
    """Return the fixed v3.3 tail window for every logical scene.

    ``scene_target_words`` remains part of this stable call boundary because
    readiness and the executor share it, but v3.3 intentionally does not
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
