"use client";

import { useTranslations } from "next-intl";
import type { BookCompletionAudit } from "./batchTypes";
import {
  bookCompletionAction,
  bookCompletionIssueTranslationKey,
  summarizeBookCompletionIssues,
  type BookCompletionAction,
} from "./bookCompletionPresentation";

interface BookCompletionAuditPanelProps {
  audit: BookCompletionAudit;
  titleForChapter: (chapterId: string) => string;
  onJumpToChapter: (chapterId: string) => void;
  onNavigateToBlueprint: () => void;
  onNavigateToMemory: () => void;
  onNavigateToReferenceCards: () => void;
  onNavigateToReferenceCardCandidates: () => void;
  onNavigateToPlotThreads: () => void;
  onOpenGenerationRuns: () => void;
  onRefresh?: () => void;
}

export default function BookCompletionAuditPanel({
  audit,
  titleForChapter,
  onJumpToChapter,
  onNavigateToBlueprint,
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
    chapterId?: string,
  ) => {
    if (action === "blueprint") onNavigateToBlueprint();
    else if (action === "chapter" && chapterId) onJumpToChapter(chapterId);
    else if (action === "memory") onNavigateToMemory();
    else if (action === "reference_cards") onNavigateToReferenceCards();
    else if (action === "reference_candidates") {
      onNavigateToReferenceCardCandidates();
    } else if (action === "plot_threads") onNavigateToPlotThreads();
    else if (action === "generation_runs") onOpenGenerationRuns();
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

      {!audit.complete && groups.length > 0 && (
        <ul className="grid gap-2">
          {groups.map((group) => {
            const action = bookCompletionAction(group);
            const chapterId = group.chapterIds[0];
            const chapterNames = group.chapterIds
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
                    onClick={() => runAction(action, chapterId)}
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
