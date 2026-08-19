"""Typed request and response contracts shared by Agent HTTP and Runtime."""

from __future__ import annotations

from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.api.llm_routers._common import GenerationParamsMixin
from backend.llm.models import TokenUsage
from backend.services.interop.card_import_proposal_service import (
    DIRECTION_CONTEXT_MAX_PROPOSALS,
)
from backend.services.llm.agent_orchestrator import (
    ContinuityReviewResult,
    CreativeDirectionResult,
    CreativeInspirationResult,
    IllustrationPromptResult,
    SceneRewriteResult,
    StyleConsistencyResult,
    VolumeRetrospectiveResult,
)


class AgentScopeRequest(GenerationParamsMixin):
    model_config = ConfigDict(extra="forbid")

    novel_id: str = Field(min_length=1)
    scope: Literal["novel", "volume", "chapter"] = "novel"
    volume_id: str | None = None
    chapter_id: str | None = None
    agent_id: str = Field(min_length=1)
    instruction: str = Field(default="", max_length=2000)


class CreativeInspirationRequest(AgentScopeRequest):
    question: str = Field(min_length=2, max_length=2000)
    constraints: str = Field(default="", max_length=2000)
    idea_count: int = Field(default=4, ge=2, le=8)


class CardImportDirectionReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str = Field(min_length=1, max_length=64)
    digest: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )


class CreativeDirectorRequest(GenerationParamsMixin):
    """Inputs available before a novel resource exists."""

    model_config = ConfigDict(extra="forbid")

    user_idea: str = Field(default="", max_length=8000)
    number_of_chapters: int = Field(default=100, ge=1, le=1000)
    words_per_chapter: int = Field(default=3000, ge=500, le=50000)
    agent_id: str = Field(min_length=1, max_length=120)
    instruction: str = Field(default="", max_length=2000)
    direction_count: int = Field(default=3, ge=2, le=4)
    card_imports: list[CardImportDirectionReference] = Field(
        default_factory=list,
        max_length=DIRECTION_CONTEXT_MAX_PROPOSALS,
    )

    @model_validator(mode="after")
    def validate_creation_source(self):
        if len(self.user_idea.strip()) < 2 and not self.card_imports:
            raise ValueError(
                "Creative Director requires a user idea or reviewed card imports"
            )
        return self


class ContinuityReviewRequest(AgentScopeRequest):
    focus: str = Field(default="", max_length=2000)


class StyleConsistencyRequest(AgentScopeRequest):
    scope: Literal["chapter", "volume"]
    focus: str = Field(default="", max_length=2000)


class IllustrationPromptRequest(AgentScopeRequest):
    scope: Literal["character", "novel", "chapter"] = "novel"
    volume_id: None = None
    character_card_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
    )
    target_model: str = Field(default="", max_length=200)
    focus: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def validate_target_identifiers(self):
        if self.scope == "character":
            if not self.character_card_id or self.chapter_id:
                raise ValueError(
                    "character scope requires only character_card_id"
                )
        elif self.scope == "chapter":
            if not self.chapter_id or self.character_card_id:
                raise ValueError(
                    "chapter scope requires only chapter_id"
                )
        elif self.character_card_id or self.chapter_id:
            raise ValueError("novel scope does not accept target IDs")
        return self


class VolumeRetrospectiveRequest(AgentScopeRequest):
    scope: Literal["volume"] = "volume"
    volume_id: str = Field(min_length=1)
    chapter_id: None = None
    focus: str = Field(default="", max_length=2000)


class SceneSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=1200)
    purpose: str = Field(min_length=1, max_length=600)


class RewriteChapterSceneRequest(GenerationParamsMixin):
    model_config = ConfigDict(extra="forbid")

    novel_id: str = Field(min_length=1)
    chapter_id: str = Field(min_length=1)
    scene_index: int = Field(ge=0)
    base_scene: SceneSnapshot
    scene: SceneSnapshot
    agent_id: str = Field(default="scene_balanced", min_length=1)
    instruction: str = Field(default="", max_length=1200)


class AgentAttemptView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt_id: str
    provider_alias: str
    phase: str
    state: str
    usage: TokenUsage


ResultT = TypeVar("ResultT", bound=BaseModel)


class AgentPreviewResponse(BaseModel, Generic[ResultT]):
    model_config = ConfigDict(extra="forbid")

    result: ResultT
    run_id: str
    agent_id: str
    agent_version: int
    provider_alias: str
    usage: TokenUsage
    attempts: list[AgentAttemptView]
    context_report: dict[str, Any]
    context_snapshot: dict[str, Any]
    write_policy: Literal["preview_only"] = "preview_only"


class CreativeInspirationResponse(
    AgentPreviewResponse[CreativeInspirationResult]
):
    pass


class ContinuityReviewResponse(
    AgentPreviewResponse[ContinuityReviewResult]
):
    pass


class StyleConsistencyResponse(
    AgentPreviewResponse[StyleConsistencyResult]
):
    pass


class IllustrationPromptResponse(
    AgentPreviewResponse[IllustrationPromptResult]
):
    pass


class VolumeRetrospectiveResponse(
    AgentPreviewResponse[VolumeRetrospectiveResult]
):
    pass


class CreativeDirectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: CreativeDirectionResult
    agent_id: str
    agent_version: int
    provider_alias: str
    usage: TokenUsage
    attempts: list[AgentAttemptView]
    write_policy: Literal["preview_only"] = "preview_only"
    card_context_digest: str | None = None
    card_context_report: dict[str, Any] | None = None


class SceneRewriteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scene: SceneRewriteResult
    agent_id: str
    provider_alias: str
    usage: TokenUsage
    context_report: dict[str, Any]
