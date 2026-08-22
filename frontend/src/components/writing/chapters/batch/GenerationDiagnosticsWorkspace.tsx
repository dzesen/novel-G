"use client";

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { apiGet } from "@/lib/api";
import type {
  GenerationDiagnosticsSummary,
  GenerationRunsNavigationTarget,
} from "./batchTypes";
import GenerationDiagnosticsPanel from "./GenerationDiagnosticsPanel";

interface GenerationDiagnosticsWorkspaceProps {
  novelId: string;
  onOpenRecord: (target: GenerationRunsNavigationTarget) => void;
  onClose: () => void;
}

export default function GenerationDiagnosticsWorkspace({
  novelId,
  onOpenRecord,
  onClose,
}: GenerationDiagnosticsWorkspaceProps) {
  const t = useTranslations("writing.autoBook");
  const tBatch = useTranslations("writing.batch");
  const [summary, setSummary] = useState<GenerationDiagnosticsSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setSummary(await apiGet<GenerationDiagnosticsSummary>(
        `/api/generation-jobs/novel/${novelId}/diagnostics?limit=30`,
      ));
    } catch {
      setError(tBatch("diagnosticsLoadError"));
    } finally {
      setLoading(false);
    }
  }, [novelId, tBatch]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <main
      className="flex h-full min-h-0 flex-col bg-surface"
      aria-labelledby="auto-book-diagnostics-title"
    >
      <header className="shrink-0 border-b border-border px-4 py-4 sm:px-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="text-xs font-medium text-accent">{t("pages.diagnostics.eyebrow")}</p>
            <h2
              id="auto-book-diagnostics-title"
              className="mt-1 text-base font-semibold text-foreground"
            >
              {t("pages.diagnostics.title")}
            </h2>
            <p className="mt-1 max-w-3xl text-xs leading-5 text-warm-700 dark:text-muted">
              {t("pages.diagnostics.description")}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="min-h-9 shrink-0 rounded-md border border-border px-3 py-2 text-xs font-medium text-foreground hover:bg-surface-secondary"
          >
            {t("backToRuns")}
          </button>
        </div>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4 sm:px-5">
        {summary && (summary.unresolved_event_count ?? 0) > 0 && (
          <section
            aria-labelledby="diagnostics-unresolved-title"
            className="mb-4 rounded-md border border-amber-300 bg-amber-50 px-4 py-3 dark:border-amber-900/70 dark:bg-amber-950/30"
          >
            <h3 id="diagnostics-unresolved-title" className="text-sm font-semibold text-amber-900 dark:text-amber-100">
              {t("diagnosticsUnresolvedTitle", { count: summary.unresolved_event_count ?? 0 })}
            </h3>
            <p className="mt-1 text-xs leading-5 text-amber-800 dark:text-amber-200">
              {t("diagnosticsUnresolvedDescription")}
            </p>
          </section>
        )}

        <GenerationDiagnosticsPanel
          summary={summary}
          loading={loading}
          error={error}
          onRetry={() => void load()}
          onOpenEvent={(event) => onOpenRecord({
            jobId: event.job_id,
            chapterId: event.chapter_id || undefined,
            eventId: event.event_id,
          })}
          embedded
        />

        <p className="mt-4 max-w-3xl text-xs leading-5 text-warm-700 dark:text-muted">
          {t("diagnosticsPrivacyHint")}
        </p>
      </div>
    </main>
  );
}
