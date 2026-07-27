import type {
  ChapterProgress,
  GenerationNotice,
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
