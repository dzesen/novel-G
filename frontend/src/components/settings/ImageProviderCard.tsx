"use client";

import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { useTranslations } from "next-intl";
import {
  Button,
  Card,
  Chip,
  Input,
  Label,
  ListBox,
  ListBoxItem,
  NumberField,
  Select,
  Switch,
  TextField,
} from "@heroui/react";
import { apiPost } from "@/lib/api";
import { ComfyUIWorkflowOverrides } from "./ComfyUIWorkflowOverrides";
import type {
  AppConfig,
  ComfyUIImageProviderConfig,
  ComfyUIWorkflowInputBinding,
  ImageConfigScalar,
  ImageFieldParameterMapping,
  ImageParameterMapping,
  ImageProviderConfig,
  ImageProviderTestResponse,
  ImageValueMapParameterMapping,
  OpenAICompatibleImageProviderConfig,
} from "@/types/config";
import {
  getReplacementDefaultImageProviderAlias,
  newComfyUIImageProviderConfig,
  newOpenAICompatibleImageProviderConfig,
  removeImageProviderAlias,
  renameImageProviderAlias,
} from "@/types/config";

interface Props {
  config: AppConfig;
  onChange: (config: AppConfig) => void;
  onProviderRename: (from: string, to: string) => void;
  onProviderDelete: (alias: string, replacementDefaultAlias?: string) => void;
  hasUnsavedChanges: boolean;
}

type ProviderTestRunState = {
  status: "idle" | "running" | "done" | "error";
  response?: ImageProviderTestResponse;
  error?: string;
  revisionBackfilled?: boolean;
  stale?: boolean;
};

const ALIAS_REGEX = /^[a-zA-Z0-9_]+$/;
const KNOWN_SEMANTIC_SLOTS = [
  "positive_prompt",
  "negative_prompt",
  "seed",
  "width",
  "height",
  "reference_image",
  "batch_size",
] as const;
const USAGE_KEYS = [
  "character_portrait",
  "cover",
  "scene_illustration",
] as const;

const LOCALIZED_TEST_FAILURE_CODES = new Set([
  "provider_unavailable",
  "workflow_validation_failed",
  "dependency_missing",
  "execution_failed",
]);

function imageProviderIsSelectable(provider: ImageProviderConfig | undefined): boolean {
  return Boolean(provider?.enabled && provider.type === "comfyui");
}

function uniqueName(prefix: string, existing: Record<string, unknown>): string {
  let index = 1;
  while (`${prefix}_${index}` in existing) index += 1;
  return `${prefix}_${index}`;
}

function parseOptionalNumber(value: string): number | null {
  if (!value.trim()) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function parseScalar(value: string): ImageConfigScalar {
  const trimmed = value.trim();
  if (!trimmed) return "";
  try {
    const parsed: unknown = JSON.parse(trimmed);
    if (
      typeof parsed === "string"
      || typeof parsed === "number"
      || typeof parsed === "boolean"
    ) {
      return parsed;
    }
  } catch {
    return value;
  }
  return value;
}

export function ImageProviderCard({
  config,
  onChange,
  onProviderRename,
  onProviderDelete,
  hasUnsavedChanges,
}: Props) {
  const t = useTranslations("settings.imageProvider");
  const providers = config.image_providers.providers;
  const aliases = Object.keys(providers);
  const [selectedAlias, setSelectedAlias] = useState("");
  const [showAddForm, setShowAddForm] = useState(false);
  const [newAlias, setNewAlias] = useState("");
  const [newType, setNewType] = useState<ImageProviderConfig["type"]>("comfyui");
  const [aliasError, setAliasError] = useState("");
  const [renamingAlias, setRenamingAlias] = useState("");
  const [renameValue, setRenameValue] = useState("");
  const [renameError, setRenameError] = useState("");
  const [pendingDeleteAlias, setPendingDeleteAlias] = useState("");
  const [testStates, setTestStates] = useState<Record<string, ProviderTestRunState>>({});

  useEffect(() => {
    if (selectedAlias && providers[selectedAlias]) return;
    setSelectedAlias(aliases[0] || "");
  }, [aliases, providers, selectedAlias]);

  const selectedProvider = selectedAlias ? providers[selectedAlias] : undefined;
  const selectedTest = selectedAlias ? testStates[selectedAlias] : undefined;
  const selectableAliases = aliases.filter((alias) => imageProviderIsSelectable(providers[alias]));
  const affectedUsages = (alias: string) => USAGE_KEYS.filter((usage) => (
    config.image_providers.usages[usage] || config.image_providers.default_provider
  ) === alias);

  const cloneProviderForUsage = (
    alias: string,
    usage: typeof USAGE_KEYS[number],
  ) => {
    const provider = providers[alias];
    if (!provider) return;
    const prefix = `${alias}_${usage}`;
    const cloneAlias = prefix in providers ? uniqueName(prefix, providers) : prefix;
    const clone = JSON.parse(JSON.stringify(provider)) as ImageProviderConfig;
    updateImageConfig({
      ...config.image_providers,
      providers: { ...providers, [cloneAlias]: clone },
      usages: { ...config.image_providers.usages, [usage]: cloneAlias },
    });
    setSelectedAlias(cloneAlias);
  };

  const updateImageConfig = (next: AppConfig["image_providers"]) => {
    onChange({ ...config, image_providers: next });
  };

  const updateProvider = (
    alias: string,
    provider: ImageProviderConfig,
    markTestStale = true,
  ) => {
    updateImageConfig({
      ...config.image_providers,
      providers: { ...providers, [alias]: provider },
    });
    if (markTestStale) {
      setTestStates((current) => current[alias]
        ? { ...current, [alias]: { ...current[alias], stale: true } }
        : current);
    }
  };

  const addProvider = () => {
    const alias = newAlias.trim();
    if (!alias || !ALIAS_REGEX.test(alias)) {
      setAliasError(t("aliasRule"));
      return;
    }
    if (alias in providers) {
      setAliasError(t("aliasDuplicate"));
      return;
    }
    const provider = newType === "comfyui"
      ? newComfyUIImageProviderConfig()
      : newOpenAICompatibleImageProviderConfig();
    updateImageConfig({
      ...config.image_providers,
      providers: { ...providers, [alias]: provider },
    });
    setSelectedAlias(alias);
    setNewAlias("");
    setAliasError("");
    setShowAddForm(false);
  };

  const confirmRename = (alias: string) => {
    const nextAlias = renameValue.trim();
    if (!nextAlias || !ALIAS_REGEX.test(nextAlias)) {
      setRenameError(t("aliasRule"));
      return;
    }
    if (nextAlias === alias) {
      setRenamingAlias("");
      return;
    }
    if (nextAlias in providers) {
      setRenameError(t("aliasDuplicate"));
      return;
    }
    onChange(renameImageProviderAlias(config, alias, nextAlias));
    onProviderRename(alias, nextAlias);
    setSelectedAlias(nextAlias);
    setTestStates((current) => {
      const next = { ...current };
      if (next[alias]) {
        next[nextAlias] = next[alias];
        delete next[alias];
      }
      return next;
    });
    setRenamingAlias("");
    setRenameValue("");
    setRenameError("");
  };

  const deleteProvider = (alias: string) => {
    const replacement = config.image_providers.default_provider === alias
      ? getReplacementDefaultImageProviderAlias(providers, alias)
      : "";
    if (config.image_providers.default_provider === alias && !replacement) {
      window.alert(t("deleteDefaultRequiresReplacement"));
      return;
    }
    onChange(removeImageProviderAlias(config, alias, replacement));
    onProviderDelete(alias, replacement || undefined);
    setPendingDeleteAlias("");
    setSelectedAlias(aliases.find((item) => item !== alias) || "");
    setTestStates((current) => {
      const next = { ...current };
      delete next[alias];
      return next;
    });
  };

  const runConnectionTest = async (alias: string) => {
    const provider = providers[alias];
    if (!provider || provider.type !== "comfyui") return;
    setPendingDeleteAlias("");
    setTestStates((current) => ({
      ...current,
      [alias]: { status: "running" },
    }));
    try {
      const response = await apiPost<ImageProviderTestResponse>(
        "/api/config/image-providers/test",
        { alias, provider },
      );
      const revisionBackfilled = Boolean(
        response.template_revision
        && response.template_revision !== provider.workflow.template_revision,
      );
      if (revisionBackfilled) {
        updateProvider(alias, {
          ...provider,
          workflow: {
            ...provider.workflow,
            template_revision: response.template_revision,
          },
        }, false);
      }
      setTestStates((current) => ({
        ...current,
        [alias]: {
          status: "done",
          response,
          revisionBackfilled,
          stale: false,
        },
      }));
    } catch (error) {
      setTestStates((current) => ({
        ...current,
        [alias]: {
          status: "error",
          error: error instanceof Error ? error.message : t("test.unknownError"),
        },
      }));
    }
  };

  const setDefaultProvider = (alias: string) => {
    updateImageConfig({ ...config.image_providers, default_provider: alias });
  };

  const setUsage = (
    usage: typeof USAGE_KEYS[number],
    alias: string,
  ) => {
    updateImageConfig({
      ...config.image_providers,
      usages: { ...config.image_providers.usages, [usage]: alias },
    });
  };

  return (
    <Card className="border border-border bg-surface shadow-sm">
      <Card.Header className="flex-col items-start gap-3 border-b border-border/70 pb-3">
        <div className="w-full">
          <Card.Title className="text-lg font-semibold text-foreground">
            {t("title")}
          </Card.Title>
          <p className="mt-1 max-w-3xl text-sm text-muted">{t("description")}</p>
        </div>
        <ProviderAssignments
          config={config}
          selectableAliases={selectableAliases}
          onDefaultChange={setDefaultProvider}
          onUsageChange={setUsage}
          t={t}
        />
      </Card.Header>

      <Card.Content className="p-3 pt-4">
        <div className="grid min-h-[680px] grid-cols-1 gap-3 lg:grid-cols-[280px_minmax(0,1fr)]">
          <aside className="flex min-h-[320px] flex-col rounded-lg bg-surface-secondary/40 p-3">
            <div className="flex items-center justify-between gap-3">
              <div className="min-w-0">
                <h3 className="truncate text-sm font-semibold text-foreground">
                  {t("listTitle")}
                </h3>
                <p className="text-xs text-muted">{t("listCount", { count: aliases.length })}</p>
              </div>
              <Chip size="sm" variant="soft" className="shrink-0 bg-accent/10 text-accent">
                {aliases.length}
              </Chip>
            </div>

            <div className="mt-3 flex-1 space-y-2 overflow-y-auto pr-1">
              {aliases.map((alias) => {
                const provider = providers[alias];
                const testState = testStates[alias];
                return (
                  <button
                    key={alias}
                    type="button"
                    onClick={() => setSelectedAlias(alias)}
                    className={`w-full rounded-lg border p-3 text-left transition-colors ${
                      selectedAlias === alias
                        ? "border-accent bg-surface"
                        : "border-border bg-surface/80 hover:border-warm-400 hover:bg-surface"
                    }`}
                  >
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <div className="truncate text-sm font-semibold text-foreground">
                          {alias}
                        </div>
                        <div className="mt-1 truncate text-xs text-muted">
                          {t(`types.${provider.type}`)}
                        </div>
                      </div>
                      <div className="flex shrink-0 flex-wrap justify-end gap-1">
                        <StatusChip tone={provider.enabled ? "success" : "muted"}>
                          {provider.enabled ? t("enabled") : t("disabled")}
                        </StatusChip>
                        {provider.type === "openai_compatible" && (
                          <StatusChip tone="warning">{t("unavailableBadge")}</StatusChip>
                        )}
                        {testState?.status === "done" && (
                          <StatusChip tone={testState.response?.status === "passed" ? "success" : "danger"}>
                            {testState.response?.status === "passed"
                              ? t("test.passed")
                              : t("test.failed")}
                          </StatusChip>
                        )}
                      </div>
                    </div>
                  </button>
                );
              })}
              {aliases.length === 0 && (
                <div className="rounded-lg border border-dashed border-border bg-surface/70 p-4 text-sm text-muted">
                  {t("empty")}
                </div>
              )}
            </div>

            <div className="mt-3 border-t border-border pt-3">
              {showAddForm ? (
                <div className="space-y-3">
                  <TextField
                    value={newAlias}
                    onChange={(value) => {
                      setNewAlias(value);
                      setAliasError("");
                    }}
                    isInvalid={Boolean(aliasError)}
                  >
                    <Label className="text-sm text-muted">{t("alias")}</Label>
                    <Input placeholder={t("aliasPlaceholder")} className="border-border" />
                  </TextField>
                  <label className="block text-sm text-muted">
                    <span>{t("protocol")}</span>
                    <select
                      value={newType}
                      onChange={(event) => setNewType(event.target.value as ImageProviderConfig["type"])}
                      className="mt-1 w-full rounded-lg border border-border bg-surface px-3 py-2 text-sm text-foreground"
                    >
                      <option value="comfyui">{t("types.comfyui")}</option>
                      <option value="openai_compatible">{t("types.openai_compatible")}</option>
                    </select>
                  </label>
                  {aliasError && <p className="text-xs text-red-600">{aliasError}</p>}
                  <div className="grid grid-cols-2 gap-2">
                    <Button
                      size="sm"
                      onPress={addProvider}
                      className="bg-accent text-white hover:bg-accent-hover"
                    >
                      {t("add")}
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      onPress={() => {
                        setShowAddForm(false);
                        setNewAlias("");
                        setAliasError("");
                      }}
                      className="border-border text-foreground"
                    >
                      {t("cancel")}
                    </Button>
                  </div>
                </div>
              ) : (
                <Button
                  variant="outline"
                  onPress={() => setShowAddForm(true)}
                  className="w-full border-border text-foreground"
                >
                  + {t("add")}
                </Button>
              )}
            </div>
          </aside>

          <div className="min-w-0 rounded-lg border border-border bg-surface p-3">
            {selectedProvider ? (
              <ProviderDetail
                alias={selectedAlias}
                provider={selectedProvider}
                isRenaming={renamingAlias === selectedAlias}
                renameValue={renameValue}
                renameError={renameError}
                pendingDelete={pendingDeleteAlias === selectedAlias}
                hasUnsavedChanges={hasUnsavedChanges}
                affectedUsages={affectedUsages(selectedAlias)}
                testState={selectedTest}
                onChange={(provider) => updateProvider(selectedAlias, provider)}
                onRenameStart={() => {
                  setRenamingAlias(selectedAlias);
                  setRenameValue(selectedAlias);
                  setRenameError("");
                  setPendingDeleteAlias("");
                }}
                onRenameValueChange={(value) => {
                  setRenameValue(value);
                  setRenameError("");
                }}
                onRenameConfirm={() => confirmRename(selectedAlias)}
                onRenameCancel={() => {
                  setRenamingAlias("");
                  setRenameValue("");
                  setRenameError("");
                }}
                onTest={() => runConnectionTest(selectedAlias)}
                onCloneForUsage={(usage) => cloneProviderForUsage(selectedAlias, usage)}
                onDeleteAsk={() => {
                  setPendingDeleteAlias(selectedAlias);
                  setRenamingAlias("");
                }}
                onDeleteConfirm={() => deleteProvider(selectedAlias)}
                onDeleteCancel={() => setPendingDeleteAlias("")}
                t={t}
              />
            ) : (
              <div className="flex min-h-[360px] items-center justify-center rounded-lg border border-dashed border-border bg-surface-secondary/40 text-sm text-muted">
                {t("empty")}
              </div>
            )}
          </div>
        </div>
      </Card.Content>
    </Card>
  );
}

function ProviderAssignments({
  config,
  selectableAliases,
  onDefaultChange,
  onUsageChange,
  t,
}: {
  config: AppConfig;
  selectableAliases: string[];
  onDefaultChange: (alias: string) => void;
  onUsageChange: (usage: typeof USAGE_KEYS[number], alias: string) => void;
  t: ReturnType<typeof useTranslations>;
}) {
  const optionsFor = (current: string) => {
    const options = current && !selectableAliases.includes(current)
      ? [current, ...selectableAliases]
      : selectableAliases;
    return ["", ...options];
  };
  return (
    <section className="w-full rounded-lg border border-border bg-surface-secondary/30 p-3">
      <div>
        <h3 className="text-sm font-semibold text-foreground">{t("assignments.title")}</h3>
        <p className="mt-1 text-xs text-muted">{t("assignments.description")}</p>
      </div>
      <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <AliasSelect
          label={t("assignments.default")}
          value={config.image_providers.default_provider}
          options={optionsFor(config.image_providers.default_provider)}
          providers={config.image_providers.providers}
          onChange={onDefaultChange}
          emptyLabel={t("assignments.none")}
        />
        {USAGE_KEYS.map((usage) => (
          <AliasSelect
            key={usage}
            label={t(`assignments.${usage}`)}
            value={config.image_providers.usages[usage]}
            options={optionsFor(config.image_providers.usages[usage])}
            providers={config.image_providers.providers}
            onChange={(alias) => onUsageChange(usage, alias)}
            emptyLabel={t("assignments.inherit")}
          />
        ))}
      </div>
    </section>
  );
}

function AliasSelect({
  label,
  value,
  options,
  providers,
  onChange,
  emptyLabel,
}: {
  label: string;
  value: string;
  options: string[];
  providers: Record<string, ImageProviderConfig>;
  onChange: (value: string) => void;
  emptyLabel: string;
}) {
  return (
    <Select
      aria-label={label}
      selectedKey={options.includes(value) ? value || "__empty__" : null}
      onSelectionChange={(key) => {
        if (key) onChange(String(key) === "__empty__" ? "" : String(key));
      }}
    >
      <Label className="text-sm text-muted">{label}</Label>
      <Select.Trigger className="rounded-lg border border-border bg-surface px-3 py-2 text-sm">
        <Select.Value />
      </Select.Trigger>
      <Select.Popover>
        <ListBox>
          {options.map((alias) => (
            <ListBoxItem
              key={alias || "__empty__"}
              id={alias || "__empty__"}
              textValue={alias || emptyLabel}
              isDisabled={Boolean(alias) && !imageProviderIsSelectable(providers[alias])}
            >
              {alias || emptyLabel}
            </ListBoxItem>
          ))}
        </ListBox>
      </Select.Popover>
    </Select>
  );
}

function ProviderDetail({
  alias,
  provider,
  isRenaming,
  renameValue,
  renameError,
  pendingDelete,
  hasUnsavedChanges,
  affectedUsages,
  testState,
  onChange,
  onRenameStart,
  onRenameValueChange,
  onRenameConfirm,
  onRenameCancel,
  onTest,
  onCloneForUsage,
  onDeleteAsk,
  onDeleteConfirm,
  onDeleteCancel,
  t,
}: {
  alias: string;
  provider: ImageProviderConfig;
  isRenaming: boolean;
  renameValue: string;
  renameError: string;
  pendingDelete: boolean;
  hasUnsavedChanges: boolean;
  affectedUsages: (typeof USAGE_KEYS)[number][];
  testState?: ProviderTestRunState;
  onChange: (provider: ImageProviderConfig) => void;
  onRenameStart: () => void;
  onRenameValueChange: (value: string) => void;
  onRenameConfirm: () => void;
  onRenameCancel: () => void;
  onTest: () => void;
  onCloneForUsage: (usage: (typeof USAGE_KEYS)[number]) => void;
  onDeleteAsk: () => void;
  onDeleteConfirm: () => void;
  onDeleteCancel: () => void;
  t: ReturnType<typeof useTranslations>;
}) {
  const testDisabled = provider.type !== "comfyui";
  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-border bg-surface-secondary/30 p-3">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="truncate text-lg font-semibold text-foreground">{alias}</h3>
              <StatusChip tone="accent">{t(`types.${provider.type}`)}</StatusChip>
              {provider.type === "openai_compatible" && (
                <StatusChip tone="warning">{t("unavailableBadge")}</StatusChip>
              )}
            </div>
            <p className="mt-1 text-sm text-muted">
              {provider.type === "comfyui"
                ? t("comfyuiSummary")
                : t("openaiSummary")}
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button
              size="sm"
              variant="outline"
              onPress={onRenameStart}
              isDisabled={testState?.status === "running"}
              className="border-border text-foreground"
            >
              {t("rename")}
            </Button>
            <Button
              size="sm"
              onPress={onTest}
              isDisabled={testDisabled || testState?.status === "running"}
              className="bg-accent text-white hover:bg-accent-hover"
            >
              {testState?.status === "running" ? t("test.running") : t("test.run")}
            </Button>
          </div>
        </div>
        {provider.type === "openai_compatible" ? (
          <div className="mt-3 rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-800 dark:border-amber-800 dark:bg-amber-950/30 dark:text-amber-200">
            {t("openaiUnavailable")}
          </div>
        ) : (
          <p className="mt-3 text-xs text-muted">
            {t(hasUnsavedChanges ? "test.draftUnsaved" : "test.draftOnly")}
          </p>
        )}
        {testState?.stale && provider.type === "comfyui" && (
          <div className="mt-3 rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-800 dark:border-amber-800 dark:bg-amber-950/30 dark:text-amber-200">
            {t("test.staleResult")}
          </div>
        )}

        {isRenaming && (
          <div className="mt-3 grid gap-3 border-t border-border pt-3 md:grid-cols-[minmax(0,1fr)_auto_auto] md:items-end">
            <div>
              <TextField value={renameValue} onChange={onRenameValueChange} isInvalid={Boolean(renameError)}>
                <Label className="text-sm text-muted">{t("renameTo")}</Label>
                <Input className="border-border" />
              </TextField>
              {renameError && <p className="mt-1 text-xs text-red-600">{renameError}</p>}
            </div>
            <Button size="sm" onPress={onRenameConfirm} className="bg-accent text-white">
              {t("confirm")}
            </Button>
            <Button size="sm" variant="outline" onPress={onRenameCancel} className="border-border">
              {t("cancel")}
            </Button>
          </div>
        )}
      </div>

      {provider.type === "comfyui" ? (
        <ComfyUIForm
          key={alias}
          provider={provider}
          testState={testState}
          affectedUsages={affectedUsages}
          onChange={onChange}
          onCloneForUsage={onCloneForUsage}
          t={t}
        />
      ) : (
        <OpenAICompatibleForm
          provider={provider}
          onChange={onChange}
          t={t}
        />
      )}

      <section className="rounded-lg border border-red-200 bg-red-50/60 p-3 dark:border-red-900/70 dark:bg-red-950/20">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h4 className="text-sm font-semibold text-red-700 dark:text-red-300">
              {t("danger.title")}
            </h4>
            <p className="mt-1 text-xs text-red-700/80 dark:text-red-300/80">
              {t("danger.description")}
            </p>
          </div>
          {pendingDelete ? (
            <div className="flex flex-wrap gap-2">
              <Button size="sm" onPress={onDeleteConfirm} className="bg-red-600 text-white">
                {t("danger.confirm")}
              </Button>
              <Button size="sm" variant="outline" onPress={onDeleteCancel} className="border-red-300 text-red-700">
                {t("cancel")}
              </Button>
            </div>
          ) : (
            <Button
              size="sm"
              variant="outline"
              onPress={onDeleteAsk}
              isDisabled={testState?.status === "running"}
              className="border-red-300 text-red-700"
            >
              {t("delete")}
            </Button>
          )}
        </div>
      </section>
    </div>
  );
}

function BaseConnectionFields({
  provider,
  onChange,
  includeRetries,
  t,
}: {
  provider: ImageProviderConfig;
  onChange: (provider: ImageProviderConfig) => void;
  includeRetries: boolean;
  t: ReturnType<typeof useTranslations>;
}) {
  return (
    <section className="rounded-lg border border-border bg-surface-secondary/20 p-3">
      <SectionTitle title={t("connection.title")} description={t("connection.description")} />
      <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2">
        <div className="md:col-span-2">
          <TextField value={provider.base_url} onChange={(base_url) => onChange({ ...provider, base_url })}>
            <Label className="text-sm text-muted">{t("connection.baseUrl")}</Label>
            <Input
              placeholder={provider.type === "comfyui" ? "http://127.0.0.1:8188" : "https://api.example.com/v1"}
              className="border-border"
            />
          </TextField>
        </div>
        <NumberField
          value={provider.timeout_seconds}
          minValue={1}
          onChange={(value) => onChange({ ...provider, timeout_seconds: Math.max(1, value) })}
        >
          <Label className="text-sm text-muted">{t("connection.timeout")}</Label>
          <NumberField.Group>
            <NumberField.DecrementButton />
            <NumberField.Input className="border-border" />
            <NumberField.IncrementButton />
          </NumberField.Group>
        </NumberField>
        <NumberField
          value={provider.max_concurrency}
          minValue={1}
          onChange={(value) => onChange({ ...provider, max_concurrency: Math.max(1, value) })}
        >
          <Label className="text-sm text-muted">{t("connection.maxConcurrency")}</Label>
          <NumberField.Group>
            <NumberField.DecrementButton />
            <NumberField.Input className="border-border" />
            <NumberField.IncrementButton />
          </NumberField.Group>
        </NumberField>
        {includeRetries && provider.type === "openai_compatible" && (
          <NumberField
            value={provider.max_retries}
            minValue={0}
            onChange={(value) => onChange({ ...provider, max_retries: Math.max(0, value) })}
          >
            <Label className="text-sm text-muted">{t("connection.maxRetries")}</Label>
            <NumberField.Group>
              <NumberField.DecrementButton />
              <NumberField.Input className="border-border" />
              <NumberField.IncrementButton />
            </NumberField.Group>
          </NumberField>
        )}
        <div className="flex items-end">
          <Switch
            isSelected={provider.enabled}
            isDisabled={provider.type === "openai_compatible" && !provider.enabled}
            onChange={(enabled) => {
              if (provider.type === "openai_compatible" && enabled) return;
              onChange({ ...provider, enabled });
            }}
          >
            <Switch.Control><Switch.Thumb /></Switch.Control>
            <Switch.Content className="text-sm">{t("connection.enabled")}</Switch.Content>
          </Switch>
        </div>
      </div>
    </section>
  );
}

function ComfyUIForm({
  provider,
  testState,
  affectedUsages,
  onChange,
  onCloneForUsage,
  t,
}: {
  provider: ComfyUIImageProviderConfig;
  testState?: ProviderTestRunState;
  affectedUsages: (typeof USAGE_KEYS)[number][];
  onChange: (provider: ImageProviderConfig) => void;
  onCloneForUsage: (usage: (typeof USAGE_KEYS)[number]) => void;
  t: ReturnType<typeof useTranslations>;
}) {
  const workflow = provider.workflow;
  const bindings = workflow.bindings;
  const duplicateTargets = useMemo(() => {
    const targets = new Map<string, string>();
    const duplicates = new Set<string>();
    Object.entries(bindings).forEach(([slot, binding]) => {
      const key = `${binding.node_id}.${binding.input}`;
      const previous = targets.get(key);
      if (previous) {
        duplicates.add(previous);
        duplicates.add(slot);
      } else {
        targets.set(key, slot);
      }
    });
    return duplicates;
  }, [bindings]);

  const updateWorkflow = (next: Partial<ComfyUIImageProviderConfig["workflow"]>) => {
    onChange({ ...provider, workflow: { ...workflow, ...next } });
  };

  const renameBinding = (from: string, toValue: string) => {
    const to = toValue.trim();
    if (!to || to === from || to in bindings) return;
    updateWorkflow({
      bindings: Object.fromEntries(
        Object.entries(bindings).map(([slot, binding]) => [
          slot === from ? to : slot,
          binding,
        ]),
      ),
    });
  };

  return (
    <>
      <BaseConnectionFields provider={provider} onChange={onChange} includeRetries={false} t={t} />

      <section className="rounded-lg border border-border bg-surface-secondary/20 p-3">
        <SectionTitle title={t("workflow.title")} description={t("workflow.description")} />
        <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2">
          <div className="md:col-span-2">
            <TextField
              value={workflow.template_path}
              onChange={(template_path) => updateWorkflow({ template_path })}
            >
              <Label className="text-sm text-muted">{t("workflow.templatePath")}</Label>
              <Input className="border-border" />
            </TextField>
            <p className="mt-1 text-xs text-muted">{t("workflow.templatePathHint")}</p>
          </div>
          <TextField value={workflow.template_revision} isReadOnly>
            <Label className="text-sm text-muted">{t("workflow.templateRevision")}</Label>
            <Input
              readOnly
              placeholder={t("workflow.templateRevisionEmpty")}
              className="border-border font-mono text-xs"
            />
          </TextField>
          <label className="block text-sm text-muted">
            <span>{t("workflow.referenceMode")}</span>
            <select
              value={workflow.reference_mode}
              onChange={(event) => updateWorkflow({
                reference_mode: event.target.value as typeof workflow.reference_mode,
              })}
              className="mt-1 w-full rounded-lg border border-border bg-surface px-3 py-2 text-sm text-foreground"
            >
              {(["none", "img2img", "controlnet", "style_reference", "edit_model"] as const).map((mode) => (
                <option key={mode} value={mode}>{t(`workflow.referenceModes.${mode}`)}</option>
              ))}
            </select>
          </label>
        </div>
      </section>

      <ComfyUIWorkflowOverrides
        workflow={workflow}
        controls={testState?.response?.workflow_inputs ?? []}
        inspected={Boolean(testState?.response)}
        affectedUsages={affectedUsages}
        onChange={(nextWorkflow) => onChange({ ...provider, workflow: nextWorkflow })}
        onCloneForUsage={onCloneForUsage}
        t={t}
      />

      <section className="rounded-lg border border-border bg-surface-secondary/20 p-3">
        <SectionTitle title={t("slots.title")} description={t("slots.description")} />
        <div className="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
          {KNOWN_SEMANTIC_SLOTS.map((slot) => {
            const binding = bindings[slot];
            return (
              <div
                key={slot}
                className={`rounded-lg border p-2.5 ${
                  binding
                    ? "border-green-300 bg-green-50 dark:border-green-900 dark:bg-green-950/20"
                    : "border-border bg-surface"
                }`}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="min-w-0 truncate text-sm font-medium text-foreground">{slot}</span>
                  <StatusChip tone={binding ? "success" : "muted"}>
                    {binding ? t("slots.declared") : t("slots.ignored")}
                  </StatusChip>
                </div>
                {binding && (
                  <p className="mt-1 truncate text-xs text-muted">
                    {binding.node_id}.{binding.input}
                    {binding.required ? ` · ${t("slots.required")}` : ""}
                  </p>
                )}
              </div>
            );
          })}
        </div>
        <p className="mt-3 text-xs text-muted">{t("slots.ignoredHint")}</p>

        <div className="mt-4 space-y-3 border-t border-border pt-3">
          {Object.entries(bindings).map(([slot, binding]) => (
            <div
              key={slot}
              className={`rounded-lg border bg-surface p-3 ${
                duplicateTargets.has(slot) ? "border-red-400" : "border-border"
              }`}
            >
              <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_minmax(0,1fr)_auto] md:items-end">
                <TextField
                  defaultValue={slot}
                  onBlur={(event) => renameBinding(slot, event.target.value)}
                >
                  <Label className="text-xs text-muted">{t("slots.semanticName")}</Label>
                  <Input className="border-border" />
                </TextField>
                <TextField
                  value={binding.node_id}
                  onChange={(node_id) => updateWorkflow({
                    bindings: { ...bindings, [slot]: { ...binding, node_id } },
                  })}
                >
                  <Label className="text-xs text-muted">{t("slots.nodeId")}</Label>
                  <Input className="border-border" />
                </TextField>
                <TextField
                  value={binding.input}
                  onChange={(input) => updateWorkflow({
                    bindings: { ...bindings, [slot]: { ...binding, input } },
                  })}
                >
                  <Label className="text-xs text-muted">{t("slots.input")}</Label>
                  <Input className="border-border" />
                </TextField>
                <Button
                  size="sm"
                  variant="outline"
                  onPress={() => {
                    const next = { ...bindings };
                    delete next[slot];
                    updateWorkflow({ bindings: next });
                  }}
                  className="border-border text-red-600"
                >
                  {t("remove")}
                </Button>
              </div>
              <div className="mt-3 flex flex-wrap gap-4">
                <Switch
                  isSelected={binding.required}
                  onChange={(required) => updateWorkflow({
                    bindings: { ...bindings, [slot]: { ...binding, required } },
                  })}
                >
                  <Switch.Control><Switch.Thumb /></Switch.Control>
                  <Switch.Content className="text-xs">{t("slots.required")}</Switch.Content>
                </Switch>
                <Switch
                  isSelected={binding.upload}
                  onChange={(upload) => updateWorkflow({
                    bindings: { ...bindings, [slot]: { ...binding, upload } },
                  })}
                >
                  <Switch.Control><Switch.Thumb /></Switch.Control>
                  <Switch.Content className="text-xs">{t("slots.upload")}</Switch.Content>
                </Switch>
              </div>
              {duplicateTargets.has(slot) && (
                <p className="mt-2 text-xs text-red-600">{t("slots.collision")}</p>
              )}
            </div>
          ))}
          <Button
            size="sm"
            variant="outline"
            onPress={() => {
              const slot = uniqueName("slot", bindings);
              const binding: ComfyUIWorkflowInputBinding = {
                node_id: "",
                input: "",
                required: false,
                upload: false,
              };
              updateWorkflow({ bindings: { ...bindings, [slot]: binding } });
            }}
            className="border-border text-foreground"
          >
            + {t("slots.add")}
          </Button>
        </div>
      </section>

      <section className="grid gap-4 xl:grid-cols-2">
        <div className="rounded-lg border border-border bg-surface-secondary/20 p-3">
          <SectionTitle title={t("outputs.title")} description={t("outputs.description")} />
          <div className="mt-3 space-y-3">
            {workflow.outputs.map((output, index) => (
              <div key={`${index}-${output.node_id}-${output.field}`} className="grid gap-3 rounded-lg border border-border bg-surface p-3 sm:grid-cols-[1fr_1fr_auto] sm:items-end">
                <TextField
                  value={output.node_id}
                  onChange={(node_id) => updateWorkflow({
                    outputs: workflow.outputs.map((item, itemIndex) => (
                      itemIndex === index ? { ...item, node_id } : item
                    )),
                  })}
                >
                  <Label className="text-xs text-muted">{t("slots.nodeId")}</Label>
                  <Input className="border-border" />
                </TextField>
                <TextField
                  value={output.field}
                  onChange={(field) => updateWorkflow({
                    outputs: workflow.outputs.map((item, itemIndex) => (
                      itemIndex === index ? { ...item, field } : item
                    )),
                  })}
                >
                  <Label className="text-xs text-muted">{t("outputs.field")}</Label>
                  <Input className="border-border" />
                </TextField>
                <Button
                  size="sm"
                  variant="outline"
                  onPress={() => updateWorkflow({
                    outputs: workflow.outputs.filter((_, itemIndex) => itemIndex !== index),
                  })}
                  className="border-border text-red-600"
                >
                  {t("remove")}
                </Button>
              </div>
            ))}
            <Button
              size="sm"
              variant="outline"
              onPress={() => updateWorkflow({
                outputs: [...workflow.outputs, { node_id: "", field: "images" }],
              })}
              className="border-border text-foreground"
            >
              + {t("outputs.add")}
            </Button>
          </div>
        </div>

        <div className="rounded-lg border border-border bg-surface-secondary/20 p-3">
          <SectionTitle title={t("dependencies.title")} description={t("dependencies.description")} />
          <p className="mt-3 text-xs text-muted">{t("dependencies.legacyNote")}</p>
          <div className="mt-3 space-y-3">
            {(["node_types", "checkpoints", "loras"] as const).map((kind) => (
              <div key={kind}>
                <div className="text-xs font-medium text-foreground">{t(`dependencies.${kind}`)}</div>
                <div className="mt-1 flex flex-wrap gap-1.5">
                  {workflow.dependencies[kind].length > 0 ? workflow.dependencies[kind].map((item) => (
                    <code key={item} className="max-w-full break-all rounded bg-warm-200 px-2 py-1 text-xs text-muted dark:bg-warm-300/30">
                      {item}
                    </code>
                  )) : (
                    <span className="text-xs text-muted">{t("dependencies.none")}</span>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      </section>

      <ReadinessResult testState={testState} t={t} />
    </>
  );
}

function ReadinessResult({
  testState,
  t,
}: {
  testState?: ProviderTestRunState;
  t: ReturnType<typeof useTranslations>;
}) {
  if (!testState || testState.status === "idle") return null;
  if (testState.status === "running") {
    return (
      <section className="rounded-lg border border-border bg-surface-secondary/20 p-3 text-sm text-muted">
        {t("test.running")}
      </section>
    );
  }
  if (testState.status === "error") {
    return (
      <section className="rounded-lg border border-red-300 bg-red-50 p-3 text-sm text-red-700 dark:border-red-800 dark:bg-red-950/30 dark:text-red-300">
        <div className="font-medium">{t("test.failure.generic.message")}</div>
        <p className="mt-1">{t("test.failure.generic.action")}</p>
      </section>
    );
  }
  const response = testState.response;
  if (!response) return null;
  const failureCode = response.failure && LOCALIZED_TEST_FAILURE_CODES.has(response.failure.code)
    ? response.failure.code
    : "generic";
  return (
    <section className="rounded-lg border border-border bg-surface-secondary/20 p-3">
      <SectionTitle
        title={t("test.resultTitle")}
        description={t(response.status === "passed" ? "test.summaryPassed" : "test.summaryFailed")}
      />
      <div className="mt-3 grid gap-2 sm:grid-cols-3">
        <Metric label={t("test.version")} value={response.comfyui_version || "—"} />
        <Metric label={t("test.runningJobs")} value={String(response.queue_running)} />
        <Metric label={t("test.pendingJobs")} value={String(response.queue_pending)} />
      </div>
      {testState.revisionBackfilled && (
        <div className="mt-3 rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-800 dark:border-amber-800 dark:bg-amber-950/30 dark:text-amber-200">
          {t("test.revisionBackfilled")}
        </div>
      )}
      <div className="mt-3 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        {response.dependency_checks.map((check) => (
          <div key={check.kind} className="min-w-0 rounded-lg border border-border bg-surface p-3">
            <div className="flex items-center justify-between gap-2">
              <h5 className="truncate text-sm font-semibold text-foreground">
                {t(`dependencies.${check.kind}`)}
              </h5>
              <StatusChip tone={check.status === "passed" ? "success" : "danger"}>
                {check.status === "passed" ? t("test.ready") : t("test.missing")}
              </StatusChip>
            </div>
            <p className="mt-1 text-xs text-muted">
              {t("test.readyCount", {
                ready: check.available.length,
                total: check.required.length,
              })}
            </p>
            {check.missing.length > 0 && (
              <ul className="mt-2 space-y-1 text-xs text-red-600 dark:text-red-300">
                {check.missing.map((item) => (
                  <li key={item} className="break-all">{item}</li>
                ))}
              </ul>
            )}
          </div>
        ))}
      </div>
      {response.failure && (
        <div className="mt-3 rounded-lg border border-red-300 bg-red-50 p-3 text-sm text-red-700 dark:border-red-800 dark:bg-red-950/30 dark:text-red-300">
          <div className="font-medium">{t(`test.failure.${failureCode}.message`)}</div>
          <p className="mt-1">{t(`test.failure.${failureCode}.action`)}</p>
        </div>
      )}
      <p className="mt-3 text-xs text-muted">{t("test.promptIsFinalCheck")}</p>
    </section>
  );
}

function OpenAICompatibleForm({
  provider,
  onChange,
  t,
}: {
  provider: OpenAICompatibleImageProviderConfig;
  onChange: (provider: ImageProviderConfig) => void;
  t: ReturnType<typeof useTranslations>;
}) {
  const [showKey, setShowKey] = useState(false);
  const parameters = provider.parameters;

  const updateParameter = (slot: string, mapping: ImageParameterMapping | null) => {
    onChange({
      ...provider,
      parameters: { ...parameters, [slot]: mapping },
    });
  };

  const renameParameter = (from: string, toValue: string) => {
    const to = toValue.trim();
    if (!to || to === from || to in parameters) return;
    onChange({
      ...provider,
      parameters: Object.fromEntries(
        Object.entries(parameters).map(([slot, mapping]) => [
          slot === from ? to : slot,
          mapping,
        ]),
      ),
    });
  };

  return (
    <>
      <BaseConnectionFields provider={provider} onChange={onChange} includeRetries t={t} />

      <section className="rounded-lg border border-border bg-surface-secondary/20 p-3">
        <SectionTitle title={t("openai.connectionTitle")} description={t("openai.connectionDescription")} />
        <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2">
          <TextField
            value={provider.default_model}
            onChange={(default_model) => onChange({ ...provider, default_model })}
          >
            <Label className="text-sm text-muted">{t("openai.defaultModel")}</Label>
            <Input className="border-border" />
          </TextField>
          <div>
            <TextField
              value={provider.api_key || ""}
              onChange={(api_key) => onChange({
                ...provider,
                api_key,
                api_key_mode: "replace",
              })}
            >
              <Label className="flex flex-wrap items-center justify-between gap-2 text-sm text-muted">
                <span>{t("openai.apiKey")}</span>
                <StatusChip tone={provider.has_api_key ? "success" : "muted"}>
                  {provider.has_api_key ? t("openai.apiKeyStored") : t("openai.apiKeyNotStored")}
                </StatusChip>
              </Label>
              <Input
                type={showKey ? "text" : "password"}
                placeholder={
                  provider.has_api_key && provider.api_key_mode !== "clear"
                    ? t("openai.apiKeySaved")
                    : ""
                }
                className="border-border"
              />
            </TextField>
            <div className="mt-1 flex flex-wrap gap-3 text-xs">
              <button
                type="button"
                onClick={() => setShowKey((current) => !current)}
                className="text-muted hover:text-foreground"
              >
                {showKey ? t("openai.hideKey") : t("openai.showKey")}
              </button>
              {(provider.has_api_key || provider.api_key) && (
                <button
                  type="button"
                  onClick={() => onChange({
                    ...provider,
                    api_key: "",
                    api_key_mode: "clear",
                    has_api_key: false,
                  })}
                  className="text-red-600 hover:text-red-700"
                >
                  {t("openai.clearKey")}
                </button>
              )}
            </div>
          </div>
        </div>
      </section>

      <section className="rounded-lg border border-border bg-surface-secondary/20 p-3">
        <SectionTitle title={t("parameters.title")} description={t("parameters.description")} />
        <div className="mt-3 space-y-3">
          {Object.entries(parameters).map(([slot, mapping]) => (
            <ParameterRow
              key={slot}
              slot={slot}
              mapping={mapping}
              onRename={(value) => renameParameter(slot, value)}
              onChange={(next) => updateParameter(slot, next)}
              onRemove={() => {
                const next = { ...parameters };
                delete next[slot];
                onChange({ ...provider, parameters: next });
              }}
              t={t}
            />
          ))}
          <Button
            size="sm"
            variant="outline"
            onPress={() => {
              const slot = uniqueName("parameter", parameters);
              updateParameter(slot, null);
            }}
            className="border-border text-foreground"
          >
            + {t("parameters.add")}
          </Button>
        </div>
      </section>

      <section className="rounded-lg border border-border bg-surface-secondary/20 p-3">
        <SectionTitle title={t("result.title")} description={t("result.description")} />
        <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2">
          {(Object.keys(provider.result) as (keyof typeof provider.result)[]).map((field) => (
            <TextField
              key={field}
              value={provider.result[field]}
              onChange={(value) => onChange({
                ...provider,
                result: { ...provider.result, [field]: value },
              })}
            >
              <Label className="text-sm text-muted">{t(`result.${field}`)}</Label>
              <Input className="border-border" />
            </TextField>
          ))}
        </div>
      </section>
    </>
  );
}

function ParameterRow({
  slot,
  mapping,
  onRename,
  onChange,
  onRemove,
  t,
}: {
  slot: string;
  mapping: ImageParameterMapping | null;
  onRename: (value: string) => void;
  onChange: (mapping: ImageParameterMapping | null) => void;
  onRemove: () => void;
  t: ReturnType<typeof useTranslations>;
}) {
  const kind = mapping?.kind || "unsupported";
  return (
    <div className="rounded-lg border border-border bg-surface p-3">
      <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_220px_auto] md:items-end">
        <TextField defaultValue={slot} onBlur={(event) => onRename(event.target.value)}>
          <Label className="text-xs text-muted">{t("parameters.semanticSlot")}</Label>
          <Input className="border-border" />
        </TextField>
        <label className="block text-xs text-muted">
          <span>{t("parameters.mappingKind")}</span>
          <select
            value={kind}
            onChange={(event) => {
              const nextKind = event.target.value;
              if (nextKind === "unsupported") onChange(null);
              else if (nextKind === "field") {
                onChange({ kind: "field", field: "" });
              } else {
                onChange({ kind: "value_map", values: { preset: { field: "" } } });
              }
            }}
            className="mt-1 w-full rounded-lg border border-border bg-surface px-3 py-2 text-sm text-foreground"
          >
            <option value="unsupported">{t("parameters.unsupported")}</option>
            <option value="field">{t("parameters.field")}</option>
            <option value="value_map">{t("parameters.valueMap")}</option>
          </select>
        </label>
        <Button size="sm" variant="outline" onPress={onRemove} className="border-border text-red-600">
          {t("remove")}
        </Button>
      </div>
      {mapping?.kind === "field" && (
        <FieldMappingEditor mapping={mapping} onChange={onChange} t={t} />
      )}
      {mapping?.kind === "value_map" && (
        <ValueMapEditor mapping={mapping} onChange={onChange} t={t} />
      )}
    </div>
  );
}

function FieldMappingEditor({
  mapping,
  onChange,
  t,
}: {
  mapping: ImageFieldParameterMapping;
  onChange: (mapping: ImageFieldParameterMapping) => void;
  t: ReturnType<typeof useTranslations>;
}) {
  return (
    <div className="mt-3 grid gap-3 border-t border-border pt-3 md:grid-cols-2 xl:grid-cols-4">
      <TextField value={mapping.field} onChange={(field) => onChange({ ...mapping, field })}>
        <Label className="text-xs text-muted">{t("parameters.backendField")}</Label>
        <Input className="border-border" />
      </TextField>
      <TextField
        value={mapping.minimum == null ? "" : String(mapping.minimum)}
        onChange={(value) => onChange({ ...mapping, minimum: parseOptionalNumber(value) })}
      >
        <Label className="text-xs text-muted">{t("parameters.minimum")}</Label>
        <Input type="number" className="border-border" />
      </TextField>
      <TextField
        value={mapping.maximum == null ? "" : String(mapping.maximum)}
        onChange={(value) => onChange({ ...mapping, maximum: parseOptionalNumber(value) })}
      >
        <Label className="text-xs text-muted">{t("parameters.maximum")}</Label>
        <Input type="number" className="border-border" />
      </TextField>
      <TextField
        value={mapping.default == null ? "" : JSON.stringify(mapping.default)}
        onChange={(value) => onChange({
          ...mapping,
          default: value.trim() ? parseScalar(value) : null,
        })}
      >
        <Label className="text-xs text-muted">{t("parameters.default")}</Label>
        <Input className="border-border" />
      </TextField>
      <div className="md:col-span-2 xl:col-span-4">
        <JsonEditor
          value={mapping.allowed_values ?? []}
          expected="array"
          label={t("parameters.allowedValues")}
          hint={t("parameters.allowedValuesHint")}
          onCommit={(value) => onChange({
            ...mapping,
            allowed_values: value as ImageConfigScalar[],
          })}
          t={t}
        />
      </div>
    </div>
  );
}

function ValueMapEditor({
  mapping,
  onChange,
  t,
}: {
  mapping: ImageValueMapParameterMapping;
  onChange: (mapping: ImageValueMapParameterMapping) => void;
  t: ReturnType<typeof useTranslations>;
}) {
  return (
    <div className="mt-3 border-t border-border pt-3">
      <JsonEditor
        value={mapping.values}
        expected="object"
        label={t("parameters.valueMapValues")}
        hint={t("parameters.valueMapHint")}
        onCommit={(value) => onChange({
          ...mapping,
          values: value as ImageValueMapParameterMapping["values"],
        })}
        t={t}
      />
    </div>
  );
}

interface JsonEditorProps {
  value: object;
  expected: "array" | "object";
  label: string;
  hint: string;
  onCommit: (value: object) => void;
  t: ReturnType<typeof useTranslations>;
}

function JsonEditor({ value, ...props }: JsonEditorProps) {
  const initialDraft = JSON.stringify(value, null, 2);
  return <JsonEditorDraft key={initialDraft} initialDraft={initialDraft} {...props} />;
}

function JsonEditorDraft({
  initialDraft,
  expected,
  label,
  hint,
  onCommit,
  t,
}: Omit<JsonEditorProps, "value"> & { initialDraft: string }) {
  const [draft, setDraft] = useState(initialDraft);
  const [error, setError] = useState("");
  const commit = () => {
    try {
      const parsed: unknown = JSON.parse(draft);
      const valid = expected === "array"
        ? Array.isArray(parsed)
        : Boolean(parsed && typeof parsed === "object" && !Array.isArray(parsed));
      if (!valid) {
        setError(t(`parameters.json${expected === "array" ? "Array" : "Object"}`));
        return;
      }
      setError("");
      onCommit(parsed as object);
    } catch {
      setError(t("parameters.jsonInvalid"));
    }
  };
  return (
    <label className="block text-xs text-muted">
      <span>{label}</span>
      <textarea
        value={draft}
        onChange={(event) => setDraft(event.currentTarget.value)}
        onBlur={commit}
        rows={Math.min(12, Math.max(3, draft.split("\n").length))}
        spellCheck={false}
        className={`mt-1 w-full resize-y rounded-lg border bg-surface px-3 py-2 font-mono text-xs text-foreground ${
          error ? "border-red-400" : "border-border"
        }`}
      />
      <span className={`mt-1 block ${error ? "text-red-600" : "text-muted"}`}>
        {error || hint}
      </span>
    </label>
  );
}

function SectionTitle({ title, description }: { title: string; description: string }) {
  return (
    <div>
      <h4 className="text-base font-semibold text-foreground">{title}</h4>
      <p className="mt-1 max-w-3xl text-xs text-muted">{description}</p>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-border bg-surface p-3">
      <div className="text-xs text-muted">{label}</div>
      <div className="mt-1 truncate text-sm font-semibold text-foreground" title={value}>
        {value}
      </div>
    </div>
  );
}

function StatusChip({
  tone,
  children,
}: {
  tone: "accent" | "success" | "warning" | "danger" | "muted";
  children: ReactNode;
}) {
  const classes: Record<typeof tone, string> = {
    accent: "bg-accent/10 text-accent",
    success: "bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-300",
    warning: "bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300",
    danger: "bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-300",
    muted: "bg-warm-200 text-muted dark:bg-warm-300/30",
  };
  return (
    <Chip size="sm" variant="soft" className={classes[tone]}>
      {children}
    </Chip>
  );
}
