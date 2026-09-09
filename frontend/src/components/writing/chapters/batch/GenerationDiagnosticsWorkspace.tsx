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
    <section
      className="auto-book-statistics flex h-full min-h-0 flex-col"
      aria-labelledby="auto-book-diagnostics-title"
    >
      <header className="auto-book-page-header shrink-0">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h2
              id="auto-book-diagnostics-title"
              className="text-xl font-semibold text-foreground sm:text-2xl"
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
              {t("backToRecords")}
          </button>
        </div>
      </header>

      <div className="auto-book-records-body min-h-0 flex-1 overflow-y-auto">
        {summary && (summary.unresolved_event_count ?? 0) > 0 && (
          <section
            aria-labelledby="diagnostics-unresolved-title"
            className="mb-6 border-b border-border pb-4"
          >
            <h3 id="diagnostics-unresolved-title" className="text-sm font-semibold text-foreground">
              {t("diagnosticsUnresolvedTitle", { count: summary.unresolved_event_count ?? 0 })}
            </h3>
            <p className="mt-1 text-xs leading-5 text-muted">
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
    </section>
  );
}
