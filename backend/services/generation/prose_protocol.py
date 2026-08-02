"""Stable identities and compatibility predicates for prose execution protocols."""
from __future__ import annotations

from typing import Any


SCENE_CONTINUATION_V3_PROTOCOL_REVISION = "scene-continuation-v3"
CURRENT_SCENE_CONTINUATION_PROTOCOL_REVISION = (
    f"{SCENE_CONTINUATION_V3_PROTOCOL_REVISION}.1"
)


def is_scene_continuation_v3_family(revision: Any) -> bool:
    """Return whether a persisted revision uses the v3 per-scene executor."""
    normalized = str(revision or "").strip()
    return (
        normalized == SCENE_CONTINUATION_V3_PROTOCOL_REVISION
        or normalized.startswith(f"{SCENE_CONTINUATION_V3_PROTOCOL_REVISION}.")
    )
