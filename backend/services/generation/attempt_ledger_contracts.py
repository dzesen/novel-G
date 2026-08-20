"""Closed contracts for persisted Generation Job attempt ledgers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


PERSISTED_ATTEMPT_STATES = frozenset({
    "claimed",
    "accounted",
    "uncertain",
    "released_pre_dispatch",
    "uncertain_retry_acknowledged",
    "uncertain_skip_acknowledged",
    "uncertain_abort_acknowledged",
})

EVIDENCE_ATTEMPT_STATES = frozenset({
    "accounted",
    "uncertain",
    "uncertain_retry_acknowledged",
    "uncertain_skip_acknowledged",
    "uncertain_abort_acknowledged",
})

RECOVERED_OUTCOME_ATTEMPT_STATES = frozenset({
    "accounted",
    "released_pre_dispatch",
    "uncertain_retry_acknowledged",
    "uncertain_skip_acknowledged",
    "uncertain_abort_acknowledged",
})

LAUNCHABLE_ATTEMPT_STATES = frozenset({
    "accounted",
    "released_pre_dispatch",
    "uncertain_retry_acknowledged",
    "uncertain_skip_acknowledged",
    "uncertain_abort_acknowledged",
})

PERSISTED_TOKEN_RESERVATION_STATES = frozenset({
    "claimed",
    "uncertain",
    "uncertain_retry_acknowledged",
    "uncertain_skip_acknowledged",
    "uncertain_abort_acknowledged",
})

LAUNCHABLE_TOKEN_RESERVATION_STATES = frozenset({
    "uncertain_retry_acknowledged",
    "uncertain_skip_acknowledged",
    "uncertain_abort_acknowledged",
})


MAX_PERSISTED_ATTEMPT_TOKENS = 1_000_000_000
MAX_PERSISTED_LEDGER_TOKENS = 2**63 - 1
_ATTEMPT_IDENTITY_FIELDS = (
    "attempt_id",
    "chapter_id",
    "step_id",
    "phase",
    "provider_alias",
)
_ATTEMPT_FIELD_LIMITS = {
    "attempt_id": 128,
    "chapter_id": 64,
    "step_id": 128,
    "phase": 64,
    "provider_alias": 64,
}


@dataclass(frozen=True)
class LaunchableAttemptLedgerV1:
    """Canonical accounting floor frozen into one execution-lease CAS."""

    reserved_tokens: int
    charged_token_floor: int
    accounted_attempt_ids: tuple[str, ...]


def _checked_add(total: int, value: int) -> int:
    if value < 0 or total > MAX_PERSISTED_LEDGER_TOKENS - value:
        raise ValueError("persisted Provider attempt usage exceeds its bound")
    return total + value


def _strict_identity(item: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    for field in _ATTEMPT_IDENTITY_FIELDS:
        value = item.get(field)
        if (
            not isinstance(value, str)
            or not value
            or len(value) > _ATTEMPT_FIELD_LIMITS[field]
        ):
            raise ValueError("persisted Provider attempt identity is invalid")
        values.append(value)
    return tuple(values)


def _strict_bound(value: Any, *, required: bool) -> int | None:
    if value is None and not required:
        return None
    if (
        type(value) is not int
        or value < 1
        or value > MAX_PERSISTED_ATTEMPT_TOKENS
    ):
        raise ValueError("persisted Provider attempt bound is invalid")
    return value


def _strict_usage_floor(value: Any) -> int:
    if not isinstance(value, Mapping) or set(value) != {
        "input_tokens",
        "output_tokens",
        "total_tokens",
    }:
        raise ValueError("persisted Provider attempt usage is invalid")
    components: list[int] = []
    for field in ("input_tokens", "output_tokens", "total_tokens"):
        component = value.get(field)
        if (
            type(component) is not int
            or component < 0
            or component > MAX_PERSISTED_ATTEMPT_TOKENS
        ):
            raise ValueError("persisted Provider attempt usage is invalid")
        components.append(component)
    component_total = _checked_add(components[0], components[1])
    floor = max(component_total, components[2])
    if floor > MAX_PERSISTED_ATTEMPT_TOKENS:
        raise ValueError("persisted Provider attempt usage exceeds its bound")
    return floor


def validate_launchable_attempt_ledgers(
    *,
    attempt_slots: Any,
    active_token_reservations: Any,
    usage_attempt_ids: Any,
    attempt_capacity: Any,
    attempts_claimed: Any,
    tokens_used: Any,
    tokens_reserved: Any,
    token_budget: Any,
    maximum_active_reservations: int,
) -> LaunchableAttemptLedgerV1:
    """Validate both ledgers as one bounded, conservative domain value."""

    if (
        not isinstance(attempt_slots, list)
        or not isinstance(active_token_reservations, list)
        or type(attempt_capacity) is not int
        or attempt_capacity < 0
        or attempt_capacity > MAX_PERSISTED_LEDGER_TOKENS
        or type(attempts_claimed) is not int
        or attempts_claimed < 0
        or attempts_claimed != len(attempt_slots)
        or attempts_claimed > attempt_capacity
        or len(active_token_reservations) > maximum_active_reservations
    ):
        raise ValueError("persisted Provider attempt ledger exceeds its bound")
    if (
        type(tokens_used) is not int
        or tokens_used < 0
        or tokens_used > MAX_PERSISTED_LEDGER_TOKENS
        or type(tokens_reserved) is not int
        or tokens_reserved < 0
        or tokens_reserved > MAX_PERSISTED_LEDGER_TOKENS
        or (
            token_budget is not None
            and (
                type(token_budget) is not int
                or token_budget < 0
                or token_budget > MAX_PERSISTED_LEDGER_TOKENS
            )
        )
    ):
        raise ValueError("persisted Provider token ledger is invalid")

    slots: dict[str, tuple[tuple[str, ...], str, int | None]] = {}
    expected_reservations: set[str] = set()
    accounted_ids: list[str] = []
    charged_floor = 0
    for item in attempt_slots:
        if not isinstance(item, Mapping):
            raise ValueError("persisted Provider attempt ledger is invalid")
        identity = _strict_identity(item)
        attempt_id = identity[0]
        state = item.get("state")
        if (
            attempt_id in slots
            or not isinstance(state, str)
            or state not in PERSISTED_ATTEMPT_STATES
            or state not in LAUNCHABLE_ATTEMPT_STATES
        ):
            raise ValueError("persisted Provider attempt ledger is invalid")
        bound = _strict_bound(item.get("conservative_tokens"), required=True)
        slots[attempt_id] = (identity, state, bound)
        if state == "accounted":
            floor = _strict_usage_floor(item.get("usage"))
            charged = item.get("charged_tokens")
            if charged is not None:
                if (
                    type(charged) is not int
                    or charged < floor
                    or charged > MAX_PERSISTED_ATTEMPT_TOKENS
                ):
                    raise ValueError(
                        "persisted Provider attempt usage is invalid"
                    )
                floor = charged
            charged_floor = _checked_add(charged_floor, floor)
            accounted_ids.append(attempt_id)
        elif item.get("usage") is not None:
            if _strict_usage_floor(item.get("usage")) != 0:
                raise ValueError(
                    "non-accounted Provider attempt has paid usage"
                )
        if state in LAUNCHABLE_TOKEN_RESERVATION_STATES and bound is not None:
            expected_reservations.add(attempt_id)
        if (
            token_budget is not None
            and state in LAUNCHABLE_TOKEN_RESERVATION_STATES
            and bound is None
        ):
            raise ValueError(
                "finite budget has an unbounded Provider attempt"
            )

    reservations: set[str] = set()
    reserved_total = 0
    for item in active_token_reservations:
        if not isinstance(item, Mapping):
            raise ValueError("persisted Provider token reservation is invalid")
        identity = _strict_identity(item)
        attempt_id = identity[0]
        state = item.get("state")
        bound = _strict_bound(item.get("conservative_tokens"), required=True)
        slot = slots.get(attempt_id)
        if (
            attempt_id in reservations
            or not isinstance(state, str)
            or state not in PERSISTED_TOKEN_RESERVATION_STATES
            or state not in LAUNCHABLE_TOKEN_RESERVATION_STATES
            or slot is None
            or slot != (identity, state, bound)
        ):
            raise ValueError(
                "persisted Provider token reservation identity is invalid"
            )
        reservations.add(attempt_id)
        reserved_total = _checked_add(reserved_total, bound)
    if reservations != expected_reservations:
        raise ValueError("persisted Provider token reservation is incomplete")

    if not isinstance(usage_attempt_ids, list) or any(
        not isinstance(value, str) or not value
        for value in usage_attempt_ids
    ):
        raise ValueError("persisted Provider accounted-attempt ledger is invalid")
    if (
        len(set(usage_attempt_ids)) != len(usage_attempt_ids)
        or set(usage_attempt_ids) != set(accounted_ids)
        or tokens_used < charged_floor
        or tokens_reserved != reserved_total
        or (
            token_budget is not None
            and tokens_used + tokens_reserved > token_budget
        )
    ):
        raise ValueError("persisted Provider token ledger is inconsistent")
    return LaunchableAttemptLedgerV1(
        reserved_tokens=reserved_total,
        charged_token_floor=charged_floor,
        accounted_attempt_ids=tuple(accounted_ids),
    )
