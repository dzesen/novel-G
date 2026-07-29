"use client";

import { Button } from "@heroui/react";
import Image from "next/image";
import { useTranslations } from "next-intl";
import { useEffect, useMemo, useRef, useState } from "react";
import IllustrationPromptEditor from "@/components/image/IllustrationPromptEditor";
import ImageJobStatusPanel from "@/components/image/ImageJobStatusPanel";
import { useImageJob } from "@/components/image/useImageJob";
import { apiGet, apiPost, apiPut, getImageUrl } from "@/lib/api";
import {
  constrainIllustrationPromptEdit,
} from "@/lib/illustrationPrompt";
import type {
  AgentProfile,
  IllustrationPromptResult,
} from "@/types/agent";
import type {
  ImageAsset,
  NovelCoverJob,
  NovelCoverState,
} from "@/types/image";

interface NovelCoverPanelProps {
  novelId: string;
  novelTitle: string;
  coverAssetId?: string | null;
  coverImage?: string | null;
  hasUnsavedChanges: boolean;
  onCoverChanged: () => void | Promise<void>;
}

interface NovelCoverInitialData {
  state: NovelCoverState;
  agents: AgentProfile[];
}

const pendingInitialLoads = new Map<
  string,
  Promise<NovelCoverInitialData>
>();

function loadNovelCoverInitialData(
  coverPath: string,
): Promise<NovelCoverInitialData> {
  const pending = pendingInitialLoads.get(coverPath);
  if (pending) return pending;

  const request = (async () => {
    const [state, response] = await Promise.all([
      apiGet<NovelCoverState>(coverPath),
      apiGet<{ data: AgentProfile[] }>(
        "/api/agents?capability=illustration_prompt",
      ),
    ]);
    return { state, agents: response.data };
  })();
  pendingInitialLoads.set(coverPath, request);
  void request.then(
    () => {
      if (pendingInitialLoads.get(coverPath) === request) {
        pendingInitialLoads.delete(coverPath);
      }
    },
    () => {
      if (pendingInitialLoads.get(coverPath) === request) {
        pendingInitialLoads.delete(coverPath);
      }
    },
  );
  return request;
}

export default function NovelCoverPanel({
  novelId,
  novelTitle,
  coverAssetId,
  coverImage,
  hasUnsavedChanges,
  onCoverChanged,
}: NovelCoverPanelProps) {
  const t = useTranslations("writing.novelInfo.cover");
  const coverPath = `/api/novels/${novelId}/cover`;
  const jobBase = `${coverPath}/jobs`;
  const [state, setState] = useState<NovelCoverState | null>(null);
  const [agents, setAgents] = useState<AgentProfile[]>([]);
  const [agentId, setAgentId] = useState("");
  const [prompt, setPrompt] = useState<IllustrationPromptResult | null>(
    null,
  );
  const [width, setWidth] = useState("512");
  const [height, setHeight] = useState("768");
  const [explicitAssetId, setExplicitAssetId] = useState<string | null>(
    coverAssetId ?? null,
  );
  const [loading, setLoading] = useState(true);
  const [translating, setTranslating] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [selectingAssetId, setSelectingAssetId] = useState<
    string | null | undefined
  >(undefined);
  const [error, setError] = useState<string | null>(null);
  const [completionNotice, setCompletionNotice] = useState<
    string | null
  >(null);
  const [currentImageFailed, setCurrentImageFailed] = useState(false);
  const handledCompletionJobId = useRef<string | null>(null);
  const {
    job,
    pollError,
    cancelling,
    adoptJob,
    cancel,
  } = useImageJob<NovelCoverJob>({ jobBase });
  const {
    job: cleanupJobState,
    pollError: cleanupPollError,
    cancelling: cleanupCancelling,
    adoptJob: adoptCleanupJob,
    cancel: retryCleanup,
  } = useImageJob<NovelCoverJob>({ jobBase });

  useEffect(() => {
    setExplicitAssetId(coverAssetId ?? null);
  }, [coverAssetId]);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    const load = async () => {
      try {
        const initial = await loadNovelCoverInitialData(coverPath);
        if (!active) return;
        setState(initial.state);
        setExplicitAssetId(
          initial.state.current_asset?.asset_id ??
            coverAssetId ??
            null,
        );
        adoptJob(initial.state.active_job);
        adoptCleanupJob(
          initial.state.cleanup_job?.job_id ===
            initial.state.active_job?.job_id
            ? null
            : initial.state.cleanup_job,
        );
        const available = initial.agents.filter(
          (agent) =>
            agent.enabled &&
            agent.capabilities.includes("illustration_prompt"),
        );
        setAgents(available);
        setAgentId(available[0]?.agent_id ?? "");
      } catch (reason) {
        if (active) {
          setError(
            reason instanceof Error ? reason.message : t("loadFailed"),
          );
        }
      } finally {
        if (active) setLoading(false);
      }
    };
    void load();
    return () => {
      active = false;
    };
  }, [
    adoptCleanupJob,
    adoptJob,
    coverAssetId,
    coverPath,
    t,
  ]);

  const producedAsset = job?.asset ?? null;
  const currentAsset = state?.current_asset ?? null;
  useEffect(() => {
    if (
      !producedAsset ||
      !job ||
      !job.terminal ||
      handledCompletionJobId.current === job.job_id
    ) {
      return;
    }
    handledCompletionJobId.current = job.job_id;
    const selectedAsCurrent = job.selected_as_current === true;
    if (selectedAsCurrent) {
      setExplicitAssetId(producedAsset.asset_id);
      setCurrentImageFailed(false);
    }
    setCompletionNotice(
      selectedAsCurrent ? null : t("generatedNotSelected"),
    );
    setState((current) =>
      current
        ? {
            ...current,
            current_asset: selectedAsCurrent
              ? producedAsset
              : current.current_asset,
            assets: [
              producedAsset,
              ...current.assets.filter(
                (asset) => asset.asset_id !== producedAsset.asset_id,
              ),
            ],
          }
        : current,
    );
    if (selectedAsCurrent) {
      void onCoverChanged();
    }
  }, [job, onCoverChanged, producedAsset, t]);

  useEffect(() => {
    setCurrentImageFailed(false);
  }, [currentAsset?.asset_id, explicitAssetId, coverImage]);

  const assets = useMemo(() => {
    const byId = new Map<string, ImageAsset>();
    if (producedAsset) byId.set(producedAsset.asset_id, producedAsset);
    for (const asset of state?.assets ?? []) {
      if (!byId.has(asset.asset_id)) byId.set(asset.asset_id, asset);
    }
    return Array.from(byId.values());
  }, [producedAsset, state?.assets]);
  const cleanupJob =
    cleanupJobState?.cleanup_pending ? cleanupJobState : null;
  const activeJob = Boolean((job && !job.terminal) || cleanupJob);
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
  const parsedWidth = Number(width);
  const parsedHeight = Number(height);
  const dimensionsValid =
    Number.isInteger(parsedWidth) &&
    Number.isInteger(parsedHeight) &&
    parsedWidth >= 64 &&
    parsedWidth <= 4096 &&
    parsedHeight >= 64 &&
    parsedHeight <= 4096;
  const explicitAssetMissing = Boolean(
    explicitAssetId &&
      (
        !currentAsset ||
        currentAsset.asset_id !== explicitAssetId ||
        currentAsset.state === "missing" ||
        currentImageFailed
      ),
  );
  const manualCoverVisible =
    !explicitAssetId && Boolean(coverImage) && !currentImageFailed;

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
          scope: "novel",
          volume_id: null,
          chapter_id: null,
          character_card_id: null,
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
    if (
      !prompt ||
      !dimensionsValid ||
      submitting ||
      activeJob ||
      hasUnsavedChanges
    ) {
      return;
    }
    setSubmitting(true);
    setError(null);
    setCompletionNotice(null);
    try {
      const next = await apiPost<NovelCoverJob>(jobBase, {
        prompt,
        width: parsedWidth,
        height: parsedHeight,
      });
      adoptJob(next);
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : t("submitFailed"),
      );
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

  const chooseAsset = async (asset: ImageAsset | null) => {
    const assetId = asset?.asset_id ?? null;
    if (selectingAssetId !== undefined) return;
    setSelectingAssetId(assetId);
    setError(null);
    try {
      await apiPut(`${coverPath}/current`, { asset_id: assetId });
      setExplicitAssetId(assetId);
      setCompletionNotice(null);
      setState((current) =>
        current ? { ...current, current_asset: asset } : current,
      );
      setCurrentImageFailed(false);
      await onCoverChanged();
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : t("selectFailed"),
      );
    } finally {
      setSelectingAssetId(undefined);
    }
  };

  return (
    <section
      aria-labelledby="novel-cover-title"
      className="min-w-0 rounded-xl border border-border bg-surface px-5 py-5"
    >
      <div className="flex min-w-0 flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3
            id="novel-cover-title"
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
      {completionNotice && (
        <div
          role="status"
          className="mt-4 rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-100"
        >
          {completionNotice}
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
          <p className="mb-2 text-sm font-medium text-foreground">
            {t("currentTitle")}
          </p>
          <div className="relative aspect-[2/3] overflow-hidden rounded-xl border border-border bg-surface-secondary">
            {explicitAssetId &&
            currentAsset &&
            currentAsset.asset_id === explicitAssetId &&
            currentAsset.state === "available" &&
            currentAsset.content_url &&
            !currentImageFailed ? (
              <Image
                src={getImageUrl(currentAsset.content_url)}
                alt={t("currentAlt", { title: novelTitle })}
                fill
                sizes="(min-width: 1024px) 15rem, 100vw"
                unoptimized
                className="object-cover"
                onError={() => setCurrentImageFailed(true)}
              />
            ) : manualCoverVisible ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={getImageUrl(coverImage)}
                alt={t("currentAlt", { title: novelTitle })}
                className="h-full w-full object-cover"
                onError={() => setCurrentImageFailed(true)}
              />
            ) : explicitAssetMissing ? (
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

          <fieldset className="mt-5 min-w-0">
            <legend className="text-sm font-medium text-foreground">
              {t("dimensionsTitle")}
            </legend>
            <p className="mt-1 text-xs leading-5 text-muted">
              {t("dimensionsHint")}
            </p>
            <div className="mt-3 grid min-w-0 grid-cols-2 gap-3">
              <label className="min-w-0 text-sm">
                <span className="mb-1.5 block text-muted">
                  {t("width")}
                </span>
                <input
                  aria-label={t("width")}
                  type="number"
                  min={64}
                  max={4096}
                  step={8}
                  value={width}
                  onChange={(event) => setWidth(event.target.value)}
                  className="w-full min-w-0 rounded-lg border border-border bg-surface px-3 py-2.5 text-sm text-foreground outline-none focus:border-accent focus:ring-2 focus:ring-accent/15"
                />
              </label>
              <label className="min-w-0 text-sm">
                <span className="mb-1.5 block text-muted">
                  {t("height")}
                </span>
                <input
                  aria-label={t("height")}
                  type="number"
                  min={64}
                  max={4096}
                  step={8}
                  value={height}
                  onChange={(event) => setHeight(event.target.value)}
                  className="w-full min-w-0 rounded-lg border border-border bg-surface px-3 py-2.5 text-sm text-foreground outline-none focus:border-accent focus:ring-2 focus:ring-accent/15"
                />
              </label>
            </div>
            {!dimensionsValid && (
              <p role="alert" className="mt-2 text-sm text-red-600 dark:text-red-400">
                {t("dimensionsInvalid")}
              </p>
            )}
          </fieldset>

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
                  !dimensionsValid ||
                  submitting ||
                  activeJob ||
                  !provider?.available ||
                  hasUnsavedChanges
                }
                onPress={() => void submit()}
              >
                {submitting ? t("submitting") : t("generate")}
              </Button>
            )}
            onCancel={() => void cancel()}
            onRetryCleanup={() => void retryCleanup()}
          />
        </div>
      </div>

      <div className="mt-6 border-t border-border pt-5">
        <div className="flex min-w-0 flex-wrap items-end justify-between gap-3">
          <div className="min-w-0">
            <h4 className="text-sm font-semibold text-foreground">
              {t("historyTitle")}
            </h4>
            <p className="mt-1 text-xs leading-5 text-muted">
              {t("historyDescription")}
            </p>
          </div>
          {coverImage && explicitAssetId && (
            <Button
              variant="outline"
              size="sm"
              isDisabled={selectingAssetId !== undefined}
              onPress={() => void chooseAsset(null)}
            >
              {selectingAssetId === null
                ? t("selecting")
                : t("useManualCover")}
            </Button>
          )}
        </div>
        {assets.length === 0 ? (
          <p className="mt-3 text-sm text-muted">{t("historyEmpty")}</p>
        ) : (
          <ul className="mt-4 grid min-w-0 grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5">
            {assets.map((asset) => {
              const selected = explicitAssetId === asset.asset_id;
              return (
                <li key={asset.asset_id} className="min-w-0">
                  <CoverHistoryItem
                    asset={asset}
                    selected={selected}
                    busy={selectingAssetId !== undefined}
                    selecting={selectingAssetId === asset.asset_id}
                    alt={t("historyAlt", { title: novelTitle })}
                    selectedLabel={t("currentBadge")}
                    selectLabel={t("useThisCover")}
                    selectingLabel={t("selecting")}
                    missingLabel={t("assetMissing")}
                    onSelect={() => void chooseAsset(asset)}
                  />
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </section>
  );
}

function CoverHistoryItem({
  asset,
  selected,
  busy,
  selecting,
  alt,
  selectedLabel,
  selectLabel,
  selectingLabel,
  missingLabel,
  onSelect,
}: {
  asset: ImageAsset;
  selected: boolean;
  busy: boolean;
  selecting: boolean;
  alt: string;
  selectedLabel: string;
  selectLabel: string;
  selectingLabel: string;
  missingLabel: string;
  onSelect: () => void;
}) {
  const [failed, setFailed] = useState(false);
  const missing =
    failed || asset.state === "missing" || !asset.content_url;
  return (
    <div className="min-w-0 rounded-lg border border-border bg-surface-secondary p-2">
      <div className="relative aspect-[2/3] overflow-hidden rounded-md bg-surface">
        {!missing ? (
          <Image
            src={getImageUrl(asset.content_url)}
            alt={alt}
            fill
            sizes="(min-width: 1024px) 9rem, 45vw"
            unoptimized
            className="object-cover"
            onError={() => setFailed(true)}
          />
        ) : (
          <div className="flex h-full items-center justify-center px-2 text-center text-xs text-muted">
            {missingLabel}
          </div>
        )}
      </div>
      <Button
        variant={selected ? "secondary" : "outline"}
        size="sm"
        className="mt-2 w-full min-w-0"
        aria-pressed={selected}
        isDisabled={selected || busy}
        onPress={onSelect}
      >
        {selected
          ? selectedLabel
          : selecting
            ? selectingLabel
            : selectLabel}
      </Button>
    </div>
  );
}
