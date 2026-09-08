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
  prose_no_progress: "proseNoProgress",
  prose_no_progress_without_quota: "proseNoProgressWithoutQuota",
  prose_scene_divergence_stopped: "proseSceneDivergenceStopped",
  scene_word_budget_exceeded: "sceneWordBudgetExceeded",
  scene_word_budget_below_minimum: "sceneWordBudgetBelowMinimum",
  uncertain_provider_attempt: "uncertainProviderAttempt",
  completion_contract_failed: "completionContractFailed",
} as const;

const PROSE_ADVISORY_KEYS = {
  below_minimum_word_ratio: "belowMinimumWordRatio",
  scene_below_minimum_word_budget: "sceneBelowMinimumWordBudget",
  scene_above_maximum_word_budget: "sceneAboveMaximumWordBudget",
} as const;

const FINISH_REASON_KEYS = {
  stop: "finishReasonStop",
  end_turn: "finishReasonStop",
  stop_sequence: "finishReasonStop",
  complete: "finishReasonStop",
  completed: "finishReasonStop",
  length: "finishReasonLength",
  max_tokens: "finishReasonLength",
  max_token: "finishReasonLength",
  max_output_tokens: "finishReasonLength",
  token_limit: "finishReasonLength",
  content_filter: "finishReasonContentFilter",
  safety: "finishReasonContentFilter",
  recitation: "finishReasonContentFilter",
  prohibited_content: "finishReasonContentFilter",
  blocklist: "finishReasonContentFilter",
  spii: "finishReasonContentFilter",
  tool_call: "finishReasonToolCall",
  tool_calls: "finishReasonToolCall",
  tool_use: "finishReasonToolCall",
  function_call: "finishReasonToolCall",
  cancelled: "finishReasonCancelled",
  canceled: "finishReasonCancelled",
  abort: "finishReasonCancelled",
  aborted: "finishReasonCancelled",
  error: "finishReasonError",
  failed: "finishReasonError",
  failure: "finishReasonError",
  budget: "finishReasonBudget",
  unreported: "finishReasonUnreported",
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

export type ProseAdvisoryTranslationKey =
  (typeof PROSE_ADVISORY_KEYS)[keyof typeof PROSE_ADVISORY_KEYS];

export function proseAdvisoryTranslationKey(
  advisoryCode: string,
): ProseAdvisoryTranslationKey | null {
  return PROSE_ADVISORY_KEYS[
    advisoryCode as keyof typeof PROSE_ADVISORY_KEYS
  ] ?? null;
}

export type FinishReasonTranslationKey =
  | (typeof FINISH_REASON_KEYS)[keyof typeof FINISH_REASON_KEYS]
  | "finishReasonUnknown";

/**
 * Provider finish values are useful telemetry, but raw values are not useful
 * instructions for a writer. Keep the display vocabulary bounded and friendly.
 */
export function finishReasonTranslationKey(
  reasonCode: string,
): FinishReasonTranslationKey {
  const normalizedReason = reasonCode
    .trim()
    .toLowerCase()
    .replace(/[-\s]+/g, "_");
  return FINISH_REASON_KEYS[
    normalizedReason as keyof typeof FINISH_REASON_KEYS
  ] ?? "finishReasonUnknown";
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
  if (!partial) {
    throw new Error(
      "A complete AI draft requires the chapter completion certificate endpoint",
    );
  }
  return {
    novel_id: novelId,
    chapter_id: chapterId,
    expected_run_revision: runRevision,
    accept_partial: partial,
    partial_acknowledgement: partial,
  };
}

interface InteractiveCompletionReadinessPayloadInput {
  novelId: string;
  chapterId: string;
  runRevision: number;
  reviewRequested?: boolean;
  reviewEnforcement?: "advisory" | "strict";
}

export function buildInteractiveCompletionReadinessPayload({
  novelId,
  chapterId,
  runRevision,
  reviewRequested,
  reviewEnforcement,
}: InteractiveCompletionReadinessPayloadInput) {
  return {
    novel_id: novelId,
    chapter_id: chapterId,
    expected_run_revision: runRevision,
    ...(reviewRequested !== undefined ? { review_requested: reviewRequested } : {}),
    ...(reviewEnforcement !== undefined ? { review_enforcement: reviewEnforcement } : {}),
  };
}

interface InteractiveCompletionPayloadInput
  extends InteractiveCompletionReadinessPayloadInput {
  authorizationId: string;
  authorizationRevision: number;
  readinessDigest: string;
}

export type InteractiveCompletionResolutionAction = "retry" | "abort";

export function buildInteractiveCompletionPayload({
  novelId,
  chapterId,
  runRevision,
  authorizationId,
  authorizationRevision,
  readinessDigest,
  reviewRequested,
  reviewEnforcement,
}: InteractiveCompletionPayloadInput) {
  if (!/^[0-9a-f]{24}$/.test(authorizationId)) {
    throw new Error("completion authorization id is invalid");
  }
  if (!/^[0-9a-f]{64}$/.test(readinessDigest)) {
    throw new Error("completion readiness digest is invalid");
  }
  return {
    novel_id: novelId,
    chapter_id: chapterId,
    expected_run_revision: runRevision,
    authorization_id: authorizationId,
    authorization_revision: authorizationRevision,
    completion_readiness_digest: readinessDigest,
    completion_readiness_confirmed: true,
    ...(reviewRequested !== undefined ? { review_requested: reviewRequested } : {}),
    ...(reviewEnforcement !== undefined ? { review_enforcement: reviewEnforcement } : {}),
  };
}

export function buildInteractiveCompletionStatusPayload(
  input: InteractiveCompletionPayloadInput,
) {
  const payload = buildInteractiveCompletionPayload(input);
  return {
    novel_id: payload.novel_id,
    chapter_id: payload.chapter_id,
    expected_run_revision: payload.expected_run_revision,
    authorization_id: payload.authorization_id,
    authorization_revision: payload.authorization_revision,
    completion_readiness_digest: payload.completion_readiness_digest,
  };
}

export function buildInteractiveCompletionResolutionPayload({
  novelId,
  chapterId,
  runRevision,
  authorizationId,
  authorizationRevision,
  readinessDigest,
  action,
}: InteractiveCompletionPayloadInput & {
  action: InteractiveCompletionResolutionAction;
}) {
  return {
    novel_id: novelId,
    chapter_id: chapterId,
    expected_run_revision: runRevision,
    authorization_id: authorizationId,
    authorization_revision: authorizationRevision,
    completion_readiness_digest: readinessDigest,
    action,
  };
}

export function interactiveCompletionErrorCode(error: unknown): string | null {
  if (!error || typeof error !== "object") return null;
  const detail = (error as { detail?: unknown }).detail;
  if (!detail || typeof detail !== "object") return null;
  const code = (detail as { code?: unknown }).code;
  return typeof code === "string" && code ? code : null;
}
