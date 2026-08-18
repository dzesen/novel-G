"use client";

import { useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { apiGet } from "@/lib/api";
import { referenceCleanupForDisplay } from "../../generationMetadataPresentation";
import {
  auditCategoryTone,
  buildStateAuditQuery,
  stateAuditIssueId,
  visibleAuditChapters,
} from "./stateAuditPresentation";

interface StateAuditChapter {
  chapter_id: string;
  volume_id: string;
  volume_title: string;
  order_index: number;
  title: string;
  has_content: boolean;
  has_summary: boolean;
  category: string;
  repair_recommended: boolean;
  completion: {
    status: string;
    prose_eligible: boolean;
    completion_reason?: string | null;
    reference_resolution?: {
      dropped?: Record<string, string[]>;
    };
  };
}

interface StateAuditReport {
  novel_id: string;
  scope: "book" | "volume";
  volume_id: string | null;
  chapter_count: number;
  issue_count: number;
  counts: Record<string, number>;
  repair_queue: string[];
  chapters: StateAuditChapter[];
  read_only: boolean;
}

interface StateCompletenessAuditPanelProps {
  novelId: string;
  selectedVolumeId: string | null;
  initialChapterId?: string;
  initialIssueId?: string;
  onIssueTargetValidation: (issueId: string, valid: boolean) => void;
  onLocate: (chapterId: string, repair: boolean) => void;
}

const toneClass = {
  neutral: "border-border bg-background text-muted",
  info: "border-blue-200 bg-blue-50 text-blue-800 dark:border-blue-900/70 dark:bg-blue-950/35 dark:text-blue-200",
  warning: "border-amber-200 bg-amber-50 text-amber-900 dark:border-amber-900/70 dark:bg-amber-950/35 dark:text-amber-200",
  danger: "border-red-200 bg-red-50 text-red-800 dark:border-red-900/70 dark:bg-red-950/35 dark:text-red-200",
} as const;

export default function StateCompletenessAuditPanel({
  novelId,
  selectedVolumeId,
  initialChapterId,
  initialIssueId,
  onIssueTargetValidation,
  onLocate,
}: StateCompletenessAuditPanelProps) {
  const t = useTranslations("stateAudit");
  const metadataT = useTranslations("writing.generationMetadata");
  const [scope, setScope] = useState<"book" | "volume">(
    selectedVolumeId ? "volume" : "book",
  );
  const [issuesOnly, setIssuesOnly] = useState(!initialChapterId);
  const [response, setResponse] = useState<{
    query: string;
    report: StateAuditReport;
  } | null>(null);
  const [requestError, setRequestError] = useState<{
    query: string;
    message: string;
  } | null>(null);
  const [requestRevision, setRequestRevision] = useState(0);
  const effectiveScope =
    scope === "volume" && !selectedVolumeId ? "book" : scope;
  const query = buildStateAuditQuery(
    novelId,
    effectiveScope,
    selectedVolumeId,
  );
  const requestKey = `${query}::${requestRevision}`;
  const report = response?.query === requestKey ? response.report : null;
  const error = requestError?.query === requestKey ? requestError.message : "";
  const loading = report === null && !error;

  useEffect(() => {
    let cancelled = false;
    void apiGet<StateAuditReport>(query)
      .then((next) => {
        if (!cancelled) {
          setResponse({ query: requestKey, report: next });
          setRequestError(null);
        }
      })
      .catch((reason) => {
        if (!cancelled) {
          setRequestError({
            query: requestKey,
            message: reason instanceof Error ? reason.message : String(reason),
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [query, requestKey]);

  const visible = useMemo(
    () => visibleAuditChapters(report?.chapters ?? [], issuesOnly),
    [issuesOnly, report?.chapters],
  );
  const legacyCount = report?.counts.unknown_legacy ?? 0;

  useEffect(() => {
    if (!report || !initialIssueId) return;
    const matched = report.chapters.find(
      (chapter) =>
        stateAuditIssueId(chapter) === initialIssueId &&
        (!initialChapterId || chapter.chapter_id === initialChapterId),
    );
    onIssueTargetValidation(initialIssueId, Boolean(matched));
    if (matched) {
      document
        .getElementById(`state-issue-${matched.chapter_id}`)
        ?.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }, [initialChapterId, initialIssueId, onIssueTargetValidation, report]);

  useEffect(() => {
    if (!report || !initialChapterId || initialIssueId) return;
    document
      .getElementById(`state-issue-${initialChapterId}`)
      ?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [initialChapterId, initialIssueId, report]);

  return (
    <section className="flex h-full min-h-0 min-w-0 flex-col overflow-hidden bg-background">
        <header className="border-b border-border bg-surface px-4 py-4 sm:px-6">
          <h2 className="text-lg font-semibold text-foreground">
            {t("title")}
          </h2>
          <p className="mt-1 max-w-[72ch] text-sm leading-6 text-muted">
            {t("description")}
          </p>
          <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-xs text-muted">
            <span>
              {t("summaryChapters")}: <strong className="tabular-nums text-foreground">{report?.chapter_count ?? "—"}</strong>
            </span>
            <span>
              {t("summaryIssues")}: <strong className="tabular-nums text-amber-700 dark:text-amber-300">{report?.issue_count ?? "—"}</strong>
            </span>
            <span>
              {t("summaryRepair")}: <strong className="tabular-nums text-accent">{report?.repair_queue.length ?? "—"}</strong>
            </span>
          </div>
        </header>

        <div className="flex flex-wrap items-center gap-2 border-b border-border bg-surface-secondary/35 px-4 py-3 sm:px-6">
            <div className="inline-flex rounded-lg border border-border bg-surface p-0.5">
              <button
                type="button"
                onClick={() => setScope("book")}
                className={`rounded-md px-3 py-1.5 text-xs font-medium ${
                  effectiveScope === "book"
                    ? "bg-accent text-white"
                    : "text-muted hover:text-foreground"
                }`}
              >
                {t("scopeBook")}
              </button>
              <button
                type="button"
                onClick={() => setScope("volume")}
                disabled={!selectedVolumeId}
                className={`rounded-md px-3 py-1.5 text-xs font-medium disabled:opacity-40 ${
                  effectiveScope === "volume"
                    ? "bg-accent text-white"
                    : "text-muted hover:text-foreground"
                }`}
              >
                {t("scopeVolume")}
              </button>
            </div>
            <label className="ml-auto flex items-center gap-2 text-xs text-muted">
              <input
                type="checkbox"
                checked={issuesOnly}
                onChange={(event) => setIssuesOnly(event.target.checked)}
                className="h-4 w-4 accent-[var(--accent)]"
              />
              {t("issuesOnly")}
            </label>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4 sm:px-6">
          <div className="mb-3 rounded-lg border border-blue-200 bg-blue-50 px-3 py-2 text-xs leading-5 text-blue-800 dark:border-blue-900/70 dark:bg-blue-950/35 dark:text-blue-200">
            {t("readOnlyNotice")}
            {legacyCount > 0 && (
              <span className="ml-1">{t("legacyNotice", { count: legacyCount })}</span>
            )}
          </div>

          {error && (
            <div role="alert" className="flex flex-wrap items-center gap-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900/70 dark:bg-red-950/35 dark:text-red-200">
              <span className="min-w-0 flex-1 break-words">{error}</span>
              <button
                type="button"
                onClick={() => setRequestRevision((value) => value + 1)}
                className="min-h-9 rounded border border-red-300 px-3 font-medium hover:bg-red-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-500 dark:border-red-800 dark:hover:bg-red-950/60"
              >
                {t("retry")}
              </button>
            </div>
          )}
          {loading && (
            <div className="grid gap-2" aria-label={t("loading")}>
              {[0, 1, 2, 3].map((item) => (
                <div key={item} className="h-20 animate-pulse rounded-lg bg-border/35" />
              ))}
            </div>
          )}
          {!loading && !error && visible.length === 0 && (
            <div className="flex min-h-48 flex-col items-center justify-center text-center">
              <p className="text-sm font-medium text-foreground">{t("emptyTitle")}</p>
              <p className="mt-1 text-xs text-muted">{t("emptyDescription")}</p>
            </div>
          )}
          {!loading && !error && visible.length > 0 && (
            <div className="grid gap-2">
              {visible.map((chapter) => {
                const tone = auditCategoryTone(chapter.category);
                const issueId = stateAuditIssueId(chapter);
                const selected = initialIssueId
                  ? issueId === initialIssueId
                  : initialChapterId === chapter.chapter_id;
                const dropped = referenceCleanupForDisplay(
                  chapter.completion.reference_resolution?.dropped,
                );
                return (
                  <article
                    id={`state-issue-${chapter.chapter_id}`}
                    key={chapter.chapter_id}
                    className={`flex min-w-0 flex-col gap-3 rounded-lg border bg-surface px-4 py-3 sm:flex-row sm:items-center ${
                      selected
                        ? "border-accent ring-2 ring-accent/20"
                        : "border-border"
                    }`}
                  >
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm font-medium text-foreground">
                        <span className="mr-2 text-xs font-normal text-muted">
                          {chapter.volume_title} · {t("chapterOrder", { order: chapter.order_index })}
                        </span>
                        {chapter.title}
                      </p>
                      <div className="mt-1.5 flex flex-wrap items-center gap-2">
                        <span className={`rounded-md border px-2 py-0.5 text-[11px] font-medium ${toneClass[tone]}`}>
                          {t(`categories.${chapter.category}`)}
                        </span>
                        {dropped.length > 0 && (
                          <span className="max-w-full truncate text-[11px] text-muted">
                            {t("droppedReferences", {
                              summary: dropped.map((group) => metadataT(
                                "referenceCleanupCount",
                                {
                                  count: group.count,
                                  kind: metadataT(`referenceKinds.${group.kind}`),
                                },
                              )).join(metadataT("listSeparator")),
                            })}
                          </span>
                        )}
                      </div>
                    </div>
                    <button
                      type="button"
                      onClick={() =>
                        onLocate(chapter.chapter_id, chapter.repair_recommended)
                      }
                      className="min-h-10 shrink-0 rounded-lg border border-border px-3 py-1.5 text-xs font-medium text-foreground hover:border-accent hover:text-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                    >
                      {chapter.repair_recommended ? t("repair") : t("locate")}
                    </button>
                  </article>
                );
              })}
            </div>
          )}
        </div>
      </section>
  );
}
