"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@heroui/react";
import { apiPost } from "@/lib/api";
import OutlineGenerationParams, {
  EMPTY_GENERATION_PARAMS,
  toRequestParams,
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
  GenerationJob,
  GenerationReadiness,
  OutlineDeviationPolicy,
} from "./batchTypes";
import {
  buildAuthorizedStartPayload,
  readinessAllowsStart,
} from "./readinessPresentation";
import { readinessIssueCopy } from "./readinessIssuePresentation";

interface StartJobDialogProps {
  scope: "volume" | "book";
  targetId: string;         // volume: volume_id；book: novel_id
  title: string;            // 对话框标题（调用方按 scope 解析好的 i18n 文案）
  targetHeading: string;    // 目标区小标题（"目标卷" / "目标"）
  targetLabel: string;      // 目标展示名（卷名 / "全书"）
  fillableCount: number;
  onSubmitted: (job: GenerationJob) => void;
  onClose: () => void;
  onNavigateToReferenceCards: () => void;
}

export default function StartJobDialog({
  scope,
  targetId,
  title,
  targetHeading,
  targetLabel,
  fillableCount,
  onSubmitted,
  onClose,
  onNavigateToReferenceCards,
}: StartJobDialogProps) {
  const t = useTranslations("writing.batch");
  const firstInputRef = useRef<HTMLInputElement>(null);
  const [checkpointInterval, setCheckpointInterval] = useState(5);
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
  const readinessConfigurationKey = JSON.stringify({
    continuationPolicy,
    referenceCardAutoCreationPolicy,
    tokenBudget: parsedTokenBudget,
    generationParams,
  });
  const [submitting, setSubmitting] = useState(false);
  const [readiness, setReadiness] = useState<GenerationReadiness | null>(null);
  const [readinessLoading, setReadinessLoading] = useState(true);
  const [readinessConfiguration, setReadinessConfiguration] = useState<string | null>(null);
  const readinessIsCurrent = Boolean(readiness)
    && readinessConfiguration === readinessConfigurationKey;
  const [acknowledgedCodes, setAcknowledgedCodes] = useState<Set<string>>(new Set());
  const [error, setError] = useState("");

  const loadReadiness = useCallback(async () => {
    setReadinessLoading(true);
    setError("");
    try {
      const report = await apiPost<GenerationReadiness>(
        `/api/generation-jobs/${scope}/${targetId}/readiness`,
        {
          token_budget: parsedTokenBudget,
          prose_continuation_policy: continuationPolicy,
          reference_card_auto_creation_policy: referenceCardAutoCreationPolicy,
          ...toRequestParams(generationParams),
        },
      );
      setReadiness(report);
      setReadinessConfiguration(readinessConfigurationKey);
      setAcknowledgedCodes(new Set());
    } catch (err) {
      setReadiness(null);
      setReadinessConfiguration(null);
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setReadinessLoading(false);
    }
  }, [
    continuationPolicy,
    generationParams,
    parsedTokenBudget,
    referenceCardAutoCreationPolicy,
    readinessConfigurationKey,
    scope,
    targetId,
  ]);

  useEffect(() => {
    void loadReadiness();
  }, [loadReadiness]);

  useEffect(() => {
    firstInputRef.current?.focus();
  }, []);

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !submitting) onClose();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [onClose, submitting]);

  const referenceCardTypeLabel = (cardType: ReferenceCardType) =>
    t(referenceCardTypeTranslationKey(cardType));

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
        checkpointInterval,
        tokenBudget: parsedTokenBudget,
        readiness,
        acknowledgedCodes,
        outlineDeviationPolicy,
        generationParams: toRequestParams(generationParams),
        proseContinuationPolicy: continuationPolicy,
        referenceCardAutoCreationPolicy,
      });
      const job = await apiPost<GenerationJob>(
        `/api/generation-jobs/${scope}/${targetId}`,
        payload,
      );
      onSubmitted(job);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      await loadReadiness();
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/25 px-3 py-4 sm:px-4 sm:py-6">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="start-generation-title"
        className="flex max-h-[calc(100dvh-2rem)] w-full max-w-2xl flex-col overflow-hidden rounded-md border border-border bg-surface shadow-lg sm:max-h-[calc(100dvh-3rem)]"
      >
        <header className="border-b border-border px-4 py-3 sm:px-5 sm:py-4">
          <h3 id="start-generation-title" className="break-words text-base font-semibold text-foreground">{title}</h3>
        </header>

        <div className="grid min-w-0 gap-4 overflow-y-auto px-4 py-4 sm:px-5">
          <div className="grid gap-1 text-sm">
            <span className="text-xs font-medium text-muted">{targetHeading}</span>
            <div className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-1 rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground">
              <span className="min-w-0 break-words font-medium">{targetLabel}</span>
              <span className="text-xs text-muted">{t("dialogFillable", { count: fillableCount })}</span>
            </div>
          </div>

          <label className="grid gap-1 text-sm">
            <span className="text-xs font-medium text-muted">{t("dialogCheckpointLabel")}</span>
            <input
              type="number"
              ref={firstInputRef}
              min={1}
              max={1000}
              value={checkpointInterval}
              onChange={(e) => setCheckpointInterval(Number(e.target.value))}
              className="min-h-10 w-full rounded-md border border-border bg-background px-3 py-2 text-base text-foreground outline-none focus:border-accent sm:text-sm"
            />
            <span className="text-xs text-muted">{t("dialogCheckpointHint")}</span>
          </label>

          <label className="grid gap-1 text-sm">
            <span className="text-xs font-medium text-muted">{t("dialogTokenLabel")}</span>
            <input
              type="number"
              min={1}
              value={tokenBudget}
              onChange={(e) => setTokenBudget(e.target.value)}
              placeholder={t("dialogTokenPlaceholder")}
              className="min-h-10 w-full rounded-md border border-border bg-background px-3 py-2 text-base text-foreground outline-none focus:border-accent sm:text-sm"
            />
            <span className="text-xs text-muted">{t("dialogTokenHint")}</span>
          </label>
          <ProseContinuationControls
            idPrefix="batch-prose"
            value={continuationPolicy}
            onChange={setContinuationPolicy}
            disabled={submitting}
          />
          {automaticContinuationsEnabled && !parsedTokenBudget && (
            <p role="note" className="text-xs leading-5 text-amber-800 dark:text-amber-200">
              {t("continuationBudgetRequired")}
            </p>
          )}

          <ReferenceCardAutoCreationControls
            value={referenceCardAutoCreationPolicy}
            onChange={setReferenceCardAutoCreationPolicy}
            disabled={submitting}
          />


          <section className="grid gap-2">
            <OutlineGenerationParams
              value={generationParams}
              onChange={setGenerationParams}
            />
            <p className="px-1 text-xs leading-5 text-muted">
              {t("dialogGenerationParamsHint")}
            </p>
            {generationParams.system_prompt !== null && (
              <p
                role="note"
                className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-800 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-300"
              >
                {t("dialogSystemPromptScopeHint")}
              </p>
            )}
          </section>

          <fieldset className="grid gap-2 rounded-md border border-border bg-background p-3">
            <legend className="px-1 text-xs font-medium text-muted">
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
                <span className="mt-0.5 block text-xs leading-5 text-muted">
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
                <span className="mt-0.5 block text-xs leading-5 text-muted">
                  {t("dialogDeviationContinueBody")}
                </span>
              </span>
            </label>
            <p className="border-t border-border pt-2 text-xs leading-5 text-muted">
              {t("dialogDeviationCostHint")}
            </p>
          </fieldset>

          <section aria-labelledby="generation-readiness-title" className="border-t border-border pt-4">
            <div className="flex items-start justify-between gap-3">
              <div>
                <h4 id="generation-readiness-title" className="text-sm font-semibold text-foreground">
                  {t("readinessTitle")}
                </h4>
                <p className="mt-1 text-xs leading-5 text-muted">{t("readinessDescription")}</p>
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
              <p role="status" className="mt-3 text-sm text-muted">{t("readinessLoading")}</p>
            )}

            {readiness && !readinessLoading && (
              <div className="mt-3 grid gap-3">
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
                      <p className="mt-1 text-xs text-muted">
                        {t("readinessWorkCounts", {
                          generate: readiness.work.steps[step].generate,
                          reuse: readiness.work.steps[step].reuse,
                        })}
                      </p>
                    </div>
                  ))}
                </div>

                <div className="grid gap-1 text-xs text-muted sm:grid-cols-2">
                  <p>
                    {t("readinessResources", {
                      characters: readiness.resources.character,
                      locations: readiness.resources.location,
                      items: readiness.resources.item,
                      rules: readiness.resources.rule,
                      lores: readiness.resources.lore,
                    })}
                  </p>
                  {readiness.planning.reference_card_auto_creation_policy && (() => {
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
                  {readiness.planning.prose_strategy && (
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
                  {readiness.planning.prose_continuation_authorization && (
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
          <Button
            variant="primary"
            size="sm"
            className="bg-accent text-white hover:bg-accent-hover"
            onPress={() => void submit()}
            isDisabled={
              submitting
              || readinessLoading
              || !readiness
              || !readinessIsCurrent
              || !readinessAllowsStart(readiness, acknowledgedCodes)
            }
          >
            {submitting ? t("dialogStarting") : t("dialogStart")}
          </Button>
        </footer>
      </div>
    </div>
  );
}
