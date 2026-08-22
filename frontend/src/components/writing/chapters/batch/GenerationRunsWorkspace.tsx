"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { ApiError, apiGet, apiPost } from "@/lib/api";
import type { WritingTargetKey } from "@/lib/writingRoute";
import type { ChapterSummary, VolumeSummary } from "@/types/novel";
import {
  type GenerationDiagnostic,
  type GenerationJob,
  type GenerationRunsNavigationTarget,
  isActive,
  isResumable,
  isTerminal,
} from "./batchTypes";
import { DiagnosticEventSummary } from "./GenerationDiagnosticsPanel";
import ResumeJobDialog, { isResumeReadinessRequired } from "./ResumeJobDialog";
import {
  finishReasonTranslationKey,
  proseReasonTranslationKey,
} from "../prose/prosePresentation";
import {
  diagnosticReasonTranslationKey,
  jobPauseReasonTranslationKey,
} from "./generationReasonPresentation";
import {
  aggregateChapterProgress,
  currentJobReasonCode,
  diagnosticHistoryState,
  newestDiagnostics,
} from "./generationRunsPresentation";

interface ProseRunTelemetry {
  run_id: string;
  novel_id: string;
  chapter_id: string;
  generation_job_id: string | null;
  revision: number;
  status: string;
  provider: { alias: string; model: string };
  plan: {
    mode: string;
    requested_word_count: number;
    scene_count: number;
    scheduled_base_call_count: number;
    protocol_revision: string;
  };
  completion: {
    status: string;
    requested_word_count: number;
    actual_word_count: number;
    scene_count: number;
    completed_scene_count: number;
    finish_reason: string;
    reason_codes: string[];
  };
  scene_progress: Array<{
    scene_index: number;
    status: string;
    base_calls_used: number;
    automatic_continuations_used: number;
    manual_continuations_used: number;
    word_count: number;
    raw_word_count: number;
    effective_word_count: number;
    replayed_characters_total: number;
    scene_target_words: number;
    converge_attempts: number;
    converge_attempts_without_stop: number;
    continues_truncated_output_count: number;
    max_cross_call_repeat_characters: number;
    pause_reason: string | null;
    last_prompt_mode: string | null;
    last_finish_reason: string;
    consecutive_no_progress: number;
  }>;
  usage: {
    provider_attempt_count: number;
    tokens_used: number;
    tokens_reserved: number;
    token_budget: number | null;
  };
  authorization: {
    content_identity: string;
    authorization_revision: number;
    automatic_continuations_per_scene: number;
    continuation_target_words: number;
    max_base_calls: number;
    max_automatic_continuation_calls: number;
    max_logical_prose_calls: number;
    conservative_token_bound: number;
  };
  has_uncertain_attempt: boolean;
  continuation_exhausted: boolean;
  created_at: string;
  updated_at: string;
}

interface GenerationRunsWorkspaceProps {
  novelId: string;
  chapters: ChapterSummary[];
  chaptersLoading: boolean;
  chaptersError: string;
  onRetryChapters: () => void;
  volumes: VolumeSummary[];
  target: GenerationRunsNavigationTarget;
  proseRunsRevision: number;
  onTargetValidation: (
    key: WritingTargetKey,
    value: string,
    valid: boolean,
  ) => void;
  onNavigate: (target: GenerationRunsNavigationTarget) => void;
  onClose: () => void;
  onOpenReadiness: (job: GenerationJob) => void;
  onJumpToChapter: (chapterId: string, runId?: string) => void;
  readOnly?: boolean;
}

type ScopeFilter = "all" | "chapter" | "book" | "volume";
type TimeFilter = "all" | "day" | "week" | "month";

function eventLocator(
  jobId: string,
  event: GenerationDiagnostic,
  index: number,
): string {
  if (event.event_id) return event.event_id;
  return [
    jobId,
    event.occurred_at ?? "",
    event.chapter_id ?? "",
    event.step,
    event.code,
    String(index),
  ].map(encodeURIComponent).join(".");
}

function dateWithin(value: string, range: TimeFilter): boolean {
  if (range === "all") return true;
  const parsed = new Date(value).getTime();
  if (Number.isNaN(parsed)) return false;
  const now = Date.now();
  const duration = range === "day"
    ? 24 * 60 * 60 * 1000
    : range === "week"
      ? 7 * 24 * 60 * 60 * 1000
      : 31 * 24 * 60 * 60 * 1000;
  return parsed >= now - duration;
}

function providerValues(job: GenerationJob): string[] {
  return Array.from(new Set([
    ...(job.diagnostics ?? []).flatMap(
      (event) => event.details.provider_aliases ?? [],
    ),
    job.readiness?.planning.prose_strategy?.provider_alias,
  ].filter((value): value is string => Boolean(value))));
}

function modelValues(job: GenerationJob): string[] {
  return Array.from(new Set([
    ...(job.diagnostics ?? []).flatMap(
      (event) => event.details.provider_models ?? [],
    ),
    job.readiness?.planning.prose_strategy?.provider_model,
  ].filter((value): value is string => Boolean(value))));
}

function telemetryReasonValues(run: ProseRunTelemetry): string[] {
  return Array.from(new Set([
    ...run.completion.reason_codes,
    run.completion.finish_reason,
    ...run.scene_progress.map((scene) => scene.pause_reason).filter(
      (reason): reason is string => Boolean(reason),
    ),
    ...run.scene_progress.map((scene) => scene.last_finish_reason).filter(Boolean),
  ].filter(Boolean)));
}

function telemetryMatchesScope(scope: ScopeFilter): boolean {
  return scope === "all" || scope === "chapter";
}

function reasonValues(job: GenerationJob): string[] {
  return Array.from(new Set(
    [
      ...(job.diagnostics ?? []).map((event) => event.code),
      job.pause_reason,
    ].filter((reason): reason is string => Boolean(reason)),
  ));
}

function statusLabel(
  status: string,
  t: ReturnType<typeof useTranslations>,
): string {
  const key = {
    active: "statusRunning",
    complete: "statusCompleted",
    incomplete: "statusIncomplete",
    stale: "statusStale",
    superseded: "statusSuperseded",
    discarded: "statusDiscarded",
  }[status] ?? `status${status[0].toUpperCase()}${status.slice(1)}`;
  return t(key);
}

function scopeLabel(
  job: GenerationJob,
  volumes: VolumeSummary[],
  t: ReturnType<typeof useTranslations>,
): string {
  if (job.scope === "book") return t("scopeBook");
  return t("scopeVolume", {
    title: volumes.find((volume) => volume._id === job.volume_id)?.title ?? t("unknown"),
  });
}

function formatDate(
  value: string | undefined,
  locale: string,
  unknown: string,
): string {
  if (!value) return unknown;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return unknown;
  return new Intl.DateTimeFormat(locale, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(parsed);
}

function JobActionButtons({
  job,
  busy,
  abortArmed,
  onPause,
  onResume,
  onRetryUncertain,
  onSkipUncertain,
  onAbort,
  t,
}: {
  job: GenerationJob;
  busy: boolean;
  abortArmed: boolean;
  onPause: () => void;
  onResume: () => void;
  onRetryUncertain: () => void;
  onSkipUncertain: () => void;
  onAbort: () => void;
  t: ReturnType<typeof useTranslations>;
}) {
  if (isTerminal(job.status)) return null;
  return (
    <div className="flex flex-wrap gap-2">
      {isActive(job.status) && (
        <button
          type="button"
          onClick={onPause}
          disabled={busy}
          className="min-h-9 rounded-md border border-border px-3 py-2 text-xs font-medium text-foreground hover:bg-surface disabled:cursor-wait disabled:opacity-60"
        >
          {t("pause")}
        </button>
      )}
      {isResumable(job.status) && (
        job.has_uncertain_attempts ? (
          <>
            <button
              type="button"
              onClick={onSkipUncertain}
              disabled={busy}
              className="min-h-9 rounded-md border border-border px-3 py-2 text-xs font-medium text-foreground hover:bg-surface disabled:cursor-wait disabled:opacity-60"
            >
              {t("uncertainSkip")}
            </button>
            <button
              type="button"
              onClick={onRetryUncertain}
              disabled={busy}
              className="min-h-9 rounded-md bg-accent px-3 py-2 text-xs font-medium text-white hover:bg-accent-hover disabled:cursor-wait disabled:opacity-60"
            >
              {t("uncertainRetry")}
            </button>
          </>
        ) : (
          <button
            type="button"
            onClick={onResume}
            disabled={busy}
            className="min-h-9 rounded-md bg-accent px-3 py-2 text-xs font-medium text-white hover:bg-accent-hover disabled:cursor-wait disabled:opacity-60"
          >
            {t("resume")}
          </button>
        )
      )}
      <button
        type="button"
        onClick={onAbort}
        disabled={busy}
        className="min-h-9 rounded-md border border-red-300 px-3 py-2 text-xs font-medium text-red-700 hover:bg-red-50 disabled:cursor-wait disabled:opacity-60 dark:border-red-900 dark:text-red-300 dark:hover:bg-red-950/30"
      >
        {abortArmed ? t("abortConfirm") : t("abort")}
      </button>
    </div>
  );
}

export default function GenerationRunsWorkspace({
  novelId,
  chapters,
  chaptersLoading,
  chaptersError,
  onRetryChapters,
  volumes,
  target,
  proseRunsRevision,
  onTargetValidation,
  onNavigate,
  onClose,
  onOpenReadiness,
  onJumpToChapter,
  readOnly = false,
}: GenerationRunsWorkspaceProps) {
  const t = useTranslations("writing.generationRuns");
  const tBatch = useTranslations("writing.batch");
  const tProse = useTranslations("writing.prose");
  const locale = useLocale();
  const headingRef = useRef<HTMLHeadingElement>(null);
  const loadRequestRef = useRef(0);
  const [jobs, setJobs] = useState<GenerationJob[]>([]);
  const [proseRuns, setProseRuns] = useState<ProseRunTelemetry[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [telemetryError, setTelemetryError] = useState<string | null>(null);
  const [exactRunLookup, setExactRunLookup] = useState<{
    requestedId: string | null;
    run: ProseRunTelemetry | null;
    loading: boolean;
    error: string | null;
  }>({ requestedId: null, run: null, loading: false, error: null });
  const [exactRunLookupRevision, setExactRunLookupRevision] = useState(0);
  const [scopeFilter, setScopeFilter] = useState<ScopeFilter>("all");
  const [statusFilter, setStatusFilter] = useState("all");
  const [providerFilter, setProviderFilter] = useState("all");
  const [modelFilter, setModelFilter] = useState("all");
  const [reasonFilter, setReasonFilter] = useState("all");
  const [timeFilter, setTimeFilter] = useState<TimeFilter>("all");
  const [actionJobId, setActionJobId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [abortArmed, setAbortArmed] = useState<string | null>(null);
  const [resumeReviewJob, setResumeReviewJob] = useState<GenerationJob | null>(null);

  const pauseReasonLabel = (reasonCode: string) => {
    const diagnosticKey = diagnosticReasonTranslationKey(reasonCode);
    if (diagnosticKey) return tBatch(diagnosticKey);
    const pauseKey = jobPauseReasonTranslationKey(reasonCode);
    if (pauseKey) return tBatch(pauseKey);
    const key = proseReasonTranslationKey(reasonCode);
    if (key) return tProse(`reasons.${key}`);
    return tProse(`reasons.${finishReasonTranslationKey(reasonCode)}`);
  };

  const load = useCallback(async (initial = false) => {
    const requestId = ++loadRequestRef.current;
    if (initial) setLoading(true);
    else setRefreshing(true);
    setLoadError(null);
    setTelemetryError(null);

    const telemetryParams = new URLSearchParams({ limit: "100" });
    if (target.jobId) telemetryParams.set("job_id", target.jobId);
    const [jobsResult, telemetryResult] = await Promise.allSettled([
      apiGet<GenerationJob[]>(`/api/generation-jobs/novel/${novelId}`),
      apiGet<ProseRunTelemetry[]>(
        `/api/llm/prose-runs/novel/${novelId}/telemetry?${telemetryParams}`,
      ),
    ]);
    if (requestId !== loadRequestRef.current) return;

    if (jobsResult.status === "fulfilled") {
      setJobs(jobsResult.value);
    } else {
      setLoadError(t("loadError"));
    }
    if (telemetryResult.status === "fulfilled") {
      setProseRuns(telemetryResult.value);
    } else {
      setTelemetryError(t("telemetryLoadError"));
    }
    setLoading(false);
    setRefreshing(false);
  }, [novelId, t, target.jobId]);

  useEffect(() => {
    void load(true);
  }, [load, proseRunsRevision]);

  useEffect(() => {
    const runId = target.runId;
    if (!runId) {
      setExactRunLookup({
        requestedId: null,
        run: null,
        loading: false,
        error: null,
      });
      return;
    }

    let cancelled = false;
    setExactRunLookup({
      requestedId: runId,
      run: null,
      loading: true,
      error: null,
    });
    void apiGet<ProseRunTelemetry>(
      `/api/llm/prose-runs/${encodeURIComponent(runId)}/telemetry`,
    ).then((run) => {
      if (cancelled) return;
      const relatedRunIds = target.jobId
        ? jobs.find((job) => job._id === target.jobId)?.related_prose_run_ids ?? []
        : [];
      const valid = run.run_id === runId
        && run.novel_id === novelId
        && (!target.chapterId || run.chapter_id === target.chapterId)
        && (
          !target.jobId
          || run.generation_job_id === target.jobId
          || relatedRunIds.includes(runId)
        );
      onTargetValidation("run", runId, valid);
      setExactRunLookup({
        requestedId: runId,
        run: valid ? run : null,
        loading: false,
        error: null,
      });
    }).catch((error: unknown) => {
      if (cancelled) return;
      if (error instanceof ApiError && [400, 404].includes(error.status)) {
        onTargetValidation("run", runId, false);
        setExactRunLookup({
          requestedId: runId,
          run: null,
          loading: false,
          error: null,
        });
        return;
      }
      setExactRunLookup({
        requestedId: runId,
        run: null,
        loading: false,
        error: t("telemetryLoadError"),
      });
    });
    return () => { cancelled = true; };
  }, [
    exactRunLookupRevision,
    jobs,
    novelId,
    onTargetValidation,
    t,
    target.chapterId,
    target.jobId,
    target.runId,
  ]);

  useEffect(() => {
    headingRef.current?.focus();
  }, [target.chapterId, target.eventId, target.jobId, target.runId]);

  const providerOptions = useMemo(() => Array.from(new Set([
    ...jobs.flatMap(providerValues),
    ...proseRuns.map((run) => run.provider.alias).filter(Boolean),
  ])).sort(), [jobs, proseRuns]);
  const modelOptions = useMemo(() => Array.from(new Set([
    ...jobs.flatMap(modelValues),
    ...proseRuns.map((run) => run.provider.model).filter(Boolean),
  ])).sort(), [jobs, proseRuns]);
  const reasonOptions = useMemo(() => Array.from(new Set([
    ...jobs.flatMap(reasonValues),
    ...proseRuns.flatMap(telemetryReasonValues),
  ])).sort(), [jobs, proseRuns]);
  const statusOptions = useMemo(() => Array.from(new Set([
    ...jobs.map((job) => job.status),
    ...proseRuns.map((run) => run.status),
  ])).sort(), [jobs, proseRuns]);
  const filteredJobs = useMemo(() => jobs.filter((job) => (
    (scopeFilter === "all" || job.scope === scopeFilter)
    && (statusFilter === "all" || job.status === statusFilter)
    && (providerFilter === "all" || providerValues(job).includes(providerFilter))
    && (modelFilter === "all" || modelValues(job).includes(modelFilter))
    && (reasonFilter === "all" || reasonValues(job).includes(reasonFilter))
    && dateWithin(job.updated_at, timeFilter)
  )), [
    jobs,
    modelFilter,
    providerFilter,
    reasonFilter,
    scopeFilter,
    statusFilter,
    timeFilter,
  ]);
  const filteredTelemetry = useMemo(() => proseRuns.filter((run) => (
    telemetryMatchesScope(scopeFilter)
    && (statusFilter === "all" || run.status === statusFilter)
    && (providerFilter === "all" || run.provider.alias === providerFilter)
    && (modelFilter === "all" || run.provider.model === modelFilter)
    && (reasonFilter === "all" || telemetryReasonValues(run).includes(reasonFilter))
    && dateWithin(run.updated_at, timeFilter)
  )), [
    modelFilter,
    providerFilter,
    proseRuns,
    reasonFilter,
    scopeFilter,
    statusFilter,
    timeFilter,
  ]);
  const selectedJob = target.jobId
    ? jobs.find((job) => job._id === target.jobId) ?? null
    : null;
  const selectedChapterProgress = useMemo(
    () => aggregateChapterProgress(selectedJob?.progress ?? []),
    [selectedJob],
  );
  const orderedDiagnostics = useMemo(
    () => newestDiagnostics(selectedJob?.diagnostics),
    [selectedJob],
  );
  const selectedChapter = target.chapterId
    ? chapters.find((chapter) => chapter._id === target.chapterId) ?? null
    : null;
  const exactRun = target.runId
    && exactRunLookup.requestedId === target.runId
    ? exactRunLookup.run
    : null;
  const selectedRun = target.runId
    ? exactRun ?? proseRuns.find((run) => run.run_id === target.runId) ?? null
    : null;
  const selectedTelemetry = target.runId
    ? selectedRun
      ? [selectedRun]
      : []
    : target.chapterId
      ? filteredTelemetry.filter((run) => run.chapter_id === target.chapterId)
      : filteredTelemetry.slice(0, target.jobId ? 100 : 12);
  const telemetryFilteredOut = !target.runId && Boolean(target.chapterId)
    && proseRuns.some((run) => run.chapter_id === target.chapterId)
    && selectedTelemetry.length === 0;
  const selectedEvent = useMemo(() => {
    if (!selectedJob || !target.eventId) return null;
    return (selectedJob.diagnostics ?? []).find(
      (event, index) => eventLocator(selectedJob._id, event, index) === target.eventId,
    ) ?? null;
  }, [selectedJob, target.eventId]);
  const currentBlocker = selectedJob
    && ["failed", "interrupted", "paused"].includes(selectedJob.status)
    ? orderedDiagnostics[0]?.event ?? null
    : null;
  const currentBlockerIndex = currentBlocker && selectedJob
    ? (selectedJob.diagnostics ?? []).indexOf(currentBlocker)
    : -1;
  const currentBlockerLocator = currentBlocker && selectedJob
    ? eventLocator(
        selectedJob._id,
        currentBlocker,
        Math.max(0, currentBlockerIndex),
      )
    : null;
  const currentBlockerRunId = typeof currentBlocker?.details.prose_run_id === "string"
    ? currentBlocker.details.prose_run_id
    : null;
  const missingJob = Boolean(target.jobId)
    && !loading
    && !loadError
    && selectedJob === null;

  useEffect(() => {
    if (!target.jobId || loading || loadError) return;
    onTargetValidation("job", target.jobId, selectedJob !== null);
  }, [loadError, loading, onTargetValidation, selectedJob, target.jobId]);

  useEffect(() => {
    if (!target.chapterId || chaptersLoading || chaptersError) return;
    onTargetValidation("chapter", target.chapterId, selectedChapter !== null);
  }, [
    chaptersError,
    chaptersLoading,
    onTargetValidation,
    selectedChapter,
    target.chapterId,
  ]);

  useEffect(() => {
    if (!target.eventId || loading || loadError || !selectedJob) return;
    onTargetValidation("event", target.eventId, selectedEvent !== null);
  }, [
    loadError,
    loading,
    onTargetValidation,
    selectedEvent,
    selectedJob,
    target.eventId,
  ]);

  const control = useCallback(async (
    job: GenerationJob,
    action: "pause" | "resume" | "abort",
    body: Record<string, boolean> = {},
  ) => {
    setActionJobId(job._id);
    setActionError(null);
    try {
      const next = await apiPost<GenerationJob>(
        `/api/generation-jobs/${job._id}/${action}`,
        body,
      );
      setJobs((current) => current.map((item) => (
        item._id === next._id ? next : item
      )));
      setAbortArmed(null);
      void load();
    } catch (error) {
      if (action === "resume" && isResumeReadinessRequired(error)) {
        setResumeReviewJob(job);
        return;
      }
      setActionError(
        t("actionError", {
          message: error instanceof Error ? error.message : String(error),
        }),
      );
    } finally {
      setActionJobId(null);
    }
  }, [load, t]);

  const requestResume = useCallback((job: GenerationJob) => {
    if (
      job.pause_reason === "cost_cap"
      || job.pause_reason === "authorization_scope_increased"
    ) {
      setResumeReviewJob(job);
      return;
    }
    void control(job, "resume");
  }, [control]);

  const requestAbort = (job: GenerationJob) => {
    if (abortArmed !== job._id) {
      setAbortArmed(job._id);
      return;
    }
    void control(job, "abort");
  };

  const exactRunPending = Boolean(target.runId)
    && (
      exactRunLookup.requestedId !== target.runId
      || exactRunLookup.loading
    );
  const displayedTelemetryError = target.runId
    ? exactRunLookup.requestedId === target.runId
      ? exactRunLookup.error
      : null
    : telemetryError;
  const refresh = useCallback(() => {
    void load();
    if (target.runId) {
      setExactRunLookupRevision((current) => current + 1);
    }
  }, [load, target.runId]);

  return (
    <section className="flex h-full min-h-0 flex-col bg-surface" aria-labelledby="generation-runs-title">
      {resumeReviewJob && (
        <ResumeJobDialog
          job={resumeReviewJob}
          onClose={() => setResumeReviewJob(null)}
          onSubmitted={(resumed) => {
            setJobs((current) => current.map((item) => (
              item._id === resumed._id ? resumed : item
            )));
            setResumeReviewJob(null);
            void load();
          }}
        />
      )}
      <header className="shrink-0 border-b border-border px-4 py-4 sm:px-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h2
              id="generation-runs-title"
              ref={headingRef}
              tabIndex={-1}
              className="text-base font-semibold text-foreground outline-none"
            >
              {t("title")}
            </h2>
            <p className="mt-1 max-w-3xl text-xs leading-5 text-muted">
              {t("description")}
            </p>
          </div>
          <div className="flex shrink-0 flex-wrap gap-2">
            <button
              type="button"
              onClick={refresh}
              disabled={loading || refreshing}
              className="min-h-9 rounded-md border border-border px-3 py-2 text-xs font-medium text-foreground hover:bg-surface-secondary disabled:cursor-wait disabled:opacity-60"
            >
              {refreshing ? t("refreshing") : t("refresh")}
            </button>
            <button
              type="button"
              onClick={onClose}
              className="min-h-9 rounded-md border border-border px-3 py-2 text-xs font-medium text-foreground hover:bg-surface-secondary"
            >
              {t("backToEditor")}
            </button>
          </div>
        </div>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4 sm:px-5">
        {loadError && (
          <section role="alert" className="mb-4 rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-800 dark:border-red-900/70 dark:bg-red-950/30 dark:text-red-200">
            <p>{loadError}</p>
            <button
              type="button"
              onClick={() => void load()}
              className="mt-2 text-xs font-medium underline underline-offset-2"
            >
              {t("retry")}
            </button>
          </section>
        )}

        {chaptersError && (
          <section role="alert" className="mb-4 rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-800 dark:border-red-900/70 dark:bg-red-950/30 dark:text-red-200">
            <p>{chaptersError}</p>
            <button
              type="button"
              onClick={onRetryChapters}
              className="mt-2 text-xs font-medium underline underline-offset-2"
            >
              {t("retry")}
            </button>
          </section>
        )}

        <section aria-labelledby="generation-run-filters-title" className="mb-4 rounded-md border border-border bg-background p-3">
          <h3 id="generation-run-filters-title" className="text-sm font-semibold text-foreground">
            {t("filtersTitle")}
          </h3>
          <div className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-6">
            <label className="grid gap-1 text-xs text-muted">
              <span>{t("filterScope")}</span>
              <select
                value={scopeFilter}
                onChange={(event) => setScopeFilter(event.target.value as ScopeFilter)}
                className="min-h-9 min-w-0 rounded-md border border-border bg-surface px-2 text-sm text-foreground"
              >
                <option value="all">{t("allScopes")}</option>
                <option value="chapter">{t("scopeChapter")}</option>
                <option value="book">{t("scopeBook")}</option>
                <option value="volume">{t("scopeVolumeFilter")}</option>
              </select>
            </label>
            <label className="grid gap-1 text-xs text-muted">
              <span>{t("filterStatus")}</span>
              <select
                value={statusFilter}
                onChange={(event) => setStatusFilter(event.target.value)}
                className="min-h-9 min-w-0 rounded-md border border-border bg-surface px-2 text-sm text-foreground"
              >
                <option value="all">{t("allStatuses")}</option>
                {statusOptions.map((status) => (
                  <option key={status} value={status}>{statusLabel(status, t)}</option>
                ))}
              </select>
            </label>
            <label className="grid gap-1 text-xs text-muted">
              <span>{t("filterProvider")}</span>
              <select
                value={providerFilter}
                onChange={(event) => setProviderFilter(event.target.value)}
                className="min-h-9 min-w-0 rounded-md border border-border bg-surface px-2 text-sm text-foreground"
              >
                <option value="all">{t("allProviders")}</option>
                {providerOptions.map((provider) => (
                  <option key={provider} value={provider}>{provider}</option>
                ))}
              </select>
            </label>
            <label className="grid gap-1 text-xs text-muted">
              <span>{t("filterModel")}</span>
              <select
                value={modelFilter}
                onChange={(event) => setModelFilter(event.target.value)}
                className="min-h-9 min-w-0 rounded-md border border-border bg-surface px-2 text-sm text-foreground"
              >
                <option value="all">{t("allModels")}</option>
                {modelOptions.map((model) => (
                  <option key={model} value={model}>{model}</option>
                ))}
              </select>
            </label>
            <label className="grid gap-1 text-xs text-muted">
              <span>{t("filterReason")}</span>
              <select
                value={reasonFilter}
                onChange={(event) => setReasonFilter(event.target.value)}
                className="min-h-9 min-w-0 rounded-md border border-border bg-surface px-2 text-sm text-foreground"
              >
                <option value="all">{t("allReasons")}</option>
                {reasonOptions.map((reason) => (
                  <option key={reason} value={reason}>{pauseReasonLabel(reason)}</option>
                ))}
              </select>
            </label>
            <label className="grid gap-1 text-xs text-muted">
              <span>{t("filterTime")}</span>
              <select
                value={timeFilter}
                onChange={(event) => setTimeFilter(event.target.value as TimeFilter)}
                className="min-h-9 min-w-0 rounded-md border border-border bg-surface px-2 text-sm text-foreground"
              >
                <option value="all">{t("timeAll")}</option>
                <option value="day">{t("timeDay")}</option>
                <option value="week">{t("timeWeek")}</option>
                <option value="month">{t("timeMonth")}</option>
              </select>
            </label>
          </div>
        </section>

        <div className="grid min-h-0 gap-4 lg:grid-cols-[minmax(17rem,0.85fr)_minmax(0,1.75fr)]">
          <section
            aria-label={t("jobsListAria")}
            className="min-w-0 rounded-md border border-border bg-background"
          >
            <div className="border-b border-border px-3 py-2.5">
              <h3 id="generation-run-list-title" className="text-sm font-semibold text-foreground">
                {t("jobsTitle", { count: filteredJobs.length })}
              </h3>
            </div>
            <div className="max-h-[32rem] divide-y divide-border overflow-y-auto lg:max-h-[calc(100vh-20rem)]">
              {loading && (
                <p role="status" className="px-3 py-4 text-sm text-muted">{t("loading")}</p>
              )}
              {!loading && filteredJobs.length === 0 && (
                <p className="px-3 py-4 text-sm leading-6 text-muted">{t("jobsEmpty")}</p>
              )}
              {filteredJobs.map((job) => {
                const isSelected = job._id === target.jobId;
                const currentReason = currentJobReasonCode(job);
                return (
                  <button
                    key={job._id}
                    type="button"
                    onClick={() => onNavigate({ jobId: job._id })}
                    className={`grid w-full gap-1 px-3 py-3 text-left hover:bg-surface-secondary focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-accent ${
                      isSelected ? "bg-accent/10" : ""
                    }`}
                  >
                    <span className="flex min-w-0 items-center justify-between gap-2">
                      <span className="truncate text-sm font-medium text-foreground">
                        {scopeLabel(job, volumes, t)}
                      </span>
                      <span className="shrink-0 text-[11px] text-muted">
                        {statusLabel(job.status, t)}
                      </span>
                    </span>
                    <span className="truncate text-xs text-muted">
                      {formatDate(job.updated_at, locale, t("unknown"))}
                    </span>
                    {currentReason && (
                      <span className="truncate text-[11px] text-amber-800 dark:text-amber-200">
                        {pauseReasonLabel(currentReason)}
                      </span>
                    )}
                  </button>
                );
              })}
            </div>
          </section>

          <section aria-labelledby="generation-run-detail-title" className="min-w-0 rounded-md border border-border bg-background">
            {!selectedJob && !missingJob && (
              <div className="px-4 py-8 text-sm leading-6 text-muted">
                <h3 id="generation-run-detail-title" className="font-semibold text-foreground">
                  {t("detailTitle")}
                </h3>
                <p className="mt-2">{t("detailSelect")}</p>
              </div>
            )}
            {selectedJob && (
              <div className="grid gap-4 px-4 py-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <h3 id="generation-run-detail-title" className="break-words text-base font-semibold text-foreground">
                      {scopeLabel(selectedJob, volumes, t)}
                    </h3>
                    <p className="mt-1 text-xs text-muted">
                      {t("detailStatus", { status: statusLabel(selectedJob.status, t) })}
                    </p>
                  </div>
                  {!readOnly && (
                    <JobActionButtons
                      job={selectedJob}
                      busy={actionJobId === selectedJob._id}
                      abortArmed={abortArmed === selectedJob._id}
                      onPause={() => void control(selectedJob, "pause")}
                      onResume={() => requestResume(selectedJob)}
                      onRetryUncertain={() => void control(selectedJob, "resume", { confirm_uncertain_retry: true })}
                      onSkipUncertain={() => void control(selectedJob, "resume", { skip_uncertain: true })}
                      onAbort={() => requestAbort(selectedJob)}
                      t={tBatch}
                    />
                  )}
                </div>

                {actionError && (
                  <p role="alert" className="rounded-md border border-red-300 bg-red-50 px-3 py-2 text-xs leading-5 text-red-800 dark:border-red-900/70 dark:bg-red-950/30 dark:text-red-200">
                    {actionError}
                  </p>
                )}

                {currentBlocker && (
                  <section
                    aria-labelledby="generation-run-current-blocker-title"
                    className="rounded-md border border-amber-300 bg-amber-50/70 p-3 dark:border-amber-900/70 dark:bg-amber-950/20"
                  >
                    <h4
                      id="generation-run-current-blocker-title"
                      className="text-sm font-semibold text-foreground"
                    >
                      {t("currentBlockerTitle")}
                    </h4>
                    <div className="mt-2">
                      <DiagnosticEventSummary event={currentBlocker} />
                    </div>
                    <div className="mt-3 flex flex-wrap gap-2">
                      {currentBlocker.chapter_id && (
                        <button
                          type="button"
                          onClick={() => onJumpToChapter(
                            currentBlocker.chapter_id,
                            currentBlockerRunId ?? undefined,
                          )}
                          className="min-h-9 rounded-md border border-border bg-background px-3 py-2 text-xs font-medium text-foreground hover:bg-surface-secondary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
                        >
                          {t("resolveOpenChapter")}
                        </button>
                      )}
                      {currentBlockerRunId && currentBlockerLocator && (
                        <button
                          type="button"
                          onClick={() => onNavigate({
                            jobId: selectedJob._id,
                            chapterId: currentBlocker.chapter_id || undefined,
                            eventId: currentBlockerLocator,
                            runId: currentBlockerRunId,
                          })}
                          className="min-h-9 rounded-md border border-border bg-background px-3 py-2 text-xs font-medium text-foreground hover:bg-surface-secondary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
                        >
                          {t("resolveInspectRun")}
                        </button>
                      )}
                      {currentBlocker.action_codes?.includes("restart_generation_job") && (
                        <button
                          type="button"
                          onClick={() => onOpenReadiness(selectedJob)}
                          className="min-h-9 rounded-md bg-accent px-3 py-2 text-xs font-medium text-white hover:bg-accent-hover focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
                        >
                          {t("resolveReturnToReadiness")}
                        </button>
                      )}
                    </div>
                  </section>
                )}

                <dl className="grid gap-3 text-xs sm:grid-cols-2 xl:grid-cols-3">
                  <div className="min-w-0 rounded-md border border-border bg-surface p-3">
                    <dt className="text-muted">{t("detailCreated")}</dt>
                    <dd className="mt-1 break-words text-foreground">{formatDate(selectedJob.created_at, locale, t("unknown"))}</dd>
                  </div>
                  <div className="min-w-0 rounded-md border border-border bg-surface p-3">
                    <dt className="text-muted">{t("detailUpdated")}</dt>
                    <dd className="mt-1 break-words text-foreground">{formatDate(selectedJob.updated_at, locale, t("unknown"))}</dd>
                  </div>
                  <div className="min-w-0 rounded-md border border-border bg-surface p-3">
                    <dt className="text-muted">{t("detailCalls")}</dt>
                    <dd className="mt-1 break-words text-foreground">
                      {t("detailCallsValue", {
                        used: selectedJob.usage_attempt_claimed,
                        total: selectedJob.usage_attempt_capacity,
                      })}
                    </dd>
                  </div>
                  <div className="min-w-0 rounded-md border border-border bg-surface p-3">
                    <dt className="text-muted">{t("detailTokens")}</dt>
                    <dd className="mt-1 break-words text-foreground">
                      {t("detailTokensValue", {
                        used: selectedJob.tokens_used,
                        reserved: selectedJob.tokens_reserved ?? 0,
                        remaining: selectedJob.token_budget == null
                          ? t("unlimited")
                          : Math.max(0, selectedJob.token_budget - selectedJob.tokens_used - (selectedJob.tokens_reserved ?? 0)),
                      })}
                    </dd>
                  </div>
                  <div className="min-w-0 rounded-md border border-border bg-surface p-3">
                    <dt className="text-muted">{t("detailPause")}</dt>
                    <dd className="mt-1 break-words text-foreground">
                      {selectedJob.pause_reason
                        ? pauseReasonLabel(selectedJob.pause_reason)
                        : t("none")}
                    </dd>
                  </div>
                  <div className="min-w-0 rounded-md border border-border bg-surface p-3">
                    <dt className="text-muted">{t("detailCurrentChapter")}</dt>
                    <dd className="mt-1 truncate text-foreground">
                      {chapters.find((chapter) => chapter._id === selectedJob.current_chapter_id)?.title ?? t("none")}
                    </dd>
                  </div>
                </dl>

                {selectedJob.prose_continuation_authorization && (
                  <section aria-labelledby="generation-run-authorization-title" className="rounded-md border border-border bg-surface p-3">
                    <h4 id="generation-run-authorization-title" className="text-sm font-semibold text-foreground">
                      {t("authorizationTitle")}
                    </h4>
                    <div className="mt-2 grid gap-1 text-xs leading-5 text-muted">
                      <p>{t("authorizationCalls", {
                        base: selectedJob.prose_continuation_authorization.max_base_calls,
                        automatic: selectedJob.prose_continuation_authorization.max_automatic_continuation_calls,
                        total: selectedJob.prose_continuation_authorization.max_logical_prose_calls,
                      })}</p>
                      <p>{t("authorizationBudget", {
                        bound: selectedJob.prose_continuation_authorization.conservative_token_bound,
                        budget: selectedJob.prose_continuation_authorization.token_budget ?? t("unlimited"),
                      })}</p>
                      <p>{t("authorizationRevision", {
                        revision: selectedJob.prose_continuation_authorization.authorization_revision,
                      })}</p>
                    </div>
                  </section>
                )}

                <section aria-labelledby="generation-run-chapters-title">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <h4 id="generation-run-chapters-title" className="text-sm font-semibold text-foreground">
                      {t("chaptersTitle")}
                    </h4>
                    <p className="text-xs text-muted">{t("chaptersDescription")}</p>
                  </div>
                  <div className="mt-2 grid gap-2">
                    {selectedChapterProgress.length === 0 && (
                      <p className="rounded-md border border-dashed border-border px-3 py-3 text-xs text-muted">
                        {currentBlocker?.step === "candidate_pipeline"
                          ? t("chaptersEmptyCandidate")
                          : t("chaptersEmpty")}
                      </p>
                    )}
                    {selectedChapterProgress.map((progress) => {
                      const chapter = chapters.find((item) => item._id === progress.chapter_id);
                      const sceneCount = progress.incomplete_prose?.scene_count ?? 0;
                      const completedSceneCount = Math.min(
                        sceneCount,
                        progress.incomplete_prose?.completed_scene_count ?? 0,
                      );
                      const scenePercent = sceneCount > 0
                        ? Math.round((completedSceneCount / sceneCount) * 100)
                        : null;
                      return (
                        <div key={progress.chapter_id} className="grid gap-2 rounded-md border border-border p-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
                          <button
                            type="button"
                            onClick={() => onNavigate({
                              jobId: selectedJob._id,
                              chapterId: progress.chapter_id,
                            })}
                            className="min-w-0 text-left"
                          >
                            <span className="block truncate text-sm font-medium text-foreground hover:text-accent">
                              {chapter?.title ?? t("unknownChapter")}
                            </span>
                            <span className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs leading-5 text-muted">
                              {scenePercent !== null && (
                                <span>{t("chapterSceneProgress", {
                                  completed: completedSceneCount,
                                  total: sceneCount,
                                  percent: scenePercent,
                                })}</span>
                              )}
                              <span>{t("chapterStepProgress", {
                                completed: progress.completedStepCount,
                                total: 3,
                                percent: progress.stepPercent,
                              })}</span>
                              <span>{t("chapterTokensCumulative", {
                                tokens: progress.tokens,
                              })}</span>
                            </span>
                          </button>
                          <button
                            type="button"
                            onClick={() => onJumpToChapter(progress.chapter_id)}
                            className="justify-self-start text-xs font-medium text-accent hover:underline sm:justify-self-end"
                          >
                            {t("openChapter")}
                          </button>
                        </div>
                      );
                    })}
                  </div>
                </section>

                <section aria-labelledby="generation-run-events-title">
                  <h4 id="generation-run-events-title" className="text-sm font-semibold text-foreground">
                    {t("eventsTitle")}
                  </h4>
                  <div className="mt-2 rounded-md border border-border">
                    {orderedDiagnostics.length === 0 && (
                      <p className="px-3 py-3 text-xs text-muted">{t("eventsEmpty")}</p>
                    )}
                    {orderedDiagnostics.length > 0 && (
                      <ol className="divide-y divide-border">
                        {orderedDiagnostics.map(({
                          event,
                          originalIndex,
                        }, orderedIndex) => {
                          const locator = eventLocator(
                            selectedJob._id,
                            event,
                            originalIndex,
                          );
                          const historyState = diagnosticHistoryState(
                            selectedJob,
                            orderedIndex,
                          );
                          return (
                            <li key={locator}>
                              <button
                                type="button"
                                onClick={() => onNavigate({
                                  jobId: selectedJob._id,
                                  chapterId: event.chapter_id || undefined,
                                  eventId: locator,
                                  runId: typeof event.details.prose_run_id === "string"
                                    ? event.details.prose_run_id
                                    : undefined,
                                })}
                                className={`block w-full px-3 py-3 text-left hover:bg-surface-secondary ${
                                  target.eventId === locator ? "bg-accent/10" : ""
                                }`}
                              >
                                <p className="mb-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] leading-4 text-muted">
                                  <time dateTime={event.occurred_at}>
                                    {formatDate(
                                      event.occurred_at,
                                      locale,
                                      t("eventTimeUnknown"),
                                    )}
                                  </time>
                                  <span aria-hidden="true">·</span>
                                  <span>{t(`eventStates.${historyState}`)}</span>
                                </p>
                                <DiagnosticEventSummary event={event} />
                              </button>
                            </li>
                          );
                        })}
                      </ol>
                    )}
                  </div>
                  {selectedEvent && (
                    <p className="mt-2 text-xs leading-5 text-muted">{t("eventLocated")}</p>
                  )}
                </section>
              </div>
            )}
          </section>
        </div>

        <section aria-labelledby="generation-run-telemetry-title" className="mt-4 rounded-md border border-border bg-background p-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <h3 id="generation-run-telemetry-title" className="text-sm font-semibold text-foreground">
                {t("telemetryTitle")}
              </h3>
              <p className="mt-1 text-xs leading-5 text-muted">
                {target.jobId
                  ? target.chapterId && selectedChapter
                    ? t("telemetryJobChapterDescription", { title: selectedChapter.title })
                    : t("telemetryJobDescription")
                  : target.chapterId && selectedChapter
                    ? t("telemetryChapterDescription", { title: selectedChapter.title })
                    : t("telemetryDescription")}
              </p>
            </div>
            {target.chapterId && (
              <button
                type="button"
                onClick={() =>
                  onNavigate({ jobId: target.jobId, runId: target.runId })
                }
                className="shrink-0 text-xs font-medium text-accent hover:underline"
              >
                {target.jobId ? t("clearJobChapter") : t("clearChapter")}
              </button>
            )}
          </div>
          {displayedTelemetryError && (
            <p role="alert" className="mt-3 text-xs leading-5 text-amber-800 dark:text-amber-200">
              {displayedTelemetryError}
            </p>
          )}
          {telemetryFilteredOut && (
            <p className="mt-3 text-xs leading-5 text-muted">
              {t("telemetryFilteredOut")}
            </p>
          )}
          {!displayedTelemetryError && !exactRunPending && selectedTelemetry.length === 0 && (
            <p className="mt-3 text-xs text-muted">{t("telemetryEmpty")}</p>
          )}
          <div className="mt-3 grid gap-3 lg:grid-cols-2">
            {selectedTelemetry.map((run) => (
              <article
                key={run.run_id}
                className={[
                  "min-w-0 rounded-md border bg-surface p-3",
                  target.runId === run.run_id
                    ? "border-accent ring-1 ring-accent/30"
                    : "border-border",
                ].join(" ")}
              >
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div className="min-w-0">
                    <h4 className="truncate text-sm font-medium text-foreground">
                      {chapters.find((chapter) => chapter._id === run.chapter_id)?.title ?? t("unknownChapter")}
                    </h4>
                    <p className="mt-1 truncate text-xs text-muted">
                      {t("telemetryProvider", {
                        provider: run.provider.alias || t("unknown"),
                        model: run.provider.model || t("unknown"),
                      })}
                    </p>
                  </div>
                  <span className="shrink-0 text-xs text-muted">{statusLabel(run.status, t)}</span>
                </div>
                <dl className="mt-3 grid gap-2 text-xs leading-5 text-muted sm:grid-cols-2">
                  <div>
                    <dt>{t("telemetryCalls")}</dt>
                    <dd className="text-foreground">{t("telemetryCallsValue", {
                      base: run.authorization.max_base_calls,
                      automatic: run.authorization.max_automatic_continuation_calls,
                      total: run.authorization.max_logical_prose_calls,
                    })}</dd>
                  </div>
                  <div>
                    <dt>{t("telemetryTokens")}</dt>
                    <dd className="text-foreground">{t("telemetryTokensValue", {
                      used: run.usage.tokens_used,
                      reserved: run.usage.tokens_reserved,
                      budget: run.usage.token_budget ?? t("unlimited"),
                    })}</dd>
                  </div>
                  <div>
                    <dt>{t("telemetryIdentity")}</dt>
                    <dd className="break-all text-foreground">
                      {run.authorization.content_identity
                        ? t("telemetryIdentityValue")
                        : t("unknown")}
                    </dd>
                  </div>
                  <div>
                    <dt>{t("telemetryFinishReason")}</dt>
                    <dd className="text-foreground">
                      {pauseReasonLabel(run.completion.finish_reason)}
                    </dd>
                  </div>
                </dl>
                <div className="mt-3 grid gap-2 border-t border-border pt-3">
                  {run.scene_progress.map((scene) => (
                    <div key={scene.scene_index} className="grid gap-1 text-xs leading-5 text-muted sm:grid-cols-[auto_minmax(0,1fr)] sm:gap-x-3">
                      <span className="font-medium text-foreground">{t("sceneLabel", { index: scene.scene_index + 1 })}</span>
                      <span className="min-w-0 break-words">{t("sceneCalls", {
                        base: scene.base_calls_used,
                        automatic: scene.automatic_continuations_used,
                        manual: scene.manual_continuations_used,
                        words: scene.word_count,
                        status: statusLabel(scene.status, t),
                        remaining: Math.max(
                          0,
                          run.authorization.automatic_continuations_per_scene
                            - scene.automatic_continuations_used,
                        ),
                      })}</span>
                      {scene.scene_target_words > 0 ? (
                        <span className="min-w-0 break-words">
                          {t("sceneDivergenceMetrics", {
                            actual: scene.word_count,
                            target: scene.scene_target_words,
                            ratio: (scene.word_count / scene.scene_target_words).toFixed(2),
                            converge: scene.converge_attempts,
                            withoutStop: scene.converge_attempts_without_stop,
                          })}
                        </span>
                      ) : (
                        <span className="min-w-0 break-words">
                          {t("sceneDivergenceMetricsTargetUnknown", {
                            actual: scene.word_count,
                            converge: scene.converge_attempts,
                            withoutStop: scene.converge_attempts_without_stop,
                          })}
                        </span>
                      )}
                      <span className="min-w-0 break-words">
                        {t("sceneWordMetrics", {
                          raw: scene.raw_word_count,
                          effective: scene.effective_word_count,
                          replayed: scene.replayed_characters_total,
                        })}
                      </span>
                      <span className="min-w-0 break-words">
                        {t("sceneContinuityMetrics", {
                          repeat: scene.max_cross_call_repeat_characters,
                          truncated: scene.continues_truncated_output_count,
                        })}
                      </span>
                      {scene.pause_reason && (
                        <span className="min-w-0 break-words text-amber-800 dark:text-amber-200">
                          {t("scenePause", { reason: pauseReasonLabel(scene.pause_reason) })}
                        </span>
                      )}
                      {scene.last_finish_reason && (
                        <span className="min-w-0 break-words">
                          {t("sceneFinishReason", {
                            reason: pauseReasonLabel(scene.last_finish_reason),
                          })}
                        </span>
                      )}
                    </div>
                  ))}
                  {run.scene_progress.length === 0 && (
                    <p className="text-xs text-muted">{t("sceneEmpty")}</p>
                  )}
                </div>
              </article>
            ))}
          </div>
        </section>

      </div>
    </section>
  );
}
