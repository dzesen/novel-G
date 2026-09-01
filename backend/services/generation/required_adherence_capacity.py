"""Pure dedicated-review capacity checks for ADR-0008's successor contract.

These values prove capacity only. They do not claim an attempt, debit a ledger,
authorize a Provider, or replace the Job's atomic pre-dispatch reservation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.services.generation.independent_outline_review import IndependentReviewPlan


class RequiredAdherenceCapacityError(ValueError):
    pass


@dataclass(frozen=True)
class PostRewriteReviewCapacity:
    next_rewrite_ordinal: int
    review_attempts: int
    review_tokens: int
    serial_seconds: int


class RequiredAdherenceCapacity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["required_adherence_capacity.v1"]
    review_contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    max_rewrite_dispatches: Literal[2]
    max_attempts_per_review: Literal[2]
    input_tokens_per_attempt: int = Field(ge=1, le=2**63 - 1)
    output_tokens_per_attempt: int = Field(ge=1, le=2**63 - 1)
    timeout_seconds_per_attempt: int = Field(ge=1, le=2**31 - 1)

    @model_validator(mode="after")
    def validate_aggregate_bound(self) -> "RequiredAdherenceCapacity":
        if self.max_review_tokens_per_chapter > 2**63 - 1:
            raise ValueError("review capacity exceeds the ledger integer bound")
        return self

    @classmethod
    def from_plan(cls, plan: IndependentReviewPlan) -> "RequiredAdherenceCapacity":
        return cls(
            schema_version="required_adherence_capacity.v1",
            review_contract_digest=plan.contract_digest,
            max_rewrite_dispatches=2,
            max_attempts_per_review=2,
            input_tokens_per_attempt=plan.input_token_bound,
            output_tokens_per_attempt=plan.generation.max_output_tokens,
            timeout_seconds_per_attempt=plan.generation.timeout_seconds,
        )

    @property
    def max_logical_reviews(self) -> int:
        return 1 + self.max_rewrite_dispatches

    @property
    def tokens_per_review(self) -> int:
        return self.max_attempts_per_review * (
            self.input_tokens_per_attempt + self.output_tokens_per_attempt
        )

    @property
    def seconds_per_review(self) -> int:
        return self.max_attempts_per_review * self.timeout_seconds_per_attempt

    @property
    def max_review_attempts_per_chapter(self) -> int:
        return self.max_logical_reviews * self.max_attempts_per_review

    @property
    def max_review_tokens_per_chapter(self) -> int:
        return self.max_logical_reviews * self.tokens_per_review

    @property
    def max_review_seconds_per_chapter(self) -> int:
        return self.max_logical_reviews * self.seconds_per_review

    def before_rewrite(
        self,
        *,
        plan: IndependentReviewPlan,
        rewrite_dispatches_used: int,
        logical_reviews_used: int,
        remaining_review_attempts: int,
        remaining_review_tokens: int,
        remaining_seconds: int,
        rewrite_worst_seconds: int,
        uncertain_attempts: int,
    ) -> PostRewriteReviewCapacity:
        if self != self.from_plan(plan):
            raise RequiredAdherenceCapacityError("review_capacity_plan_mismatch")
        counters = (
            rewrite_dispatches_used, logical_reviews_used, remaining_review_attempts,
            remaining_review_tokens, remaining_seconds, rewrite_worst_seconds,
            uncertain_attempts,
        )
        if any(type(value) is not int or value < 0 or value > 2**63 - 1 for value in counters):
            raise RequiredAdherenceCapacityError("review_capacity_counter_invalid")
        if uncertain_attempts:
            raise RequiredAdherenceCapacityError("review_uncertain")
        if rewrite_dispatches_used >= self.max_rewrite_dispatches:
            raise RequiredAdherenceCapacityError("rewrite_dispatch_limit")
        if (
            logical_reviews_used >= self.max_logical_reviews
            or logical_reviews_used > rewrite_dispatches_used + 1
        ):
            raise RequiredAdherenceCapacityError("review_logical_limit")
        if remaining_review_attempts < self.max_attempts_per_review:
            raise RequiredAdherenceCapacityError("review_attempt_capacity")
        if remaining_review_tokens < self.tokens_per_review:
            raise RequiredAdherenceCapacityError("review_token_capacity")
        serial_seconds = rewrite_worst_seconds + self.seconds_per_review
        if (
            rewrite_worst_seconds < 1
            or serial_seconds > 2**63 - 1
            or remaining_seconds < serial_seconds
        ):
            raise RequiredAdherenceCapacityError("review_serial_deadline_capacity")
        return PostRewriteReviewCapacity(
            next_rewrite_ordinal=rewrite_dispatches_used + 1,
            review_attempts=self.max_attempts_per_review,
            review_tokens=self.tokens_per_review,
            serial_seconds=serial_seconds,
        )
