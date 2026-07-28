"use client";

import { useId } from "react";
import { useTranslations } from "next-intl";
import {
  STYLE_CONTROL_OPTIONS,
  getCustomStyleNoteLimit,
  hasStyleControls,
  normalizeStyleControls,
} from "@/lib/styleControls";
import type { StyleControls } from "@/types/novel";

interface BoundedStyleControlsProps {
  value: StyleControls;
  isEditing: boolean;
  onChange: (value: StyleControls) => void;
}

const FIELD_KEYS = Object.keys(
  STYLE_CONTROL_OPTIONS,
) as Array<keyof typeof STYLE_CONTROL_OPTIONS>;

export default function BoundedStyleControls({
  value,
  isEditing,
  onChange,
}: BoundedStyleControlsProps) {
  const t = useTranslations("writing.novelInfo.styleControls");
  const noteHintId = useId();
  const noteLimit = getCustomStyleNoteLimit(value);

  const updateField = (field: keyof StyleControls, selected: string) => {
    onChange(
      normalizeStyleControls({
        ...value,
        [field]: selected || undefined,
      }),
    );
  };

  return (
    <div className="mt-5 border-t border-border/50 pt-5 space-y-4">
      <div>
        <h4 className="text-sm font-semibold text-foreground">{t("title")}</h4>
        <p className="mt-1 text-xs leading-5 text-muted">{t("description")}</p>
      </div>

      {isEditing ? (
        <>
          <div className="grid gap-4 sm:grid-cols-2">
            {FIELD_KEYS.map((field) => (
              <label key={field} className="space-y-2">
                <span className="text-sm font-medium text-foreground">
                  {t(`fields.${field}`)}
                </span>
                <select
                  className="w-full rounded-lg border border-border bg-background px-3 py-1.5 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary"
                  value={value[field] ?? ""}
                  onChange={(event) => updateField(field, event.target.value)}
                >
                  <option value="">{t("inherit")}</option>
                  {STYLE_CONTROL_OPTIONS[field].map((option) => (
                    <option key={option} value={option}>
                      {t(`options.${field}.${option}`)}
                    </option>
                  ))}
                </select>
              </label>
            ))}
          </div>

          <label className="block space-y-2">
            <span className="text-sm font-medium text-foreground">
              {t("fields.custom_style_note")}
            </span>
            <textarea
              className="min-h-24 w-full resize-y rounded-lg border border-border bg-background p-3 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary"
              value={value.custom_style_note ?? ""}
              maxLength={noteLimit}
              placeholder={t("notePlaceholder")}
              aria-describedby={noteHintId}
              onChange={(event) =>
                updateField("custom_style_note", event.target.value)
              }
            />
            <span
              id={noteHintId}
              className="flex items-start justify-between gap-3 text-xs text-muted"
            >
              <span className="min-w-0">{t("boundedHint")}</span>
              <span className="shrink-0">
                {t("characterCount", {
                  count: value.custom_style_note?.length ?? 0,
                  limit: noteLimit,
                })}
              </span>
            </span>
          </label>
        </>
      ) : hasStyleControls(value) ? (
        <dl className="grid gap-3 sm:grid-cols-2">
          {FIELD_KEYS.map((field) => {
            const selected = value[field];
            if (!selected) {
              return null;
            }
            return (
              <div key={field} className="space-y-1">
                <dt className="text-xs text-muted">{t(`fields.${field}`)}</dt>
                <dd className="text-sm text-foreground">
                  {t(`options.${field}.${selected}`)}
                </dd>
              </div>
            );
          })}
          {value.custom_style_note ? (
            <div className="space-y-1 sm:col-span-2">
              <dt className="text-xs text-muted">
                {t("fields.custom_style_note")}
              </dt>
              <dd className="break-words whitespace-pre-wrap text-sm text-foreground">
                {value.custom_style_note}
              </dd>
            </div>
          ) : null}
        </dl>
      ) : (
        <p className="text-sm text-muted">{t("empty")}</p>
      )}
    </div>
  );
}
