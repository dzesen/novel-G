export interface ProseCompletionSummary {
  status: "complete" | "degraded" | "incomplete" | "stale";
  can_write_formal_prose: boolean;
}

export function proseRequiresPartialAcknowledgement(
  completion: Pick<ProseCompletionSummary, "status" | "can_write_formal_prose">,
): boolean {
  return !completion.can_write_formal_prose || completion.status === "incomplete";
}

export function proseRunHasUncertainAttempt(
  run: { segments?: Array<{ status?: string }> },
): boolean {
  return (run.segments ?? []).some((segment) => segment.status === "uncertain");
}

const PROSE_REASON_KEYS = {
  outline_revision_stale: "outlineRevisionStale",
  finish_reason_length: "finishReasonLength",
  finish_reason_content_filter: "finishReasonContentFilter",
  finish_reason_tool_call: "finishReasonToolCall",
  finish_reason_cancelled: "finishReasonCancelled",
  finish_reason_error: "finishReasonError",
  finish_reason_unreported: "finishReasonUnreported",
  scenes_incomplete: "scenesIncomplete",
  below_minimum_word_ratio: "belowMinimumWordRatio",
  continuation_limit_reached: "continuationLimitReached",
  uncertain_provider_attempt: "uncertainProviderAttempt",
  completion_contract_failed: "completionContractFailed",
} as const;

export type ProseReasonTranslationKey =
  (typeof PROSE_REASON_KEYS)[keyof typeof PROSE_REASON_KEYS];

export function proseReasonTranslationKey(
  reasonCode: string,
): ProseReasonTranslationKey | null {
  return PROSE_REASON_KEYS[
    reasonCode as keyof typeof PROSE_REASON_KEYS
  ] ?? null;
}

interface AcceptPayloadInput {
  novelId: string;
  chapterId: string;
  runId: string;
  runRevision: number;
  partial: boolean;
}

export function buildProseAcceptPayload({
  novelId,
  chapterId,
  runId,
  runRevision,
  partial,
}: AcceptPayloadInput) {
  if (!runId.trim()) throw new Error("runId is required");
  return {
    novel_id: novelId,
    chapter_id: chapterId,
    expected_run_revision: runRevision,
    accept_partial: partial,
    partial_acknowledgement: partial,
  };
}
