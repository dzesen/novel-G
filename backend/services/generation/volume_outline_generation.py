"""Shared declaration for the volume-outline generation workflow.

The interactive preview route and auto-book structure initialization must use
the same workflow identity, schema and prompt inputs.  Keeping that contract
outside the HTTP adapter prevents the background/application path from
importing a router.
"""

from __future__ import annotations

from typing import Any, Mapping

from backend.llm.schemas.novel_pydantic import VolumeOutlineResultSchema
from backend.services.llm.workflow_runner import WorkflowStep


VOLUME_OUTLINE_WORKFLOW = "create_volume_outline_by_ai"
VOLUME_OUTLINE_STEP = "volume_outline"


def _safe_novel_text(
    novel: Mapping[str, Any],
    field: str,
    fallback: str = "未提供",
) -> str:
    value = novel.get(field)
    if isinstance(value, list):
        rendered = "、".join(
            str(item).strip() for item in value if str(item).strip()
        )
        return rendered or fallback
    return str(value or "").strip() or fallback


def volume_outline_params(novel: Mapping[str, Any]) -> dict[str, Any]:
    """Return the exact prompt parameters shared by preview and execution."""

    return {
        "number_of_chapters": int(novel.get("number_of_chapters") or 100),
        "title": _safe_novel_text(novel, "title"),
        "genre": _safe_novel_text(novel, "genre", "未分类"),
        "tone": _safe_novel_text(novel, "tone"),
        "core_idea": _safe_novel_text(novel, "core_idea"),
        "core_seed": _safe_novel_text(novel, "core_seed"),
        "summary": _safe_novel_text(novel, "summary"),
        "worldview": _safe_novel_text(novel, "worldview"),
        "plot": _safe_novel_text(novel, "plot"),
    }


VOLUME_OUTLINE_STEPS: tuple[WorkflowStep, ...] = (
    WorkflowStep(
        key=VOLUME_OUTLINE_STEP,
        schema=VolumeOutlineResultSchema,
        prompt_args=lambda ctx: {
            "number_of_chapters": ctx.params["number_of_chapters"],
            "title": ctx.params["title"],
            "genre": ctx.params["genre"],
            "tone": ctx.params["tone"],
            "core_idea": ctx.params["core_idea"],
            "core_seed": ctx.params["core_seed"],
            "summary": ctx.params["summary"],
            "worldview": ctx.params["worldview"],
            "plot": ctx.params["plot"],
        },
    ),
)
