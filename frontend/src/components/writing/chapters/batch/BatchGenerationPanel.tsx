"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@heroui/react";
import { ApiError, apiGet, apiPost } from "@/lib/api";
import type { ChapterSummary, VolumeSummary } from "@/types/novel";
import {
  type BookCompletionAudit,
  type GenerationJob,
  type GenerationRunsNavigationTarget,
  type LeftoverProseRun,
  isActive,
  isResumable,
  isTerminal,
  jobChapters,
} from "./batchTypes";
import { useGenerationJob } from "./useGenerationJob";
import StartJobDialog from "./StartJobDialog";
import ResumeJobDialog, { isResumeReadinessRequired } from "./ResumeJobDialog";
import CheckpointReview from "./CheckpointReview";
import LeftoverProseRuns from "./LeftoverProseRuns";
import type { ReferenceCardType } from "./referenceCardAutoCreation";
import ReferenceCardAutomationAuditPanel from "./ReferenceCardAutomationAuditPanel";
import BookCompletionAuditPanel from "./BookCompletionAuditPanel";
import {
  bookCompletionAuditMatchesJob,
  bookCompletionResult,
} from "./bookCompletionPresentation";
import {
  currentJobStatusByProseRun,
  requiresResumeReadinessReview,
  selectCurrentGenerationJob,
} from "./generationRunsPresentation";

const ABORT_DIALOG_FOCUSABLE_SELECTOR = [
  "button:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

type AbortIntent = "abort" | "successor";

interface BatchGenerationPanelProps {
  surface: "start" | "runs";
  novelId: string;
  initialJobId?: string;
  onJobTargetValidation: (jobId: string, valid: boolean) => void;
  selectedVolumeId: string | null;
  volumes: VolumeSummary[];
  chapters: ChapterSummary[];
  structureLoaded: boolean;
  startScope: "volume" | "book" | null;
  preferWorldAutoSupplement: boolean;
  onStartClose: () => void;
  onJumpToChapter: (chapterId: string) => void;
  onQuietRefresh: () => Promise<void>;
  onNavigateToMemory: () => void;
  onNavigateToBlueprint: () => void;
  onNavigateToWorldBaseline: () => void;
  onNavigateToReferenceCards: (
    cardType?: ReferenceCardType,
    cardId?: string,
  ) => void;
  onNavigateToReferenceCardCandidates: (candidateId?: string) => void;
  onNavigateToPlotThreads: () => void;
  proseRunsRevision: number;
  onOpenProseRun: (run: LeftoverProseRun) => void;
  onStartFreshProse: (chapterId: string) => void;
  onOpenSuccessorReadiness: (
    scope: "volume" | "book",
    volumeId?: string,
  ) => void;
  onOpenGenerationRuns: (target?: GenerationRunsNavigationTarget) => void;
  onJobStarted?: (job: GenerationJob) => void;
}

export default function BatchGenerationPanel({
  surface,
  novelId,
  initialJobId,
  onJobTargetValidation,
  selectedVolumeId,
  volumes,
  chapters,
  structureLoaded,
  startScope,
  preferWorldAutoSupplement,
  onStartClose,
  onJumpToChapter,
  onQuietRefresh,
  onNavigateToMemory,
  onNavigateToBlueprint,
  onNavigateToWorldBaseline,
  onNavigateToReferenceCards,
  onNavigateToReferenceCardCandidates,
  onNavigateToPlotThreads,
  proseRunsRevision,
  onOpenProseRun,
  onStartFreshProse,
  onOpenSuccessorReadiness,
  onOpenGenerationRuns,
  onJobStarted,
}: BatchGenerationPanelProps) {
  const t = useTranslations("writing.batch");
  const { job, error: pollError, setJob } = useGenerationJob({ onProgress: onQuietRefresh });
  const [controlBusy, setControlBusy] = useState(false);
  const [controlError, setControlError] = useState<string | null>(null);
  const [abortIntent, setAbortIntent] = useState<AbortIntent | null>(null);
  const abortDialogRef = useRef<HTMLDivElement>(null);
  const abortCancelRef = useRef<HTMLButtonElement>(null);
  const abortTriggerRef = useRef<HTMLElement | null>(null);
  const [resumeReviewOpen, setResumeReviewOpen] = useState(false);
  const [dismissed, setDismissed] = useState<string | null>(null); // 已关闭的终态作业 id
  const [jobLookupError, setJobLookupError] = useState("");
  const [jobLookupRevision, setJobLookupRevision] = useState(0);
  const [currentBookAudit, setCurrentBookAudit] =
    useState<BookCompletionAudit | null>(null);
  const [currentBookAuditError, setCurrentBookAuditError] = useState("");
  const [currentBookAuditRevision, setCurrentBookAuditRevision] = useState(0);
  const currentJobHidden = Boolean(
    job
    && isTerminal(job.status)
    && (
      dismissed === job._id
      || (job.status === "aborted" && !initialJobId)
    ),
  );
  const jobStatusByProseRun = useMemo(
    () => currentJobStatusByProseRun(job && !currentJobHidden ? [job] : []),
    [currentJobHidden, job],
  );


  // 精确 job 深链优先；URL 未指定 job 时只检查最新作业，避免越过已结束作业复活旧任务。
  useEffect(() => {
    let cancelled = false;
    setJob(null);
    setDismissed(null);
    setJobLookupError("");
    if (surface === "start") {
      return () => { cancelled = true; };
    }
    void (async () => {
      try {
        if (initialJobId) {
          const requested = await apiGet<GenerationJob>(
            `/api/generation-jobs/${encodeURIComponent(initialJobId)}`,
          );
          if (cancelled) return;
          const valid = requested._id === initialJobId
            && requested.novel_id === novelId;
          onJobTargetValidation(initialJobId, valid);
          if (valid) setJob(requested);
          return;
        }
        const jobs = await apiGet<GenerationJob[]>(`/api/generation-jobs/novel/${novelId}`);
        if (cancelled) return;
        const current = selectCurrentGenerationJob(jobs);
        if (current) setJob(current);
      } catch (reason) {
        if (cancelled) return;
        if (
          initialJobId
          && reason instanceof ApiError
          && [400, 404].includes(reason.status)
        ) {
          onJobTargetValidation(initialJobId, false);
        } else {
          setJobLookupError(
            reason instanceof Error ? reason.message : t("jobLookupFailed"),
          );
        }
      }
    })();
    return () => { cancelled = true; };
  }, [
    initialJobId,
    jobLookupRevision,
    novelId,
    onJobTargetValidation,
    setJob,
    surface,
    t,
  ]);

  // A persisted completion audit is historical evidence. Completed Jobs must
  // refresh the read-only audit before the UI may still call the book complete.
  useEffect(() => {
    let cancelled = false;
    setCurrentBookAudit(null);
    setCurrentBookAuditError("");
    if (job?.scope !== "book" || job.status !== "completed") {
      return () => { cancelled = true; };
    }
    void apiGet<BookCompletionAudit>(
      `/api/generation-jobs/book/${encodeURIComponent(novelId)}`
      + `/completion-audit?job_id=${encodeURIComponent(job._id)}`,
    ).then((audit) => {
      if (!bookCompletionAuditMatchesJob(audit, job._id)) {
        throw new Error(t("bookAuditJobMismatch"));
      }
      if (!cancelled) setCurrentBookAudit(audit);
    }).catch((reason) => {
      if (!cancelled) {
        setCurrentBookAuditError(
          reason instanceof Error ? reason.message : t("bookAuditRefreshFailed"),
        );
      }
    });
    return () => { cancelled = true; };
  }, [
    currentBookAuditRevision,
    job?._id,
    job?.scope,
    job?.status,
    novelId,
    t,
  ]);

  const chapterById = useMemo(() => new Map(chapters.map((c) => [c._id, c])), [chapters]);
  const titleForChapter = (id: string) => chapterById.get(id)?.title ?? id;

  const control = async (
    action: "pause" | "resume" | "abort",
    body: Record<string, boolean> = {},
  ): Promise<boolean> => {
    if (!job) return false;
    setControlBusy(true);
    setControlError(null);
    try {
      const next = await apiPost<GenerationJob>(`/api/generation-jobs/${job._id}/${action}`, body);
      setJob(next);
      return true;
    } catch (err) {
      if (action === "resume" && isResumeReadinessRequired(err)) {
        setResumeReviewOpen(true);
        return false;
      }
      // resume 可能 409（别处有在跑作业）；原样展示（设计 §7.4）。
      setControlError(err instanceof Error ? err.message : String(err));
      return false;
    } finally {
      setControlBusy(false);
    }
  };

  const openAbortConfirmation = useCallback((intent: AbortIntent) => {
    abortTriggerRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    setAbortIntent(intent);
  }, []);

  const dismissAbortConfirmation = useCallback((restoreFocus: boolean) => {
    setAbortIntent(null);
    if (!restoreFocus) return;
    window.requestAnimationFrame(() => {
      if (abortTriggerRef.current?.isConnected) abortTriggerRef.current.focus();
    });
  }, []);

  const closeAbortConfirmation = useCallback(() => {
    if (controlBusy) return;
    dismissAbortConfirmation(true);
  }, [controlBusy, dismissAbortConfirmation]);

  useEffect(() => {
    if (!abortIntent) return;
    abortCancelRef.current?.focus();
    const containFocus = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !controlBusy) {
        event.preventDefault();
        closeAbortConfirmation();
        return;
      }
      if (event.key !== "Tab") return;
      const dialog = abortDialogRef.current;
      if (!dialog) return;
      const focusable = Array.from(
        dialog.querySelectorAll<HTMLElement>(ABORT_DIALOG_FOCUSABLE_SELECTOR),
      ).filter((element) => element.getClientRects().length > 0);
      if (focusable.length === 0) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;
      if (event.shiftKey && (active === first || !dialog.contains(active))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (active === last || !dialog.contains(active))) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", containFocus);
    return () => window.removeEventListener("keydown", containFocus);
  }, [abortIntent, closeAbortConfirmation, controlBusy]);

  const requestResume = () => {
    if (!job) return;
    if (requiresResumeReadinessReview(job)) {
      setResumeReviewOpen(true);
      return;
    }
    void control("resume");
  };

  const confirmAbort = () => {
    if (!job || !abortIntent) return;
    const intent = abortIntent;
    const abortedJobId = job._id;
    const successorScope = job.scope;
    const successorVolumeId = job.volume_id ?? undefined;
    void control("abort").then((succeeded) => {
      if (!succeeded) return;
      if (intent === "abort") setDismissed(abortedJobId);
      dismissAbortConfirmation(intent === "abort");
      if (intent === "successor") {
        onOpenSuccessorReadiness(successorScope, successorVolumeId);
      }
    });
  };

  // accepted state delta 不在章节列表响应里；真实工作量由 readiness 报告决定。
  // 这里仅传范围内章节上限，避免再把 summary 误当作状态完成证明。
  const selectedVolume = volumes.find((v) => v._id === selectedVolumeId) ?? null;
  const selectedVolumeChapters = chapters.filter((c) => c.volume_id === selectedVolumeId);
  const fillableCount = selectedVolumeChapters.length;
  const bookFillableCount = chapters.length;

  const dialog =
    startScope === "volume" && selectedVolumeId ? (
      <StartJobDialog
        scope="volume"
        targetId={selectedVolumeId}
        title={t("dialogTitle")}
        targetHeading={t("dialogVolumeLabel")}
        targetLabel={selectedVolume?.title ?? ""}
        fillableCount={fillableCount}
        preferWorldAutoSupplement={preferWorldAutoSupplement}
        onReadinessLoaded={onQuietRefresh}
        requiresStructureInitialization={false}
        onClose={onStartClose}
        onNavigateToReferenceCards={() => onNavigateToReferenceCards()}
        onNavigateToWorldBaseline={onNavigateToWorldBaseline}
        onNavigateToBookStructure={onNavigateToBlueprint}
        onSubmitted={(started) => {
          setDismissed(null);
          setJob(started);
          onStartClose();
          onJobStarted?.(started);
        }}
      />
    ) : startScope === "book" ? (
      <StartJobDialog
        scope="book"
        targetId={novelId}
        title={t("dialogTitleBook")}
        targetHeading={t("dialogBookLabel")}
        targetLabel={t("dialogBookTarget")}
        fillableCount={bookFillableCount}
        preferWorldAutoSupplement={preferWorldAutoSupplement}
        onReadinessLoaded={onQuietRefresh}
        requiresStructureInitialization={
          structureLoaded && volumes.length === 0 && chapters.length === 0
        }
        onClose={onStartClose}
        onNavigateToReferenceCards={() => onNavigateToReferenceCards()}
        onNavigateToWorldBaseline={onNavigateToWorldBaseline}
        onNavigateToBookStructure={onNavigateToBlueprint}
        onStructureInitialized={async () => {
          await onQuietRefresh();
        }}
        onSubmitted={(started) => {
          setDismissed(null);
          setJob(started);
          onStartClose();
          onJobStarted?.(started);
        }}
      />
    ) : null;

  if (surface === "start") {
    return dialog;
  }
  const resumeDialog = resumeReviewOpen && job ? (
    <ResumeJobDialog
      job={job}
      onClose={() => setResumeReviewOpen(false)}
      onSubmitted={(resumed) => {
        setJob(resumed);
        setResumeReviewOpen(false);
      }}
    />
  ) : null;

  const generationRunsEntry = (
    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border bg-surface px-4 py-2.5">
      <p className="min-w-0 text-xs leading-5 text-muted">
        {!job || currentJobHidden
          ? t("generationRunsNoCurrent")
          : job.diagnostics?.length || job.error || job.pause_reason === "incomplete_scene"
          ? t("generationRunsExceptionEntry")
          : t("generationRunsEntryDescription")}
      </p>
      <button
        type="button"
        onClick={() => onOpenGenerationRuns(
          job
            ? {
                jobId: job._id,
                chapterId: job.current_chapter_id ?? job.error?.chapter_id ?? undefined,
              }
            : {},
        )}
        className="shrink-0 text-xs font-medium text-accent hover:underline"
      >
        {t("generationRunsOpen")}
      </button>
    </div>
  );
  const jobLookupAlert = jobLookupError ? (
    <div role="alert" className="m-4 flex flex-wrap items-center justify-between gap-3 rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-800 dark:border-red-900/70 dark:bg-red-950/30 dark:text-red-200">
      <span className="min-w-0">{jobLookupError}</span>
      <button
        type="button"
        onClick={() => setJobLookupRevision((current) => current + 1)}
        className="shrink-0 text-xs font-medium underline underline-offset-2"
      >
        {t("retry")}
      </button>
    </div>
  ) : null;

  const leftoverPanel = (
    <LeftoverProseRuns
      key={novelId}
      novelId={novelId}
      chapters={chapters}
      refreshKey={[
        proseRunsRevision,
        job?.status ?? "none",
        job?.error?.chapter_id ?? "none",
      ].join(":")}
      jobStatusByRun={jobStatusByProseRun}
      onOpenRun={onOpenProseRun}
      onStartFresh={onStartFreshProse}
    />
  );

  // 残留草稿是小说级状态，即使没有作业或终态条已关闭也必须常驻发现。
  if (!job || currentJobHidden) {
    return (
      <>
        {dialog}
        {resumeDialog}
        {jobLookupAlert}
        {leftoverPanel}
        {generationRunsEntry}
      </>
    );
  }

  const volumeChapters = jobChapters(job, chapters);
  const total = volumeChapters.length;
  const complete = volumeChapters.filter((c) => c.word_count > 0 && c.summary.trim()).length;
  const jobScopeLabel = job.scope === "book"
    ? t("progressBook")
    : t("progressVolume", { title: volumes.find((v) => v._id === job.volume_id)?.title ?? "" });
  const currentChapter = job.current_chapter_id ? chapterById.get(job.current_chapter_id) : undefined;
  const processedChapterCount = new Set(
    job.progress.map((entry) => entry.chapter_id),
  ).size;
  const boundCurrentBookAudit = currentBookAudit
    && bookCompletionAuditMatchesJob(currentBookAudit, job._id)
    ? currentBookAudit
    : null;
  const displayedCompletionAudit = job.scope === "book"
    && job.status === "completed"
    ? boundCurrentBookAudit
    : job.completion_audit ?? null;
  const completionResult = bookCompletionResult(job, boundCurrentBookAudit);
  const showCompletionAudit = Boolean(
    displayedCompletionAudit
    && job.scope === "book"
    && (job.status === "completed" || job.pause_reason === "final_audit"),
  );

  return (
    <>
      {dialog}
      {resumeDialog}
      {jobLookupAlert}
      {leftoverPanel}
      {!isResumable(job.status) && generationRunsEntry}

      <div className="shrink-0 border-b border-border">
        {job.scope === "book"
          && job.status === "completed"
          && !boundCurrentBookAudit
          && (
            <div
              role={currentBookAuditError ? "alert" : "status"}
              className="flex min-w-0 flex-wrap items-center justify-between gap-3 border-b border-border bg-surface-secondary px-4 py-3 text-xs text-muted"
            >
              <span className="min-w-0 break-words">
                {currentBookAuditError || t("bookAuditRefreshing")}
              </span>
              {currentBookAuditError && (
                <button
                  type="button"
                  onClick={() => setCurrentBookAuditRevision((value) => value + 1)}
                  className="shrink-0 font-medium text-accent hover:underline"
                >
                  {t("retry")}
                </button>
              )}
            </div>
          )}
        {showCompletionAudit && displayedCompletionAudit && (
          <BookCompletionAuditPanel
            audit={displayedCompletionAudit}
            titleForChapter={titleForChapter}
            onJumpToChapter={onJumpToChapter}
            onNavigateToBlueprint={onNavigateToBlueprint}
            onNavigateToMemory={onNavigateToMemory}
            onNavigateToReferenceCards={() => onNavigateToReferenceCards()}
            onNavigateToReferenceCardCandidates={() => (
              onNavigateToReferenceCardCandidates()
            )}
            onNavigateToPlotThreads={onNavigateToPlotThreads}
            onOpenGenerationRuns={() => onOpenGenerationRuns({
              jobId: job._id,
              chapterId: job.current_chapter_id ?? job.error?.chapter_id ?? undefined,
            })}
            onRefresh={job.status === "completed"
              ? () => setCurrentBookAuditRevision((value) => value + 1)
              : undefined}
          />
        )}
        {isActive(job.status) && (
          <div className="grid gap-2 bg-surface px-4 py-3">
          <div className="flex items-center justify-between gap-3">
            <div className="min-w-0">
              <h3 className="truncate text-sm font-semibold text-foreground">
                {t("progressTitle")} · {jobScopeLabel}
              </h3>
              <p className="mt-0.5 text-xs text-muted">
                {t("progressChapters", { done: complete, total })}
                {currentChapter && (
                  <> · {t("progressCurrent", { order: currentChapter.order_index, title: currentChapter.title })}</>
                )}
              </p>
            </div>
            <div className="flex shrink-0 gap-2">
              <Button variant="outline" size="sm" onPress={() => void control("pause")} isDisabled={controlBusy}>
                {controlBusy ? t("pausing") : t("pause")}
              </Button>
              <Button variant="outline" size="sm" onPress={() => openAbortConfirmation("abort")} isDisabled={controlBusy}>
                {t("abort")}
              </Button>
            </div>
          </div>

          <div className="h-1.5 w-full overflow-hidden rounded-full bg-border/40">
            <div
              className="h-full bg-accent transition-all"
              style={{ width: total > 0 ? `${Math.round((complete / total) * 100)}%` : "0%" }}
            />
          </div>

          <p className="text-[11px] text-muted">
            {t("progressTokens", { tokens: job.tokens_used })}
            <span className="text-muted/70">{t("progressTokensNote")}</span>
          </p>
          {pollError && <p className="text-[11px] text-amber-600 dark:text-amber-400">{t("pollingRetry")}</p>}
          {controlError && (
            <p className="text-[11px] text-red-600 dark:text-red-400">{t("controlError", { message: controlError })}</p>
          )}
          </div>
        )}

        {isResumable(job.status) && (
          <CheckpointReview
            job={job}
            titleForChapter={titleForChapter}
            onJumpToChapter={onJumpToChapter}
            onNavigateToMemory={onNavigateToMemory}
            onNavigateToReferenceCards={onNavigateToReferenceCards}
            onNavigateToReferenceCardCandidates={
              onNavigateToReferenceCardCandidates
            }
            onNavigateToPlotThreads={onNavigateToPlotThreads}
            onResume={requestResume}
            onStartSuccessor={() => openAbortConfirmation("successor")}
            onRetryUncertain={() => void control("resume", { confirm_uncertain_retry: true })}
            onSkipUncertain={() => void control("resume", { skip_uncertain: true })}
            onAbort={() => openAbortConfirmation("abort")}
            busy={controlBusy}
            controlError={controlError}
            onOpenGenerationRuns={() => onOpenGenerationRuns({
              jobId: job._id,
              chapterId: job.current_chapter_id ?? job.error?.chapter_id ?? undefined,
            })}
          />
        )}

        {isTerminal(job.status) && (
          <div className="flex items-center justify-between gap-3 bg-surface px-4 py-3">
            <p className="text-sm text-foreground">
              {job.status === "completed"
                ? (job.scope === "book"
                    ? completionResult === "complete"
                      ? t("resultCompletedBook", { count: processedChapterCount, tokens: job.tokens_used })
                      : t("resultCompletedBookUnverified", { count: processedChapterCount, tokens: job.tokens_used })
                    : t("resultCompleted", { count: processedChapterCount, tokens: job.tokens_used }))
                : t("resultAborted")}
            </p>
            <button type="button" onClick={() => setDismissed(job._id)} className="shrink-0 text-xs font-medium text-accent hover:underline">
              {t("resultDismiss")}
            </button>
          </div>
        )}

        <ReferenceCardAutomationAuditPanel
          job={job}
          titleForChapter={titleForChapter}
          onJumpToChapter={onJumpToChapter}
          onNavigateToReferenceCards={onNavigateToReferenceCards}
          onNavigateToReferenceCardCandidates={
            onNavigateToReferenceCardCandidates
          }
        />

        {abortIntent && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 px-4 py-6">
            <div
              ref={abortDialogRef}
              role="alertdialog"
              aria-modal="true"
              aria-labelledby="abort-generation-title"
              aria-describedby="abort-generation-description"
              tabIndex={-1}
              className="max-h-[calc(100dvh-3rem)] w-full max-w-sm overflow-y-auto rounded-md border border-border bg-surface p-5 shadow-lg"
            >
              <h4 id="abort-generation-title" className="text-sm font-semibold text-foreground">
                {t(abortIntent === "successor" ? "successorConfirmTitle" : "abortConfirmTitle")}
              </h4>
              <p id="abort-generation-description" className="mt-2 text-xs leading-5 text-warm-700 dark:text-muted">
                {t(abortIntent === "successor" ? "successorConfirmBody" : "abortConfirmBody")}
              </p>
              {controlError && (
                <p
                  role="alert"
                  className="mt-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs leading-5 text-red-700 dark:border-red-900/60 dark:bg-red-950/40 dark:text-red-300"
                >
                  {t("controlError", { message: controlError })}
                </p>
              )}
              <div className="mt-4 flex flex-wrap justify-end gap-2">
                <button
                  ref={abortCancelRef}
                  type="button"
                  onClick={closeAbortConfirmation}
                  disabled={controlBusy}
                  className="min-h-9 rounded-md px-3 text-sm font-medium text-foreground hover:bg-surface-secondary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:opacity-60"
                >
                  {t("abortConfirmNo")}
                </button>
                <button
                  type="button"
                  onClick={confirmAbort}
                  disabled={controlBusy}
                  className="min-h-9 rounded-md border border-red-300 bg-red-50 px-3 text-sm font-semibold text-red-800 hover:bg-red-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-red-600 disabled:opacity-60 dark:border-red-900 dark:bg-red-950/40 dark:text-red-200"
                >
                  {controlBusy
                    ? t(abortIntent === "successor" ? "successorPreparing" : "aborting")
                    : t(abortIntent === "successor" ? "successorConfirmYes" : "abortConfirmYes")}
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </>
  );
}
