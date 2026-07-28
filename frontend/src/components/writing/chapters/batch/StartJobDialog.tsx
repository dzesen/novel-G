"use client";

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@heroui/react";
import { apiGet, apiPost } from "@/lib/api";
import OutlineGenerationParams, {
  EMPTY_GENERATION_PARAMS,
  toRequestParams,
  type GenerationParams,
} from "../outline/OutlineGenerationParams";
import type {
  GenerationJob,
  GenerationReadiness,
  OutlineDeviationPolicy,
  ReadinessIssue,
} from "./batchTypes";
import {
  buildAuthorizedStartPayload,
  readinessAllowsStart,
} from "./readinessPresentation";

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
  const [checkpointInterval, setCheckpointInterval] = useState(5);
  const [tokenBudget, setTokenBudget] = useState("");
  const [outlineDeviationPolicy, setOutlineDeviationPolicy] =
    useState<OutlineDeviationPolicy>("pause_for_rewrite");
  const [generationParams, setGenerationParams] = useState<GenerationParams>(
    () => ({ ...EMPTY_GENERATION_PARAMS }),
  );
  const [submitting, setSubmitting] = useState(false);
  const [readiness, setReadiness] = useState<GenerationReadiness | null>(null);
  const [readinessLoading, setReadinessLoading] = useState(true);
  const [acknowledgedCodes, setAcknowledgedCodes] = useState<Set<string>>(new Set());
  const [error, setError] = useState("");

  const loadReadiness = useCallback(async () => {
    setReadinessLoading(true);
    setError("");
    try {
      const report = await apiGet<GenerationReadiness>(
        `/api/generation-jobs/${scope}/${targetId}/readiness`,
      );
      setReadiness(report);
      setAcknowledgedCodes(new Set());
    } catch (err) {
      setReadiness(null);
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setReadinessLoading(false);
    }
  }, [scope, targetId]);

  useEffect(() => {
    void loadReadiness();
  }, [loadReadiness]);

  const issueCopy = (issue: ReadinessIssue) => {
    switch (issue.code) {
      case "character_cards_missing":
        return {
          title: t("readinessIssueCharacterCardsMissingTitle"),
          body: t("readinessIssueCharacterCardsMissingBody"),
        };
      case "world_cards_missing":
        return {
          title: t("readinessIssueWorldCardsMissingTitle"),
          body: t("readinessIssueWorldCardsMissingBody"),
        };
      case "reference_card_proposal_pending":
        return {
          title: t("readinessIssueProposalPendingTitle"),
          body: t("readinessIssueProposalPendingBody"),
        };
      case "provider_plan_invalid":
        return {
          title: t("readinessIssueProviderInvalidTitle"),
          body: t("readinessIssueProviderInvalidBody"),
        };
      case "no_generation_work":
        return {
          title: t("readinessIssueNoWorkTitle"),
          body: t("readinessIssueNoWorkBody"),
        };
      case "prose_scene_segmentation_planned":
        return {
          title: t("readinessIssueProseSegmentsTitle"),
          body: t("readinessIssueProseSegmentsBody", {
            segmented: Number(issue.details.scene_segment_chapters ?? 0),
            unknown: Number(issue.details.unknown_outline_chapters ?? 0),
            calls: Number(issue.details.maximum_prose_calls ?? 0),
          }),
        };
      case "partial_prose_requires_manual_completion":
        return {
          title: t("readinessIssuePartialProseTitle"),
          body: t("readinessIssuePartialProseBody", {
            count: Number(issue.details.chapter_count ?? 0),
          }),
        };
      default:
        return {
          title: t("readinessIssueUnknownTitle"),
          body: t("readinessIssueUnknownBody", { code: issue.code }),
        };
    }
  };

  const submit = async () => {
    if (!readiness || !readinessAllowsStart(readiness, acknowledgedCodes)) return;
    setSubmitting(true);
    setError("");
    try {
      const parsedBudget = Number(tokenBudget);
      const budget =
        tokenBudget.trim() && Number.isFinite(parsedBudget) && parsedBudget >= 1
          ? Math.floor(parsedBudget)
          : null;
      const payload = buildAuthorizedStartPayload({
        checkpointInterval,
        tokenBudget: budget,
        readiness,
        acknowledgedCodes,
        outlineDeviationPolicy,
        generationParams: toRequestParams(generationParams),
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
    <div className="absolute inset-0 z-30 flex items-center justify-center bg-black/25 px-4 py-6">
      <div className="flex max-h-full w-full max-w-2xl flex-col rounded-md border border-border bg-surface shadow-lg">
        <header className="border-b border-border px-5 py-4">
          <h3 className="text-base font-semibold text-foreground">{title}</h3>
        </header>

        <div className="grid gap-4 overflow-y-auto px-5 py-4">
          <div className="grid gap-1 text-sm">
            <span className="text-xs font-medium text-muted">{targetHeading}</span>
            <div className="rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground">
              <span className="font-medium">{targetLabel}</span>
              <span className="ml-2 text-xs text-muted">{t("dialogFillable", { count: fillableCount })}</span>
            </div>
          </div>

          <label className="grid gap-1 text-sm">
            <span className="text-xs font-medium text-muted">{t("dialogCheckpointLabel")}</span>
            <input
              type="number"
              min={1}
              max={1000}
              value={checkpointInterval}
              onChange={(e) => setCheckpointInterval(Number(e.target.value))}
              className="min-h-9 w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground outline-none focus:border-accent"
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
              className="min-h-9 w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground outline-none focus:border-accent"
            />
            <span className="text-xs text-muted">{t("dialogTokenHint")}</span>
          </label>

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
                </div>

                {readiness.issues.map((issue) => {
                  const copy = issueCopy(issue);
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
                          <span>{t("readinessAcknowledge")}</span>
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

        <footer className="flex justify-end gap-2 border-t border-border px-5 py-3">
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
