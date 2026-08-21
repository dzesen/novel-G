"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";

import { useAuth } from "@/components/auth/AuthProvider";
import GenerationPresetImportDialog, {
  type ImportedGenerationPresetDraft,
} from "@/components/writing/agents/GenerationPresetImportDialog";
import { apiDelete, apiGet, apiPost, apiPut } from "@/lib/api";
import {
  MAX_CUSTOM_AGENT_INSTRUCTION_CHARS,
  MAX_CUSTOM_AGENT_OUTPUT_TOKENS,
} from "@/lib/generationPreset";
import type {
  AgentCapability,
  AgentCapabilityId,
  AgentProfile,
  AgentProviderOption,
} from "@/types/agent";

interface GenerationRoleDraft {
  label: string;
  description: string;
  capability: AgentCapabilityId;
  instruction: string;
  providerAlias: string;
  temperature: string;
  topP: string;
  maxTokens: string;
  presencePenalty: string;
  frequencyPenalty: string;
  visibility: "private" | "shared";
  enabled: boolean;
}

const fieldClass =
  "w-full rounded-lg border border-border bg-surface px-3 py-2 text-sm text-foreground outline-none transition-colors placeholder:text-muted focus:border-accent";

function emptyDraft(): GenerationRoleDraft {
  return {
    label: "",
    description: "",
    capability: "creative_inspiration",
    instruction: "",
    providerAlias: "",
    temperature: "",
    topP: "",
    maxTokens: "",
    presencePenalty: "",
    frequencyPenalty: "",
    visibility: "private",
    enabled: true,
  };
}

function profileToDraft(profile: AgentProfile): GenerationRoleDraft {
  return {
    label: profile.label,
    description: profile.description,
    capability: profile.capabilities[0],
    instruction: profile.instruction,
    providerAlias: profile.provider_alias ?? "",
    temperature:
      profile.generation_params.temperature == null
        ? ""
        : String(profile.generation_params.temperature),
    topP:
      profile.generation_params.top_p == null
        ? ""
        : String(profile.generation_params.top_p),
    maxTokens:
      profile.generation_params.max_tokens == null
        ? ""
        : String(profile.generation_params.max_tokens),
    presencePenalty:
      profile.generation_params.presence_penalty == null
        ? ""
        : String(profile.generation_params.presence_penalty),
    frequencyPenalty:
      profile.generation_params.frequency_penalty == null
        ? ""
        : String(profile.generation_params.frequency_penalty),
    visibility: profile.visibility,
    enabled: profile.enabled,
  };
}

function draftPayload(draft: GenerationRoleDraft) {
  return {
    label: draft.label.trim(),
    description: draft.description.trim(),
    capability: draft.capability,
    instruction: draft.instruction.trim(),
    provider_alias: draft.providerAlias || null,
    generation_params: {
      temperature:
        draft.temperature === "" ? null : Number(draft.temperature),
      top_p: draft.topP === "" ? null : Number(draft.topP),
      max_tokens: draft.maxTokens === "" ? null : Number(draft.maxTokens),
      presence_penalty:
        draft.presencePenalty === ""
          ? null
          : Number(draft.presencePenalty),
      frequency_penalty:
        draft.frequencyPenalty === ""
          ? null
          : Number(draft.frequencyPenalty),
    },
    visibility: draft.visibility,
    enabled: draft.enabled,
  };
}

export default function GenerationRoleSettings() {
  const t = useTranslations("writing.agentStudio");
  const settingsT = useTranslations("settings.generationRoles");
  const { user } = useAuth();
  const [roles, setRoles] = useState<AgentProfile[]>([]);
  const [capabilities, setCapabilities] = useState<AgentCapability[]>([]);
  const [providers, setProviders] = useState<AgentProviderOption[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [filter, setFilter] = useState<AgentCapabilityId | "all">("all");
  const [selectedRoleId, setSelectedRoleId] = useState<string | null>(null);
  const [creatingRole, setCreatingRole] = useState(false);
  const [draft, setDraft] = useState<GenerationRoleDraft>(emptyDraft);
  const [saving, setSaving] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [presetImportOpen, setPresetImportOpen] = useState(false);

  const loadCatalog = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [roleResponse, capabilityResponse, providerResponse] =
        await Promise.all([
          apiGet<{ data: AgentProfile[] }>("/api/agents?include_disabled=true"),
          apiGet<{ data: AgentCapability[] }>("/api/agents/capabilities"),
          apiGet<{ data: AgentProviderOption[] }>("/api/agents/providers"),
        ]);
      setRoles(roleResponse.data);
      setCapabilities(capabilityResponse.data);
      setProviders(providerResponse.data);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : t("loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    void loadCatalog();
  }, [loadCatalog]);

  const capabilityMap = useMemo(
    () =>
      new Map(
        capabilities.map((capability) => [
          capability.capability,
          capability,
        ]),
      ),
    [capabilities],
  );
  const selectedRole = roles.find((role) => role.agent_id === selectedRoleId);
  const selectedCapability = selectedRole
    ? capabilityMap.get(selectedRole.capabilities[0])
    : capabilityMap.get(draft.capability);
  const filteredRoles = roles.filter(
    (role) => filter === "all" || role.capabilities.includes(filter),
  );
  const formLocked = !creatingRole && !selectedRole?.editable;

  const selectRole = (role: AgentProfile) => {
    setCreatingRole(false);
    setSelectedRoleId(role.agent_id);
    setDraft(profileToDraft(role));
    setConfirmingDelete(false);
    setNotice(null);
  };

  const startCreate = () => {
    setCreatingRole(true);
    setSelectedRoleId(null);
    setDraft(emptyDraft());
    setConfirmingDelete(false);
    setNotice(null);
  };

  const useImportedPresetDraft = (imported: ImportedGenerationPresetDraft) => {
    setCreatingRole(true);
    setSelectedRoleId(null);
    setDraft({ ...emptyDraft(), ...imported });
    setConfirmingDelete(false);
    setPresetImportOpen(false);
    setError(null);
    setNotice(t("management.presetImport.draftReady"));
  };

  const reloadAndSelect = async (roleId: string) => {
    const response = await apiGet<{ data: AgentProfile[] }>(
      "/api/agents?include_disabled=true",
    );
    setRoles(response.data);
    const role = response.data.find((item) => item.agent_id === roleId);
    if (role) selectRole(role);
  };

  const saveRole = async () => {
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const payload = draftPayload(draft);
      let response: { agent: AgentProfile };
      if (creatingRole) {
        response = await apiPost<{ agent: AgentProfile }>("/api/agents", payload);
      } else if (selectedRole?.editable) {
        response = await apiPut<{ agent: AgentProfile }>(
          `/api/agents/${selectedRole.agent_id}`,
          { ...payload, expected_version: selectedRole.version },
        );
      } else {
        return;
      }
      await reloadAndSelect(response.agent.agent_id);
      setNotice(t("management.saved"));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : t("saveFailed"));
    } finally {
      setSaving(false);
    }
  };

  const cloneRole = async (role: AgentProfile) => {
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const response = await apiPost<{ agent: AgentProfile }>(
        `/api/agents/${role.agent_id}/clone`,
        {},
      );
      await reloadAndSelect(response.agent.agent_id);
      setNotice(t("management.cloned"));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : t("saveFailed"));
    } finally {
      setSaving(false);
    }
  };

  const deleteRole = async () => {
    if (!selectedRole?.editable) return;
    setSaving(true);
    setError(null);
    try {
      await apiDelete(`/api/agents/${selectedRole.agent_id}`);
      setSelectedRoleId(null);
      setCreatingRole(false);
      setConfirmingDelete(false);
      setNotice(t("management.deleted"));
      await loadCatalog();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : t("saveFailed"));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="min-w-0 space-y-5">
      <header className="border-b border-border pb-4">
        <h2 className="text-xl font-semibold text-foreground">
          {settingsT("title")}
        </h2>
        <p className="mt-1 max-w-3xl text-sm leading-6 text-muted">
          {settingsT("description")}
        </p>
      </header>

      {error && (
        <div role="alert" className="rounded-lg border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-800 dark:border-red-900 dark:bg-red-950 dark:text-red-200">
          {error}
        </div>
      )}
      {notice && (
        <div role="status" className="rounded-lg border border-green-300 bg-green-50 px-4 py-3 text-sm text-green-800 dark:border-green-900 dark:bg-green-950 dark:text-green-200">
          {notice}
        </div>
      )}

      {loading ? (
        <div className="rounded-lg border border-border bg-surface px-6 py-16 text-center text-sm text-muted">
          {t("loading")}
        </div>
      ) : (
        <div className="grid min-h-0 gap-5 lg:grid-cols-[minmax(16rem,0.7fr)_minmax(0,1.3fr)]">
          <section className="rounded-lg border border-border bg-surface p-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <h3 className="font-semibold text-foreground">
                  {t("management.catalogTitle")}
                </h3>
                <p className="mt-1 text-xs text-muted">
                  {t("management.catalogHint")}
                </p>
              </div>
              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  onClick={() => setPresetImportOpen(true)}
                  className="rounded-lg border border-accent px-3 py-2 text-sm font-semibold text-accent hover:bg-accent/5"
                >
                  {t("management.presetImport.openButton")}
                </button>
                <button
                  type="button"
                  onClick={startCreate}
                  className="rounded-lg bg-accent px-3 py-2 text-sm font-semibold text-white"
                >
                  {t("management.new")}
                </button>
              </div>
            </div>
            <select
              aria-label={t("management.capabilityFilter")}
              className={`${fieldClass} mt-4`}
              value={filter}
              onChange={(event) =>
                setFilter(event.target.value as AgentCapabilityId | "all")
              }
            >
              <option value="all">{t("management.allCapabilities")}</option>
              {capabilities.map((capability) => (
                <option key={capability.capability} value={capability.capability}>
                  {capability.label}
                </option>
              ))}
            </select>
            <div className="mt-4 max-h-[58vh] space-y-1 overflow-y-auto pr-1">
              {filteredRoles.map((role) => (
                <button
                  key={role.agent_id}
                  type="button"
                  onClick={() => selectRole(role)}
                  className={`w-full rounded-lg border px-3 py-3 text-left transition-colors ${
                    role.agent_id === selectedRoleId
                      ? "border-accent bg-accent/5"
                      : "border-transparent hover:border-border hover:bg-surface-secondary"
                  }`}
                >
                  <div className="flex items-start justify-between gap-2">
                    <span className="min-w-0 break-words text-sm font-medium text-foreground">
                      {role.label}
                    </span>
                    {!role.enabled && (
                      <span className="shrink-0 rounded bg-surface-secondary px-1.5 py-0.5 text-[11px] text-muted">
                        {t("management.disabled")}
                      </span>
                    )}
                  </div>
                  <div className="mt-1 flex flex-wrap gap-2 text-xs text-muted">
                    <span>
                      {capabilityMap.get(role.capabilities[0])?.label ??
                        role.capabilities[0]}
                    </span>
                    <span>·</span>
                    <span>
                      {role.origin === "builtin"
                        ? t("management.builtin")
                        : role.editable
                          ? t("management.mine")
                          : t("management.shared")}
                    </span>
                  </div>
                </button>
              ))}
            </div>
          </section>

          <section className="min-w-0 rounded-lg border border-border bg-surface p-4 sm:p-5">
            {!creatingRole && !selectedRole ? (
              <div className="flex min-h-72 items-center justify-center text-center">
                <div>
                  <h3 className="font-semibold text-foreground">
                    {t("management.selectTitle")}
                  </h3>
                  <p className="mt-2 text-sm text-muted">
                    {t("management.selectDescription")}
                  </p>
                </div>
              </div>
            ) : (
              <div className="space-y-5">
                <div className="flex flex-wrap items-start justify-between gap-3 border-b border-border pb-4">
                  <div className="min-w-0">
                    <p className="text-xs font-semibold uppercase tracking-[0.14em] text-accent">
                      {creatingRole
                        ? t("management.new")
                        : selectedRole?.origin === "builtin"
                          ? t("management.builtin")
                          : t("management.custom")}
                    </p>
                    <h3 className="mt-1 break-words text-xl font-semibold text-foreground">
                      {creatingRole
                        ? t("management.createTitle")
                        : selectedRole?.label}
                    </h3>
                    {selectedCapability && (
                      <p className="mt-1 text-sm text-muted">
                        {selectedCapability.description}
                      </p>
                    )}
                  </div>
                  {selectedRole &&
                    selectedCapability?.customizable &&
                    !selectedRole.editable && (
                      <button
                        type="button"
                        disabled={saving}
                        onClick={() => void cloneRole(selectedRole)}
                        className="rounded-lg border border-accent px-3 py-2 text-sm font-medium text-accent disabled:opacity-50"
                      >
                        {t("management.clone")}
                      </button>
                    )}
                </div>

                {formLocked && (
                  <div className="rounded-lg bg-surface-secondary px-4 py-3 text-sm leading-6 text-muted">
                    {selectedCapability?.customizable
                      ? t("management.readonlyCloneHint")
                      : t("management.pipelineLockedHint")}
                  </div>
                )}

                <div className="grid gap-4 md:grid-cols-2">
                  <label className="space-y-1.5 text-sm">
                    <span className="text-muted">{t("management.name")}</span>
                    <input
                      className={fieldClass}
                      disabled={formLocked}
                      value={draft.label}
                      onChange={(event) =>
                        setDraft({ ...draft, label: event.target.value })
                      }
                    />
                  </label>
                  <label className="space-y-1.5 text-sm">
                    <span className="text-muted">
                      {t("management.capability")}
                    </span>
                    <select
                      className={fieldClass}
                      disabled={formLocked || !creatingRole}
                      value={draft.capability}
                      onChange={(event) =>
                        setDraft({
                          ...draft,
                          capability: event.target.value as AgentCapabilityId,
                        })
                      }
                    >
                      {(creatingRole
                        ? capabilities.filter((item) => item.customizable)
                        : capabilities
                      ).map((capability) => (
                        <option key={capability.capability} value={capability.capability}>
                          {capability.label}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="space-y-1.5 text-sm md:col-span-2">
                    <span className="text-muted">
                      {t("management.description")}
                    </span>
                    <input
                      className={fieldClass}
                      disabled={formLocked}
                      value={draft.description}
                      onChange={(event) =>
                        setDraft({ ...draft, description: event.target.value })
                      }
                    />
                  </label>
                  <label className="space-y-1.5 text-sm md:col-span-2">
                    <span className="text-muted">
                      {t("management.instruction")}
                    </span>
                    <textarea
                      className={`${fieldClass} min-h-40 resize-y font-mono text-[13px] leading-6`}
                      disabled={formLocked}
                      value={draft.instruction}
                      onChange={(event) =>
                        setDraft({ ...draft, instruction: event.target.value })
                      }
                      placeholder={t("management.instructionPlaceholder")}
                    />
                    {!formLocked && (
                      <span className="block text-xs text-muted">
                        {t("management.instructionHint", {
                          current: Array.from(draft.instruction).length,
                          maximum: MAX_CUSTOM_AGENT_INSTRUCTION_CHARS,
                        })}
                      </span>
                    )}
                  </label>
                </div>

                <div className="border-t border-border pt-5">
                  <h4 className="text-sm font-semibold text-foreground">
                    {t("management.runtimeTitle")}
                  </h4>
                  <p className="mt-1 text-xs text-muted">
                    {t("management.runtimeHint")}
                  </p>
                  <div className="mt-4 grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                    <label className="space-y-1.5 text-sm">
                      <span className="text-muted">
                        {t("management.provider")}
                      </span>
                      <select
                        className={fieldClass}
                        disabled={formLocked}
                        value={draft.providerAlias}
                        onChange={(event) =>
                          setDraft({ ...draft, providerAlias: event.target.value })
                        }
                      >
                        <option value="">{t("management.inheritProvider")}</option>
                        {providers.map((provider) => (
                          <option key={provider.alias} value={provider.alias}>
                            {provider.alias} · {provider.model || provider.type}
                          </option>
                        ))}
                      </select>
                    </label>
                    <NumberField
                      label="temperature"
                      value={draft.temperature}
                      disabled={formLocked}
                      min={0}
                      max={2}
                      step={0.1}
                      onChange={(temperature) => setDraft({ ...draft, temperature })}
                    />
                    <NumberField
                      label="top_p"
                      value={draft.topP}
                      disabled={formLocked}
                      min={0}
                      max={1}
                      step={0.1}
                      onChange={(topP) => setDraft({ ...draft, topP })}
                    />
                    <NumberField
                      label="max_tokens"
                      value={draft.maxTokens}
                      disabled={formLocked}
                      min={1}
                      max={MAX_CUSTOM_AGENT_OUTPUT_TOKENS}
                      step={100}
                      onChange={(maxTokens) => setDraft({ ...draft, maxTokens })}
                    />
                    <NumberField
                      label={t("management.presencePenalty")}
                      value={draft.presencePenalty}
                      disabled={formLocked}
                      min={-2}
                      max={2}
                      step={0.1}
                      onChange={(presencePenalty) =>
                        setDraft({ ...draft, presencePenalty })
                      }
                    />
                    <NumberField
                      label={t("management.frequencyPenalty")}
                      value={draft.frequencyPenalty}
                      disabled={formLocked}
                      min={-2}
                      max={2}
                      step={0.1}
                      onChange={(frequencyPenalty) =>
                        setDraft({ ...draft, frequencyPenalty })
                      }
                    />
                  </div>
                </div>

                {!formLocked && (
                  <div className="flex flex-wrap items-center justify-between gap-4 border-t border-border pt-5">
                    <div className="flex flex-wrap items-center gap-4">
                      <label className="flex items-center gap-2 text-sm text-muted">
                        <input
                          type="checkbox"
                          checked={draft.enabled}
                          onChange={(event) =>
                            setDraft({ ...draft, enabled: event.target.checked })
                          }
                        />
                        {t("management.enabled")}
                      </label>
                      <label className="flex items-center gap-2 text-sm text-muted">
                        <span>{t("management.visibility")}</span>
                        <select
                          className="rounded-lg border border-border bg-surface px-2 py-1.5 text-sm text-foreground"
                          value={draft.visibility}
                          onChange={(event) =>
                            setDraft({
                              ...draft,
                              visibility: event.target.value as "private" | "shared",
                            })
                          }
                        >
                          <option value="private">{t("management.private")}</option>
                          {user?.role === "admin" && (
                            <option value="shared">{t("management.shared")}</option>
                          )}
                        </select>
                      </label>
                    </div>
                    <div className="flex flex-wrap items-center gap-2">
                      {selectedRole?.editable &&
                        (confirmingDelete ? (
                          <>
                            <span className="text-sm text-muted">
                              {t("management.confirmDelete")}
                            </span>
                            <button
                              type="button"
                              disabled={saving}
                              onClick={() => void deleteRole()}
                              className="rounded-lg border border-red-300 px-3 py-2 text-sm font-medium text-red-600 disabled:opacity-50"
                            >
                              {t("management.delete")}
                            </button>
                            <button
                              type="button"
                              onClick={() => setConfirmingDelete(false)}
                              className="rounded-lg border border-border px-3 py-2 text-sm text-muted"
                            >
                              {t("management.cancel")}
                            </button>
                          </>
                        ) : (
                          <button
                            type="button"
                            onClick={() => setConfirmingDelete(true)}
                            className="rounded-lg px-3 py-2 text-sm text-red-600"
                          >
                            {t("management.delete")}
                          </button>
                        ))}
                      <button
                        type="button"
                        disabled={
                          saving ||
                          draft.label.trim().length < 2 ||
                          draft.instruction.trim().length < 20 ||
                          Array.from(draft.instruction.trim()).length >
                            MAX_CUSTOM_AGENT_INSTRUCTION_CHARS
                        }
                        onClick={() => void saveRole()}
                        className="rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        {saving
                          ? t("management.saving")
                          : t("management.save")}
                      </button>
                    </div>
                  </div>
                )}
              </div>
            )}
          </section>
        </div>
      )}

      {presetImportOpen && (
        <GenerationPresetImportDialog
          capabilities={capabilities}
          defaultCapability={
            filter !== "all" && capabilityMap.get(filter)?.customizable
              ? filter
              : "creative_inspiration"
          }
          onClose={() => setPresetImportOpen(false)}
          onUseDraft={useImportedPresetDraft}
        />
      )}
    </div>
  );
}

function NumberField({
  label,
  value,
  disabled,
  min,
  max,
  step,
  onChange,
}: {
  label: string;
  value: string;
  disabled: boolean;
  min: number;
  max: number;
  step: number;
  onChange: (value: string) => void;
}) {
  return (
    <label className="space-y-1.5 text-sm">
      <span className="text-muted">{label}</span>
      <input
        type="number"
        min={min}
        max={max}
        step={step}
        className={fieldClass}
        disabled={disabled}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  );
}
