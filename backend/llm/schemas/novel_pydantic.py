from typing import List, Literal, Optional
from pydantic import BaseModel, Field, ConfigDict, model_validator


class ExpandIdeaSchema(BaseModel):
    """
    将用户创意扩写为完整长篇小说压缩故事
    """
    model_config = ConfigDict(extra="forbid")

    plot: str = Field(
        ...,
        min_length=200,
        max_length=5000,
        description="完整的约3000字长篇压缩故事正文"
    )


class ExtractIdeaSchema(BaseModel):
    """
    从扩展剧情中提炼出的故事构思要素
    """
    model_config = ConfigDict(extra="forbid")

    genre: str = Field(
        ...,
        min_length=1,
        max_length=30,
        description="小说类型，如玄幻、科幻、都市、悬疑、历史、仙侠等"
    )
    tone: str = Field(
        ...,
        min_length=1,
        max_length=30,
        description="整体基调，如热血、黑暗、轻松、搞笑、治愈、压抑等"
    )
    target_audience: str = Field(
        ...,
        min_length=1,
        max_length=30,
        description="目标读者群体，如男频、女频、青少年、泛幻想读者等"
    )
    core_idea: str = Field(
        ...,
        min_length=10,
        max_length=300,
        description="用1-2句话描述故事的核心设想"
    )


class CoreSeedSchema(BaseModel):
    """
    雪花写作法第一步生成的故事核心公式
    """
    model_config = ConfigDict(extra="forbid")

    core_seed: str = Field(
        ...,
        min_length=30,
        max_length=150,
        description="故事核心公式，需包含显性冲突、潜在危机、人物核心驱动力与世界观关键矛盾暗示，长度30-100字"
    )


class NovelMetaSchema(BaseModel):
    """
    小说整体设定
    """
    model_config = ConfigDict(extra="forbid")

    title: str = Field(
        ...,
        min_length=1,
        max_length=30,
        description="小说主标题，具有吸引力和传播性，符合类型读者审美"
    )
    subtitle: str = Field(
        ...,
        min_length=1,
        max_length=50,
        description="副标题，补充核心冲突或主题，具有一定文学感或商业感"
    )
    introduction: str = Field(
        ...,
        min_length=100,
        max_length=300,
        description="小说引言，100-300字，引入故事，吸引读者阅读兴趣"
    )
    summary: str = Field(
        ...,
        min_length=100,
        max_length=500,
        description="小说简介，100-500字，需清晰呈现主线冲突与悬念"
    )
    worldview: str = Field(
        ...,
        min_length=100,
        max_length=800,
        description="世界观设定，说明世界规则、力量体系、社会结构等，不少于100字"
    )
    writing_style: str = Field(
        ...,
        min_length=2,
        max_length=50,
        description="创作风格，如偏黑暗现实、轻快幽默、史诗宏大等"
    )
    narrative_pov: Literal["第一人称", "第三人称有限视角", "全知视角"] = Field(
        ...,
        description="叙事视角，只能为第一人称、第三人称有限视角、全知视角之一"
    )
    era_background: str = Field(
        ...,
        min_length=2,
        max_length=50,
        description="时代背景，如架空古代、未来星际、现代都市、末世废土等"
    )
    tags: list[str] = Field(
        default_factory=list,
        description="小说标签，3-5个，概括小说核心元素，如硬科幻、冒险、爱情等"
    )


FactionRelationType = Literal[
    "hostile",
    "allied",
    "cold_war",
    "dependent",
    "subordinate",
    "trade_partner",
    "secret_cooperation",
    "historical_enemy",
]


class CoreFactionSchema(BaseModel):
    """
    全书级核心阵营生成结果。
    """
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=80, description="阵营名称")
    faction_type: str = Field(..., min_length=1, max_length=80, description="阵营类型")
    positioning: str = Field(..., min_length=10, max_length=500, description="阵营在世界结构中的定位")
    public_stance: str = Field(..., min_length=5, max_length=500, description="阵营公开对外立场")
    core_goal: str = Field(..., min_length=5, max_length=500, description="阵营真实核心目标")
    hidden_goal: str = Field(default="", max_length=500, description="阵营隐藏目标")
    resources_and_advantages: list[str] = Field(
        ...,
        min_length=1,
        max_length=6,
        description="阵营主要资源或优势",
    )
    organization_style: str = Field(..., min_length=5, max_length=300, description="组织气质或行动风格")
    core_values: list[str] = Field(..., min_length=1, max_length=6, description="核心价值观")
    conflict_with_mainline: str = Field(..., min_length=10, max_length=600, description="与主线冲突的关系")
    is_public: bool = Field(default=True, description="阵营是否公开存在")
    influence_scope: str = Field(..., min_length=2, max_length=80, description="影响范围")
    expandability: str = Field(..., min_length=5, max_length=500, description="后续可扩展方向")
    tags: list[str] = Field(default_factory=list, max_length=8, description="阵营标签")


class CoreFactionRelationSchema(BaseModel):
    """
    AI 生成阶段使用阵营名称引用的阵营关系。
    """
    model_config = ConfigDict(extra="forbid")

    source_faction_name: str = Field(..., min_length=1, max_length=80, description="关系发起方阵营名称")
    target_faction_name: str = Field(..., min_length=1, max_length=80, description="关系目标方阵营名称")
    relation_type: FactionRelationType = Field(..., description="阵营关系类型")
    current_state: str = Field(..., min_length=5, max_length=500, description="当前关系状态")
    core_conflict: str = Field(..., min_length=5, max_length=600, description="关系中的核心矛盾")
    hidden_tension: str = Field(default="", max_length=500, description="尚未爆发的深层张力")
    possible_change: str = Field(..., min_length=5, max_length=500, description="后续可能变化")
    intensity: int = Field(..., ge=1, le=5, description="关系强度，1到5")
    is_active: bool = Field(default=True, description="关系是否当前有效")


class CoreFactionsResultSchema(BaseModel):
    """
    全书核心阵营和阵营关系的完整生成结果。
    """
    model_config = ConfigDict(extra="forbid")

    core_factions: list[CoreFactionSchema] = Field(
        ...,
        min_length=2,
        max_length=6,
        description="全书级核心阵营列表",
    )
    faction_relations: list[CoreFactionRelationSchema] = Field(
        ...,
        min_length=1,
        max_length=20,
        description="核心阵营之间的关系列表",
    )

    @model_validator(mode="after")
    def validate_faction_relation_references(self) -> "CoreFactionsResultSchema":
        """校验阵营数量、名称唯一性和关系引用。

        Args:
            无。

        Returns:
            校验通过后的当前模型。

        Raises:
            ValueError: 阵营名称重复、关系引用不存在或缺少关键关系类型时抛出。
        """
        faction_names = [faction.name for faction in self.core_factions]
        if len(faction_names) != len(set(faction_names)):
            raise ValueError("核心阵营名称不能重复")

        faction_name_set = set(faction_names)
        relation_types = {relation.relation_type for relation in self.faction_relations}
        complex_types = {"cold_war", "secret_cooperation", "historical_enemy", "dependent"}

        for relation in self.faction_relations:
            if relation.source_faction_name not in faction_name_set:
                raise ValueError(f"关系发起方阵营不存在: {relation.source_faction_name}")
            if relation.target_faction_name not in faction_name_set:
                raise ValueError(f"关系目标方阵营不存在: {relation.target_faction_name}")
            if relation.source_faction_name == relation.target_faction_name:
                raise ValueError("阵营关系不能指向自身")

        if "hostile" not in relation_types and "historical_enemy" not in relation_types:
            raise ValueError("核心阵营关系至少需要一组对立或历史敌对关系")
        if relation_types.isdisjoint(complex_types):
            raise ValueError("核心阵营关系至少需要一组复杂关系")

        return self


class ChapterRangeSchema(BaseModel):
    """一卷覆盖的章序区间（全书章号）。"""
    model_config = ConfigDict(extra="forbid")

    start: int = Field(..., ge=1, description="本卷起始章序（全书编号，从 1 起）")
    end: int = Field(..., ge=1, description="本卷结束章序（全书编号）")

    @model_validator(mode="after")
    def _end_not_before_start(self) -> "ChapterRangeSchema":
        if self.end < self.start:
            raise ValueError(f"chapter_range.end({self.end}) 不能小于 start({self.start})")
        return self


class VolumeOutlineItemSchema(BaseModel):
    """单卷大纲。跨卷区间连续性由 validate_chapter_ranges 校验（需 number_of_chapters）。"""
    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1, max_length=50, description="卷标题")
    summary: str = Field(..., min_length=10, max_length=1000, description="本卷剧情摘要")
    arc: str = Field(..., min_length=5, max_length=500, description="本卷弧线（起承转合）")
    chapter_range: ChapterRangeSchema = Field(..., description="本卷覆盖的章序区间")


class VolumeOutlineResultSchema(BaseModel):
    """AI 分卷大纲生成结果（LLM 输出 schema，非存储 schema）。"""
    model_config = ConfigDict(extra="forbid")

    volumes: list[VolumeOutlineItemSchema] = Field(
        ..., min_length=1, max_length=50, description="分卷列表，按章序递增"
    )


class SceneSchema(BaseModel):
    """章内一场戏。"""
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(..., min_length=1, max_length=500, description="这一场发生了什么")
    purpose: str = Field(..., min_length=1, max_length=200, description="这一场在全局的作用")


class NewThreadSchema(BaseModel):
    """细纲提议的新伏笔。**无 id**——伏笔尚不存在，accept 时创建并把 id 回填进
    chapter.outline.threads_planted（设计 §3.2）。伏笔表在此之前没有任何创建入口。
    """
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=100, description="伏笔名")
    description: str = Field(default="", max_length=1000, description="伏笔说明")
    due_chapter_order: Optional[int] = Field(
        default=None, ge=1, description="预计回收章序（全书章号）；null 表示尚未确定"
    )
    importance: Literal["main", "sub"] = Field(default="sub", description="伏笔重要度")


class ChapterOutlineAuthoredSchema(BaseModel):
    """章节细纲的**作者字段**——生成与编辑共用的公共基（设计 §2.5）。

    抽出来是为了让"AI 能生成的作者字段"与"人能编辑的作者字段"结构性恒等、
    约束不可能漂移。id 字段在这里是**字符串**，落库时转 ObjectId。
    不含 new_threads（仅生成有）、也不含 threads_planted / generated_at /
    edited_by_human（存储 schema 才有、服务端赋值）。
    """
    model_config = ConfigDict(extra="forbid")

    pov_character_card_id: Optional[str] = Field(default=None, description="视角人物卡 id")
    present_character_card_ids: List[str] = Field(default_factory=list, description="本章出场人物卡 id")
    mentioned_character_card_ids: List[str] = Field(
        default_factory=list, description="本章被提及但不出场的人物卡 id"
    )
    referenced_worldbook_card_ids: List[str] = Field(
        default_factory=list, description="本章引用的地点/物品/规则卡 id（worldbook 集合，非 characters）"
    )
    scenes: List[SceneSchema] = Field(..., min_length=1, max_length=20, description="本章场景序列")
    core_conflict: str = Field(..., min_length=1, max_length=500, description="本章核心冲突")
    ending_hook: str = Field(..., min_length=1, max_length=500, description="章末钩子")
    target_word_count: int = Field(..., ge=100, le=50000, description="本章目标字数")
    threads_resolved: List[str] = Field(default_factory=list, description="本章回收的伏笔 id")


class ChapterOutlineResultSchema(ChapterOutlineAuthoredSchema):
    """AI 章节细纲生成结果（**LLM 输出 schema，非存储 schema**，见设计 §3.2）。

    在作者字段基上多一个 new_threads。与库里 chapter.outline 的三处系统性差异：
    - id 字段在这里是**字符串**，落库时转 ObjectId；
    - **没有** threads_planted——它由 accept 创建 new_threads 后回填；
    - **没有** generated_at / edited_by_human——服务端赋值。
    """

    new_threads: List[NewThreadSchema] = Field(
        default_factory=list, max_length=10, description="本章新埋下的伏笔（尚无 id）"
    )


class ChapterOutlineEditSchema(ChapterOutlineAuthoredSchema):
    """人工编辑已存细纲的**编辑输入 schema**（设计 §3/§4）。

    = 作者字段基本身。**不含** new_threads（编辑不能建伏笔）、threads_planted
    （保留不动，服务端从现有 outline 并回）、generated_at（保留原值）、
    edited_by_human（服务端强制 true）。
    """


class PermanentFactProposalSchema(BaseModel):
    """AI 提议的一条永久事实。

    **没有 chapter_order**：它由服务端赋值为本章 order_index（设计 §3.1）。
    该字段驱动"重写旧章时按 chapter_order 过滤"的回溯机制，让模型填等于
    把回溯正确性交给模型，而正确值服务端本来就知道。
    created_at 同理，由 append_permanent_fact 自己打时间戳。
    """
    model_config = ConfigDict(extra="forbid")

    fact: str = Field(..., min_length=1, max_length=500, description="不可逆的既成事实")
    kind: Literal["death", "injury", "identity", "relation", "ability"] = Field(
        ..., description="事实类别；须与 character_state_repository.FACT_KIND_VALUES 一致"
    )


class CharacterStateUpdateSchema(BaseModel):
    """AI 提议的单个角色状态更新。"""
    model_config = ConfigDict(extra="forbid")

    card_id: str = Field(..., min_length=1, description="character 卡片 id（字符串）")
    current_state: str = Field(default="", max_length=1000, description="可覆盖的当下状态")
    new_permanent_facts: List[PermanentFactProposalSchema] = Field(
        default_factory=list, max_length=10, description="本章新确立的永久事实（提议）"
    )


class ThreadStatusUpdateSchema(BaseModel):
    """AI 提议的单个伏笔状态推进。

    只允许 developing / resolved：不新建、不提议 abandoned（设计 §6.2）。
    废弃是对作者意图的判断，读一章正文不足以得出。
    """
    model_config = ConfigDict(extra="forbid")

    thread_id: str = Field(..., min_length=1, description="既有伏笔 id（字符串）")
    status: Literal["developing", "resolved"] = Field(..., description="推进后的状态")
    evidence: str = Field(default="", max_length=500, description="正文依据；给人看，不入库")


class ConsistencyIssueSchema(BaseModel):
    """正文与既有 permanent_facts 的冲突报告（北极星 §6 第二道防线）。

    **不入库、不阻断接受**。回忆/幻觉/诈死/鬼魂都是合法叙事，硬校验误报太多，
    故只标红提示人复核。
    """
    model_config = ConfigDict(extra="forbid")

    card_id: Optional[str] = Field(default=None, description="涉及的角色卡 id；无法归属时为 null")
    fact: str = Field(..., min_length=1, max_length=500, description="被违反的既有永久事实")
    conflict: str = Field(..., min_length=1, max_length=1000, description="正文中与之冲突的内容")


class ChapterStateResultSchema(BaseModel):
    """AI 状态回填结果（**LLM 输出 schema，非存储 schema**，见设计 §3）。"""
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(..., min_length=1, max_length=2000, description="本章摘要 → chapter.summary")
    character_updates: List[CharacterStateUpdateSchema] = Field(
        default_factory=list, max_length=30, description="角色状态更新"
    )
    thread_updates: List[ThreadStatusUpdateSchema] = Field(
        default_factory=list, max_length=20, description="伏笔状态推进"
    )
    consistency_issues: List[ConsistencyIssueSchema] = Field(
        default_factory=list, max_length=20, description="一致性冲突报告；不入库"
    )


class AcceptedCharacterStateSchema(BaseModel):
    """accept 入参里的单个角色：只带**勾上的**永久事实（设计 §5.1）。"""
    model_config = ConfigDict(extra="forbid")

    card_id: str = Field(..., min_length=1)
    current_state: str = Field(default="", max_length=1000)
    accepted_permanent_facts: List[PermanentFactProposalSchema] = Field(
        default_factory=list, max_length=10
    )


class AcceptedThreadUpdateSchema(BaseModel):
    """accept 入参里的单个伏笔变更。**不含 evidence**——它只给人看、不入库。"""
    model_config = ConfigDict(extra="forbid")

    thread_id: str = Field(..., min_length=1)
    status: Literal["developing", "resolved"]


class ChapterStateAcceptSchema(BaseModel):
    """accept 入参（**非 LLM 输出**）。

    勾选结果表达成"只发勾上的"，后端不需要 checked 布尔位。
    consistency_issues 不在此处：它不入库（设计 §6.3）。
    """
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(..., min_length=1, max_length=2000)
    character_updates: List[AcceptedCharacterStateSchema] = Field(
        default_factory=list, max_length=30
    )
    accepted_thread_updates: List[AcceptedThreadUpdateSchema] = Field(
        default_factory=list, max_length=20
    )
