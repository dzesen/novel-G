import type {
  BookCompletionIssue,
  BookCompletionIssueCategory,
  GenerationJob,
} from "./batchTypes";

const ISSUE_TRANSLATION_KEYS = {
  book_has_no_volumes: "bookHasNoVolumes",
  volume_order_gap: "volumeOrderGap",
  volume_has_no_chapters: "volumeHasNoChapters",
  chapter_order_gap: "chapterOrderGap",
  chapter_has_inactive_volume: "chapterHasInactiveVolume",
  chapter_outline_incomplete: "chapterOutlineIncomplete",
  chapter_prose_missing: "chapterProseMissing",
  chapter_prose_partial: "chapterProsePartial",
  chapter_prose_completion_unproven: "chapterProseCompletionUnproven",
  chapter_review_authorization_unproven: "chapterReviewAuthorizationUnproven",
  chapter_prose_acceptance_stale: "chapterProseAcceptanceStale",
  ai_prose_completion_gate_unproven: "aiProseCompletionGateUnproven",
  ai_prose_scene_gate_unproven: "aiProseSceneGateUnproven",
  manual_prose_completion_gate_unproven: "manualProseCompletionGateUnproven",
  chapter_prose_below_word_gate: "chapterProseBelowWordGate",
  chapter_word_count_stale: "chapterWordCountStale",
  chapter_state_not_current: "chapterStateNotCurrent",
  chapter_reference_unresolved: "chapterReferenceUnresolved",
  blocking_reference_candidates: "blockingReferenceCandidates",
  blocking_reference_card_proposal: "blockingReferenceCardProposal",
  blocking_card_import_proposal: "blockingCardImportProposal",
  repair_receipt_unresolved: "repairReceiptUnresolved",
  plot_thread_unresolved: "plotThreadUnresolved",
  frozen_worklist_invalid: "frozenWorklistInvalid",
  frozen_worklist_drift: "frozenWorklistDrift",
  semantic_review_unresolved: "semanticReviewUnresolved",
  semantic_review_stale: "semanticReviewStale",
  semantic_conflict_unresolved: "semanticConflictUnresolved",
  uncertain_provider_attempt: "uncertainProviderAttempt",
  generation_source_changed: "generationSourceChanged",
  reference_card_repair_exhausted: "referenceCardRepairExhausted",
  repair_checkpoint_unresolved: "repairCheckpointUnresolved",
  world_baseline_not_current: "worldBaselineNotCurrent",
  current_failure_event_missing: "currentFailureEventMissing",
  current_failure_event_invalid: "currentFailureEventInvalid",
  generation_failure_active: "generationFailureActive",
} as const;

export type BookCompletionResult =
  | "not_book"
  | "unverified"
  | "blocked"
  | "complete";

export type BookCompletionAction =
  | "blueprint"
  | "world_baseline"
  | "chapter"
  | "memory"
  | "reference_cards"
  | "reference_candidates"
  | "plot_threads"
  | "generation_runs"
  | null;

export interface BookCompletionIssueTarget {
  chapterId?: string;
  jobId?: string;
  eventId?: string;
  candidateIds: string[];
}

export interface BookCompletionIssueGroup {
  code: string;
  category: BookCompletionIssueCategory;
  level: "blocking" | "advisory";
  count: number;
  targets: BookCompletionIssueTarget[];
}

function normalizedTargetId(value: unknown): string | undefined {
  if (
    typeof value === "string"
    && value.length > 0
    && value === value.trim()
  ) {
    return value;
  }
  return undefined;
}

function issueTarget(
  issue: BookCompletionIssue,
): BookCompletionIssueTarget {
  const chapterId = normalizedTargetId(issue.chapter_id);
  const jobId = normalizedTargetId(issue.job_id);
  const eventId = normalizedTargetId(issue.details.event_id);
  const candidateIds = issue.details.candidate_ids;
  return {
    ...(chapterId ? { chapterId } : {}),
    ...(jobId ? { jobId } : {}),
    ...(eventId ? { eventId } : {}),
    candidateIds: Array.isArray(candidateIds)
      ? [...new Set(candidateIds
          .map(normalizedTargetId)
          .filter((candidateId): candidateId is string => Boolean(candidateId)))]
      : [],
  };
}

function appendIssueTarget(
  group: BookCompletionIssueGroup,
  issue: BookCompletionIssue,
): void {
  const target = issueTarget(issue);
  const key = JSON.stringify(target);
  if (!group.targets.some((item) => JSON.stringify(item) === key)) {
    group.targets.push(target);
  }
}

export function bookCompletionChapterIds(
  group: Pick<BookCompletionIssueGroup, "targets">,
): string[] {
  return [...new Set(group.targets
    .map((target) => target.chapterId)
    .filter((chapterId): chapterId is string => Boolean(chapterId)))];
}

export function bookCompletionAuditMatchesJob(
  audit: NonNullable<GenerationJob["completion_audit"]>,
  jobId: string,
): boolean {
  return audit.blueprint.frozen_job_id === jobId;
}

export function bookCompletionResult(
  job: Pick<GenerationJob, "_id" | "scope" | "status" | "completion_audit">,
  currentAudit: GenerationJob["completion_audit"] = null,
): BookCompletionResult {
  if (job.scope !== "book") return "not_book";
  if (
    job.status === "completed"
    && currentAudit
    && !bookCompletionAuditMatchesJob(currentAudit, job._id)
  ) {
    return "unverified";
  }
  const audit = job.status === "completed"
    ? currentAudit
    : job.completion_audit;
  if (!audit) return "unverified";
  if (audit.complete && audit.status === "complete") {
    return job.status === "completed" ? "complete" : "unverified";
  }
  return "blocked";
}

export function summarizeBookCompletionIssues(
  issues: BookCompletionIssue[],
): BookCompletionIssueGroup[] {
  const groups = new Map<string, BookCompletionIssueGroup>();
  for (const issue of issues) {
    const key = `${issue.level}:${issue.category}:${issue.code}`;
    const existing = groups.get(key);
    if (!existing) {
      const group: BookCompletionIssueGroup = {
        code: issue.code,
        category: issue.category,
        level: issue.level,
        count: 1,
        targets: [],
      };
      appendIssueTarget(group, issue);
      groups.set(key, group);
      continue;
    }
    existing.count += 1;
    appendIssueTarget(existing, issue);
  }
  return [...groups.values()];
}

export function bookCompletionIssueTranslationKey(code: string):
  | (typeof ISSUE_TRANSLATION_KEYS)[keyof typeof ISSUE_TRANSLATION_KEYS]
  | "unknown" {
  return ISSUE_TRANSLATION_KEYS[
    code as keyof typeof ISSUE_TRANSLATION_KEYS
  ] ?? "unknown";
}

export function bookCompletionAction(
  group: Pick<BookCompletionIssueGroup, "code" | "category" | "targets">,
): BookCompletionAction {
  if (group.code === "world_baseline_not_current") {
    return "world_baseline";
  }
  if (group.code === "blocking_reference_candidates") {
    return "reference_candidates";
  }
  if (group.category === "runtime") return "generation_runs";
  if (bookCompletionChapterIds(group).length > 0) return "chapter";
  if (group.category === "structure") return "blueprint";
  if (group.category === "reference") return "reference_cards";
  if (group.category === "thread") return "plot_threads";
  if (group.category === "state") return "memory";
  return null;
}
