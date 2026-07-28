import type { NovelRewriteFieldKey } from "@/lib/novelFields";
import type { StoredChapterOutline } from "@/components/writing/chapters/outline/outlineTypes";
import type { CreativeDirectionSelection } from "@/types/agent";

export type { NovelRewriteFieldKey } from "@/lib/novelFields";

export interface StyleControls {
  narrative_person?: "first" | "third";
  narrative_distance?: "close" | "medium" | "omniscient";
  pacing?: "tight" | "balanced" | "relaxed";
  prose_density?: "sparse" | "balanced" | "rich";
  dialogue_ratio?: "low" | "medium" | "high";
  content_rating?: "general" | "moderate" | "mature";
  custom_style_note?: string;
}

export interface NovelSummary {
  _id: string;
  title: string;
  subtitle?: string;
  genre: string;
  tags: string[];
  cover_image?: string;
  status: string;
  stats: {
    chapter_count: number;
    total_word_count: number;
  };
  created_at: string;
  updated_at: string;
}

export interface NovelDetail extends NovelSummary {
  introduction?: string;
  summary?: string;
  core_seed?: string;
  worldview?: string;
  writing_style?: string;
  narrative_pov?: string;
  era_background?: string;
  plot?: string;
  tone?: string;
  target_audience?: string;
  core_idea?: string;
  number_of_chapters?: number;
  words_per_chapter?: number;
  style_controls?: StyleControls;
  creation_source?: "manual" | "ai";
  creation_provenance?: {
    creative_director?: CreativeDirectionSelection;
  };
}

export interface CreateNovelRequest {
  title: string;
  subtitle?: string;
  genre?: string;
  tags?: string[];
  introduction?: string;
  summary?: string;
  core_seed?: string;
  worldview?: string;
  writing_style?: string;
  narrative_pov?: string;
  era_background?: string;
  cover_image?: string;
  plot?: string;
  tone?: string;
  target_audience?: string;
  core_idea?: string;
  number_of_chapters?: number;
  words_per_chapter?: number;
  style_controls?: StyleControls;
  creation_mode?: "manual" | "ai";
  creative_direction?: CreativeDirectionSelection;
  card_creation_id?: string;
  card_imports?: CardImportCreationSelection[];
}

export type ChapterStatus = "draft" | "writing" | "completed";

export interface VolumeSummary {
  _id: string;
  novel_id: string;
  title: string;
  summary: string;
  order_index: number;
  status: string;
  chapter_count: number;
  word_count: number;
  updated_at: string;
}

export interface ChapterSummary {
  _id: string;
  novel_id: string;
  volume_id: string;
  title: string;
  summary: string;
  status: ChapterStatus;
  order_index: number;
  word_count: number;
  is_deleted: boolean;
  deleted_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface ChapterDetail extends ChapterSummary {
  content: string;
  /** 已接受的细纲；未接受过的章没有这个字段。后端 chapter_router._serialize_outline
   *  已把其中 6 个 id 字段转成字符串。 */
  outline?: StoredChapterOutline;
}

export interface ChapterDraft {
  title: string;
  summary: string;
  content: string;
  status: ChapterStatus;
}

export type ReferenceCardType =
  | "character"
  | "location"
  | "item"
  | "rule"
  | "lore";

export interface CharacterProfile {
  aliases: string[];
  portrayal_context: string;
  dialogue_examples: string[];
  scene_opening_examples: string[];
  portrayal_notes: string;
}

export interface ReferenceCardInterop {
  writing_participation?: {
    status: "active" | "not_participating";
    label: string;
    projected_fields: string[];
    isolated_fields: string[];
  };
}

export interface ReferenceCard {
  _id: string;
  novel_id: string;
  card_type: ReferenceCardType;
  name: string;
  subtitle: string;
  description: string;
  details: Record<string, string>;
  tags: string[];
  sort_order: number;
  importance: "main" | "sub";
  is_favorite?: boolean;
  character_profile?: CharacterProfile;
  interop?: ReferenceCardInterop;
  is_deleted: boolean;
  deleted_at?: string | null;
  created_at: string;
  updated_at: string;
}

export type ReferenceCardCurationAction =
  | "create"
  | "merge"
  | "restore_merge"
  | "skip";

export interface CardImportDirectionReference {
  proposal_id: string;
  digest: string;
}

export interface CardImportDecision {
  candidate_id: string;
  action: ReferenceCardCurationAction;
  target_card_id?: string;
  overrides?: Record<string, unknown>;
  overwrite_fields?: string[];
}

export interface CardImportCreationSelection
  extends CardImportDirectionReference {
  decisions: CardImportDecision[];
}

export interface CardImportConflict {
  target_card_id: string;
  match_kind: string;
  is_deleted: boolean;
  field_diffs: Record<
    string,
    {
      existing: unknown;
      imported: unknown;
    }
  >;
}

export interface CardImportCandidate {
  candidate_id: string;
  target_type: "character" | "lore";
  fields: {
    name: string;
    subtitle?: string;
    description?: string;
    tags?: string[];
    importance?: "main" | "sub";
    details?: Record<string, string>;
    character_profile?: CharacterProfile;
    interop?: {
      scenario?: string;
      first_mes?: string;
      mes_example?: string;
      creator_notes?: string;
      writing_participation?: ReferenceCardInterop["writing_participation"];
    };
  };
  interop_preview?: {
    name?: string;
    keys?: string[];
    secondary_keys?: string[];
    enabled?: boolean;
    constant?: boolean;
    insertion_order?: number;
    position?: string | number;
    use_regex?: false;
    external_uid?: string | number | null;
    source_locator?: string;
    regex_fields?: string[];
    unrecognized_fields?: string[];
    unsupported_features?: Array<{
      field: string;
      category: string;
      enabled: false;
    }>;
    preview_notices?: Array<{
      code: string;
      path: string;
      message: string;
    }>;
  };
  conflicts: CardImportConflict[];
  recommended_action: ReferenceCardCurationAction;
}

export interface CardImportProposal {
  proposal_id: string;
  novel_id: string | null;
  source_format: "v1" | "v2" | "v3" | "worldbook_standalone";
  source_container: "json" | "png";
  source_name?: string | null;
  digest: string;
  status: "pending_review" | "stale" | "applying" | "applied";
  detected_warnings: string[];
  prompt_risk_fields: Array<{
    kind: string;
    path: string;
    value?: unknown;
    enabled: false;
  }>;
  decorators: Array<{
    path: string;
    name: string;
    value: string;
    raw: string;
    fallback: boolean;
    known: boolean;
    enabled: false;
  }>;
  assets: Array<{
    path: string;
    type: string;
    uri: string;
    name: string;
    ext: string;
    retrieval_enabled: false;
  }>;
  container_preview: {
    selected_png_chunk?: string | null;
    png_chunk_classification?: string | null;
    png_preview_label?: string | null;
    image_data_discarded: boolean;
  };
  worldbook_preview?: {
    source_kind: string;
    source_format: string;
    entry_count: number;
    detected_warnings: string[];
    unrecognized_top_level_fields: string[];
  } | null;
  proposed_cards: CardImportCandidate[];
  apply_result?: ReferenceCardCurationResult;
  duplicate_source?: {
    proposal_id?: string;
    card_id?: string;
    novel_id?: string;
    imported_at?: string;
  } | null;
}

export interface ReferenceCardCandidate {
  candidate_id: string;
  card_type: ReferenceCardType;
  name: string;
  subtitle: string;
  description: string;
  details: Record<string, string>;
  tags: string[];
  importance: "main" | "sub";
  character_profile?: CharacterProfile;
  recommended_action: ReferenceCardCurationAction;
  recommended_target_card_id?: string | null;
  recommended_target?: Partial<ReferenceCard> | null;
  field_conflicts: Array<{
    field: string;
    existing: unknown;
    candidate: unknown;
  }>;
  warnings: Array<{
    code: string;
    message: string;
    card_id?: string;
    card_type?: ReferenceCardType;
    name?: string;
    score?: number;
  }>;
}

export interface ReferenceCardCurationResult {
  proposal_id: string;
  counts: {
    created: number;
    merged: number;
    restored_merged: number;
    skipped: number;
  };
  mappings: Array<{
    candidate_id: string;
    action: ReferenceCardCurationAction;
    card_id: string | null;
  }>;
}

export interface ReferenceCardCurationProposal {
  proposal_id: string;
  novel_id: string;
  status: "proposed" | "claimed" | "applied";
  candidates: Record<
    "characters" | "locations" | "items" | "rules" | "lores",
    ReferenceCardCandidate[]
  >;
  generation_audit: {
    provider_alias?: string;
    structured_output_mode?: string;
    attempt_count?: number;
    usage?: {
      input_tokens?: number;
      output_tokens?: number;
      total_tokens?: number;
    };
  };
  proposal_expires_at: string;
  acceptance_token: string;
  apply_result?: ReferenceCardCurationResult;
}

export interface RewriteChatMessage {
  id: string;
  role: "user" | "assistant";
  target_field: NovelRewriteFieldKey;
  content: string;
  provider?: string;
  status?: "failed";
  error_message?: string;
  created_at: string;
}

export interface RewriteFieldRevision {
  id: string;
  value: string | string[];
  source: "initial" | "manual" | "ai";
  instruction?: string;
  created_at: string;
}

export interface WritingDraftRewriteState {
  messagesByField: Partial<Record<NovelRewriteFieldKey, RewriteChatMessage[]>>;
  revisionsByField: Partial<Record<NovelRewriteFieldKey, RewriteFieldRevision[]>>;
  activeRevisionIdByField: Partial<Record<NovelRewriteFieldKey, string>>;
}

export interface RewriteNovelFieldRequest {
  provider: string;
  target_field: NovelRewriteFieldKey;
  instruction: string;
  current_value: string | string[];
  context: Record<string, unknown>;
  chat_history: Pick<RewriteChatMessage, "role" | "content">[];
}

export interface RewriteNovelFieldResponse {
  target_field: NovelRewriteFieldKey;
  value: string | string[];
}

export type FactionRelationType =
  | "hostile"
  | "allied"
  | "cold_war"
  | "dependent"
  | "subordinate"
  | "trade_partner"
  | "secret_cooperation"
  | "historical_enemy";

export interface GeneratedCoreFaction {
  name: string;
  faction_type: string;
  positioning: string;
  public_stance: string;
  core_goal: string;
  hidden_goal?: string;
  resources_and_advantages: string[];
  organization_style: string;
  core_values: string[];
  conflict_with_mainline: string;
  is_public: boolean;
  influence_scope: string;
  expandability: string;
  tags: string[];
}

export interface CoreFaction extends GeneratedCoreFaction {
  _id?: string;
  novel_id?: string;
  faction_id?: string;
  is_deleted?: boolean;
  deleted_at?: string | null;
  alias?: string[];
  level_type?: string;
  parent_faction_id?: string | null;
  active_status?: string;
  sort_order?: number;
}

export interface FactionRelation {
  _id?: string;
  novel_id?: string;
  relation_id?: string;
  source_faction_id?: string;
  target_faction_id?: string;
  source_faction_name?: string;
  target_faction_name?: string;
  relation_type: FactionRelationType;
  current_state: string;
  core_conflict: string;
  hidden_tension?: string;
  possible_change: string;
  intensity: number;
  is_active: boolean;
}

export interface GeneratedFactionRelation extends FactionRelation {
  source_faction_name: string;
  target_faction_name: string;
}

export interface CoreFactionsPayload {
  core_factions: GeneratedCoreFaction[];
  faction_relations: GeneratedFactionRelation[];
}

export interface GenerateCoreFactionsRequest {
  novel_id: string;
  temperature?: number | null;
  top_p?: number | null;
  max_tokens?: number | null;
  presence_penalty?: number | null;
  frequency_penalty?: number | null;
  system_prompt?: string | null;
  allow_failure_retry?: boolean;
}

export interface BulkCreateCoreFactionsResponse {
  factions: CoreFaction[];
  faction_relations: FactionRelation[];
}

export interface AICreateRequest {
  user_idea: string;
  number_of_chapters?: number;
  words_per_chapter?: number;
  creative_direction?: CreativeDirectionSelection;
  cached_steps?: AICreateCachedSteps;
  // 可选生成参数
  temperature?: number | null;
  top_p?: number | null;
  max_tokens?: number | null;
  presence_penalty?: number | null;
  frequency_penalty?: number | null;
  system_prompt?: string | null;
  allow_failure_retry?: boolean;
}

export interface AICreateStepResult {
  step: string;
  data: Record<string, unknown>;
}

export interface AICreateResponse {
  expand_idea?: {
    plot: string;
  };
  extract_idea: {
    plot?: string;
    genre: string;
    tone: string;
    target_audience: string;
    core_idea: string;
  };
  core_seed: {
    core_seed: string;
  };
  novel_meta: {
    title: string;
    subtitle: string;
    introduction: string;
    summary: string;
    worldview: string;
    writing_style: string;
    narrative_pov: string;
    era_background: string;
    tags: string[];
  };
}

export type AICreateStepKey = "expand_idea" | "extract_idea" | "core_seed" | "novel_meta";

export type AICreateCachedSteps = Partial<{
  expand_idea: NonNullable<AICreateResponse["expand_idea"]>;
  extract_idea: AICreateResponse["extract_idea"];
  core_seed: AICreateResponse["core_seed"];
  novel_meta: AICreateResponse["novel_meta"];
}>;

/** AI 创建草稿，用于本地存储传递到 Writing 创建态 */
export interface WritingDraft extends CreateNovelRequest {
  _fromAI?: boolean;
  _rewriteState?: WritingDraftRewriteState;
}

/** Writing 侧栏导航项 */
export type WritingSidebarItem =
  | "novel-info"
  | "chapter-editor"
  | "agent-studio"
  | "character-cards"
  | "location-cards"
  | "faction-cards"
  | "item-cards"
  | "rule-cards"
  | "lore-cards"
  | "relationship-map"
  | "plot-threads"
  | "story-health"
  | "character-memory";
