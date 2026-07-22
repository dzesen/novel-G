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
  type ProviderCommand,
  type RenameProviderCommand,
  type WorkflowDefinition,
} from "@/types/config";

interface ConfigState {
  config: AppConfig | null;
  workflowCatalog: WorkflowDefinition[];
  revision: string;
  loading: boolean;
  saving: boolean;
  error: string | null;
  success: string | null;
}

export function useConfig() {
  const [pendingProviderCommands, setPendingProviderCommands] = useState<ProviderCommand[]>([]);
  const [state, setState] = useState<ConfigState>({
    config: null, workflowCatalog: [], revision: "", loading: true, saving: false,
    error: null, success: null,
  });

  const clearMessages = useCallback(() => {
    setState((current) => ({ ...current, error: null, success: null }));
  }, []);

  const fetchConfig = useCallback(async () => {
    setState((current) => ({ ...current, loading: true, error: null }));
    const catalogPromise = apiGet<WorkflowDefinition[]>("/api/config/workflows").catch(() => []);
    try {
      const view = await apiGet<ConfigView>("/api/config");
      const catalog = await catalogPromise;
      const normalized = normalizeAppConfig(view.editable_data, catalog);
      setPendingProviderCommands([]);
      setState((current) => ({
        ...current, config: normalized, workflowCatalog: catalog,
        revision: view.revision, loading: false,
      }));
      return normalized;
    } catch (error) {
      setState((current) => ({
        ...current, loading: false,
        error: error instanceof Error ? error.message : "Unknown error",
      }));
      return null;
    }
  }, []);

  const saveConfig = useCallback(async (data: AppConfig, successMsg: string) => {
    setState((current) => ({ ...current, saving: true, error: null, success: null }));
    try {
      const payload = buildConfigPatch(data, state.revision, pendingProviderCommands);
      if (pendingProviderCommands.some((command) => command.kind === "delete")) {
        const preview = await apiPost<ConfigChangePreview>("/api/config/preview", payload);
        const paths = preview.reference_changes.map((change) => change.path).join("\n");
        const confirmed = window.confirm(
          `删除 Provider 将同步更新以下引用：\n${paths || "（无引用）"}\n\n确认继续？`,
        );
        if (!confirmed) {
          setState((current) => ({ ...current, saving: false }));
          return false;
        }
        if (!preview.confirmation_token) throw new Error("配置预览未返回确认令牌");
        payload.confirmation_token = preview.confirmation_token;
      }

      const view = await apiPatch<ConfigView>("/api/config", payload);
      const normalized = normalizeAppConfig(view.editable_data, state.workflowCatalog);
      setPendingProviderCommands([]);
      setState((current) => ({
        ...current, config: normalized, revision: view.revision,
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

  useEffect(() => { void fetchConfig(); }, [fetchConfig]);

  return {
    ...state,
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
