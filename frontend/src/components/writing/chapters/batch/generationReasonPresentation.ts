const DIAGNOSTIC_REASON_KEYS = {
  short_stop: "diagnosticsReasonShortStop",
  provider_length_limit: "diagnosticsReasonLength",
  provider_content_filter: "diagnosticsReasonContentFilter",
  provider_tool_call: "diagnosticsReasonToolCall",
  cancelled: "diagnosticsReasonCancelled",
  provider_or_transport_error: "diagnosticsReasonProviderFailure",
  provider_or_transport_failure: "diagnosticsReasonProviderFailure",
  provider_attempt_uncertain: "diagnosticsReasonUncertain",
  historical_provider_attempt_uncertain: "diagnosticsReasonUncertain",
  chapter_or_narrative_changed: "diagnosticsReasonSourceChanged",
  chapter_deleted_during_generation: "diagnosticsReasonChapterDeleted",
  context_budget_exceeded: "diagnosticsReasonContextBudget",
  attempt_capacity_exhausted: "diagnosticsReasonAttemptCapacity",
  continuation_limit_reached: "diagnosticsReasonContinuationLimit",
  invalid_internal_id: "diagnosticsReasonInvalidId",
  historical_invalid_internal_id: "diagnosticsReasonInvalidId",
  validation_rejected: "diagnosticsReasonValidation",
  historical_completion_contract_failed: "diagnosticsReasonCompletion",
  completion_contract_failed: "diagnosticsReasonCompletion",
  job_aborted: "diagnosticsReasonAborted",
} as const;

const JOB_PAUSE_REASON_KEYS = {
  checkpoint: "reasonCheckpoint",
  conflict: "reasonConflict",
  outline_deviation: "reasonOutlineDeviation",
  cost_cap: "reasonCostCap",
  attempt_capacity: "reasonAttemptCapacity",
  manual: "reasonManual",
  process_restart: "reasonInterrupted",
  uncertain_attempt: "reasonInterrupted",
  source_changed: "reasonSourceChanged",
  incomplete_scene: "reasonIncompleteScene",
  authorization_scope_increased: "reasonAuthorizationScopeIncreased",
  reference_card_review: "reasonReferenceCardReview",
  reference_card_auto_creation_recovery: "reasonReferenceCardAutoCreationRecovery",
  reference_card_repair_exhausted: "reasonReferenceCardRepairExhausted",
} as const;

export type DiagnosticReasonTranslationKey =
  (typeof DIAGNOSTIC_REASON_KEYS)[keyof typeof DIAGNOSTIC_REASON_KEYS];
export type JobPauseReasonTranslationKey =
  (typeof JOB_PAUSE_REASON_KEYS)[keyof typeof JOB_PAUSE_REASON_KEYS];

export function diagnosticReasonTranslationKey(
  reasonCode: string,
): DiagnosticReasonTranslationKey | null {
  return DIAGNOSTIC_REASON_KEYS[
    reasonCode as keyof typeof DIAGNOSTIC_REASON_KEYS
  ] ?? null;
}

export function jobPauseReasonTranslationKey(
  reasonCode: string,
): JobPauseReasonTranslationKey | null {
  return JOB_PAUSE_REASON_KEYS[
    reasonCode as keyof typeof JOB_PAUSE_REASON_KEYS
  ] ?? null;
}
