export interface ReviewValidationGroup {
  phase: "primary" | "repair" | "validation";
  issues: Array<{ path: string; errorType: string }>;
  truncated: boolean;
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : null;
}

export function reviewValidationGroups(value: unknown): ReviewValidationGroup[] {
  const source = record(value);
  if (!source) return [];
  const entries: Array<[ReviewValidationGroup["phase"], unknown]> =
    source.schema_version === "structured_repair_failure.v1"
      && source.failure_type === "structured_repair_invalid"
      ? [["primary", source.primary_validation], ["repair", source.repair_validation]]
      : [["validation", source]];
  return entries.flatMap(([phase, candidate]) => {
    const validation = record(candidate);
    if (validation?.schema_version !== "structured_validation_issues.v1"
      || !Array.isArray(validation.issues)) return [];
    const issues = validation.issues.slice(0, 20).flatMap((raw) => {
      const issue = record(raw);
      const path = issue?.path;
      const errorType = issue?.error_type;
      if (typeof path !== "string" || path.length > 240 || !/^[\w$.[\]*]+$/.test(path)
        || typeof errorType !== "string" || errorType.length > 64
        || !/^[a-zA-Z0-9_]+$/.test(errorType)) return [];
      return [{ path, errorType }];
    });
    return issues.length ? [{
      phase, issues, truncated: validation.truncated === true || validation.issues.length > 20,
    }] : [];
  });
}

const VALIDATION_ERROR_KEYS = {
  json_decode_error: "completionValidationJsonInvalid",
  state_fact_prose_spans_required: "completionValidationProseSpansRequired",
  review_anchor_unknown: "completionValidationAnchorUnknown",
  review_quote_missing: "completionValidationQuoteMissing",
  review_quote_ambiguous: "completionValidationQuoteAmbiguous",
  review_quote_scene_mismatch: "completionValidationSceneMismatch",
  review_source_mismatch: "completionValidationSourceMismatch",
  review_anchor_range_reversed: "completionValidationRangeInvalid",
  review_anchor_range_too_long: "completionValidationRangeInvalid",
  state_fact_span_quote_not_found: "completionValidationQuoteMissing",
  state_fact_span_quote_ambiguous: "completionValidationQuoteAmbiguous",
  state_fact_span_bounds_invalid: "completionValidationRangeInvalid",
  state_fact_span_order_invalid: "completionValidationRangeInvalid",
} as const;

export function reviewValidationErrorKey(errorType: string) {
  return Object.hasOwn(VALIDATION_ERROR_KEYS, errorType)
    ? VALIDATION_ERROR_KEYS[errorType as keyof typeof VALIDATION_ERROR_KEYS]
    : "completionValidationStructureInvalid";
}
