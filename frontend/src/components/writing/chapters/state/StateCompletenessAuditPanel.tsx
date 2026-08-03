"use client";

import { useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { apiGet } from "@/lib/api";
import { referenceCleanupForDisplay } from "../../generationMetadataPresentation";
import {
  auditCategoryTone,
  buildStateAuditQuery,
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
  onClose: () => void;
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
  onClose,
  onLocate,
}: StateCompletenessAuditPanelProps) {
  const t = useTranslations("stateAudit");
  const metadataT = useTranslations("writing.generationMetadata");
  const [scope, setScope] = useState<"book" | "volume">(
    selectedVolumeId ? "volume" : "book",
  );
  const [issuesOnly, setIssuesOnly] = useState(true);
  const [response, setResponse] = useState<{
    query: string;
    report: StateAuditReport;
  } | null>(null);
  const [requestError, setRequestError] = useState<{
    query: string;
    message: string;
  } | null>(null);
  const effectiveScope =
    scope === "volume" && !selectedVolumeId ? "book" : scope;
  const query = buildStateAuditQuery(
    novelId,
    effectiveScope,
    selectedVolumeId,
  );
  const report = response?.query === query ? response.report : null;
  const error = requestError?.query === query ? requestError.message : "";
  const loading = report === null && !error;

  useEffect(() => {
    let cancelled = false;
    void apiGet<StateAuditReport>(query)
      .then((next) => {
        if (!cancelled) {
          setResponse({ query, report: next });
          setRequestError(null);
        }
      })
      .catch((reason) => {
        if (!cancelled) {
          setRequestError({
            query,
            message: reason instanceof Error ? reason.message : String(reason),
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [query]);

  const visible = useMemo(
    () => visibleAuditChapters(report?.chapters ?? [], issuesOnly),
    [issuesOnly, report?.chapters],
  );
  const legacyCount = report?.counts.unknown_legacy ?? 0;

  return (
    <div className="absolute inset-0 z-40 flex items-center justify-center bg-black/30 px-3 py-4 sm:px-6">
      <section className="flex max-h-full w-full max-w-6xl flex-col overflow-hidden rounded-xl border border-border bg-surface shadow-2xl">
        <header className="flex flex-col gap-4 border-b border-border px-5 py-4 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-accent">
              {t("eyebrow")}
            </p>
            <h2 className="mt-1 text-lg font-semibold text-foreground">
              {t("title")}
            </h2>
            <p className="mt-1 max-w-2xl text-xs leading-5 text-muted">
              {t("description")}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="self-start rounded-lg px-3 py-1.5 text-xs font-medium text-muted hover:bg-surface-secondary hover:text-foreground"
          >
            {t("close")}
          </button>
        </header>

        <div className="border-b border-border bg-surface-secondary/35 px-5 py-4">
          <div className="grid gap-3 sm:grid-cols-3">
            <div className="rounded-lg border border-border bg-surface px-3 py-2.5">
              <p className="text-[11px] text-muted">{t("summaryChapters")}</p>
              <p className="mt-1 text-xl font-semibold tabular-nums text-foreground">
                {report?.chapter_count ?? "—"}
              </p>
            </div>
            <div className="rounded-lg border border-border bg-surface px-3 py-2.5">
              <p className="text-[11px] text-muted">{t("summaryIssues")}</p>
              <p className="mt-1 text-xl font-semibold tabular-nums text-amber-700 dark:text-amber-300">
                {report?.issue_count ?? "—"}
              </p>
            </div>
            <div className="rounded-lg border border-border bg-surface px-3 py-2.5">
              <p className="text-[11px] text-muted">{t("summaryRepair")}</p>
              <p className="mt-1 text-xl font-semibold tabular-nums text-accent">
                {report?.repair_queue.length ?? "—"}
              </p>
            </div>
          </div>

          <div className="mt-3 flex flex-wrap items-center gap-2">
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
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          <div className="mb-3 rounded-lg border border-blue-200 bg-blue-50 px-3 py-2 text-xs leading-5 text-blue-800 dark:border-blue-900/70 dark:bg-blue-950/35 dark:text-blue-200">
            {t("readOnlyNotice")}
            {legacyCount > 0 && (
              <span className="ml-1">{t("legacyNotice", { count: legacyCount })}</span>
            )}
          </div>

          {error && (
            <p role="alert" className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900/70 dark:bg-red-950/35 dark:text-red-200">
              {error}
            </p>
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
                const dropped = referenceCleanupForDisplay(
                  chapter.completion.reference_resolution?.dropped,
                );
                return (
                  <article
                    key={chapter.chapter_id}
                    className="flex flex-col gap-3 rounded-lg border border-border bg-background px-4 py-3 sm:flex-row sm:items-center"
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
                      className="shrink-0 rounded-lg border border-border px-3 py-1.5 text-xs font-medium text-foreground hover:border-accent hover:text-accent"
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
    </div>
  );
}
