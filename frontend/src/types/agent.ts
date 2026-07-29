export type AgentCapabilityId =
  | "chapter_outline"
  | "chapter_prose"
  | "chapter_state"
  | "scene_rewrite"
  | "novel_direction"
  | "creative_inspiration"
  | "continuity_review"
  | "style_consistency"
  | "illustration_prompt"
  | "volume_retrospective";

export type AgentScope = "character" | "novel" | "volume" | "chapter";

export interface AgentCapability {
  capability: AgentCapabilityId;
  version: number;
  label: string;
  description: string;
  customizable: boolean;
  preview_only: boolean;
  scope_options: string[];
  input_contract: string;
  output_contract: string;
  context_policy: string;
  side_effect_policy: "preview_only" | "accept_required" | "system_write";
  handler_id: string;
}

export interface AgentProfile {
  agent_id: string;
  label: string;
  description: string;
  instruction: string;
  capabilities: AgentCapabilityId[];
  origin: "builtin" | "custom";
  owner_id: string | null;
  provider_alias: string | null;
  generation_params: {
    temperature?: number;
    top_p?: number;
    max_tokens?: number;
  };
  enabled: boolean;
  version: number;
  visibility: "private" | "shared";
  editable: boolean;
}

export interface CreativeDirection {
  title: string;
  pitch: string;
  core_conflict: string;
  protagonist_arc: string;
  story_engine: string;
  world_hook: string;
  tone_and_style: string;
  must_keep: string[];
  risks: string[];
}

export interface CreativeDirectionResult {
  framing: string;
  directions: CreativeDirection[];
}

export interface CreativeDirectionSelection {
  agent_id: string;
  agent_version: number;
  provider_alias: string | null;
  direction: CreativeDirection;
  user_adjustments: string;
  card_context_digest?: string | null;
}

export interface CreativeDirectorResponse {
  result: CreativeDirectionResult;
  agent_id: string;
  agent_version: number;
  provider_alias: string;
  usage: {
    input_tokens?: number;
    output_tokens?: number;
    total_tokens?: number;
  };
  attempts: AgentRunAttempt[];
  write_policy: "preview_only";
  card_context_digest?: string | null;
  card_context_report?: {
    character_count: number;
    world_entry_count: number;
    truncated_fields: string[];
    dropped_world_entries: number;
    max_characters: number;
  } | null;
}

export interface AgentProviderOption {
  alias: string;
  type: string;
  model: string;
}

export interface CreativeIdea {
  title: string;
  concept: string;
  fit_reason: string;
  affected_elements: string[];
  risks: string[];
  suggested_changes: string[];
}

export interface CreativeInspirationResult {
  framing: string;
  ideas: CreativeIdea[];
}

export type ContinuitySeverity = "high" | "medium" | "low";

export interface ContinuityEvidenceReference {
  kind: "chapter" | "scene" | "fact" | "thread";
  label: string;
  excerpt?: string;
  chapter_id?: string;
  scene_index?: number;
  fact_id?: string;
  thread_id?: string;
}

export interface ContinuityIssue {
  severity: ContinuitySeverity;
  category:
    | "character_state"
    | "timeline"
    | "location"
    | "world_rule"
    | "plot_thread"
    | "volume_outline"
    | "faction"
    | "lore"
    | "other";
  location: string;
  evidence: string[];
  references: ContinuityEvidenceReference[];
  problem: string;
  suggestion: string;
  confidence: number;
}

export interface ContinuityReviewResult {
  summary: string;
  coverage: string;
  issues: ContinuityIssue[];
}

export interface StyleConsistencyEvidenceReference {
  evidence_id: string;
  role: "target" | "baseline";
  kind: "chapter_paragraph" | "character_profile";
  label: string;
  excerpt: string;
  chapter_id?: string;
  paragraph_index?: number;
  card_id?: string;
  profile_field?: "dialogue_examples" | "portrayal_notes";
  example_index?: number;
}

export interface StyleConsistencyIssue {
  severity: ContinuitySeverity;
  category: "prose_style" | "character_voice";
  location: string;
  character_card_id?: string | null;
  evidence: string[];
  references: StyleConsistencyEvidenceReference[];
  baseline: string;
  deviation: string;
  suggestion: string;
  confidence: number;
}

export interface StyleConsistencyResult {
  summary: string;
  coverage: string;
  issues: StyleConsistencyIssue[];
}

export interface IllustrationPromptResult {
  subject: string;
  appearance: string;
  scene: string;
  style: string;
  negative: string;
}

export interface VolumeRetrospectiveEvidenceReference {
  evidence_id: string;
  role: "promise" | "outcome" | "deterministic";
  kind:
    | "volume_outline"
    | "chapter_outline"
    | "chapter_prose"
    | "story_health_plot_thread"
    | "story_health_character_absence"
    | "story_health_volume_word_count"
    | "story_health_chapter_word_count";
  label: string;
  excerpt: string;
  volume_id?: string;
  chapter_id?: string;
  paragraph_index?: number;
  thread_id?: string;
  card_id?: string;
}

export interface VolumeRetrospectiveIssue {
  severity: ContinuitySeverity;
  category: "promise_delivery" | "plot_thread_payoff" | "pacing";
  location: string;
  evidence: string[];
  references: VolumeRetrospectiveEvidenceReference[];
  problem: string;
  suggestion: string;
  confidence: number;
}

export interface VolumeRetrospectiveResult {
  summary: string;
  coverage: string;
  issues: VolumeRetrospectiveIssue[];
}

export interface AgentToolMetadata {
  run_id: string;
  agent_id: string;
  agent_version: number;
  provider_alias: string;
  usage: {
    input_tokens?: number;
    output_tokens?: number;
    total_tokens?: number;
  };
  attempts: AgentRunAttempt[];
  context_report: {
    coverage: string;
    truncated_sections: string[];
  };
  context_snapshot: AgentRun["context_snapshot"];
  write_policy: "preview_only";
}

export interface AgentRunAttempt {
  attempt_id: string;
  provider_alias: string;
  phase: string;
  state: string;
  usage: {
    input_tokens?: number;
    output_tokens?: number;
    total_tokens?: number;
  };
}

export interface AgentRun {
  run_id: string;
  novel_id: string;
  actor_id: string;
  capability:
    | "creative_inspiration"
    | "continuity_review"
    | "style_consistency"
    | "illustration_prompt"
    | "volume_retrospective";
  status: "running" | "completed" | "failed" | "stale";
  agent_id: string;
  agent_version: number;
  provider_alias?: string | null;
  structured_output?: string | null;
  request: {
    scope?: AgentScope;
    volume_id?: string | null;
    chapter_id?: string | null;
    character_card_id?: string | null;
    question?: string;
    focus?: string;
    target_model?: string;
  };
  context_snapshot: {
    novel_id: string;
    scope: AgentScope;
    volume_id?: string | null;
    chapter_id?: string | null;
    character_card_id?: string | null;
    narrative_revision: number;
    context_digest: string;
    story_health_schema_version?: string | null;
    chapter_scene_counts: Array<{
      chapter_id: string;
      scene_count: number;
    }>;
    fact_ids: string[];
    thread_ids: string[];
    style_evidence?: Array<
      Omit<StyleConsistencyEvidenceReference, "excerpt">
    >;
    volume_retrospective_evidence?: Array<
      Omit<VolumeRetrospectiveEvidenceReference, "excerpt">
    >;
  };
  result?:
    | CreativeInspirationResult
    | ContinuityReviewResult
    | StyleConsistencyResult
    | IllustrationPromptResult
    | VolumeRetrospectiveResult
    | null;
  usage: {
    input_tokens?: number;
    output_tokens?: number;
    total_tokens?: number;
  };
  attempts: AgentRunAttempt[];
  error?: {
    type: string;
    message: string;
  } | null;
  created_at: string;
  completed_at?: string | null;
  updated_at: string;
}

export type AgentRevisionTargetKind =
  | "volume_outline"
  | "chapter_outline"
  | "scene"
  | "chapter_prose";

export interface AgentRevisionTarget {
  kind: AgentRevisionTargetKind;
  volume_id?: string;
  chapter_id?: string;
  scene_index?: number;
}

export interface AgentRevisionPatch {
  summary?: string;
  arc?: string;
  core_conflict?: string;
  ending_hook?: string;
  scene_summary?: string;
  scene_purpose?: string;
  content?: string;
}

export interface AgentRevisionProposal {
  proposal_id: string;
  novel_id: string;
  actor_id: string;
  run_id: string;
  source_kind: "creative_idea" | "continuity_issue";
  source_index: number;
  source: CreativeIdea | ContinuityIssue;
  agent: {
    agent_id: string;
    agent_version: number;
    provider_alias?: string | null;
  };
  context_snapshot: AgentRun["context_snapshot"];
  target: AgentRevisionTarget;
  target_revision: string;
  patch: AgentRevisionPatch;
  status: "proposed" | "applying" | "applied" | "rejected" | "stale";
  version: number;
  acceptance?: {
    actor_id: string;
    accepted_at: string;
    narrative_revision_before: number;
    narrative_revision_after: number;
  } | null;
  rejection?: {
    actor_id: string;
    reason: string;
    rejected_at: string;
  } | null;
  stale_reason?: string | null;
  created_at: string;
  updated_at: string;
}
