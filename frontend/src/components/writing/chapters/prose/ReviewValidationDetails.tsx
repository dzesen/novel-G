"use client";

import { useTranslations } from "next-intl";
import { reviewValidationErrorKey, reviewValidationGroups } from "./reviewValidationPresentation";

export default function ReviewValidationDetails({ diagnostics }: { diagnostics: unknown }) {
  const t = useTranslations("writing.prose");
  const groups = reviewValidationGroups(diagnostics);
  if (!groups.length) return <p>{t("completionValidationNotRecorded")}</p>;
  const phaseKeys = {
    primary: "completionValidationPrimary",
    repair: "completionValidationRepair",
    validation: "completionValidationLocal",
  } as const;
  return (
    <details className="min-w-0" data-testid="review-validation-details">
      <summary className="cursor-pointer font-medium">{t("completionValidationDetails")}</summary>
      <div className="mt-2 grid max-h-64 min-w-0 gap-3 overflow-y-auto">
        {groups.map((group) => (
          <section className="min-w-0" key={group.phase}>
            <p className="font-medium">{t(phaseKeys[group.phase])}</p>
            <ul className="mt-1 grid min-w-0 gap-2">
              {group.issues.map((issue, index) => (
                <li className="min-w-0" key={`${issue.path}-${issue.errorType}-${index}`}>
                  <p>{t(reviewValidationErrorKey(issue.errorType))}</p>
                  <code className="block break-all text-muted-foreground">
                    {issue.path} · {issue.errorType}
                  </code>
                </li>
              ))}
            </ul>
            {group.truncated && <p>{t("completionValidationTruncated")}</p>}
          </section>
        ))}
      </div>
    </details>
  );
}
