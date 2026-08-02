import type { CheckpointProseCompletion } from "./batchTypes";

/**
 * Display-only threshold. It never participates in formal-prose acceptance,
 * continuation, pause, or job-control decisions.
 */
export const CHECKPOINT_WORD_COUNT_DISPLAY_OVERRUN_RATIO = 1.5;

export interface CheckpointWordCountPresentation {
  actualWordCount: number;
  targetWordCount: number;
  ratio: number;
  hasVisibleOverrun: boolean;
}

function nonNegativeInteger(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= 0
    ? value
    : null;
}

export function checkpointWordCountPresentation(
  completion: CheckpointProseCompletion | undefined,
): CheckpointWordCountPresentation | null {
  if (!completion) return null;
  const actualWordCount = nonNegativeInteger(completion.actual_word_count);
  const targetWordCount = nonNegativeInteger(completion.requested_word_count);
  if (actualWordCount === null || targetWordCount === null || targetWordCount <= 0) {
    return null;
  }
  const ratio = actualWordCount / targetWordCount;
  return {
    actualWordCount,
    targetWordCount,
    ratio,
    hasVisibleOverrun: ratio >= CHECKPOINT_WORD_COUNT_DISPLAY_OVERRUN_RATIO,
  };
}