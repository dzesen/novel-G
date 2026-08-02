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
from typing import Any, Mapping


MIN_AUTOMATIC_CONTINUATIONS_PER_SCENE = 0
MAX_AUTOMATIC_CONTINUATIONS_PER_SCENE = 15
DEFAULT_AUTOMATIC_CONTINUATIONS_PER_SCENE = 0

MIN_CONTINUATION_TARGET_WORDS = 400
MAX_CONTINUATION_TARGET_WORDS = 5_000
DEFAULT_CONTINUATION_TARGET_WORDS = 1_000


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
class ProseContinuationAuthorization:
    """Immutable readiness snapshot for one explicit paid-call authorization."""

    policy: ProseContinuationPolicy
    authorization_revision: int
    content_identity: str
    provider_plan_revision: str
    max_base_calls: int
    max_automatic_continuation_calls: int
    max_logical_prose_calls: int
    conservative_token_bound: int
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
            "conservative_token_bound": self.conservative_token_bound,
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
        normalized_budget = (
            None if token_budget is None else max(1, int(token_budget))
        )
        payload = {
            "protocol_revision": "scene-continuation-v3",
            "policy": policy.to_dict(),
            "authorization_revision": revision,
            "content_identity": str(content_identity or ""),
            "provider_plan_revision": str(provider_plan_revision or ""),
            "max_base_calls": base_calls,
            "max_automatic_continuation_calls": automatic_calls,
            "max_logical_prose_calls": logical_calls,
            "conservative_token_bound": conservative_bound,
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
            conservative_token_bound=conservative_bound,
            token_budget=normalized_budget,
            readiness_digest=_digest(payload),
        )


prose_authorization_module = ProseAuthorizationModule()
