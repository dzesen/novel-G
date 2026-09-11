const DIAGNOSTIC_REASON_KEYS = {
  repair_budget_exhausted: "diagnosticsReasonRepairBudgetExhausted",
  repair_not_converged: "diagnosticsReasonRepairNotConverged",
  review_evidence_invalid: "diagnosticsReasonReviewEvidenceInvalid",
  unclassified_failure: "diagnosticsReasonUnknown",
  historical_unclassified_failure: "diagnosticsReasonUnknown",
  short_stop: "diagnosticsReasonShortStop",
  provider_length_limit: "diagnosticsReasonLength",
  provider_content_filter: "diagnosticsReasonContentFilter",
  provider_tool_call: "diagnosticsReasonToolCall",
  cancelled: "diagnosticsReasonCancelled",
  provider_or_transport_error: "diagnosticsReasonProviderFailure",
  provider_or_transport_failure: "diagnosticsReasonProviderFailure",
  provider_authentication_failed: "diagnosticsReasonProviderAuth",
  provider_rate_limited: "diagnosticsReasonProviderRateLimit",
  provider_timeout: "diagnosticsReasonProviderTimeout",
  provider_connection_failed: "diagnosticsReasonProviderConnection",
  provider_http_status_error: "diagnosticsReasonProviderHttpStatus",
  provider_response_invalid: "diagnosticsReasonProviderResponse",
  provider_schema_unsupported: "diagnosticsReasonProviderSchema",
  structured_output_invalid: "diagnosticsReasonStructuredOutput",
  structured_output_truncated: "diagnosticsReasonStructuredTruncated",
  candidate_result_projection_missing: "diagnosticsReasonResultMissing",
  candidate_narrative_revision_changed: "diagnosticsReasonSourceChanged",
  provider_attempt_uncertain: "diagnosticsReasonUncertain",
  historical_provider_attempt_uncertain: "diagnosticsReasonUncertain",
  chapter_or_narrative_changed: "diagnosticsReasonSourceChanged",
  outline_revision_stale: "diagnosticsReasonOutlineRevisionStale",
  chapter_deleted_during_generation: "diagnosticsReasonChapterDeleted",
  context_budget_exceeded: "diagnosticsReasonContextBudget",
  attempt_capacity_exhausted: "diagnosticsReasonAttemptCapacity",
  token_budget_exceeded_before_dispatch: "diagnosticsReasonTokenBudget",
  continuation_limit_reached: "diagnosticsReasonContinuationLimit",
  invalid_internal_id: "diagnosticsReasonInvalidId",
  historical_invalid_internal_id: "diagnosticsReasonInvalidId",
  world_baseline_confirmation_required: "diagnosticsReasonWorldBaseline",
  readiness_confirmation_required: "diagnosticsReasonReadinessConfirmation",
  authorization_scope_increased: "diagnosticsReasonAuthorizationScope",
  validation_rejected: "diagnosticsReasonValidation",
  candidate_completion_repair_exhausted: "diagnosticsReasonCandidateCompletionExhausted",
  candidate_adherence_repair_exhausted: "diagnosticsReasonCandidateAdherenceExhausted",
  candidate_state_repair_exhausted: "diagnosticsReasonCandidateStateExhausted",
  candidate_finalization_writeback_failed: "diagnosticsReasonFinalizationWriteback",
  historical_completion_contract_failed: "diagnosticsReasonCompletion",
  completion_contract_failed: "diagnosticsReasonCompletion",
  job_aborted: "diagnosticsReasonAborted",
} as const;

const DIAGNOSTIC_IMPACT_KEYS = {
  formal_prose_not_written: "diagnosticsImpactProseNotWritten",
  generation_step_not_committed: "diagnosticsImpactNotCommitted",
  generated_result_rejected: "diagnosticsImpactRejected",
  authorization_snapshot_stale: "diagnosticsImpactAuthorizationStale",
  generation_paused_before_commit: "diagnosticsImpactPaused",
  job_stopped_by_user: "diagnosticsImpactUserStopped",
  cause_not_identified: "diagnosticsImpactUnknown",
  candidate_not_committed: "diagnosticsImpactCandidateNotCommitted",
  formal_write_recovery_pending: "diagnosticsImpactWritebackRecovery",
} as const;

const DIAGNOSTIC_ACTION_KEYS = {
  open_incomplete_prose: "diagnosticsActionOpenDraft",
  review_provider_output_limit: "diagnosticsActionOutputLimit",
  open_provider_settings: "diagnosticsActionProviderSettings",
  retry_after_provider_check: "diagnosticsActionRetryAfterProviderCheck",
  retry_generation_step: "diagnosticsActionRetryStep",
  review_generation_record: "diagnosticsActionReviewRecord",
  refresh_generation_readiness: "diagnosticsActionRefreshReadiness",
  restart_generation_job: "diagnosticsActionRestartJob",
  review_generation_authorization: "diagnosticsActionReviewAuthorization",
  open_affected_chapter: "diagnosticsActionOpenAffectedChapter",
  resume_generation_job: "diagnosticsActionResumeJob",
} as const;

const JOB_PAUSE_REASON_KEYS = {
  checkpoint: "reasonCheckpoint",
  conflict: "reasonConflict",
  outline_deviation: "reasonOutlineDeviation",
  cost_cap: "reasonCostCap",
  attempt_capacity: "reasonAttemptCapacity",
  manual: "reasonManual",
  process_restart: "reasonProcessRestart",
  uncertain_attempt: "reasonInterrupted",
  source_changed: "reasonSourceChanged",
  incomplete_scene: "reasonIncompleteScene",
  authorization_scope_increased: "reasonAuthorizationScopeIncreased",
  world_baseline_confirmation_required: "reasonWorldBaselineConfirmation",
  readiness_confirmation_required: "reasonReadinessConfirmation",
  reference_card_review: "reasonReferenceCardReview",
  reference_card_auto_creation_recovery: "reasonReferenceCardAutoCreationRecovery",
  reference_card_repair_exhausted: "reasonReferenceCardRepairExhausted",
  final_audit: "reasonFinalAudit",
  uncertain_skipped: "reasonUncertainSkipped",
  outline_adherence_manual_review: "reasonAdherenceManualReview",
  repair_not_converged: "reasonRepairNotConverged",
  repair_budget_exhausted_provider_technical_retry: "reasonProviderRetryBudgetExhausted",
  repair_budget_exhausted_adherence_judge_retry: "reasonJudgeRetryBudgetExhausted",
  repair_budget_exhausted_state_reextraction: "reasonStateReextractionBudgetExhausted",
  repair_budget_exhausted_local_prose_repair: "reasonProseRepairBudgetExhausted",
  repair_budget_exhausted_scene_regeneration: "reasonSceneRegenerationBudgetExhausted",
  repair_budget_exhausted_outline_rollback: "reasonOutlineRollbackBudgetExhausted",
} as const;

export type DiagnosticReasonTranslationKey =
  (typeof DIAGNOSTIC_REASON_KEYS)[keyof typeof DIAGNOSTIC_REASON_KEYS];
export type JobPauseReasonTranslationKey =
  (typeof JOB_PAUSE_REASON_KEYS)[keyof typeof JOB_PAUSE_REASON_KEYS];
export type DiagnosticImpactTranslationKey =
  (typeof DIAGNOSTIC_IMPACT_KEYS)[keyof typeof DIAGNOSTIC_IMPACT_KEYS];
export type DiagnosticActionTranslationKey =
  (typeof DIAGNOSTIC_ACTION_KEYS)[keyof typeof DIAGNOSTIC_ACTION_KEYS];

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

export function diagnosticImpactTranslationKey(
  impact: string,
): DiagnosticImpactTranslationKey | null {
  return DIAGNOSTIC_IMPACT_KEYS[
    impact as keyof typeof DIAGNOSTIC_IMPACT_KEYS
  ] ?? null;
}

export function diagnosticActionTranslationKey(
  actionCode: string,
): DiagnosticActionTranslationKey | null {
  return DIAGNOSTIC_ACTION_KEYS[
    actionCode as keyof typeof DIAGNOSTIC_ACTION_KEYS
  ] ?? null;
}


/** Only an explicit checkpoint reason may claim a planned review stop. */
export function checkpointPauseReasonTranslationKey(
  pauseReason: string | null | undefined,
  pauseReasonDetail?: string | null,
): JobPauseReasonTranslationKey | "reasonPauseUnknown" {
  return (pauseReasonDetail ? jobPauseReasonTranslationKey(pauseReasonDetail) : null)
    ?? (pauseReason ? jobPauseReasonTranslationKey(pauseReason) : null)
    ?? "reasonPauseUnknown";
}

const REPAIR_COMPONENT_KEYS = {
  provider_technical_retry: "repairComponentProviderRetry",
  adherence_judge_retry: "repairComponentJudgeRetry",
  state_reextraction: "repairComponentStateReextraction",
  local_prose_repair: "repairComponentProseRepair",
  scene_regeneration: "repairComponentSceneRegeneration",
  outline_rollback: "repairComponentOutlineRollback",
} as const;

export function diagnosticRepairBudget(details: Record<string, unknown>) {
  const component = details.repair_component;
  if (typeof component !== "string" || !Object.hasOwn(REPAIR_COMPONENT_KEYS, component)) return null;
  const used = details.component_used;
  const limit = details.component_limit;
  if (typeof used !== "number" || !Number.isSafeInteger(used) || used < 0
      || typeof limit !== "number" || !Number.isSafeInteger(limit) || limit < 0) return null;
  return {
    componentKey: REPAIR_COMPONENT_KEYS[component as keyof typeof REPAIR_COMPONENT_KEYS],
    used,
    limit,
  };
}
