"use client";

import { Button } from "@heroui/react";
import Image from "next/image";
import { useTranslations } from "next-intl";
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";
import IllustrationPromptEditor from "@/components/image/IllustrationPromptEditor";
import ImageJobStatusPanel from "@/components/image/ImageJobStatusPanel";
import StagedIllustrationWorkspace from "./StagedIllustrationWorkspace";
import { useImageJob } from "@/components/image/useImageJob";
import { apiGet, apiPost, getImageUrl } from "@/lib/api";
import { constrainIllustrationPromptEdit } from "@/lib/illustrationPrompt";
import type {
  AgentProfile,
  IllustrationPromptResult,
} from "@/types/agent";
import type {
  ImageAsset,
  SceneIllustrationCharacter,
  SceneIllustrationJob,
  SceneIllustrationState,
} from "@/types/image";
import type { StoredChapterOutline } from "./outline/outlineTypes";

interface SceneIllustrationPanelProps {
  novelId: string;
  chapterId: string;
  chapterTitle: string;
  chapterOutline: StoredChapterOutline;
  onClose: () => void;
  onOpenCharacterCards: () => void;
}

export default function SceneIllustrationPanel({
  novelId,
  chapterId,
  chapterTitle,
  chapterOutline,
  onClose,
  onOpenCharacterCards,
}: SceneIllustrationPanelProps) {
  const t = useTranslations("writing.sceneIllustration");
  const scenePath =
    `/api/novels/${novelId}/chapters/${chapterId}/scene-illustration`;
  const jobBase = `${scenePath}/jobs`;
  const dialogRef = useRef<HTMLDivElement>(null);
  const handledAssetJobId = useRef<string | null>(null);
  const [state, setState] = useState<SceneIllustrationState | null>(null);
  const [agents, setAgents] = useState<AgentProfile[]>([]);
  const [agentId, setAgentId] = useState("");
  const [selectedCharacterIds, setSelectedCharacterIds] = useState<
    string[]
  >([]);
  const [referenceCharacterId, setReferenceCharacterId] = useState("");
  const [prompt, setPrompt] =
    useState<IllustrationPromptResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [translating, setTranslating] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const {
    job,
    pollError,
    cancelling,
    adoptJob,
    cancel,
  } = useImageJob<SceneIllustrationJob>({ jobBase });
  const {
    job: cleanupJobState,
    pollError: cleanupPollError,
    cancelling: cleanupCancelling,
    adoptJob: adoptCleanupJob,
    cancel: retryCleanup,
  } = useImageJob<SceneIllustrationJob>({ jobBase });

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    setSelectedCharacterIds([]);
    setReferenceCharacterId("");
    setPrompt(null);
    const load = async () => {
      try {
        const [nextState, response] = await Promise.all([
          apiGet<SceneIllustrationState>(scenePath),
          apiGet<{ data: AgentProfile[] }>(
            "/api/agents?capability=illustration_prompt",
          ),
        ]);
        if (!active) return;
        setState(nextState);
        adoptJob(nextState.active_job);
        adoptCleanupJob(
          nextState.cleanup_job?.job_id ===
            nextState.active_job?.job_id
            ? null
            : nextState.cleanup_job,
        );
        const availableAgents = response.data.filter(
          (agent) =>
            agent.enabled &&
            agent.capabilities.includes("illustration_prompt"),
        );
        setAgents(availableAgents);
        setAgentId(availableAgents[0]?.agent_id ?? "");
        if (nextState.characters.length === 1) {
          const character = nextState.characters[0];
          setSelectedCharacterIds([character.card_id]);
          if (character.anchored && character.descriptor) {
            setReferenceCharacterId(character.card_id);
          }
        }
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
  }, [adoptCleanupJob, adoptJob, scenePath, t]);

  useEffect(() => {
    const previouslyFocused =
      document.activeElement instanceof HTMLElement
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

  useEffect(() => {
    if (
      !job?.terminal ||
      !job.asset ||
      handledAssetJobId.current === job.job_id
    ) {
      return;
    }
    handledAssetJobId.current = job.job_id;
    const asset = job.asset;
    setState((current) =>
      current
        ? {
            ...current,
            assets: [
              asset,
              ...current.assets.filter(
                (candidate) => candidate.asset_id !== asset.asset_id,
              ),
            ],
          }
        : current,
    );
  }, [job]);

  const selectedCharacters = useMemo(
    () =>
      (state?.characters ?? []).filter((character) =>
        selectedCharacterIds.includes(character.card_id),
      ),
    [selectedCharacterIds, state?.characters],
  );
  const selectedUnanchored = selectedCharacters.filter(
    (character) => !character.anchored || !character.descriptor,
  );
  const referenceCharacter =
    selectedCharacters.find(
      (character) =>
        character.card_id === referenceCharacterId &&
        character.anchored &&
        character.descriptor,
    ) ?? null;
  const cleanupJob =
    cleanupJobState?.cleanup_pending ? cleanupJobState : null;
  const activeJob = Boolean((job && !job.terminal) || cleanupJob);
  const provider = job?.provider ?? state?.provider ?? null;
  const templateSupportsReference =
    Boolean(provider) && provider?.reference_mode !== "none";
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
  const assets = useMemo(() => {
    const byId = new Map<string, ImageAsset>();
    if (job?.asset) byId.set(job.asset.asset_id, job.asset);
    for (const asset of state?.assets ?? []) {
      if (!byId.has(asset.asset_id)) byId.set(asset.asset_id, asset);
    }
    return Array.from(byId.values());
  }, [job?.asset, state?.assets]);
  const canSubmit =
    Boolean(prompt) &&
    selectedCharacters.length > 0 &&
    selectedUnanchored.length === 0 &&
    Boolean(referenceCharacter) &&
    Boolean(provider?.available) &&
    templateSupportsReference &&
    !submitting &&
    !activeJob;

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

  const toggleCharacter = (cardId: string, selected: boolean) => {
    const nextIds = selected
      ? Array.from(new Set([...selectedCharacterIds, cardId]))
      : selectedCharacterIds.filter((candidate) => candidate !== cardId);
    setSelectedCharacterIds(nextIds);
    const nextCharacters = (state?.characters ?? []).filter((character) =>
      nextIds.includes(character.card_id),
    );
    if (
      nextCharacters.length === 1 &&
      nextCharacters[0].anchored &&
      nextCharacters[0].descriptor
    ) {
      setReferenceCharacterId(nextCharacters[0].card_id);
    } else {
      setReferenceCharacterId("");
    }
  };

  const translate = async () => {
    if (!agentId || translating || activeJob) return;
    setTranslating(true);
    setError(null);
    try {
      const response = await apiPost<{ result: IllustrationPromptResult }>(
        "/api/llm/agent-illustration-prompt",
        {
          novel_id: novelId,
          scope: "chapter",
          volume_id: null,
          chapter_id: chapterId,
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
    if (!prompt || !canSubmit || !referenceCharacter) return;
    setSubmitting(true);
    setError(null);
    try {
      const next = await apiPost<SceneIllustrationJob>(jobBase, {
        prompt,
        scene_character_card_ids: selectedCharacters.map(
          (character) => character.card_id,
        ),
        reference_character_card_id: referenceCharacter.card_id,
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
    ).filter((element) => element.offsetParent !== null);
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    const active = document.activeElement;
    if (event.shiftKey && active === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    }
  };

  return (
    <div className="absolute inset-0 z-30 flex items-center justify-center bg-black/25 px-3 py-4 sm:px-4 sm:py-6">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="scene-illustration-title"
        onKeyDown={handleDialogKeyDown}
        className="flex max-h-full w-full max-w-6xl min-w-0 flex-col overflow-hidden rounded-xl border border-border bg-surface shadow-lg"
      >
        <header className="flex min-w-0 items-start justify-between gap-3 border-b border-border px-4 py-4 sm:px-5">
          <div className="min-w-0">
            <h3
              id="scene-illustration-title"
              className="truncate text-base font-semibold text-foreground"
            >
              {t("title")}
            </h3>
            <p className="mt-1 line-clamp-2 text-xs leading-5 text-muted">
              {t("description", { chapter: chapterTitle })}
            </p>
          </div>
          <Button
            variant="ghost"
            size="sm"
            className="shrink-0"
            onPress={onClose}
          >
            {t("close")}
          </Button>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4 sm:px-5">
          {loading && (
            <p role="status" className="text-sm text-muted">
              {t("loading")}
            </p>
          )}
          {error && (
            <div
              role="alert"
              className="rounded-lg border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-800 dark:border-red-900 dark:bg-red-950 dark:text-red-200"
            >
              {error}
            </div>
          )}

          {!loading && state && (
            <>
              <div className="flex min-w-0 flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <h4 className="text-sm font-semibold text-foreground">
                    {t("charactersTitle")}
                  </h4>
                  <p className="mt-1 max-w-3xl text-xs leading-5 text-muted">
                    {t("charactersHint")}
                  </p>
                </div>
                <div className="min-w-0 max-w-full text-left text-xs text-muted sm:text-right">
                  <p className="truncate" title={provider?.alias}>
                    {t("provider", {
                      provider: provider?.alias ?? "",
                    })}
                  </p>
                  <p className="truncate" title={provider?.model}>
                    {t("model", { model: provider?.model ?? "" })}
                  </p>
                  {!job &&
                    provider?.queue_position !== null &&
                    provider?.queue_position !== undefined && (
                      <p>
                        {t("providerQueuePosition", {
                          count: provider.queue_position,
                        })}
                      </p>
                    )}
                  {!job &&
                    provider?.estimated_seconds !== null &&
                    provider?.estimated_seconds !== undefined && (
                      <p>
                        {t("providerEstimated", {
                          duration: formatDuration(
                            provider.estimated_seconds,
                          ),
                        })}
                      </p>
                    )}
                </div>
              </div>

              {state.characters.length === 0 ? (
                <div
                  role="status"
                  className="mt-4 rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-100"
                >
                  {t("noCharacters")}
                </div>
              ) : (
                <fieldset className="mt-4 min-w-0">
                  <legend className="sr-only">{t("charactersTitle")}</legend>
                  <ul className="grid min-w-0 gap-2 md:grid-cols-2 xl:grid-cols-3">
                    {state.characters.map((character) => {
                      const selected = selectedCharacterIds.includes(
                        character.card_id,
                      );
                      return (
                        <li
                          key={character.card_id}
                          className={`min-w-0 rounded-lg border px-3 py-3 ${
                            selected
                              ? "border-accent bg-accent/5"
                              : "border-border bg-surface-secondary"
                          }`}
                        >
                          <label className="flex min-w-0 cursor-pointer items-start gap-2">
                            <input
                              type="checkbox"
                              checked={selected}
                              onChange={(event) =>
                                toggleCharacter(
                                  character.card_id,
                                  event.target.checked,
                                )
                              }
                              aria-label={t("includeCharacter", {
                                name: character.name,
                              })}
                              className="mt-0.5 shrink-0 accent-[var(--color-accent)]"
                            />
                            <span className="min-w-0">
                              <span className="block truncate text-sm font-medium text-foreground">
                                {character.name}
                              </span>
                              <span
                                className={`mt-0.5 block text-xs ${
                                  character.anchored &&
                                  character.descriptor
                                    ? "text-green-700 dark:text-green-400"
                                    : "text-amber-700 dark:text-amber-300"
                                }`}
                              >
                                {character.anchored &&
                                character.descriptor
                                  ? t("anchorAvailable")
                                  : t("anchorMissing")}
                              </span>
                            </span>
                          </label>
                        </li>
                      );
                    })}
                  </ul>
                </fieldset>
              )}

              {selectedUnanchored.length > 0 && (
                <div
                  role="alert"
                  className="mt-4 rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-100"
                >
                  <p>
                    {t("selectedMissingAnchor", {
                      names: selectedUnanchored
                        .map((character) => character.name)
                        .join(t("nameSeparator")),
                    })}
                  </p>
                  <Button
                    variant="outline"
                    size="sm"
                    className="mt-3"
                    onPress={() => {
                      onClose();
                      onOpenCharacterCards();
                    }}
                  >
                    {t("openCharacterCards")}
                  </Button>
                </div>
              )}

              {selectedCharacters.length > 0 && (
                <section
                  aria-labelledby="scene-reference-character-title"
                  className="mt-5 min-w-0 rounded-xl border border-border bg-surface-secondary px-4 py-4"
                >
                  <h4
                    id="scene-reference-character-title"
                    className="text-sm font-semibold text-foreground"
                  >
                    {t("referenceTitle")}
                  </h4>
                  <p className="mt-1 text-xs leading-5 text-muted">
                    {t("referenceHint")}
                  </p>
                  <fieldset className="mt-3 min-w-0">
                    <legend className="sr-only">{t("referenceTitle")}</legend>
                    <div className="flex min-w-0 flex-wrap gap-2">
                      {selectedCharacters.map((character) => {
                        const anchored =
                          character.anchored &&
                          Boolean(character.descriptor);
                        return (
                          <label
                            key={character.card_id}
                            className={`flex min-w-0 items-center gap-2 rounded-lg border px-3 py-2 text-sm ${
                              anchored
                                ? "cursor-pointer border-border bg-surface text-foreground"
                                : "cursor-not-allowed border-border/70 text-muted"
                            }`}
                          >
                            <input
                              type="radio"
                              name="scene-reference-character"
                              value={character.card_id}
                              checked={
                                referenceCharacterId ===
                                character.card_id
                              }
                              disabled={!anchored}
                              onChange={() =>
                                setReferenceCharacterId(
                                  character.card_id,
                                )
                              }
                              aria-label={t("referenceFor", {
                                name: character.name,
                              })}
                              className="shrink-0 accent-[var(--color-accent)]"
                            />
                            <span className="min-w-0 truncate">
                              {character.name}
                            </span>
                          </label>
                        );
                      })}
                    </div>
                  </fieldset>
                  {selectedCharacters.length > 1 && (
                    <p className="mt-3 rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-100">
                      {t("singleReferenceBoundary")}
                    </p>
                  )}
                </section>
              )}

              {referenceCharacter && (
                <section
                  aria-labelledby="scene-anchor-prefix-title"
                  className="mt-5 min-w-0 rounded-xl border border-accent/35 bg-accent/5 px-4 py-4"
                >
                  <h4
                    id="scene-anchor-prefix-title"
                    className="text-sm font-semibold text-foreground"
                  >
                    {t("prefixTitle")}
                  </h4>
                  <p className="mt-1 text-xs leading-5 text-muted">
                    {t("prefixHint")}
                  </p>
                  <dl className="mt-3 min-w-0 space-y-2">
                    {selectedCharacters
                      .filter(
                        (character): character is SceneIllustrationCharacter & {
                          descriptor: string;
                        } =>
                          character.anchored &&
                          Boolean(character.descriptor),
                      )
                      .map((character) => (
                        <div
                          key={character.card_id}
                          className="min-w-0 rounded-lg bg-surface px-3 py-2"
                        >
                          <dt className="flex min-w-0 flex-wrap items-center gap-2 text-xs font-medium text-foreground">
                            <span className="truncate">
                              {character.name}
                            </span>
                            {character.card_id ===
                              referenceCharacter.card_id && (
                              <span className="rounded-full bg-accent/10 px-2 py-0.5 text-[11px] text-accent">
                                {t("referenceBadge")}
                              </span>
                            )}
                          </dt>
                          <dd className="mt-1 break-words text-sm leading-6 text-foreground">
                            {character.descriptor}
                          </dd>
                        </div>
                      ))}
                  </dl>
                  <p className="mt-3 text-xs leading-5 text-muted">
                    {t("consistencyBoundary")}
                  </p>
                </section>
              )}

              {provider && !provider.available && (
                <div
                  role="alert"
                  className="mt-4 rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-100"
                >
                  {t("providerUnavailable")}
                </div>
              )}
              {provider && !templateSupportsReference && (
                <div
                  role="alert"
                  className="mt-4 rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-100"
                >
                  {t("templateUnsupported")}
                </div>
              )}
              {warnings.length > 0 && (
                <div className="mt-4 rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-100">
                  <p className="font-medium">{t("warningsTitle")}</p>
                  <ul className="mt-1 list-disc space-y-1 pl-5">
                    {warnings.map((warning, index) => (
                      <li
                        key={`${warning}-${index}`}
                        className="break-words"
                      >
                        {warning}
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              <div className="mt-5 flex min-w-0 flex-wrap items-end gap-3">
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
                    !agentId
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
                  totalLabel={(count) =>
                    t("promptCount", { count })
                  }
                  onChange={updatePrompt}
                />
              )}

              <StagedIllustrationWorkspace
                novelId={novelId}
                chapterId={chapterId}
                chapterTitle={chapterTitle}
                outline={chapterOutline}
                characters={state.characters}
                selectedCharacterIds={selectedCharacterIds}
                referenceCharacterId={referenceCharacterId}
                prompt={prompt}
                legacyAssets={assets}
              />

              <div className="mt-6 rounded-lg border border-dashed border-border bg-surface-secondary/30 p-3">
                <p className="text-sm font-semibold text-foreground">{t("legacyModeTitle")}</p>
                <p className="mt-1 text-xs leading-5 text-muted">{t("legacyModeDescription")}</p>
              </div>

              <ImageJobStatusPanel                job={job}
                cleanupJob={cleanupJob}
                pollError={pollError}
                cleanupPollError={cleanupPollError}
                cancelling={cancelling}
                cleanupCancelling={cleanupCancelling}
                formatDuration={formatDuration}
                labels={{
                  status: (status) => t(`status.${status}`),
                  queuePosition: (count) =>
                    t("queuePosition", { count }),
                  estimated: (duration) =>
                    t("estimated", { duration }),
                  elapsed: (duration) => t("elapsed", { duration }),
                  completedImages: (count) =>
                    t("completedImages", { count }),
                  pollRetrying: t("pollRetrying"),
                  cleanupPending: t("cleanupPending"),
                  olderCleanupTitle: t("olderCleanupTitle"),
                  olderCleanupDescription: t(
                    "olderCleanupDescription",
                  ),
                  retryCleanup: t("retryCleanup"),
                  retryingCleanup: t("retryingCleanup"),
                  abandon: t("abandon"),
                  abandoning: t("abandoning"),
                  abandonWarning: t("abandonWarning"),
                  ignoredDimensions: t("ignoredDimensions"),
                  ignoredSlots: (slots) =>
                    t("ignoredSlots", { slots }),
                  pollUnavailable: t("pollUnavailable"),
                  cancel: t("cancel"),
                  cancelling: t("cancelling"),
                }}
                primaryAction={(
                  <Button
                    variant="primary"
                    className="bg-accent text-white hover:bg-accent-hover"
                    isDisabled={!canSubmit}
                    onPress={() => void submit()}
                  >
                    {submitting ? t("submitting") : t("generate")}
                  </Button>
                )}
                onCancel={() => void cancel()}
                onRetryCleanup={() => void retryCleanup()}
              />

              <section
                aria-labelledby="scene-illustration-history-title"
                className="mt-6 border-t border-border pt-5"
              >
                <h4
                  id="scene-illustration-history-title"
                  className="text-sm font-semibold text-foreground"
                >
                  {t("historyTitle")}
                </h4>
                <p className="mt-1 text-xs leading-5 text-muted">
                  {t("historyDescription")}
                </p>
                {assets.length === 0 ? (
                  <p className="mt-3 text-sm text-muted">
                    {t("historyEmpty")}
                  </p>
                ) : (
                  <ul className="mt-4 grid min-w-0 grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
                    {assets.map((asset) => (
                      <li key={asset.asset_id} className="min-w-0">
                        <SceneAssetCard
                          asset={asset}
                          alt={t("imageAlt", { chapter: chapterTitle })}
                          missingLabel={t("assetMissing")}
                        />
                      </li>
                    ))}
                  </ul>
                )}
              </section>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function SceneAssetCard({
  asset,
  alt,
  missingLabel,
}: {
  asset: ImageAsset;
  alt: string;
  missingLabel: string;
}) {
  const [failed, setFailed] = useState(false);
  const missing =
    failed || asset.state === "missing" || !asset.content_url;
  return (
    <div className="min-w-0 overflow-hidden rounded-lg border border-border bg-surface-secondary">
      <div className="relative aspect-[3/2] bg-surface">
        {!missing ? (
          <Image
            src={getImageUrl(asset.content_url)}
            alt={alt}
            fill
            sizes="(min-width: 1024px) 14rem, 45vw"
            unoptimized
            className="object-cover"
            onError={() => setFailed(true)}
          />
        ) : (
          <div
            role="status"
            className="flex h-full items-center justify-center px-3 text-center text-xs text-muted"
          >
            {missingLabel}
          </div>
        )}
      </div>
    </div>
  );
}
