"""Dependency-neutral version identifiers and guards for scene contracts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

SCENE_TRANSITION_CONTRACT_VERSION = "scene_transition_contract.v2"
CURRENT_SCENE_CONTRACT_VERSION = "scene_transition_contract.v3"
MODERN_SCENE_CONTRACT_VERSIONS = frozenset({
    SCENE_TRANSITION_CONTRACT_VERSION, CURRENT_SCENE_CONTRACT_VERSION,
})
CHAPTER_OUTLINE_PROMPT_REVISION = "chapter-outline-prompt-r5"
LEGACY_OUTLINE_ADHERENCE_EVIDENCE_VERSION = (
    "chapter_outline_adherence_evidence.v2"
)
LEGACY_LOCAL_OUTLINE_ADHERENCE_EVIDENCE_VERSION = (
    "chapter_outline_adherence_evidence.v3"
)
OUTLINE_ADHERENCE_EVIDENCE_VERSION = (
    "chapter_outline_adherence_evidence.v4"
)
LEGACY_OUTLINE_ADHERENCE_ISSUE_POLICY_VERSION = (
    "chapter_outline_issue_policy.v1"
)
OUTLINE_ADHERENCE_ISSUE_POLICY_VERSION = "chapter_outline_issue_policy.v2"
COMPLETION_REVIEW_EVIDENCE_VERSION = "chapter_outline_adherence_evidence.v5"
COMPLETION_REVIEW_ISSUE_POLICY_VERSION = "chapter_outline_issue_policy.v3"
CURRENT_OUTLINE_ADHERENCE_POLICIES = {
    OUTLINE_ADHERENCE_EVIDENCE_VERSION: OUTLINE_ADHERENCE_ISSUE_POLICY_VERSION,
    COMPLETION_REVIEW_EVIDENCE_VERSION: COMPLETION_REVIEW_ISSUE_POLICY_VERSION,
}
NARRATIVE_QUALITY_SIDECAR_SCHEMA = "chapter_narrative_quality_sidecar.v1"
NARRATIVE_REPETITION_SIGNAL_POLICY = "narrative_repetition_signal_policy.v1"
NARRATIVE_REPETITION_SIGNAL_LAYERS = (
    "literal_similarity",
    "event_fingerprint",
    "narrative_function",
)
MAX_NARRATIVE_REPETITION_CANDIDATES = 20
OUTLINE_ADHERENCE_DECISIONS = frozenset({
    "pass",
    "repair",
    "manual_review",
})

# A V2 outline is copied into the never-truncated chapter context.  Keep its
# own projection below the shared 8k-token context ceiling with enough room
# for the other mandatory context sections.  UTF-8 bytes are deliberately the
# harder bound here: the supported context estimator counts wide characters
# as one token and Latin text at roughly four characters per token.
MAX_V2_OUTLINE_CONTEXT_UTF8_BYTES = 20_000
MAX_V2_OUTLINE_RESPONSE_UTF8_BYTES = 16_000
MAX_V3_OUTLINE_CONTEXT_UTF8_BYTES = 40_000
MAX_V3_OUTLINE_RESPONSE_UTF8_BYTES = 32_000
# Transport protection is distinct from canonical, non-ASCII-escaped JSON.
MAX_V3_OUTLINE_RAW_UTF8_BYTES = 128_000
OUTLINE_RESPONSE_BYTE_BUDGET_REASON_CODE = (
    "outline_response_byte_budget_exceeded"
)

SceneContractVersion = Literal[
    "legacy_v1", "scene_transition_contract.v2", "scene_transition_contract.v3"
]


def outline_context_byte_cap(version: str | None) -> int:
    return (
        MAX_V3_OUTLINE_CONTEXT_UTF8_BYTES
        if version == CURRENT_SCENE_CONTRACT_VERSION
        else MAX_V2_OUTLINE_CONTEXT_UTF8_BYTES
    )


def outline_response_byte_cap(version: str | None) -> int:
    return (
        MAX_V3_OUTLINE_RESPONSE_UTF8_BYTES
        if version == CURRENT_SCENE_CONTRACT_VERSION
        else MAX_V2_OUTLINE_RESPONSE_UTF8_BYTES
    )


OutlineAdherenceDecision = Literal["pass", "repair", "manual_review"]


def current_outline_adherence_decision(
    value: Mapping[str, Any] | None,
) -> OutlineAdherenceDecision | None:
    """Read a decision only when both current evidence contracts match."""

    if not isinstance(value, Mapping):
        return None
    evidence_version = value.get("evidence_schema_version")
    if (
        not isinstance(evidence_version, str)
        or evidence_version not in CURRENT_OUTLINE_ADHERENCE_POLICIES
        or value.get("issue_policy_version")
        != CURRENT_OUTLINE_ADHERENCE_POLICIES[evidence_version]
    ):
        return None
    decision = value.get("decision")
    return decision if decision in OUTLINE_ADHERENCE_DECISIONS else None


def require_known_scene_contract_version(
    outline: Mapping[str, Any] | None,
) -> SceneContractVersion:
    """Classify supported versions; unknown values fail closed."""

    version = (outline or {}).get("scene_contract_version")
    if version is None:
        return "legacy_v1"
    if isinstance(version, str) and version in MODERN_SCENE_CONTRACT_VERSIONS:
        return version
    raise ValueError(f"unknown scene_contract_version: {version!r}")
