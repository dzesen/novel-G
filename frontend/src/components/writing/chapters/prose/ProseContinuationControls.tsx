"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import {
  MAX_AUTOMATIC_CONTINUATIONS_PER_SCENE,
  MAX_CONTINUATION_TARGET_WORDS,
  MIN_AUTOMATIC_CONTINUATIONS_PER_SCENE,
  MIN_CONTINUATION_TARGET_WORDS,
  permitsAutomaticContinuation,
  type ProseContinuationPolicy,
} from "./proseContinuation";

interface ProseContinuationControlsProps {
  idPrefix: string;
  value: ProseContinuationPolicy;
  onChange: (value: ProseContinuationPolicy) => void;
  disabled?: boolean;
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, Math.round(value)));
}

export default function ProseContinuationControls({
  idPrefix,
  value,
  onChange,
  disabled = false,
}: ProseContinuationControlsProps) {
  const t = useTranslations("writing.prose");
  const [automaticInput, setAutomaticInput] = useState(
    String(value.automatic_continuations_per_scene),
  );
  const [targetInput, setTargetInput] = useState(
    String(value.continuation_target_words),
  );

  const updateAutomatic = (raw: string) => {
    setAutomaticInput(raw);
    const parsed = Number(raw);
    if (!Number.isFinite(parsed)) return;
    const next = clamp(
      parsed,
      MIN_AUTOMATIC_CONTINUATIONS_PER_SCENE,
      MAX_AUTOMATIC_CONTINUATIONS_PER_SCENE,
    );
    if (String(next) === raw) {
      onChange({ ...value, automatic_continuations_per_scene: next });
    }
  };

  const updateTarget = (raw: string) => {
    setTargetInput(raw);
    const parsed = Number(raw);
    if (!Number.isFinite(parsed)) return;
    const next = clamp(
      parsed,
      MIN_CONTINUATION_TARGET_WORDS,
      MAX_CONTINUATION_TARGET_WORDS,
    );
    if (String(next) === raw) {
      onChange({ ...value, continuation_target_words: next });
    }
  };

  const commitAutomatic = () => {
    const parsed = Number(automaticInput);
    const next = Number.isFinite(parsed)
      ? clamp(
          parsed,
          MIN_AUTOMATIC_CONTINUATIONS_PER_SCENE,
          MAX_AUTOMATIC_CONTINUATIONS_PER_SCENE,
        )
      : value.automatic_continuations_per_scene;
    setAutomaticInput(String(next));
    onChange({ ...value, automatic_continuations_per_scene: next });
  };

  const commitTarget = () => {
    const parsed = Number(targetInput);
    const next = Number.isFinite(parsed)
      ? clamp(
          parsed,
          MIN_CONTINUATION_TARGET_WORDS,
          MAX_CONTINUATION_TARGET_WORDS,
        )
      : value.continuation_target_words;
    setTargetInput(String(next));
    onChange({ ...value, continuation_target_words: next });
  };

  return (
    <section
      aria-labelledby={`${idPrefix}-continuation-title`}
      className="grid gap-3 rounded-md border border-border bg-background p-3"
    >
      <div>
        <h4
          id={`${idPrefix}-continuation-title`}
          className="text-sm font-semibold text-foreground"
        >
          {t("continuationSettingsTitle")}
        </h4>
        <p className="mt-1 text-xs leading-5 text-muted">
          {t("continuationSettingsDescription")}
        </p>
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <label className="grid gap-1 text-sm">
          <span className="text-xs font-medium text-muted">
            {t("continuationAutomaticLabel")}
          </span>
          <input
            id={`${idPrefix}-automatic-continuations`}
            type="number"
            min={MIN_AUTOMATIC_CONTINUATIONS_PER_SCENE}
            max={MAX_AUTOMATIC_CONTINUATIONS_PER_SCENE}
            step={1}
            inputMode="numeric"
            value={automaticInput}
            disabled={disabled}
            onChange={(event) => updateAutomatic(event.target.value)}
            onBlur={commitAutomatic}
            className="min-h-9 w-full rounded-md border border-border bg-surface px-3 py-2 text-sm text-foreground outline-none focus:border-accent disabled:cursor-not-allowed disabled:opacity-60"
          />
          <span className="text-xs leading-5 text-muted">
            {t("continuationAutomaticHint", {
              minimum: MIN_AUTOMATIC_CONTINUATIONS_PER_SCENE,
              maximum: MAX_AUTOMATIC_CONTINUATIONS_PER_SCENE,
            })}
          </span>
        </label>

        <label className="grid gap-1 text-sm">
          <span className="text-xs font-medium text-muted">
            {t("continuationTargetLabel")}
          </span>
          <input
            id={`${idPrefix}-continuation-target`}
            type="number"
            min={MIN_CONTINUATION_TARGET_WORDS}
            max={MAX_CONTINUATION_TARGET_WORDS}
            step={100}
            inputMode="numeric"
            value={targetInput}
            disabled={disabled}
            onChange={(event) => updateTarget(event.target.value)}
            onBlur={commitTarget}
            className="min-h-9 w-full rounded-md border border-border bg-surface px-3 py-2 text-sm text-foreground outline-none focus:border-accent disabled:cursor-not-allowed disabled:opacity-60"
          />
          <span className="text-xs leading-5 text-muted">
            {t("continuationTargetHint", {
              minimum: MIN_CONTINUATION_TARGET_WORDS,
              maximum: MAX_CONTINUATION_TARGET_WORDS,
            })}
          </span>
        </label>
      </div>

      {permitsAutomaticContinuation(value) && (
        <p
          role="note"
          className="border-t border-amber-200 pt-3 text-xs leading-5 text-amber-800 dark:border-amber-900/60 dark:text-amber-200"
        >
          {t("continuationAutomaticWarning")}
        </p>
      )}
    </section>
  );
}
