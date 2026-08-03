import type {
  ChapterProgress,
  GenerationNotice,
  OutlineAdherenceIssue,
  OutlineAdherenceReview,
  StepOutcome,
  StepOutcomeStatus,
} from "./batchTypes.ts";
import {
  contextCountsForDisplay,
  contextSectionKind,
  generationStepKind,
  referenceCleanupForDisplay,
  referenceRemapForDisplay,
  type ContextSectionKind,
  type GenerationStepKind,
  type ReferenceCleanupPresentation,
  type ReferenceRemapPresentation,
// @ts-expect-error Node's strip-types test runner requires the source extension.
} from "../../generationMetadataPresentation.ts";

export interface StepBadge {
  step: string;
  status: StepOutcomeStatus;
  reasonCode: string | null;
}

export interface ContextNoticePresentation {
  step: GenerationStepKind;
  truncatedSections: ContextSectionKind[];
  droppedItemCounts: Partial<Record<ContextSectionKind, number>>;
}

export interface ChapterPresentation {
  stepBadges: StepBadge[];
  contextNotices: ContextNoticePresentation[];
  referenceNotices: ReferenceCleanupPresentation[];
  referenceRemapNotices: ReferenceRemapPresentation[];
}

function asStrings(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value.map(String).filter((item) => item.trim().length > 0);
  }
  if (value === null || value === undefined || String(value).trim().length === 0) return [];
  return [String(value)];
}

function asCounts(value: unknown): Record<string, number> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return Object.fromEntries(
    Object.entries(value)
      .map(([key, raw]) => [key, Number(raw)] as const)
      .filter(([, count]) => Number.isFinite(count) && count >= 0),
  );
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function isOutlineAdherenceIssue(value: unknown): value is OutlineAdherenceIssue {
  const issue = asRecord(value);
  if (!issue) return false;
  return (
    (issue.severity === "warning" || issue.severity === "error")
    && (
      issue.category === "scene_coverage"
      || issue.category === "scene_order"
      || issue.category === "core_conflict"
      || issue.category === "ending_hook"
      || issue.category === "unplanned_major_event"
      || issue.category === "volume_arc"
    )
    && typeof issue.outline_requirement === "string"
    && typeof issue.prose_evidence === "string"
    && typeof issue.explanation === "string"
  );
}

function isSceneCoverage(
  value: unknown,
): value is OutlineAdherenceReview["scene_coverage"][number] {
  const scene = asRecord(value);
  return Boolean(
    scene
    && typeof scene.scene_index === "number"
    && (scene.status === "covered" || scene.status === "partial" || scene.status === "missing")
    && typeof scene.evidence === "string",
  );
}

/**
 * Old checkpoints can contain the default `{}` when generation stopped before
 * the adherence step.  An absent review is not a failed review and must not
 * be rendered as one.
 */
export function outlineAdherenceForDisplay(
  value: unknown,
): OutlineAdherenceReview | null {
  const review = asRecord(value);
  if (!review) return null;
  const verdict = review.verdict;
  if (verdict !== "pass" && verdict !== "warn" && verdict !== "fail") return null;
  return {
    verdict,
    summary: typeof review.summary === "string" ? review.summary : "",
    scene_coverage: Array.isArray(review.scene_coverage)
      ? review.scene_coverage.filter(isSceneCoverage)
      : [],
    issues: Array.isArray(review.issues)
      ? review.issues.filter(isOutlineAdherenceIssue)
      : [],
  };
}

function legacyStepBadges(progress: ChapterProgress): StepBadge[] {
  return [
    ...progress.steps_done.map((step) => ({
      step,
      status: "generated" as const,
      reasonCode: null,
    })),
    ...progress.steps_skipped.map((step) => ({
      step,
      status: "reused" as const,
      reasonCode: "legacy_existing",
    })),
  ];
}

function noticeFields(notice: GenerationNotice): ReferenceCleanupPresentation[] {
  const rawFields = notice.details.fields;
  if (!Array.isArray(rawFields)) return [];
  const dropped: Record<string, unknown> = {};
  for (const raw of rawFields) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) continue;
    const field = String((raw as Record<string, unknown>).field ?? "");
    const values = asStrings((raw as Record<string, unknown>).values);
    if (field && values.length > 0) dropped[field] = values;
  }
  return referenceCleanupForDisplay(dropped);
}

function noticeMappings(notice: GenerationNotice): ReferenceRemapPresentation[] {
  const rawMappings = notice.details.mappings;
  if (!Array.isArray(rawMappings)) return [];
  return rawMappings.flatMap((raw) => {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return [];
    const item = raw as Record<string, unknown>;
    const field = String(item.field ?? "");
    const from = String(item.from ?? "");
    const to = String(item.to ?? "");
    if (!field || !from || !to) return [];
    return [referenceRemapForDisplay({
      field,
      from,
      to,
      matched_by: item.matched_by,
    })];
  });
}

export function buildChapterPresentation(progress: ChapterProgress): ChapterPresentation {
  const stepBadges = (progress.step_outcomes?.length
    ? progress.step_outcomes.map((outcome: StepOutcome) => ({
        step: outcome.step,
        status: outcome.status,
        reasonCode: outcome.reason_code,
      }))
    : legacyStepBadges(progress));

  const contextNotices = (progress.notices ?? [])
    .filter((notice) => notice.category === "context")
    .map((notice) => ({
      step: generationStepKind(notice.step),
      truncatedSections: asStrings(notice.details.truncated_sections).map(contextSectionKind),
      droppedItemCounts: contextCountsForDisplay(asCounts(notice.details.dropped_item_counts)),
    }));
  if (contextNotices.length === 0) {
    contextNotices.push(...progress.truncations.map((truncation) => ({
      step: generationStepKind(truncation.step),
      truncatedSections: truncation.truncated_sections.map(contextSectionKind),
      droppedItemCounts: contextCountsForDisplay(truncation.dropped_item_counts),
    })));
  }

  const referenceNotices = (progress.notices ?? [])
    .filter((notice) => notice.category === "reference")
    .flatMap(noticeFields);
  if (referenceNotices.length === 0) {
    referenceNotices.push(...referenceCleanupForDisplay(progress.dropped_ids));
  }

  const referenceRemapNotices = (progress.notices ?? [])
    .filter((notice) => notice.category === "reference_remap")
    .flatMap(noticeMappings);

  return { stepBadges, contextNotices, referenceNotices, referenceRemapNotices };
}
