"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@heroui/react";
import { apiPost } from "@/lib/api";
import OutlineGenerationParams, {
  EMPTY_GENERATION_PARAMS,
  type GenerationParams,
} from "../outline/OutlineGenerationParams";
import ProseContinuationControls from "../prose/ProseContinuationControls";
import ReferenceCardAutoCreationControls from "./ReferenceCardAutoCreationControls";
import {
  DEFAULT_REFERENCE_CARD_AUTO_CREATION_POLICY,
  type ReferenceCardType,
} from "./referenceCardAutoCreation";
import { referenceCardTypeTranslationKey } from "./referenceCardAutoCreationPresentation";
import {
  DEFAULT_PROSE_CONTINUATION_POLICY,
  parsePositiveInteger,
  permitsAutomaticContinuation,
  type ProseBudgetCoverage,
  type ProseContinuationPolicy,
} from "../prose/proseContinuation";
import type {
  BookStructureInitializationResult,
  GenerationJob,
  GenerationReadiness,
  OutlineDeviationPolicy,
} from "./batchTypes";
import {
  buildAuthorizedStartPayload,
  readinessAllowsStart,
} from "./readinessPresentation";
import { readinessIssueCopy } from "./readinessIssuePresentation";
import {
  START_JOB_STAGES,
  batchGenerationOverrides,
  checkpointIntervalAllowsNext,
  initialAuthorizationAllowsNext,
  nextStartJobStage,
  previousStartJobStage,
  type StartJobStage,
} from "./startJobFlow";

const FOCUSABLE_SELECTOR = [
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "summary",
  "a[href]",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

interface StartJobDialogProps {
  scope: "volume" | "book";
  targetId: string;         // volume: volume_id；book: novel_id
  title: string;            // 对话框标题（调用方按 scope 解析好的 i18n 文案）
  targetHeading: string;    // 目标区小标题（"目标卷" / "目标"）
  targetLabel: string;      // 目标展示名（卷名 / "全书"）
  fillableCount: number;
  requiresStructureInitialization?: boolean;
  onSubmitted: (job: GenerationJob) => void;
  onStructureInitialized?: (result: BookStructureInitializationResult) => void;
  onClose: () => void;
  onNavigateToReferenceCards: () => void;
  onNavigateToWorldBaseline: () => void;
  onNavigateToBookStructure: () => void;
}

export default function StartJobDialog({
  scope,
  targetId,
  title,
  targetHeading,
  targetLabel,
  fillableCount,
  requiresStructureInitialization = false,
  onSubmitted,
  onStructureInitialized,
  onClose,
  onNavigateToReferenceCards,
  onNavigateToWorldBaseline,
  onNavigateToBookStructure,
}: StartJobDialogProps) {
  const t = useTranslations("writing.batch");
  const dialogRef = useRef<HTMLDivElement>(null);
  const firstInputRef = useRef<HTMLInputElement>(null);
  const stageHeadingRef = useRef<HTMLHeadingElement>(null);
  const [stage, setStage] = useState<StartJobStage>("authorization");
  const [checkpointInterval, setCheckpointInterval] = useState(5);
  const [periodicCheckpointsEnabled, setPeriodicCheckpointsEnabled] = useState(true);
  const [tokenBudget, setTokenBudget] = useState("");
  const [continuationPolicy, setContinuationPolicy] =
    useState<ProseContinuationPolicy>(DEFAULT_PROSE_CONTINUATION_POLICY);
  const [referenceCardAutoCreationPolicy, setReferenceCardAutoCreationPolicy] =
    useState(() => ({
      ...DEFAULT_REFERENCE_CARD_AUTO_CREATION_POLICY,
      allowed_card_types: [
        ...DEFAULT_REFERENCE_CARD_AUTO_CREATION_POLICY.allowed_card_types,
      ],
    }));

  const [outlineDeviationPolicy, setOutlineDeviationPolicy] =
    useState<OutlineDeviationPolicy>("pause_for_rewrite");
  const [generationParams, setGenerationParams] = useState<GenerationParams>(
    () => ({ ...EMPTY_GENERATION_PARAMS }),
  );
  const parsedTokenBudget = parsePositiveInteger(tokenBudget);
  const automaticContinuationsEnabled = permitsAutomaticContinuation(
    continuationPolicy,
  );
  const generationOverrides = batchGenerationOverrides(generationParams);
  const effectiveCheckpointInterval = periodicCheckpointsEnabled
    ? checkpointInterval
    : null;
  const readinessConfigurationKey = JSON.stringify({
    continuationPolicy,
    referenceCardAutoCreationPolicy,
    tokenBudget: parsedTokenBudget,
    generationParams,
    outlineDeviationPolicy,
  });
  const [submitting, setSubmitting] = useState(false);
  const [readiness, setReadiness] = useState<GenerationReadiness | null>(null);
  const [readinessLoading, setReadinessLoading] = useState(false);
  const [readinessConfiguration, setReadinessConfiguration] = useState<string | null>(null);
  const readinessIsCurrent = Boolean(readiness)
    && readinessConfiguration === readinessConfigurationKey;
  const [acknowledgedCodes, setAcknowledgedCodes] = useState<Set<string>>(new Set());
  const [error, setError] = useState("");
  const structureFlow = scope === "book" && (
    readiness
      ? Number(readiness.work.structure?.generate ?? 0) > 0
        || Boolean(
          readiness.work.structure
          && readiness.work.chapter_count === 0,
        )
      : requiresStructureInitialization
  );
  const structureTargetChapterCount = Number(
    readiness?.work.structure?.target_chapter_count ?? 0,
  );

  const loadReadiness = useCallback(async (preserveError = false) => {
    setReadinessLoading(true);
    if (!preserveError) setError("");
    try {
      const report = await apiPost<GenerationReadiness>(
        `/api/generation-jobs/${scope}/${targetId}/readiness`,
        {
          token_budget: parsedTokenBudget,
          outline_deviation_policy: outlineDeviationPolicy,
          prose_continuation_policy: continuationPolicy,
          reference_card_auto_creation_policy: referenceCardAutoCreationPolicy,
          ...generationOverrides,
        },
      );
      setReadiness(report);
      setReadinessConfiguration(readinessConfigurationKey);
      setAcknowledgedCodes(new Set());
    } catch (err) {
      setReadiness(null);
      setReadinessConfiguration(null);
      if (!preserveError) {
        setError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setReadinessLoading(false);
    }
  }, [
    continuationPolicy,
    generationOverrides,
    parsedTokenBudget,
    outlineDeviationPolicy,
    referenceCardAutoCreationPolicy,
    readinessConfigurationKey,
    scope,
    targetId,
  ]);

  useEffect(() => {
    const keepFocusInsideDialog = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !submitting) {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const dialog = dialogRef.current;
      if (!dialog) return;
      const focusable = Array.from(
        dialog.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
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
    window.addEventListener("keydown", keepFocusInsideDialog);
    return () => window.removeEventListener("keydown", keepFocusInsideDialog);
  }, [onClose, submitting]);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      if (stage === "authorization") firstInputRef.current?.focus();
      else stageHeadingRef.current?.focus();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [stage]);

  const referenceCardTypeLabel = (cardType: ReferenceCardType) =>
    t(referenceCardTypeTranslationKey(cardType));

  const generationOverrideSummary = [
    generationParams.temperature !== null
      ? t("dialogConfirmationParamTemperature", { value: generationParams.temperature })
      : null,
    generationParams.top_p !== null
      ? t("dialogConfirmationParamTopP", { value: generationParams.top_p })
      : null,
    generationParams.max_tokens !== null
      ? t("dialogConfirmationParamMaxTokens", { value: generationParams.max_tokens })
      : null,
    generationParams.presence_penalty !== null
      ? t("dialogConfirmationParamPresencePenalty", { value: generationParams.presence_penalty })
      : null,
    generationParams.frequency_penalty !== null
      ? t("dialogConfirmationParamFrequencyPenalty", { value: generationParams.frequency_penalty })
      : null,
    generationParams.allow_failure_retry
      ? t("dialogConfirmationParamRetryEnabled")
      : t("dialogConfirmationParamRetryDisabled"),
  ].filter((value): value is string => value !== null);

  const budgetCoverageReason = (coverage: ProseBudgetCoverage) => {
    switch (coverage.unavailable_reason) {
      case "token_budget_missing":
        return t("readinessCoverageReasonTokenBudgetMissing");
      case "token_bound_unproven":
        return t("readinessCoverageReasonTokenBoundUnproven");
      case "zero_token_bound":
        return t("readinessCoverageReasonZeroTokenBound");
      case "no_prose_chapters":
        return t("readinessCoverageReasonNoProseChapters");
      default:
        return t("readinessCoverageReasonUnknown");
    }
  };

  const submit = async () => {
    if (!readiness || !readinessIsCurrent || !readinessAllowsStart(
      readiness, acknowledgedCodes,
    )) return;
    setSubmitting(true);
    setError("");
    try {
      const payload = buildAuthorizedStartPayload({
        checkpointInterval: structureFlow ? null : effectiveCheckpointInterval,
        tokenBudget: parsedTokenBudget,
        readiness,
        acknowledgedCodes,
        outlineDeviationPolicy,
        generationParams: generationOverrides,
        proseContinuationPolicy: continuationPolicy,
        referenceCardAutoCreationPolicy,
      });
      if (structureFlow) {
        const result = await apiPost<BookStructureInitializationResult>(
          `/api/generation-jobs/book/${targetId}/initialize-structure`,
          payload,
        );
        onStructureInitialized?.(result);
      } else {
        const job = await apiPost<GenerationJob>(
          `/api/generation-jobs/${scope}/${targetId}`,
          payload,
        );
        onSubmitted(job);
      }
    } catch (err) {
      const startError = err instanceof Error ? err.message : String(err);
      setError(startError);
      await loadReadiness(true);
      setError(startError);
      setStage("readiness");
    } finally {
      setSubmitting(false);
    }
  };

  const advance = async () => {
    if (stage === "authorization") {
      if (!initialAuthorizationAllowsNext(parsedTokenBudget)) return;
      setStage(nextStartJobStage(stage));
      return;
    }
    if (stage === "behavior") {
      if (!checkpointIntervalAllowsNext(effectiveCheckpointInterval)) return;
      setStage("readiness");
      await loadReadiness();
      return;
    }
    if (
      stage === "readiness"
      && readiness
      && readinessIsCurrent
      && readinessAllowsStart(readiness, acknowledgedCodes)
    ) {
      setStage(nextStartJobStage(stage));
    }
  };

  const goBack = () => {
    if (submitting) return;
    setError("");
    setStage(previousStartJobStage(stage));
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/25 px-3 py-4 sm:px-4 sm:py-6">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="start-generation-title"
        tabIndex={-1}
        className="flex max-h-[calc(100dvh-2rem)] w-full max-w-2xl flex-col overflow-hidden rounded-md border border-border bg-surface shadow-lg sm:max-h-[calc(100dvh-3rem)]"
      >
        <header className="border-b border-border px-4 py-3 sm:px-5 sm:py-4">
          <h3 id="start-generation-title" className="break-words text-base font-semibold text-foreground">{title}</h3>
          <ol
            aria-label={t("dialogStageAria")}
            className="mt-3 grid grid-cols-4 gap-1"
          >
            {START_JOB_STAGES.map((item, index) => {
              const currentIndex = START_JOB_STAGES.indexOf(stage);
              const isCurrent = item === stage;
              const isComplete = index < currentIndex;
              return (
                <li key={item} className="min-w-0">
                  <div
                    aria-current={isCurrent ? "step" : undefined}
                    className={[
                      "h-1 rounded-full",
                      isCurrent || isComplete ? "bg-accent" : "bg-border",
                    ].join(" ")}
                  />
                  <span className={[
                    "mt-1 block truncate text-[10px] sm:text-xs",
                    isCurrent
                      ? "font-medium text-foreground"
                      : "text-warm-700 dark:text-muted",
                  ].join(" ")}
                  >
                    {t(`dialogStages.${item}`)}
                  </span>
                </li>
              );
            })}
          </ol>
        </header>

        <div className="grid min-w-0 gap-4 overflow-y-auto px-4 py-4 sm:px-5">
          <div className="grid gap-1 text-sm">
            <span className="text-xs font-medium text-warm-700 dark:text-muted">{targetHeading}</span>
            <div className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-1 rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground">
              <span className="min-w-0 break-words font-medium">{targetLabel}</span>
              <span className="text-xs text-warm-700 dark:text-muted">
                {structureFlow
                  ? structureTargetChapterCount > 0
                    ? t("dialogStructureTargetCount", {
                        count: structureTargetChapterCount,
                      })
                    : t("dialogStructureTargetPending")
                  : t("dialogFillable", { count: fillableCount })}
              </span>
            </div>
          </div>

          {stage === "authorization" && (
            <>
              <section className="rounded-md border border-accent/30 bg-accent/5 px-3 py-2.5">
                <h4
                  ref={stageHeadingRef}
                  tabIndex={-1}
                  className="text-sm font-semibold text-foreground outline-none"
                >
                  {structureFlow
                    ? t("dialogStructureAuthorizationTitle")
                    : t("dialogAuthorizationTitle")}
                </h4>
                <p className="mt-1 text-xs leading-5 text-warm-700 dark:text-muted">
                  {structureFlow
                    ? t("dialogStructureAuthorizationDescription")
                    : t("dialogAuthorizationDescription")}
                </p>
              </section>

              <div className="grid gap-1 text-sm">
                <label htmlFor="batch-token-budget" className="text-xs font-medium text-warm-700 dark:text-muted">
                  {t("dialogTokenLabel")}
                </label>
                <input
                  id="batch-token-budget"
                  type="number"
                  ref={firstInputRef}
                  min={1}
                  value={tokenBudget}
                  onChange={(e) => setTokenBudget(e.target.value)}
                  placeholder={t("dialogTokenPlaceholder")}
                  aria-invalid={tokenBudget !== "" && !parsedTokenBudget}
                  aria-describedby="batch-token-budget-hint"
                  className="min-h-10 w-full rounded-md border border-border bg-background px-3 py-2 text-base text-foreground outline-none focus:border-accent sm:text-sm"
                />
                <span id="batch-token-budget-hint" className="text-xs leading-5 text-warm-700 dark:text-muted">{t("dialogTokenHint")}</span>
              </div>
              {!parsedTokenBudget && (
                <p role="note" className="text-xs leading-5 text-amber-800 dark:text-amber-200">
                  {t("dialogTokenRequired")}
                </p>
              )}

              <label className="flex cursor-pointer items-start gap-3 rounded-md border border-border bg-background px-3 py-3">
                <input
                  type="checkbox"
                  checked={generationParams.allow_failure_retry}
                  onChange={(event) => setGenerationParams((current) => ({
                    ...current,
                    allow_failure_retry: event.target.checked,
                  }))}
                  className="mt-0.5 size-4 shrink-0"
                />
                <span className="min-w-0">
                  <span className="block text-sm font-medium text-foreground">
                    {t("dialogRetryPermission")}
                  </span>
                  <span className="mt-1 block text-xs leading-5 text-warm-700 dark:text-muted">
                    {t("dialogRetryPermissionHint")}
                  </span>
                </span>
              </label>

              {!structureFlow && (
              <>
              <details className="group rounded-md border border-border bg-background px-3 py-2.5">
                <summary className="cursor-pointer list-none text-sm font-medium text-foreground marker:hidden focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent">
                  <span className="flex min-w-0 items-center justify-between gap-3">
                    <span>{t("dialogContinuationPermission")}</span>
                    <span className="flex shrink-0 items-center gap-2 text-xs font-normal text-warm-700 dark:text-muted">
                      <span>
                        {t("dialogContinuationPermissionValue", {
                          count: continuationPolicy.automatic_continuations_per_scene,
                        })}
                      </span>
                      <svg aria-hidden="true" viewBox="0 0 20 20" className="size-4 transition-transform group-open:rotate-180">
                        <path d="m5 7.5 5 5 5-5" fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.75" />
                      </svg>
                    </span>
                  </span>
                  <span className="mt-1 block text-xs font-normal leading-5 text-warm-700 dark:text-muted">
                    {t("dialogPermissionOpenHint")}
                  </span>
                </summary>
                <div className="mt-3 border-t border-border pt-3">
                  <ProseContinuationControls
                    idPrefix="batch-prose"
                    value={continuationPolicy}
                    onChange={setContinuationPolicy}
                    disabled={submitting}
                  />
                </div>
              </details>
              {automaticContinuationsEnabled && !parsedTokenBudget && (
                <p role="note" className="text-xs leading-5 text-amber-800 dark:text-amber-200">
                  {t("continuationBudgetRequired")}
                </p>
              )}

              <details className="group rounded-md border border-border bg-background px-3 py-2.5">
                <summary className="cursor-pointer list-none text-sm font-medium text-foreground marker:hidden focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent">
                  <span className="flex min-w-0 items-center justify-between gap-3">
                    <span>{t("dialogReferenceCardPermission")}</span>
                    <span className="flex shrink-0 items-center gap-2 text-xs font-normal text-warm-700 dark:text-muted">
                      <span>
                        {referenceCardAutoCreationPolicy.enabled
                          ? t("readinessAutoCardsEnabled")
                          : t("readinessAutoCardsDisabled")}
                      </span>
                      <svg aria-hidden="true" viewBox="0 0 20 20" className="size-4 transition-transform group-open:rotate-180">
                        <path d="m5 7.5 5 5 5-5" fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.75" />
                      </svg>
                    </span>
                  </span>
                  <span className="mt-1 block text-xs font-normal leading-5 text-warm-700 dark:text-muted">
                    {t("dialogPermissionOpenHint")}
                  </span>
                </summary>
                <div className="mt-3 border-t border-border pt-3">
                  <ReferenceCardAutoCreationControls
                    value={referenceCardAutoCreationPolicy}
                    onChange={setReferenceCardAutoCreationPolicy}
                    disabled={submitting}
                  />
                </div>
              </details>

              <fieldset className="grid gap-2 rounded-md border border-border bg-background p-3">
                <legend className="px-1 text-xs font-medium text-warm-700 dark:text-muted">
                  {t("dialogDeviationPolicyLabel")}
                </legend>
                <label className="flex cursor-pointer items-start gap-2 text-sm">
                  <input
                    type="radio"
                    name="outline-deviation-policy"
                    value="pause_for_rewrite"
                    checked={outlineDeviationPolicy === "pause_for_rewrite"}
                    onChange={() => setOutlineDeviationPolicy("pause_for_rewrite")}
                    className="mt-0.5 size-4"
                  />
                  <span>
                    <span className="font-medium text-foreground">
                      {t("dialogDeviationPauseTitle")}
                    </span>
                    <span className="mt-0.5 block text-xs leading-5 text-warm-700 dark:text-muted">
                      {t("dialogDeviationPauseBody")}
                    </span>
                  </span>
                </label>
                <label className="flex cursor-pointer items-start gap-2 text-sm">
                  <input
                    type="radio"
                    name="outline-deviation-policy"
                    value="accept_and_continue"
                    checked={outlineDeviationPolicy === "accept_and_continue"}
                    onChange={() => setOutlineDeviationPolicy("accept_and_continue")}
                    className="mt-0.5 size-4"
                  />
                  <span>
                    <span className="font-medium text-foreground">
                      {t("dialogDeviationContinueTitle")}
                    </span>
                    <span className="mt-0.5 block text-xs leading-5 text-warm-700 dark:text-muted">
                      {t("dialogDeviationContinueBody")}
                    </span>
                  </span>
                </label>
                <p className="border-t border-border pt-2 text-xs leading-5 text-warm-700 dark:text-muted">
                  {t("dialogDeviationCostHint")}
                </p>
              </fieldset>
              </>
              )}
            </>
          )}

          {stage === "behavior" && (
            <>
              <section className="rounded-md border border-border bg-background px-3 py-2.5">
                <h4
                  ref={stageHeadingRef}
                  tabIndex={-1}
                  className="text-sm font-semibold text-foreground outline-none"
                >
                  {structureFlow
                    ? t("dialogStructureBehaviorTitle")
                    : t("dialogBehaviorTitle")}
                </h4>
                <p className="mt-1 text-xs leading-5 text-warm-700 dark:text-muted">
                  {structureFlow
                    ? t("dialogStructureBehaviorDescription")
                    : t("dialogBehaviorDescription")}
                </p>
              </section>

              {!structureFlow && (
              <fieldset className="grid gap-3 rounded-md border border-border bg-background p-3">
                <legend className="px-1 text-xs font-medium text-warm-700 dark:text-muted">
                  {t("dialogCheckpointLabel")}
                </legend>
                <label className="flex cursor-pointer items-start gap-2 text-sm">
                  <input
                    type="radio"
                    name="periodic-checkpoint-policy"
                    checked={periodicCheckpointsEnabled}
                    onChange={() => setPeriodicCheckpointsEnabled(true)}
                    aria-label={t("dialogCheckpointPeriodicTitle")}
                    aria-describedby="batch-checkpoint-periodic-hint"
                    className="mt-0.5 size-4 shrink-0"
                  />
                  <span className="min-w-0">
                    <span className="font-medium text-foreground">
                      {t("dialogCheckpointPeriodicTitle")}
                    </span>
                    <span
                      id="batch-checkpoint-periodic-hint"
                      className="mt-0.5 block text-xs leading-5 text-warm-700 dark:text-muted"
                    >
                      {t("dialogCheckpointPeriodicBody")}
                    </span>
                  </span>
                </label>
                <label className="flex cursor-pointer items-start gap-2 text-sm">
                  <input
                    type="radio"
                    name="periodic-checkpoint-policy"
                    checked={!periodicCheckpointsEnabled}
                    onChange={() => setPeriodicCheckpointsEnabled(false)}
                    aria-label={t("dialogCheckpointDisabledTitle")}
                    aria-describedby="batch-checkpoint-disabled-hint"
                    className="mt-0.5 size-4 shrink-0"
                  />
                  <span className="min-w-0">
                    <span className="font-medium text-foreground">
                      {t("dialogCheckpointDisabledTitle")}
                    </span>
                    <span
                      id="batch-checkpoint-disabled-hint"
                      className="mt-0.5 block text-xs leading-5 text-warm-700 dark:text-muted"
                    >
                      {t("dialogCheckpointDisabledBody")}
                    </span>
                  </span>
                </label>
                {periodicCheckpointsEnabled && (
                  <div className="grid gap-1 border-t border-border pt-3 text-sm">
                    <label htmlFor="batch-checkpoint-interval" className="text-xs font-medium text-warm-700 dark:text-muted">
                      {t("dialogCheckpointIntervalLabel")}
                    </label>
                    <input
                      id="batch-checkpoint-interval"
                      type="number"
                      min={1}
                      max={1000}
                      value={checkpointInterval}
                      onChange={(e) => setCheckpointInterval(Number(e.target.value))}
                      aria-invalid={!checkpointIntervalAllowsNext(checkpointInterval)}
                      aria-describedby="batch-checkpoint-interval-hint"
                      className="min-h-10 w-full rounded-md border border-border bg-surface px-3 py-2 text-base text-foreground outline-none focus:border-accent sm:text-sm"
                    />
                    <span id="batch-checkpoint-interval-hint" className="text-xs leading-5 text-warm-700 dark:text-muted">
                      {t("dialogCheckpointHint")}
                    </span>
                    {!checkpointIntervalAllowsNext(checkpointInterval) && (
                      <span role="note" className="text-xs leading-5 text-amber-800 dark:text-amber-200">
                        {t("dialogCheckpointInvalid")}
                      </span>
                    )}
                  </div>
                )}
              </fieldset>
              )}

              <section className="grid gap-2">
                <OutlineGenerationParams
                  value={generationParams}
                  onChange={setGenerationParams}
                  showSystemPrompt={false}
                  showFailureRetry={false}
                />
                <p className="px-1 text-xs leading-5 text-warm-700 dark:text-muted">
                  {structureFlow
                    ? t("dialogStructureGenerationParamsHint")
                    : t("dialogGenerationParamsHint")}
                </p>
              </section>

            </>
          )}

          {stage === "readiness" && (
          <section aria-labelledby="generation-readiness-title" className="min-w-0">
            <div className="flex items-start justify-between gap-3">
              <div>
                <h4
                  ref={stageHeadingRef}
                  id="generation-readiness-title"
                  tabIndex={-1}
                  className="text-sm font-semibold text-foreground outline-none"
                >
                  {t("readinessTitle")}
                </h4>
                <p className="mt-1 text-xs leading-5 text-warm-700 dark:text-muted">{t("readinessDescription")}</p>
              </div>
              {!readinessLoading && (
                <button
                  type="button"
                  onClick={() => void loadReadiness()}
                  className="shrink-0 text-xs font-medium text-accent hover:underline"
                >
                  {t("readinessRefresh")}
                </button>
              )}
            </div>
            {readiness && !readinessLoading && !readinessIsCurrent && (
              <p role="status" className="mt-3 text-xs leading-5 text-amber-800 dark:text-amber-200">
                {t("readinessSettingsChanged")}
              </p>
            )}


            {readinessLoading && (
              <p role="status" className="mt-3 text-sm text-warm-700 dark:text-muted">{t("readinessLoading")}</p>
            )}

            {readiness && !readinessLoading && (
              <div className="mt-3 grid gap-3">
                {structureFlow && readiness.work.structure ? (
                  <div className="grid min-w-0 gap-2 rounded-md border border-accent/30 bg-accent/5 p-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
                    <div className="min-w-0">
                      <p className="text-sm font-semibold text-foreground">
                        {t("readinessStructureTitle")}
                      </p>
                      <p className="mt-1 break-words text-xs leading-5 text-warm-700 dark:text-muted">
                        {t("readinessStructureDescription", {
                          count: readiness.work.structure.target_chapter_count,
                        })}
                      </p>
                    </div>
                    <p className="text-xs font-medium tabular-nums text-accent sm:text-end">
                      {t("readinessWorkCounts", {
                        generate: readiness.work.structure.generate,
                        reuse: readiness.work.structure.reuse,
                      })}
                    </p>
                    <p className="text-xs leading-5 text-warm-700 dark:text-muted sm:col-span-2">
                      {t("readinessStructureNext")}
                    </p>
                  </div>
                ) : (
                  <div className="grid gap-3 rounded-md border border-border bg-background p-3 sm:grid-cols-3">
                    {(["outline", "prose", "state"] as const).map((step) => (
                      <div key={step}>
                        <p className="text-xs font-medium text-foreground">
                          {step === "outline"
                            ? t("stepOutline")
                            : step === "prose"
                              ? t("stepProse")
                              : t("stepState")}
                        </p>
                        <p className="mt-1 text-xs text-warm-700 dark:text-muted">
                          {t("readinessWorkCounts", {
                            generate: readiness.work.steps[step].generate,
                            reuse: readiness.work.steps[step].reuse,
                          })}
                        </p>
                      </div>
                    ))}
                  </div>
                )}

                <div className="grid gap-1 text-xs text-warm-700 dark:text-muted sm:grid-cols-2">
                  <p>
                    {t("readinessResources", {
                      characters: readiness.resources.character,
                      locations: readiness.resources.location,
                      items: readiness.resources.item,
                      rules: readiness.resources.rule,
                      lores: readiness.resources.lore,
                    })}
                  </p>
                  {!structureFlow && readiness.planning.reference_card_auto_creation_policy && (() => {
                    const policy = readiness.planning.reference_card_auto_creation_policy;
                    return (
                      <p className="min-w-0 break-words sm:col-span-2">
                        {t("readinessAutoCardsSummary", {
                          status: policy.enabled
                            ? t("readinessAutoCardsEnabled")
                            : t("readinessAutoCardsDisabled"),
                          types: policy.allowed_card_types
                            .map(referenceCardTypeLabel)
                            .join(t("referenceCardNameSeparator")),
                          perChapter: policy.max_auto_creates_per_chapter,
                          perBook: policy.max_auto_creates_per_book,
                          repair: policy.max_candidate_repair_cycles_per_chapter,
                        })}
                      </p>
                    );
                  })()}
                  <p>
                    {t("readinessProviders", {
                      providers: readiness.planning.providers.join(", ") || t("readinessNone"),
                      attempts: readiness.planning.attempt_capacity,
                    })}
                  </p>
                  {!structureFlow && readiness.planning.prose_strategy && (
                    <>
                      <p className="sm:col-span-2">
                        {t("readinessProseStrategy", {
                          single: readiness.planning.prose_strategy.single_call_chapters,
                          segmented: readiness.planning.prose_strategy.scene_segment_chapters,
                          unknown: readiness.planning.prose_strategy.unknown_outline_chapters,
                          calls: readiness.planning.prose_strategy.maximum_prose_calls,
                        })}
                      </p>
                      {readiness.planning.prose_strategy.provider_alias && (
                        <p className="sm:col-span-2">
                          {t("readinessProseCapability", {
                            provider: readiness.planning.prose_strategy.provider_alias,
                            model: readiness.planning.prose_strategy.provider_model || "—",
                            tokens: readiness.planning.prose_strategy.max_output_tokens ?? t("readinessUnknown"),
                            words: readiness.planning.prose_strategy.safe_output_words ?? "—",
                            source: readiness.planning.prose_strategy.output_limit_known
                              ? t("readinessCapabilityKnown")
                              : t("readinessCapabilityConservative"),
                          })}
                        </p>
                      )}
                    </>
                  )}
                  {!structureFlow && readiness.planning.prose_continuation_authorization && (
                    <>
                      <p className="sm:col-span-2">
                        {t("readinessContinuationCalls", {
                          base: readiness.planning.prose_continuation_authorization.max_base_calls,
                          automatic: readiness.planning.prose_continuation_authorization.max_automatic_continuation_calls,
                          total: readiness.planning.prose_continuation_authorization.max_logical_prose_calls,
                        })}
                      </p>
                      <p className="sm:col-span-2">
                        {t("readinessContinuationBudget", {
                          bound: readiness.planning.prose_continuation_authorization.conservative_token_bound,
                          budget: readiness.planning.prose_continuation_authorization.token_budget ?? t("readinessNone"),
                        })}
                      </p>
                      {(() => {
                        const authorization = readiness.planning.prose_continuation_authorization;
                        const coverage = authorization.budget_coverage;
                        if (!coverage) return null;
                        if (
                          coverage.status === "available"
                          && coverage.chapters_with_automatic_continuations !== null
                          && coverage.chapters_without_automatic_continuations !== null
                        ) {
                          return authorization.max_automatic_continuation_calls > 0 ? (
                            <p className="sm:col-span-2">
                              {t("readinessBudgetCoverage", {
                                automatic: coverage.chapters_with_automatic_continuations,
                                base: coverage.chapters_without_automatic_continuations,
                                total: coverage.estimated_prose_chapter_count,
                              })}
                            </p>
                          ) : (
                            <p className="sm:col-span-2">
                              {t("readinessBudgetCoverageNoAutomatic", {
                                base: coverage.chapters_without_automatic_continuations,
                                total: coverage.estimated_prose_chapter_count,
                              })}
                            </p>
                          );
                        }
                        return (
                          <p className="sm:col-span-2">
                            {t("readinessBudgetCoverageUnavailable", {
                              reason: budgetCoverageReason(coverage),
                            })}
                          </p>
                        );
                      })()}
                    </>
                  )}
                </div>

                {readiness.issues.map((issue) => {
                  const copy = readinessIssueCopy(
                    issue,
                    t,
                    continuationPolicy.automatic_continuations_per_scene,
                  );
                  const requiresAck = issue.level === "warning_requires_ack";
                  const blocked = issue.level === "blocked";
                  return (
                    <div
                      key={issue.code}
                      className={
                        blocked
                          ? "rounded-md border border-red-300 bg-red-50 px-3 py-2.5 dark:border-red-900 dark:bg-red-950/40"
                          : "rounded-md border border-amber-300 bg-amber-50 px-3 py-2.5 dark:border-amber-900 dark:bg-amber-950/30"
                      }
                    >
                      <p className={blocked
                        ? "text-sm font-medium text-red-800 dark:text-red-200"
                        : "text-sm font-medium text-amber-900 dark:text-amber-200"}
                      >
                        {copy.title}
                      </p>
                      <p className={blocked
                        ? "mt-1 text-xs leading-5 text-red-700 dark:text-red-300"
                        : "mt-1 text-xs leading-5 text-amber-800 dark:text-amber-300"}
                      >
                        {copy.body}
                      </p>
                      {requiresAck && (
                        <label className="mt-2 flex cursor-pointer items-start gap-2 text-xs leading-5 text-amber-900 dark:text-amber-200">
                          <input
                            type="checkbox"
                            checked={acknowledgedCodes.has(issue.code)}
                            onChange={(event) => {
                              setAcknowledgedCodes((current) => {
                                const next = new Set(current);
                                if (event.target.checked) next.add(issue.code);
                                else next.delete(issue.code);
                                return next;
                              });
                            }}
                            className="mt-0.5 size-4"
                          />
                          <span>
                            {issue.code === "automatic_continuations_require_confirmation"
                              ? t("readinessAcknowledgeAutomatic", {
                                  count: continuationPolicy.automatic_continuations_per_scene,
                                })
                              : issue.code === "book_structure_initialization_required"
                                ? t("readinessAcknowledgeStructure", {
                                    count: Number(
                                      issue.details.target_chapter_count ?? 0,
                                    ),
                                  })
                              : issue.code === "automatic_reference_card_creation_requires_confirmation"
                                ? t("readinessAcknowledgeAutoCards")
                                : issue.code === "prose_output_risk_requires_ack"
                                ? t("readinessAcknowledgeOutputRisk")
                                : t("readinessAcknowledge")}
                          </span>
                        </label>
                      )}
                      {issue.action_codes.some((code) =>
                        code === "curate_reference_cards"
                        || code === "review_reference_card_proposal"
                      ) && (
                        <button
                          type="button"
                          onClick={() => {
                            onClose();
                            onNavigateToReferenceCards();
                          }}
                          className="mt-2 text-xs font-medium text-accent hover:underline"
                        >
                          {t("readinessOpenCards")}
                        </button>
                      )}
                      {issue.action_codes.includes("open_world_baseline") && (
                        <button
                          type="button"
                          onClick={() => {
                            onClose();
                            onNavigateToWorldBaseline();
                          }}
                          className="mt-2 text-xs font-medium text-accent hover:underline"
                        >
                          {t("readinessOpenWorldBaseline")}
                        </button>
                      )}
                      {issue.action_codes.some((code) =>
                        code === "review_book_structure"
                        || code === "review_book_structure_trash"
                        || code === "review_novel_blueprint"
                      ) && (
                        <button
                          type="button"
                          onClick={() => {
                            onClose();
                            onNavigateToBookStructure();
                          }}
                          className="mt-2 text-xs font-medium text-accent hover:underline"
                        >
                          {t("readinessOpenBookStructure")}
                        </button>
                      )}
                    </div>
                  );
                })}

                {readiness.issues.length === 0 && (
                  <p className="rounded-md border border-green-300 bg-green-50 px-3 py-2 text-sm text-green-800 dark:border-green-900 dark:bg-green-950/40 dark:text-green-200">
                    {t("readinessReady")}
                  </p>
                )}
              </div>
            )}
          </section>
          )}

          {stage === "confirmation" && readiness && (
            <section aria-labelledby="generation-confirmation-title" className="grid gap-3">
              <div className="rounded-md border border-accent/30 bg-accent/5 px-3 py-2.5">
                <h4
                  ref={stageHeadingRef}
                  id="generation-confirmation-title"
                  tabIndex={-1}
                  className="text-sm font-semibold text-foreground outline-none"
                >
                  {structureFlow
                    ? t("dialogStructureConfirmationTitle")
                    : t("dialogConfirmationTitle")}
                </h4>
                <p className="mt-1 text-xs leading-5 text-warm-700 dark:text-muted">
                  {structureFlow
                    ? t("dialogStructureConfirmationDescription")
                    : t("dialogConfirmationDescription")}
                </p>
              </div>
              <dl className="grid gap-px overflow-hidden rounded-md border border-border bg-border text-sm sm:grid-cols-2">
                <div className="min-w-0 bg-background p-3">
                  <dt className="text-xs text-warm-700 dark:text-muted">{t("dialogConfirmationScope")}</dt>
                  <dd className="mt-1 break-words font-medium text-foreground">{targetLabel}</dd>
                </div>
                <div className="min-w-0 bg-background p-3">
                  <dt className="text-xs text-warm-700 dark:text-muted">{t("dialogConfirmationWork")}</dt>
                  <dd className="mt-1 font-medium text-foreground">
                    {structureFlow
                      ? t("dialogStructureConfirmationWorkValue", {
                          count: structureTargetChapterCount,
                        })
                      : t("dialogConfirmationWorkValue", {
                          count: readiness.work.chapter_count,
                        })}
                  </dd>
                </div>
                <div className="min-w-0 bg-background p-3">
                  <dt className="text-xs text-warm-700 dark:text-muted">{t("dialogConfirmationBudget")}</dt>
                  <dd className="mt-1 font-medium tabular-nums text-foreground">
                    {t("dialogConfirmationBudgetValue", { budget: parsedTokenBudget ?? 0 })}
                  </dd>
                </div>
                <div className="min-w-0 bg-background p-3">
                  <dt className="text-xs text-warm-700 dark:text-muted">{t("dialogConfirmationCalls")}</dt>
                  <dd className="mt-1 font-medium tabular-nums text-foreground">
                    {t("dialogConfirmationCallsValue", { count: readiness.planning.attempt_capacity })}
                  </dd>
                </div>
                {!structureFlow && (
                <>
                <div className="min-w-0 bg-background p-3">
                  <dt className="text-xs text-warm-700 dark:text-muted">{t("dialogConfirmationCheckpoint")}</dt>
                  <dd className="mt-1 font-medium text-foreground">
                    {effectiveCheckpointInterval === null
                      ? t("dialogConfirmationCheckpointDisabledValue")
                      : t("dialogConfirmationCheckpointValue", {
                          count: effectiveCheckpointInterval,
                        })}
                  </dd>
                </div>
                <div className="min-w-0 bg-background p-3">
                  <dt className="text-xs text-warm-700 dark:text-muted">{t("dialogConfirmationContinuation")}</dt>
                  <dd className="mt-1 text-xs leading-5 text-foreground">
                    {t("dialogConfirmationContinuationValue", {
                      count: continuationPolicy.automatic_continuations_per_scene,
                    })}
                  </dd>
                </div>
                </>
                )}
                {!structureFlow && (
                <div className="min-w-0 bg-background p-3 sm:col-span-2">
                  <dt className="text-xs text-warm-700 dark:text-muted">{t("dialogConfirmationCards")}</dt>
                  <dd className="mt-1 text-xs leading-5 text-foreground">
                    {referenceCardAutoCreationPolicy.enabled
                      ? t("dialogConfirmationCardsEnabledValue", {
                          types: referenceCardAutoCreationPolicy.allowed_card_types
                            .map(referenceCardTypeLabel)
                            .join(t("referenceCardNameSeparator")),
                          perChapter: referenceCardAutoCreationPolicy.max_auto_creates_per_chapter,
                          perBook: referenceCardAutoCreationPolicy.max_auto_creates_per_book,
                          repair: referenceCardAutoCreationPolicy.max_candidate_repair_cycles_per_chapter,
                        })
                      : t("dialogConfirmationCardsDisabledValue")}
                  </dd>
                </div>
                )}
                {!structureFlow && (
                <div className="min-w-0 bg-background p-3 sm:col-span-2">
                  <dt className="text-xs text-warm-700 dark:text-muted">{t("dialogConfirmationDeviation")}</dt>
                  <dd className="mt-1 text-xs leading-5 text-foreground">
                    {outlineDeviationPolicy === "pause_for_rewrite"
                      ? t("dialogDeviationPauseTitle")
                      : t("dialogDeviationContinueTitle")}
                  </dd>
                </div>
                )}
                <div className="min-w-0 bg-background p-3 sm:col-span-2">
                  <dt className="text-xs text-warm-700 dark:text-muted">{t("dialogConfirmationGeneration")}</dt>
                  <dd className="mt-1 text-xs leading-5 text-foreground">
                    {generationOverrideSummary.join(t("dialogConfirmationParameterSeparator"))}
                    <span className="mt-1 block text-warm-700 dark:text-muted">
                      {structureFlow
                        ? t("dialogStructureConfirmationProtectedPrompt")
                        : t("dialogConfirmationProtectedPrompt")}
                    </span>
                  </dd>
                </div>
              </dl>
              <p className="text-xs leading-5 text-warm-700 dark:text-muted">
                {structureFlow
                  ? t("dialogStructureConfirmationFinalHint")
                  : t("dialogConfirmationFinalHint")}
              </p>
            </section>
          )}

          {error && (
            <div role="alert" className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900/60 dark:bg-red-950/40 dark:text-red-300">
              {error}
            </div>
          )}
        </div>

        <footer className="flex flex-wrap justify-end gap-2 border-t border-border px-4 py-3 sm:px-5">
          <Button variant="ghost" size="sm" onPress={onClose} isDisabled={submitting}>
            {t("dialogCancel")}
          </Button>
          {stage !== "authorization" && (
            <Button variant="outline" size="sm" onPress={goBack} isDisabled={submitting}>
              {t("dialogBack")}
            </Button>
          )}
          {stage === "confirmation" ? (
            <Button
              variant="primary"
              size="sm"
              className="bg-accent text-white hover:bg-accent-hover"
              onPress={() => void submit()}
              isDisabled={
                submitting
                || !readiness
                || !readinessIsCurrent
                || !readinessAllowsStart(readiness, acknowledgedCodes)
              }
            >
              {structureFlow
                ? submitting
                  ? t("dialogStructureStarting")
                  : t("dialogStructureStart")
                : submitting
                  ? t("dialogStarting")
                  : t("dialogStart")}
            </Button>
          ) : (
            <Button
              variant="primary"
              size="sm"
              className="bg-accent text-white hover:bg-accent-hover"
              onPress={() => void advance()}
              isDisabled={
                submitting
                || readinessLoading
                || (stage === "authorization" && !initialAuthorizationAllowsNext(parsedTokenBudget))
                || (stage === "behavior" && !checkpointIntervalAllowsNext(
                  effectiveCheckpointInterval,
                ))
                || (stage === "readiness" && (
                  !readiness
                  || !readinessIsCurrent
                  || !readinessAllowsStart(readiness, acknowledgedCodes)
                ))
              }
            >
              {stage === "behavior"
                ? t("dialogRunReadiness")
                : stage === "readiness"
                  ? t("dialogReviewAuthorization")
                  : t("dialogNext")}
            </Button>
          )}
        </footer>
      </div>
    </div>
  );
}
