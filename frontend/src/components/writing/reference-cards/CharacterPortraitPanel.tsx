"use client";

import { Button } from "@heroui/react";
import Image from "next/image";
import { useLocale, useTranslations } from "next-intl";
import { useEffect, useMemo, useState } from "react";
import { ApiError, apiDelete, apiGet, apiPost, getImageUrl } from "@/lib/api";
import {
  constrainIllustrationPromptEdit,
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
import IllustrationPromptEditor from "@/components/image/IllustrationPromptEditor";
import ImageJobStatusPanel from "@/components/image/ImageJobStatusPanel";
import CharacterVisualProfilePanel from "./CharacterVisualProfilePanel";
import { useCharacterPortraitJob } from "./useCharacterPortraitJob";

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
  const locale = useLocale();
  const [state, setState] = useState<CharacterPortraitState | null>(null);
  const [agents, setAgents] = useState<AgentProfile[]>([]);
  const [agentId, setAgentId] = useState("");
  const [prompt, setPrompt] = useState<IllustrationPromptResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [translating, setTranslating] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [detaching, setDetaching] = useState(false);
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

  const terminalJobId = job?.terminal ? job.job_id : null;
  useEffect(() => {
    if (!terminalJobId) return;
    let active = true;

    void apiGet<CharacterPortraitState>(portraitPath)
      .then((nextState) => {
        if (active) setState(nextState);
      })
      .catch(() => {
        // The terminal job already carries its own actionable failure. Keep it
        // visible and let a later panel reload retry the state reconciliation.
      });

    return () => {
      active = false;
    };
  }, [portraitPath, terminalJobId]);

  const completedAnchor =
    job?.status === "succeeded" ? job.anchor : null;
  const completedAsset =
    job?.status === "succeeded" ? job.asset : null;
  const anchor: AppearanceAnchor | null =
    completedAnchor ?? state?.anchor ?? null;
  const asset: CharacterPortraitAsset | null =
    completedAsset ?? state?.asset ?? null;
  const anchorEstablishedAt = useMemo(() => {
    if (!anchor) return "";
    const parsed = new Date(anchor.established_at);
    if (Number.isNaN(parsed.getTime())) return anchor.established_at;
    return new Intl.DateTimeFormat(locale, {
      dateStyle: "medium",
      timeStyle: "short",
    }).format(parsed);
  }, [anchor, locale]);
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
  const anchorDependencies = state?.anchor_dependencies ?? [];
  const anchorDependencyTotal = state?.anchor_dependency_total ?? 0;
  const hiddenAnchorDependencyCount = Math.max(
    0,
    anchorDependencyTotal - anchorDependencies.length,
  );
  const cleanupJob =
    cleanupJobState?.cleanup_pending ? cleanupJobState : null;
  const activeJob = Boolean(
    (job && !job.terminal) || cleanupJob,
  );
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
    if (
      resetting &&
      !window.confirm(t("resetWarning"))
    ) return;

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

  const detachAnchor = async () => {
    if (
      !anchor ||
      detaching ||
      activeJob ||
      anchorDependencyTotal > 0
    ) return;
    if (!window.confirm(t("detachWarning"))) return;

    setDetaching(true);
    setError(null);
    try {
      const nextState = await apiDelete<CharacterPortraitState>(
        `${portraitPath}/anchor?expected_reference_asset=${encodeURIComponent(anchor.reference_asset)}`,
      );
      setState(nextState);
      adoptJob(null);
      setImageFailed(false);
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 409) {
        const detail =
          reason.detail && typeof reason.detail === "object"
            ? (reason.detail as Record<string, unknown>)
            : null;
        const code = typeof detail?.code === "string" ? detail.code : "";
        try {
          const refreshed = await apiGet<CharacterPortraitState>(portraitPath);
          setState(refreshed);
        } catch {
          // Keep the inspected anchor visible; the next panel load retries.
        }
        setError(
          code === "appearance_anchor_in_use"
            ? t("detachBlockedChanged")
            : code === "appearance_anchor_busy"
              ? t("detachBusy")
              : t("detachConflict"),
        );
      } else {
        setError(reason instanceof Error ? reason.message : t("detachFailed"));
      }
    } finally {
      setDetaching(false);
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
            <section
              aria-labelledby={`portrait-anchor-title-${cardId}`}
              className="mt-3 min-w-0 rounded-lg border border-accent/25 bg-accent/5 p-3"
            >
              <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
                <h4
                  id={`portrait-anchor-title-${cardId}`}
                  className="text-sm font-semibold text-foreground"
                >
                  {t("anchorTitle")}
                </h4>
                <span className="rounded-full bg-accent/10 px-2 py-0.5 text-[0.6875rem] font-medium text-accent">
                  {t("anchorBound")}
                </span>
              </div>
              <p className="mt-1.5 text-xs leading-5 text-muted">
                {t("anchorDescription")}
              </p>
              <dl className="mt-3 min-w-0 space-y-2 border-t border-accent/15 pt-3 text-xs">
                <div className="min-w-0">
                  <dt className="text-muted">{t("anchorAsset")}</dt>
                  <dd className="mt-0.5 truncate text-foreground">
                    {t("anchorAssetValue")}
                  </dd>
                </div>
                <div className="min-w-0">
                  <dt className="text-muted">{t("anchorEstablishedAt")}</dt>
                  <dd className="mt-0.5 truncate text-foreground">
                    <time dateTime={anchor.established_at}>
                      {anchorEstablishedAt}
                    </time>
                  </dd>
                </div>
                <div className="min-w-0">
                  <dt className="text-muted">{t("anchorProvider")}</dt>
                  <dd
                    className="mt-0.5 truncate text-foreground"
                    title={`${anchor.provider} / ${anchor.model}`}
                  >
                    {anchor.provider} / {anchor.model}
                  </dd>
                </div>
                <div className="grid min-w-0 grid-cols-2 gap-3">
                  <div className="min-w-0">
                    <dt className="text-muted">{t("anchorSeed")}</dt>
                    <dd className="mt-0.5 truncate text-foreground">
                      {anchor.seed}
                    </dd>
                  </div>
                  <div className="min-w-0">
                    <dt className="text-muted">{t("anchorReferenceMode")}</dt>
                    <dd className="mt-0.5 truncate text-foreground">
                      {anchor.reference_mode}
                    </dd>
                  </div>
                </div>
              </dl>
              {anchorDependencyTotal > 0 ? (
                <div
                  role="status"
                  className="mt-3 min-w-0 rounded-lg border border-amber-300 bg-amber-50 px-3 py-2.5 text-xs leading-5 text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-100"
                >
                  <p className="font-medium">
                    {t("detachBlocked", { count: anchorDependencyTotal })}
                  </p>
                  <p className="mt-1">{t("detachBlockedHint")}</p>
                  <ul className="mt-2 space-y-1">
                    {anchorDependencies.map((dependency) => (
                      <li
                        key={dependency.job_id}
                        className="min-w-0 break-words"
                      >
                        {dependency.chapter_order !== null &&
                        dependency.chapter_title
                          ? t("dependencyChapter", {
                              order: dependency.chapter_order,
                              title: dependency.chapter_title,
                            })
                          : dependency.chapter_title
                            ? t("dependencyTitle", {
                                title: dependency.chapter_title,
                              })
                            : t("dependencyUnavailable")}
                      </li>
                    ))}
                  </ul>
                  {hiddenAnchorDependencyCount > 0 && (
                    <p className="mt-1">
                      {t("dependencyMore", {
                        count: hiddenAnchorDependencyCount,
                      })}
                    </p>
                  )}
                </div>
              ) : (
                <p className="mt-3 text-xs leading-5 text-muted">
                  {t("detachAvailable")}
                </p>
              )}
              <Button
                type="button"
                size="sm"
                variant="outline"
                isDisabled={
                  detaching || activeJob || anchorDependencyTotal > 0
                }
                onPress={() => void detachAnchor()}
                className="mt-3 w-full border-red-300 text-red-700 dark:border-red-800 dark:text-red-300 sm:w-auto"
              >
                {detaching ? t("detaching") : t("detach")}
              </Button>
            </section>
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
            <IllustrationPromptEditor
              prompt={prompt}
              labels={{
                subject: t("fields.subject"),
                appearance: t("fields.appearance"),
                scene: t("fields.scene"),
                style: t("fields.style"),
                negative: t("fields.negative"),
              }}
              totalLabel={(count) => t("promptCount", { count })}
              onChange={updatePrompt}
            />
          )}

          <ImageJobStatusPanel
            job={job}
            cleanupJob={cleanupJob}
            pollError={pollError}
            cleanupPollError={cleanupPollError}
            cancelling={cancelling}
            cleanupCancelling={cleanupCancelling}
            formatDuration={formatDuration}
            labels={{
              status: (status) => t(`status.${status}`),
              queuePosition: (count) => t("queuePosition", { count }),
              estimated: (duration) => t("estimated", { duration }),
              elapsed: (duration) => t("elapsed", { duration }),
              completedImages: (count) =>
                t("completedImages", { count }),
              pollRetrying: t("pollRetrying"),
              cleanupPending: t("cleanupPending"),
              olderCleanupTitle: t("olderCleanupTitle"),
              olderCleanupDescription: t("olderCleanupDescription"),
              retryCleanup: t("retryCleanup"),
              retryingCleanup: t("retryingCleanup"),
              abandon: t("abandon"),
              abandoning: t("abandoning"),
              abandonWarning: t("abandonWarning"),
              ignoredDimensions: t("ignoredDimensions"),
              ignoredSlots: (slots) => t("ignoredSlots", { slots }),
              pollUnavailable: t("pollUnavailable"),
              cancel: t("cancel"),
              cancelling: t("cancelling"),
            }}
            primaryAction={(
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
            )}
            onCancel={() => void cancel()}
            onRetryCleanup={() => void retryCleanup()}
          />
        </div>
      </div>
      <CharacterVisualProfilePanel
        novelId={novelId}
        cardId={cardId}
        cardName={cardName}
        currentAsset={asset}
      />
    </section>
  );
}
