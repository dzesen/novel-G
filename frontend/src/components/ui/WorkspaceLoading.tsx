"use client";

import { useTranslations } from "next-intl";

export function WorkspaceLoading() {
  const t = useTranslations("writing.navigation");
  return (
    <div className="grid h-full min-h-0 place-items-center bg-background px-6" role="status" aria-live="polite">
      <div className="flex items-center gap-3 text-sm text-muted">
        <span className="h-2.5 w-2.5 animate-pulse rounded-full bg-accent" />
        {t("loadingWorkspace")}
      </div>
    </div>
  );
}
