"use client";

import { useTranslations } from "next-intl";
import { Button } from "@heroui/react";
import { type ChapterProgress, type GenerationJob, checkpointWindow } from "./batchTypes";
import { buildChapterPresentation } from "./batchPresentation";

interface CheckpointReviewProps {
  job: GenerationJob;
  titleForChapter: (chapterId: string) => string;
  onJumpToChapter: (chapterId: string) => void;
  onNavigateToMemory: () => void;
  onResume: () => void;
  onRetryUncertain: () => void;
  onSkipUncertain: () => void;
  onAbort: () => void;
  busy: boolean;
  controlError: string | null;
}

function Banner({ job }: { job: GenerationJob }) {
  const t = useTranslations("writing.batch");
  if (job.status === "failed") {
    const e = job.error;
    return (
      <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900/60 dark:bg-red-950/40 dark:text-red-300">
        <span className="font-medium">{t("failedTitle")}</span>
        {e && <span className="ml-1">{t("failedBody", { step: e.step, message: e.message })}</span>}
      </div>
    );
  }
  if (job.status === "interrupted") {
    return (
      <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800 dark:border-amber-900/60 dark:bg-amber-950/40 dark:text-amber-200">
        {t("reasonInterrupted")}
      </div>
    );
  }
  const key =
    job.pause_reason === "conflict" ? "reasonConflict"
      : job.pause_reason === "cost_cap" ? "reasonCostCap"
        : job.pause_reason === "attempt_capacity" ? "reasonAttemptCapacity"
        : job.pause_reason === "manual" ? "reasonManual"
          : "reasonCheckpoint";
  const tone =
    job.pause_reason === "conflict"
      ? "border-red-200 bg-red-50 text-red-700 dark:border-red-900/60 dark:bg-red-950/40 dark:text-red-300"
      : "border-border bg-background text-foreground";
  return <div className={`rounded-md border px-3 py-2 text-sm ${tone}`}>{t(key)}</div>;
}

function StepTags({ progress }: { progress: ChapterProgress }) {
  const t = useTranslations("writing.batch");
  const { stepBadges } = buildChapterPresentation(progress);
  const label = (s: string) =>
    s === "outline" ? t("stepOutline") : s === "prose" ? t("stepProse") : s === "state" ? t("stepState") : s;
  const statusLabel = (status: string) =>
    status === "reused" ? t("reusedTag")
      : status === "skipped" ? t("skippedTag")
        : status === "degraded" ? t("degradedTag")
          : status === "incomplete" ? t("incompleteTag")
            : status === "blocked" ? t("blockedTag")
              : status === "failed" ? t("failedTag")
                : "";
  const statusClass = (status: string) =>
    status === "generated" ? "border-border bg-background text-foreground"
      : status === "reused" ? "border-dashed border-border text-muted"
        : status === "degraded" || status === "incomplete"
          ? "border-amber-300 bg-amber-50 text-amber-800 dark:border-amber-900/70 dark:bg-amber-950/30 dark:text-amber-200"
          : "border-red-300 bg-red-50 text-red-700 dark:border-red-900/70 dark:bg-red-950/30 dark:text-red-300";
  return (
    <div className="flex flex-wrap gap-1.5 text-[11px]">
      {stepBadges.map((badge) => (
        <span key={`${badge.step}-${badge.status}`} className={`rounded border px-1.5 py-0.5 ${statusClass(badge.status)}`}>
          {label(badge.step)}
          {statusLabel(badge.status) && <>·{statusLabel(badge.status)}</>}
        </span>
      ))}
    </div>
  );
}

function ChapterCard({
  progress,
  title,
  onJump,
  onNavigateToMemory,
}: {
  progress: ChapterProgress;
  title: string;
  onJump: () => void;
  onNavigateToMemory: () => void;
}) {
  const t = useTranslations("writing.batch");
  const hasConflict = progress.consistency_issues.length > 0;
  const presentation = buildChapterPresentation(progress);
  return (
    <div className={`rounded-md border p-3 ${hasConflict ? "border-red-300 dark:border-red-900/70" : "border-border"} bg-surface`}>
      <button type="button" onClick={onJump} title={t("jumpHint")} className="mb-2 block w-full text-left">
        <span className="text-sm font-medium text-foreground hover:text-accent">
          {t("chapterRowTitle", { order: progress.order_index, title })}
        </span>
      </button>

      <StepTags progress={progress} />

      <div className="mt-2 flex flex-wrap gap-3 text-xs text-muted">
        <span>{t("factsAdded", { count: progress.facts_added })}</span>
        <span>{t("threadsAdvanced", { count: progress.threads_advanced })}</span>
      </div>

      {hasConflict && (
        <div className="mt-2 grid gap-2 rounded-md border border-red-200 bg-red-50 p-2 dark:border-red-900/60 dark:bg-red-950/30">
          <span className="text-xs font-semibold text-red-700 dark:text-red-300">{t("conflictTitle")}</span>
          {progress.consistency_issues.map((issue, i) => (
            <div key={i} className="text-xs text-red-700 dark:text-red-300">
              <div>{t("conflictFact", { fact: issue.fact })}</div>
              <div>{t("conflictConflict", { conflict: issue.conflict })}</div>
            </div>
          ))}
          <button type="button" onClick={onNavigateToMemory} className="justify-self-start text-xs font-medium text-accent hover:underline">
            {t("conflictJumpMemory")}
          </button>
        </div>
      )}

      {presentation.contextNotices.length > 0 && (
        <div className="mt-2 grid gap-1 rounded-md border border-amber-200 bg-amber-50 p-2 text-[11px] text-amber-800 dark:border-amber-900/60 dark:bg-amber-950/30 dark:text-amber-200">
          <span className="font-semibold">{t("truncationTitle")}</span>
          {presentation.contextNotices.map((notice, i) => (
            <div key={i}>
              {notice.truncatedSections.length > 0 && (
                <div>{t("truncationSections", { step: notice.step ?? "-", sections: notice.truncatedSections.join("、") })}</div>
              )}
              {Object.keys(notice.droppedItemCounts).length > 0 && (
                <div>
                  {t("truncationDropped", {
                    step: notice.step ?? "-",
                    detail: Object.entries(notice.droppedItemCounts).map(([k, v]) => `${k} ${v}`).join("、"),
                  })}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {presentation.referenceNotices.length > 0 && (
        <div className="mt-2 grid gap-1.5 rounded-md border border-amber-200 bg-amber-50 p-2 text-[11px] text-amber-800 dark:border-amber-900/60 dark:bg-amber-950/30 dark:text-amber-200">
          <span className="font-semibold">{t("referenceCleanupTitle")}</span>
          {presentation.referenceNotices.map((notice, i) => (
            <div key={`${notice.field}-${i}`}>
              {t("referenceCleanupDetail", {
                step: notice.step ?? "-",
                field: notice.field,
                values: notice.values.join("、"),
              })}
            </div>
          ))}
          <div>{t("referenceCleanupImpact")}</div>
          <button type="button" onClick={onNavigateToMemory} className="justify-self-start font-medium text-accent hover:underline">
            {t("referenceCleanupAction")}
          </button>
        </div>
      )}

      {presentation.referenceRemapNotices.length > 0 && (
        <div className="mt-2 grid gap-1.5 rounded-md border border-blue-200 bg-blue-50 p-2 text-[11px] text-blue-800 dark:border-blue-900/60 dark:bg-blue-950/30 dark:text-blue-200">
          <span className="font-semibold">{t("referenceRemapTitle")}</span>
          {presentation.referenceRemapNotices.map((notice, i) => (
            <div key={`${notice.field}-${notice.from}-${i}`}>
              {t("referenceRemapDetail", {
                step: notice.step ?? "-",
                field: notice.field,
                from: notice.from,
                to: notice.to,
                matchedBy: notice.matchedBy,
              })}
            </div>
          ))}
          <div>{t("referenceRemapImpact")}</div>
        </div>
      )}
    </div>
  );
}

export default function CheckpointReview({
  job,
  titleForChapter,
  onJumpToChapter,
  onNavigateToMemory,
  onResume,
  onRetryUncertain,
  onSkipUncertain,
  onAbort,
  busy,
  controlError,
}: CheckpointReviewProps) {
  const t = useTranslations("writing.batch");
  const reviewWindow = checkpointWindow(job);
  const hasUncertainAttempt = job.has_uncertain_attempts || job.pause_reason === "uncertain_attempt";

  return (
    <div className="grid gap-3 border-b border-border bg-surface-secondary/40 px-4 py-3">
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-sm font-semibold text-foreground">{t("reviewTitle")}</h3>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onPress={onAbort} isDisabled={busy}>
            {t("abort")}
          </Button>
          {hasUncertainAttempt ? (
            <>
              <Button variant="outline" size="sm" onPress={onSkipUncertain} isDisabled={busy}>
                {t("uncertainSkip")}
              </Button>
              <Button
                variant="primary"
                size="sm"
                className="bg-accent text-white hover:bg-accent-hover"
                onPress={onRetryUncertain}
                isDisabled={busy}
              >
                {busy ? t("resuming") : t("uncertainRetry")}
              </Button>
            </>
          ) : (
            <Button
              variant="primary"
              size="sm"
              className="bg-accent text-white hover:bg-accent-hover"
              onPress={onResume}
              isDisabled={busy}
            >
              {busy ? t("resuming") : t("resume")}
            </Button>
          )}
        </div>
      </div>

      <Banner job={job} />
      {controlError && (
        <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900/60 dark:bg-red-950/40 dark:text-red-300">
          {t("controlError", { message: controlError })}
        </div>
      )}
      {hasUncertainAttempt && (
        <p className="text-xs leading-5 text-amber-700 dark:text-amber-300">
          {t("uncertainDetail")}
        </p>
      )}

      {reviewWindow.length === 0 ? (
        <p className="py-4 text-center text-xs text-muted">{t("windowEmpty")}</p>
      ) : (
        <div className="grid max-h-[42vh] gap-2 overflow-y-auto pr-1">
          {reviewWindow.map((p) => (
            <ChapterCard
              key={p.chapter_id}
              progress={p}
              title={titleForChapter(p.chapter_id)}
              onJump={() => onJumpToChapter(p.chapter_id)}
              onNavigateToMemory={onNavigateToMemory}
            />
          ))}
        </div>
      )}
    </div>
  );
}
