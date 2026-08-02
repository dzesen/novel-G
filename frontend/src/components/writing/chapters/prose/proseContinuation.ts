export interface ProseContinuationPolicy {
  automatic_continuations_per_scene: number;
  continuation_target_words: number;
}

export interface ProseContinuationAuthorization {
  policy: ProseContinuationPolicy;
  authorization_revision: number;
  max_base_calls: number;
  max_automatic_continuation_calls: number;
  max_logical_prose_calls: number;
  conservative_token_bound: number;
  token_budget: number | null;
  readiness_digest: string;
}

export interface ProseReadiness {
  execution_plan: {
    requested_word_count: number;
    scene_count: number;
    mode: "single_call" | "scene_segments";
    safe_output_budget: number;
    scheduled_base_call_count: number;
    protocol_revision: string;
  };
  authorization: ProseContinuationAuthorization;
  warnings: string[];
  token_bound_known: boolean;
  requires_automatic_confirmation: boolean;
  provider: {
    alias: string;
    model: string;
    max_output_tokens: number | null;
  };
}

export const DEFAULT_PROSE_CONTINUATION_POLICY: ProseContinuationPolicy = {
  automatic_continuations_per_scene: 0,
  continuation_target_words: 1000,
};

export const MIN_AUTOMATIC_CONTINUATIONS_PER_SCENE = 0;
export const MAX_AUTOMATIC_CONTINUATIONS_PER_SCENE = 15;
export const MIN_CONTINUATION_TARGET_WORDS = 400;
export const MAX_CONTINUATION_TARGET_WORDS = 5000;

export function permitsAutomaticContinuation(
  policy: ProseContinuationPolicy,
): boolean {
  return policy.automatic_continuations_per_scene > 0;
}

export function parsePositiveInteger(value: string): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) && Number.isInteger(parsed) && parsed >= 1
    ? parsed
    : null;
}
