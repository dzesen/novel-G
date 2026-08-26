"""Shared bounded projection for content-free diagnostic reason codes."""

from __future__ import annotations

from collections.abc import Iterable
import re
from typing import Any


DEFAULT_STABLE_REASON_CODE_LIMIT = 20
_STABLE_REASON_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,119}$")


def normalize_stable_reason_code(value: Any) -> str | None:
    """Return one safe machine code, or ``None`` for untrusted input."""

    code = str(value or "").strip()
    return code if _STABLE_REASON_CODE_PATTERN.fullmatch(code) else None


def project_stable_reason_codes(
    values: Iterable[Any],
    *,
    limit: int = DEFAULT_STABLE_REASON_CODE_LIMIT,
) -> tuple[str, ...]:
    """Keep stable unique reason codes in source order under a hard limit."""

    result: list[str] = []
    for value in values:
        code = normalize_stable_reason_code(value)
        if code is None or code in result:
            continue
        result.append(code)
        if len(result) >= limit:
            break
    return tuple(result)
