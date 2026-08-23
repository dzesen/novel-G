import type {
  ChapterProgress,
  GenerationDiagnostic,
  GenerationJob,
  JobStatus,
} from "./batchTypes";

export const JOB_CHAPTER_STEPS = ["outline", "prose", "state"] as const;

export interface ChapterProgressPresentation extends ChapterProgress {
  attemptCount: number;
  completedStepCount: number;
  stepPercent: number;
}

export interface OrderedDiagnostic {
  event: GenerationDiagnostic;
  originalIndex: number;
}

export type DiagnosticHistoryState =
  | "current_blocker"
  | "history_resumed"
  | "history_completed"
  | "history_ended"
  | "history_recorded";

function timestamp(value: string | undefined): number {
  if (!value) return Number.NEGATIVE_INFINITY;
  const parsed = new Date(value).getTime();
  return Number.isNaN(parsed) ? Number.NEGATIVE_INFINITY : parsed;
}

function compareNewestFirst(
  leftValue: string | undefined,
  rightValue: string | undefined,
): number {
  const left = timestamp(leftValue);
  const right = timestamp(rightValue);
  if (left === right) return 0;
  return right > left ? 1 : -1;
}

function unique(values: string[]): string[] {
  return Array.from(new Set(values));
}

/**
 * Generation jobs append one progress snapshot for every chapter attempt.
 * Present those immutable snapshots as one cumulative chapter row while
 * preserving the latest snapshot for non-additive metadata.
 */
export function aggregateChapterProgress(
  progress: ChapterProgress[],
): ChapterProgressPresentation[] {
  const grouped = new Map<string, Array<{ entry: ChapterProgress; index: number }>>();
  progress.forEach((entry, index) => {
    const group = grouped.get(entry.chapter_id) ?? [];
    group.push({ entry, index });
    grouped.set(entry.chapter_id, group);
  });

  return Array.from(grouped.values())
    .map((entries) => {
      const ordered = [...entries].sort((left, right) => (
        compareNewestFirst(left.entry.completed_at, right.entry.completed_at)
        || right.index - left.index
      ));
      const latest = ordered[0].entry;
      const stepsDone = unique(entries.flatMap(({ entry }) => entry.steps_done));
      const stepsSkipped = unique(entries.flatMap(({ entry }) => entry.steps_skipped))
        .filter((step) => !stepsDone.includes(step));
      const completedStepCount = JOB_CHAPTER_STEPS.filter(
        (step) => stepsDone.includes(step) || stepsSkipped.includes(step),
      ).length;

      return {
        ...latest,
        steps_done: stepsDone,
        steps_skipped: stepsSkipped,
        tokens: entries.reduce((total, { entry }) => total + Math.max(0, entry.tokens), 0),
        attemptCount: entries.length,
        completedStepCount,
        stepPercent: Math.round(
          (completedStepCount / JOB_CHAPTER_STEPS.length) * 100,
        ),
      };
    })
    .sort((left, right) => left.order_index - right.order_index);
}

export function newestDiagnostics(
  diagnostics: GenerationDiagnostic[] | undefined,
): OrderedDiagnostic[] {
  return (diagnostics ?? [])
    .map((event, originalIndex) => ({ event, originalIndex }))
    .sort((left, right) => (
      compareNewestFirst(left.event.occurred_at, right.event.occurred_at)
      || right.originalIndex - left.originalIndex
    ));
}

export function currentJobReasonCode(job: GenerationJob): string | null {
  if (job.pause_reason) return job.pause_reason;
  if (job.status !== "failed" && job.status !== "interrupted") return null;
  return newestDiagnostics(job.diagnostics)[0]?.event.code ?? null;
}

export function requiresSuccessorJob(job: GenerationJob): boolean {
  return !job.resume_original_writeback_available
    && Boolean(job.error?.reason_codes?.includes("successor_required"));
}

export function requiresResumeReadinessReview(job: GenerationJob): boolean {
  return job.pause_reason === "cost_cap"
    || job.pause_reason === "authorization_scope_increased"
    || (
      job.pause_reason === "source_changed"
      && !job.resume_original_writeback_available
    );
}

export function diagnosticHistoryState(
  job: GenerationJob,
  orderedIndex: number,
): DiagnosticHistoryState {
  if (job.status === "running" || job.status === "pending") {
    return "history_resumed";
  }
  if (job.status === "completed") return "history_completed";
  if (job.status === "aborted") return "history_ended";
  const pausedForDiagnosedBlocker = job.status === "paused" && [
    "conflict",
    "outline_deviation",
    "cost_cap",
    "attempt_capacity",
    "uncertain_attempt",
    "source_changed",
    "incomplete_scene",
  ].includes(job.pause_reason ?? "");
  if (
    orderedIndex === 0
    && (
      job.status === "failed"
      || job.status === "interrupted"
      || pausedForDiagnosedBlocker
    )
  ) {
    return "current_blocker";
  }
  return "history_recorded";
}

const TERMINAL_STATUSES = new Set<JobStatus>(["completed", "aborted"]);

/** Exact prose runs referenced by a non-terminal job, used to distinguish
 * current task drafts from genuinely historical leftovers. */
export function currentJobStatusByProseRun(
  jobs: GenerationJob[],
): Record<string, JobStatus> {
  const result: Record<string, JobStatus> = {};
  const ordered = [...jobs].sort((left, right) => (
    compareNewestFirst(left.updated_at, right.updated_at)
  ));
  for (const job of ordered) {
    if (TERMINAL_STATUSES.has(job.status)) continue;
    const proseRunIds = unique(job.progress
      .map((entry) => entry.incomplete_prose?.source_run_id ?? "")
      .filter(Boolean));
    for (const proseRunId of proseRunIds) {
      if (!result[proseRunId]) {
        result[proseRunId] = job.status;
      }
    }
  }
  return result;
}
