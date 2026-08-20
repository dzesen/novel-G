"""Provider-neutral prose planning and completion checks.

The module is deliberately pure. Provider adapters report terminal metadata, while
interactive and batch orchestration decide how to persist a ``ProseRun``. Keeping
the policy here prevents the SSE and headless paths from inventing different
definitions of a complete chapter.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

from backend.llm.stream_terminal import FinishReason, normalize_finish_reason
from backend.services.generation.prose_continuation import ProseContinuationPolicy
from backend.services.generation.prose_protocol import CURRENT_SCENE_CONTINUATION_PROTOCOL_REVISION
from backend.services.novel.chapter_service import count_chapter_words


def completion_allows_formal_write(
    *,
    status: Any,
    can_write_formal_prose: Any,
    finish_reason: Any,
) -> bool:
    """Return whether the frozen completion contract permits a formal write."""
    return bool(
        status == "complete"
        and can_write_formal_prose is True
        and finish_reason == "stop"
    )


@dataclass(frozen=True)
class ProseExecutionPlan:
    requested_word_count: int
    scene_count: int
    mode: Literal["single_call", "scene_segments"]
    provider_output_limit: int | None
    safe_output_budget: int
    minimum_completion_ratio: float
    segment_budgets: tuple[int, ...]
    reason_codes: tuple[str, ...]
    protocol_revision: str = CURRENT_SCENE_CONTINUATION_PROTOCOL_REVISION
    # Kept as a non-serialized compatibility attribute while v2 runs remain
    # readable. New plans never use it as a continuation authorization.
    max_continuations: int = field(default=0, compare=False, repr=False)

    @property
    def scheduled_base_call_count(self) -> int:
        return sum(
            max(1, math.ceil(budget / self.safe_output_budget))
            for budget in self.segment_budgets
        ) if self.mode == "scene_segments" else 1

    @property
    def scheduled_call_count(self) -> int:
        """Compatibility alias for callers that only need base calls."""
        return self.scheduled_base_call_count

    @property
    def call_count(self) -> int:
        """Compatibility alias; v3 continuation calls are authorization-derived."""
        return self.scheduled_base_call_count

    def maximum_logical_call_count(
        self,
        policy: ProseContinuationPolicy,
    ) -> int:
        return self.scheduled_base_call_count + (
            self.scene_count * policy.automatic_continuations_per_scene
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_word_count": self.requested_word_count,
            "scene_count": self.scene_count,
            "mode": self.mode,
            "provider_output_limit": self.provider_output_limit,
            "safe_output_budget": self.safe_output_budget,
            "minimum_completion_ratio": self.minimum_completion_ratio,
            "segment_budgets": list(self.segment_budgets),
            "reason_codes": list(self.reason_codes),
            "protocol_revision": self.protocol_revision,
            "scheduled_base_call_count": self.scheduled_base_call_count,
            "scheduled_call_count": self.scheduled_call_count,
            "call_count": self.call_count,
        }


@dataclass(frozen=True)
class ProseCompletion:
    status: Literal["complete", "degraded", "incomplete", "stale"]
    requested_word_count: int
    actual_word_count: int
    raw_character_count: int
    scene_count: int
    completed_scene_count: int
    finish_reason: FinishReason
    raw_finish_reason: str
    completion_reason: str
    mode: Literal["single_call", "scene_segments"]
    reason_codes: tuple[str, ...]
    # The raw assembled text remains the authoritative stored prose.  Scene-v3
    # callers may additionally provide a stricter completion count after
    # excluding deterministic replay coverage.
    effective_word_count: int | None = None

    @property
    def can_write_formal_prose(self) -> bool:
        return self.status in {"complete", "degraded"}

    def to_dict(self) -> dict[str, Any]:
        effective_words = (
            self.actual_word_count
            if self.effective_word_count is None
            else max(
                0,
                min(self.actual_word_count, int(self.effective_word_count)),
            )
        )
        return {
            "status": self.status,
            "requested_word_count": self.requested_word_count,
            "actual_word_count": self.actual_word_count,
            "effective_word_count": effective_words,
            "raw_character_count": self.raw_character_count,
            "scene_count": self.scene_count,
            "completed_scene_count": self.completed_scene_count,
            "finish_reason": self.finish_reason,
            "raw_finish_reason": self.raw_finish_reason,
            "completion_reason": self.completion_reason,
            "mode": self.mode,
            "reason_codes": list(self.reason_codes),
            "can_write_formal_prose": self.can_write_formal_prose,
        }


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _allocate_budgets(total: int, scene_count: int) -> tuple[int, ...]:
    count = max(1, scene_count)
    base, remainder = divmod(max(1, total), count)
    budgets = [base] * count
    # Preserve the ending scene and hook when division is uneven.
    for offset in range(remainder):
        budgets[count - 1 - (offset % count)] += 1
    return tuple(budgets)


class ProseCompletionModule:
    """Interface for planning, inspecting and deterministically assembling prose."""

    def __init__(
        self,
        *,
        unknown_provider_safe_words: int = 6_000,
        safety_ratio: float = 0.8,
        minimum_completion_ratio: float = 0.8,
    ) -> None:
        self._unknown_provider_safe_words = max(1, int(unknown_provider_safe_words))
        self._safety_ratio = min(1.0, max(0.1, float(safety_ratio)))
        self._minimum_completion_ratio = min(
            1.0,
            max(0.1, float(minimum_completion_ratio)),
        )

    def plan(
        self,
        *,
        outline: dict[str, Any],
        target_word_count: int,
        provider_capability: dict[str, Any] | None,
        request_overrides: dict[str, Any] | None,
    ) -> ProseExecutionPlan:
        scenes = list((outline or {}).get("scenes") or [])
        scene_count = max(1, len(scenes))
        requested = max(
            1,
            int(
                target_word_count
                or (outline or {}).get("target_word_count")
                or 3_000
            ),
        )
        capability = provider_capability or {}
        overrides = request_overrides or {}

        output_limit = _positive_int(capability.get("max_output_words"))
        if output_limit is None:
            output_tokens = (
                _positive_int(overrides.get("max_tokens"))
                or _positive_int(capability.get("max_output_tokens"))
            )
            if output_tokens is not None:
                # Mixed Chinese/English prose varies substantially. Treat one output
                # token as at most 0.65 Novel-G words so planning errs toward segments.
                output_limit = max(1, math.floor(output_tokens * 0.65))

        reason_codes: list[str] = []
        if output_limit is None:
            safe_budget = self._unknown_provider_safe_words
            reason_codes.append("provider_output_limit_unknown")
        else:
            safe_budget = max(1, math.floor(output_limit * self._safety_ratio))

        # Every multi-scene chapter is executed scene-by-scene in v3. A long
        # one-scene chapter may still be split into base parts by output capacity.
        scene_count_requires_segmentation = scene_count >= 2
        mode: Literal["single_call", "scene_segments"] = (
            "scene_segments"
            if requested > safe_budget or scene_count_requires_segmentation
            else "single_call"
        )
        if mode == "scene_segments":
            if requested > safe_budget:
                reason_codes.append("requested_words_exceed_safe_output")
            if scene_count_requires_segmentation:
                reason_codes.append("scene_count_requires_segmentation")
        budgets = _allocate_budgets(requested, scene_count)

        return ProseExecutionPlan(
            requested_word_count=requested,
            scene_count=scene_count,
            mode=mode,
            provider_output_limit=output_limit,
            safe_output_budget=safe_budget,
            minimum_completion_ratio=self._minimum_completion_ratio,
            segment_budgets=budgets,
            reason_codes=tuple(reason_codes),
        )

    def inspect(
        self,
        *,
        text: str,
        plan: ProseExecutionPlan,
        finish_reason: Any,
        completed_scene_indexes: Iterable[int],
        outline_revision: str,
        expected_outline_revision: str,
        raw_finish_reason: Any = None,
        effective_word_count: int | None = None,
    ) -> ProseCompletion:
        normalized_reason = normalize_finish_reason(finish_reason)
        raw_source = finish_reason if raw_finish_reason is None else raw_finish_reason
        raw_reason = str(
            getattr(raw_source, "value", raw_source) or "unreported"
        ).strip() or "unreported"
        indexes = {
            int(index)
            for index in completed_scene_indexes
            if 0 <= int(index) < plan.scene_count
        }
        reasons: list[str] = []
        if outline_revision != expected_outline_revision:
            reasons.append("outline_revision_stale")
        if normalized_reason in {
            "length",
            "content_filter",
            "tool_call",
            "cancelled",
            "error",
        }:
            reasons.append(f"finish_reason_{normalized_reason}")
        if len(indexes) != plan.scene_count:
            reasons.append("scenes_incomplete")

        actual_words = count_chapter_words(text)
        if effective_word_count is None:
            completion_words = actual_words
        else:
            try:
                supplied_effective_words = int(effective_word_count)
            except (TypeError, ValueError):
                supplied_effective_words = 0
            # Completion can only become stricter. This defensive bound also
            # makes the monotonicity contract explicit for all callers.
            completion_words = max(0, min(actual_words, supplied_effective_words))
        required_words = math.ceil(
            plan.requested_word_count * plan.minimum_completion_ratio
        )
        if completion_words < required_words:
            reasons.append("below_minimum_word_ratio")

        if "outline_revision_stale" in reasons:
            status: Literal["complete", "degraded", "incomplete", "stale"] = "stale"
        elif reasons:
            status = "incomplete"
        elif normalized_reason == "unreported":
            status = "degraded"
            reasons.append("finish_reason_unreported")
        else:
            status = "complete"

        if status == "stale":
            completion_reason = "outline_revision_stale"
        elif normalized_reason == "length":
            completion_reason = "provider_length_limit"
        elif normalized_reason == "content_filter":
            completion_reason = "provider_content_filter"
        elif normalized_reason == "tool_call":
            completion_reason = "provider_tool_call"
        elif normalized_reason == "cancelled":
            completion_reason = "cancelled"
        elif normalized_reason == "error":
            completion_reason = "provider_or_transport_error"
        elif normalized_reason == "stop" and reasons:
            completion_reason = "short_stop"
        elif normalized_reason == "unreported":
            completion_reason = "provider_reason_unreported"
        elif status == "complete":
            completion_reason = "complete"
        else:
            completion_reason = "completion_contract_failed"

        return ProseCompletion(
            status=status,
            requested_word_count=plan.requested_word_count,
            actual_word_count=actual_words,
            raw_character_count=len(text or ""),
            scene_count=plan.scene_count,
            completed_scene_count=len(indexes),
            finish_reason=normalized_reason,
            raw_finish_reason=raw_reason,
            completion_reason=completion_reason,
            mode=plan.mode,
            reason_codes=tuple(reasons),
            effective_word_count=completion_words,
        )


prose_completion_module = ProseCompletionModule()
