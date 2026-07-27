import type {
  GenerationReadiness,
  OutlineDeviationPolicy,
} from "./batchTypes.ts";

interface StartPayloadInput {
  checkpointInterval: number;
  tokenBudget: number | null;
  readiness: GenerationReadiness;
  acknowledgedCodes: Set<string>;
  outlineDeviationPolicy?: OutlineDeviationPolicy;
  generationParams?: Record<string, unknown>;
}

export function readinessAllowsStart(
  readiness: GenerationReadiness,
  acknowledgedCodes: Set<string>,
): boolean {
  if (readiness.status === "blocked" || readiness.issues.some((issue) => issue.level === "blocked")) {
    return false;
  }
  return readiness.issues
    .filter((issue) => issue.level === "warning_requires_ack")
    .every((issue) => acknowledgedCodes.has(issue.code));
}

export function buildAuthorizedStartPayload({
  checkpointInterval,
  tokenBudget,
  readiness,
  acknowledgedCodes,
  outlineDeviationPolicy = "pause_for_rewrite",
  generationParams = {},
}: StartPayloadInput) {
  return {
    checkpoint_interval: Math.min(1000, Math.max(1, Math.floor(checkpointInterval) || 1)),
    token_budget: tokenBudget,
    readiness_digest: readiness.digest,
    acknowledged_warning_codes: [...acknowledgedCodes].sort(),
    outline_deviation_policy: outlineDeviationPolicy,
    ...generationParams,
  };
}
