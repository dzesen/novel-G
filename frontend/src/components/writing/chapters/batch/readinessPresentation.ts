import type {
  GenerationReadiness,
  OutlineDeviationPolicy,
} from "./batchTypes.ts";

import type { ProseContinuationPolicy } from "../prose/proseContinuation";
import type { ReferenceCardAutoCreationPolicy } from "./referenceCardAutoCreation.ts";
import type { ChapterReviewSelection } from "./chapterReviewPolicy.ts";
interface StartPayloadInput {
  checkpointInterval: number | null;
  tokenBudget: number | null;
  readiness: GenerationReadiness;
  acknowledgedCodes: Set<string>;
  outlineDeviationPolicy?: OutlineDeviationPolicy;
  generationParams?: Record<string, unknown>;
  proseContinuationPolicy?: ProseContinuationPolicy;
  referenceCardAutoCreationPolicy?: ReferenceCardAutoCreationPolicy;
  chapterReviewSelection?: ChapterReviewSelection;
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
  proseContinuationPolicy,
  referenceCardAutoCreationPolicy,
  chapterReviewSelection,
}: StartPayloadInput) {
  return {
    checkpoint_interval: checkpointInterval === null
      ? null
      : Math.min(1000, Math.max(1, Math.floor(checkpointInterval) || 1)),
    token_budget: tokenBudget,
    readiness_digest: readiness.digest,
    acknowledged_warning_codes: [...acknowledgedCodes].sort(),
    outline_deviation_policy: outlineDeviationPolicy,
    ...generationParams,
    ...(proseContinuationPolicy ? { prose_continuation_policy: proseContinuationPolicy } : {}),
    ...(referenceCardAutoCreationPolicy
      ? { reference_card_auto_creation_policy: referenceCardAutoCreationPolicy }
      : {}),
    ...(chapterReviewSelection ? { chapter_review_selection: chapterReviewSelection } : {}),
  };
}

export function readinessChapterCount(
  readiness: GenerationReadiness | null,
  fallback: number,
): number {
  return readiness ? readiness.work.chapter_count : fallback;
}
