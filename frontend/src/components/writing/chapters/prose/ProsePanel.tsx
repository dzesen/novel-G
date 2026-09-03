"use client";

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";
import { useTranslations } from "next-intl";
import { Button } from "@heroui/react";
import { apiGet, apiPost } from "@/lib/api";
import {
  useProseStream,
  type ProseRunSnapshot,
} from "./useProseStream";
import OutlineGenerationParams, {
  EMPTY_GENERATION_PARAMS,
  toRequestParams,
  type GenerationParams,
} from "../outline/OutlineGenerationParams";
import { ContextNotices, Notice } from "../outline/outlineUi";
import { countChapterWords } from "../chapterUtils";
import {
  buildInteractiveCompletionPayload,
  buildInteractiveCompletionReadinessPayload,
  buildInteractiveCompletionResolutionPayload,
  buildProseAcceptPayload,
  finishReasonTranslationKey,
  proseAdvisoryTranslationKey,
  proseReasonTranslationKey,
  proseRequiresPartialAcknowledgement,
  interactiveCompletionErrorCode,
  type InteractiveCompletionResolutionAction,
} from "./prosePresentation";
import {
  buildProseDiscardPayload,
  proseRunActionsBlocked,
  proseRunConfirmationResetRequired,
  submitProseRunMutation,
} from "./proseRunMutation";
import ProseContinuationControls from "./ProseContinuationControls";
import {
  DEFAULT_PROSE_CONTINUATION_POLICY,
  parsePositiveInteger,
  permitsAutomaticContinuation,
  type ProseContinuationPolicy,
  type ProseReadiness,
} from "./proseContinuation";

interface InteractiveCompletionReadiness {
  schema_version: "interactive_chapter_completion_readiness.v2";
  digest: string;
  authorization_id: string;
  authorization_revision: number;
  logical_call_count: 2;
  recovery_replay_limit: 1;
  maximum_paid_attempts: number;
  conservative_token_bound: number;
  externalized_prose_utf8_bytes: number;
  provider_bounds: Array<{
    provider_alias: string;
    maximum_paid_attempts: number;
    conservative_token_bound: number;
    pricing_status: "available" | "unavailable";
    currency: string | null;
    maximum_cost: string | null;
    price_upper_bound_per_million_tokens: string | null;
    pricing_basis: string | null;
    pricing_snapshot_digest: string | null;
  }>;
  warnings: Array<{
    code: "provider_pricing_unavailable";
    provider_alias: string;
  }>;
}

interface ProsePanelProps {
  novelId: string;
  chapterId: string;
  initialRun?: ProseRunSnapshot | null;
  /** 编辑器里当前正文是否非空。为真时接受需要二次确认（设计 §2）。 */
  hasExistingContent: boolean;
  onClose: () => void;
  onRunStateChanged?: () => void;
  onAccepted: (
    text: string,
    acceptanceState: "ai_complete" | "partial_manual_required",
  ) => void;
}

export default function ProsePanel({
  novelId,
  chapterId,
  initialRun = null,
  hasExistingContent,
  onClose,
  onRunStateChanged,
  onAccepted,
}: ProsePanelProps) {
  const t = useTranslations("writing.prose");
  const stream = useProseStream();
  const hydrateRun = stream.hydrate;
  const resetStream = stream.reset;
  const [params, setParams] = useState<GenerationParams>(EMPTY_GENERATION_PARAMS);
  const [continuationPolicy, setContinuationPolicy] =
    useState<ProseContinuationPolicy>(DEFAULT_PROSE_CONTINUATION_POLICY);
  const [continuationBudget, setContinuationBudget] = useState("");
  const [continuationReadiness, setContinuationReadiness] = useState<ProseReadiness | null>(null);
  const [continuationReadinessKey, setContinuationReadinessKey] = useState<string | null>(null);
  const [continuationReadinessLoading, setContinuationReadinessLoading] = useState(false);
  const [continuationReadinessError, setContinuationReadinessError] = useState("");
  const [automaticContinuationsConfirmed, setAutomaticContinuationsConfirmed] = useState(false);
  const [completionReadiness, setCompletionReadiness] =
    useState<InteractiveCompletionReadiness | null>(null);
  const [completionReadinessLoading, setCompletionReadinessLoading] =
    useState(false);
  const [completionReadinessConfirmed, setCompletionReadinessConfirmed] =
    useState(false);
  const [completionUncertain, setCompletionUncertain] = useState(false);
  const [completionResolutionLoading, setCompletionResolutionLoading] =
    useState(false);

  const [overwriteArmed, setOverwriteArmed] = useState(false);
  const [partialArmed, setPartialArmed] = useState(false);
  const [uncertainRetryArmed, setUncertainRetryArmed] = useState(false);
  const [restoreLoading, setRestoreLoading] = useState(true);
  const [accepting, setAccepting] = useState(false);
  const [discarding, setDiscarding] = useState(false);
  const [actionError, setActionError] = useState("");
  const [syncError, setSyncError] = useState("");
  const [syncNotice, setSyncNotice] = useState("");
  const [conflictRequiresSync, setConflictRequiresSync] = useState(false);
  const [initialRunResolved, setInitialRunResolved] = useState(false);
  const dialogRef = useRef<HTMLDivElement>(null);

  const running = stream.status === "running";
  const mutationPending = accepting
    || discarding
    || completionReadinessLoading
    || completionResolutionLoading;
  const runActionsBlocked = proseRunActionsBlocked({
    restoreLoading,
    streamStatus: stream.status,
    conflictRequiresSync,
    mutationPending,
  });
  const manualSyncRequired = !restoreLoading && (
    conflictRequiresSync || stream.status === "cancelled"
  );
  const hasText = stream.text.length > 0;
  const incomplete = hasText && (
    stream.status === "cancelled"
    || stream.status === "error"
    || stream.status === "incomplete"
    || Boolean(stream.completion && !stream.completion.can_write_formal_prose)
  );
  const partialAcceptance = Boolean(
    stream.completion
      ? proseRequiresPartialAcknowledgement(stream.completion)
      : incomplete,
  );
  const completionReadinessKey = (
    stream.runId && stream.runRevision != null
      ? `${stream.runId}:${stream.runRevision}`
      : null
  );
  const selectedInitialRun = initialRunResolved ? null : initialRun;
  const resumableDraft = Boolean(
    stream.runId
    && stream.runRevision != null
    && (
      selectedInitialRun
        ? (selectedInitialRun.can_resume ?? incomplete)
        : incomplete
    ),
  );
  const reasonCodes = (
    selectedInitialRun?.reason_codes?.length
      ? selectedInitialRun.reason_codes
      : stream.completion?.reason_codes
  ) ?? [];
  const advisoryCodes = stream.completion?.advisory_codes ?? [];
  const automaticContinuationsEnabled = permitsAutomaticContinuation(
    continuationPolicy,
  );
  const continuationBudgetValue = parsePositiveInteger(continuationBudget);
  const continuationBudgetInputValid = continuationBudget === ""
    || continuationBudgetValue !== null;
  const continuationConfigurationKey = JSON.stringify({
    policy: continuationPolicy,
    tokenBudget: continuationBudgetValue,
    generationParams: params,
    resumeRunId: resumableDraft ? stream.runId : null,
    expectedRunRevision: resumableDraft ? stream.runRevision : null,
  });
  const continuationReadinessIsCurrent = Boolean(continuationReadiness)
    && continuationReadinessKey === continuationConfigurationKey;

  useEffect(() => {
    setInitialRunResolved(false);
  }, [initialRun?.run_id, initialRun?._id]);

  useEffect(() => {
    setContinuationReadiness(null);
    setContinuationReadinessKey(null);
    setContinuationReadinessError("");
    setAutomaticContinuationsConfirmed(false);
  }, [continuationConfigurationKey]);

  useEffect(() => {
    setCompletionReadiness(null);
    setCompletionReadinessConfirmed(false);
    setCompletionUncertain(false);
  }, [completionReadinessKey]);

  useEffect(() => {
    const previouslyFocused = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    const firstFocusable = dialogRef.current?.querySelector<HTMLElement>(
      'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    );
    firstFocusable?.focus();
    return () => {
      if (previouslyFocused && document.contains(previouslyFocused)) {
        previouslyFocused.focus();
      }
    };
  }, []);

  const restoreActive = useCallback(async ({
    clearWhenMissing = false,
  }: {
    clearWhenMissing?: boolean;
  } = {}): Promise<ProseRunSnapshot | null> => {
    setRestoreLoading(true);
    try {
      const run = await apiGet<ProseRunSnapshot | null>(
        `/api/llm/prose-runs/chapter/${chapterId}`,
      );
      if (run) {
        hydrateRun(run);
      } else if (clearWhenMissing) {
        resetStream();
      }
      setConflictRequiresSync(false);
      return run;
    } finally {
      setRestoreLoading(false);
    }
  }, [chapterId, hydrateRun, resetStream]);

  useEffect(() => {
    if (initialRun) {
      hydrateRun(initialRun);
      setRestoreLoading(false);
      return;
    }
    void restoreActive().catch((error: unknown) => {
      setActionError(error instanceof Error ? error.message : String(error));
    });
  }, [hydrateRun, initialRun, restoreActive]);

  useEffect(() => {
    if (stream.status !== "cancelled") return;
    setSyncError("");
    setSyncNotice("");
    void restoreActive({ clearWhenMissing: true }).catch(() => {
      setSyncError(t("restoreFailed"));
    });
  }, [restoreActive, stream.status, t]);

  const retryRunSync = async () => {
    setSyncError("");
    setSyncNotice("");
    try {
      await restoreActive({ clearWhenMissing: true });
      setSyncNotice(t("syncComplete"));
    } catch {
      setSyncError(t("restoreFailed"));
    }
  };

  const resetRunConfirmationLocks = () => {
    setPartialArmed(false);
    setOverwriteArmed(false);
    setUncertainRetryArmed(false);
  };

  const finishInteractiveCompletion = async (
    runId: string,
    runRevision: number,
    readiness: InteractiveCompletionReadiness,
  ) => {
    await apiPost(
      `/api/llm/prose-runs/${runId}/complete`,
      buildInteractiveCompletionPayload({
        novelId,
        chapterId,
        runRevision,
        authorizationId: readiness.authorization_id,
        authorizationRevision: readiness.authorization_revision,
        readinessDigest: readiness.digest,
      }),
    );
    onAccepted(stream.text, "ai_complete");
    onRunStateChanged?.();
    onClose();
  };

  const recordCompletionFailure = (error: unknown): boolean => {
    if (interactiveCompletionErrorCode(error) !== "interactive_uncertain_attempt") {
      return false;
    }
    setCompletionUncertain(true);
    setActionError("");
    return true;
  };

  const accept = async () => {
    if (!hasText || running || runActionsBlocked) return;
    const runId = stream.runId;
    const runRevision = stream.runRevision;
    setActionError("");
    setSyncNotice("");
    if (selectedInitialRun?.can_accept_partial === false) {
      setActionError(t("leftoverAcceptUnavailable"));
      return;
    }
    if (partialAcceptance && !partialArmed) {
      setPartialArmed(true);
      return;
    }
    if (hasExistingContent && !overwriteArmed) {
      setOverwriteArmed(true);
      return;
    }
    if (!runId || runRevision == null) {
      setActionError(t("runMissing"));
      return;
    }
    if (!partialAcceptance && !completionReadiness) {
      setCompletionReadinessLoading(true);
      try {
        const readiness = await apiPost<InteractiveCompletionReadiness>(
          `/api/llm/prose-runs/${runId}/completion-readiness`,
          buildInteractiveCompletionReadinessPayload({
            novelId,
            chapterId,
            runRevision,
          }),
        );
        setCompletionReadiness(readiness);
        setCompletionReadinessConfirmed(false);
      } catch (error) {
        setActionError(error instanceof Error ? error.message : String(error));
      } finally {
        setCompletionReadinessLoading(false);
      }
      return;
    }
    if (
      !partialAcceptance
      && completionReadiness
      && !completionReadinessConfirmed
    ) {
      setActionError(t("completionConfirmationRequired"));
      return;
    }
    setAccepting(true);
    try {
      if (!partialAcceptance && completionReadiness) {
        await finishInteractiveCompletion(
          runId,
          runRevision,
          completionReadiness,
        );
        return;
      }
      const outcome = await submitProseRunMutation({
        mutate: () => apiPost(
          `/api/llm/prose-runs/${runId}/accept`,
          buildProseAcceptPayload({
            novelId,
            chapterId,
            runId,
            runRevision,
            partial: true,
          }),
        ),
        refresh: () => restoreActive({ clearWhenMissing: true }),
      });
      if (outcome.status !== "success") {
        setInitialRunResolved(true);
        if (proseRunConfirmationResetRequired(outcome.status)) {
          resetRunConfirmationLocks();
        }
        if (outcome.status === "conflict_refreshed") {
          setSyncNotice(t("runConflictRefreshed"));
        } else {
          setConflictRequiresSync(true);
          setSyncError(t("runConflictRefreshFailed"));
        }
        return;
      }
      onAccepted(stream.text, "partial_manual_required");
      onRunStateChanged?.();
      onClose();
    } catch (error) {
      if (!recordCompletionFailure(error)) {
        setActionError(error instanceof Error ? error.message : String(error));
      }
    } finally {
      setAccepting(false);
    }
  };

  const resolveCompletionUncertainty = async (
    action: InteractiveCompletionResolutionAction,
  ) => {
    const runId = stream.runId;
    const runRevision = stream.runRevision;
    const readiness = completionReadiness;
    if (!runId || runRevision == null || !readiness) return;
    setCompletionResolutionLoading(true);
    setActionError("");
    setSyncNotice("");
    try {
      await apiPost(
        `/api/llm/prose-runs/${runId}/complete/uncertain-resolution`,
        buildInteractiveCompletionResolutionPayload({
          novelId,
          chapterId,
          runRevision,
          authorizationId: readiness.authorization_id,
          authorizationRevision: readiness.authorization_revision,
          readinessDigest: readiness.digest,
          action,
        }),
      );
      setCompletionUncertain(false);
      if (action === "retry") {
        await finishInteractiveCompletion(runId, runRevision, readiness);
        return;
      }
      setCompletionReadiness(null);
      setCompletionReadinessConfirmed(false);
      setSyncNotice(t("completionUncertainAborted"));
    } catch (error) {
      if (!recordCompletionFailure(error)) {
        setActionError(error instanceof Error ? error.message : String(error));
      }
    } finally {
      setCompletionResolutionLoading(false);
    }
  };

  const inspectContinuationReadiness = useCallback(async (): Promise<boolean> => {
    if (!automaticContinuationsEnabled) return true;
    if (!continuationBudgetInputValid) {
      setContinuationReadinessError(t("continuationBudgetInvalid"));
      return false;
    }
    setContinuationReadinessLoading(true);
    setContinuationReadinessError("");
    try {
      const report = await apiPost<ProseReadiness>(
        "/api/llm/write-chapter-by-ai/readiness",
        {
          novel_id: novelId,
          chapter_id: chapterId,
          ...(resumableDraft
            ? {
                resume_run_id: stream.runId,
                expected_run_revision: stream.runRevision,
              }
            : {}),
          prose_continuation_policy: continuationPolicy,
          token_budget: continuationBudgetValue,
          ...toRequestParams(params),
        },
      );
      setContinuationReadiness(report);
      setContinuationReadinessKey(continuationConfigurationKey);
      return true;
    } catch (error) {
      setContinuationReadiness(null);
      setContinuationReadinessKey(null);
      setContinuationReadinessError(
        error instanceof Error ? error.message : String(error),
      );
      return false;
    } finally {
      setContinuationReadinessLoading(false);
    }
  }, [
    automaticContinuationsEnabled,
    chapterId,
    continuationBudgetInputValid,
    continuationBudgetValue,
    continuationConfigurationKey,
    continuationPolicy,
    novelId,
    params,
    resumableDraft,
    stream.runId,
    stream.runRevision,
    t,
  ]);

  const startGeneration = async () => {
    if (runActionsBlocked) return;
    // 保险栓在每次重新生成时复位：上一份预览已被新的一轮取代，
    // 针对它的确认不该延续到下一份（2a Task 7 就栽在栓不复位上）。
    setOverwriteArmed(false);
    setPartialArmed(false);
    if (selectedInitialRun?.can_resume === false) {
      setActionError(t("leftoverResumeUnavailable"));
      return;
    }
    const resuming = resumableDraft;
    if (automaticContinuationsEnabled) {
      if (!continuationBudgetInputValid) {
        setContinuationReadinessError(t("continuationBudgetInvalid"));
        return;
      }
      if (!continuationReadinessIsCurrent) {
        await inspectContinuationReadiness();
        return;
      }
      if (!continuationReadiness?.token_bound_known) {
        setContinuationReadinessError(t("continuationTokenBoundMissing"));
        return;
      }
      if (!automaticContinuationsConfirmed) {
        setContinuationReadinessError(t("continuationConfirmationMissing"));
        return;
      }
    }
    if (resuming && stream.hasUncertainAttempt && !uncertainRetryArmed) {
      setUncertainRetryArmed(true);
      return;
    }
    const confirmUncertainRetry = Boolean(
      stream.hasUncertainAttempt && uncertainRetryArmed,
    );
    setUncertainRetryArmed(false);
    setActionError("");
    if (selectedInitialRun) setInitialRunResolved(true);
    setContinuationReadinessError("");
    void stream.start({
      novel_id: novelId,
      chapter_id: chapterId,
      ...(resuming
        ? {
            resume_run_id: stream.runId,
            expected_run_revision: stream.runRevision,
            confirm_uncertain_retry: confirmUncertainRetry,
          }
        : {}),
      ...toRequestParams(params),
      prose_continuation_policy: continuationPolicy,
      ...(automaticContinuationsEnabled
        ? {
            token_budget: continuationBudgetValue,
            readiness_digest: continuationReadiness?.authorization.readiness_digest,
            confirm_automatic_continuations: true,
          }
        : {}),
    });
  };

  const discard = async () => {
    if (runActionsBlocked) return;
    const runId = stream.runId;
    const runRevision = stream.runRevision;
    setActionError("");
    setSyncNotice("");
    if (runId) {
      if (runRevision == null) {
        setActionError(t("runMissing"));
        return;
      }
      const expectedRunRevision = runRevision;
      setDiscarding(true);
      try {
        const outcome = await submitProseRunMutation({
          mutate: () => apiPost(
            `/api/llm/prose-runs/${runId}/discard`,
            buildProseDiscardPayload({
              novelId,
              chapterId,
              runRevision: expectedRunRevision,
            }),
          ),
          refresh: () => restoreActive({ clearWhenMissing: true }),
        });
        if (outcome.status !== "success") {
          setInitialRunResolved(true);
          if (proseRunConfirmationResetRequired(outcome.status)) {
            resetRunConfirmationLocks();
          }
          if (outcome.status === "conflict_refreshed") {
            setSyncNotice(t("runConflictRefreshed"));
          } else {
            setConflictRequiresSync(true);
            setSyncError(t("runConflictRefreshFailed"));
          }
          return;
        }
      } catch (error) {
        setActionError(error instanceof Error ? error.message : String(error));
        return;
      } finally {
        setDiscarding(false);
      }
    }
    // 保险栓是面板本地状态，不属于 stream，stream.reset() 清不到它——
    // 两边要一起复位，否则会同屏出现"空状态提示"与"覆盖警告"互相矛盾的界面。
    resetStream();
    setOverwriteArmed(false);
    setPartialArmed(false);
    setUncertainRetryArmed(false);
    setInitialRunResolved(true);
    onRunStateChanged?.();
  };

  const reasonLabel = (reasonCode: string) => {
    const key = proseReasonTranslationKey(reasonCode);
    return key
      ? t(`reasons.${key}`)
      : t("reasons.unknown", { code: reasonCode });
  };

  const advisoryLabel = (advisoryCode: string) => {
    const key = proseAdvisoryTranslationKey(advisoryCode);
    return key
      ? t(`advisories.${key}`)
      : t("advisories.unknown");
  };

  const resumeUnavailableMessage = selectedInitialRun?.status === "stale"
    ? t("leftoverResumeStale")
    : selectedInitialRun?.status === "superseded"
        ? t("leftoverResumeSuperseded")
        : t("leftoverResumeUnavailable");

  const handleDialogKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      onClose();
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = Array.from(
      dialogRef.current?.querySelectorAll<HTMLElement>(
        'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ) ?? [],
    ).filter((element) => (
      element.getAttribute("aria-hidden") !== "true"
      && element.getClientRects().length > 0
    ));
    if (focusable.length === 0) {
      event.preventDefault();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    const active = document.activeElement;
    if (event.shiftKey && (active === first || !dialogRef.current?.contains(active))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    }
  };

  return (
    <div className="absolute inset-0 z-30 flex items-center justify-center bg-black/25 px-4 py-6">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="prose-panel-title"
        onKeyDown={handleDialogKeyDown}
        className="flex max-h-full w-full max-w-5xl flex-col rounded-md border border-border bg-surface shadow-lg"
      >
        <header className="flex flex-wrap items-start justify-between gap-3 border-b border-border px-5 py-4">
          <div className="min-w-0">
            <h3
              id="prose-panel-title"
              className="text-base font-semibold text-foreground"
            >
              {t("title")}
            </h3>
            <p className="mt-1 text-xs leading-5 text-muted">{t("description")}</p>
          </div>
          <div className="flex shrink-0 gap-2">
            {running ? (
              <Button variant="outline" size="sm" onPress={stream.cancel}>
                {t("cancel")}
              </Button>
            ) : (
              <Button
                variant="primary"
                size="sm"
                className="bg-accent text-white hover:bg-accent-hover"
                onPress={() => void startGeneration()}
                isDisabled={
                  runActionsBlocked
                  || selectedInitialRun?.can_resume === false
                  || (automaticContinuationsEnabled && continuationReadinessLoading)
                }
              >
                {selectedInitialRun?.can_resume === false
                  ? t("resumeUnavailable")
                  : resumableDraft
                    ? t("resume")
                    : hasText
                      ? t("regenerate")
                      : t("generate")}
              </Button>
            )}
            <Button variant="ghost" size="sm" onPress={onClose}>
              {t("close")}
            </Button>
          </div>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          {(restoreLoading || syncNotice || syncError) && (
            <section
              data-testid="prose-run-sync-state"
              aria-busy={restoreLoading}
              aria-live="polite"
              className="mb-4 grid gap-2"
            >
              {restoreLoading && (
                <Notice tone="info">
                  <span role="status">{t("syncingRun")}</span>
                </Notice>
              )}
              {syncNotice && (
                <Notice tone="warning">
                  <span role="status">{syncNotice}</span>
                </Notice>
              )}
              {syncError && (
                <Notice tone="error">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span role="alert">{syncError}</span>
                    {manualSyncRequired && (
                      <Button
                        variant="outline"
                        size="sm"
                        onPress={() => void retryRunSync()}
                        isDisabled={restoreLoading}
                      >
                        {t("retrySync")}
                      </Button>
                    )}
                  </div>
                </Notice>
              )}
            </section>
          )}
          <div className="mb-4 grid gap-3">
            <ProseContinuationControls
              idPrefix="single-prose"
              value={continuationPolicy}
              onChange={setContinuationPolicy}
              disabled={running || runActionsBlocked}
            />
            <OutlineGenerationParams
              value={params}
              onChange={setParams}
              showSystemPrompt={false}
            />
            {automaticContinuationsEnabled && (
              <section
                aria-labelledby="single-prose-readiness-title"
                className="grid gap-3 rounded-md border border-border bg-background p-3"
              >
                <div>
                  <h4
                    id="single-prose-readiness-title"
                    className="text-sm font-semibold text-foreground"
                  >
                    {t("continuationReadinessTitle")}
                  </h4>
                  <p className="mt-1 text-xs leading-5 text-muted">
                    {t("continuationReadinessDescription")}
                  </p>
                </div>

                <label className="grid gap-1 text-sm">
                  <span className="text-xs font-medium text-muted">
                    {t("continuationBudgetLabel")}
                  </span>
                  <input
                    type="number"
                    min={1}
                    inputMode="numeric"
                    value={continuationBudget}
                    disabled={running || runActionsBlocked}
                    onChange={(event) => setContinuationBudget(event.target.value)}
                    placeholder={t("continuationBudgetPlaceholder")}
                    aria-invalid={!continuationBudgetInputValid}
                    className="min-h-9 w-full rounded-md border border-border bg-surface px-3 py-2 text-sm text-foreground outline-none focus:border-accent disabled:cursor-not-allowed disabled:opacity-60"
                  />
                  <span className="text-xs leading-5 text-muted">
                    {t("continuationBudgetHint")}
                  </span>
                </label>

                <div className="flex flex-wrap items-center gap-2">
                  <button
                    type="button"
                    onClick={() => void inspectContinuationReadiness()}
                    disabled={
                      running
                      || runActionsBlocked
                      || continuationReadinessLoading
                      || !continuationBudgetInputValid
                    }
                    className="min-h-9 rounded-md border border-border px-3 py-2 text-sm font-medium text-foreground hover:bg-surface disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    {continuationReadinessLoading
                      ? t("continuationInspecting")
                      : t("continuationInspect")}
                  </button>
                  {!continuationReadinessIsCurrent && !continuationReadinessLoading && (
                    <p role="status" className="text-xs leading-5 text-muted">
                      {t("continuationReadinessRequired")}
                    </p>
                  )}
                </div>

                {continuationReadinessIsCurrent && continuationReadiness && (
                  <div className="grid gap-2 rounded-md border border-border bg-surface p-3 text-xs leading-5 text-muted">
                    <p>
                      {t("continuationReadinessCalls", {
                        base: continuationReadiness.authorization.max_base_calls,
                        automatic: continuationReadiness.authorization.max_automatic_continuation_calls,
                        total: continuationReadiness.authorization.max_logical_prose_calls,
                      })}
                    </p>
                    <p>
                      {t("continuationReadinessBudget", {
                        bound: continuationReadiness.authorization.conservative_token_bound,
                        budget: continuationReadiness.authorization.token_budget ?? t("continuationUnknown"),
                      })}
                    </p>
                    <p>
                      {t("continuationReadinessProvider", {
                        provider: continuationReadiness.provider.alias,
                        model: continuationReadiness.provider.model,
                        tokens: continuationReadiness.provider.max_output_tokens ?? t("continuationUnknown"),
                      })}
                    </p>
                    {!continuationReadiness.token_bound_known && (
                      <p className="font-medium text-amber-800 dark:text-amber-200">
                        {t("continuationTokenBoundMissing")}
                      </p>
                    )}
                    {continuationReadiness.warnings.includes(
                      "automatic_token_budget_requires_confirmation",
                    ) && (
                      <p className="font-medium text-amber-800 dark:text-amber-200">
                        {t("continuationAutomaticBudgetNotice", {
                          budget: continuationReadiness.authorization.token_budget
                            ?? t("continuationUnknown"),
                        })}
                      </p>
                    )}
                  </div>
                )}

                <label className="flex cursor-pointer items-start gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={automaticContinuationsConfirmed}
                    disabled={
                      running
                      || runActionsBlocked
                      || !continuationReadinessIsCurrent
                      || !continuationReadiness?.token_bound_known
                    }
                    onChange={(event) => setAutomaticContinuationsConfirmed(event.target.checked)}
                    className="mt-0.5 size-4"
                  />
                  <span className="text-xs leading-5 text-muted">
                    {continuationReadiness?.warnings.includes(
                      "automatic_token_budget_requires_confirmation",
                    )
                      ? t("continuationConfirmationWithSystemBudget", {
                          count: continuationPolicy.automatic_continuations_per_scene,
                          budget: continuationReadiness.authorization.token_budget
                            ?? t("continuationUnknown"),
                        })
                      : t("continuationConfirmation", {
                          count: continuationPolicy.automatic_continuations_per_scene,
                        })}
                  </span>
                </label>
              </section>
            )}
          </div>

          <ContextNotices report={stream.contextReport} />
          {hasText && !partialAcceptance && (
            <Notice tone="info">
              <section
                data-testid="interactive-completion-readiness"
                className="grid min-w-0 gap-2"
              >
                <div>
                  <p className="font-medium text-foreground">
                    {t("completionReadinessTitle")}
                  </p>
                  <p className="mt-1 text-xs leading-5 text-muted">
                    {t("completionReadinessDescription")}
                  </p>
                </div>
                {completionReadinessLoading && (
                  <p role="status" className="text-xs text-muted">
                    {t("completionInspecting")}
                  </p>
                )}
                {completionReadiness && (
                  <>
                    <div className="grid gap-1 rounded-md border border-border bg-surface p-3 text-xs leading-5 text-muted sm:grid-cols-2">
                      <p>
                        {t("completionReadinessCalls", {
                          logical: completionReadiness.logical_call_count,
                          maximum: completionReadiness.maximum_paid_attempts,
                        })}
                      </p>
                      <p>
                        {t("completionReadinessTokens", {
                          count: completionReadiness.conservative_token_bound,
                        })}
                      </p>
                      <p>
                        {t("completionReadinessExternalization", {
                          bytes: completionReadiness.externalized_prose_utf8_bytes,
                        })}
                      </p>
                      <p className="min-w-0 break-words">
                        {t("completionReadinessProviders", {
                          providers: completionReadiness.provider_bounds
                            .map((item) => item.provider_alias)
                            .join(", "),
                        })}
                      </p>
                      <p className="min-w-0 break-words sm:col-span-2">
                        {t("completionReadinessCost", {
                          costs: completionReadiness.provider_bounds.some(
                            (item) => item.pricing_status === "available",
                          )
                            ? completionReadiness.provider_bounds
                              .filter((item) => item.pricing_status === "available")
                              .map((item) => (
                                `${item.provider_alias} ${item.currency} ${item.maximum_cost}`
                              ))
                              .join("；")
                            : t("completionReadinessCostUnavailable"),
                        })}
                      </p>
                      <p className="min-w-0 break-words sm:col-span-2">
                        {t("completionReadinessPricingBasis", {
                          basis: completionReadiness.provider_bounds.some(
                            (item) => item.pricing_status === "available",
                          )
                            ? completionReadiness.provider_bounds
                              .filter((item) => item.pricing_status === "available")
                              .map((item) => (
                                `${item.provider_alias}: ${item.pricing_basis}`
                              ))
                              .join("；")
                            : t("completionReadinessCostUnavailable"),
                        })}
                      </p>
                    </div>
                    {completionReadiness.warnings.length > 0 && (
                      <p className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-900 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-200">
                        {t("completionReadinessPricingUnavailable", {
                          providers: completionReadiness.warnings
                            .map((warning) => warning.provider_alias)
                            .join(", "),
                        })}
                      </p>
                    )}
                    <label className="flex cursor-pointer items-start gap-2">
                      <input
                        type="checkbox"
                        checked={completionReadinessConfirmed}
                        disabled={accepting || runActionsBlocked}
                        onChange={(event) => {
                          setCompletionReadinessConfirmed(event.target.checked);
                          setActionError("");
                        }}
                        className="mt-0.5 size-4 shrink-0"
                      />
                      <span className="text-xs leading-5 text-muted">
                        {completionReadiness.warnings.length > 0
                          ? t("completionReadinessConfirmationWithUnknownPricing")
                          : t("completionReadinessConfirmation")}
                      </span>
                    </label>
                  </>
                )}
              </section>
            </Notice>
          )}
          {stream.error && <Notice tone="error">{stream.error}</Notice>}
          {actionError && <Notice tone="error">{actionError}</Notice>}
          {completionUncertain && completionReadiness && (
            <Notice tone="warning">
              <section className="grid min-w-0 gap-3">
                <div>
                  <p className="font-medium text-foreground">
                    {t("completionUncertainTitle")}
                  </p>
                  <p className="mt-1 text-xs leading-5 text-muted">
                    {t("completionUncertainDescription")}
                  </p>
                </div>
                <div className="flex flex-wrap gap-2">
                  <Button
                    variant="primary"
                    size="sm"
                    onPress={() => void resolveCompletionUncertainty("retry")}
                    isDisabled={completionResolutionLoading}
                  >
                    {completionResolutionLoading
                      ? t("completionUncertainResolving")
                      : t("completionUncertainRetry")}
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    onPress={() => void resolveCompletionUncertainty("abort")}
                    isDisabled={completionResolutionLoading}
                  >
                    {t("completionUncertainAbort")}
                  </Button>
                </div>
              </section>
            </Notice>
          )}
          {continuationReadinessError && (
            <Notice tone="error">{continuationReadinessError}</Notice>
          )}
          {incomplete && !selectedInitialRun && (
            <Notice tone="warning">{t("incomplete")}</Notice>
          )}
          {selectedInitialRun?.can_resume && (
            <Notice tone="warning">{t("leftoverResumeAvailable")}</Notice>
          )}
          {reasonCodes.length > 0 && (
            <Notice tone="warning">
              <span className="font-medium">{t("reasonTitle")}</span>
              <ul className="mt-1 list-disc space-y-1 ps-5">
                {reasonCodes.map((reasonCode) => (
                  <li key={reasonCode}>{reasonLabel(reasonCode)}</li>
                ))}
              </ul>
            </Notice>
          )}
          {advisoryCodes.length > 0 && (
            <Notice tone="info">
              <section
                data-testid="prose-length-advisories"
                aria-live="polite"
                className="min-w-0"
              >
                <p className="font-medium">{t("advisoryTitle")}</p>
                <ul className="mt-1 list-disc space-y-1 ps-5">
                  {advisoryCodes.map((advisoryCode) => (
                    <li key={advisoryCode} className="break-words">
                      {advisoryLabel(advisoryCode)}
                    </li>
                  ))}
                </ul>
              </section>
            </Notice>
          )}
          {selectedInitialRun?.can_resume === false && (
            <Notice tone="warning">{resumeUnavailableMessage}</Notice>
          )}
          {selectedInitialRun?.can_accept_partial === false && (
            <Notice tone="warning">{t("leftoverAcceptUnavailable")}</Notice>
          )}
          {partialArmed && <Notice tone="warning">{t("partialConfirm")}</Notice>}
          {uncertainRetryArmed && (
            <Notice tone="warning">{t("uncertainRetryConfirm")}</Notice>
          )}
          {overwriteArmed && <Notice tone="warning">{t("overwriteArm")}</Notice>}
          {stream.executionPlan?.mode === "scene_segments" && (
            <Notice tone="warning">
              {t("segmentedPlan", {
                scenes: stream.executionPlan.scene_count,
                calls: stream.executionPlan.call_count,
              })}
            </Notice>
          )}
          {stream.completion && (
            <div className="mb-3 grid gap-1 rounded-md border border-border bg-background px-3 py-2 text-xs text-muted sm:grid-cols-3">
              <span>
                {t("completionWords", {
                  actual: stream.completion.actual_word_count,
                  requested: stream.completion.requested_word_count,
                })}
              </span>
              <span>
                {t("completionScenes", {
                  completed: stream.completion.completed_scene_count,
                  total: stream.completion.scene_count,
                })}
              </span>
              <span>
                {t("completionFinish", {
                  reason: t(`reasons.${finishReasonTranslationKey(
                    stream.completion.finish_reason,
                  )}`),
                })}
              </span>
            </div>
          )}

          {!hasText && running && (
            <p className="py-10 text-center text-sm text-muted">{t("generating")}</p>
          )}
          {!hasText && !running && (
            <p className="py-10 text-center text-sm text-muted">{t("emptyPreview")}</p>
          )}

          {hasText && (
            <>
              <div className="mb-3 flex flex-wrap items-center gap-2">
                <span className="rounded-md border border-border bg-background px-2 py-1 text-xs tabular-nums text-muted">
                  {t("charCount", { count: countChapterWords(stream.text) })}
                </span>
                {/*
                  用量只在成功的 done 帧里到货；取消或失败时根本没有那一帧，
                  usage 保持 null、这里什么都不渲染。设计 §7.1 要求"如实报 0 或不报，
                  绝不编造估算值"——不渲染正是"不报"。
                */}
                {stream.usage && (
                  <span className="rounded-md border border-border bg-background px-2 py-1 text-xs tabular-nums text-muted">
                    {t("tokenUsage", {
                      total: stream.usage.total_tokens,
                      input: stream.usage.input_tokens,
                      output: stream.usage.output_tokens,
                    })}
                  </span>
                )}
              </div>
              {/*
                预览区刻意**只读**：流式写入与人工编辑并存必然打架。
                要改就先接受、改在编辑器里——那才是编辑正文的地方（设计 §3.2）。
              */}
              <div className="whitespace-pre-wrap rounded-md border border-border bg-background px-4 py-3 text-[15px] leading-8 text-foreground">
                {stream.text}
              </div>
            </>
          )}
        </div>

        <footer className="flex flex-wrap justify-end gap-2 border-t border-border px-5 py-3">
          <Button
            variant="ghost"
            size="sm"
            onPress={() => void discard()}
            isDisabled={
              (!hasText && !stream.runId)
              || (Boolean(stream.runId) && stream.runRevision == null)
              || running
              || runActionsBlocked
              || selectedInitialRun?.can_discard === false
            }
          >
            {discarding ? t("discarding") : t("discard")}
          </Button>
          <Button
            variant="primary"
            size="sm"
            className="bg-accent text-white hover:bg-accent-hover"
            onPress={() => void accept()}
            isDisabled={
              !hasText
              || running
              || runActionsBlocked
              || completionUncertain
              || selectedInitialRun?.can_accept_partial === false
              || (
                !partialAcceptance
                && Boolean(completionReadiness)
                && !completionReadinessConfirmed
              )
            }
          >
            {accepting
              ? t(partialAcceptance ? "accepting" : "completionFinalizing")
              : completionReadinessLoading
                ? t("completionInspecting")
              : partialAcceptance && !partialArmed
                ? t("acceptPartial")
                : overwriteArmed && !completionReadiness
                  ? t("overwriteConfirm")
                  : !partialAcceptance && !completionReadiness
                    ? t("completionInspect")
                    : !partialAcceptance
                      ? t("completionFinalize")
                      : t("accept")}
          </Button>
        </footer>
      </div>
    </div>
  );
}
