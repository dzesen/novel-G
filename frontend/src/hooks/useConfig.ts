"use client";

import { useState, useCallback, useEffect } from "react";
import { apiGet, apiPatch, apiPost } from "@/lib/api";
import {
  buildConfigPatch,
  normalizeAppConfig,
  type AppConfig,
  type ConfigChangePreview,
  type ConfigView,
  type DeleteProviderCommand,
  type ImagePipelineStatusView,
  type ProviderCommand,
  type RenameProviderCommand,
  type WorkflowDefinition,
} from "@/types/config";

function fingerprintImageProviders(config: AppConfig): string {
  return JSON.stringify(config.image_providers);
}

interface ConfigState {
  config: AppConfig | null;
  workflowCatalog: WorkflowDefinition[];
  revision: string;
  imagePipelineStatuses: ImagePipelineStatusView[];
  savedImageProvidersFingerprint: string;
  loading: boolean;
  saving: boolean;
  error: string | null;
  success: string | null;
}

interface SaveConfigOptions {
  confirmProviderDeletion: (preview: ConfigChangePreview) => boolean | Promise<boolean>;
  missingConfirmationTokenMessage: string;
}

export function useConfig(enabled = true) {
  const [pendingProviderCommands, setPendingProviderCommands] = useState<ProviderCommand[]>([]);
  const [state, setState] = useState<ConfigState>({
    config: null, workflowCatalog: [], revision: "", imagePipelineStatuses: [],
    loading: true, saving: false,
    savedImageProvidersFingerprint: "",
    error: null, success: null,
  });

  const clearMessages = useCallback(() => {
    setState((current) => ({ ...current, error: null, success: null }));
  }, []);

  const fetchConfig = useCallback(async () => {
    if (!enabled) return null;
    setState((current) => ({ ...current, loading: true, error: null }));
    const catalogPromise = apiGet<WorkflowDefinition[]>("/api/config/workflows").catch(() => []);
    try {
      const view = await apiGet<ConfigView>("/api/config");
      const catalog = await catalogPromise;
      const normalized = normalizeAppConfig(view.editable_data, catalog);
      setPendingProviderCommands([]);
      setState((current) => ({
        ...current, config: normalized, workflowCatalog: catalog,
        revision: view.revision,
        imagePipelineStatuses: view.image_pipeline_statuses || [], loading: false,
        savedImageProvidersFingerprint: fingerprintImageProviders(normalized),
      }));
      return normalized;
    } catch (error) {
      setState((current) => ({
        ...current, loading: false,
        error: error instanceof Error ? error.message : "Unknown error",
      }));
      return null;
    }
  }, [enabled]);

  const saveConfig = useCallback(async (
    data: AppConfig,
    successMsg: string,
    options: SaveConfigOptions,
  ) => {
    setState((current) => ({ ...current, saving: true, error: null, success: null }));
    try {
      const payload = buildConfigPatch(data, state.revision, pendingProviderCommands);
      if (pendingProviderCommands.some((command) => command.kind === "delete")) {
        const preview = await apiPost<ConfigChangePreview>("/api/config/preview", payload);
        const confirmed = await options.confirmProviderDeletion(preview);
        if (!confirmed) {
          setState((current) => ({ ...current, saving: false }));
          return false;
        }
        if (!preview.confirmation_token) throw new Error(options.missingConfirmationTokenMessage);
        payload.confirmation_token = preview.confirmation_token;
      }

      const view = await apiPatch<ConfigView>("/api/config", payload);
      const normalized = normalizeAppConfig(view.editable_data, state.workflowCatalog);
      setPendingProviderCommands([]);
      setState((current) => ({
        ...current, config: normalized, revision: view.revision,
        imagePipelineStatuses: view.image_pipeline_statuses || [],
        savedImageProvidersFingerprint: fingerprintImageProviders(normalized),
        saving: false, success: successMsg,
      }));
      return true;
    } catch (error) {
      setState((current) => ({
        ...current, saving: false,
        error: error instanceof Error ? error.message : "Unknown error",
      }));
      return false;
    }
  }, [pendingProviderCommands, state.revision, state.workflowCatalog]);

  useEffect(() => {
    if (enabled) void fetchConfig();
  }, [enabled, fetchConfig]);

  return {
    ...state,
    loading: enabled && state.loading,
    imageProvidersDirty: state.config !== null && (
      fingerprintImageProviders(state.config) !== state.savedImageProvidersFingerprint
    ),
    fetchConfig,
    saveConfig,
    queueProviderRename: (rename: RenameProviderCommand) =>
      setPendingProviderCommands((current) => [...current, rename]),
    queueProviderDelete: (command: DeleteProviderCommand) =>
      setPendingProviderCommands((current) => [...current, command]),
    setConfig: (config: AppConfig) => setState((current) => ({ ...current, config })),
    clearMessages,
  };
}
