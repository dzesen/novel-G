"""Typed blueprint inputs and deterministic step dependencies, independent of HTTP."""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from backend.novel_scale import ChapterCount, CreationIdea, WordsPerChapter
from backend.llm.schemas.novel_pydantic import ExpandIdeaSchema, ExtractIdeaSchema, CoreSeedSchema, NovelMetaSchema
from backend.services.llm.generation_params import GenerationParamsMixin
from backend.services.llm.agent_orchestrator import CreativeDirectionSelection
from backend.services.llm.agent_capability_contracts import CardImportDirectionReference
from backend.services.llm.workflow_runner import WorkflowStep
from backend.services.generation.author_brief import AuthorConstraints, creation_author_brief, render_author_brief_record

BLUEPRINT_WORKFLOW_PROTOCOL = "blueprint_workflow.v1"
WORKFLOW_NAME = "create_novel_by_ai"

AI_CREATE_STEPS: tuple[WorkflowStep, ...] = (
    WorkflowStep(
        key="expand_idea",
        # 唯一一个两套词汇不同名的步骤：另外三步恰好同名，这个区别极易被忽略。
        config_key="expand_idea_to_full_novel_story",
        schema=ExpandIdeaSchema,
        prompt_context=lambda ctx: render_author_brief_record(ctx.params["author_brief"]),
        prompt_args=lambda ctx: {"user_idea": ctx.params["user_idea"]},
    ),
    WorkflowStep(
        key="extract_idea",
        schema=ExtractIdeaSchema,
        prompt_context=lambda ctx: render_author_brief_record(ctx.params["author_brief"]),
        prompt_args=lambda ctx: {"plot": ctx.results["expand_idea"].plot},
    ),
    WorkflowStep(
        key="core_seed",
        schema=CoreSeedSchema,
        prompt_context=lambda ctx: render_author_brief_record(ctx.params["author_brief"]),
        prompt_args=lambda ctx: {
            "plot": ctx.results["expand_idea"].plot,
            "genre": ctx.results["extract_idea"].genre,
            "tone": ctx.results["extract_idea"].tone,
            "target_audience": ctx.results["extract_idea"].target_audience,
            "core_idea": ctx.results["extract_idea"].core_idea,
            "number_of_chapters": ctx.params["number_of_chapters"],
            "words_per_chapter": ctx.params["words_per_chapter"],
        },
    ),
    WorkflowStep(
        key="novel_meta",
        schema=NovelMetaSchema,
        prompt_context=lambda ctx: render_author_brief_record(ctx.params["author_brief"]),
        prompt_args=lambda ctx: {
            "plot": ctx.results["expand_idea"].plot,
            "genre": ctx.results["extract_idea"].genre,
            "tone": ctx.results["extract_idea"].tone,
            "target_audience": ctx.results["extract_idea"].target_audience,
            "core_idea": ctx.results["extract_idea"].core_idea,
            "number_of_chapters": ctx.params["number_of_chapters"],
            "words_per_chapter": ctx.params["words_per_chapter"],
            "core_seed": ctx.results["core_seed"].core_seed,
        },
    ),
)

AI_CREATE_STEP_ORDER: tuple[str, ...] = tuple(step.key for step in AI_CREATE_STEPS)


class AICreateCachedSteps(BaseModel):
    """AI 创建小说流程的可复用步骤缓存。

    Args:
        expand_idea: 已完成的扩写完整剧情结果。
        extract_idea: 已完成的提炼创意结果。
        core_seed: 已完成的故事核心结果。
        novel_meta: 已完成的小说设定结果。

    Returns:
        请求体中的缓存步骤会被 Pydantic 校验为对应 schema 实例。
    """

    expand_idea: ExpandIdeaSchema | None = None
    extract_idea: ExtractIdeaSchema | None = None
    core_seed: CoreSeedSchema | None = None
    novel_meta: NovelMetaSchema | None = None


def _get_contiguous_cached_steps(cached_steps: AICreateCachedSteps | None) -> dict[str, BaseModel]:
    """读取从第一步开始连续存在的缓存步骤。

    Args:
        cached_steps: 前端传入的可选缓存步骤。

    Returns:
        只包含连续前缀的缓存字典；中间断档后的缓存会被忽略，避免错误续跑。
    """
    if cached_steps is None:
        return {}

    prefix: dict[str, BaseModel] = {}
    for step_name in AI_CREATE_STEP_ORDER:
        cached_value = getattr(cached_steps, step_name)
        if cached_value is None:
            break
        # 只信任连续前缀，后续步骤即使传入也会从断点重新生成。
        prefix[step_name] = cached_value
    return prefix


class AICreateNovelRequest(GenerationParamsMixin):
    user_idea: CreationIdea
    number_of_chapters: ChapterCount = 100
    words_per_chapter: WordsPerChapter = 3000
    creative_direction: CreativeDirectionSelection | None = None
    author_constraints: AuthorConstraints = Field(default_factory=AuthorConstraints)
    cached_steps: AICreateCachedSteps | None = None

    @model_validator(mode="after")
    def validate_author_input(self):
        creation_author_brief(self)
        return self


class BlueprintGenerationRequest(AICreateNovelRequest):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    draft_id: str | None = Field(default=None, min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    reuse_run_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{24}$")
    card_imports: list[CardImportDirectionReference] = Field(default_factory=list, max_length=100)
    strategy: Literal["four_step"] = "four_step"
    max_tokens: int | None = Field(default=16_384, ge=1, le=200_000)
    system_prompt: str | None = Field(default=None, max_length=20_000)
    token_budget: int | None = Field(default=None, ge=1, le=2**63 - 1, strict=True)
    allow_failure_retry: bool = False


class BlueprintGenerationStartRequest(BlueprintGenerationRequest):
    run_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    readiness_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    acknowledge_automatic_token_budget: bool = False
    acknowledge_uncertain_source: bool = False


class BlueprintResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    readiness_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


def workflow_params(request: AICreateNovelRequest):
    return {
        "author_brief": creation_author_brief(request).to_record(),
        "user_idea": request.user_idea,
        "number_of_chapters": request.number_of_chapters,
        "words_per_chapter": request.words_per_chapter,
    }
