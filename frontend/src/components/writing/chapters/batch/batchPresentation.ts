import type {
  ChapterProgress,
  GenerationNotice,
  OutlineAdherenceIssue,
  OutlineAdherenceReview,
  StepOutcome,
  StepOutcomeStatus,
} from "./batchTypes.ts";

export interface StepBadge {
  step: string;
  status: StepOutcomeStatus;
  reasonCode: string | null;
}

export interface ContextNoticePresentation {
  step: string | null;
  truncatedSections: string[];
  droppedItemCounts: Record<string, number>;
}

export interface ReferenceNoticePresentation {
  step?: string | null;
  field: string;
  values: string[];
  impact: string;
  actionCodes: string[];
}

export interface ReferenceRemapPresentation {
  step: string | null;
  field: string;
  from: string;
  to: string;
  matchedBy: string;
}

export interface ChapterPresentation {
  stepBadges: StepBadge[];
  contextNotices: ContextNoticePresentation[];
  referenceNotices: ReferenceNoticePresentation[];
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

function noticeFields(notice: GenerationNotice): ReferenceNoticePresentation[] {
  const rawFields = notice.details.fields;
  if (!Array.isArray(rawFields)) return [];
  return rawFields.flatMap((raw) => {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return [];
    const field = String((raw as Record<string, unknown>).field ?? "");
    const values = asStrings((raw as Record<string, unknown>).values);
    if (!field || values.length === 0) return [];
    return [{
      step: notice.step,
      field,
      values,
      impact: notice.impact,
      actionCodes: notice.action_codes,
    }];
  });
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
    return [{
      step: notice.step,
      field,
      from,
      to,
      matchedBy: String(item.matched_by ?? ""),
    }];
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
      step: notice.step,
      truncatedSections: asStrings(notice.details.truncated_sections),
      droppedItemCounts: asCounts(notice.details.dropped_item_counts),
    }));
  if (contextNotices.length === 0) {
    contextNotices.push(...progress.truncations.map((truncation) => ({
      step: truncation.step,
      truncatedSections: truncation.truncated_sections,
      droppedItemCounts: truncation.dropped_item_counts,
    })));
  }

  const referenceNotices = (progress.notices ?? [])
    .filter((notice) => notice.category === "reference")
    .flatMap(noticeFields);
  if (referenceNotices.length === 0) {
    referenceNotices.push(...Object.entries(progress.dropped_ids).flatMap(([field, raw]) => {
      const values = asStrings(raw);
      if (values.length === 0) return [];
      return [{
        field,
        values,
        impact: "references_not_applied",
        actionCodes: ["review_reference_cards"],
      }];
    }));
  }

  const referenceRemapNotices = (progress.notices ?? [])
    .filter((notice) => notice.category === "reference_remap")
    .flatMap(noticeMappings);

  return { stepBadges, contextNotices, referenceNotices, referenceRemapNotices };
}
