// 镜像后端 generation_job_router._serialize_job 后的 JSON 形状（设计 §8）。
export type JobStatus =
  | "pending" | "running" | "paused"
  | "completed" | "aborted" | "failed" | "interrupted";

export type PauseReason = "checkpoint" | "conflict" | "cost_cap" | "manual" | null;

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
  scope: "volume";
  volume_id: string;
  status: JobStatus;
  pause_reason: PauseReason;
  checkpoint_interval: number;
  token_budget: number | null;
  tokens_used: number;
  current_chapter_id: string | null;
  progress: ChapterProgress[];
  last_checkpoint_index: number;
  error: JobError | null;
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
