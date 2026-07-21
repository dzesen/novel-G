"use client";

import { useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@heroui/react";
import { apiGet, apiPost } from "@/lib/api";
import type { ChapterSummary, VolumeSummary } from "@/types/novel";
import { type GenerationJob, isActive, isResumable, isTerminal } from "./batchTypes";
import { useGenerationJob } from "./useGenerationJob";
import StartVolumeJobDialog from "./StartVolumeJobDialog";
import CheckpointReview from "./CheckpointReview";

interface BatchGenerationPanelProps {
  novelId: string;
  selectedVolumeId: string | null;
  volumes: VolumeSummary[];
  chapters: ChapterSummary[];
  startOpen: boolean;
  onStartClose: () => void;
  onJumpToChapter: (chapterId: string) => void;
  onQuietRefresh: () => void;
  onNavigateToMemory: () => void;
}

export default function BatchGenerationPanel({
  novelId,
  selectedVolumeId,
  volumes,
  chapters,
  startOpen,
  onStartClose,
  onJumpToChapter,
  onQuietRefresh,
  onNavigateToMemory,
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

  const control = async (action: "pause" | "resume" | "abort") => {
    if (!job) return;
    setControlBusy(true);
    setControlError(null);
    try {
      const next = await apiPost<GenerationJob>(`/api/generation-jobs/${job._id}/${action}`, {});
      setJob(next);
    } catch (err) {
      // resume 可能 409（别处有在跑作业）；原样展示（设计 §7.4）。
      setControlError(err instanceof Error ? err.message : String(err));
    } finally {
      setControlBusy(false);
    }
  };

  // 启动对话框（分子/分母口径同进度条：word_count>0 && summary 视为已完整）。
  const selectedVolume = volumes.find((v) => v._id === selectedVolumeId) ?? null;
  const selectedVolumeChapters = chapters.filter((c) => c.volume_id === selectedVolumeId);
  const fillableCount = selectedVolumeChapters.filter((c) => !(c.word_count > 0 && c.summary.trim())).length;

  const dialog = startOpen && selectedVolumeId ? (
    <StartVolumeJobDialog
      volumeId={selectedVolumeId}
      volumeTitle={selectedVolume?.title ?? ""}
      fillableCount={fillableCount}
      onClose={onStartClose}
      onSubmitted={(started) => {
        setDismissed(null);
        setJob(started);
        onStartClose();
      }}
    />
  ) : null;

  // 无作业，或终态已关闭：只渲染可能的启动对话框，不留常驻条。
  if (!job || (isTerminal(job.status) && dismissed === job._id)) {
    return dialog;
  }

  const volumeChapters = chapters.filter((c) => c.volume_id === job.volume_id);
  const total = volumeChapters.length;
  const complete = volumeChapters.filter((c) => c.word_count > 0 && c.summary.trim()).length;
  const jobVolumeTitle = volumes.find((v) => v._id === job.volume_id)?.title ?? "";
  const currentChapter = job.current_chapter_id ? chapterById.get(job.current_chapter_id) : undefined;

  return (
    <div className="shrink-0 border-b border-border">
      {dialog}

      {isActive(job.status) && (
        <div className="grid gap-2 bg-surface px-4 py-3">
          <div className="flex items-center justify-between gap-3">
            <div className="min-w-0">
              <h3 className="truncate text-sm font-semibold text-foreground">
                {t("progressTitle")} · {t("progressVolume", { title: jobVolumeTitle })}
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
          onAbort={() => setAbortConfirm(true)}
          busy={controlBusy}
          controlError={controlError}
        />
      )}

      {isTerminal(job.status) && (
        <div className="flex items-center justify-between gap-3 bg-surface px-4 py-3">
          <p className="text-sm text-foreground">
            {job.status === "completed"
              ? t("resultCompleted", { count: job.progress.length, tokens: job.tokens_used })
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
  );
}
