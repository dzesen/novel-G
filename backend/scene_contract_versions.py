"""Dependency-neutral version identifiers and guards for scene contracts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

SCENE_TRANSITION_CONTRACT_VERSION = "scene_transition_contract.v2"
CHAPTER_OUTLINE_PROMPT_REVISION = "chapter-outline-prompt-r2"
OUTLINE_ADHERENCE_EVIDENCE_VERSION = (
    "chapter_outline_adherence_evidence.v2"
)

# A V2 outline is copied into the never-truncated chapter context.  Keep its
# own projection below the shared 8k-token context ceiling with enough room
# for the other mandatory context sections.  UTF-8 bytes are deliberately the
# harder bound here: the supported context estimator counts wide characters
# as one token and Latin text at roughly four characters per token.
MAX_V2_OUTLINE_CONTEXT_UTF8_BYTES = 20_000
MAX_V2_OUTLINE_RESPONSE_UTF8_BYTES = 16_000

SceneContractVersion = Literal["legacy_v1", "scene_transition_contract.v2"]


def require_known_scene_contract_version(
    outline: Mapping[str, Any] | None,
) -> SceneContractVersion:
    """Classify only the two supported versions; unknown values fail closed."""

    version = (outline or {}).get("scene_contract_version")
    if version is None:
        return "legacy_v1"
    if version == SCENE_TRANSITION_CONTRACT_VERSION:
        return SCENE_TRANSITION_CONTRACT_VERSION
    raise ValueError(f"unknown scene_contract_version: {version!r}")
