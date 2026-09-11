"""Recover explicit user-discarded prose attempts without replaying them."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from backend.db.repositories.prose_run_repository import prose_run_repo
from backend.services.generation.prose_protocol import (
    AUTOMATIC_PROSE_SEQUENCE_FLOOR,
)


MAX_DISCARDED_CANDIDATE_RUNS = 200
DISCARD_RESOLVED_ATTEMPT_STATES = frozenset({
    "accounted", "uncertain_retry_acknowledged", "uncertain_skip_acknowledged",
})


def _strict_usage(value: Any) -> tuple[int, int, int] | None:
    if not isinstance(value, Mapping) or set(value) != {
        "input_tokens",
        "output_tokens",
        "total_tokens",
    }:
        return None
    values: list[int] = []
    for field in ("input_tokens", "output_tokens", "total_tokens"):
        component = value.get(field)
        if isinstance(component, bool) or not isinstance(component, int):
            return None
        if component < 0:
            return None
        values.append(component)
    return values[0], values[1], values[2]


def _ordered_segment_usages(
    run: Mapping[str, Any],
) -> tuple[str, tuple[tuple[bool, tuple[int, int, int]], ...]] | None:
    if run.get("status") != "discarded" or run.get("is_deleted") is True:
        return None
    raw_segments = run.get("segments")
    if (
        not isinstance(raw_segments, list)
        or not raw_segments
        or len(raw_segments) > 5_000
    ):
        return None
    segments: list[tuple[bool, tuple[int, int, int]]] = []
    order_keys: list[tuple[int, int, int]] = []
    seen_sequences: set[int] = set()
    for raw_segment in raw_segments:
        if not isinstance(raw_segment, Mapping):
            return None
        sequence = raw_segment.get("sequence_index")
        usage = _strict_usage(raw_segment.get("usage"))
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence < 0
            or sequence in seen_sequences
            or usage is None
        ):
            return None
        scene_index = raw_segment.get("scene_index", 0)
        call_index = raw_segment.get(
            "scene_call_index",
            raw_segment.get("part_index", 0),
        )
        scene_index = 0 if scene_index is None else scene_index
        call_index = 0 if call_index is None else call_index
        if (
            isinstance(scene_index, bool)
            or not isinstance(scene_index, int)
            or scene_index < 0
            or isinstance(call_index, bool)
            or not isinstance(call_index, int)
            or call_index < 0
        ):
            return None
        order_key = (scene_index, call_index, sequence)
        if order_keys and order_key <= order_keys[-1]:
            return None
        seen_sequences.add(sequence)
        order_keys.append(order_key)
        segments.append((raw_segment.get("status") == "uncertain", usage))
    base_sequences = sorted(
        sequence
        for sequence in seen_sequences
        if sequence < AUTOMATIC_PROSE_SEQUENCE_FLOOR
    )
    automatic_sequences = sorted(
        sequence
        for sequence in seen_sequences
        if sequence >= AUTOMATIC_PROSE_SEQUENCE_FLOOR
    )
    if base_sequences != list(range(len(base_sequences))):
        return None
    if automatic_sequences and automatic_sequences != list(range(
        AUTOMATIC_PROSE_SEQUENCE_FLOOR,
        AUTOMATIC_PROSE_SEQUENCE_FLOOR + len(automatic_sequences),
    )):
        return None
    provider_plan = run.get("provider_plan")
    provider_alias = (
        provider_plan.get("provider_alias")
        if isinstance(provider_plan, Mapping)
        else None
    )
    if not isinstance(provider_alias, str) or not provider_alias:
        return None
    return provider_alias, tuple(segments)


def match_discarded_candidate_attempt_ids(
    *,
    discarded_runs: Sequence[Mapping[str, Any]],
    attempt_slots: Sequence[Mapping[str, Any]],
    protected_attempt_ids: Sequence[str] = (),
) -> tuple[str, ...]:
    """Match discarded run segments to the uncheckpointed paid prose prefix.

    The match is deliberately exact and metadata-only.  Any ambiguity returns
    no resolution, leaving the existing missing-checkpoint guard in force.
    """

    protected = set(protected_attempt_ids)
    if len(protected) != len(tuple(protected_attempt_ids)):
        return ()
    candidate_slots: list[tuple[str, str, bool, tuple[int, int, int]]] = []
    ledger_attempt_ids: set[str] = set()
    protected_prose_seen = False
    for slot in attempt_slots:
        if not isinstance(slot, Mapping):
            return ()
        raw_attempt_id = slot.get("attempt_id")
        if isinstance(raw_attempt_id, str) and raw_attempt_id:
            if raw_attempt_id in ledger_attempt_ids:
                return ()
            ledger_attempt_ids.add(raw_attempt_id)
        if str(slot.get("step_id") or "") != "candidate-prose":
            continue
        state = str(slot.get("state") or "")
        if state not in DISCARD_RESOLVED_ATTEMPT_STATES:
            # An unresolved call cannot be skipped over to match later evidence.
            if state not in {"released_pre_dispatch"}:
                return ()
            continue
        attempt_id = raw_attempt_id
        provider_alias = slot.get("provider_alias")
        uncertain = state != "accounted"
        if uncertain:
            reserved = slot.get("conservative_tokens")
            if isinstance(reserved, bool) or not isinstance(reserved, int) or reserved <= 0:
                return ()
            # ProseRun persists the conservative hold as synthetic usage when
            # the provider returns no usage. Acknowledgement never refunds it.
            usage = (0, 0, reserved)
        else:
            usage = _strict_usage(slot.get("usage"))
        if (
            not isinstance(attempt_id, str)
            or not attempt_id
            or not isinstance(provider_alias, str)
            or not provider_alias
            or usage is None
        ):
            return ()
        if attempt_id in protected:
            protected_prose_seen = True
            continue
        if protected_prose_seen:
            return ()
        candidate_slots.append((attempt_id, provider_alias, uncertain, usage))

    if not candidate_slots or not discarded_runs:
        return ()
    if len(discarded_runs) > MAX_DISCARDED_CANDIDATE_RUNS:
        return ()

    resolved: list[str] = []
    offset = 0
    for run in discarded_runs:
        if not isinstance(run, Mapping):
            return ()
        projected = _ordered_segment_usages(run)
        if projected is None:
            return ()
        provider_alias, usages = projected
        window = candidate_slots[offset : offset + len(usages)]
        if len(window) != len(usages):
            return ()
        if any(
            slot_provider != provider_alias
            or slot_uncertain != segment_uncertain
            or slot_usage != segment_usage
            for (
                _attempt_id,
                slot_provider,
                slot_uncertain,
                slot_usage,
            ), (segment_uncertain, segment_usage) in zip(window, usages, strict=True)
        ):
            return ()
        resolved.extend(attempt_id for attempt_id, _provider, _uncertain, _usage in window)
        offset += len(window)
    return tuple(resolved)


async def resolve_discarded_candidate_attempt_ids(
    *,
    execution_id: str,
    owner_id: str,
    novel_id: str,
    chapter_id: str,
    attempt_slots: Sequence[Mapping[str, Any]],
    protected_attempt_ids: Sequence[str] = (),
) -> tuple[str, ...]:
    """Load durable discard evidence for one exact GenerationJob chapter."""

    if not any(
        str(slot.get("step_id") or "") == "candidate-prose"
        and str(slot.get("state") or "") in DISCARD_RESOLVED_ATTEMPT_STATES
        and str(slot.get("attempt_id") or "") not in protected_attempt_ids
        for slot in attempt_slots
        if isinstance(slot, Mapping)
    ):
        return ()
    runs = await prose_run_repo.list_discarded_by_generation_job(
        generation_job_id=execution_id,
        owner_id=owner_id,
        novel_id=novel_id,
        chapter_id=chapter_id,
        limit=MAX_DISCARDED_CANDIDATE_RUNS + 1,
    )
    return match_discarded_candidate_attempt_ids(
        discarded_runs=runs,
        attempt_slots=attempt_slots,
        protected_attempt_ids=protected_attempt_ids,
    )
