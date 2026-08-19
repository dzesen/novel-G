"""Composition root for every executable application capability."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from backend.services.generation.chapter_capability_registry import (
    build_chapter_capability_registry,
)
from backend.services.generation.chapter_generation_application import (
    ChapterGenerationApplicationService,
)
from backend.services.llm.agent_capability_registry import (
    build_agent_capability_registry,
)
from backend.services.llm.capability_registry import (
    CapabilityDefinition,
    CapabilityRegistry,
)


PUBLIC_CAPABILITY_IDS = (
    "chapter_outline",
    "chapter_prose",
    "chapter_state",
    "scene_rewrite",
    "novel_direction",
    "creative_inspiration",
    "continuity_review",
    "style_consistency",
    "illustration_prompt",
    "volume_retrospective",
)


def build_application_capability_registry(
    *,
    chapter_service_factory: Callable[
        [], ChapterGenerationApplicationService
    ] = ChapterGenerationApplicationService,
    agent_access: Any | None = None,
    agent_catalog: Any | None = None,
    agent_runs: Any | None = None,
) -> CapabilityRegistry:
    """Compose core, internal, and Agent tools without duplicating metadata."""

    chapter = build_chapter_capability_registry(
        service_factory=chapter_service_factory,
    )
    agents = build_agent_capability_registry(
        access=agent_access,
        catalog=agent_catalog,
        runs=agent_runs,
    )
    return CapabilityRegistry((*chapter.list(), *agents.list()))


def list_public_capability_definitions() -> tuple[
    CapabilityDefinition,
    ...,
]:
    registry = build_application_capability_registry()
    return tuple(registry.get(capability) for capability in PUBLIC_CAPABILITY_IDS)
