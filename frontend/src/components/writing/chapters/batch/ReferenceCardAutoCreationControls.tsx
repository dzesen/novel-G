"use client";

import { useTranslations } from "next-intl";
import {
  REFERENCE_CARD_AUTO_CREATION_HARD_LIMITS,
  REFERENCE_CARD_TYPES,
  type ReferenceCardAutoCreationPolicy,
  type ReferenceCardType,
} from "./referenceCardAutoCreation";
import { referenceCardTypeTranslationKey } from "./referenceCardAutoCreationPresentation";

interface ReferenceCardAutoCreationControlsProps {
  value: ReferenceCardAutoCreationPolicy;
  onChange: (value: ReferenceCardAutoCreationPolicy) => void;
  disabled?: boolean;
}

function clampInteger(value: string, minimum: number, maximum: number): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return minimum;
  return Math.min(maximum, Math.max(minimum, Math.floor(parsed)));
}

export default function ReferenceCardAutoCreationControls({
  value,
  onChange,
  disabled = false,
}: ReferenceCardAutoCreationControlsProps) {
  const t = useTranslations("writing.batch");
  const labelForType = (cardType: ReferenceCardType) =>
    t(referenceCardTypeTranslationKey(cardType));
  const toggleType = (cardType: ReferenceCardType, selected: boolean) => {
    const selectedTypes = new Set(value.allowed_card_types);
    if (selected) selectedTypes.add(cardType);
    else selectedTypes.delete(cardType);
    if (selectedTypes.size === 0) return;
    onChange({
      ...value,
      allowed_card_types: REFERENCE_CARD_TYPES.filter((item) =>
        selectedTypes.has(item)),
    });
  };

  return (
    <fieldset className="grid min-w-0 gap-3 rounded-md border border-border bg-background p-3 sm:p-4">
      <legend className="px-1 text-xs font-medium text-warm-700 dark:text-muted">
        {t("dialogAutoCardsTitle")}
      </legend>
      <label className="flex min-w-0 cursor-pointer items-start gap-3">
        <input
          type="checkbox"
          checked={value.enabled}
          onChange={(event) => onChange({
            ...value,
            enabled: event.target.checked,
            max_candidate_repair_cycles_per_chapter: event.target.checked
              ? value.max_candidate_repair_cycles_per_chapter
              : 0,
          })}
          disabled={disabled}
          className="mt-0.5 size-4 shrink-0"
        />
        <span className="min-w-0">
          <span className="block text-sm font-medium text-foreground">
            {t("dialogAutoCardsEnable")}
          </span>
          <span className="mt-0.5 block text-xs leading-5 text-warm-700 dark:text-muted">
            {value.enabled
              ? t("dialogAutoCardsEnabledBody")
              : t("dialogAutoCardsDisabledBody")}
          </span>
        </span>
      </label>

      {value.enabled && (
        <div className="grid min-w-0 gap-3 border-t border-border pt-3">
          <div className="grid gap-2">
            <p className="text-xs font-medium text-warm-700 dark:text-muted">
              {t("dialogAutoCardsTypes")}
            </p>
            <div className="flex flex-wrap gap-x-4 gap-y-2" role="group" aria-label={t("dialogAutoCardsTypes")}>
              {REFERENCE_CARD_TYPES.map((cardType) => {
                const checked = value.allowed_card_types.includes(cardType);
                const isLastSelected = checked && value.allowed_card_types.length === 1;
                return (
                  <label key={cardType} className="flex cursor-pointer items-center gap-2 text-sm text-foreground">
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={(event) => toggleType(cardType, event.target.checked)}
                      disabled={disabled || isLastSelected}
                      className="size-4 shrink-0"
                    />
                    <span>{labelForType(cardType)}</span>
                  </label>
                );
              })}
            </div>
            <p className="text-xs leading-5 text-warm-700 dark:text-muted">
              {t("dialogAutoCardsTypesHint")}
            </p>
          </div>

          <div className="grid min-w-0 gap-3 sm:grid-cols-2">
            <label className="grid min-w-0 gap-1 text-sm">
              <span className="text-xs font-medium text-warm-700 dark:text-muted">
                {t("dialogAutoCardsPerChapter")}
              </span>
              <input
                type="number"
                min={0}
                max={REFERENCE_CARD_AUTO_CREATION_HARD_LIMITS.perChapter}
                inputMode="numeric"
                value={value.max_auto_creates_per_chapter}
                onChange={(event) => onChange({
                  ...value,
                  max_auto_creates_per_chapter: clampInteger(
                    event.target.value,
                    0,
                    REFERENCE_CARD_AUTO_CREATION_HARD_LIMITS.perChapter,
                  ),
                })}
                disabled={disabled}
                className="min-h-10 w-full rounded-md border border-border bg-surface px-3 py-2 text-base text-foreground outline-none focus:border-accent disabled:opacity-60 sm:text-sm"
              />
            </label>
            <label className="grid min-w-0 gap-1 text-sm">
              <span className="text-xs font-medium text-warm-700 dark:text-muted">
                {t("dialogAutoCardsPerBook")}
              </span>
              <input
                type="number"
                min={0}
                max={REFERENCE_CARD_AUTO_CREATION_HARD_LIMITS.perBook}
                inputMode="numeric"
                value={value.max_auto_creates_per_book}
                onChange={(event) => onChange({
                  ...value,
                  max_auto_creates_per_book: clampInteger(
                    event.target.value,
                    0,
                    REFERENCE_CARD_AUTO_CREATION_HARD_LIMITS.perBook,
                  ),
                })}
                disabled={disabled}
                className="min-h-10 w-full rounded-md border border-border bg-surface px-3 py-2 text-base text-foreground outline-none focus:border-accent disabled:opacity-60 sm:text-sm"
              />
            </label>
          </div>
          <p className="text-xs leading-5 text-warm-700 dark:text-muted">
            {t("dialogAutoCardsLimitHint", {
              perChapter: REFERENCE_CARD_AUTO_CREATION_HARD_LIMITS.perChapter,
              perBook: REFERENCE_CARD_AUTO_CREATION_HARD_LIMITS.perBook,
            })}
          </p>
          <label className="grid min-w-0 gap-1 text-sm">
            <span className="text-xs font-medium text-warm-700 dark:text-muted">
              {t("dialogAutoCardsRepairLabel")}
            </span>
            <select
              value={value.max_candidate_repair_cycles_per_chapter}
              onChange={(event) => onChange({
                ...value,
                max_candidate_repair_cycles_per_chapter: clampInteger(
                  event.target.value,
                  0,
                  REFERENCE_CARD_AUTO_CREATION_HARD_LIMITS.repairCyclesPerChapter,
                ),
              })}
              disabled={disabled}
              className="min-h-10 w-full rounded-md border border-border bg-surface px-3 py-2 text-base text-foreground outline-none focus:border-accent disabled:opacity-60 sm:text-sm"
            >
              <option value={0}>{t("dialogAutoCardsRepairOff")}</option>
              <option value={1}>{t("dialogAutoCardsRepairOnce")}</option>
              <option value={2}>{t("dialogAutoCardsRepairTwice")}</option>
            </select>
            <span className="text-xs leading-5 text-warm-700 dark:text-muted">
              {t("dialogAutoCardsRepairHint", {
                selected: value.max_candidate_repair_cycles_per_chapter,
                hard: REFERENCE_CARD_AUTO_CREATION_HARD_LIMITS.repairCyclesPerChapter,
              })}
            </span>
          </label>
          <p className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-900 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-200">
            {t("dialogAutoCardsSafety")}
          </p>
        </div>
      )}
    </fieldset>
  );
}
