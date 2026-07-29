"use client";

import { Button } from "@heroui/react";
import Image from "next/image";
import { useTranslations } from "next-intl";
import { useEffect, useMemo, useState } from "react";
import { apiGet, apiPost, getImageUrl } from "@/lib/api";
import {
  constrainIllustrationPromptEdit,
  illustrationPromptCharacterCount,
} from "@/lib/illustrationPrompt";
import type {
  AgentProfile,
  IllustrationPromptResult,
} from "@/types/agent";
import type {
  AppearanceAnchor,
  CharacterPortraitAsset,
  CharacterPortraitJob,
  CharacterPortraitState,
} from "@/types/image";
import { useCharacterPortraitJob } from "./useCharacterPortraitJob";

const PROMPT_FIELDS: Array<keyof IllustrationPromptResult> = [
  "subject",
  "appearance",
  "scene",
  "style",
  "negative",
];

interface CharacterPortraitPanelProps {
  novelId: string;
  cardId: string;
  cardName: string;
  hasUnsavedChanges: boolean;
}

interface CharacterPortraitInitialData {
  state: CharacterPortraitState;
  agents: AgentProfile[];
}

const pendingInitialLoads = new Map<
  string,
  Promise<CharacterPortraitInitialData>
>();

function loadCharacterPortraitInitialData(
  portraitPath: string,
): Promise<CharacterPortraitInitialData> {
  const pending = pendingInitialLoads.get(portraitPath);
  if (pending) return pending;

  const request = (async () => {
    const portraitState =
      await apiGet<CharacterPortraitState>(portraitPath);
    const response = await apiGet<{ data: AgentProfile[] }>(
      "/api/agents?capability=illustration_prompt",
    );
    return { state: portraitState, agents: response.data };
  })();
  pendingInitialLoads.set(portraitPath, request);
  void request.then(
    () => {
      if (pendingInitialLoads.get(portraitPath) === request) {
        pendingInitialLoads.delete(portraitPath);
      }
    },
    () => {
      if (pendingInitialLoads.get(portraitPath) === request) {
        pendingInitialLoads.delete(portraitPath);
      }
    },
  );
  return request;
}

export default function CharacterPortraitPanel({
  novelId,
  cardId,
  cardName,
  hasUnsavedChanges,
}: CharacterPortraitPanelProps) {
  const t = useTranslations("writing.referenceCards.portrait");
  const [state, setState] = useState<CharacterPortraitState | null>(null);
  const [agents, setAgents] = useState<AgentProfile[]>([]);
  const [agentId, setAgentId] = useState("");
  const [prompt, setPrompt] = useState<IllustrationPromptResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [translating, setTranslating] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [imageFailed, setImageFailed] = useState(false);
  const { job, pollError, cancelling, adoptJob, cancel } =
    useCharacterPortraitJob({ novelId, cardId });
  const {
    job: cleanupJobState,
    pollError: cleanupPollError,
    cancelling: cleanupCancelling,
    adoptJob: adoptCleanupJob,
    cancel: retryCleanup,
  } = useCharacterPortraitJob({ novelId, cardId });

  const portraitPath =
    `/api/reference-cards/novel/${novelId}/character/${cardId}/portrait`;

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);

    const load = async () => {
      try {
        const initialData =
          await loadCharacterPortraitInitialData(portraitPath);
        if (!active) return;
        setState(initialData.state);
        adoptJob(initialData.state.active_job);
        adoptCleanupJob(
          initialData.state.cleanup_job?.job_id ===
            initialData.state.active_job?.job_id
            ? null
            : initialData.state.cleanup_job,
        );
        const available = initialData.agents.filter(
          (agent) =>
            agent.enabled &&
            agent.capabilities.includes("illustration_prompt"),
        );
        setAgents(available);
        setAgentId(available[0]?.agent_id ?? "");
      } catch (reason) {
        if (active) {
          setError(reason instanceof Error ? reason.message : t("loadFailed"));
        }
      } finally {
        if (active) setLoading(false);
      }
    };

    void load();
    return () => {
      active = false;
    };
  }, [adoptCleanupJob, adoptJob, portraitPath, t]);

  useEffect(() => {
    setImageFailed(false);
  }, [job?.asset?.asset_id, state?.asset?.asset_id]);

  const completedAnchor =
    job?.status === "succeeded" ? job.anchor : null;
  const completedAsset =
    job?.status === "succeeded" ? job.asset : null;
  const anchor: AppearanceAnchor | null =
    completedAnchor ?? state?.anchor ?? null;
  const asset: CharacterPortraitAsset | null =
    completedAsset ?? state?.asset ?? null;
  const provider = job?.provider ?? state?.provider ?? null;
  const warnings = useMemo(
    () =>
      Array.from(
        new Set([
          ...(state?.warnings ?? []),
          ...(state?.provider.warnings ?? []),
          ...(job?.warnings ?? []),
          ...(job?.provider?.warnings ?? []),
        ]),
      ),
    [job?.provider?.warnings, job?.warnings, state],
  );
  const assetMissing =
    imageFailed || asset?.state === "missing" || Boolean(anchor && !asset);
  const cleanupJob =
    cleanupJobState?.cleanup_pending ? cleanupJobState : null;
  const activeJob = Boolean(
    (job && !job.terminal) || cleanupJob,
  );
  const cancelAvailable = Boolean(
    job &&
      !job.terminal &&
      job.status !== "cancelling" &&
      job.status !== "storing_asset" &&
      job.status !== "finalizing" &&
      (
        !job.cleanup_pending ||
        job.abandonable ||
        (job.failure !== null && !job.failure.retryable)
      ),
  );
  const cleanupActionAvailable = Boolean(
    cleanupJob &&
      (
        cleanupJob.abandonable ||
        (
          cleanupJob.failure !== null &&
          !cleanupJob.failure.retryable
        )
      ),
  );
  const abandonLostJob = Boolean(job?.abandonable);
  const promptCount = prompt
    ? illustrationPromptCharacterCount(prompt)
    : 0;
  const formatDuration = (seconds: number): string => {
    const rounded = Math.max(0, Math.round(seconds));
    if (rounded < 60) return t("durationSeconds", { count: rounded });
    const minutes = Math.floor(rounded / 60);
    const remainder = rounded % 60;
    return remainder
      ? t("durationMinutesSeconds", {
          minutes,
          seconds: remainder,
        })
      : t("durationMinutes", { count: minutes });
  };

  const translate = async () => {
    if (!agentId || translating || hasUnsavedChanges) return;
    setTranslating(true);
    setError(null);
    try {
      const response = await apiPost<{ result: IllustrationPromptResult }>(
        "/api/llm/agent-illustration-prompt",
        {
          novel_id: novelId,
          scope: "character",
          volume_id: null,
          chapter_id: null,
          character_card_id: cardId,
          agent_id: agentId,
          instruction: "",
          target_model: provider?.model ?? "",
          focus: "",
        },
      );
      setPrompt(response.result);
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : t("translateFailed"),
      );
    } finally {
      setTranslating(false);
    }
  };

  const submit = async () => {
    if (!prompt || submitting || activeJob || hasUnsavedChanges) return;
    const resetting = Boolean(anchor);
    if (resetting && !window.confirm(t("resetWarning"))) return;

    setSubmitting(true);
    setError(null);
    try {
      const next = await apiPost<CharacterPortraitJob>(
        `${portraitPath}/jobs`,
        {
          prompt,
          confirm_anchor_reset: resetting,
        },
      );
      adoptJob(next);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t("submitFailed"));
    } finally {
      setSubmitting(false);
    }
  };

  const updatePrompt = (
    field: keyof IllustrationPromptResult,
    rawValue: string,
  ) => {
    setPrompt((current) =>
      current
        ? constrainIllustrationPromptEdit(current, field, rawValue)
        : current,
    );
  };

  return (
    <section
      aria-labelledby={`portrait-title-${cardId}`}
      className="min-w-0 border-t border-border pt-6 md:col-span-2"
    >
      <div className="flex min-w-0 flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3
            id={`portrait-title-${cardId}`}
            className="text-base font-semibold text-foreground"
          >
            {t("title")}
          </h3>
          <p className="mt-1 max-w-2xl text-sm leading-6 text-muted">
            {t("description")}
          </p>
        </div>
        {provider && (
          <div className="min-w-0 max-w-full text-right text-xs text-muted">
            <p className="truncate" title={provider.alias}>
              {t("provider", { provider: provider.alias })}
            </p>
            <p className="truncate" title={provider.model}>
              {t("model", { model: provider.model })}
            </p>
            {!job &&
              (provider.queue_position === null &&
              provider.estimated_seconds === null ? (
                <p>{t("providerEstimateUnavailable")}</p>
              ) : (
                <>
                  {provider.queue_position !== null && (
                    <p>
                      {t("providerQueuePosition", {
                        count: provider.queue_position,
                      })}
                    </p>
                  )}
                  {provider.estimated_seconds !== null && (
                    <p>
                      {t("providerEstimated", {
                        duration: formatDuration(
                          provider.estimated_seconds,
                        ),
                      })}
                    </p>
                  )}
                </>
              ))}
          </div>
        )}
      </div>

      {loading && (
        <p role="status" className="mt-4 text-sm text-muted">
          {t("loading")}
        </p>
      )}
      {error && (
        <div
          role="alert"
          className="mt-4 rounded-lg border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-800 dark:border-red-900 dark:bg-red-950 dark:text-red-200"
        >
          {error}
        </div>
      )}
      {hasUnsavedChanges && (
        <div
          role="status"
          className="mt-4 rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-100"
        >
          {t("saveFirst")}
        </div>
      )}
      {provider && !provider.available && (
        <div
          role="status"
          className="mt-4 rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-100"
        >
          {t("providerUnavailable")}
        </div>
      )}
      {warnings.length > 0 && (
        <div className="mt-4 rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-100">
          <p className="font-medium">{t("warningsTitle")}</p>
          <ul className="mt-1 list-disc space-y-1 pl-5">
            {warnings.map((warning, index) => (
              <li key={`${warning}-${index}`} className="break-words">
                {warning}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="mt-5 grid min-w-0 gap-5 lg:grid-cols-[minmax(0,15rem)_minmax(0,1fr)]">
        <div className="min-w-0">
          <div className="relative aspect-[2/3] overflow-hidden rounded-xl border border-border bg-surface-secondary">
            {asset &&
            asset.state === "available" &&
            asset.content_url &&
            !imageFailed ? (
              <Image
                src={getImageUrl(asset.content_url)}
                alt={t("imageAlt", { name: cardName })}
                fill
                sizes="(min-width: 1024px) 15rem, 100vw"
                unoptimized
                className="object-cover"
                onError={() => setImageFailed(true)}
              />
            ) : assetMissing ? (
              <div
                role="status"
                className="flex h-full items-center justify-center px-4 text-center text-sm font-medium text-muted"
              >
                {t("assetMissing")}
              </div>
            ) : (
              <div className="flex h-full items-center justify-center px-4 text-center text-sm text-muted">
                {t("empty")}
              </div>
            )}
          </div>
          {anchor && (
            <dl className="mt-3 min-w-0 space-y-1 text-xs text-muted">
              <div className="flex min-w-0 gap-2">
                <dt className="shrink-0">{t("anchorSeed")}</dt>
                <dd className="truncate">{anchor.seed}</dd>
              </div>
              <div className="flex min-w-0 gap-2">
                <dt className="shrink-0">{t("anchorProvider")}</dt>
                <dd className="truncate" title={`${anchor.provider} / ${anchor.model}`}>
                  {anchor.provider} / {anchor.model}
                </dd>
              </div>
              <div className="flex min-w-0 gap-2">
                <dt className="shrink-0">{t("anchorReferenceMode")}</dt>
                <dd className="truncate">{anchor.reference_mode}</dd>
              </div>
            </dl>
          )}
        </div>

        <div className="min-w-0">
          <div className="flex min-w-0 flex-wrap items-end gap-3">
            <label className="min-w-0 flex-1 text-sm">
              <span className="mb-1.5 block text-muted">
                {t("agentLabel")}
              </span>
              <select
                value={agentId}
                onChange={(event) => setAgentId(event.target.value)}
                disabled={loading || translating || activeJob}
                className="w-full min-w-0 rounded-lg border border-border bg-surface px-3 py-2.5 text-sm text-foreground outline-none focus:border-accent focus:ring-2 focus:ring-accent/15 disabled:opacity-60"
              >
                {agents.length === 0 && (
                  <option value="">{t("noAgent")}</option>
                )}
                {agents.map((agent) => (
                  <option key={agent.agent_id} value={agent.agent_id}>
                    {agent.label}
                  </option>
                ))}
              </select>
            </label>
            <Button
              variant="outline"
              isDisabled={
                loading ||
                translating ||
                activeJob ||
                !agentId ||
                hasUnsavedChanges
              }
              onPress={() => void translate()}
            >
              {translating ? t("translating") : t("translate")}
            </Button>
          </div>

          {prompt && (
            <div className="mt-5 grid min-w-0 gap-4 md:grid-cols-2">
              {PROMPT_FIELDS.map((field) => (
                <label
                  key={field}
                  className={`min-w-0 text-sm ${
                    field === "scene" ? "md:col-span-2" : ""
                  }`}
                >
                  <span className="mb-1.5 block font-medium text-foreground">
                    {t(`fields.${field}`)}
                  </span>
                  <textarea
                    aria-label={t(`fields.${field}`)}
                    value={prompt[field]}
                    onChange={(event) =>
                      updatePrompt(field, event.target.value)
                    }
                    rows={field === "scene" ? 4 : 3}
                    className="w-full min-w-0 resize-y rounded-lg border border-border bg-surface px-3 py-2.5 text-sm leading-6 text-foreground outline-none focus:border-accent focus:ring-2 focus:ring-accent/15"
                  />
                </label>
              ))}
              <p className="text-xs text-muted md:col-span-2">
                {t("promptCount", { count: promptCount })}
              </p>
            </div>
          )}

          {cleanupJob && (
            <div
              role="status"
              className="mt-5 min-w-0 rounded-xl border border-amber-300 bg-amber-50 px-4 py-3 text-sm dark:border-amber-800 dark:bg-amber-950/30"
            >
              <p className="font-medium text-foreground">
                {t("olderCleanupTitle")}
              </p>
              <p className="mt-1 text-muted">
                {t("olderCleanupDescription")}
              </p>
              <div className="mt-2 flex min-w-0 flex-wrap items-center gap-x-4 gap-y-2">
                <span className="font-medium text-foreground">
                  {t(`status.${cleanupJob.status}`)}
                </span>
                {cleanupJob.elapsed_seconds > 0 && (
                  <span className="text-muted">
                    {t("elapsed", {
                      duration: formatDuration(
                        cleanupJob.elapsed_seconds,
                      ),
                    })}
                  </span>
                )}
                {cleanupJob.completed_images > 0 && (
                  <span className="text-muted">
                    {t("completedImages", {
                      count: cleanupJob.completed_images,
                    })}
                  </span>
                )}
              </div>
              <p className="mt-2 text-amber-700 dark:text-amber-300">
                {t("cleanupPending")}
              </p>
              {cleanupJob.failure && (
                <div
                  className={`mt-2 ${
                    cleanupJob.failure.retryable
                      ? "text-amber-700 dark:text-amber-300"
                      : "text-red-700 dark:text-red-300"
                  }`}
                >
                  <p>{cleanupJob.failure.message}</p>
                  <p>{cleanupJob.failure.action}</p>
                </div>
              )}
              {cleanupPollError && (
                <p className="mt-2 text-amber-700 dark:text-amber-300">
                  {t("pollUnavailable")}
                </p>
              )}
              {cleanupActionAvailable && (
                <div className="mt-3 flex min-w-0 flex-wrap gap-2">
                  <Button
                    variant="outline"
                    isDisabled={cleanupCancelling}
                    onPress={() => {
                      if (
                        cleanupJob.abandonable &&
                        !window.confirm(t("abandonWarning"))
                      ) {
                        return;
                      }
                      void retryCleanup();
                    }}
                  >
                    {cleanupCancelling
                      ? cleanupJob.abandonable
                        ? t("abandoning")
                        : t("retryingCleanup")
                      : cleanupJob.abandonable
                        ? t("abandon")
                        : t("retryCleanup")}
                  </Button>
                </div>
              )}
            </div>
          )}

          {job && (
            <div
              role="status"
              className="mt-5 min-w-0 rounded-xl border border-border bg-surface-secondary px-4 py-3 text-sm"
            >
              <div className="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-2">
                <span className="font-medium text-foreground">
                  {t(`status.${job.status}`)}
                </span>
                {job.queue_position !== null && (
                  <span className="text-muted">
                    {t("queuePosition", { count: job.queue_position })}
                  </span>
                )}
                {job.estimated_seconds !== null && (
                  <span className="text-muted">
                    {t("estimated", {
                      duration: formatDuration(job.estimated_seconds),
                    })}
                  </span>
                )}
                {job.elapsed_seconds > 0 && (
                  <span className="text-muted">
                    {t("elapsed", {
                      duration: formatDuration(job.elapsed_seconds),
                    })}
                  </span>
                )}
                {job.completed_images > 0 && (
                  <span className="text-muted">
                    {t("completedImages", {
                      count: job.completed_images,
                    })}
                  </span>
                )}
              </div>
              {job.cleanup_pending && (
                <p className="mt-2 text-amber-700 dark:text-amber-300">
                  {t("cleanupPending")}
                </p>
              )}
              {job.status === "failed" &&
                job.failure?.retryable &&
                !job.terminal && (
                  <p className="mt-2 text-amber-700 dark:text-amber-300">
                    {t("pollRetrying")}
                  </p>
                )}
              {job.failure && job.status !== "cancelled" && (
                  <div
                    className={`mt-2 ${
                      !job.terminal && job.failure.retryable
                        ? "text-amber-700 dark:text-amber-300"
                        : "text-red-700 dark:text-red-300"
                    }`}
                  >
                    <p>{job.failure.message}</p>
                    <p>{job.failure.action}</p>
                  </div>
              )}
              {job.ignored_slots.length > 0 && (
                <p className="mt-2 break-words text-amber-700 dark:text-amber-300">
                  {t("ignoredSlots", {
                    slots: job.ignored_slots.join(", "),
                  })}
                </p>
              )}
              {pollError && (
                <p className="mt-2 text-amber-700 dark:text-amber-300">
                  {t("pollUnavailable")}
                </p>
              )}
            </div>
          )}

          <div className="mt-5 flex min-w-0 flex-wrap gap-2">
            <Button
              variant="primary"
              className="bg-accent text-white hover:bg-accent-hover"
              isDisabled={
                !prompt ||
                submitting ||
                activeJob ||
                !provider?.available ||
                hasUnsavedChanges
              }
              onPress={() => void submit()}
            >
              {submitting
                ? t("submitting")
                : anchor
                  ? t("resetAndGenerate")
                  : t("generate")}
            </Button>
            {cancelAvailable && (
              <Button
                variant="outline"
                isDisabled={cancelling}
                onPress={() => {
                  if (
                    abandonLostJob &&
                    !window.confirm(t("abandonWarning"))
                  ) {
                    return;
                  }
                  void cancel();
                }}
              >
                {cancelling
                  ? abandonLostJob
                    ? t("abandoning")
                    : job?.cleanup_pending
                    ? t("retryingCleanup")
                    : t("cancelling")
                  : abandonLostJob
                    ? t("abandon")
                    : job?.cleanup_pending
                    ? t("retryCleanup")
                    : t("cancel")}
              </Button>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}
