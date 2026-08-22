"use client";

import { useTranslations } from "next-intl";
import { Button } from "@heroui/react";
import { type ChapterProgress, type GenerationJob, checkpointWindow } from "./batchTypes";
import {
  buildChapterPresentation,
  currentStopDiagnostic,
  isPlaceholderChapterTitle,
  outlineAdherenceForDisplay,
} from "./batchPresentation";
import { DiagnosticEventSummary } from "./GenerationDiagnosticsPanel";
import { checkpointWordCountPresentation } from "./checkpointWordCount";
import { jobPauseReasonTranslationKey } from "./generationReasonPresentation";
import { generationStepKind } from "../../generationMetadataPresentation";
import type { ReferenceCardType } from "./referenceCardAutoCreation";

interface CheckpointReviewProps {
  job: GenerationJob;
  titleForChapter: (chapterId: string) => string;
  onJumpToChapter: (chapterId: string) => void;
  onNavigateToMemory: () => void;
  onNavigateToReferenceCards: (
    cardType?: ReferenceCardType,
    cardId?: string,
  ) => void;
  onNavigateToReferenceCardCandidates: (candidateId?: string) => void;
  onNavigateToPlotThreads: () => void;
  onResume: () => void;
  onStartSuccessor: () => void;
  onRetryUncertain: () => void;
  onSkipUncertain: () => void;
  onAbort: () => void;
  busy: boolean;
  controlError: string | null;
  onOpenGenerationRuns: () => void;
}

function requiresSuccessorJob(job: GenerationJob): boolean {
  return job.error?.reason_codes?.includes("successor_required") ?? false;
}

function Banner({ job }: { job: GenerationJob }) {
  const t = useTranslations("writing.batch");
  const metadataT = useTranslations("writing.generationMetadata");
  const hasStructuredDiagnostic = Boolean(job.diagnostics?.length);
  if (job.status === "failed") {
    const e = job.error;
    return (
      <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900/60 dark:bg-red-950/40 dark:text-red-300">
        <span className="font-medium">{t("failedTitle")}</span>
        {hasStructuredDiagnostic
          ? <span className="ml-1">{t("failedStructuredBody")}</span>
          : e && <span className="ml-1">{t("failedBody", {
              step: metadataT(`steps.${generationStepKind(e.step)}`),
            })}</span>}
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
  const isUncertainReferenceRepair = (
    job.pause_reason === "uncertain_attempt"
    && Boolean(job.error?.auto_creation)
  );
  const key = requiresSuccessorJob(job)
    ? "reasonSourceChangedSuccessor"
    : isUncertainReferenceRepair
    ? "reasonReferenceCardRepairUncertain"
    : job.pause_reason
      ? jobPauseReasonTranslationKey(job.pause_reason) ?? "reasonCheckpoint"
      : "reasonCheckpoint";
  const tone =
    job.pause_reason === "conflict" || job.pause_reason === "outline_deviation"
      ? "border-red-200 bg-red-50 text-red-700 dark:border-red-900/60 dark:bg-red-950/40 dark:text-red-300"
      : job.pause_reason === "incomplete_scene"
          || job.pause_reason === "reference_card_review"
          || job.pause_reason === "reference_card_auto_creation_recovery"
          || job.pause_reason === "reference_card_repair_exhausted"
          || isUncertainReferenceRepair
        ? "border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-900/60 dark:bg-amber-950/40 dark:text-amber-200"
      : "border-border bg-background text-foreground";
  return <div className={`rounded-md border px-3 py-2 text-sm ${tone}`}>{t(key)}</div>;
}

function autoCreationDenialKeys(reasonCodes: string[]): string[] {
  const keys = new Set<string>();
  for (const code of reasonCodes) {
    if ([
      "pending_candidate_identity",
      "confirmed_alias",
      "deleted_identity",
      "cross_type_identity",
      "existing_name",
      "fuzzy_identity",
    ].includes(code)) {
      keys.add("referenceCardAutoDeniedIdentity");
    } else if (["field_conflict", "source_conflict"].includes(code)) {
      keys.add("referenceCardAutoDeniedConflict");
    } else if (code === "limit_reached") {
      keys.add("referenceCardAutoDeniedLimit");
    } else if ([
      "source_changed",
      "candidate_changed",
      "authorization_invalid",
      "unauthorized_type",
      "journal_drift",
    ].includes(code)) {
      keys.add("referenceCardAutoDeniedChanged");
    } else {
      keys.add("referenceCardAutoDeniedSafety");
    }
  }
  return [...keys];
}

function StepTags({ progress }: { progress: ChapterProgress }) {
  const t = useTranslations("writing.batch");
  const metadataT = useTranslations("writing.generationMetadata");
  const { stepBadges } = buildChapterPresentation(progress);
  const label = (step: string) => metadataT(
    `steps.${generationStepKind(step)}`,
  );
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
  onNavigateToReferenceCards,
  onNavigateToPlotThreads,
  isCurrentStopCause,
}: {
  progress: ChapterProgress;
  title: string;
  onJump: () => void;
  onNavigateToMemory: () => void;
  onNavigateToReferenceCards: () => void;
  onNavigateToPlotThreads: () => void;
  isCurrentStopCause: boolean;
}) {
  const t = useTranslations("writing.batch");
  const chapterEditorT = useTranslations("writing.chapterEditor");
  const metadataT = useTranslations("writing.generationMetadata");
  const hasConflict = progress.consistency_issues.length > 0;
  const adherence = outlineAdherenceForDisplay(progress.outline_adherence);
  const hasOutlineDeviation = adherence?.verdict === "fail";
  const presentation = buildChapterPresentation(progress);
  const proseWordCount = checkpointWordCountPresentation(progress.prose_completion);
  const chapterHeading = isPlaceholderChapterTitle(
    title,
    chapterEditorT("defaultChapterTitle", { index: progress.order_index }),
  )
    ? t("chapterRowOrder", { order: progress.order_index })
    : t("chapterRowTitle", { order: progress.order_index, title });
  return (
    <div className={`rounded-md border p-3 ${hasConflict || hasOutlineDeviation ? "border-red-300 dark:border-red-900/70" : "border-border"} bg-surface`}>
      <button type="button" onClick={onJump} title={t("jumpHint")} className="mb-2 block w-full text-left">
        <span className="text-sm font-medium text-foreground hover:text-accent">
          {chapterHeading}
        </span>
      </button>

      <StepTags progress={progress} />

      <div className="mt-2 flex flex-wrap gap-3 text-xs text-muted">
        <span>{t("factsAdded", { count: progress.facts_added })}</span>
        <span>{t("threadsAdvanced", { count: progress.threads_advanced })}</span>
      </div>

      {proseWordCount && (
        <div
          data-testid="checkpoint-word-count"
          className={`mt-2 grid gap-1 rounded-md border p-2 text-xs leading-5 ${
            proseWordCount.hasVisibleOverrun
              ? "border-amber-300 bg-amber-50 text-amber-800 dark:border-amber-900/70 dark:bg-amber-950/30 dark:text-amber-200"
              : "border-border bg-surface-secondary/40 text-muted"
          }`}
        >
          <span>{t("checkpointWordCount", {
            actual: proseWordCount.actualWordCount,
            target: proseWordCount.targetWordCount,
            ratio: proseWordCount.ratio.toFixed(2),
          })}</span>
          {proseWordCount.hasVisibleOverrun && (
            <span data-testid="checkpoint-word-count-overrun" className="font-medium">
              {t("checkpointWordCountOverrun")}
            </span>
          )}
        </div>
      )}

      {hasConflict && (
        <div className="mt-2 grid gap-2 rounded-md border border-red-200 bg-red-50 p-2 dark:border-red-900/60 dark:bg-red-950/30">
          <div className="flex flex-wrap items-center gap-2 text-xs font-semibold text-red-700 dark:text-red-300">
            <span>{t("conflictTitle")}</span>
            {isCurrentStopCause && (
              <span className="rounded-full border border-red-300 px-2 py-0.5 dark:border-red-800">
                {t("currentStopReason")}
              </span>
            )}
          </div>
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

      {adherence && adherence.verdict !== "pass" && (
        <div className={`mt-2 grid gap-2 rounded-md border p-2 ${
          adherence.verdict === "fail"
            ? "border-red-200 bg-red-50 text-red-700 dark:border-red-900/60 dark:bg-red-950/30 dark:text-red-300"
            : "border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-900/60 dark:bg-amber-950/30 dark:text-amber-200"
        }`}>
          <div className="flex flex-wrap items-center gap-2 text-xs font-semibold">
            <span>
              {adherence.verdict === "fail"
                ? t("outlineDeviationTitle")
                : t("outlineDeviationWarningTitle")}
            </span>
            {isCurrentStopCause && adherence.verdict === "fail" && (
              <span className="rounded-full border border-current/40 px-2 py-0.5">
                {t("currentStopReason")}
              </span>
            )}
          </div>
          <p className="text-xs">{adherence.summary}</p>
          {adherence.issues.map((issue, index) => (
            <div key={`${issue.category}-${index}`} className="grid gap-0.5 text-xs">
              <div>{t("outlineDeviationRequirement", { requirement: issue.outline_requirement })}</div>
              <div>{t("outlineDeviationEvidence", { evidence: issue.prose_evidence })}</div>
              <div>{t("outlineDeviationExplanation", { explanation: issue.explanation })}</div>
            </div>
          ))}
          {hasOutlineDeviation && (
            <button type="button" onClick={onJump} className="justify-self-start text-xs font-medium text-accent hover:underline">
              {t("outlineDeviationAction")}
            </button>
          )}
        </div>
      )}

      {presentation.contextNotices.length > 0 && (
        <div className="mt-2 grid gap-1 rounded-md border border-amber-200 bg-amber-50 p-2 text-[11px] text-amber-800 dark:border-amber-900/60 dark:bg-amber-950/30 dark:text-amber-200">
          <span className="font-semibold">{t("truncationTitle")}</span>
          {presentation.contextNotices.map((notice, i) => (
            <div key={i}>
              {notice.truncatedSections.length > 0 && (
                <div>{metadataT("contextTruncated", {
                  step: metadataT(`steps.${notice.step}`),
                  sections: notice.truncatedSections
                    .map((section) => metadataT(`contextSections.${section}`))
                    .join(metadataT("listSeparator")),
                })}</div>
              )}
              {Object.keys(notice.droppedItemCounts).length > 0 && (
                <div>
                  {metadataT("contextReduced", {
                    step: metadataT(`steps.${notice.step}`),
                    detail: Object.entries(notice.droppedItemCounts)
                      .map(([section, count]) => metadataT("contextItemCount", {
                        section: metadataT(`contextSections.${section}`),
                        count,
                      }))
                      .join(metadataT("listSeparator")),
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
            <div key={`${notice.kind}-${i}`}>
              <span className="font-medium">
                {metadataT("referenceCleanupCount", {
                  count: notice.count,
                  kind: metadataT(`referenceKinds.${notice.kind}`),
                })}
              </span>
              <span className="ml-1">
                {notice.readableValues.length > 0
                  ? metadataT("referenceCleanupNames", {
                      names: notice.readableValues.join(metadataT("listSeparator")),
                    })
                  : metadataT("referenceCleanupOpaque")}
              </span>
            </div>
          ))}
          <div>{t("referenceCleanupImpact")}</div>
          <div className="flex flex-wrap gap-x-4 gap-y-1">
            {presentation.referenceNotices.some(
              (notice) => notice.actionTarget === "reference_cards",
            ) && (
              <button type="button" onClick={onNavigateToReferenceCards} className="font-medium text-accent hover:underline">
                {t("referenceCleanupOpenCards")}
              </button>
            )}
            {presentation.referenceNotices.some(
              (notice) => notice.actionTarget === "plot_threads",
            ) && (
              <button type="button" onClick={onNavigateToPlotThreads} className="font-medium text-accent hover:underline">
                {t("referenceCleanupOpenThreads")}
              </button>
            )}
          </div>
        </div>
      )}

      {presentation.referenceRemapNotices.length > 0 && (
        <div className="mt-2 grid gap-1.5 rounded-md border border-blue-200 bg-blue-50 p-2 text-[11px] text-blue-800 dark:border-blue-900/60 dark:bg-blue-950/30 dark:text-blue-200">
          <span className="font-semibold">{t("referenceRemapTitle")}</span>
          {presentation.referenceRemapNotices.map((notice, i) => (
            <div key={`${notice.kind}-${notice.source ?? "reference"}-${i}`}>
              {notice.source
                ? metadataT("referenceRemapWithoutTarget", {
                    source: notice.source,
                    method: metadataT(`matchMethods.${notice.matchedBy}`),
                    kind: metadataT(`referenceKinds.${notice.kind}`),
                  })
                : metadataT("referenceRemapGeneric", {
                    method: metadataT(`matchMethods.${notice.matchedBy}`),
                    kind: metadataT(`referenceKinds.${notice.kind}`),
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
  onNavigateToReferenceCards,
  onNavigateToReferenceCardCandidates,
  onNavigateToPlotThreads,
  onResume,
  onStartSuccessor,
  onRetryUncertain,
  onSkipUncertain,
  onAbort,
  busy,
  controlError,
  onOpenGenerationRuns,
}: CheckpointReviewProps) {
  const t = useTranslations("writing.batch");
  const reviewWindow = checkpointWindow(job);
  const isUncertainReferenceRepair = (
    job.pause_reason === "uncertain_attempt"
    && Boolean(job.error?.auto_creation)
  );
  const hasUncertainAttempt = !isUncertainReferenceRepair
    && (job.has_uncertain_attempts || job.pause_reason === "uncertain_attempt");
  const requiresReferenceCardReview = (
    job.pause_reason === "reference_card_review"
    || job.pause_reason === "reference_card_repair_exhausted"
    || isUncertainReferenceRepair
  );
  const hasReferenceCardBoundary = Boolean(job.error?.auto_creation)
    || requiresReferenceCardReview;
  const requiresSuccessor = requiresSuccessorJob(job);

  const stopDiagnostic = currentStopDiagnostic(job);
  const blockingProgress = [...reviewWindow].reverse().find((progress) => {
    if (job.pause_reason === "outline_deviation") {
      return outlineAdherenceForDisplay(progress.outline_adherence)?.verdict === "fail";
    }
    return job.pause_reason === "conflict" && progress.consistency_issues.length > 0;
  });
  const currentStopChapterId = blockingProgress?.chapter_id ?? job.error?.chapter_id ?? null;
  const incompleteChapterId = job.pause_reason === "incomplete_scene" ? job.error?.chapter_id : null;

  return (
    <div className="grid gap-3 border-b border-border bg-surface-secondary/40 px-4 py-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <h3 className="text-sm font-semibold text-foreground">
          {t(
            requiresSuccessor
              ? "reviewSuccessorTitle"
              : job.pause_reason === "checkpoint"
                ? "reviewTitle"
                : "reviewRecoveryTitle",
          )}
        </h3>
        <div className="flex flex-wrap justify-end gap-2">
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
          ) : requiresReferenceCardReview ? (
            <Button
              variant="primary"
              size="sm"
              className="bg-amber-700 text-white hover:bg-amber-800 dark:bg-amber-600"
              onPress={() => onNavigateToReferenceCardCandidates()}
              isDisabled={busy}
            >
              {t("reviewReferenceCards")}
            </Button>
          ) : requiresSuccessor ? (
            <Button
              variant="primary"
              size="sm"
              className="max-w-full whitespace-normal bg-accent text-white hover:bg-accent-hover"
              onPress={onStartSuccessor}
              isDisabled={busy}
            >
              {t("startSuccessor")}
            </Button>
          ) : (
            <Button
              variant="primary"
              size="sm"
              className="bg-accent text-white hover:bg-accent-hover"
              onPress={onResume}
              isDisabled={busy}
            >
              {busy
                ? t("resuming")
                : job.pause_reason === "outline_deviation"
                  ? t("resumeAfterRewrite")
                  : job.pause_reason === "source_changed"
                    ? t("resumeAfterSourceChange")
                    : job.pause_reason === "final_audit"
                      ? t("rerunFinalAudit")
                    : job.pause_reason === "reference_card_auto_creation_recovery"
                      ? t("resumeAutoCardRecovery")
                    : t("resume")}
            </Button>
          )}
        </div>
      </div>

      <Banner job={job} />
      {hasReferenceCardBoundary
        && job.error?.candidate_names?.length ? (
          <p className="text-xs leading-5 text-amber-800 dark:text-amber-200">
            {t("referenceCardReviewNames", {
              names: job.error.candidate_names.join(t("referenceCardNameSeparator")),
            })}
          </p>
        ) : null}
      {hasReferenceCardBoundary
        && job.error?.auto_creation ? (
          <div className="grid gap-1 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-800 dark:border-amber-900/60 dark:bg-amber-950/30 dark:text-amber-200">
            <p className="font-medium">{t("referenceCardAutoManualReview")}</p>
            {autoCreationDenialKeys(job.error.auto_creation.deny_reasons).map((key) => (
              <p key={key}>{t(key)}</p>
            ))}
          </div>
        ) : null}
      {(incompleteChapterId || !stopDiagnostic) && (
        <div className="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-2">
          {incompleteChapterId && (
            <button
              type="button"
              onClick={() => onJumpToChapter(incompleteChapterId)}
              className="text-xs font-medium text-accent hover:underline"
            >
              {t("incompleteSceneOpenChapter")}
            </button>
          )}
          {!stopDiagnostic && (
            <button
              type="button"
              onClick={onOpenGenerationRuns}
              className="text-xs font-medium text-accent hover:underline"
            >
              {t("generationRunsOpen")}
            </button>
          )}
        </div>
      )}
      {controlError && (
        <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900/60 dark:bg-red-950/40 dark:text-red-300">
          {t("controlError", { message: controlError })}
        </div>
      )}
      {stopDiagnostic && (
        <details className="rounded-md border border-border bg-background px-3 py-2">
          <summary className="cursor-pointer text-xs font-semibold text-foreground">
            {t("diagnosticsCurrentTitle")}
          </summary>
          <div className="mt-2 border-t border-border pt-2">
            <DiagnosticEventSummary event={stopDiagnostic} />
          </div>
        </details>
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
              onNavigateToReferenceCards={() => onNavigateToReferenceCards()}
              onNavigateToPlotThreads={onNavigateToPlotThreads}
              isCurrentStopCause={p.chapter_id === currentStopChapterId}
            />
          ))}
        </div>
      )}
    </div>
  );
}
