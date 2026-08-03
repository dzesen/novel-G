"""Pure domain model for bounded, per-scene prose continuation authorization.

The prose execution plan answers *what* has to be written.  This module answers
*how many explicitly-authorized extra calls* may be made for every scene.  The
separation is intentional: changing the authorization must not silently change
the content identity of a persisted draft.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from backend.services.generation.prose_protocol import CURRENT_SCENE_CONTINUATION_PROTOCOL_REVISION


MIN_AUTOMATIC_CONTINUATIONS_PER_SCENE = 0
MAX_AUTOMATIC_CONTINUATIONS_PER_SCENE = 15
DEFAULT_AUTOMATIC_CONTINUATIONS_PER_SCENE = 0

MIN_CONTINUATION_TARGET_WORDS = 400
MAX_CONTINUATION_TARGET_WORDS = 5_000
DEFAULT_CONTINUATION_TARGET_WORDS = 1_000

# Healthy scenes stayed at or below 1.7x their target with repeats no longer
# than 14 characters, while pathological scenes reached at least 2.5x with
# 1,884+ replayed characters.  2.0 is the measured gap between them: a
# centrally-owned safety cutoff, not a user-facing generation control.
SCENE_DIVERGENCE_STOP_FACTOR = 2.0


class ProseContinuationPolicyError(ValueError):
    """Raised when a user-facing continuation control is outside its contract."""


def _bounded_int(
    value: Any,
    *,
    field_name: str,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool):
        raise ProseContinuationPolicyError(f"{field_name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ProseContinuationPolicyError(f"{field_name} must be an integer") from exc
    if parsed < minimum or parsed > maximum:
        raise ProseContinuationPolicyError(
            f"{field_name} must be between {minimum} and {maximum}"
        )
    return parsed


def _digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ProseContinuationPolicy:
    """A user-selected continuation policy, scoped to one prose run or job."""

    automatic_continuations_per_scene: int = (
        DEFAULT_AUTOMATIC_CONTINUATIONS_PER_SCENE
    )
    continuation_target_words: int = DEFAULT_CONTINUATION_TARGET_WORDS

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "automatic_continuations_per_scene",
            _bounded_int(
                self.automatic_continuations_per_scene,
                field_name="automatic_continuations_per_scene",
                minimum=MIN_AUTOMATIC_CONTINUATIONS_PER_SCENE,
                maximum=MAX_AUTOMATIC_CONTINUATIONS_PER_SCENE,
            ),
        )
        object.__setattr__(
            self,
            "continuation_target_words",
            _bounded_int(
                self.continuation_target_words,
                field_name="continuation_target_words",
                minimum=MIN_CONTINUATION_TARGET_WORDS,
                maximum=MAX_CONTINUATION_TARGET_WORDS,
            ),
        )

    @property
    def permits_automatic_continuation(self) -> bool:
        return self.automatic_continuations_per_scene > 0

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any] | None,
    ) -> "ProseContinuationPolicy":
        payload = dict(value or {})
        return cls(
            automatic_continuations_per_scene=payload.get(
                "automatic_continuations_per_scene",
                DEFAULT_AUTOMATIC_CONTINUATIONS_PER_SCENE,
            ),
            continuation_target_words=payload.get(
                "continuation_target_words",
                DEFAULT_CONTINUATION_TARGET_WORDS,
            ),
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "automatic_continuations_per_scene": (
                self.automatic_continuations_per_scene
            ),
            "continuation_target_words": self.continuation_target_words,
        }


@dataclass(frozen=True)
class ProseBudgetCoverage:
    """A side-effect-free, conservative chapter-coverage estimate."""

    status: Literal["available", "unavailable"]
    estimated_prose_chapter_count: int
    chapters_with_automatic_continuations: int | None
    chapters_without_automatic_continuations: int | None
    unavailable_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "estimated_prose_chapter_count": self.estimated_prose_chapter_count,
            "chapters_with_automatic_continuations": (
                self.chapters_with_automatic_continuations
            ),
            "chapters_without_automatic_continuations": (
                self.chapters_without_automatic_continuations
            ),
            "unavailable_reason": self.unavailable_reason,
        }


@dataclass(frozen=True)
class ProseContinuationAuthorization:
    """Immutable readiness snapshot for one explicit paid-call authorization."""

    policy: ProseContinuationPolicy
    authorization_revision: int
    content_identity: str
    provider_plan_revision: str
    max_base_calls: int
    max_automatic_continuation_calls: int
    max_logical_prose_calls: int
    base_output_token_bound: int
    continuation_output_token_bound: int
    conservative_base_token_bound: int
    conservative_continuation_token_bound: int
    conservative_token_bound: int
    conservative_total_token_bound: int
    token_bound_known: bool
    budget_coverage: ProseBudgetCoverage
    token_budget: int | None
    readiness_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy.to_dict(),
            "authorization_revision": self.authorization_revision,
            "content_identity": self.content_identity,
            "provider_plan_revision": self.provider_plan_revision,
            "max_base_calls": self.max_base_calls,
            "max_automatic_continuation_calls": (
                self.max_automatic_continuation_calls
            ),
            "max_logical_prose_calls": self.max_logical_prose_calls,
            "base_output_token_bound": self.base_output_token_bound,
            "continuation_output_token_bound": self.continuation_output_token_bound,
            "conservative_base_token_bound": self.conservative_base_token_bound,
            "conservative_continuation_token_bound": (
                self.conservative_continuation_token_bound
            ),
            "conservative_token_bound": self.conservative_token_bound,
            "conservative_total_token_bound": self.conservative_total_token_bound,
            "token_bound_known": self.token_bound_known,
            "budget_coverage": self.budget_coverage.to_dict(),
            "token_budget": self.token_budget,
            "readiness_digest": self.readiness_digest,
        }


class ProseAuthorizationModule:
    """Small interface for policy validation and deterministic readiness snapshots."""

    @staticmethod
    def maximum_automatic_continuation_calls(
        *,
        scene_count: int,
        policy: ProseContinuationPolicy,
    ) -> int:
        return max(1, int(scene_count)) * policy.automatic_continuations_per_scene

    def maximum_logical_prose_calls(
        self,
        *,
        scheduled_base_calls: int,
        scene_count: int,
        policy: ProseContinuationPolicy,
    ) -> int:
        return max(0, int(scheduled_base_calls)) + (
            self.maximum_automatic_continuation_calls(
                scene_count=scene_count,
                policy=policy,
            )
        )

    @staticmethod
    def estimate_budget_coverage(
        *,
        token_budget: int | None,
        token_bound_known: bool,
        estimated_chapter_count: int,
        conservative_base_token_bound: int,
        conservative_total_token_bound: int,
    ) -> ProseBudgetCoverage:
        chapter_count = max(0, int(estimated_chapter_count))
        base_total = max(0, int(conservative_base_token_bound))
        total = max(0, int(conservative_total_token_bound))
        if token_budget is None:
            reason = "token_budget_missing"
        elif not token_bound_known:
            reason = "token_bound_unproven"
        elif chapter_count <= 0:
            reason = "no_prose_chapters"
        elif base_total <= 0 or total <= 0:
            reason = "zero_token_bound"
        else:
            budget = max(1, int(token_budget))
            return ProseBudgetCoverage(
                status="available",
                estimated_prose_chapter_count=chapter_count,
                chapters_with_automatic_continuations=min(
                    chapter_count,
                    (budget * chapter_count) // total,
                ),
                chapters_without_automatic_continuations=min(
                    chapter_count,
                    (budget * chapter_count) // base_total,
                ),
                unavailable_reason=None,
            )
        return ProseBudgetCoverage(
            status="unavailable",
            estimated_prose_chapter_count=chapter_count,
            chapters_with_automatic_continuations=None,
            chapters_without_automatic_continuations=None,
            unavailable_reason=reason,
        )

    def authorize(
        self,
        *,
        policy: ProseContinuationPolicy,
        authorization_revision: int,
        content_identity: str,
        provider_plan_revision: str,
        scheduled_base_calls: int,
        scene_count: int,
        conservative_token_bound: int = 0,
        base_output_token_bound: int = 0,
        continuation_output_token_bound: int = 0,
        conservative_base_token_bound: int | None = None,
        conservative_continuation_token_bound: int | None = None,
        token_bound_known: bool | None = None,
        estimated_chapter_count: int = 1,
        token_budget: int | None = None,
    ) -> ProseContinuationAuthorization:
        revision = max(1, int(authorization_revision))
        base_calls = max(0, int(scheduled_base_calls))
        automatic_calls = self.maximum_automatic_continuation_calls(
            scene_count=scene_count,
            policy=policy,
        )
        logical_calls = base_calls + automatic_calls
        conservative_bound = max(0, int(conservative_token_bound))
        base_output_bound = max(0, int(base_output_token_bound))
        continuation_output_bound = max(0, int(continuation_output_token_bound))
        base_conservative_bound = max(
            0,
            int(
                conservative_bound
                if conservative_base_token_bound is None
                else conservative_base_token_bound
            ),
        )
        continuation_conservative_bound = max(
            0,
            int(
                conservative_bound
                if conservative_continuation_token_bound is None
                else conservative_continuation_token_bound
            ),
        )
        conservative_bound = max(
            conservative_bound,
            base_conservative_bound,
            continuation_conservative_bound,
        )
        known_bound = (
            conservative_bound > 0
            if token_bound_known is None
            else bool(token_bound_known)
        )
        chapter_count = max(0, int(estimated_chapter_count))
        conservative_total = (
            base_calls * base_conservative_bound
            + automatic_calls * continuation_conservative_bound
        )
        normalized_budget = (
            None if token_budget is None else max(1, int(token_budget))
        )
        budget_coverage = self.estimate_budget_coverage(
            token_budget=normalized_budget,
            token_bound_known=known_bound,
            estimated_chapter_count=chapter_count,
            conservative_base_token_bound=base_calls * base_conservative_bound,
            conservative_total_token_bound=conservative_total,
        )
        payload = {
            "protocol_revision": CURRENT_SCENE_CONTINUATION_PROTOCOL_REVISION,
            "policy": policy.to_dict(),
            "authorization_revision": revision,
            "content_identity": str(content_identity or ""),
            "provider_plan_revision": str(provider_plan_revision or ""),
            "max_base_calls": base_calls,
            "max_automatic_continuation_calls": automatic_calls,
            "max_logical_prose_calls": logical_calls,
            "base_output_token_bound": base_output_bound,
            "continuation_output_token_bound": continuation_output_bound,
            "conservative_base_token_bound": base_conservative_bound,
            "conservative_continuation_token_bound": continuation_conservative_bound,
            "conservative_token_bound": conservative_bound,
            "conservative_total_token_bound": conservative_total,
            "token_bound_known": known_bound,
            "estimated_chapter_count": chapter_count,
            "budget_coverage": budget_coverage.to_dict(),
            "token_budget": normalized_budget,
        }
        return ProseContinuationAuthorization(
            policy=policy,
            authorization_revision=revision,
            content_identity=payload["content_identity"],
            provider_plan_revision=payload["provider_plan_revision"],
            max_base_calls=base_calls,
            max_automatic_continuation_calls=automatic_calls,
            max_logical_prose_calls=logical_calls,
            base_output_token_bound=base_output_bound,
            continuation_output_token_bound=continuation_output_bound,
            conservative_base_token_bound=base_conservative_bound,
            conservative_continuation_token_bound=continuation_conservative_bound,
            conservative_token_bound=conservative_bound,
            conservative_total_token_bound=conservative_total,
            token_bound_known=known_bound,
            budget_coverage=budget_coverage,
            token_budget=normalized_budget,
            readiness_digest=_digest(payload),
        )


prose_authorization_module = ProseAuthorizationModule()
