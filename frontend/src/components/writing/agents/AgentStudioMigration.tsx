"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";

import type { WritingArea, WritingView } from "@/lib/writingRoute";

interface AgentStudioMigrationProps {
  locale: "en" | "zh";
  onNavigate: (area: WritingArea, view: WritingView) => void;
}

const destinationClass =
  "min-h-11 rounded-md border border-border bg-surface px-4 py-2.5 text-left text-sm font-semibold text-foreground transition-colors hover:border-accent hover:text-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent";

export default function AgentStudioMigration({
  locale,
  onNavigate,
}: AgentStudioMigrationProps) {
  const t = useTranslations("writing.agentStudio.migration");

  return (
    <div className="h-full overflow-y-auto bg-background px-4 py-8 sm:px-6">
      <section className="mx-auto max-w-3xl border-y border-border py-7 sm:py-10">
        <p className="text-xs font-semibold uppercase tracking-[0.14em] text-accent">
          {t("management")}
        </p>
        <h1 className="mt-2 text-2xl font-semibold tracking-tight text-foreground">
          {t("title")}
        </h1>
        <p className="mt-3 max-w-2xl text-sm leading-6 text-muted">
          {t("description")}
        </p>

        <div className="mt-6 grid gap-3 sm:grid-cols-2">
          <button
            type="button"
            className={destinationClass}
            onClick={() => onNavigate("blueprint", "inspiration")}
          >
            {t("creative")}
          </button>
          <button
            type="button"
            className={destinationClass}
            onClick={() => onNavigate("continuity", "reviews")}
          >
            {t("reviews")}
          </button>
          <button
            type="button"
            className={destinationClass}
            onClick={() => onNavigate("auto-book", "retrospective")}
          >
            {t("retrospective")}
          </button>
          <button
            type="button"
            className={destinationClass}
            onClick={() => onNavigate("continuity", "review-history")}
          >
            {t("history")}
          </button>
          <Link
            href={`/${locale}/settings?section=generation-roles`}
            className={`${destinationClass} flex items-center sm:col-span-2`}
          >
            {t("management")}
          </Link>
        </div>

        <p className="mt-6 rounded-md bg-surface-secondary px-4 py-3 text-sm leading-6 text-muted">
          {t("visual")}
        </p>
      </section>
    </div>
  );
}
