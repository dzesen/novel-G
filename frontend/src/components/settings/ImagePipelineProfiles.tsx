"use client";

import { useMemo, useState } from "react";
import { Button, Input, Label, TextField } from "@heroui/react";
import { useTranslations } from "next-intl";
import type {
  AppConfig,
  ImagePipelineProfile,
  ImagePipelineStatusView,
} from "@/types/config";
import {
  addImagePipelineProfile,
  removeImagePipelineAlias,
  renameImagePipelineAlias,
  setImagePipelineKind,
} from "@/types/config";

const PIPELINE_ALIAS_REGEX = /^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$/;

type Props = {
  config: AppConfig;
  statuses: ImagePipelineStatusView[];
  selectableProviderAliases: string[];
  hasUnsavedChanges: boolean;
  onChange: (config: AppConfig) => void;
};

export function ImagePipelineProfiles({
  config,
  statuses,
  selectableProviderAliases,
  hasUnsavedChanges,
  onChange,
}: Props) {
  const t = useTranslations("settings.imageProvider.pipelines");
  const pipelines = config.image_providers.pipelines;
  const aliases = useMemo(() => Object.keys(pipelines), [pipelines]);
  const [selectedAlias, setSelectedAlias] = useState("");
  const [showAdd, setShowAdd] = useState(false);
  const [newAlias, setNewAlias] = useState("");
  const [newKind, setNewKind] = useState<ImagePipelineProfile["kind"]>("quick");
  const [aliasError, setAliasError] = useState("");
  const [renaming, setRenaming] = useState(false);
  const [renameValue, setRenameValue] = useState("");
  const [renameError, setRenameError] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const statusMap = useMemo(
    () => new Map(statuses.map((status) => [status.alias, status])),
    [statuses],
  );

  const activeAlias = selectedAlias && pipelines[selectedAlias]
    ? selectedAlias
    : aliases[0] || "";
  const selectedProfile = activeAlias ? pipelines[activeAlias] : undefined;
  const selectedStatus = activeAlias ? statusMap.get(activeAlias) : undefined;
  const preferredProvider = selectableProviderAliases[0] || "";

  const updateImageConfig = (next: AppConfig["image_providers"]) => {
    onChange({ ...config, image_providers: next });
  };

  const updateProfile = (profile: ImagePipelineProfile) => {
    if (!activeAlias) return;
    updateImageConfig({
      ...config.image_providers,
      pipelines: { ...pipelines, [activeAlias]: profile },
    });
  };

  const addPipeline = () => {
    const alias = newAlias.trim();
    if (!PIPELINE_ALIAS_REGEX.test(alias)) {
      setAliasError(t("aliasRule"));
      return;
    }
    if (alias in pipelines) {
      setAliasError(t("aliasDuplicate"));
      return;
    }
    if (!preferredProvider) {
      setAliasError(t("providerRequired"));
      return;
    }
    const next = addImagePipelineProfile(
      config,
      alias,
      newKind,
      preferredProvider,
    );
    if (next === config) {
      setAliasError(t("aliasRule"));
      return;
    }
    onChange(next);
    setSelectedAlias(alias);
    setNewAlias("");
    setAliasError("");
    setShowAdd(false);
  };

  const confirmRename = () => {
    const nextAlias = renameValue.trim();
    if (!PIPELINE_ALIAS_REGEX.test(nextAlias)) {
      setRenameError(t("aliasRule"));
      return;
    }
    if (nextAlias !== activeAlias && nextAlias in pipelines) {
      setRenameError(t("aliasDuplicate"));
      return;
    }
    const next = renameImagePipelineAlias(config, activeAlias, nextAlias);
    if (next === config && nextAlias !== activeAlias) {
      setRenameError(t("aliasRule"));
      return;
    }
    if (next !== config) onChange(next);
    setSelectedAlias(nextAlias);
    setRenaming(false);
    setRenameError("");
  };

  const removePipeline = () => {
    const next = removeImagePipelineAlias(config, activeAlias);
    onChange(next);
    setConfirmDelete(false);
    setRenaming(false);
  };

  const setKind = (kind: ImagePipelineProfile["kind"]) => {
    const next = setImagePipelineKind(
      config,
      activeAlias,
      kind,
      preferredProvider,
    );
    if (next !== config) onChange(next);
  };

  return (
    <section
      data-testid="image-pipeline-profiles"
      className="w-full rounded-lg border border-border bg-surface-secondary/30 p-3"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold text-foreground">{t("title")}</h3>
          <p className="mt-1 max-w-3xl text-xs text-muted">{t("description")}</p>
        </div>
        <label className="w-full text-sm text-muted sm:w-72">
          <span>{t("defaultLabel")}</span>
          <select
            aria-label={t("defaultLabel")}
            value={config.image_providers.default_scene_pipeline}
            onChange={(event) => updateImageConfig({
              ...config.image_providers,
              default_scene_pipeline: event.target.value,
            })}
            className="mt-1 w-full rounded-lg border border-border bg-surface px-3 py-2 text-sm text-foreground"
          >
            <option value="">{t("none")}</option>
            {aliases.map((alias) => (
              <option key={alias} value={alias}>{alias}</option>
            ))}
          </select>
        </label>
      </div>

      <div className="mt-3 grid grid-cols-1 gap-3 lg:grid-cols-[240px_minmax(0,1fr)]">
        <aside className="rounded-lg border border-border bg-surface/70 p-3">
          <div className="flex items-center justify-between gap-2">
            <span className="text-xs font-medium text-muted">
              {t("count", { count: aliases.length })}
            </span>
            {!showAdd && (
              <Button
                size="sm"
                variant="outline"
                onPress={() => setShowAdd(true)}
                className="border-border text-foreground"
              >
                {t("add")}
              </Button>
            )}
          </div>
          <div className="mt-3 space-y-2">
            {aliases.map((alias) => {
              const profile = pipelines[alias];
              const quality = statusMap.get(alias)?.quality_status || "experimental";
              return (
                <button
                  key={alias}
                  type="button"
                  onClick={() => {
                    setSelectedAlias(alias);
                    setRenaming(false);
                    setConfirmDelete(false);
                  }}
                  className={`w-full rounded-lg border p-3 text-left transition-colors ${
                    activeAlias === alias
                      ? "border-accent bg-surface"
                      : "border-border bg-surface/80 hover:border-warm-400"
                  }`}
                >
                  <div className="truncate text-sm font-semibold text-foreground">{alias}</div>
                  <div className="mt-1 flex flex-wrap gap-1 text-xs text-muted">
                    <span>{profile.display_name || t(`kinds.${profile.kind}`)}</span>
                    <span aria-hidden="true">·</span>
                    <span>{t(`quality.${quality}`)}</span>
                  </div>
                </button>
              );
            })}
            {aliases.length === 0 && (
              <div className="rounded-lg border border-dashed border-border p-3 text-xs text-muted">
                {t("empty")}
              </div>
            )}
          </div>

          {showAdd && (
            <div className="mt-3 space-y-3 border-t border-border pt-3">
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
                <span>{t("kind")}</span>
                <select
                  aria-label={t("kind")}
                  value={newKind}
                  onChange={(event) => setNewKind(
                    event.target.value as ImagePipelineProfile["kind"],
                  )}
                  className="mt-1 w-full rounded-lg border border-border bg-surface px-3 py-2 text-sm text-foreground"
                >
                  <option value="quick">{t("kinds.quick")}</option>
                  <option value="consistency">{t("kinds.consistency")}</option>
                </select>
              </label>
              {aliasError && <p className="text-xs text-red-600">{aliasError}</p>}
              <div className="grid grid-cols-2 gap-2">
                <Button
                  size="sm"
                  onPress={addPipeline}
                  isDisabled={!preferredProvider}
                  className="bg-accent text-white"
                >
                  {t("confirmAdd")}
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  onPress={() => {
                    setShowAdd(false);
                    setAliasError("");
                  }}
                  className="border-border text-foreground"
                >
                  {t("cancel")}
                </Button>
              </div>
            </div>
          )}
        </aside>

        <div className="min-w-0 rounded-lg border border-border bg-surface p-3">
          {selectedProfile ? (
            <div className="space-y-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <h4 className="break-all text-base font-semibold text-foreground">
                      {activeAlias}
                    </h4>
                    <span className="rounded-full bg-accent/10 px-2 py-0.5 text-xs text-accent">
                      {t(`kinds.${selectedProfile.kind}`)}
                    </span>
                    <span className={`rounded-full px-2 py-0.5 text-xs ${
                      selectedStatus?.quality_status === "drifted"
                        ? "bg-red-100 text-red-700 dark:bg-red-950/40 dark:text-red-300"
                        : "bg-amber-100 text-amber-800 dark:bg-amber-950/40 dark:text-amber-200"
                    }`}>
                      {t(`quality.${selectedStatus?.quality_status || "experimental"}`)}
                    </span>
                  </div>
                  <p className="mt-1 text-xs text-muted">{t("closedHint")}</p>
                </div>
                <div className="flex flex-wrap gap-2">
                  <Button
                    size="sm"
                    variant="outline"
                    onPress={() => {
                      setRenaming(true);
                      setRenameValue(activeAlias);
                      setRenameError("");
                    }}
                    className="border-border text-foreground"
                  >
                    {t("rename")}
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    onPress={() => setConfirmDelete(true)}
                    className="border-red-300 text-red-700 dark:border-red-800 dark:text-red-300"
                  >
                    {t("delete")}
                  </Button>
                </div>
              </div>

              {renaming && (
                <div className="rounded-lg border border-border bg-surface-secondary/30 p-3">
                  <TextField
                    value={renameValue}
                    onChange={(value) => {
                      setRenameValue(value);
                      setRenameError("");
                    }}
                    isInvalid={Boolean(renameError)}
                  >
                    <Label className="text-sm text-muted">{t("renameTo")}</Label>
                    <Input className="border-border" />
                  </TextField>
                  {renameError && <p className="mt-1 text-xs text-red-600">{renameError}</p>}
                  <div className="mt-3 flex flex-wrap gap-2">
                    <Button size="sm" onPress={confirmRename} className="bg-accent text-white">
                      {t("confirmRename")}
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      onPress={() => setRenaming(false)}
                      className="border-border text-foreground"
                    >
                      {t("cancel")}
                    </Button>
                  </div>
                </div>
              )}

              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <TextField
                  value={selectedProfile.display_name}
                  onChange={(value) => updateProfile({
                    ...selectedProfile,
                    display_name: value.slice(0, 120),
                  })}
                >
                  <Label className="text-sm text-muted">{t("displayName")}</Label>
                  <Input className="border-border" />
                </TextField>
                <label className="block text-sm text-muted">
                  <span>{t("kind")}</span>
                  <select
                    aria-label={t("kind")}
                    value={selectedProfile.kind}
                    onChange={(event) => setKind(
                      event.target.value as ImagePipelineProfile["kind"],
                    )}
                    className="mt-1 w-full rounded-lg border border-border bg-surface px-3 py-2 text-sm text-foreground"
                  >
                    <option value="quick">{t("kinds.quick")}</option>
                    <option value="consistency">{t("kinds.consistency")}</option>
                  </select>
                </label>
                <ProviderSelect
                  label={t("composeProvider")}
                  value={selectedProfile.compose_provider}
                  options={selectableProviderAliases}
                  onChange={(compose_provider) => updateProfile({
                    ...selectedProfile,
                    compose_provider,
                  })}
                  missingLabel={t("providerMissing")}
                />
                {selectedProfile.kind === "consistency" && (
                  <>
                    <ProviderSelect
                      label={t("identityProvider")}
                      value={selectedProfile.identity_edit_provider}
                      options={selectableProviderAliases}
                      onChange={(identity_edit_provider) => updateProfile({
                        ...selectedProfile,
                        identity_edit_provider,
                      })}
                      missingLabel={t("providerMissing")}
                    />
                    <ProviderSelect
                      label={t("refineProvider")}
                      value={selectedProfile.refine_provider || ""}
                      options={selectableProviderAliases}
                      optional
                      onChange={(refineProvider) => {
                        const nextProfile = { ...selectedProfile };
                        if (refineProvider) nextProfile.refine_provider = refineProvider;
                        else delete nextProfile.refine_provider;
                        updateProfile(nextProfile);
                      }}
                      missingLabel={t("providerMissing")}
                      emptyLabel={t("noRefine")}
                    />
                  </>
                )}
              </div>

              <div className="rounded-lg border border-border bg-surface-secondary/30 p-3">
                <h5 className="text-sm font-semibold text-foreground">{t("statusTitle")}</h5>
                <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
                  <label className="min-w-0 text-xs text-muted">
                    <span>{t("effectiveRevision")}</span>
                    <input
                      aria-label={t("effectiveRevision")}
                      readOnly
                      value={selectedStatus?.effective_revision || ""}
                      placeholder={t("revisionUnavailable")}
                      className="mt-1 w-full rounded-lg border border-border bg-surface px-3 py-2 font-mono text-xs text-foreground"
                    />
                  </label>
                  <div className="min-w-0 text-xs text-muted">
                    <div>{t("qualityStatus")}</div>
                    <div className="mt-1 rounded-lg border border-border bg-surface px-3 py-2 text-sm text-foreground">
                      {t(`quality.${selectedStatus?.quality_status || "experimental"}`)}
                    </div>
                  </div>
                </div>
                {selectedStatus?.quality_status === "drifted" && selectedStatus.issue && (
                  <p className="mt-3 break-words text-xs text-red-600 dark:text-red-300">
                    {t("driftedReason")}: {selectedStatus.issue}
                  </p>
                )}
                {hasUnsavedChanges && (
                  <p className="mt-3 text-xs text-amber-700 dark:text-amber-200">
                    {t("draftStatus")}
                  </p>
                )}
                <p className="mt-3 text-xs text-muted">{t("experimentalWarning")}</p>
              </div>

              {confirmDelete && (
                <div className="rounded-lg border border-red-300 bg-red-50 p-3 text-sm text-red-700 dark:border-red-800 dark:bg-red-950/30 dark:text-red-300">
                  <p>{t("deleteConfirm", { alias: activeAlias })}</p>
                  <div className="mt-3 flex flex-wrap gap-2">
                    <Button size="sm" onPress={removePipeline} className="bg-red-600 text-white">
                      {t("confirmDelete")}
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      onPress={() => setConfirmDelete(false)}
                      className="border-border text-foreground"
                    >
                      {t("cancel")}
                    </Button>
                  </div>
                </div>
              )}
            </div>
          ) : (
            <div className="flex min-h-44 items-center justify-center text-sm text-muted">
              {t("empty")}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

function ProviderSelect({
  label,
  value,
  options,
  onChange,
  missingLabel,
  optional = false,
  emptyLabel = "",
}: {
  label: string;
  value: string;
  options: string[];
  onChange: (value: string) => void;
  missingLabel: string;
  optional?: boolean;
  emptyLabel?: string;
}) {
  const visibleOptions = value && !options.includes(value) ? [value, ...options] : options;
  return (
    <label className="block min-w-0 text-sm text-muted">
      <span>{label}</span>
      <select
        aria-label={label}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="mt-1 w-full rounded-lg border border-border bg-surface px-3 py-2 text-sm text-foreground"
      >
        {optional && <option value="">{emptyLabel}</option>}
        {visibleOptions.map((alias) => (
          <option key={alias} value={alias}>
            {alias}{options.includes(alias) ? "" : ` (${missingLabel})`}
          </option>
        ))}
      </select>
    </label>
  );
}
