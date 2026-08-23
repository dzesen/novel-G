"""Stable terminal vocabulary for streamed text generation."""
from __future__ import annotations

from typing import Any, Literal


FinishReason = Literal[
    "stop",
    "length",
    "content_filter",
    "tool_call",
    "cancelled",
    "error",
    "unreported",
]

INCOMPLETE_FINISH_REASONS: frozenset[FinishReason] = frozenset({
    "length",
    "content_filter",
    "tool_call",
    "cancelled",
    "error",
})


def normalize_finish_reason(raw_reason: Any) -> FinishReason:
    raw = str(raw_reason or "").strip().lower().replace("-", "_").replace(" ", "_")
    if raw in {"stop", "end_turn", "stop_sequence", "complete", "completed"}:
        return "stop"
    if raw in {
        "length",
        "max_tokens",
        "max_token",
        "max_output_tokens",
        "token_limit",
    }:
        return "length"
    if raw in {
        "content_filter",
        "safety",
        "recitation",
        "prohibited_content",
        "blocklist",
        "spii",
    }:
        return "content_filter"
    if raw in {"tool_call", "tool_calls", "tool_use", "function_call"}:
        return "tool_call"
    if raw in {"cancelled", "canceled", "abort", "aborted"}:
        return "cancelled"
    if raw in {"error", "failed", "failure"}:
        return "error"
    return "unreported"
