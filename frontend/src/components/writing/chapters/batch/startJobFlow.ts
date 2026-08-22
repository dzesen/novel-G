export const START_JOB_STAGES = [
  "authorization",
  "behavior",
  "readiness",
  "confirmation",
] as const;

export type StartJobStage = (typeof START_JOB_STAGES)[number];

export const INITIAL_AUTHORIZATION_PERMISSIONS = [
  "token_budget",
  "provider_retry",
  "prose_continuation",
  "reference_card_auto_creation",
  "outline_deviation",
] as const;

const BATCH_GENERATION_OVERRIDE_KEYS = [
  "temperature",
  "top_p",
  "max_tokens",
  "presence_penalty",
  "frequency_penalty",
  "allow_failure_retry",
] as const;

export function batchGenerationOverrides(
  params: Readonly<Partial<Record<
    (typeof BATCH_GENERATION_OVERRIDE_KEYS)[number],
    unknown
  >>>,
): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const key of BATCH_GENERATION_OVERRIDE_KEYS) {
    const value = params[key];
    if (value !== null && value !== undefined) result[key] = value;
  }
  return result;
}

export function initialAuthorizationAllowsNext(
  tokenBudget: number | null,
): boolean {
  return tokenBudget !== null
    && Number.isInteger(tokenBudget)
    && tokenBudget > 0;
}

export function checkpointIntervalAllowsNext(value: number | null): boolean {
  return value === null
    || (Number.isInteger(value) && value >= 1 && value <= 1000);
}

export function nextStartJobStage(stage: StartJobStage): StartJobStage {
  const index = START_JOB_STAGES.indexOf(stage);
  return START_JOB_STAGES[Math.min(index + 1, START_JOB_STAGES.length - 1)];
}

export function previousStartJobStage(stage: StartJobStage): StartJobStage {
  const index = START_JOB_STAGES.indexOf(stage);
  return START_JOB_STAGES[Math.max(index - 1, 0)];
}
