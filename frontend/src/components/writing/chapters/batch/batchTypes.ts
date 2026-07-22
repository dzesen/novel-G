import type { ChapterSummary } from "@/types/novel";

// 镜像后端 generation_job_router._serialize_job 后的 JSON 形状（设计 §8）。
export type JobStatus =
  | "pending" | "running" | "paused"
  | "completed" | "aborted" | "failed" | "interrupted";

export type PauseReason =
  | "checkpoint" | "conflict" | "cost_cap" | "manual"
  | "attempt_capacity" | "uncertain_attempt" | "uncertain_skipped" | "process_restart"
  | null;

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

export interface Truncation {
  step: string; // "outline" | "prose" | "state"
  truncated_sections: string[];
  dropped_item_counts: Record<string, number>;
}

export interface ChapterProgress {
  chapter_id: string;
  order_index: number;
  steps_done: string[];
  steps_skipped: string[];
  tokens: number;
  consistency_issues: ConsistencyIssue[];
  facts_added: number;
  threads_advanced: number;
  summary_written: boolean;
  dropped_ids: Record<string, unknown>;
  truncations: Truncation[];
  completed_at: string;
}

export interface JobError {
  step: string;
  chapter_id: string;
  message: string;
}

export interface GenerationJob {
  _id: string;
  novel_id: string;
  scope: "volume" | "book";
  volume_id: string | null;
  status: JobStatus;
  pause_reason: PauseReason;
  checkpoint_interval: number;
  token_budget: number | null;
  tokens_used: number;
  current_chapter_id: string | null;
  progress: ChapterProgress[];
  last_checkpoint_index: number;
  error: JobError | null;
  usage_attempt_capacity: number;
  usage_attempt_claimed: number;
  usage_attempt_summaries: AttemptSummary[];
  has_uncertain_attempts: boolean;
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
  return job.progress.slice(job.last_checkpoint_index);
}

/** 作业覆盖的章集合：整本=全书章；整卷=按 volume_id 过滤（设计 §5.1）。进度分母统一走它。 */
export function jobChapters(job: GenerationJob, chapters: ChapterSummary[]): ChapterSummary[] {
  return job.scope === "book" ? chapters : chapters.filter((c) => c.volume_id === job.volume_id);
}
