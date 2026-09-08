"use client";

import { useTranslations } from "next-intl";
import type { ReviewEnforcement } from "./chapterReviewPolicy";

interface Props {
  id: string;
  value: ReviewEnforcement;
  onChange: (value: ReviewEnforcement) => void;
  disabled?: boolean;
  automaticRepair?: boolean;
}

export default function ReviewEnforcementControl({
  id, value, onChange, disabled = false, automaticRepair = false,
}: Props) {
  const t = useTranslations("writing.batch");
  return (
    <div className="grid min-w-0 gap-2 py-2">
      <label htmlFor={id} className="text-xs text-warm-700 dark:text-muted">{t("reviewEnforcementLabel")}</label>
      <select
        id={id}
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value as ReviewEnforcement)}
        aria-describedby={`${id}-hint`}
        className="min-h-11 w-full min-w-0 rounded-md border border-border bg-surface px-3 py-2 text-base text-foreground focus-visible:outline-2 focus-visible:outline-accent sm:text-sm"
      >
        <option value="advisory">{t("reviewEnforcementAdvisory")}</option>
        <option value="strict">{t("reviewEnforcementStrict")}</option>
      </select>
      <p id={`${id}-hint`} className="text-xs leading-5 text-warm-700 dark:text-muted">
        {t(value === "advisory" ? "reviewEnforcementAdvisoryHint"
          : automaticRepair ? "reviewEnforcementStrictAutomaticHint" : "reviewEnforcementStrictManualHint")}
      </p>
    </div>
  );
}
