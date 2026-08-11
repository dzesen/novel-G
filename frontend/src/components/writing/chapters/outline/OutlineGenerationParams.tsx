"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import {
  OptionalNumberParam,
  OptionalSliderParam,
  OptionalTextParam,
  SwitchParam,
} from "@/components/shared/OptionalParamControls";

/** 多个创作端点共用的请求级生成参数；采样参数为 null 时不进请求体。 */
export interface GenerationParams {
  temperature: number | null;
  top_p: number | null;
  max_tokens: number | null;
  presence_penalty: number | null;
  frequency_penalty: number | null;
  system_prompt: string | null;
  allow_failure_retry: boolean;
}

export const EMPTY_GENERATION_PARAMS: GenerationParams = {
  temperature: null,
  top_p: null,
  max_tokens: null,
  presence_penalty: null,
  frequency_penalty: null,
  system_prompt: null,
  allow_failure_retry: true,
};

/** 把已启用的参数摊平进请求体；未启用的整个键都不出现，由后端用配置默认值。 */
export function toRequestParams(params: GenerationParams): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(params)) {
    if (value !== null) result[key] = value;
  }
  return result;
}

export default function OutlineGenerationParams({
  value,
  onChange,
  maxTokensLimit = 200_000,
  maxTokensEnableValue = 4_096,
  maxTokensStep = 256,
}: {
  value: GenerationParams;
  onChange: (next: GenerationParams) => void;
  maxTokensLimit?: number;
  maxTokensEnableValue?: number;
  maxTokensStep?: number;
}) {
  const t = useTranslations("writing.outline");
  const [open, setOpen] = useState(false);

  const set = <K extends keyof GenerationParams>(key: K, next: GenerationParams[K]) =>
    onChange({ ...value, [key]: next });

  return (
    <div className="rounded-md border border-border bg-background p-3">
      <button
        type="button"
        onClick={() => setOpen((current) => !current)}
        className="text-xs font-medium text-muted hover:text-foreground"
      >
        {open ? "▾ " : "▸ "}
        {t("paramsToggle")}
      </button>
      {open && (
        <div className="mt-3 grid gap-3">
          <OptionalSliderParam
            label={t("paramTemperature")}
            value={value.temperature}
            min={0}
            max={2}
            step={0.1}
            onToggle={(enabled) => set("temperature", enabled ? 0.8 : null)}
            onValueChange={(v) => set("temperature", v)}
          />
          <OptionalSliderParam
            label={t("paramTopP")}
            value={value.top_p}
            min={0}
            max={1}
            step={0.05}
            onToggle={(enabled) => set("top_p", enabled ? 0.9 : null)}
            onValueChange={(v) => set("top_p", v)}
          />
          <OptionalNumberParam
            label={t("paramMaxTokens")}
            value={value.max_tokens}
            min={1}
            max={maxTokensLimit}
            step={maxTokensStep}
            onToggle={(enabled) =>
              set("max_tokens", enabled ? maxTokensEnableValue : null)
            }
            onValueChange={(v) => set("max_tokens", v)}
          />
          <SwitchParam
            label={t("paramAllowFailureRetry")}
            description={t("paramAllowFailureRetryHint")}
            value={value.allow_failure_retry}
            onChange={(enabled) => set("allow_failure_retry", enabled)}
          />
          <OptionalSliderParam
            label={t("paramPresencePenalty")}
            value={value.presence_penalty}
            min={-2}
            max={2}
            step={0.1}
            onToggle={(enabled) => set("presence_penalty", enabled ? 0 : null)}
            onValueChange={(v) => set("presence_penalty", v)}
          />
          <OptionalSliderParam
            label={t("paramFrequencyPenalty")}
            value={value.frequency_penalty}
            min={-2}
            max={2}
            step={0.1}
            onToggle={(enabled) => set("frequency_penalty", enabled ? 0 : null)}
            onValueChange={(v) => set("frequency_penalty", v)}
          />
          <OptionalTextParam
            label={t("paramSystemPrompt")}
            value={value.system_prompt}
            placeholder={t("paramSystemPromptPlaceholder")}
            onToggle={(enabled) => set("system_prompt", enabled ? "" : null)}
            onValueChange={(v) => set("system_prompt", v)}
          />
        </div>
      )}
    </div>
  );
}
