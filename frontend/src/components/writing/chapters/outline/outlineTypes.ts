/**
 * 对齐后端 schema 的类型定义。
 *
 * 关键区分（2a 设计 §3.2）：**LLM 输出 schema ≠ 存储 schema**。
 * ChapterOutlineResult 是 AI 吐出、也是 accept 接受的形状（id 为字符串、有
 * new_threads、无 threads_planted/generated_at/edited_by_human）；
 * StoredChapterOutline 是库里存的形状。二者不可互换——把后者贴回 accept 会 400，
 * 因为后端用 extra="forbid" 校验（设计 §5.3）。
 */

export interface ChapterRange {
  start: number;
  end: number;
}

export interface VolumeOutlineItem {
  title: string;
  summary: string;
  arc: string;
  chapter_range: ChapterRange;
}

export interface VolumeOutlineResult {
  volumes: VolumeOutlineItem[];
}

export interface LegacyScene {
  summary: string;
  purpose: string;
}

export interface SceneCondition {
  condition_id: string;
  description: string;
}

export interface SceneBeat {
  beat_id: string;
  description: string;
  expected_transition: string;
  required: boolean;
}

export interface NarrativeDelta {
  delta_id: string;
  dimension: "knowledge" | "relationship" | "goal" | "risk" | "choice" | "emotion";
  before: string;
  after: string;
}

export interface SceneTransitionContract extends LegacyScene {
  contract_version: "scene_transition_contract.v2";
  scene_id: string;
  preconditions: SceneCondition[];
  beats: SceneBeat[];
  postconditions: SceneCondition[];
  forbidden_conditions: SceneCondition[];
  narrative_delta: NarrativeDelta[];
  event_key: string;
  repetition_policy: "forbid" | "allow_if_escalated" | "allow";
  word_budget: { min: number; target: number; max: number };
}

export type Scene = LegacyScene | SceneTransitionContract;

export function isSceneTransitionContract(
  scene: Scene,
): scene is SceneTransitionContract {
  return (
    "contract_version" in scene &&
    scene.contract_version === "scene_transition_contract.v2"
  );
}

export type ThreadImportance = "main" | "sub";

export type DueTarget =
  | { kind: "chapter"; chapter_id: string }
  | { kind: "planned_ordinal"; ordinal: number };

export interface NewThread {
  name: string;
  description: string;
  due_target: DueTarget | null;
  /** 仅用于读取旧预览；保存时后端转换为 due_target。 */
  due_chapter_order?: number | null;
  importance: ThreadImportance;
}

/** AI 输出 / accept 入参的细纲形状。 */
export interface ChapterOutlineResult {
  scene_contract_version?: "scene_transition_contract.v2" | null;
  pov_character_card_id: string | null;
  present_character_card_ids: string[];
  mentioned_character_card_ids: string[];
  referenced_worldbook_card_ids: string[];
  scenes: Scene[];
  core_conflict: string;
  ending_hook: string;
  target_word_count: number;
  threads_resolved: string[];
  new_threads: NewThread[];
}

/** 库里存的细纲形状；**不可**直接贴回 accept（见文件头注释）。 */
export interface StoredChapterOutline {
  scene_contract_version?: "scene_transition_contract.v2" | null;
  pov_character_card_id: string | null;
  present_character_card_ids: string[];
  mentioned_character_card_ids: string[];
  referenced_worldbook_card_ids: string[];
  scenes: Scene[];
  core_conflict: string;
  ending_hook: string;
  target_word_count: number;
  threads_resolved: string[];
  threads_planted: string[];
  generated_at?: string;
  edited_by_human?: boolean;
}

/** context 帧（设计 §7.1）。两个字段都要用上：只看 truncated_sections 会把
 *  "部分被丢"误读成"完整"。 */
export interface ContextReport {
  truncated_sections: string[];
  dropped_item_counts: Record<string, number>;
}

/** id_validation 帧（设计 §7.2）：字段名 → 被剔除的 id 列表。 */
export type DroppedIds = Record<string, string[]>;

/** id_remapping 帧：唯一名称/别名被确定性转换为正式角色 ID。 */
export interface RemappedReference {
  field: string;
  from: string;
  to: string;
  matched_by: "name" | "alias";
}

export type ThreadStatus = "planted" | "developing" | "resolved" | "abandoned";

export type ThreadSource = "outline" | "manual";

export interface PlotThread {
  _id: string;
  novel_id: string;
  name: string;
  description: string;
  status: ThreadStatus;
  importance: ThreadImportance;
  due_chapter_order: number | null;
  due_target?: DueTarget | null;
  planted_chapter_id?: string | null;
  resolved_chapter_id?: string | null;
  planted_chapter_order?: number | null;
  resolved_chapter_order?: number | null;
  notes?: string;
  source?: ThreadSource;
  referenced_by_chapter_orders?: number[];
  referenced_by_chapters?: Array<{ chapter_id: string; book_ordinal: number; label: string }>;
}

/** 细纲的作者字段（生成与编辑共用；= ChapterOutlineResult 去掉 new_threads）。 */
export type ChapterOutlineAuthoredFields = Omit<ChapterOutlineResult, "new_threads">;

/** PUT /api/chapters/{id}/outline 的请求体 outline 字段形状（仅作者字段）。 */
export type ChapterOutlineEditPayload = ChapterOutlineAuthoredFields;

export interface AcceptVolumeOutlineResponse {
  volume_count: number;
  chapter_count: number;
  volume_ids: string[];
  next_route: { area: "world"; view: "baseline" };
}

export interface AcceptChapterOutlineResponse {
  chapter_id: string;
  created_thread_ids: string[];
  /** 非空即表示覆盖了旧细纲、上次创建的伏笔成了孤儿（设计 §7.4）。必须显示。 */
  previous_thread_ids: string[];
}
