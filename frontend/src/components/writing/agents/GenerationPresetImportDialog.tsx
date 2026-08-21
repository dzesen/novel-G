"use client";

import { useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";

import { ApiError, apiPostForm } from "@/lib/api";
import {
  composePresetInstruction,
  defaultPresetSelection,
  presetInstructionCharacterCount,
  presetSelectionKey,
} from "@/lib/generationPreset";
import type {
  AgentCapability,
  AgentCapabilityId,
} from "@/types/agent";
import type { GenerationPresetPreview } from "@/types/generationPreset";

export interface ImportedGenerationPresetDraft {
  label: string;
  description: string;
  capability: AgentCapabilityId;
  instruction: string;
  temperature: string;
  topP: string;
  maxTokens: string;
  presencePenalty: string;
  frequencyPenalty: string;
}

interface Props {
  capabilities: AgentCapability[];
  defaultCapability: AgentCapabilityId;
  onClose: () => void;
  onUseDraft: (draft: ImportedGenerationPresetDraft) => void;
}

function structuredErrorCode(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null;
  if (!error.detail || typeof error.detail !== "object") return null;
  const code = (error.detail as { code?: unknown }).code;
  return typeof code === "string" ? code : null;
}

function fileStem(filename: string): string {
  const stem = filename.replace(/\.json$/iu, "").trim();
  return stem || "Preset";
}

function truncateCodePoints(value: string, maximum: number): string {
  return Array.from(value).slice(0, maximum).join("");
}

function parameterValue(value: number | undefined, enabled: boolean): string {
  return enabled && value != null ? String(value) : "";
}

export default function GenerationPresetImportDialog({
  capabilities,
  defaultCapability,
  onClose,
  onUseDraft,
}: Props) {
  const t = useTranslations(
    "writing.agentStudio.management.presetImport",
  );
  const customizable = capabilities.filter((item) => item.customizable);
  const initialCapability = customizable.some(
    (item) => item.capability === defaultCapability,
  )
    ? defaultCapability
    : (customizable[0]?.capability ?? "creative_inspiration");

  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<GenerationPresetPreview | null>(null);
  const [profileIndex, setProfileIndex] = useState(0);
  const [selectedKeys, setSelectedKeys] = useState<Set<string>>(new Set());
  const [capability, setCapability] =
    useState<AgentCapabilityId>(initialCapability);
  const [applyParameters, setApplyParameters] = useState(true);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !loading) onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [loading, onClose]);

  const profile = preview?.order_profiles.find(
    (item) => item.profile_index === profileIndex,
  );
  const instruction = useMemo(
    () =>
      preview
        ? composePresetInstruction(preview, profileIndex, selectedKeys)
        : "",
    [preview, profileIndex, selectedKeys],
  );
  const instructionChars = presetInstructionCharacterCount(instruction);
  const maxInstructionChars = preview?.max_instruction_chars ?? 0;
  const overLimit = Boolean(
    preview && instructionChars > maxInstructionChars,
  );
  const selectedCount = profile
    ? profile.items.filter((item) =>
        selectedKeys.has(
          presetSelectionKey(profile.profile_index, item.order_index),
        ),
      ).length
    : 0;
  const missingReferenceCount =
    profile?.items.filter((item) => !item.resolved).length ?? 0;
  const isolatedItemCount =
    preview?.isolated_extensions.reduce(
      (total, item) => total + item.item_count,
      0,
    ) ?? 0;

  const resetSelection = (
    nextPreview: GenerationPresetPreview,
    nextProfileIndex: number,
  ) => {
    setProfileIndex(nextProfileIndex);
    setSelectedKeys(
      new Set(defaultPresetSelection(nextPreview, nextProfileIndex)),
    );
  };

  const loadPreview = async () => {
    if (!file) return;
    setLoading(true);
    setError("");
    try {
      const form = new FormData();
      form.append("file", file);
      const result = await apiPostForm<GenerationPresetPreview>(
        "/api/generation-presets/preview",
        form,
      );
      setPreview(result);
      resetSelection(
        result,
        result.default_profile_index ??
          result.order_profiles[0]?.profile_index ??
          0,
      );
    } catch (caught) {
      const code = structuredErrorCode(caught);
      if (code === "not_generation_preset") {
        setError(t("errors.notPreset"));
      } else if (code === "file_too_large") {
        setError(t("errors.tooLarge"));
      } else if (
        code === "unsupported_media_type" ||
        code === "invalid_extension"
      ) {
        setError(t("errors.fileType"));
      } else if (code === "duplicate_prompt_identifier") {
        setError(t("errors.duplicateIdentifier"));
      } else {
        setError(t("errors.previewFailed"));
      }
    } finally {
      setLoading(false);
    }
  };

  const toggleItem = (orderIndex: number) => {
    const key = presetSelectionKey(profileIndex, orderIndex);
    setSelectedKeys((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const useDraft = () => {
    if (!preview || !instruction || overLimit) return;
    const mapped = preview.mapped_generation_params;
    onUseDraft({
      label: truncateCodePoints(
        t("draftName", { name: fileStem(preview.source_name) }),
        64,
      ),
      description: truncateCodePoints(
        t("draftDescription", { name: preview.source_name }),
        500,
      ),
      capability,
      instruction,
      temperature: parameterValue(mapped.temperature, applyParameters),
      topP: parameterValue(mapped.top_p, applyParameters),
      maxTokens: parameterValue(mapped.max_tokens, applyParameters),
      presencePenalty: parameterValue(
        mapped.presence_penalty,
        applyParameters,
      ),
      frequencyPenalty: parameterValue(
        mapped.frequency_penalty,
        applyParameters,
      ),
    });
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-stretch justify-center bg-black/45 p-0 sm:items-center sm:p-5"
      role="presentation"
    >
      <section
        role="dialog"
        aria-modal="true"
        aria-labelledby="generation-preset-import-title"
        className="flex h-full w-full max-w-5xl flex-col overflow-hidden bg-background shadow-2xl sm:h-[min(860px,calc(100vh-2.5rem))] sm:rounded-2xl sm:border sm:border-border"
      >
        <header className="flex shrink-0 items-start justify-between gap-4 border-b border-border px-5 py-4 sm:px-7 sm:py-5">
          <div className="min-w-0">
            <h2
              id="generation-preset-import-title"
              className="text-xl font-semibold tracking-tight text-foreground"
            >
              {t("title")}
            </h2>
            <p className="mt-1 max-w-3xl text-sm leading-6 text-muted">
              {t("description")}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={loading}
            className="shrink-0 rounded-lg border border-border px-3 py-2 text-sm font-medium text-muted transition-colors hover:bg-surface-secondary hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:opacity-40"
          >
            {t("close")}
          </button>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-5 sm:px-7">
          {!preview ? (
            <div className="mx-auto max-w-2xl space-y-6 py-3 sm:py-8">
              <div>
                <label
                  htmlFor="generation-preset-file"
                  className="block text-sm font-semibold text-foreground"
                >
                  {t("fileLabel")}
                </label>
                <p className="mt-1 text-sm leading-6 text-muted">
                  {t("fileHint")}
                </p>
                <input
                  id="generation-preset-file"
                  type="file"
                  accept=".json,application/json"
                  autoFocus
                  disabled={loading}
                  className="mt-4 block w-full text-sm text-foreground file:mr-3 file:rounded-lg file:border-0 file:bg-accent/10 file:px-3 file:py-2 file:font-semibold file:text-accent"
                  onChange={(event) => {
                    setFile(event.target.files?.[0] ?? null);
                    setError("");
                  }}
                />
              </div>

              <div className="border-y border-border bg-surface-secondary/60 px-4 py-4 text-sm leading-6 text-muted sm:px-5">
                <p className="font-semibold text-foreground">
                  {t("safetyTitle")}
                </p>
                <p className="mt-1">{t("safetyDescription")}</p>
              </div>

              {error && (
                <p
                  role="alert"
                  className="rounded-lg border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-800 dark:border-red-900 dark:bg-red-950 dark:text-red-200"
                >
                  {error}
                </p>
              )}

              <div className="flex flex-wrap justify-end gap-2">
                <button
                  type="button"
                  onClick={onClose}
                  disabled={loading}
                  className="rounded-lg border border-border px-4 py-2 text-sm font-medium text-muted hover:bg-surface-secondary disabled:opacity-40"
                >
                  {t("cancel")}
                </button>
                <button
                  type="button"
                  onClick={() => void loadPreview()}
                  disabled={!file || loading}
                  className="rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {loading ? t("previewing") : t("preview")}
                </button>
              </div>
            </div>
          ) : (
            <div className="grid gap-6 lg:grid-cols-[minmax(16rem,0.72fr)_minmax(0,1.28fr)]">
              <aside className="space-y-5 lg:border-r lg:border-border lg:pr-6">
                <div>
                  <p className="break-all text-sm font-semibold text-foreground">
                    {preview.source_name}
                  </p>
                  <p className="mt-1 text-xs leading-5 text-muted">
                    {t("sourceSummary", {
                      active: preview.active_prompt_count,
                      total: preview.prompt_count,
                      characters: preview.active_prompt_chars,
                    })}
                  </p>
                </div>

                <div className="border-y border-border bg-surface-secondary/60 px-4 py-3 text-xs leading-5 text-muted">
                  <p className="font-semibold text-foreground">
                    {t("isolationTitle")}
                  </p>
                  <p className="mt-1">
                    {t("isolationSummary", {
                      items: isolatedItemCount,
                      missing: missingReferenceCount,
                    })}
                  </p>
                </div>

                {preview.order_profiles.length > 1 && (
                  <label className="block space-y-1.5 text-sm">
                    <span className="text-muted">{t("profileLabel")}</span>
                    <select
                      className="w-full rounded-lg border border-border bg-surface px-3 py-2 text-sm text-foreground outline-none focus:border-accent"
                      value={profileIndex}
                      onChange={(event) =>
                        resetSelection(preview, Number(event.target.value))
                      }
                    >
                      {preview.order_profiles.map((item, index) => (
                        <option
                          key={item.profile_index}
                          value={item.profile_index}
                        >
                          {t("profileOption", {
                            index: index + 1,
                            id: item.external_character_id ?? t("none"),
                          })}
                        </option>
                      ))}
                    </select>
                  </label>
                )}

                <label className="block space-y-1.5 text-sm">
                  <span className="text-muted">{t("capabilityLabel")}</span>
                  <select
                    className="w-full rounded-lg border border-border bg-surface px-3 py-2 text-sm text-foreground outline-none focus:border-accent"
                    value={capability}
                    onChange={(event) =>
                      setCapability(event.target.value as AgentCapabilityId)
                    }
                  >
                    {customizable.map((item) => (
                      <option key={item.capability} value={item.capability}>
                        {item.label}
                      </option>
                    ))}
                  </select>
                  <span className="block text-xs leading-5 text-muted">
                    {t("capabilityHint")}
                  </span>
                </label>

                <div className="space-y-2 border-t border-border pt-4">
                  <label className="flex items-start gap-2 text-sm text-muted">
                    <input
                      type="checkbox"
                      className="mt-1"
                      checked={applyParameters}
                      onChange={(event) =>
                        setApplyParameters(event.target.checked)
                      }
                    />
                    <span>
                      <span className="block font-medium text-foreground">
                        {t("applyParameters")}
                      </span>
                      <span className="mt-0.5 block text-xs leading-5">
                        {Object.keys(preview.mapped_generation_params).length
                          ? Object.entries(preview.mapped_generation_params)
                              .map(([key, value]) => `${key}=${value}`)
                              .join(" · ")
                          : t("noCompatibleParameters")}
                      </span>
                    </span>
                  </label>
                  {preview.unsupported_generation_params.length > 0 && (
                    <p className="text-xs leading-5 text-amber-700 dark:text-amber-300">
                      {t("unsupportedParameters", {
                        fields: preview.unsupported_generation_params
                          .map((item) => item.field)
                          .join(", "),
                      })}
                    </p>
                  )}
                </div>

                {preview.unassigned_prompts.length > 0 && (
                  <p className="border-t border-border pt-4 text-xs leading-5 text-muted">
                    {t("unassignedPrompts", {
                      count: preview.unassigned_prompts.length,
                    })}
                  </p>
                )}
              </aside>

              <section className="min-w-0">
                <div className="flex flex-wrap items-end justify-between gap-3 border-b border-border pb-3">
                  <div>
                    <h3 className="font-semibold text-foreground">
                      {t("promptListTitle")}
                    </h3>
                    <p className="mt-1 text-xs leading-5 text-muted">
                      {t("promptListHint")}
                    </p>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <button
                      type="button"
                      onClick={() => resetSelection(preview, profileIndex)}
                      className="rounded-lg border border-border px-3 py-1.5 text-xs font-medium text-muted hover:bg-surface-secondary"
                    >
                      {t("restoreOriginal")}
                    </button>
                    <button
                      type="button"
                      onClick={() => setSelectedKeys(new Set())}
                      className="rounded-lg border border-border px-3 py-1.5 text-xs font-medium text-muted hover:bg-surface-secondary"
                    >
                      {t("clearSelection")}
                    </button>
                  </div>
                </div>

                <div className="mt-3 max-h-[48vh] divide-y divide-border overflow-y-auto overscroll-contain pr-1 lg:max-h-[59vh]">
                  {profile?.items.map((item) => {
                    const prompt = item.prompt;
                    const selectable = Boolean(
                      prompt && !prompt.marker && prompt.content.trim(),
                    );
                    const key = presetSelectionKey(
                      profile.profile_index,
                      item.order_index,
                    );
                    return (
                      <label
                        key={key}
                        className={`flex gap-3 px-1 py-3 ${
                          selectable
                            ? "cursor-pointer"
                            : "cursor-not-allowed opacity-60"
                        }`}
                      >
                        <input
                          type="checkbox"
                          className="mt-1 shrink-0"
                          disabled={!selectable}
                          checked={selectable && selectedKeys.has(key)}
                          onChange={() => toggleItem(item.order_index)}
                        />
                        <span className="min-w-0 flex-1">
                          <span className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                            <span className="break-words text-sm font-medium text-foreground">
                              {prompt?.name || item.identifier}
                            </span>
                            <span className="text-[11px] text-muted">
                              {prompt?.marker
                                ? t("marker")
                                : !item.resolved
                                  ? t("missingReference")
                                  : !prompt?.content.trim()
                                    ? t("emptyContent")
                                    : item.enabled
                                      ? t("originallyEnabled")
                                      : t("originallyDisabled")}
                            </span>
                            {prompt && (
                              <span className="text-[11px] text-muted">
                                {t("sourceRole", {
                                  role: prompt.source_role,
                                })}
                              </span>
                            )}
                          </span>
                          {prompt?.content.trim() && !prompt.marker && (
                            <span className="mt-1 line-clamp-2 block break-words text-xs leading-5 text-muted">
                              {prompt.content.replace(/\s+/gu, " ").trim()}
                            </span>
                          )}
                        </span>
                      </label>
                    );
                  })}
                </div>
              </section>
            </div>
          )}
        </div>

        {preview && (
          <footer className="shrink-0 border-t border-border bg-surface px-5 py-4 sm:px-7">
            {error && (
              <p role="alert" className="mb-3 text-sm text-red-700 dark:text-red-300">
                {error}
              </p>
            )}
            <div className="flex flex-wrap items-center justify-between gap-3">
              <p
                className={`text-xs leading-5 ${
                  overLimit
                    ? "font-medium text-red-700 dark:text-red-300"
                    : "text-muted"
                }`}
              >
                {overLimit
                  ? t("instructionOverLimit", {
                      current: instructionChars,
                      maximum: maxInstructionChars,
                    })
                  : t("selectionSummary", {
                      selected: selectedCount,
                      current: instructionChars,
                      maximum: maxInstructionChars,
                    })}
              </p>
              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  onClick={() => {
                    setPreview(null);
                    setSelectedKeys(new Set());
                    setError("");
                  }}
                  className="rounded-lg border border-border px-4 py-2 text-sm font-medium text-muted hover:bg-surface-secondary"
                >
                  {t("chooseAnother")}
                </button>
                <button
                  type="button"
                  onClick={useDraft}
                  disabled={!instruction || overLimit || !customizable.length}
                  className="rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {t("useDraft")}
                </button>
              </div>
            </div>
          </footer>
        )}
      </section>
    </div>
  );
}
