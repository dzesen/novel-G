"""Provider-independent Agent profiles over the deterministic LLM runtime.

The workflow engine remains responsible for ordering, checkpoints, paid-attempt
accounting and database writes. An Agent only contributes a bounded role,
generation defaults and an optional Provider override.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from backend.services.llm.generation_runtime import (
    ExplicitProviderTarget,
    GenerationTarget,
    PromptPlan,
)


@dataclass(frozen=True)
class AgentProfile:
    agent_id: str
    label: str
    description: str
    instruction: str
    capabilities: tuple[str, ...]
    origin: str = "builtin"
    owner_id: str | None = None
    provider_alias: str | None = None
    generation_params: Mapping[str, Any] = field(default_factory=dict)
    enabled: bool = True
    version: int = 1
    visibility: str = "shared"
    editable: bool = False

    def public_view(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "label": self.label,
            "description": self.description,
            "instruction": self.instruction,
            "capabilities": list(self.capabilities),
            "origin": self.origin,
            "owner_id": self.owner_id,
            "provider_alias": self.provider_alias,
            "generation_params": dict(self.generation_params),
            "enabled": self.enabled,
            "version": self.version,
            "visibility": self.visibility,
            "editable": self.editable,
        }


_AGENT_PROFILES: tuple[AgentProfile, ...] = (
    AgentProfile(
        agent_id="chapter_planner",
        label="章节策划 Agent",
        description="依据卷纲、前文和伏笔规划章节细纲。",
        instruction="你是章节策划 Agent。优先保证卷纲服从性、因果连续性和场景功能完整。",
        capabilities=("chapter_outline",),
    ),
    AgentProfile(
        agent_id="chapter_writer",
        label="章节执笔 Agent",
        description="依据已接受细纲撰写章节正文。",
        instruction="你是章节执笔 Agent。严格执行已接受细纲，并保持人物、事实和叙事风格连续。",
        capabilities=("chapter_prose",),
    ),
    AgentProfile(
        agent_id="continuity_editor",
        label="状态提取 Agent",
        description="从正文提取状态、永久事实与伏笔变更。",
        instruction="你是状态提取 Agent。以可追溯事实为准，识别冲突并准确回填结构化状态。",
        capabilities=("chapter_state",),
    ),
    AgentProfile(
        agent_id="scene_balanced",
        label="均衡改写 Agent",
        description="保持原场景功能，综合改善清晰度、节奏和可写性。",
        instruction=(
            "你是均衡场景改写 Agent。保留场景的叙事功能、因果结构和人物站位，"
            "同时提升动作清晰度、节奏与后续正文可写性。"
        ),
        capabilities=("scene_rewrite",),
    ),
    AgentProfile(
        agent_id="scene_tension",
        label="冲突强化 Agent",
        description="强化阻力、风险升级、时间压力与场景转折。",
        instruction=(
            "你是冲突强化 Agent。围绕既定场景目的强化阻力、风险、时间压力与转折，"
            "但不得改变章节主线结果或凭空引入重大设定。"
        ),
        capabilities=("scene_rewrite",),
    ),
    AgentProfile(
        agent_id="scene_atmosphere",
        label="氛围描写 Agent",
        description="强化空间感、感官细节和情绪基调。",
        instruction=(
            "你是氛围描写 Agent。用可供正文展开的空间、感官与情绪线索改善场景摘要，"
            "避免堆砌形容词，并保持原场景功能不变。"
        ),
        capabilities=("scene_rewrite",),
    ),
    AgentProfile(
        agent_id="scene_character",
        label="人物驱动 Agent",
        description="强化人物动机、选择、关系张力和潜台词。",
        instruction=(
            "你是人物驱动 Agent。让场景由人物目标、选择与关系张力推动，"
            "保留既定剧情结果，不篡改人物卡和永久事实。"
        ),
        capabilities=("scene_rewrite",),
    ),
    AgentProfile(
        agent_id="creative_director",
        label="创意总监 Agent",
        description="在新建小说前，把原始灵感发展为多个可比较、可持续推进的长篇创作方向。",
        instruction=(
            "你是创意总监 Agent。你负责在正式创建小说前澄清作品的核心承诺、长篇故事引擎、"
            "人物成长与世界钩子，给出真正不同且可执行的方向。不得替用户直接确认方向，"
            "不得把候选建议描述成已保存的小说事实。"
        ),
        capabilities=("novel_direction",),
        generation_params={"temperature": 0.85, "max_tokens": 2600},
    ),
    AgentProfile(
        agent_id="creative_inspiration",
        label="创意启发 Agent",
        description="围绕当前小说约束提出多个可比较、可落地的创意方向。",
        instruction=(
            "你是创意启发 Agent。提出彼此真正不同的方案，并逐项说明与既有设定、"
            "卷纲和人物弧的适配理由、影响范围与风险。不得把建议伪装成已发生事实。"
        ),
        capabilities=("creative_inspiration",),
    ),
    AgentProfile(
        agent_id="continuity_reviewer",
        label="前后一致性检查 Agent",
        description="基于结构化证据检查人物、时间、地点、规则、伏笔与卷纲冲突。",
        instruction=(
            "你是前后一致性检查 Agent。每个问题必须给出可定位证据，区分确定冲突、"
            "高风险疑点和低置信度提醒；证据不足时不得下结论，也不得直接改写正文。"
        ),
        capabilities=("continuity_review",),
    ),
    AgentProfile(
        agent_id="style_consistency_reviewer",
        label="文风与人物声音一致性 Agent",
        description="以早期正文抽样和角色卡声音字段为基准，定位文风与人物声音漂移。",
        instruction=(
            "你是文风与人物声音一致性 Agent。只报告能够同时给出目标段落与基准证据的"
            "偏离，区分整体文风漂移与具体人物声音漂移；证据不足时不得下结论，"
            "不得直接改写正文。"
        ),
        capabilities=("style_consistency",),
    ),
    AgentProfile(
        agent_id="illustration_prompt_translator",
        label="插图提示词转译 Agent",
        description="把中文小说设定转译为可编辑的结构化文生图提示词。",
        instruction=(
            "你是插图提示词转译 Agent。只使用本次有界证据，提取能够被画面表现的"
            "主体、外观、动作、环境与构图，丢弃无法视觉化的心理判断；根据用户给出的"
            "目标模型偏好组织表达。不得照抄整段小说描述，不得生成图片、写入素材、"
            "建立外观锚点或修改小说数据。"
        ),
        capabilities=("illustration_prompt",),
    ),
    AgentProfile(
        agent_id="volume_retrospective_reviewer",
        label="卷级复盘 Agent",
        description="结合卷纲、卷内正文与确定性故事健康报告，复核卷纲兑现和节奏质量。",
        instruction=(
            "你是卷级复盘 Agent。伏笔状态、人物缺席和字数事实只能采用系统提供的"
            "故事健康记录，不得自行计数或从正文反推；你只判断卷纲承诺是否真正兑现、"
            "关键转折是否发生以及节奏是否失衡。每个问题必须引用本次证据包，"
            "不得直接改写正文或声称已经修改。"
        ),
        capabilities=("volume_retrospective",),
    ),
)
_AGENT_BY_ID = {profile.agent_id: profile for profile in _AGENT_PROFILES}


class SceneRewriteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=5, max_length=1200)
    purpose: str = Field(min_length=5, max_length=600)


class CreativeDirection(BaseModel):
    """One candidate direction presented before the novel creation workflow."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=2, max_length=120)
    pitch: str = Field(min_length=20, max_length=1600)
    core_conflict: str = Field(min_length=10, max_length=1200)
    protagonist_arc: str = Field(min_length=10, max_length=1200)
    story_engine: str = Field(min_length=10, max_length=1200)
    world_hook: str = Field(min_length=10, max_length=1200)
    tone_and_style: str = Field(min_length=2, max_length=500)
    must_keep: list[str] = Field(default_factory=list, max_length=10)
    risks: list[str] = Field(default_factory=list, max_length=8)


class CreativeDirectionResult(BaseModel):
    """Comparable directions returned by the pre-creation Creative Director."""

    model_config = ConfigDict(extra="forbid")

    framing: str = Field(min_length=5, max_length=800)
    directions: list[CreativeDirection] = Field(min_length=2, max_length=4)


class CreativeDirectionSelection(BaseModel):
    """A user-confirmed direction that becomes creation provenance and constraint."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1, max_length=120)
    agent_version: int = Field(ge=1)
    provider_alias: str | None = Field(default=None, max_length=120)
    direction: CreativeDirection
    user_adjustments: str = Field(default="", max_length=2000)
    card_context_digest: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )


class CreativeIdea(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=2, max_length=120)
    concept: str = Field(min_length=10, max_length=1600)
    fit_reason: str = Field(min_length=5, max_length=800)
    affected_elements: list[str] = Field(default_factory=list, max_length=12)
    risks: list[str] = Field(default_factory=list, max_length=8)
    suggested_changes: list[str] = Field(default_factory=list, max_length=12)


class CreativeInspirationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    framing: str = Field(min_length=5, max_length=600)
    ideas: list[CreativeIdea] = Field(min_length=1, max_length=8)


class ContinuityEvidenceReference(BaseModel):
    """A stable, machine-navigable reference into the captured novel evidence."""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(pattern="^(chapter|scene|fact|thread)$")
    label: str = Field(min_length=1, max_length=240)
    excerpt: str = Field(default="", max_length=800)
    chapter_id: str | None = Field(default=None, min_length=1, max_length=64)
    scene_index: int | None = Field(default=None, ge=0)
    fact_id: str | None = Field(default=None, min_length=1, max_length=64)
    thread_id: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_reference_shape(self):
        if self.kind == "chapter" and not self.chapter_id:
            raise ValueError("chapter reference requires chapter_id")
        if self.kind == "scene" and (
            not self.chapter_id or self.scene_index is None
        ):
            raise ValueError("scene reference requires chapter_id and scene_index")
        if self.kind == "fact" and not self.fact_id:
            raise ValueError("fact reference requires fact_id")
        if self.kind == "thread" and not self.thread_id:
            raise ValueError("thread reference requires thread_id")
        return self


class ContinuityIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: str = Field(pattern="^(high|medium|low)$")
    category: str = Field(
        pattern=(
            "^(character_state|timeline|location|world_rule|plot_thread|"
            "volume_outline|faction|lore|other)$"
        )
    )
    location: str = Field(min_length=1, max_length=240)
    evidence: list[str] = Field(min_length=1, max_length=8)
    references: list[ContinuityEvidenceReference] = Field(
        min_length=1,
        max_length=8,
    )
    problem: str = Field(min_length=5, max_length=1200)
    suggestion: str = Field(min_length=5, max_length=1200)
    confidence: float = Field(ge=0, le=1)


class ContinuityReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=5, max_length=1200)
    coverage: str = Field(min_length=5, max_length=800)
    issues: list[ContinuityIssue] = Field(default_factory=list, max_length=40)


class StyleConsistencyEvidenceReference(BaseModel):
    """A style evidence pointer, canonically hydrated after generation."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(min_length=1, max_length=200)
    role: str = Field(pattern="^(target|baseline)$")
    kind: str = Field(pattern="^(chapter_paragraph|character_profile)$")
    label: str = Field(default="", max_length=240)
    excerpt: str = Field(default="", max_length=800)
    chapter_id: str | None = Field(default=None, min_length=1, max_length=64)
    paragraph_index: int | None = Field(default=None, ge=0)
    card_id: str | None = Field(default=None, min_length=1, max_length=64)
    profile_field: str | None = Field(
        default=None,
        pattern="^(dialogue_examples|portrayal_notes)$",
    )
    example_index: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_reference_shape(self):
        if self.kind == "character_profile" and self.role != "baseline":
            raise ValueError(
                "character profile evidence can only be a baseline"
            )
        return self


class StyleConsistencyIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: str = Field(pattern="^(high|medium|low)$")
    category: str = Field(pattern="^(prose_style|character_voice)$")
    location: str = Field(min_length=1, max_length=240)
    character_card_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
    )
    evidence: list[str] = Field(min_length=2, max_length=8)
    references: list[StyleConsistencyEvidenceReference] = Field(
        min_length=2,
        max_length=8,
    )
    baseline: str = Field(min_length=5, max_length=1200)
    deviation: str = Field(min_length=5, max_length=1200)
    suggestion: str = Field(min_length=5, max_length=1200)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_evidence_roles(self):
        target_references = [
            reference
            for reference in self.references
            if reference.role == "target"
        ]
        baseline_references = [
            reference
            for reference in self.references
            if reference.role == "baseline"
        ]
        if not target_references or not baseline_references:
            raise ValueError(
                "style issue requires target and baseline references"
            )
        if any(
            reference.kind != "chapter_paragraph"
            for reference in target_references
        ):
            raise ValueError(
                "style issue targets must be chapter paragraphs"
            )
        if self.category == "prose_style":
            if self.character_card_id is not None:
                raise ValueError(
                    "prose style issue cannot include character_card_id"
                )
            if not any(
                reference.kind == "chapter_paragraph"
                for reference in baseline_references
            ):
                raise ValueError(
                    "prose style issue requires an early chapter baseline"
                )
        else:
            if self.character_card_id is None:
                raise ValueError(
                    "character voice issue requires character_card_id"
                )
            if not any(
                reference.kind == "character_profile"
                for reference in baseline_references
            ):
                raise ValueError(
                    "character voice issue requires a profile baseline"
                )
        return self


class StyleConsistencyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=5, max_length=1200)
    coverage: str = Field(min_length=5, max_length=800)
    issues: list[StyleConsistencyIssue] = Field(
        default_factory=list,
        max_length=40,
    )


ILLUSTRATION_SUBJECT_MAX_CHARACTERS = 1200
ILLUSTRATION_APPEARANCE_MAX_CHARACTERS = 1200
ILLUSTRATION_SCENE_MAX_CHARACTERS = 1600
ILLUSTRATION_STYLE_MAX_CHARACTERS = 800
ILLUSTRATION_NEGATIVE_MAX_CHARACTERS = 800
ILLUSTRATION_PROMPT_TOTAL_CHARACTER_LIMIT = 4800


class IllustrationPromptResult(BaseModel):
    """Editable visual-language fields produced before any image request."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
    )

    subject: str = Field(max_length=ILLUSTRATION_SUBJECT_MAX_CHARACTERS)
    appearance: str = Field(
        max_length=ILLUSTRATION_APPEARANCE_MAX_CHARACTERS
    )
    scene: str = Field(max_length=ILLUSTRATION_SCENE_MAX_CHARACTERS)
    style: str = Field(max_length=ILLUSTRATION_STYLE_MAX_CHARACTERS)
    negative: str = Field(max_length=ILLUSTRATION_NEGATIVE_MAX_CHARACTERS)

    @field_validator(
        "subject",
        "appearance",
        "scene",
        "style",
        "negative",
        mode="before",
    )
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator(
        "subject",
        "appearance",
        "scene",
        "style",
        "negative",
    )
    @classmethod
    def enforce_assignment_total_limit(
        cls,
        value: str,
        info: ValidationInfo,
    ) -> str:
        total = len(value) + sum(
            len(str(other_value or ""))
            for field_name, other_value in info.data.items()
            if field_name
            in {
                "subject",
                "appearance",
                "scene",
                "style",
                "negative",
            }
        )
        if total > ILLUSTRATION_PROMPT_TOTAL_CHARACTER_LIMIT:
            raise ValueError(
                "Illustration prompt exceeds the total character limit"
            )
        return value

    @model_validator(mode="after")
    def enforce_total_limit(self) -> "IllustrationPromptResult":
        total = sum(
            len(value)
            for value in (
                self.subject,
                self.appearance,
                self.scene,
                self.style,
                self.negative,
            )
        )
        if total > ILLUSTRATION_PROMPT_TOTAL_CHARACTER_LIMIT:
            raise ValueError(
                "Illustration prompt exceeds the total character limit"
            )
        return self


class VolumeRetrospectiveEvidenceReference(BaseModel):
    """A pointer into the exact bounded retrospective evidence packet."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(
        min_length=1,
        max_length=200,
        description="Exact evidence_id copied from the supplied evidence packet.",
    )
    role: str = Field(
        pattern="^(promise|outcome|deterministic)$",
        description="Exact role copied from the same evidence record.",
    )
    kind: str = Field(
        pattern=(
            "^(volume_outline|chapter_outline|chapter_prose|"
            "story_health_plot_thread|story_health_character_absence|"
            "story_health_volume_word_count|story_health_chapter_word_count)$"
        ),
        description="Exact kind copied from the same evidence record.",
    )
    label: str = Field(default="", max_length=240)
    excerpt: str = Field(default="", max_length=2000)
    volume_id: str | None = Field(default=None, min_length=1, max_length=64)
    chapter_id: str | None = Field(default=None, min_length=1, max_length=64)
    paragraph_index: int | None = Field(default=None, ge=0)
    thread_id: str | None = Field(default=None, min_length=1, max_length=64)
    card_id: str | None = Field(default=None, min_length=1, max_length=64)


class VolumeRetrospectiveIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: str = Field(pattern="^(high|medium|low)$")
    category: str = Field(
        pattern="^(promise_delivery|plot_thread_payoff|pacing)$"
    )
    location: str = Field(min_length=1, max_length=240)
    evidence: list[str] = Field(min_length=1, max_length=8)
    references: list[VolumeRetrospectiveEvidenceReference] = Field(
        min_length=1,
        max_length=10,
    )
    problem: str = Field(min_length=5, max_length=1200)
    suggestion: str = Field(min_length=5, max_length=1200)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_evidence_shape(self):
        kinds = {reference.kind for reference in self.references}
        roles = {reference.role for reference in self.references}
        if self.category == "promise_delivery":
            if "volume_outline" not in kinds or "chapter_prose" not in kinds:
                raise ValueError(
                    "promise delivery issue requires volume outline and prose evidence"
                )
            if not {"promise", "outcome"}.issubset(roles):
                raise ValueError(
                    "promise delivery issue requires promise and outcome roles"
                )
        elif self.category == "plot_thread_payoff":
            if "story_health_plot_thread" not in kinds:
                raise ValueError(
                    "plot thread issue requires deterministic story health evidence"
                )
        else:
            word_count_kinds = {
                "story_health_volume_word_count",
                "story_health_chapter_word_count",
            }
            if not kinds.intersection(word_count_kinds):
                raise ValueError(
                    "pacing issue requires deterministic word-count evidence"
                )
            if "chapter_prose" not in kinds:
                raise ValueError("pacing issue requires sampled prose evidence")
        return self


class VolumeRetrospectiveResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=5, max_length=1200)
    coverage: str = Field(min_length=5, max_length=1000)
    issues: list[VolumeRetrospectiveIssue] = Field(
        default_factory=list,
        max_length=40,
    )


def get_agent_profile(agent_id: str) -> AgentProfile:
    """Resolve an immutable built-in Agent."""
    try:
        return _AGENT_BY_ID[agent_id]
    except KeyError as exc:
        raise ValueError(f"未知内置 Agent: {agent_id}") from exc


def get_agent_profiles(*, capability: str | None = None) -> tuple[AgentProfile, ...]:
    if capability is None:
        return _AGENT_PROFILES
    return tuple(
        profile for profile in _AGENT_PROFILES if capability in profile.capabilities
    )


def apply_agent_profile(agent_id: str, prompt: str) -> str:
    """Apply a built-in Agent to deterministic workflow prompts."""
    profile = get_agent_profile(agent_id)
    return apply_resolved_agent_profile(profile, prompt)


def apply_resolved_agent_profile(profile: AgentProfile, prompt: str) -> str:
    return (
        f"【Agent 身份：{profile.label}】\n"
        f"{profile.instruction}\n\n"
        f"{prompt}"
    )


class AgentOrchestrator:
    """Apply one resolved Agent profile, then delegate to GenerationRuntime."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    async def generate_structured(
        self,
        *,
        target: GenerationTarget,
        schema: type[BaseModel],
        prompts: PromptPlan,
        agent_id: str | None = None,
        profile: AgentProfile | None = None,
        **gen_kwargs: Any,
    ):
        if profile is None:
            if not agent_id:
                raise ValueError("agent_id 或 profile 至少需要一个")
            profile = get_agent_profile(agent_id)
        if not profile.enabled:
            raise ValueError(f"Agent 已停用: {profile.agent_id}")

        resolved_target: GenerationTarget = target
        if profile.provider_alias:
            resolved_target = ExplicitProviderTarget(profile.provider_alias)
        plan = self.runtime.plan_structured(resolved_target)
        profiled_prompts = PromptPlan(
            native_schema_prompt=apply_resolved_agent_profile(
                profile, prompts.native_schema_prompt
            ),
            prompt_json_prompt=apply_resolved_agent_profile(
                profile, prompts.prompt_json_prompt
            ),
        )
        merged_gen_kwargs = dict(profile.generation_params)
        merged_gen_kwargs.update(
            {key: value for key, value in gen_kwargs.items() if value is not None}
        )
        return await self.runtime.generate_structured(
            plan,
            schema,
            profiled_prompts,
            **merged_gen_kwargs,
        )
