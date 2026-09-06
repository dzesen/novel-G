"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { apiGet } from "@/lib/api";
import type { GenerationJob, GenerationJobPage, GenerationJobSummary } from "./batchTypes";
import { isRootGenerationJob } from "./generationRunsPresentation";
import { DiagnosticEventSummary } from "./GenerationDiagnosticsPanel";

const STAGES = new Set(["review", "state", "finalization", "book_audit", "completed", "blocked"]);

/** Stage runs are evidence under their root; all task controls stay on the root. */
export default function GenerationJobStages({ job, onOpenJob, onOpenRootJob = onOpenJob }: {
  job: GenerationJob;
  onOpenJob: (jobId: string) => void;
  onOpenRootJob?: (jobId: string) => void;
}) {
  const t = useTranslations("writing.generationRuns");
  const [stages, setStages] = useState<GenerationJobSummary[] | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const requestRef = useRef<AbortController | null>(null);
  useEffect(() => () => { requestRef.current?.abort(); }, []);
  const rootId = job.root_job_id ?? job.parent_job_id
    ?? job.required_book_successor_parent_job_id ?? job._id;
  const stageLabel = (stage: string | null | undefined) => t(
    STAGES.has(stage ?? "") ? `stages.${stage}` : "stages.unknown",
  );

  async function load(more = false) {
    if (requestRef.current) return;
    const controller = new AbortController();
    requestRef.current = controller;
    setBusy(true);
    setError(false);
    try {
      const params = new URLSearchParams({ limit: "20" });
      if (more && cursor) params.set("cursor", cursor);
      const page = await apiGet<GenerationJobPage>(
        `/api/generation-jobs/${encodeURIComponent(rootId)}/children?${params}`,
        { signal: controller.signal },
      );
      if (controller.signal.aborted) return;
      const children = page.items.filter((item) => item.novel_id === job.novel_id
        && item.parent_job_id === rootId && item.root_job_id === rootId);
      setStages((current) => more
        ? [...current ?? [], ...children.filter((item) => !current?.some((old) => old._id === item._id))]
        : children);
      setCursor(page.next_cursor);
    } catch {
      if (!controller.signal.aborted) setError(true);
    } finally {
      if (requestRef.current === controller) {
        requestRef.current = null;
        if (!controller.signal.aborted) setBusy(false);
      }
    }
  }

  if (!isRootGenerationJob(job)) {
    return (
      <div className="grid gap-2 border-l-2 border-border pl-3 text-xs leading-5 text-muted">
        <p>{t("stageChildHint", { stage: stageLabel(job.current_stage) })}</p>
        <button type="button" onClick={() => onOpenRootJob(rootId)}
          className="min-h-9 w-fit text-foreground underline underline-offset-2">{t("openRootJob")}</button>
      </div>
    );
  }

  return (
    <div className="min-w-0 text-xs leading-5">
      {job.current_stage && <p className="mb-2 text-muted">{t("currentStage", { stage: stageLabel(job.current_stage) })}</p>}
      <details onToggle={(event) => { if (event.currentTarget.open && stages === null) void load(); }}>
        <summary className="min-h-9 cursor-pointer py-2 font-medium text-foreground">{t("stageHistory")}</summary>
        <div className="grid min-w-0 gap-3 border-l border-border pl-3">
          {busy && <p role="status" className="text-muted">{t("loading")}</p>}
          {error && <p role="alert" className="text-red-700 dark:text-red-300">{t("loadError")}</p>}
          {stages?.length === 0 && <p className="text-muted">{t("stageHistoryEmpty")}</p>}
          {stages?.map((stage) => (
            <article key={stage._id} className="min-w-0 border-b border-border pb-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="font-medium text-foreground">{stageLabel(stage.current_stage)}</p>
                <span className="text-muted">{t(`status${stage.status[0].toUpperCase()}${stage.status.slice(1)}`)}</span>
              </div>
              <p className="text-muted">{t("stageProgress", { count: stage.progress_chapter_count })}</p>
              {stage.latest_diagnostic && <DiagnosticEventSummary event={stage.latest_diagnostic} />}
              <button type="button" onClick={() => onOpenJob(stage._id)}
                className="min-h-9 text-foreground underline underline-offset-2">{t("openStageDetail")}</button>
            </article>
          ))}
          <div className="flex flex-wrap gap-3">
            <button type="button" disabled={busy} onClick={() => void load()}
              className="min-h-9 text-foreground underline underline-offset-2 disabled:opacity-60">{t("refresh")}</button>
            {cursor && <button type="button" disabled={busy} onClick={() => void load(true)}
              className="min-h-9 text-foreground underline underline-offset-2 disabled:opacity-60">{t("loadMoreHistory")}</button>}
          </div>
        </div>
      </details>
    </div>
  );
}
