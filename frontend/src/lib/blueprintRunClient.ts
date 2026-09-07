import type { AICreateRequest, AICreateResponse, AICreateCachedSteps, AICreateStepKey, BlueprintStrategy, BlueprintExecutionRef, BlueprintGenerationParams } from "@/types/novel";
import { normalizeAuthorConstraints } from "./authorInput.ts";

export interface BlueprintRunRequest extends Omit<AICreateRequest, "cached_steps" | "creative_direction"> {
  creative_direction?: AICreateRequest["creative_direction"] | null;
  card_imports?: Array<{ proposal_id: string; digest: string }>;
  draft_id?: string | null;
  reuse_run_id?: string | null;
  strategy?: BlueprintStrategy;
  token_budget?: number | null;
}
export interface BlueprintReadiness {
  version: 3;
  run_id: string;
  draft_id: string;
  digest: string;
  author_brief_revision: string;
  prompt_revision: string;
  strategy: BlueprintStrategy;
  status: "ready" | "warning_requires_ack" | "blocked";
  token_budget: number | null;
  uses_system_token_budget: boolean;
  maximum_provider_attempts: number;
  maximum_tokens_total: number;
  budget_covers_conservative_maximum: boolean;
  providers: Array<{ step: string; provider_alias: string; provider_model: string; maximum_attempts: number }>;
  uncertain_source: boolean;
  reused_steps: string[];
  source_summary: { calls_used: number; tokens_used: number; has_uncertain: boolean } | null;
}
export interface BlueprintRunSummary extends BlueprintExecutionRef {
  status: "ready" | "running" | "paused" | "completed" | "failed" | "uncertain";
  completed_steps: string[];
  current_step: string | null;
  calls_used: number;
  tokens_used: number;
  tokens_reserved: number;
  cumulative_calls_used: number;
  cumulative_tokens_used: number;
  has_uncertain: boolean;
  failure_code: string | null;
  created_at: string;
  updated_at: string;
}
export interface BlueprintRun extends BlueprintRunSummary {
  request: BlueprintRunRequest;
  readiness: BlueprintReadiness;
  cached_steps: AICreateCachedSteps;
  result: AICreateResponse | null;
}

const PARAMS = ["temperature", "top_p", "max_tokens", "presence_penalty", "frequency_penalty", "system_prompt"] as const;
export function blueprintStepOrder(strategy: BlueprintStrategy = "four_step"): AICreateStepKey[] {
  return strategy === "two_step" ? ["expand_idea", "blueprint"] : ["expand_idea", "extract_idea", "core_seed", "novel_meta"];
}

export function isBlueprintStrategy(value: unknown): value is BlueprintStrategy {
  return value === "four_step" || value === "two_step";
}
export function blueprintGenerationParams(request: AICreateRequest): BlueprintGenerationParams {
  return Object.fromEntries(PARAMS.filter((key) => (request[key] != null || (key === "max_tokens" && request[key] === null))).map((key) => [key, request[key]]));
}

function canonical(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([k, v]) => [k, canonical(v)]));
  return value;
}
/** Compare actual author inputs and generation knobs, independently of run/budget pointers. */
export function blueprintInputIdentity(request: BlueprintRunRequest): string {
  return JSON.stringify(canonical({
    user_idea: request.user_idea.trim(),
    number_of_chapters: request.number_of_chapters ?? 100,
    words_per_chapter: request.words_per_chapter ?? 3000,
    author_constraints: normalizeAuthorConstraints(request.author_constraints),
    creative_direction: request.creative_direction ? {
      ...request.creative_direction,
      provider_alias: request.creative_direction.provider_alias ?? null,
      user_adjustments: request.creative_direction.user_adjustments ?? "",
      card_context_digest: request.creative_direction.card_context_digest ?? null,
      direction: { ...request.creative_direction.direction, must_keep: request.creative_direction.direction.must_keep ?? [], risks: request.creative_direction.direction.risks ?? [] },
    } : null,
    card_imports: request.card_imports ?? [],
    strategy: request.strategy ?? "four_step",
    ...Object.fromEntries(PARAMS.map((key) => [key, request[key] === undefined ? (key === "max_tokens" ? 16384 : null) : request[key]])),
  }));
}

export function blueprintAuthorIdentity(request: BlueprintRunRequest): string {
  return blueprintInputIdentity({ ...request, temperature: null, top_p: null, max_tokens: null, presence_penalty: null, frequency_penalty: null, system_prompt: null });
}

export function blueprintExecutionRef(value: BlueprintRun | BlueprintReadiness): BlueprintExecutionRef {
  return {
    run_id: value.run_id, draft_id: value.draft_id,
    authorization_digest: "digest" in value ? value.digest : value.authorization_digest,
    author_brief_revision: value.author_brief_revision, prompt_revision: value.prompt_revision,
  };
}

export function normalizeBlueprintExecution(value: unknown): BlueprintExecutionRef | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return;
  const record = value as Record<string, unknown>;
  if (typeof record.run_id !== "string" || !/^[0-9a-f]{24}$/.test(record.run_id)
    || typeof record.draft_id !== "string" || !/^[a-zA-Z0-9_-]{1,100}$/.test(record.draft_id)
    || !["authorization_digest", "author_brief_revision", "prompt_revision"].every((key) => typeof record[key] === "string" && /^[0-9a-f]{64}$/.test(record[key]))) return;
  return blueprintExecutionRef(record as unknown as BlueprintRun);
}

export function normalizeBlueprintParams(value: unknown): BlueprintGenerationParams | undefined {
  if (value === undefined) return undefined;
  if (!value || typeof value !== "object" || Array.isArray(value)) return;
  const record = value as Record<string, unknown>;
  const bounds = { temperature: [0, 2], top_p: [0, 1], max_tokens: [1, 200000], presence_penalty: [-2, 2], frequency_penalty: [-2, 2] };
  for (const [key, v] of Object.entries(record)) {
    if (!PARAMS.includes(key as typeof PARAMS[number])) return;
    if (v == null) continue;
    if (key === "system_prompt") { if (typeof v !== "string" || v.length > 20000) return; }
    else {
      const [min, max] = bounds[key as keyof typeof bounds];
      if (typeof v !== "number" || !Number.isFinite(v) || v < min || v > max || (key === "max_tokens" && !Number.isInteger(v))) return;
    }
  }
  return blueprintGenerationParams(record as unknown as AICreateRequest);
}


export function buildBlueprintStartRequest(request: BlueprintRunRequest, report: BlueprintReadiness, acknowledgeBudget = false, acknowledgeUnknown = false) {
  return { ...request, run_id: report.run_id, readiness_digest: report.digest,
    acknowledge_automatic_token_budget: acknowledgeBudget, acknowledge_uncertain_source: acknowledgeUnknown };
}

export function buildBlueprintResumeRequest(execution: BlueprintExecutionRef) {
  return { readiness_digest: execution.authorization_digest };
}
