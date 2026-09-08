"use client";

import { useTranslations } from "next-intl";
import type {
  BookCompletionAudit,
  GenerationRunsNavigationTarget,
} from "./batchTypes";
import {
  bookCompletionAction,
  bookCompletionChapterIds,
  bookCompletionIssueTranslationKey,
  summarizeBookCompletionIssues,
  type BookCompletionAction,
} from "./bookCompletionPresentation";

interface BookCompletionAuditPanelProps {
  audit: BookCompletionAudit;
  titleForChapter: (chapterId: string) => string;
  onJumpToChapter: (chapterId: string) => void;
  onNavigateToBlueprint: () => void;
  onNavigateToWorldBaseline: () => void;
  onNavigateToMemory: () => void;
  onNavigateToReferenceCards: () => void;
  onNavigateToReferenceCardCandidates: (candidateId?: string) => void;
  onNavigateToPlotThreads: () => void;
  onOpenGenerationRuns: (target?: GenerationRunsNavigationTarget) => void;
  onRefresh?: () => void;
}

export default function BookCompletionAuditPanel({
  audit,
  titleForChapter,
  onJumpToChapter,
  onNavigateToBlueprint,
  onNavigateToWorldBaseline,
  onNavigateToMemory,
  onNavigateToReferenceCards,
  onNavigateToReferenceCardCandidates,
  onNavigateToPlotThreads,
  onOpenGenerationRuns,
  onRefresh,
}: BookCompletionAuditPanelProps) {
  const t = useTranslations("writing.batch");
  const groups = summarizeBookCompletionIssues(audit.issues);
  const tone = audit.complete
    ? "border-emerald-300 bg-emerald-50 text-emerald-950 dark:border-emerald-900/70 dark:bg-emerald-950/30 dark:text-emerald-100"
    : "border-amber-300 bg-amber-50 text-amber-950 dark:border-amber-900/70 dark:bg-amber-950/30 dark:text-amber-100";

  const runAction = (
    action: BookCompletionAction,
    group: ReturnType<typeof summarizeBookCompletionIssues>[number],
  ) => {
    const target = group.targets.find((item) => (
      action === "reference_candidates"
        ? item.candidateIds.length > 0
        : action === "generation_runs"
          ? Boolean(item.jobId || item.eventId || item.chapterId)
          : Boolean(item.chapterId)
    )) ?? group.targets[0];
    const chapterId = target?.chapterId;
    if (action === "blueprint") onNavigateToBlueprint();
    else if (action === "world_baseline") onNavigateToWorldBaseline();
    else if (action === "chapter" && chapterId) onJumpToChapter(chapterId);
    else if (action === "memory") onNavigateToMemory();
    else if (action === "reference_cards") onNavigateToReferenceCards();
    else if (action === "reference_candidates") {
      onNavigateToReferenceCardCandidates(target?.candidateIds[0]);
    } else if (action === "plot_threads") onNavigateToPlotThreads();
    else if (action === "generation_runs") {
      onOpenGenerationRuns({
        jobId: target?.jobId,
        chapterId,
        eventId: target?.eventId,
      });
    }
  };

  return (
    <section
      aria-labelledby="book-completion-audit-title"
      aria-live="polite"
      className={`grid gap-3 border-b px-4 py-3 ${tone}`}
    >
      <div className="flex min-w-0 flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3
            id="book-completion-audit-title"
            className="text-sm font-semibold"
          >
            {t(audit.complete ? "bookAuditCompleteTitle" : "bookAuditBlockedTitle")}
          </h3>
          <p className="mt-1 text-xs leading-5 opacity-85">
            {audit.complete
              ? t("bookAuditCompleteSummary", {
                  chapters: audit.summary.chapter_count,
                  states: audit.summary.current_state_count,
                })
              : t("bookAuditBlockedSummary", {
                  issues: audit.summary.blocking_issue_count,
                  complete: audit.summary.complete_chapter_count,
                  total: audit.summary.chapter_count,
                  states: audit.summary.current_state_count,
                })}
          </p>
        </div>
        <div className="flex shrink-0 flex-wrap items-center justify-end gap-2">
          <span className="rounded-full border border-current/20 px-2 py-1 text-[11px] font-medium">
            {t("bookAuditEvidence", { digest: audit.audit_digest.slice(0, 12) })}
          </span>
          {onRefresh && (
            <button
              type="button"
              className="rounded-md border border-current/25 px-2 py-1 text-[11px] font-medium hover:bg-white/40 dark:hover:bg-black/10"
              onClick={onRefresh}
            >
              {t("bookAuditRefresh")}
            </button>
          )}
        </div>
      </div>

      {audit.summary.reviewed_chapter_count != null && audit.summary.unreviewed_chapter_count != null && (
        <details data-testid="book-audit-review-coverage" className="text-xs leading-5">
          <summary className="min-h-11 cursor-pointer py-3 font-medium focus-visible:outline-2 focus-visible:outline-current">
            {t("bookAuditReviewCoverage", {
              reviewed: audit.summary.reviewed_chapter_count,
              unreviewed: audit.summary.unreviewed_chapter_count,
            })}
          </summary>
          <p className="mb-2">{t("bookAuditReviewCoverageHint")}</p>
          <ul className="grid max-h-60 gap-1 overflow-y-auto overscroll-contain">
            {audit.chapters.filter((chapter) => chapter.independent_review_status).map((chapter) => (
              <li key={chapter.chapter_id} className="flex min-w-0 flex-wrap items-center justify-between gap-x-3 gap-y-1">
                <button type="button" onClick={() => onJumpToChapter(chapter.chapter_id)} className="min-h-11 min-w-0 break-words py-2 text-left font-medium underline underline-offset-4 focus-visible:outline-2 focus-visible:outline-current">
                  {titleForChapter(chapter.chapter_id)}
                </button>
                <span>{t(chapter.independent_review_status === "passed" ? "bookAuditReviewed" : "bookAuditNotReviewed")}</span>
              </li>
            ))}
          </ul>
        </details>
      )}

      {!audit.complete && groups.length > 0 && (
        <ul className="grid gap-2">
          {groups.map((group) => {
            const action = bookCompletionAction(group);
            const chapterNames = bookCompletionChapterIds(group)
              .slice(0, 3)
              .map(titleForChapter)
              .join(t("bookAuditChapterSeparator"));
            return (
              <li
                key={`${group.level}:${group.category}:${group.code}`}
                className="flex min-w-0 flex-wrap items-start justify-between gap-x-4 gap-y-2 rounded-md border border-current/15 bg-white/45 px-3 py-2 text-xs dark:bg-black/10"
              >
                <div className="min-w-0 flex-1 leading-5">
                  <p className="font-medium">
                    {t(`bookAuditIssues.${bookCompletionIssueTranslationKey(group.code)}`, {
                      count: group.count,
                    })}
                  </p>
                  {chapterNames && (
                    <p className="break-words opacity-75">
                      {t("bookAuditChapters", { chapters: chapterNames })}
                    </p>
                  )}
                </div>
                {action && (
                  <button
                    type="button"
                    className="shrink-0 font-medium text-accent hover:underline"
                    onClick={() => runAction(action, group)}
                  >
                    {t(`bookAuditActions.${action}`)}
                  </button>
                )}
              </li>
            );
          })}
        </ul>
      )}

      <p className="text-[11px] leading-5 opacity-75">
        {t("bookAuditOptionalExcluded")}
      </p>
    </section>
  );
}
