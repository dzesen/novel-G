"use client";

import { useTranslations } from "next-intl";
import AuthorConstraintsFields from "@/components/shared/AuthorConstraintsFields";
import type { AuthorInput } from "@/types/novel";

export default function AuthorInputFields({ value, isEditing, onChange }: {
  value?: AuthorInput | null;
  isEditing: boolean;
  onChange: (value: AuthorInput) => void;
}) {
  const t = useTranslations("authorInput");
  if (!value) return null;
  return (
    <details className="border-t border-border pt-3">
      <summary className="cursor-pointer text-sm font-semibold text-foreground focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-primary">{t("title")}</summary>
      <div className="mt-3 space-y-4">
        <p className="text-xs leading-5 text-muted">{t("description")}</p>
        <div className="space-y-1.5">
          {isEditing ? (
            <>
              <label htmlFor="blueprint-original-idea" className="block text-sm font-medium text-foreground">{t("originalIdea")}</label>
              <textarea
                id="blueprint-original-idea"
                required maxLength={8000} rows={4}
                className="w-full resize-y rounded-lg border border-border bg-background px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary"
                value={value.original_idea}
                onChange={(event) => onChange({ ...value, original_idea: event.target.value })}
              />
            </>
          ) : (
            <>
              <p className="text-sm font-medium text-foreground">{t("originalIdea")}</p>
              <p className="whitespace-pre-wrap break-words text-sm leading-6 text-muted">{value.original_idea}</p>
            </>
          )}
        </div>
        {value.creative_direction && (
          <div className="space-y-1.5">
            <p className="text-sm font-medium text-foreground">{t("confirmedDirection")}</p>
            <p className="whitespace-pre-wrap break-words text-sm leading-6 text-muted">
              {[value.creative_direction.direction.title, ...value.creative_direction.direction.must_keep, value.creative_direction.user_adjustments].filter(Boolean).join("\n")}
            </p>
          </div>
        )}
        <AuthorConstraintsFields
          idPrefix="blueprint-author" value={value.constraints}
          readOnly={!isEditing}
          onChange={(constraints) => onChange({ ...value, constraints })}
        />
      </div>
    </details>
  );
}
