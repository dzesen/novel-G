"""Pure readiness and explicit authorization for one prose run.

The object built here contains no prompt text or prose.  It ties the mutable
continuation policy and budget to the current content/provider identities so a
stale browser tab cannot silently turn an ordinary chapter request into extra
paid calls.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from backend.services.generation.prose_completion import ProseExecutionPlan
from backend.services.generation.prose_continuation import (
    ProseContinuationAuthorization,
    ProseContinuationPolicy,
    prose_authorization_module,
)


class ProseReadinessBlocked(ValueError):
    """The requested automatic continuation policy lacks explicit authority."""


class StaleProseReadiness(ValueError):
    """A browser confirmation does not match the current prose inputs."""


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _plan_value(plan: Any, name: str, default: Any = None) -> Any:
    return getattr(plan, name, default)


def _provider_identity(plan: Any) -> dict[str, Any]:
    return {
        "provider_alias": str(_plan_value(plan, "provider_alias", "") or ""),
        "provider_model": str(_plan_value(plan, "provider_model", "") or ""),
        "config_revision": str(_plan_value(plan, "config_revision", "") or ""),
        "capability_snapshot": str(
            _plan_value(plan, "capability_snapshot", "") or ""
        ),
        "max_output_tokens": _positive_int(
            _plan_value(plan, "max_output_tokens")
        ),
        "thinking_mode": _plan_value(plan, "thinking_mode"),
    }


def conservative_prose_call_token_bound(
    *,
    generation_plan: Any,
    base_prompt: str,
    outline: Mapping[str, Any],
    generation_kwargs: Mapping[str, Any] | None,
    fallback_output_tokens: int | None = None,
) -> int | None:
    """Return a safe per-call upper bound for the supported text envelope.

    The runtime sends one user prompt and an optional system prompt.  UTF-8
    bytes are an upper bound for their text-token representation for the three
    supported text protocols; the fixed allowance covers role/protocol tokens.
    A later continuation additionally carries at most 2,000 tail characters
    (at most 8,000 UTF-8 bytes) plus one serialized scene from ``outline``.
    We deliberately return ``None`` without a Provider output hard limit.
    """
    kwargs = dict(generation_kwargs or {})
    output_limit = _positive_int(kwargs.get("max_tokens"))
    if output_limit is None:
        output_limit = _positive_int(
            _plan_value(generation_plan, "max_output_tokens")
        )
    if output_limit is None and _plan_value(
        generation_plan, "supports_output_token_cap", True
    ):
        output_limit = _positive_int(fallback_output_tokens)
    if output_limit is None:
        return None
    outline_json = json.dumps(
        dict(outline or {}),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    system_prompt = str(kwargs.get("system_prompt") or "")
    input_upper = (
        len(str(base_prompt or "").encode("utf-8"))
        + len(outline_json.encode("utf-8"))
        + len(system_prompt.encode("utf-8"))
        + 8_000
        + 1_024
    )
    return max(1, int(output_limit) + input_upper)


@dataclass(frozen=True)
class ProseReadiness:
    execution_plan: ProseExecutionPlan
    authorization: ProseContinuationAuthorization
    warnings: tuple[str, ...]
    token_bound_known: bool

    @property
    def requires_automatic_confirmation(self) -> bool:
        return self.authorization.policy.permits_automatic_continuation

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_plan": self.execution_plan.to_dict(),
            "authorization": self.authorization.to_dict(),
            "warnings": list(self.warnings),
            "token_bound_known": self.token_bound_known,
            "requires_automatic_confirmation": (
                self.requires_automatic_confirmation
            ),
        }


def build_prose_readiness(
    *,
    execution_plan: ProseExecutionPlan,
    generation_plan: Any,
    policy: ProseContinuationPolicy,
    token_budget: int | None,
    authorization_revision: int,
    novel_id: str,
    chapter_id: str,
    outline: Mapping[str, Any],
    context_text: str,
    base_prompt: str,
    generation_kwargs: Mapping[str, Any] | None,
) -> ProseReadiness:
    provider_identity = _provider_identity(generation_plan)
    maximum_call_target = max(
        [policy.continuation_target_words, *execution_plan.segment_budgets]
    )
    fallback_output_tokens = max(
        256,
        int((maximum_call_target / 0.65) + 0.999999),
    )
    conservative_bound = conservative_prose_call_token_bound(
        generation_plan=generation_plan,
        base_prompt=base_prompt,
        outline=outline,
        generation_kwargs=generation_kwargs,
        fallback_output_tokens=fallback_output_tokens,
    )
    content_identity = _digest(
        {
            "novel_id": str(novel_id),
            "chapter_id": str(chapter_id),
            "outline": dict(outline or {}),
            "context_revision": hashlib.sha256(
                str(context_text or "").encode("utf-8")
            ).hexdigest(),
            "execution_plan": execution_plan.to_dict(),
            "generation_kwargs": dict(generation_kwargs or {}),
        }
    )
    provider_plan_revision = _digest(provider_identity)
    authorization = prose_authorization_module.authorize(
        policy=policy,
        authorization_revision=authorization_revision,
        content_identity=content_identity,
        provider_plan_revision=provider_plan_revision,
        scheduled_base_calls=execution_plan.scheduled_base_call_count,
        scene_count=execution_plan.scene_count,
        conservative_token_bound=conservative_bound or 0,
        token_budget=token_budget,
    )
    warnings: list[str] = []
    if policy.permits_automatic_continuation:
        warnings.append("automatic_continuations_require_confirmation")
        if conservative_bound is None:
            warnings.append("prose_token_bound_unproven")
        if token_budget is None:
            warnings.append("automatic_continuations_require_token_budget")
    return ProseReadiness(
        execution_plan=execution_plan,
        authorization=authorization,
        warnings=tuple(warnings),
        token_bound_known=conservative_bound is not None,
    )


def validate_prose_readiness(
    readiness: ProseReadiness,
    *,
    supplied_digest: str | None,
    confirmed_automatic_continuations: bool,
) -> None:
    authorization = readiness.authorization
    if supplied_digest is not None and supplied_digest != authorization.readiness_digest:
        raise StaleProseReadiness("readiness_stale")
    if not authorization.policy.permits_automatic_continuation:
        return
    blocked = [
        code
        for code in readiness.warnings
        if code
        in {
            "prose_token_bound_unproven",
            "automatic_continuations_require_token_budget",
        }
    ]
    if blocked:
        raise ProseReadinessBlocked(",".join(blocked))
    if supplied_digest is None:
        raise StaleProseReadiness("readiness_stale")
    if not confirmed_automatic_continuations:
        raise ProseReadinessBlocked(
            "automatic_continuations_require_confirmation"
        )
