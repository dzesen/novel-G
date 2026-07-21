"use client";

import { useTranslations } from "next-intl";
import { Button } from "@heroui/react";
import { type ChapterProgress, type GenerationJob, checkpointWindow } from "./batchTypes";

interface CheckpointReviewProps {
  job: GenerationJob;
  titleForChapter: (chapterId: string) => string;
  onJumpToChapter: (chapterId: string) => void;
  onNavigateToMemory: () => void;
  onResume: () => void;
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
  const label = (s: string) =>
    s === "outline" ? t("stepOutline") : s === "prose" ? t("stepProse") : s === "state" ? t("stepState") : s;
  return (
    <div className="flex flex-wrap gap-1.5 text-[11px]">
      {progress.steps_done.map((s) => (
        <span key={`d-${s}`} className="rounded border border-border bg-background px-1.5 py-0.5 text-foreground">
          {label(s)}
        </span>
      ))}
      {progress.steps_skipped.map((s) => (
        <span key={`s-${s}`} className="rounded border border-dashed border-border px-1.5 py-0.5 text-muted">
          {label(s)}·{t("skippedTag")}
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

      {(progress.truncations.length > 0 || Object.keys(progress.dropped_ids).length > 0) && (
        <div className="mt-2 grid gap-1 rounded-md border border-amber-200 bg-amber-50 p-2 text-[11px] text-amber-800 dark:border-amber-900/60 dark:bg-amber-950/30 dark:text-amber-200">
          <span className="font-semibold">{t("truncationTitle")}</span>
          {progress.truncations.map((tr, i) => (
            <div key={i}>
              {tr.truncated_sections.length > 0 && (
                <div>{t("truncationSections", { step: tr.step, sections: tr.truncated_sections.join("、") })}</div>
              )}
              {Object.keys(tr.dropped_item_counts).length > 0 && (
                <div>
                  {t("truncationDropped", {
                    step: tr.step,
                    detail: Object.entries(tr.dropped_item_counts).map(([k, v]) => `${k} ${v}`).join("、"),
                  })}
                </div>
              )}
            </div>
          ))}
          {Object.keys(progress.dropped_ids).length > 0 && (
            <div>{t("droppedIdsWarning", { detail: Object.keys(progress.dropped_ids).join("、") })}</div>
          )}
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
  onAbort,
  busy,
  controlError,
}: CheckpointReviewProps) {
  const t = useTranslations("writing.batch");
  const reviewWindow = checkpointWindow(job);

  return (
    <div className="grid gap-3 border-b border-border bg-surface-secondary/40 px-4 py-3">
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-sm font-semibold text-foreground">{t("reviewTitle")}</h3>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onPress={onAbort} isDisabled={busy}>
            {t("abort")}
          </Button>
          <Button
            variant="primary"
            size="sm"
            className="bg-accent text-white hover:bg-accent-hover"
            onPress={onResume}
            isDisabled={busy}
          >
            {busy ? t("resuming") : t("resume")}
          </Button>
        </div>
      </div>

      <Banner job={job} />
      {controlError && (
        <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900/60 dark:bg-red-950/40 dark:text-red-300">
          {t("controlError", { message: controlError })}
        </div>
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
