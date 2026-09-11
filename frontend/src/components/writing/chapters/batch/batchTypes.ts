import type { ChapterSummary } from "@/types/novel";
import type { ProseRunSnapshot } from "../prose/useProseStream";
import type { ChapterReviewAuthorization, ReviewChapter } from "./chapterReviewPolicy";

import type {
  ProseContinuationAuthorization,
  ProseContinuationPolicy,
} from "../prose/proseContinuation";
import type {
  ReferenceCardAutoCreationPolicy,
  ReferenceCardType,
} from "./referenceCardAutoCreation";
// 镜像后端 generation_job_router._serialize_job 后的 JSON 形状（设计 §8）。
export type JobStatus =
  | "pending" | "running" | "paused"
  | "completed" | "aborted" | "failed" | "interrupted";

export type PauseReason =
  | "checkpoint" | "conflict" | "outline_deviation" | "cost_cap" | "manual"
  | "attempt_capacity" | "uncertain_attempt" | "uncertain_skipped" | "process_restart"
  | "source_changed" | "incomplete_scene" | "authorization_scope_increased"
  | "reference_card_review"
  | "reference_card_auto_creation_recovery"
  | "reference_card_repair_exhausted"
  | "final_audit"
  | "outline_adherence_manual_review" | "repair_not_converged"
  | "repair_budget_exhausted_provider_technical_retry"
  | "repair_budget_exhausted_adherence_judge_retry"
  | "repair_budget_exhausted_state_reextraction"
  | "repair_budget_exhausted_local_prose_repair"
  | "repair_budget_exhausted_scene_regeneration"
  | "repair_budget_exhausted_outline_rollback"
  | null;

export type BookCompletionIssueCategory =
  | "structure"
  | "prose"
  | "state"
  | "reference"
  | "thread"
  | "word_count"
  | "semantic"
  | "runtime";

export interface BookCompletionIssue {
  code: string;
  category: BookCompletionIssueCategory;
  level: "blocking" | "advisory";
  volume_id?: string | null;
  chapter_id?: string | null;
  job_id?: string | null;
  details: Record<string, unknown>;
}

export interface BookCompletionAudit {
  schema_version: "book_completion_audit.v1";
  novel_id: string;
  narrative_revision: number;
  status: "complete" | "incomplete";
  complete: boolean;
  read_only: true;
  blueprint: {
    source: "current_active_structure";
    current_structure_digest: string;
    frozen_job_id: string | null;
    frozen_worklist_digest: string | null;
    matches_frozen_worklist: boolean | null;
    world_baseline_state?:
      | "required"
      | "current"
      | "stale"
      | "blocked_pending_decisions"
      | "not_required_legacy"
      | "invalid";
    world_baseline_confirmed_at?: string | null;
  };
  summary: {
    volume_count: number;
    chapter_count: number;
    complete_chapter_count: number;
    current_state_count: number;
    blocking_reference_candidate_count: number;
    unresolved_thread_count: number;
    blocking_issue_count: number;
    advisory_issue_count: number;
    reviewed_chapter_count?: number | null;
    unreviewed_chapter_count?: number | null;
    advisory_review_chapter_count?: number | null;
  };
  chapters: Array<{
    chapter_id: string;
    volume_id: string;
    volume_order: number;
    chapter_order: number;
    outline_complete: boolean;
    prose_status: string;
    state_status: string;
    content_digest: string;
    actual_word_count: number;
    target_word_count: number | null;
    independent_review_status?: "passed" | "not_reviewed" | "advisory" | null;
  }>;
  issues: BookCompletionIssue[];
  excluded_optional_subsystems: Array<
    "illustrations" | "exports" | "optional_agent_reports"
  >;
  audit_digest: string;
}

export interface GenerationRunsNavigationTarget {
  jobId?: string;
  chapterId?: string;
  eventId?: string;
  runId?: string;
}

export interface AttemptSummary {
  attempt_id: string;
  usage: { input_tokens: number; output_tokens: number; total_tokens: number };
  accounted_at: string;
}

export interface ConsistencyIssue {
  card_id: string | null;
  fact: string;
  conflict: string;
}

export type OutlineDeviationPolicy = "pause_for_rewrite" | "accept_and_continue";

export interface JobGenerationParams {
  temperature?: number;
  top_p?: number;
  max_tokens?: number;
  presence_penalty?: number;
  frequency_penalty?: number;
  allow_failure_retry?: boolean;
  prose_continuation_policy?: ProseContinuationPolicy;
}

export interface OutlineAdherenceIssue {
  severity: "warning" | "error";
  category:
    | "scene_coverage"
    | "scene_order"
    | "core_conflict"
    | "ending_hook"
    | "unplanned_major_event"
    | "volume_arc";
  outline_requirement: string;
  prose_evidence: string;
  explanation: string;
}

export interface OutlineAdherenceReview {
  verdict: "pass" | "warn" | "fail";
  summary: string;
  scene_coverage: Array<{
    scene_index: number;
    status: "covered" | "partial" | "missing";
    evidence: string;
  }>;
  issues: OutlineAdherenceIssue[];
}

export interface Truncation {
  step: string; // "outline" | "prose" | "state"
  truncated_sections: string[];
  dropped_item_counts: Record<string, number>;
}

export type StepOutcomeStatus =
  | "generated"
  | "reused"
  | "skipped"
  | "degraded"
  | "incomplete"
  | "blocked"
  | "failed";

export interface StepOutcome {
  step: string;
  status: StepOutcomeStatus;
  reason_code: string | null;
}

export interface GenerationNotice {
  code: string;
  severity: "info" | "warning" | "error";
  category: "reuse" | "skip" | "context" | "reference" | "reference_remap" | "consistency" | "outline_adherence" | "provider" | "completion" | "recovery";
  step: string | null;
  details: Record<string, unknown>;
  impact: string;
  action_codes: string[];
  requires_pause: boolean;
}

export type ReadinessIssueLevel = "warning" | "warning_requires_ack" | "blocked";

export interface ReadinessIssue {
  code: string;
  level: ReadinessIssueLevel;
  details: Record<string, unknown>;
  action_codes: string[];
}

export interface ReadinessStepCounts {
  generate: number;
  reuse: number;
}

export interface ReadinessStructureCounts extends ReadinessStepCounts {
  target_chapter_count: number;
}

export interface BookStructureInitializationResult {
  schema_version: "book_structure_initialization_result.v1";
  volume_count: number;
  chapter_count: number;
  volume_ids: string[];
  next_route: {
    area: "world";
    view: "baseline";
  };
  usage: {
    attempt_count: number;
    total_tokens: number;
  };
}

export interface GenerationReadiness {
  version: number;
  novel_id: string;
  scope: "volume" | "book";
  volume_id: string | null;
  status: "ready" | "warning" | "warning_requires_ack" | "blocked";
  digest: string;
  issues: ReadinessIssue[];
  work: {
    chapter_count: number;
    steps: Record<"outline" | "prose" | "state", ReadinessStepCounts>;
    structure?: ReadinessStructureCounts;
    chapters?: ReviewChapter[];
  };
  resources: Record<"character" | "location" | "item" | "rule" | "lore", number> & {
    narrative_revision: number;
  };
  planning: {
    chapter_review_authorization?: ChapterReviewAuthorization;
    attempt_capacity: number;
    providers: string[];
    config_revision?: string;
    capability_snapshot?: string;
    book_structure_initialization?: {
      schema_version: "book_structure_initialization.v1";
      state: string;
      target_chapter_count: number;
    };
    prose_strategy?: {
      single_call_chapters: number;
      scene_segment_chapters: number;
      unknown_outline_chapters: number;
      maximum_prose_calls: number;
      provider_alias?: string;
      provider_model?: string;
      max_output_tokens?: number | null;
      safe_output_words?: number;
      output_limit_known?: boolean;
      high_risk_chapter_count?: number;
      high_risk_chapter_ids?: string[];
      maximum_target_words?: number;
      maximum_base_prose_calls?: number;
      maximum_automatic_continuation_calls?: number;
      maximum_logical_prose_calls?: number;
      max_actual_provider_attempts?: number;
      estimated_prose_chapter_count?: number;
      maximum_base_call_output_capacity_words?: number;
      continuation_call_target_words?: number;
      maximum_call_output_capacity_words?: number;
      base_output_token_bound?: number;
      continuation_output_token_bound?: number;
      conservative_base_token_bound?: number;
      conservative_continuation_token_bound?: number;
      conservative_token_bound?: number;
      conservative_total_token_bound?: number;
      token_bound_known?: boolean;
    };
    prose_continuation_authorization?: ProseContinuationAuthorization;
    batch_generation_budget_coverage?: {
      schema_version: "batch_generation_budget_coverage.v1";
      base_generation_maximum_tokens: number;
      candidate_repair_maximum_tokens: number;
      reference_card_repair_maximum_tokens: number;
      maximum_tokens_total: number;
      maximum_provider_attempts_total: number;
      provider_bounds: Array<{
        provider_alias: string;
        maximum_paid_attempts_total: number;
        maximum_tokens_total: number;
      }>;
      token_bound_known: boolean;
      token_budget: number | null;
      covers_full_job_authority: boolean;
    };
    reference_card_auto_creation_policy?: ReferenceCardAutoCreationPolicy;
    reference_card_creation_authorization?: {
      schema_version: "reference_card_creation_authorization.v1";
      mode: "auto_create_unique";
      policy_revision: 1;
      authorization_digest: string;
      authorization_revision: number;
      allowed_card_types: ReferenceCardType[];
      max_auto_creates_per_chapter: number;
      max_auto_creates_per_book: number;
      max_candidate_repair_cycles_per_chapter: number;
      maximum_repair_provider_attempts_total: number;
      maximum_repair_tokens_total: number;
    };
  };
}

export interface CheckpointProseCompletion {
  status: string;
  requested_word_count: number;
  actual_word_count: number;
}

export interface IncompleteProseProgress {
  chapter_id: string;
  source_run_id: string;
  source_run_revision: number;
  status: string;
  pause_reason: string;
  reason_codes: string[];
  scene_count: number;
  completed_scene_count: number;
  scene_progress: Array<Record<string, unknown>>;
}

export interface ChapterProgress {
  chapter_id: string;
  order_index: number;
  steps_done: string[];
  steps_skipped: string[];
  tokens: number;
  consistency_issues: ConsistencyIssue[];
  outline_adherence?: OutlineAdherenceReview;
  facts_added: number;
  threads_advanced: number;
  summary_written: boolean;
  dropped_ids: Record<string, unknown>;
  truncations: Truncation[];
  /** 正文完成契约的只读标量摘要；旧作业可能没有。 */
  prose_completion?: CheckpointProseCompletion;
  /** 未完成正文的恢复快照；同章重试会追加新的 progress 条目。 */
  incomplete_prose?: IncompleteProseProgress;
  /** 新作业写入；旧作业缺失时由 batchPresentation 从 legacy 字段投影。 */
  step_outcomes?: StepOutcome[];
  notices?: GenerationNotice[];
  completed_at: string;
}

/**
 * 新版候选流水线原子收口后写入的元数据摘要。
 * 它证明候选链路已收口，但不包含旧检查点卡片需要的逐项冲突与步骤明细。
 */
export interface CandidatePipelineProgress {
  schema_version: "candidate_pipeline_progress.v1";
  status: "completed";
  finalization_status: "committed";
  chapter_id: string;
  order_index: number;
  tokens: number;
  source: {
    source_run_id: string;
    source_run_revision: number;
    source_content_digest: string;
  };
  state_proposal_id: string;
  repair_cycles_used: number;
  attempt_count: number;
  truncation_count: number;
  outline_issue_categories: string[];
  scene_coverage_count: number;
  consistency_issue_count: number;
  candidate_pipeline_completion?: Record<string, unknown>;
  completed_at: string;
}

export type GenerationJobProgress = ChapterProgress | CandidatePipelineProgress;

export function isChapterProgress(
  entry: GenerationJobProgress,
): entry is ChapterProgress {
  const candidate = entry as Partial<ChapterProgress>;
  return Array.isArray(candidate.steps_done)
    && Array.isArray(candidate.steps_skipped)
    && Array.isArray(candidate.consistency_issues)
    && Array.isArray(candidate.truncations);
}

export function chapterProgressEntries(
  progress: readonly GenerationJobProgress[],
): ChapterProgress[] {
  return progress.filter(isChapterProgress);
}

export interface JobError {
  step: string;
  message?: string;
  reason_codes?: string[];
  chapter_id?: string;
  candidate_ids?: string[];
  candidate_names?: string[];
  auto_creation?: {
    outcome: "manual_review_required" | "auto_created" | "not_applicable" | "repair_exhausted";
    pause_reason?: PauseReason;
    created_count: number;
    deny_reasons: string[];
    denials: Array<{
      candidate_id?: string;
      reason: string;
      evidence?: Record<string, unknown>;
    }>;
  };
}

export interface ReferenceCardAutoCreationEvent {
  schema_version: "reference_card_auto_creation_event.v1";
  event_id: string;
  chapter_id: string;
  actor_owner_id: string;
  authorization_digest: string;
  readiness_digest?: string;
  authorization_revision?: number;
  policy_revision?: number;
  outcome:
    | "manual_review_required"
    | "auto_created"
    | "not_applicable";
  created_count: number;
  mappings: Array<{
    candidate_id?: string;
    card_id?: string;
    card_type?: ReferenceCardType;
    [key: string]: unknown;
  }>;
  deny_reasons: string[];
  denials: Array<Record<string, unknown>>;
  limit_usage: Record<string, unknown>;
  source_mutation_id?: string;
  mutation_receipt_id?: string;
  occurred_at: string;
}

export interface ReferenceCardRepairEvent {
  schema_version: "reference_card_repair_event.v1";
  event_id: string;
  chapter_id: string;
  actor_owner_id: string;
  authorization_digest: string;
  readiness_digest?: string;
  authorization_revision?: number;
  policy_revision?: number;
  cycle: number;
  outcome: "applied" | "exhausted" | "uncertain";
  resolution?: "rewritten_unique_new" | "dependency_removed" | null;
  created_reference_card_candidate_ids?: string[];
  reason: string;
  proposal_digest: string;
  source_mutation_id: string;
  occurred_at: string;
}

export type DiagnosticCategory =
  | "model_output_incomplete"
  | "provider_or_transport"
  | "validation_logic"
  | "source_changed"
  | "context_or_budget"
  | "user_action"
  | "unknown_system";

export type DiagnosticEvidence =
  | "confirmed"
  | "strong_inference"
  | "insufficient";

export interface GenerationDiagnostic {
  schema_version: number;
  event_id?: string;
  fingerprint?: string;
  category: DiagnosticCategory;
  code: string;
  evidence: DiagnosticEvidence;
  impact?: string;
  action_codes?: string[];
  source: "runtime" | "historical_inference";
  step: string;
  chapter_id: string;
  occurred_at?: string;
  details: {
    status?: string;
    requested_word_count?: number;
    actual_word_count?: number;
    raw_character_count?: number;
    scene_count?: number;
    completed_scene_count?: number;
    finish_reason?: string;
    raw_finish_reason?: string;
    completion_reason?: string;
    mode?: string;
    reason_codes?: string[];
    attempt_count?: number;
    provider_aliases?: string[];
    provider_models?: string[];
    candidate_gate?: "completion" | "outline_adherence" | "state";
    repair_cycles_used?: number;
    repair_cycles_limit?: number;
    consistency_issue_count?: number;
    dropped_reference_count?: number;
    affected_card_ids?: string[];
    outline_issue_categories?: string[];
    prose_run_id?: string;
    prose_run_revision?: number;
    [key: string]: unknown;
  };
}

export interface DiagnosticCategorySummary {
  category: DiagnosticCategory;
  event_count: number;
  job_count: number;
  evidence_counts: Record<DiagnosticEvidence, number>;
}

export interface GenerationDiagnosticsSummary {
  schema_version: number;
  window_job_count: number;
  affected_job_count: number;
  event_count: number;
  inferred_event_count: number;
  insufficient_event_count?: number;
  unresolved_event_count?: number;
  categories: DiagnosticCategorySummary[];
  recent_events: Array<GenerationDiagnostic & { job_id: string }>;
}

export interface GenerationStageEvent {
  id: string;
  kind: "request" | "chapter_complete" | "job_status";
  stage: "outline" | "prose" | "state" | "review" | "other" | "completion" | "job";
  status: "running" | "settled" | "uncertain" | "completed" | "paused" | "failed" | "interrupted" | "aborted";
  phase?: "primary" | "repair" | "text" | "other" | null;
  chapter_id?: string | null;
  order_index?: number | null;
  started_at?: string | null;
  finished_at?: string | null;
  tokens?: number | null;
  retry_index?: number | null;
}

export interface GenerationJob {
  _id: string;
  novel_id: string;
  root_job_id?: string;
  parent_job_id?: string | null;
  required_book_successor_parent_job_id?: string | null;
  job_kind?: string | null;
  current_stage?: string | null;
  detail_version?: string;
  progress_count?: number;
  progress_chapter_count?: number;
  completed_chapter_count?: number;
  scope: "volume" | "book" | "interactive_completion";
  volume_id: string | null;
  status: JobStatus;
  pause_reason: PauseReason;
  pause_reason_detail?: "authorization_scope_increased" | "world_baseline_confirmation_required" | "readiness_confirmation_required" | null;
  checkpoint_interval: number | null;
  outline_deviation_policy?: OutlineDeviationPolicy;
  generation_params?: JobGenerationParams;
  token_budget: number | null;
  tokens_used: number;
  tokens_reserved?: number;
  current_chapter_id: string | null;
  progress: GenerationJobProgress[];
  stage_history?: GenerationStageEvent[];
  stage_history_total?: number;
  last_checkpoint_index: number;
  error: JobError | null;
  diagnostics?: GenerationDiagnostic[];
  related_prose_run_ids?: string[];
  reference_card_auto_creation_events?: ReferenceCardAutoCreationEvent[];
  reference_card_repair_events?: ReferenceCardRepairEvent[];
  completion_audit?: BookCompletionAudit | null;
  prose_continuation_authorization?: ProseContinuationAuthorization;
  readiness?: GenerationReadiness;
  usage_attempt_capacity: number;
  usage_attempt_claimed: number;
  usage_attempt_summaries: AttemptSummary[];
  has_uncertain_attempts: boolean;
  resume_original_writeback_available?: boolean;
  created_at: string;
  updated_at: string;
}

/** Bounded read view. It never contains chapter candidates or execution inputs. */
export interface GenerationJobSummary extends Pick<GenerationJob,
  "_id" | "novel_id" | "root_job_id" | "parent_job_id" | "job_kind"
  | "scope" | "volume_id" | "status" | "pause_reason" | "pause_reason_detail" | "token_budget"
  | "tokens_used" | "tokens_reserved" | "current_chapter_id"
  | "usage_attempt_capacity" | "usage_attempt_claimed" | "has_uncertain_attempts"
  | "current_stage" | "created_at" | "updated_at" | "related_prose_run_ids"
> {
  detail_version: string;
  progress_count: number;
  progress_chapter_count: number;
  diagnostics_count: number;
  latest_diagnostic: GenerationDiagnostic | null;
  provider_aliases: string[];
  provider_models: string[];
  reason_codes: string[];
}

export interface GenerationJobPage {
  items: GenerationJobSummary[];
  next_cursor: string | null;
}

export interface LeftoverProseRun extends ProseRunSnapshot {
  run_id: string;
  novel_id: string;
  chapter_id: string;
  status: "incomplete" | "superseded" | "stale";
  draft_word_count: number;
  reason_codes: string[];
  continuation_exhausted: boolean;
  has_uncertain_attempt: boolean;
  can_resume: boolean;
  can_accept_partial: boolean;
  can_discard: boolean;
  created_at: string;
  updated_at: string;
}

/** 终态：不再推进、不收养（设计 §7 状态机）。 */
export function isTerminal(status: JobStatus): boolean {
  return status === "completed" || status === "aborted";
}

/** 可恢复：等人点继续/中止（设计 §7）。 */
export function isResumable(status: JobStatus): boolean {
  return status === "paused" || status === "interrupted" || status === "failed";
}

/** 活跃：需要轮询（设计 §5.2）。 */
export function isActive(status: JobStatus): boolean {
  return status === "running" || status === "pending";
}

/** 本检查点窗口 = last_checkpoint_index 之后的 progress（设计 §7.2）。 */
export function checkpointWindow(job: GenerationJob): ChapterProgress[] {
  return chapterProgressEntries(job.progress.slice(job.last_checkpoint_index));
}

/** 作业覆盖的章集合：整本=全书章；整卷=按 volume_id 过滤（设计 §5.1）。进度分母统一走它。 */
export function jobChapters(job: GenerationJob, chapters: ChapterSummary[]): ChapterSummary[] {
  if (job.scope === "interactive_completion") return chapters.filter((c) => c._id === job.current_chapter_id);
  return job.scope === "book" ? chapters : chapters.filter((c) => c.volume_id === job.volume_id);
}
