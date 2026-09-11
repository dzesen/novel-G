"""Deterministic Provider budget arithmetic shared by readiness planners."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from backend.services.llm.generation_runtime import GenerationPlan


class ProviderOutputLimitMissing(ValueError):
    """A planned call has neither a request cap nor a Provider default."""

    def __init__(self, provider_alias: str) -> None:
        super().__init__("structured output-token bound must be a positive integer")
        self.provider_alias = provider_alias


@dataclass(frozen=True)
class ProviderBudgetBound:
    """An independent upper bound for one configured Provider alias."""

    provider_alias: str
    paid_attempts: int
    tokens: int

    def __post_init__(self) -> None:
        alias = str(self.provider_alias or "").strip()
        if not alias:
            raise ValueError("provider budget alias is required")
        if (
            isinstance(self.paid_attempts, bool)
            or not isinstance(self.paid_attempts, int)
            or self.paid_attempts < 0
        ):
            raise ValueError("provider paid-attempt bound is invalid")
        if (
            isinstance(self.tokens, bool)
            or not isinstance(self.tokens, int)
            or self.tokens < 0
        ):
            raise ValueError("provider token bound is invalid")
        object.__setattr__(self, "provider_alias", alias)


@dataclass(frozen=True)
class StructuredCallBudget:
    max_paid_attempts: int
    max_context_tokens: int | None
    max_input_tokens_per_attempt: int
    max_output_tokens_per_attempt: int
    max_tokens_per_call: int
    provider_bounds: tuple[ProviderBudgetBound, ...]


def _positive_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _optional_positive_int(value: Any, *, field: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, field=field)


def merge_provider_bounds(
    *groups: Iterable[ProviderBudgetBound],
) -> tuple[ProviderBudgetBound, ...]:
    """Add independent Provider bounds, coalescing equal aliases."""

    totals: dict[str, tuple[int, int]] = {}
    for group in groups:
        for bound in group:
            if not isinstance(bound, ProviderBudgetBound):
                raise ValueError("provider budget entry is invalid")
            attempts, tokens = totals.get(bound.provider_alias, (0, 0))
            totals[bound.provider_alias] = (
                attempts + bound.paid_attempts,
                tokens + bound.tokens,
            )
    return tuple(
        ProviderBudgetBound(alias, attempts, tokens)
        for alias, (attempts, tokens) in sorted(totals.items())
        if attempts or tokens
    )


def max_provider_bounds(
    *groups: Iterable[ProviderBudgetBound],
) -> tuple[ProviderBudgetBound, ...]:
    """Take a component-wise upper envelope across mutually exclusive paths."""

    maxima: dict[str, tuple[int, int]] = {}
    for group in groups:
        normalized = merge_provider_bounds(tuple(group))
        current = {bound.provider_alias: bound for bound in normalized}
        aliases = set(maxima) | set(current)
        maxima = {
            alias: (
                max(
                    maxima.get(alias, (0, 0))[0],
                    current[alias].paid_attempts if alias in current else 0,
                ),
                max(
                    maxima.get(alias, (0, 0))[1],
                    current[alias].tokens if alias in current else 0,
                ),
            )
            for alias in aliases
        }
    return tuple(
        ProviderBudgetBound(alias, attempts, tokens)
        for alias, (attempts, tokens) in sorted(maxima.items())
        if attempts or tokens
    )


def scale_provider_bounds(
    bounds: Iterable[ProviderBudgetBound],
    multiplier: int,
) -> tuple[ProviderBudgetBound, ...]:
    if (
        isinstance(multiplier, bool)
        or not isinstance(multiplier, int)
        or multiplier < 0
    ):
        raise ValueError("provider budget multiplier is invalid")
    return tuple(
        ProviderBudgetBound(
            bound.provider_alias,
            bound.paid_attempts * multiplier,
            bound.tokens * multiplier,
        )
        for bound in bounds
        if multiplier and (bound.paid_attempts or bound.tokens)
    )


def structured_call_budget(
    plan: GenerationPlan,
    *,
    input_token_bound: int | None = None,
    output_token_bound: int | None = None,
) -> StructuredCallBudget:
    """Freeze one logical structured call, including repair/reviewer attempts."""

    if not isinstance(plan, GenerationPlan):
        raise ValueError("structured generation plan is invalid")
    provider_alias = str(plan.provider_alias or "").strip()
    if not provider_alias:
        raise ValueError("structured generation Provider is missing")
    attempts = _positive_int(
        plan.max_semantic_attempts,
        field="structured paid-attempt bound",
    )
    effective_output_bound = (
        output_token_bound if output_token_bound is not None else plan.max_output_tokens
    )
    if effective_output_bound is None:
        raise ProviderOutputLimitMissing(provider_alias)
    context_tokens = _optional_positive_int(
        plan.max_context_tokens,
        field="structured context-token bound",
    )
    input_tokens = _positive_int(
        input_token_bound
        if input_token_bound is not None
        else context_tokens,
        field="structured input-token bound",
    )
    output_tokens = _positive_int(
        effective_output_bound,
        field="structured output-token bound",
    )
    reviewer_alias = str(plan.reviewer_alias or "").strip() or None
    merged = structured_provider_bounds(
        provider_alias=provider_alias,
        reviewer_alias=reviewer_alias,
        max_paid_attempts=attempts,
        input_tokens_per_attempt=input_tokens,
        output_tokens_per_attempt=output_tokens,
    )
    return StructuredCallBudget(
        max_paid_attempts=attempts,
        max_context_tokens=context_tokens,
        max_input_tokens_per_attempt=input_tokens,
        max_output_tokens_per_attempt=output_tokens,
        max_tokens_per_call=attempts * (input_tokens + output_tokens),
        provider_bounds=merged,
    )


def structured_provider_bounds(
    *,
    provider_alias: str,
    reviewer_alias: str | None,
    max_paid_attempts: int,
    input_tokens_per_attempt: int,
    output_tokens_per_attempt: int,
) -> tuple[ProviderBudgetBound, ...]:
    """Split one frozen structured-call ceiling across primary/reviewer aliases."""

    normalized_provider = str(provider_alias or "").strip()
    if not normalized_provider:
        raise ValueError("structured generation Provider is missing")
    attempts = _positive_int(
        max_paid_attempts,
        field="structured paid-attempt bound",
    )
    input_tokens = _positive_int(
        input_tokens_per_attempt,
        field="structured input-token bound",
    )
    output_tokens = _positive_int(
        output_tokens_per_attempt,
        field="structured output-token bound",
    )
    normalized_reviewer = str(reviewer_alias or "").strip() or None
    reviewer_attempts = 1 if normalized_reviewer else 0
    primary_attempts = attempts - reviewer_attempts
    if primary_attempts < 1:
        raise ValueError("structured Provider attempt split is invalid")
    per_attempt_tokens = input_tokens + output_tokens
    provider_bounds = [
        ProviderBudgetBound(
            normalized_provider,
            primary_attempts,
            primary_attempts * per_attempt_tokens,
        )
    ]
    if normalized_reviewer:
        provider_bounds.append(
            ProviderBudgetBound(
                normalized_reviewer,
                1,
                per_attempt_tokens,
            )
        )
    return merge_provider_bounds(provider_bounds)
