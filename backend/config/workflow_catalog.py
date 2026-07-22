"""内置工作流目录：运行时归一化和设置页共享的唯一注册表。"""

from __future__ import annotations

from pydantic import BaseModel


class WorkflowStepDefinition(BaseModel):
    name: str
    label_key: str


class WorkflowDefinition(BaseModel):
    name: str
    label_key: str
    steps: tuple[WorkflowStepDefinition, ...]


_WORKFLOW_CATALOG: tuple[WorkflowDefinition, ...] = (
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
        name="create_volume_outline_by_ai",
        label_key="settings.workflow.catalog.create_volume_outline_by_ai",
        steps=(WorkflowStepDefinition(name="volume_outline", label_key="settings.workflow.steps.volume_outline"),),
    ),
    WorkflowDefinition(
        name="create_chapter_outline_by_ai",
        label_key="settings.workflow.catalog.create_chapter_outline_by_ai",
        steps=(WorkflowStepDefinition(name="chapter_outline", label_key="settings.workflow.steps.chapter_outline"),),
    ),
    WorkflowDefinition(
        name="write_chapter_by_ai",
        label_key="settings.workflow.catalog.write_chapter_by_ai",
        steps=(WorkflowStepDefinition(name="chapter_content", label_key="settings.workflow.steps.chapter_content"),),
    ),
    WorkflowDefinition(
        name="extract_chapter_state_by_ai",
        label_key="settings.workflow.catalog.extract_chapter_state_by_ai",
        steps=(WorkflowStepDefinition(name="chapter_state", label_key="settings.workflow.steps.chapter_state"),),
    ),
)


def get_workflow_catalog() -> tuple[WorkflowDefinition, ...]:
    """返回不可变工作流目录。"""
    return _WORKFLOW_CATALOG


WORKFLOW_STEPS: dict[str, tuple[str, ...]] = {
    workflow.name: tuple(step.name for step in workflow.steps)
    for workflow in _WORKFLOW_CATALOG
}
