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
    f"{SCENE_CONTINUATION_V3_PROTOCOL_REVISION}.10"
)
SCENE_EVIDENCE_FIRST_COMPLETION_PROTOCOL_REVISION = (
    CURRENT_SCENE_CONTINUATION_PROTOCOL_REVISION
)
SCENE_EVIDENCE_FIRST_COMPLETION_PROTOCOL_REVISIONS = frozenset({
    f"{SCENE_CONTINUATION_V3_PROTOCOL_REVISION}.9",
    SCENE_EVIDENCE_FIRST_COMPLETION_PROTOCOL_REVISION,
})
SEMANTIC_SCENE_DISPATCH_PROTOCOL_REVISION = (
    CURRENT_SCENE_CONTINUATION_PROTOCOL_REVISION
)

# v3.4--v3.9 capped every V2 base request at 600 words.  Retain the value only
# as historical protocol documentation; v3.10 uses the Provider-derived output
# capacity as a resource ceiling and no longer turns it into narrative slices.
LEGACY_MAX_V2_SCENE_BASE_CALL_TARGET_WORDS = 600

# This constant is part of the v3 continuation protocol, not a user control.
# Keep it here so dispatch, readiness and protocol identity move together when
# the fixed seam window changes.
SEAM_TAIL_MIN_CHARACTERS = 2_000

# Automatic/manual continuation calls are appended after planned base-call
# sequences without renumbering persisted base segments.
AUTOMATIC_PROSE_SEQUENCE_FLOOR = 1_000_000


def v2_scene_base_call_safe_output_budget(safe_output_budget: Any) -> int:
    """Normalize the Provider-derived v3.10 capacity for one prose call.

    This value is a resource ceiling.  It must never be presented to the model
    as a required narrative chunk size.
    """

    try:
        parsed = int(safe_output_budget)
    except (TypeError, ValueError):
        parsed = 1
    return max(1, parsed)


def maximum_v2_chapter_base_calls(
    *,
    safe_output_budget: Any,
    target_word_count: int = MAX_CHAPTER_OUTLINE_TARGET_WORDS,
    scene_count: int = MAX_CHAPTER_OUTLINE_SCENES,
) -> int:
    """Bound every legal positive V2 scene partition for one chapter.

    For positive scene targets with a fixed sum, the conservative bound is
    ``ceil(total / effective_call_budget) + scene_count - 1``.  The effective
    call budget remains Provider-specific, so readiness reserves every
    possible length-only continuation without exposing that partition to the
    model as a narrative requirement.
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
    """Return the fixed v3.9+ tail window for every logical scene.

    ``scene_target_words`` remains part of this stable call boundary because
    readiness and the executor share it, but v3.9+ intentionally does not
    derive model-visible context from the target.  The prior v3.2 expansion
    added cost without an observed benefit in the four-cell retest.
    """
    del scene_target_words
    return SEAM_TAIL_MIN_CHARACTERS


def uses_scene_evidence_first_completion(
    *,
    protocol_revision: Any,
    plan_reason_codes: Any,
) -> bool:
    """Return whether V2 scene lengths are advisory for this exact plan.

    The reason-code check prevents legacy outlines, which have no required-beat
    evidence contract, from losing their historical anti-truncation floor.
    v3.9 and v3.10 share the evidence-first completion rule.  Keeping both
    explicit prevents a protocol bump from silently restoring the historical
    word-count gate when old candidates are audited.
    """

    try:
        reasons = {str(item) for item in plan_reason_codes}
    except TypeError:
        return False
    return bool(
        str(protocol_revision or "")
        in SCENE_EVIDENCE_FIRST_COMPLETION_PROTOCOL_REVISIONS
        and "scene_contract_word_budgets" in reasons
    )


def uses_semantic_scene_dispatch(
    *,
    protocol_revision: Any,
    plan_reason_codes: Any,
) -> bool:
    """Return whether initial prose is dispatched by semantic scene.

    Only v3.10 plans get this behavior.  Persisted v3.9 plans keep their exact
    600-word base-part identities and remain deterministically replayable.
    """

    try:
        reasons = {str(item) for item in plan_reason_codes}
    except TypeError:
        return False
    return bool(
        str(protocol_revision or "")
        == SEMANTIC_SCENE_DISPATCH_PROTOCOL_REVISION
        and "scene_contract_semantic_scene_calls" in reasons
        and "scene_contract_word_budgets" in reasons
    )


def is_scene_continuation_v3_family(revision: Any) -> bool:
    """Return whether a persisted revision uses the v3 per-scene executor."""
    normalized = str(revision or "").strip()
    return (
        normalized == SCENE_CONTINUATION_V3_PROTOCOL_REVISION
        or normalized.startswith(f"{SCENE_CONTINUATION_V3_PROTOCOL_REVISION}.")
    )
