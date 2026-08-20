"""Pure formal-write predicate shared by generation and persistence gates."""

from __future__ import annotations

from typing import Any


def completion_allows_formal_write(
    *,
    status: Any,
    can_write_formal_prose: Any,
    finish_reason: Any,
) -> bool:
    """Only an exact successful stop may cross the formal prose boundary."""

    return bool(
        status == "complete"
        and can_write_formal_prose is True
        and finish_reason == "stop"
    )
