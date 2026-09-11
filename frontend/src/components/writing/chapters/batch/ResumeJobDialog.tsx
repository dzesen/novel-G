"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@heroui/react";
import { ApiError, apiPost } from "@/lib/api";
import {
  DEFAULT_PROSE_CONTINUATION_POLICY,
  parsePositiveInteger,
} from "../prose/proseContinuation";
import type {
  GenerationJob,
  GenerationReadiness,
} from "./batchTypes";
import {
  type ReferenceCardType,
} from "./referenceCardAutoCreation";
import { referenceCardTypeTranslationKey } from "./referenceCardAutoCreationPresentation";
import { readinessAllowsStart } from "./readinessPresentation";
import PauseResolutionLinks from "./PauseResolutionLinks";
import { readinessIssueCopy } from "./readinessIssuePresentation";

interface ResumeJobDialogProps {
  job: GenerationJob;
  onSubmitted: (job: GenerationJob) => void;
  onClose: () => void;
}

export function isResumeReadinessRequired(error: unknown): boolean {
  if (!(error instanceof ApiError) || !error.detail || typeof error.detail !== "object") {
    return false;
  }
  return (error.detail as { code?: unknown }).code === "resume_readiness_required";
}

export default function ResumeJobDialog({
  job,
  onSubmitted,
  onClose,
}: ResumeJobDialogProps) {
  const t = useTranslations("writing.batch");
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const [tokenBudget, setTokenBudget] = useState(
    job.token_budget == null ? "" : String(job.token_budget),
  );
  const continuationPolicy = job.prose_continuation_authorization?.policy
    ?? job.generation_params?.prose_continuation_policy
    ?? DEFAULT_PROSE_CONTINUATION_POLICY;
  const [readiness, setReadiness] = useState<GenerationReadiness | null>(null);
  const [readinessConfiguration, setReadinessConfiguration] = useState<string | null>(null);
  const [acknowledgedCodes, setAcknowledgedCodes] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [readinessError, setReadinessError] = useState("");
  const [submissionError, setSubmissionError] = useState("");
  const parsedTokenBudget = parsePositiveInteger(tokenBudget);
  const configurationKey = useMemo(() => JSON.stringify({
    tokenBudget: parsedTokenBudget,
  }), [parsedTokenBudget]);
  const readinessIsCurrent = Boolean(readiness)
    && readinessConfiguration === configurationKey;
  const requiresProposedBudget = job.pause_reason === "cost_cap";

  const loadReadiness = useCallback(async () => {
    setLoading(true);
    setReadinessError("");
    try {
      const report = await apiPost<GenerationReadiness>(
        `/api/generation-jobs/${job._id}/readiness`,
        parsedTokenBudget === null ? {} : { token_budget: parsedTokenBudget },
      );
      setReadiness(report);
      setReadinessConfiguration(configurationKey);
      setAcknowledgedCodes(new Set());
    } catch (loadError) {
      setReadiness(null);
      setReadinessConfiguration(null);
      setReadinessError(loadError instanceof Error ? loadError.message : String(loadError));
    } finally {
      setLoading(false);
    }
  }, [configurationKey, job._id, parsedTokenBudget]);

  useEffect(() => {
    void loadReadiness();
  }, [loadReadiness]);

  useEffect(() => {
    closeButtonRef.current?.focus();
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !submitting) onClose();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [onClose, submitting]);

  const submit = async () => {
    if (
      (requiresProposedBudget && parsedTokenBudget === null)
      || !readiness
      || !readinessIsCurrent
      || !readinessAllowsStart(
        readiness,
        acknowledgedCodes,
      )
    ) return;
    setSubmitting(true);
    setSubmissionError("");
    try {
      const resumed = await apiPost<GenerationJob>(
        `/api/generation-jobs/${job._id}/resume`,
        {
          ...(parsedTokenBudget === null ? {} : { token_budget: parsedTokenBudget }),
          readiness_digest: readiness.digest,
          acknowledged_warning_codes: [...acknowledgedCodes].sort(),
        },
      );
      onSubmitted(resumed);
    } catch (submitError) {
      setSubmissionError(
        submitError instanceof Error ? submitError.message : String(submitError),
      );
      await loadReadiness();
    } finally {
      setSubmitting(false);
    }
  };

  const authorization = readiness?.planning.prose_continuation_authorization;
  const referenceCardPolicy = readiness?.planning.reference_card_auto_creation_policy;
  const visibleError = submissionError || readinessError;
  const referenceCardTypeLabel = (cardType: ReferenceCardType) =>
    t(referenceCardTypeTranslationKey(cardType));

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/35 px-3 py-4 sm:px-4 sm:py-6">
      <section
        role="dialog"
        aria-modal="true"
        aria-labelledby="resume-readiness-title"
        className="flex max-h-[calc(100dvh-2rem)] w-full max-w-xl flex-col overflow-hidden rounded-md border border-border bg-surface shadow-lg sm:max-h-[calc(100dvh-3rem)]"
      >
        <header className="flex items-start justify-between gap-3 border-b border-border px-4 py-3 sm:px-5 sm:py-4">
          <div className="min-w-0">
            <h3 id="resume-readiness-title" className="text-base font-semibold text-foreground">
              {t("resumeReadinessTitle")}
            </h3>
            <p className="mt-1 text-xs leading-5 text-muted">
              {t(
                job.pause_reason === "source_changed"
                  ? "resumeReadinessSourceChangedDescription"
                  : "resumeReadinessDescription",
              )}
            </p>
          </div>
          <button
            ref={closeButtonRef}
            type="button"
            onClick={onClose}
            disabled={submitting}
            aria-label={t("dialogCancel")}
            className="min-h-10 shrink-0 rounded-md px-2 text-xs font-medium text-muted hover:bg-background hover:text-foreground disabled:opacity-60"
          >
            {t("dialogCancel")}
          </button>
        </header>

        <div className="grid min-w-0 gap-4 overflow-y-auto px-4 py-4 sm:px-5">
          <div className="grid gap-2 rounded-md border border-border bg-background p-3 text-xs leading-5 text-muted sm:grid-cols-2">
            <p>{t("resumeReadinessCurrentUsage", {
              used: job.tokens_used,
              reserved: job.tokens_reserved ?? 0,
            })}</p>
            <p>{t("resumeReadinessCurrentBudget", {
              budget: job.token_budget ?? t("readinessNone"),
            })}</p>
            <p className="sm:col-span-2">{t("resumeReadinessFeeUnavailable")}</p>
          </div>

          <label className="grid gap-1 text-sm">
            <span className="text-xs font-medium text-muted">{t("resumeReadinessBudgetLabel")}</span>
            <input
              id="resume-token-budget"
              type="number"
              min={1}
              inputMode="numeric"
              value={tokenBudget}
              onChange={(event) => {
                setTokenBudget(event.target.value);
                setSubmissionError("");
              }}
              placeholder={t("dialogTokenPlaceholder")}
              disabled={submitting}
              className="min-h-10 w-full rounded-md border border-border bg-background px-3 py-2 text-base text-foreground outline-none focus:border-accent disabled:opacity-60 sm:text-sm"
            />
            <span className="text-xs leading-5 text-muted">{t("resumeReadinessBudgetHint")}</span>
          </label>

          <section aria-labelledby="resume-readiness-summary-title" className="border-t border-border pt-4">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <h4 id="resume-readiness-summary-title" className="text-sm font-semibold text-foreground">
                  {t("resumeReadinessSummaryTitle")}
                </h4>
                <p className="mt-1 text-xs leading-5 text-muted">
                  {t("resumeReadinessSummaryDescription")}
                </p>
              </div>
              {!loading && (
                <button
                  type="button"
                  onClick={() => void loadReadiness()}
                  className="min-h-9 shrink-0 text-xs font-medium text-accent hover:underline"
                >
                  {t("readinessRefresh")}
                </button>
              )}
            </div>

            {loading && (
              <p role="status" className="mt-3 text-sm text-muted">{t("resumeReadinessLoading")}</p>
            )}
            {readiness && !loading && !readinessIsCurrent && (
              <p role="status" className="mt-3 text-xs leading-5 text-amber-800 dark:text-amber-200">
                {t("readinessSettingsChanged")}
              </p>
            )}
            {readiness && !loading && (
              <div className="mt-3 grid gap-3">
                {authorization && (
                  <div className="grid gap-1 rounded-md border border-border bg-background p-3 text-xs leading-5 text-muted">
                    <p>{t("readinessContinuationCalls", {
                      base: authorization.max_base_calls,
                      automatic: authorization.max_automatic_continuation_calls,
                      total: authorization.max_logical_prose_calls,
                    })}</p>
                    <p>{t("readinessContinuationBudget", {
                      bound: authorization.conservative_token_bound,
                      budget: authorization.token_budget ?? t("readinessNone"),
                    })}</p>
                    <p>{t("resumeReadinessTokenUpper", {
                      total: authorization.conservative_total_token_bound
                        ?? authorization.conservative_token_bound,
                      perCall: authorization.conservative_token_bound,
                    })}</p>
                    <p>{t("resumeReadinessRevision", {
                      revision: authorization.authorization_revision,
                    })}</p>
                  </div>
                )}

                {referenceCardPolicy && (
                  <div className="rounded-md border border-border bg-background p-3 text-xs leading-5 text-muted">
                    {t("readinessAutoCardsSummary", {
                      status: referenceCardPolicy.enabled
                        ? t("readinessAutoCardsEnabled")
                        : t("readinessAutoCardsDisabled"),
                      types: referenceCardPolicy.allowed_card_types
                        .map(referenceCardTypeLabel)
                        .join(t("referenceCardNameSeparator")),
                      perChapter: referenceCardPolicy.max_auto_creates_per_chapter,
                      perBook: referenceCardPolicy.max_auto_creates_per_book,
                      repair: referenceCardPolicy.max_candidate_repair_cycles_per_chapter,
                    })}
                  </div>
                )}

                {readiness.issues.map((issue) => {
                  const copy = readinessIssueCopy(
                    issue,
                    t,
                    continuationPolicy.automatic_continuations_per_scene,
                  );
                  const blocked = issue.level === "blocked";
                  const requiresAck = issue.level === "warning_requires_ack";
                  return (
                    <div
                      key={issue.code}
                      className={blocked
                        ? "rounded-md border border-red-300 bg-red-50 px-3 py-2.5 dark:border-red-900 dark:bg-red-950/40"
                        : "rounded-md border border-amber-300 bg-amber-50 px-3 py-2.5 dark:border-amber-900 dark:bg-amber-950/30"}
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
                            className="mt-0.5 size-4 shrink-0"
                          />
                          <span>{issue.code === "automatic_continuations_require_confirmation"
                            ? t("readinessAcknowledgeAutomatic", {
                                count: continuationPolicy.automatic_continuations_per_scene,
                              })
                            : issue.code === "automatic_reference_card_creation_requires_confirmation"
                              ? t("readinessAcknowledgeAutoCards")
                            : issue.code === "prose_output_risk_requires_ack"
                              ? t("readinessAcknowledgeOutputRisk")
                              : issue.code === "character_cards_missing"
                                ? t("readinessAcknowledge")
                                : t("resumeReadinessAcknowledge")}</span>
                        </label>
                      )}
                      <div className="mt-2">
                        <PauseResolutionLinks job={job} issue={issue} onNavigate={onClose} />
                        {issue.action_codes.some((code) => ["review_token_budget", "set_token_budget"].includes(code)) && (
                          <button type="button" onClick={() => document.getElementById("resume-token-budget")?.focus()}
                            className="min-h-10 text-sm font-semibold text-accent underline">{t("resumeReadinessBudgetLabel")}</button>
                        )}
                      </div>
                    </div>
                  );
                })}

                {readiness.issues.length === 0 && (
                  <p className="rounded-md border border-green-300 bg-green-50 px-3 py-2 text-sm text-green-800 dark:border-green-900 dark:bg-green-950/40 dark:text-green-200">
                    {t("resumeReadinessReady")}
                  </p>
                )}
              </div>
            )}
          </section>

          {visibleError && (
            <p role="alert" className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm leading-5 text-red-700 dark:border-red-900/60 dark:bg-red-950/40 dark:text-red-300">
              {visibleError}
            </p>
          )}
        </div>

        <footer className="flex flex-wrap justify-end gap-2 border-t border-border px-4 py-3 sm:px-5">
          <Button variant="ghost" size="sm" onPress={onClose} isDisabled={submitting}>
            {t("dialogCancel")}
          </Button>
          <Button
            variant="primary"
            size="sm"
            className="bg-accent text-white hover:bg-accent-hover"
            onPress={() => void submit()}
            isDisabled={
              submitting
              || loading
              || !readiness
              || !readinessIsCurrent
              || (requiresProposedBudget && parsedTokenBudget === null)
              || !readinessAllowsStart(readiness, acknowledgedCodes)
            }
          >
            {submitting ? t("resuming") : t("resumeReadinessConfirm")}
          </Button>
        </footer>
      </section>
    </div>
  );
}
