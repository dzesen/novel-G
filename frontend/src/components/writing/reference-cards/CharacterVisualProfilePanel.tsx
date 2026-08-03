"use client";

import { Button } from "@heroui/react";
import Image from "next/image";
import { useTranslations } from "next-intl";
import {
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";
import {
  ApiError,
  apiDelete,
  apiGet,
  apiPost,
  apiPut,
  getImageUrl,
} from "@/lib/api";
import type {
  CharacterPortraitAsset,
  CharacterVisualProfile,
  CharacterVisualReference,
  ExternalLoraAdapter,
} from "@/types/image";

interface CharacterVisualProfilePanelProps {
  novelId: string;
  cardId: string;
  cardName: string;
  currentAsset: CharacterPortraitAsset | null;
}

interface AdapterDraft {
  loraName: string;
  triggerWord: string;
  strength: string;
  baseModelFamily: string;
  versionNote: string;
}

const EMPTY_ADAPTER_DRAFT: AdapterDraft = {
  loraName: "",
  triggerWord: "",
  strength: "1",
  baseModelFamily: "",
  versionNote: "",
};

const pendingProfileLoads = new Map<
  string,
  Promise<CharacterVisualProfile>
>();

function loadVisualProfile(path: string): Promise<CharacterVisualProfile> {
  const pending = pendingProfileLoads.get(path);
  if (pending) return pending;

  const request = apiGet<CharacterVisualProfile>(path);
  pendingProfileLoads.set(path, request);
  void request.then(
    () => {
      if (pendingProfileLoads.get(path) === request) {
        pendingProfileLoads.delete(path);
      }
    },
    () => {
      if (pendingProfileLoads.get(path) === request) {
        pendingProfileLoads.delete(path);
      }
    },
  );
  return request;
}

function adapterToDraft(
  adapter: ExternalLoraAdapter | null,
): AdapterDraft {
  if (!adapter) return { ...EMPTY_ADAPTER_DRAFT };
  return {
    loraName: adapter.lora_name,
    triggerWord: adapter.trigger_word ?? "",
    strength: String(adapter.strength),
    baseModelFamily: adapter.base_model_family,
    versionNote: adapter.version_note ?? "",
  };
}

function optionalText(value: string | null): string | null {
  const normalized = (value ?? "").trim();
  return normalized || null;
}

function referencePayload(
  reference: CharacterVisualReference,
): CharacterVisualReference {
  return {
    asset_id: reference.asset_id,
    view: optionalText(reference.view),
    framing: optionalText(reference.framing),
    expression: optionalText(reference.expression),
    costume: optionalText(reference.costume),
    note: optionalText(reference.note),
  };
}

export default function CharacterVisualProfilePanel({
  novelId,
  cardId,
  cardName,
  currentAsset,
}: CharacterVisualProfilePanelProps) {
  const t = useTranslations("writing.referenceCards.visualProfile");
  const profilePath =
    `/api/reference-cards/novel/${novelId}/character/${cardId}/visual-profile`;
  const [profile, setProfile] =
    useState<CharacterVisualProfile | null>(null);
  const [references, setReferences] = useState<
    CharacterVisualReference[]
  >([]);
  const [adapterDraft, setAdapterDraft] = useState<AdapterDraft>(
    EMPTY_ADAPTER_DRAFT,
  );
  const [loading, setLoading] = useState(true);
  const [adding, setAdding] = useState(false);
  const [savingReferences, setSavingReferences] = useState(false);
  const [removingAssetId, setRemovingAssetId] = useState<string | null>(
    null,
  );
  const [savingAdapter, setSavingAdapter] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conflicted, setConflicted] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [failedImages, setFailedImages] = useState<Set<string>>(
    () => new Set(),
  );

  const adoptProfile = useCallback(
    (
      next: CharacterVisualProfile,
      options: {
        references?: boolean;
        adapter?: boolean;
      } = { references: true, adapter: true },
    ) => {
      setProfile(next);
      if (options.references !== false) {
        setReferences(
          next.references.map((reference) => ({ ...reference })),
        );
      }
      if (options.adapter !== false) {
        setAdapterDraft(adapterToDraft(next.external_adapter));
      }
      setFailedImages(new Set());
      setError(null);
      setConflicted(false);
    },
    [],
  );

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    setConflicted(false);
    setNotice(null);
    try {
      adoptProfile(await loadVisualProfile(profilePath));
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : t("loadFailed"),
      );
    } finally {
      setLoading(false);
    }
  }, [adoptProfile, profilePath, t]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const busy =
    adding ||
    savingReferences ||
    Boolean(removingAssetId) ||
    savingAdapter;
  const currentAssetId =
    currentAsset?.state === "available" ? currentAsset.asset_id : null;
  const currentAlreadyAdded = Boolean(
    currentAssetId &&
      references.some(
        (reference) => reference.asset_id === currentAssetId,
      ),
  );
  const canAddCurrent = Boolean(
    profile &&
      currentAssetId &&
      !currentAlreadyAdded &&
      references.length < 32 &&
      !busy,
  );
  const normalizedReferences = useMemo(
    () => references.map(referencePayload),
    [references],
  );
  const referencesDirty = useMemo(
    () =>
      Boolean(
        profile &&
          JSON.stringify(normalizedReferences) !==
            JSON.stringify(profile.references),
      ),
    [normalizedReferences, profile],
  );

  const showOperationError = (reason: unknown) => {
    setNotice(null);
    if (reason instanceof ApiError && reason.status === 409) {
      setConflicted(true);
      setError(t("revisionConflict"));
      return;
    }
    setConflicted(false);
    setError(
      reason instanceof Error ? reason.message : t("saveFailed"),
    );
  };

  const addCurrentReference = async () => {
    if (!profile || !canAddCurrent || !currentAssetId) return;
    setAdding(true);
    setError(null);
    setNotice(null);
    try {
      const next = await apiPost<CharacterVisualProfile>(
        `${profilePath}/references`,
        {
          expected_revision: profile.revision,
          reference: { asset_id: currentAssetId },
        },
      );
      adoptProfile(next, { references: true, adapter: false });
      setNotice(t("referenceAdded"));
    } catch (reason) {
      showOperationError(reason);
    } finally {
      setAdding(false);
    }
  };

  const saveReferenceLabels = async () => {
    if (!profile || !referencesDirty || busy) return;
    setSavingReferences(true);
    setError(null);
    setNotice(null);
    try {
      const next = await apiPut<CharacterVisualProfile>(profilePath, {
        expected_revision: profile.revision,
        references: normalizedReferences,
        external_adapter: profile.external_adapter,
      });
      adoptProfile(next, { references: true, adapter: false });
      setNotice(t("referencesSaved"));
    } catch (reason) {
      showOperationError(reason);
    } finally {
      setSavingReferences(false);
    }
  };

  const removeReference = async (assetId: string) => {
    if (!profile || busy) return;
    setRemovingAssetId(assetId);
    setError(null);
    setNotice(null);
    try {
      const next = await apiDelete<CharacterVisualProfile>(
        `${profilePath}/references/${encodeURIComponent(assetId)}` +
          `?expected_revision=${profile.revision}`,
      );
      adoptProfile(next, { references: true, adapter: false });
      setNotice(t("referenceRemoved"));
    } catch (reason) {
      showOperationError(reason);
    } finally {
      setRemovingAssetId(null);
    }
  };

  const buildAdapter = (): ExternalLoraAdapter | null => {
    const loraName = adapterDraft.loraName.trim();
    const baseModelFamily = adapterDraft.baseModelFamily.trim();
    const strength = Number(adapterDraft.strength);
    if (!loraName || !baseModelFamily) {
      setError(t("adapterRequired"));
      return null;
    }
    if (!Number.isFinite(strength) || strength < 0 || strength > 2) {
      setError(t("strengthInvalid"));
      return null;
    }
    return {
      kind: "lora",
      lora_name: loraName,
      trigger_word: optionalText(adapterDraft.triggerWord),
      strength,
      base_model_family: baseModelFamily,
      version_note: optionalText(adapterDraft.versionNote),
    };
  };

  const saveAdapter = async () => {
    if (!profile || busy) return;
    setError(null);
    setNotice(null);
    const adapter = buildAdapter();
    if (!adapter) return;
    setSavingAdapter(true);
    try {
      const next = await apiPut<CharacterVisualProfile>(
        `${profilePath}/external-adapter`,
        {
          expected_revision: profile.revision,
          external_adapter: adapter,
        },
      );
      adoptProfile(next, { references: false, adapter: true });
      setNotice(t("adapterSaved"));
    } catch (reason) {
      showOperationError(reason);
    } finally {
      setSavingAdapter(false);
    }
  };

  const clearAdapter = async () => {
    if (!profile?.external_adapter || busy) return;
    setSavingAdapter(true);
    setError(null);
    setNotice(null);
    try {
      const next = await apiPut<CharacterVisualProfile>(
        `${profilePath}/external-adapter`,
        {
          expected_revision: profile.revision,
          external_adapter: null,
        },
      );
      adoptProfile(next, { references: false, adapter: true });
      setNotice(t("adapterCleared"));
    } catch (reason) {
      showOperationError(reason);
    } finally {
      setSavingAdapter(false);
    }
  };

  const updateReference = (
    index: number,
    field: Exclude<keyof CharacterVisualReference, "asset_id">,
    value: string,
  ) => {
    setReferences((current) =>
      current.map((reference, itemIndex) =>
        itemIndex === index
          ? { ...reference, [field]: value }
          : reference,
      ),
    );
    setNotice(null);
  };

  return (
    <section
      aria-labelledby={`visual-profile-title-${cardId}`}
      className="mt-8 min-w-0 border-t border-border pt-7"
    >
      <div className="flex min-w-0 flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 max-w-2xl">
          <h3
            id={`visual-profile-title-${cardId}`}
            className="text-base font-semibold text-foreground"
          >
            {t("title")}
          </h3>
          <p className="mt-1 text-sm leading-6 text-muted">
            {t("description")}
          </p>
        </div>
        {profile && (
          <span className="shrink-0 rounded-full border border-border bg-surface-secondary px-2.5 py-1 text-xs font-medium text-muted">
            {t("revision", { revision: profile.revision })}
          </span>
        )}
      </div>

      <div className="mt-4 grid min-w-0 gap-2 rounded-xl border border-border bg-surface-secondary p-4 text-sm leading-6 text-foreground sm:grid-cols-2">
        <p className="min-w-0 break-words">
          <span className="font-semibold">{t("referenceBoundary")}</span>{" "}
          {t("referenceBoundaryDetail")}
        </p>
        <p className="min-w-0 break-words">
          <span className="font-semibold">{t("loraBoundary")}</span>{" "}
          {t("loraBoundaryDetail")}
        </p>
      </div>

      {loading && (
        <p role="status" className="mt-4 text-sm text-muted">
          {t("loading")}
        </p>
      )}
      {error && (
        <div
          role="alert"
          className="mt-4 min-w-0 rounded-lg border border-red-300 bg-red-50 px-4 py-3 text-sm leading-6 text-red-800 dark:border-red-900 dark:bg-red-950 dark:text-red-200"
        >
          <p className="break-words">{error}</p>
          <Button
            type="button"
            size="sm"
            variant="outline"
            onPress={() => void refresh()}
            className="mt-2 w-full sm:w-auto"
          >
            {conflicted ? t("refresh") : t("retry")}
          </Button>
        </div>
      )}
      {notice && (
        <p
          role="status"
          className="mt-4 rounded-lg border border-accent/25 bg-accent/5 px-4 py-3 text-sm text-foreground"
        >
          {notice}
        </p>
      )}

      {!loading && profile && (
        <>
          <div className="mt-6 min-w-0">
            <div className="flex min-w-0 flex-wrap items-end justify-between gap-3">
              <div className="min-w-0">
                <h4 className="text-sm font-semibold text-foreground">
                  {t("anchorTitle")}
                </h4>
                {profile.appearance_anchor ? (
                  <p className="mt-1 max-w-2xl break-words text-sm leading-6 text-muted">
                    {profile.appearance_anchor.descriptor}
                  </p>
                ) : (
                  <p className="mt-1 text-sm text-muted">
                    {t("anchorEmpty")}
                  </p>
                )}
              </div>
              <span className="shrink-0 rounded-full bg-accent/10 px-2.5 py-1 text-xs font-medium text-accent">
                {profile.appearance_anchor
                  ? t("anchorReadOnly")
                  : t("anchorNotEstablished")}
              </span>
            </div>
          </div>

          <div className="mt-7 min-w-0 border-t border-border pt-6">
            <div className="flex min-w-0 flex-wrap items-start justify-between gap-3">
              <div className="min-w-0">
                <h4 className="text-sm font-semibold text-foreground">
                  {t("referencesTitle", { count: references.length })}
                </h4>
                <p className="mt-1 max-w-2xl text-sm leading-6 text-muted">
                  {t("referencesDescription")}
                </p>
              </div>
              <Button
                type="button"
                size="sm"
                variant="outline"
                isDisabled={!canAddCurrent}
                onPress={() => void addCurrentReference()}
                className="w-full sm:w-auto"
              >
                {adding ? t("addingReference") : t("addCurrent")}
              </Button>
            </div>
            <p className="mt-2 text-xs leading-5 text-muted">
              {!currentAssetId
                ? t("currentUnavailable")
                : currentAlreadyAdded
                  ? t("currentAlreadyAdded")
                  : references.length >= 32
                    ? t("referenceLimit")
                    : t("addCurrentHint")}
            </p>

            {references.length === 0 ? (
              <div className="mt-4 rounded-lg border border-dashed border-border px-4 py-6 text-center text-sm text-muted">
                {t("referencesEmpty")}
              </div>
            ) : (
              <div className="mt-4 grid min-w-0 gap-4 xl:grid-cols-2">
                {references.map((reference, index) => {
                  const imageFailed = failedImages.has(
                    reference.asset_id,
                  );
                  return (
                    <article
                      key={reference.asset_id}
                      className="grid min-w-0 gap-4 rounded-xl border border-border bg-surface p-4 sm:grid-cols-[7rem_minmax(0,1fr)]"
                    >
                      <div className="min-w-0">
                        <div className="relative aspect-[2/3] overflow-hidden rounded-lg border border-border bg-surface-secondary">
                          {!imageFailed ? (
                            <Image
                              src={getImageUrl(
                                `/api/image-assets/${reference.asset_id}/content`,
                              )}
                              alt={t("referenceImageAlt", {
                                name: cardName,
                                index: index + 1,
                              })}
                              fill
                              sizes="7rem"
                              unoptimized
                              className="object-cover"
                              onError={() =>
                                setFailedImages((current) => {
                                  const next = new Set(current);
                                  next.add(reference.asset_id);
                                  return next;
                                })
                              }
                            />
                          ) : (
                            <div className="flex h-full items-center justify-center px-2 text-center text-xs leading-5 text-muted">
                              {t("imageMissing")}
                            </div>
                          )}
                        </div>
                        <p className="mt-2 truncate text-[0.6875rem] text-muted">
                          {t("referenceLabel", { index: index + 1 })}
                        </p>
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() =>
                            void removeReference(reference.asset_id)
                          }
                          className="mt-2 w-full rounded-lg border border-red-300 px-2.5 py-2 text-xs font-semibold text-red-700 transition-colors hover:bg-red-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-not-allowed disabled:opacity-50 dark:border-red-800 dark:text-red-300 dark:hover:bg-red-950"
                        >
                          {removingAssetId === reference.asset_id
                            ? t("removingReference")
                            : t("removeReference")}
                        </button>
                      </div>
                      <div className="grid min-w-0 gap-3 sm:grid-cols-2">
                        {(
                          [
                            "view",
                            "framing",
                            "expression",
                            "costume",
                          ] as const
                        ).map((field) => (
                          <label
                            key={field}
                            className="min-w-0 text-sm"
                          >
                            <span className="mb-1.5 block font-medium text-foreground">
                              {t("referenceFieldLabel", {
                                field: t(`fields.${field}`),
                                index: index + 1,
                              })}
                            </span>
                            <input
                              value={reference[field] ?? ""}
                              maxLength={
                                field === "costume" ? 240 : 120
                              }
                              onChange={(event) =>
                                updateReference(
                                  index,
                                  field,
                                  event.target.value,
                                )
                              }
                              className="w-full min-w-0 rounded-lg border border-border bg-surface px-3 py-2.5 text-base text-foreground outline-none focus:border-accent focus:ring-2 focus:ring-accent/15 sm:text-sm"
                            />
                          </label>
                        ))}
                        <label className="min-w-0 text-sm sm:col-span-2">
                          <span className="mb-1.5 block font-medium text-foreground">
                            {t("referenceFieldLabel", {
                              field: t("fields.note"),
                              index: index + 1,
                            })}
                          </span>
                          <textarea
                            value={reference.note ?? ""}
                            maxLength={500}
                            rows={3}
                            onChange={(event) =>
                              updateReference(
                                index,
                                "note",
                                event.target.value,
                              )
                            }
                            className="w-full min-w-0 resize-y rounded-lg border border-border bg-surface px-3 py-2.5 text-base leading-6 text-foreground outline-none focus:border-accent focus:ring-2 focus:ring-accent/15 sm:text-sm"
                          />
                        </label>
                      </div>
                    </article>
                  );
                })}
              </div>
            )}

            {references.length > 0 && (
              <div className="mt-4 flex flex-wrap items-center gap-3">
                <Button
                  type="button"
                  variant="primary"
                  isDisabled={!referencesDirty || busy}
                  onPress={() => void saveReferenceLabels()}
                  className="w-full bg-accent text-white hover:bg-accent-hover sm:w-auto"
                >
                  {savingReferences
                    ? t("savingReferences")
                    : t("saveReferences")}
                </Button>
                {referencesDirty && (
                  <p className="text-xs leading-5 text-muted">
                    {t("unsavedReferenceLabels")}
                  </p>
                )}
              </div>
            )}
          </div>

          <div className="mt-8 min-w-0 border-t border-border pt-6">
            <div className="min-w-0">
              <h4 className="text-sm font-semibold text-foreground">
                {t("adapterTitle")}
              </h4>
              <p className="mt-1 max-w-2xl text-sm leading-6 text-muted">
                {t("adapterDescription")}
              </p>
              {profile.external_adapter && (
                <p className="mt-3 min-w-0 break-all rounded-lg border border-accent/20 bg-accent/5 px-3 py-2 text-xs leading-5 text-foreground">
                  {profile.external_adapter.lora_name}
                </p>
              )}
            </div>

            <div className="mt-4 grid min-w-0 gap-4 sm:grid-cols-2">
              <ProfileField
                label={t("adapterFields.loraName")}
                value={adapterDraft.loraName}
                maxLength={500}
                onChange={(value) =>
                  setAdapterDraft((current) => ({
                    ...current,
                    loraName: value,
                  }))
                }
              />
              <ProfileField
                label={t("adapterFields.triggerWord")}
                value={adapterDraft.triggerWord}
                maxLength={300}
                onChange={(value) =>
                  setAdapterDraft((current) => ({
                    ...current,
                    triggerWord: value,
                  }))
                }
              />
              <ProfileField
                label={t("adapterFields.strength")}
                value={adapterDraft.strength}
                type="number"
                inputMode="decimal"
                min="0"
                max="2"
                step="0.05"
                onChange={(value) =>
                  setAdapterDraft((current) => ({
                    ...current,
                    strength: value,
                  }))
                }
              />
              <ProfileField
                label={t("adapterFields.baseModelFamily")}
                value={adapterDraft.baseModelFamily}
                maxLength={120}
                onChange={(value) =>
                  setAdapterDraft((current) => ({
                    ...current,
                    baseModelFamily: value,
                  }))
                }
              />
              <label className="min-w-0 text-sm sm:col-span-2">
                <span className="mb-1.5 block font-medium text-foreground">
                  {t("adapterFields.versionNote")}
                </span>
                <textarea
                  value={adapterDraft.versionNote}
                  maxLength={500}
                  rows={3}
                  onChange={(event) =>
                    setAdapterDraft((current) => ({
                      ...current,
                      versionNote: event.target.value,
                    }))
                  }
                  className="w-full min-w-0 resize-y rounded-lg border border-border bg-surface px-3 py-2.5 text-base leading-6 text-foreground outline-none focus:border-accent focus:ring-2 focus:ring-accent/15 sm:text-sm"
                />
              </label>
            </div>
            <p className="mt-2 text-xs leading-5 text-muted">
              {t("adapterHint")}
            </p>
            <div className="mt-4 flex min-w-0 flex-col gap-2 sm:flex-row sm:flex-wrap">
              <Button
                type="button"
                variant="primary"
                isDisabled={busy}
                onPress={() => void saveAdapter()}
                className="w-full bg-accent text-white hover:bg-accent-hover sm:w-auto"
              >
                {savingAdapter ? t("savingAdapter") : t("saveAdapter")}
              </Button>
              {profile.external_adapter && (
                <Button
                  type="button"
                  variant="outline"
                  isDisabled={busy}
                  onPress={() => void clearAdapter()}
                  className="w-full border-red-300 text-red-700 dark:border-red-800 dark:text-red-300 sm:w-auto"
                >
                  {t("clearAdapter")}
                </Button>
              )}
            </div>
          </div>
        </>
      )}
    </section>
  );
}

function ProfileField({
  label,
  value,
  onChange,
  type = "text",
  inputMode,
  maxLength,
  min,
  max,
  step,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  type?: "text" | "number";
  inputMode?: "text" | "decimal";
  maxLength?: number;
  min?: string;
  max?: string;
  step?: string;
}) {
  return (
    <label className="min-w-0 text-sm">
      <span className="mb-1.5 block font-medium text-foreground">
        {label}
      </span>
      <input
        type={type}
        inputMode={inputMode}
        value={value}
        maxLength={maxLength}
        min={min}
        max={max}
        step={step}
        onChange={(event) => onChange(event.target.value)}
        className="w-full min-w-0 rounded-lg border border-border bg-surface px-3 py-2.5 text-base text-foreground outline-none focus:border-accent focus:ring-2 focus:ring-accent/15 sm:text-sm"
      />
    </label>
  );
}
