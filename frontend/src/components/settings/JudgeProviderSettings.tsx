"use client";

import { useTranslations } from "next-intl";
import { useId } from "react";
import { Button, Card } from "@heroui/react";
import type { AppConfig } from "@/types/config";
import { getProviderAliasesForSelection } from "@/types/config";
import { resolveJudgeSettings, supportsKimiThinkingSetting, updateJudgeStep } from "./judgeProviderConfig";

interface Props {
  config: AppConfig;
  onChange: (config: AppConfig) => void;
  onEditProvider: (alias: string) => void;
}

const controlClass = "min-h-11 w-full min-w-0 rounded-lg border border-border bg-background px-3 py-2 text-sm text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent";

export function JudgeProviderSettings({ config, onChange, onEditProvider }: Props) {
  const t = useTranslations("settings.judge");
  const fieldId = useId();
  const settings = resolveJudgeSettings(config);
  const { provider, providerAlias } = settings;
  const aliases = getProviderAliasesForSelection(config.llm.providers, settings.overrideAlias);
  const missingOverride = settings.overrideAlias && !config.llm.providers[settings.overrideAlias];
  const setThinking = (value: string) => {
    if (!provider) return;
    onChange({
      ...config,
      llm: {
        ...config.llm,
        providers: {
          ...config.llm.providers,
          [providerAlias]: { ...provider, thinking_mode: value === "" ? null : value as "enabled" | "disabled" },
        },
      },
    });
  };
  return (
    <Card className="min-w-0 border border-border bg-surface shadow-sm" data-testid="judge-provider-settings">
      <Card.Header className="flex-col items-start gap-2">
        <Card.Title className="flex flex-wrap items-center gap-2 text-lg font-semibold text-foreground">{t("title")}<span className="rounded-md bg-surface-secondary px-2 py-1 text-xs font-medium text-muted">{t("experimental")}</span></Card.Title>
        <p className="max-w-3xl text-sm leading-6 text-muted">{t("description")}</p>
      </Card.Header>
      <Card.Content className="grid min-w-0 gap-5">
        <div className="grid min-w-0 gap-4 md:grid-cols-2">
          <label className="grid min-w-0 content-start gap-2 text-sm font-medium">
            <span id={`${fieldId}-provider-label`}>{t("provider")}</span>
            <select className={controlClass} value={settings.overrideAlias} aria-labelledby={`${fieldId}-provider-label`}
              onChange={(event) => onChange(updateJudgeStep(config, { provider: event.target.value }))}>
              <option value="">{t("inherit", { provider: settings.inheritedAlias || t("notConfigured") })}</option>
              {missingOverride && <option value={settings.overrideAlias}>{t("missingOption", { provider: settings.overrideAlias })}</option>}
              {aliases.map((alias) => (
                <option key={alias} value={alias} disabled={!config.llm.providers[alias].enabled}>
                  {alias}{config.llm.providers[alias].enabled ? "" : ` (${t("disabled")})`}
                </option>
              ))}
            </select>
          </label>
          <label className="grid min-w-0 content-start gap-2 text-sm font-medium">
            <span id={`${fieldId}-timeout-label`}>{t("timeout")}</span>
            <input type="number" min={1} step={1} className={controlClass}
              aria-labelledby={`${fieldId}-timeout-label`} aria-describedby={`${fieldId}-timeout-hint`}
              value={settings.timeoutOverride ?? ""}
              placeholder={provider ? String(provider.timeout_seconds) : ""}
              onChange={(event) => {
                const value = event.target.value;
                const parsed = Number(value);
                onChange(updateJudgeStep(config, {
                  timeout_seconds: value && Number.isFinite(parsed) ? Math.max(1, Math.floor(parsed)) : null,
                }));
              }} />
            <span id={`${fieldId}-timeout-hint`} className="text-xs font-normal leading-5 text-muted">{t("timeoutHint")}</span>
          </label>
        </div>
        {!provider || !provider.enabled ? (
          <p role="alert" className="text-sm leading-6 text-red-700 dark:text-red-300">{t(provider ? "providerDisabled" : "providerMissing")}</p>
        ) : settings.sameAsDefaultWriter ? (
          <p role="alert" className="text-sm leading-6 text-red-700 dark:text-red-300">{t("sameWriter")}</p>
        ) : null}
        <dl className="grid min-w-0 gap-4 border-y border-border py-4 sm:grid-cols-3">
          <div className="min-w-0"><dt><label htmlFor={`${fieldId}-model`} className="text-xs text-muted">{t("model")}</label></dt><dd className="mt-1"><input id={`${fieldId}-model`} className={controlClass} value={provider?.default_model ?? ""} disabled={!provider} aria-describedby={`${fieldId}-model-hint`} onChange={(event) => {
            if (!provider) return;
            onChange({ ...config, llm: { ...config.llm, providers: { ...config.llm.providers, [providerAlias]: { ...provider, default_model: event.target.value } } } });
          }} /></dd></div>
          <div className="min-w-0"><dt className="text-xs text-muted">{t("effectiveTimeout")}</dt><dd className="mt-1 text-sm tabular-nums">{settings.effectiveTimeout == null ? t("notConfigured") : t("seconds", { count: settings.effectiveTimeout })}</dd></div>
          <div className="min-w-0"><dt className="text-xs text-muted">{t("outputLimit")}</dt><dd className="mt-1 text-sm tabular-nums">{provider?.max_tokens == null ? t("providerDefault") : provider.max_tokens.toLocaleString()}</dd></div>
        </dl>
        <p id={`${fieldId}-model-hint`} className="break-words text-xs leading-5 text-muted">{t("modelBindingHint", { provider: providerAlias || t("notConfigured") })}</p>
        {supportsKimiThinkingSetting(provider) && (
          <label className="grid min-w-0 max-w-xl gap-2 text-sm font-medium">
            <span id={`${fieldId}-thinking-label`}>{t("thinking")}</span>
            <select className={controlClass} value={provider?.thinking_mode ?? ""} onChange={(event) => setThinking(event.target.value)}
              aria-labelledby={`${fieldId}-thinking-label`} aria-describedby={`${fieldId}-thinking-hint`}>
              <option value="">{t("thinkingDefault")}</option>
              <option value="disabled">{t("thinkingDisabled")}</option>
              <option value="enabled">{t("thinkingEnabled")}</option>
            </select>
            <span id={`${fieldId}-thinking-hint`} className="text-xs font-normal leading-5 text-muted">{t("sharedProviderHint")}</span>
          </label>
        )}
        <div className="flex flex-wrap items-center gap-3">
          <Button variant="secondary" isDisabled={!provider} onPress={() => onEditProvider(providerAlias)}>{t("editProvider")}</Button>
          <p className="max-w-2xl text-xs leading-5 text-muted">{t("saveHint")}</p>
        </div>
      </Card.Content>
    </Card>
  );
}
