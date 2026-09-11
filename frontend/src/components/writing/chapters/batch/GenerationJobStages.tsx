"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { apiGet } from "@/lib/api";
import type { GenerationJob, GenerationJobPage, GenerationJobSummary } from "./batchTypes";
import { isRootGenerationJob } from "./generationRunsPresentation";
import { DiagnosticEventSummary } from "./GenerationDiagnosticsPanel";

const STAGES = new Set(["review", "state", "finalization", "book_audit", "completed", "blocked"]);

/** Stage runs are evidence under their root; all task controls stay on the root. */
export default function GenerationJobStages({ job, onOpenJob, onOpenRootJob = onOpenJob, onUpdateJob }: {
  job: GenerationJob;
  onOpenJob: (jobId: string) => void;
  onOpenRootJob?: (jobId: string) => void;
  onUpdateJob?: (job: GenerationJob) => void;
}) {
  const t = useTranslations("writing.generationRuns");
  const locale = useLocale();
  const [expanded, setExpanded] = useState(false);
  const [fetchedJob, setFetchedJob] = useState<GenerationJob | null>(null);
  const shownJob = fetchedJob?._id === job._id
    && Date.parse(fetchedJob.updated_at) >= Date.parse(job.updated_at) ? fetchedJob : job;
  const history = shownJob.stage_history ?? [];
  const terminal = ["completed", "aborted"].includes(shownJob.status);
  const time = (value: string | null | undefined) => value && Number.isFinite(Date.parse(value))
    ? new Intl.DateTimeFormat(locale, { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date(value)) : null;
  const [stages, setStages] = useState<GenerationJobSummary[] | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const cursorRef = useRef<string | null>(null);
  const loadedMoreRef = useRef(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const requestRef = useRef<AbortController | null>(null);
  useEffect(() => () => { requestRef.current?.abort(); }, []);
  const rootId = job.root_job_id ?? job.parent_job_id
    ?? job.required_book_successor_parent_job_id ?? job._id;
  const stageLabel = (stage: string | null | undefined) => t(
    STAGES.has(stage ?? "") ? `stages.${stage}` : "stages.unknown",
  );

  const load = useCallback(async (more = false, poll = false) => {
    if (requestRef.current) return;
    const controller = new AbortController();
    requestRef.current = controller;
    setBusy(true);
    setError(false);
    try {
      const params = new URLSearchParams({ limit: "20" });
      if (more && cursorRef.current) params.set("cursor", cursorRef.current);
      const [pageResult, detailResult] = await Promise.allSettled([
        apiGet<GenerationJobPage>(`/api/generation-jobs/${encodeURIComponent(rootId)}/children?${params}`, { signal: controller.signal }),
        more ? Promise.resolve(null) : apiGet<GenerationJob>(`/api/generation-jobs/${encodeURIComponent(job._id)}`, { signal: controller.signal }),
      ]);
      if (controller.signal.aborted) return;
      if (detailResult.status === "fulfilled" && detailResult.value?._id === job._id
        && detailResult.value.novel_id === job.novel_id) {
        setFetchedJob(detailResult.value);
        onUpdateJob?.(detailResult.value);
      }
      if (pageResult.status === "fulfilled") {
        const page = pageResult.value;
        const children = page.items.filter((item) => item.novel_id === job.novel_id
          && item.parent_job_id === rootId && item.root_job_id === rootId);
        if (more) loadedMoreRef.current = true;
        else if (!poll) loadedMoreRef.current = false;
        setStages((current) => {
          if (more) return [...current ?? [], ...children.filter((item) => !current?.some((old) => old._id === item._id))];
          if (poll && loadedMoreRef.current) return [...children, ...current ?? []].filter((item, index, all) => all.findIndex((other) => other._id === item._id) === index);
          return children;
        });
        if (!poll || !loadedMoreRef.current) {
          cursorRef.current = page.next_cursor;
          setCursor(page.next_cursor);
        }
      }
      if (pageResult.status === "rejected" || detailResult.status === "rejected") setError(true);
    } finally {
      if (requestRef.current === controller) {
        requestRef.current = null;
        if (!controller.signal.aborted) setBusy(false);
      }
    }
  }, [rootId, job._id, job.novel_id, onUpdateJob]);

  useEffect(() => {
    if (!expanded) return;
    void load();
    if (terminal) return;
    const timer = window.setInterval(() => { void load(false, true); }, 3000);
    return () => window.clearInterval(timer);
  }, [expanded, terminal, load]);

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
      <details data-testid="generation-stage-history" onToggle={(event) => setExpanded(event.currentTarget.open)}>
        <summary className="min-h-9 cursor-pointer py-2 font-medium text-foreground">{t("stageHistory")}</summary>
        <div className="grid min-w-0 gap-3 border-l border-border pl-3">
          {busy && <p role="status" className="text-muted">{t("loading")}</p>}
          {error && <p role="alert" className="text-red-700 dark:text-red-300">{t("loadError")}</p>}
          {history.length > 0 && <>
            <p className="text-muted">{t("stageRequestHint")}</p>
            {(shownJob.stage_history_total ?? 0) > history.length && <p className="text-muted">{t("stageHistoryLimit", { count: history.length, total: shownJob.stage_history_total ?? 0 })}</p>}
            <ol className="grid min-w-0 gap-3" aria-label={t("stageExecutionHistory")}>
              {history.map((event) => <li key={event.id} data-testid="generation-stage-event" className="min-w-0 border-b border-border pb-3">
                <div className="flex flex-wrap items-start justify-between gap-x-3 gap-y-1">
                  <p className="min-w-0 break-words font-medium text-foreground">
                    {event.order_index != null && <span>{t("stageChapter", { number: event.order_index })}{" · "}</span>}
                    {t(`executionStages.${event.stage}`)}
                    {event.retry_index != null && <span className="font-normal">{" · "}{t("stageRetry", { number: event.retry_index })}</span>}
                    {event.phase === "repair" && <span className="font-normal">{" · "}{t("stageCorrection")}</span>}
                  </p>
                  <span className="text-muted">{t(`executionStatuses.${event.status}`)}</span>
                </div>
                <p className="flex flex-wrap gap-x-3 text-muted tabular-nums">
                  {time(event.started_at) && <time dateTime={event.started_at ?? undefined}>{time(event.started_at)}{event.finished_at && event.finished_at !== event.started_at ? ` – ${time(event.finished_at)}` : ""}</time>}
                  {event.tokens != null && <span>{t("stageTokens", { count: event.tokens })}</span>}
                </p>
              </li>)}
            </ol>
          </>}
          {stages?.length === 0 && history.length === 0 && !busy && <p className="text-muted">{t("stageHistoryEmpty")}</p>}
          {Boolean(stages?.length) && <p className="font-medium text-foreground">{t("stageSubtasks")}</p>}
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
