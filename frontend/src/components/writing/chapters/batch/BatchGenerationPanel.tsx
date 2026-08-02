"use client";

import { useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@heroui/react";
import { apiGet, apiPost } from "@/lib/api";
import type { ChapterSummary, VolumeSummary } from "@/types/novel";
import {
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
import CheckpointReview from "./CheckpointReview";
import LeftoverProseRuns from "./LeftoverProseRuns";

interface BatchGenerationPanelProps {
  novelId: string;
  selectedVolumeId: string | null;
  volumes: VolumeSummary[];
  chapters: ChapterSummary[];
  startScope: "volume" | "book" | null;
  onStartClose: () => void;
  onJumpToChapter: (chapterId: string) => void;
  onQuietRefresh: () => void;
  onNavigateToMemory: () => void;
  onNavigateToReferenceCards: () => void;
  proseRunsRevision: number;
  onOpenProseRun: (run: LeftoverProseRun) => void;
  onStartFreshProse: (chapterId: string) => void;
  onOpenGenerationRuns: (target?: GenerationRunsNavigationTarget) => void;
}

export default function BatchGenerationPanel({
  novelId,
  selectedVolumeId,
  volumes,
  chapters,
  startScope,
  onStartClose,
  onJumpToChapter,
  onQuietRefresh,
  onNavigateToMemory,
  onNavigateToReferenceCards,
  proseRunsRevision,
  onOpenProseRun,
  onStartFreshProse,
  onOpenGenerationRuns,
}: BatchGenerationPanelProps) {
  const t = useTranslations("writing.batch");
  const { job, error: pollError, setJob } = useGenerationJob({ onProgress: onQuietRefresh });
  const [controlBusy, setControlBusy] = useState(false);
  const [controlError, setControlError] = useState<string | null>(null);
  const [abortConfirm, setAbortConfirm] = useState(false);
  const [dismissed, setDismissed] = useState<string | null>(null); // 已关闭的终态作业 id


  // 发现：挂载时列小说作业，收养最近的非终态作业（设计 §5.1）。
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const jobs = await apiGet<GenerationJob[]>(`/api/generation-jobs/novel/${novelId}`);
        if (cancelled) return;
        const adopted = jobs
          .filter((j) => !isTerminal(j.status))
          .sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
        if (adopted) setJob(adopted);
      } catch {
        // 发现失败不致命：仍可手动启动；重新挂载再试。
      }
    })();
    return () => { cancelled = true; };
  }, [novelId, setJob]);

  const chapterById = useMemo(() => new Map(chapters.map((c) => [c._id, c])), [chapters]);
  const titleForChapter = (id: string) => chapterById.get(id)?.title ?? id;

  const control = async (
    action: "pause" | "resume" | "abort",
    body: Record<string, boolean> = {},
  ) => {
    if (!job) return;
    setControlBusy(true);
    setControlError(null);
    try {
      const next = await apiPost<GenerationJob>(`/api/generation-jobs/${job._id}/${action}`, body);
      setJob(next);
    } catch (err) {
      // resume 可能 409（别处有在跑作业）；原样展示（设计 §7.4）。
      setControlError(err instanceof Error ? err.message : String(err));
    } finally {
      setControlBusy(false);
    }
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
        onClose={onStartClose}
        onNavigateToReferenceCards={onNavigateToReferenceCards}
        onSubmitted={(started) => { setDismissed(null); setJob(started); onStartClose(); }}
      />
    ) : startScope === "book" ? (
      <StartJobDialog
        scope="book"
        targetId={novelId}
        title={t("dialogTitleBook")}
        targetHeading={t("dialogBookLabel")}
        targetLabel={t("dialogBookTarget")}
        fillableCount={bookFillableCount}
        onClose={onStartClose}
        onNavigateToReferenceCards={onNavigateToReferenceCards}
        onSubmitted={(started) => { setDismissed(null); setJob(started); onStartClose(); }}
      />
    ) : null;

  const generationRunsEntry = (
    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border bg-surface px-4 py-2.5">
      <p className="min-w-0 text-xs leading-5 text-muted">
        {job?.diagnostics?.length || job?.error || job?.pause_reason === "incomplete_scene"
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
      onOpenRun={onOpenProseRun}
      onStartFresh={onStartFreshProse}
    />
  );

  // 残留草稿是小说级状态，即使没有作业或终态条已关闭也必须常驻发现。
  if (!job || (isTerminal(job.status) && dismissed === job._id)) {
    return (
      <>
        {dialog}
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

  return (
    <>
      {dialog}
      {leftoverPanel}
      {generationRunsEntry}

      <div className="shrink-0 border-b border-border">
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
              <Button variant="outline" size="sm" onPress={() => setAbortConfirm(true)} isDisabled={controlBusy}>
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
            onResume={() => void control("resume")}
            onRetryUncertain={() => void control("resume", { confirm_uncertain_retry: true })}
            onSkipUncertain={() => void control("resume", { skip_uncertain: true })}
            onAbort={() => setAbortConfirm(true)}
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
                    ? t("resultCompletedBook", { count: processedChapterCount, tokens: job.tokens_used })
                    : t("resultCompleted", { count: processedChapterCount, tokens: job.tokens_used }))
                : t("resultAborted")}
            </p>
            <button type="button" onClick={() => setDismissed(job._id)} className="shrink-0 text-xs font-medium text-accent hover:underline">
              {t("resultDismiss")}
            </button>
          </div>
        )}

        {abortConfirm && (
          <div className="absolute inset-0 z-40 flex items-center justify-center bg-black/25 px-4 py-6">
            <div className="w-full max-w-sm rounded-md border border-border bg-surface p-5 shadow-lg">
              <h4 className="text-sm font-semibold text-foreground">{t("abortConfirmTitle")}</h4>
              <p className="mt-2 text-xs leading-5 text-muted">{t("abortConfirmBody")}</p>
              <div className="mt-4 flex justify-end gap-2">
                <Button variant="ghost" size="sm" onPress={() => setAbortConfirm(false)} isDisabled={controlBusy}>
                  {t("abortConfirmNo")}
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  onPress={() => { void control("abort").then(() => setAbortConfirm(false)); }}
                  isDisabled={controlBusy}
                >
                  {t("abortConfirmYes")}
                </Button>
              </div>
            </div>
          </div>
        )}
      </div>
    </>
  );
}
