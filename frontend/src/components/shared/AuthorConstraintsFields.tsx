"use client";

import { useTranslations } from "next-intl";
import { AUTHOR_CONSTRAINT_KEYS, normalizeAuthorConstraints } from "@/lib/authorInput";
import type { AuthorConstraints } from "@/types/novel";

export default function AuthorConstraintsFields({
  value, onChange, idPrefix, disabled = false, readOnly = false,
}: {
  value: AuthorConstraints;
  onChange?: (value: AuthorConstraints) => void;
  idPrefix: string;
  disabled?: boolean;
  readOnly?: boolean;
}) {
  const t = useTranslations("authorInput");
  return (
    <div className="space-y-3">
      {!readOnly && <p id={`${idPrefix}-hint`} className="text-xs leading-5 text-muted">{t("constraintsHint")}</p>}
      {AUTHOR_CONSTRAINT_KEYS.map((key) => (
        <div key={key} className="min-w-0 space-y-1.5">
          {readOnly ? (
            <>
              <p className="text-sm font-medium text-foreground">{t(key)}</p>
              <p className="whitespace-pre-wrap break-words text-sm leading-6 text-muted">{value[key].join("\n") || t("none")}</p>
            </>
          ) : (
            <>
              <label htmlFor={`${idPrefix}-${key}`} className="block text-sm font-medium text-foreground">{t(key)}</label>
              <textarea
                id={`${idPrefix}-${key}`}
                rows={2}
                maxLength={10019}
                value={value[key].join("\n")}
                disabled={disabled}
                aria-describedby={`${idPrefix}-hint`}
                className="w-full resize-y rounded-lg border border-border bg-background px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary"
                onChange={(event) => {
                  const next = { ...value, [key]: event.target.value.split("\n") };
                  event.target.setCustomValidity(normalizeAuthorConstraints({ [key]: next[key] }) ? "" : t("constraintsInvalid"));
                  onChange?.(next);
                }}
              />
            </>
          )}
        </div>
      ))}
    </div>
  );
}
