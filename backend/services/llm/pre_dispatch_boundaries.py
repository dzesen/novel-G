"""Typed, content-free failures raised before a Provider request is dispatched."""

from __future__ import annotations

from typing import Literal


PreDispatchBoundaryCode = Literal[
    "token_budget_exceeded_before_dispatch",
    "attempt_capacity_exhausted",
]


class TokenBudgetExceeded(ValueError):
    """A Provider dispatch would exceed the explicitly authorized token budget."""


class AttemptCapacityExceeded(ValueError):
    """The frozen attempt capacity cannot authorize another Provider dispatch."""


def pre_dispatch_boundary_code(
    exc: BaseException,
) -> PreDispatchBoundaryCode | None:
    """Project only trusted in-process boundary types onto a stable wire code."""

    if isinstance(exc, TokenBudgetExceeded):
        return "token_budget_exceeded_before_dispatch"
    if isinstance(exc, AttemptCapacityExceeded):
        return "attempt_capacity_exhausted"
    return None


def restore_pre_dispatch_boundary(
    code: object,
    message: object = None,
) -> TokenBudgetExceeded | AttemptCapacityExceeded | None:
    """Restore a trusted boundary projection without parsing exception text."""

    detail = str(message or "Provider dispatch was refused before dispatch")
    if code == "token_budget_exceeded_before_dispatch":
        return TokenBudgetExceeded(detail)
    if code == "attempt_capacity_exhausted":
        return AttemptCapacityExceeded(detail)
    return None
