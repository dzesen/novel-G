"""内置工作流目录：运行时归一化和设置页共享的唯一注册表。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from backend.scene_contract_versions import (
    OUTLINE_ADHERENCE_EVIDENCE_VERSION,
    SCENE_TRANSITION_CONTRACT_VERSION,
)
from backend.state_fact_contract_versions import STATE_FACT_EVIDENCE_VERSION


class WorkflowStepDefinition(BaseModel):
    name: str
    label_key: str
    thinking_mode: Literal["enabled", "disabled"] | None = None
    contract_version: str | None = None


class WorkflowDefinition(BaseModel):
    name: str
    label_key: str
    steps: tuple[WorkflowStepDefinition, ...]


_WORKFLOW_CATALOG: tuple[WorkflowDefinition, ...] = (
    WorkflowDefinition(
        name="create_blueprint_two_step",
        label_key="settings.workflow.catalog.create_blueprint_two_step",
        steps=tuple(
            WorkflowStepDefinition(name=name, label_key=f"settings.workflow.steps.{name}")
            for name in ("story_plan", "blueprint")
        ),
    ),
    WorkflowDefinition(
        name="create_novel_by_ai",
        label_key="settings.workflow.catalog.create_novel_by_ai",
        steps=tuple(
            WorkflowStepDefinition(
                name=name,
                label_key=f"settings.workflow.steps.{name}",
            )
            for name in (
                "expand_idea_to_full_novel_story",
                "extract_idea",
                "core_seed",
                "novel_meta",
            )
        ),
    ),
    WorkflowDefinition(
        name="create_factions_by_ai",
        label_key="settings.workflow.catalog.create_factions_by_ai",
        steps=(WorkflowStepDefinition(name="create_core_factions", label_key="settings.workflow.steps.create_core_factions"),),
    ),
    WorkflowDefinition(
        name="create_reference_cards_by_ai",
        label_key="settings.workflow.catalog.create_reference_cards_by_ai",
        steps=(
            WorkflowStepDefinition(
                name="reference_cards",
                label_key="settings.workflow.steps.reference_cards",
            ),
        ),
    ),
    WorkflowDefinition(
        name="create_volume_outline_by_ai",
        label_key="settings.workflow.catalog.create_volume_outline_by_ai",
        steps=(WorkflowStepDefinition(name="volume_outline", label_key="settings.workflow.steps.volume_outline"),),
    ),
    WorkflowDefinition(
        name="create_chapter_outline_by_ai",
        label_key="settings.workflow.catalog.create_chapter_outline_by_ai",
        steps=(
            WorkflowStepDefinition(
                name="chapter_outline",
                label_key="settings.workflow.steps.chapter_outline",
                contract_version=SCENE_TRANSITION_CONTRACT_VERSION,
            ),
        ),
    ),
    WorkflowDefinition(
        name="write_chapter_by_ai",
        label_key="settings.workflow.catalog.write_chapter_by_ai",
        steps=(
            WorkflowStepDefinition(
                name="chapter_content",
                label_key="settings.workflow.steps.chapter_content",
                thinking_mode="disabled",
            ),
        ),
    ),
    WorkflowDefinition(
        name="extract_chapter_state_by_ai",
        label_key="settings.workflow.catalog.extract_chapter_state_by_ai",
        steps=(
            WorkflowStepDefinition(
                name="chapter_state",
                label_key="settings.workflow.steps.chapter_state",
                contract_version=STATE_FACT_EVIDENCE_VERSION,
            ),
        ),
    ),
    WorkflowDefinition(
        name="remediate_chapter_prose_by_agent",
        label_key="settings.workflow.catalog.remediate_chapter_prose_by_agent",
        steps=(
            WorkflowStepDefinition(
                name="remediation_planner",
                label_key="settings.workflow.steps.remediation_planner",
            ),
            WorkflowStepDefinition(
                name="prose_candidate_rewrite",
                label_key="settings.workflow.steps.prose_candidate_rewrite",
                thinking_mode="disabled",
            ),
            WorkflowStepDefinition(
                name="outline_adherence",
                label_key="settings.workflow.steps.outline_adherence",
                contract_version=OUTLINE_ADHERENCE_EVIDENCE_VERSION,
            ),
        ),
    ),
    WorkflowDefinition(
        name="rewrite_chapter_scene_by_agent",
        label_key="settings.workflow.catalog.rewrite_chapter_scene_by_agent",
        steps=(
            WorkflowStepDefinition(
                name="scene_rewrite",
                label_key="settings.workflow.steps.scene_rewrite",
            ),
        ),
    ),
    WorkflowDefinition(
        name="creative_direction_by_agent",
        label_key="settings.workflow.catalog.creative_direction_by_agent",
        steps=(
            WorkflowStepDefinition(
                name="direction",
                label_key="settings.workflow.steps.direction",
                thinking_mode="disabled",
            ),
        ),
    ),
    WorkflowDefinition(
        name="creative_inspiration_by_agent",
        label_key="settings.workflow.catalog.creative_inspiration_by_agent",
        steps=(
            WorkflowStepDefinition(
                name="inspiration",
                label_key="settings.workflow.steps.inspiration",
            ),
        ),
    ),
    WorkflowDefinition(
        name="continuity_review_by_agent",
        label_key="settings.workflow.catalog.continuity_review_by_agent",
        steps=(
            WorkflowStepDefinition(
                name="review",
                label_key="settings.workflow.steps.review",
            ),
        ),
    ),
    WorkflowDefinition(
        name="style_consistency_by_agent",
        label_key="settings.workflow.catalog.style_consistency_by_agent",
        steps=(
            WorkflowStepDefinition(
                name="review",
                label_key="settings.workflow.steps.style_consistency_review",
            ),
        ),
    ),
    WorkflowDefinition(
        name="illustration_prompt_by_agent",
        label_key="settings.workflow.catalog.illustration_prompt_by_agent",
        steps=(
            WorkflowStepDefinition(
                name="illustration_prompt",
                label_key="settings.workflow.steps.illustration_prompt",
            ),
        ),
    ),
    WorkflowDefinition(
        name="volume_retrospective_by_agent",
        label_key="settings.workflow.catalog.volume_retrospective_by_agent",
        steps=(
            WorkflowStepDefinition(
                name="review",
                label_key="settings.workflow.steps.volume_retrospective_review",
            ),
        ),
    ),
)


def get_workflow_catalog() -> tuple[WorkflowDefinition, ...]:
    """返回不可变工作流目录。"""
    return _WORKFLOW_CATALOG


WORKFLOW_STEPS: dict[str, tuple[str, ...]] = {
    workflow.name: tuple(step.name for step in workflow.steps)
    for workflow in _WORKFLOW_CATALOG
}

WORKFLOW_STEP_DEFINITIONS: dict[
    tuple[str, str], WorkflowStepDefinition
] = {
    (workflow.name, step.name): step
    for workflow in _WORKFLOW_CATALOG
    for step in workflow.steps
}


def get_workflow_step_definition(
    workflow_name: str,
    step_name: str,
) -> WorkflowStepDefinition | None:
    return WORKFLOW_STEP_DEFINITIONS.get((workflow_name, step_name))
