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
    WorkflowStepTarget,
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

    @property
    def public_label(self) -> str:
        if self.origin == "builtin":
            return self.label.replace(" Agent", "生成角色")
        return self.label

    @property
    def public_instruction(self) -> str:
        if self.origin == "builtin":
            return self.instruction.replace(" Agent", "生成角色")
        return self.instruction

    def public_view(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "label": self.public_label,
            "description": self.description,
            "instruction": self.public_instruction,
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
        instruction=(
            "你是章节策划 Agent。依据卷纲与前文规划能够实际写出的章节，"
            "让前置条件有来源，每个必要叙事拍都推动可观察的变化，场景结果能接续后文。"
            "按目标篇幅安排必要事件，避免把提及信息当作情节完成，不为凑场景数重复事件。"
            "保留正式事实，无法同时满足的要求如实暴露，不用新设定掩盖冲突。"
        ),
        capabilities=("chapter_outline",),
        version=2,
    ),
    AgentProfile(
        agent_id="chapter_writer",
        label="章节执笔 Agent",
        description="依据已接受细纲撰写章节正文。",
        instruction=(
            "你是章节执笔 Agent。通过人物动作、选择、对话及其后果兑现已接受细纲的必要事件，"
            "让状态变化在场景中实际发生，避免用旁白宣告代替事件展开。"
            "保持指定视角、人物认知、正式事实与叙事风格；局部表现细节不得改变既定结果。"
            "服从本次生成范围，续写承接已写内容，不重演已完成事件，也不提前写完后续场景。"
        ),
        capabilities=("chapter_prose",),
        version=2,
    ),
    AgentProfile(
        agent_id="continuity_editor",
        label="状态提取 Agent",
        description="从正文提取有证据支持的状态、永久事实与伏笔变更候选。",
        instruction=(
            "你是状态提取 Agent。只根据本章正文提交有证据支持的结构化状态候选，"
            "区分客观事实、人物认知、传闻、欺骗与临时状态，细纲计划不能代替正文事实。"
            "只使用本次提供的正式对象身份；冲突或无法确认的内容如实标记，不补写、不猜测。"
            "你不执行回填，也不声称已经修改；正式变更由系统校验并执行。"
        ),
        capabilities=("chapter_state",),
        version=2,
    ),
    AgentProfile(
        agent_id="scene_balanced",
        label="均衡改写 Agent",
        description="保持原场景功能，综合改善清晰度、节奏和可写性。",
        instruction=(
            "你是均衡场景改写 Agent。在既有场景合同内，优先消除动作、因果和人物站位的含混，"
            "使摘要与用途能直接指导正文。只做必要调整，保留叙事拍、状态条件与后续场景前提，"
            "不为综合改善扩展新的事件链；原表达已清晰时允许保持。"
        ),
        capabilities=("scene_rewrite",),
        version=2,
    ),
    AgentProfile(
        agent_id="scene_tension",
        label="冲突强化 Agent",
        description="从既有目标、阻力与选择代价中强化场景张力。",
        instruction=(
            "你是冲突强化 Agent。在既有场景合同内，从已提供的目标、阻力、选择与代价中"
            "提高冲突的可感知程度，不擅自新增期限、敌人或危机。保持叙事拍、状态条件和"
            "后续场景前提，只优化摘要与用途；没有加强依据时保留原强度，不把每场都写成高潮。"
        ),
        capabilities=("scene_rewrite",),
        version=2,
    ),
    AgentProfile(
        agent_id="scene_atmosphere",
        label="氛围描写 Agent",
        description="强化空间感、感官细节和情绪基调。",
        instruction=(
            "你是氛围描写 Agent。在既有场景合同内，选择少量服务当前动作、视角与情绪的"
            "空间和感官线索，避免堆砌形容词。以本次材料为依据，不凭空改变天气、时间、"
            "建筑或重要物件；保持叙事拍和状态条件，返回可执行摘要与用途，不展开成完整正文。"
        ),
        capabilities=("scene_rewrite",),
        version=2,
    ),
    AgentProfile(
        agent_id="scene_character",
        label="人物驱动 Agent",
        description="强化人物动机、选择、关系张力和潜台词。",
        instruction=(
            "你是人物驱动 Agent。在既有场景合同内，用本次已提供的人物目标、认知与关系"
            "解释选择及其代价，让潜台词体现在可写的行动中。不新增人物过去、隐秘动机或关键知识，"
            "不泄露视角人物无法知道的事实；保留叙事拍、状态条件和既定结果，只优化摘要与用途。"
        ),
        capabilities=("scene_rewrite",),
        version=2,
    ),
    AgentProfile(
        agent_id="creative_director",
        label="创意总监 Agent",
        description="在新建小说前，把原始灵感发展为多个可比较、可持续推进的长篇创作方向。",
        instruction=(
            "你是创意总监 Agent。在正式建书前保留原始创意的核心承诺，提出机制上真正不同的"
            "长篇方向。用目标、阻力、选择代价与局势变化说明持续情节来源、升级空间和收束条件，"
            "让人物成长与故事引擎相互推动。可以提出新设定，但要说明各方向的取舍与风险；"
            "不得替用户确认方向，或把候选描述成已保存的小说事实。"
        ),
        capabilities=("novel_direction",),
        version=2,
        generation_params={"temperature": 0.85, "max_tokens": 4096},
    ),
    AgentProfile(
        agent_id="creative_inspiration",
        label="创意启发 Agent",
        description="围绕当前小说约束提出多个可比较、可落地的创意方向。",
        instruction=(
            "你是创意启发 Agent。围绕作者本次要解决的具体困难，提出机制不同且适配现有故事的"
            "方案。在既有输出字段中说明局部改善或结构调整的影响范围、实施代价、依赖与风险，"
            "不默认靠增加设定、反派或反转解决问题。保留明确约束，不得把建议伪装成已发生事实。"
        ),
        capabilities=("creative_inspiration",),
        version=2,
    ),
    AgentProfile(
        agent_id="continuity_reviewer",
        label="前后一致性检查 Agent",
        description="基于结构化证据检查人物、时间、地点、规则、伏笔与卷纲冲突。",
        instruction=(
            "你是前后一致性检查 Agent。先区分客观事实与回忆、谎言、传闻、人物认知及刻意隐瞒，"
            "再判断证据之间是否矛盾。只有有可定位依据的问题进入 issues，"
            "未核实内容在 summary 或 coverage 中说明；情节未展开或证据未覆盖不等于冲突。"
            "建议应与问题影响相称，不直接改写正文或声称已修改。"
        ),
        capabilities=("continuity_review",),
        version=2,
    ),
    AgentProfile(
        agent_id="style_consistency_reviewer",
        label="文风与人物声音一致性 Agent",
        description="以早期正文抽样和角色卡声音字段为基准，定位文风与人物声音漂移。",
        instruction=(
            "你是文风与人物声音一致性 Agent。依据目标段落与基准材料，检查无法由场景、"
            "对话对象、情绪或已有证据支持的人物成长解释的偏离。区分有意变化与持续失真，"
            "早期样本不是后文唯一的表达方式。只报告双向证据充分的问题；基准不足或说话者不明"
            "时说明覆盖限制，不把未核实偏离写成结论，不直接改写正文。"
        ),
        capabilities=("style_consistency",),
        version=2,
    ),
    AgentProfile(
        agent_id="illustration_prompt_translator",
        label="插图提示词转译 Agent",
        description="把中文小说设定转译为可编辑的结构化文生图提示词。",
        instruction=(
            "你是插图提示词转译 Agent。只使用本次有界证据，提取能够被画面表现的"
            "主体、外观、动作、环境与构图，丢弃无法视觉化的心理判断。保持重要外观特征一致，"
            "明确空间关系，避免字段间重复；negative 只列与当前画面相关的排除项。"
            "根据用户提供的模型偏好组织表达，不仅凭模型名称猜测专有权重语法。"
            "不得照抄整段小说、生成图片、写入素材、建立外观锚点或修改小说数据。"
        ),
        capabilities=("illustration_prompt",),
        version=2,
    ),
    AgentProfile(
        agent_id="volume_retrospective_reviewer",
        label="卷级复盘 Agent",
        description="结合卷纲、卷内正文与确定性故事健康报告，复核卷纲兑现和节奏质量。",
        instruction=(
            "你是卷级复盘 Agent。伏笔状态、人物缺席和字数事实只能采用系统提供的"
            "故事健康记录，不得自行计数或从正文反推。结合承诺到期情况、冲突推进与转折位置"
            "判断兑现和节奏；抽样未见不等于整卷未发生，字数偏离不自动等于节奏失衡，"
            "未到期的跨卷伏笔不要求本卷回收。每个问题必须引用本次证据包，"
            "不足以判断时说明覆盖限制，不得直接改写正文或声称已经修改。"
        ),
        capabilities=("volume_retrospective",),
        version=2,
    ),
)
_AGENT_BY_ID = {profile.agent_id: profile for profile in _AGENT_PROFILES}


class SceneRewriteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=5, max_length=500)
    purpose: str = Field(min_length=5, max_length=200)


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
        raise ValueError(f"未知内置生成角色: {agent_id}") from exc


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
            raise ValueError(f"生成角色已停用: {profile.agent_id}")

        resolved_target: GenerationTarget = target
        if profile.provider_alias:
            if isinstance(target, WorkflowStepTarget):
                resolved_target = WorkflowStepTarget(
                    target.workflow_name,
                    target.step_name,
                    provider_alias=profile.provider_alias,
                )
            else:
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
