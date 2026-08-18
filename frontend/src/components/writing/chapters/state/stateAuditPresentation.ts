export type StateAuditTone = "neutral" | "info" | "warning" | "danger";

export interface StateAuditChapterLike {
  chapter_id: string;
  category: string;
  repair_recommended: boolean;
}

const HEALTHY_CATEGORIES = new Set(["current", "legal_empty", "no_prose"]);

export function buildStateAuditQuery(
  novelId: string,
  scope: "book" | "volume",
  volumeId: string | null,
): string {
  const query = new URLSearchParams({ scope });
  if (scope === "volume" && volumeId) query.set("volume_id", volumeId);
  return `/api/state-timeline/novel/${novelId}/completeness-audit?${query.toString()}`;
}

export function visibleAuditChapters<T extends StateAuditChapterLike>(
  chapters: T[],
  issuesOnly: boolean,
): T[] {
  if (!issuesOnly) return chapters;
  return chapters.filter((chapter) => !HEALTHY_CATEGORIES.has(chapter.category));
}

export function stateAuditIssueId(
  chapter: StateAuditChapterLike,
): string | null {
  return HEALTHY_CATEGORIES.has(chapter.category)
    ? null
    : `state-completeness:${chapter.chapter_id}`;
}

export function auditCategoryTone(category: string): StateAuditTone {
  if (
    category === "degraded_all_character_updates_dropped" ||
    category === "degraded_job_reference_drop_evidence"
  ) {
    return "danger";
  }
  if (
    category === "stale_after_content_edit" ||
    category === "degraded_partial_reference_drop"
  ) {
    return "warning";
  }
  if (
    category === "summary_without_delta" ||
    category === "content_without_delta" ||
    category === "unknown_legacy"
  ) {
    return "info";
  }
  return "neutral";
}
