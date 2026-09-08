"""Shared readiness versions for planning, finalization, and failure records."""

from typing import Literal, get_args


INTERACTIVE_COMPLETION_READINESS_SCHEMA = "interactive_chapter_completion_readiness.v2"
SELECTIVE_INTERACTIVE_COMPLETION_READINESS_SCHEMA = "interactive_chapter_completion_readiness.v3"
LEGACY_INTERACTIVE_COMPLETION_READINESS_SCHEMA = "interactive_chapter_completion_readiness.v1"

InteractiveCompletionReadinessVersion = Literal[
    "interactive_chapter_completion_readiness.v2",
    "interactive_chapter_completion_readiness.v3",
]

# Historical V1 grants remain readable by finalization/recovery. New readiness
# still uses the closed V2/V3 model and its existing reauthorization policy.
SUPPORTED_INTERACTIVE_COMPLETION_READINESS_SCHEMAS = frozenset({
    *get_args(InteractiveCompletionReadinessVersion),
    LEGACY_INTERACTIVE_COMPLETION_READINESS_SCHEMA,
})
