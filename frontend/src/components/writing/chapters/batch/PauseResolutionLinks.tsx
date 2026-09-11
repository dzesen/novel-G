"use client";

import Link from "next/link";
import { useLocale, useTranslations } from "next-intl";
import type { GenerationDiagnostic, GenerationJob, ReadinessIssue } from "./batchTypes";
import { pauseDestinations, pauseDestinationSearch, readinessDestinations } from "./pauseResolution";

export default function PauseResolutionLinks({ job, diagnostic, omitChapter = false, issue, onNavigate }: {
  job: GenerationJob;
  omitChapter?: boolean;
  issue?: ReadinessIssue;
  onNavigate?: () => void;
  diagnostic?: GenerationDiagnostic | null;
}) {
  const locale = useLocale();
  const t = useTranslations("writing.batch.pauseLinks");
  const destinations = (issue ? readinessDestinations(issue) : pauseDestinations(job, diagnostic)).filter((item) => !omitChapter || item !== "chapter");
  if (!destinations.length) return null;
  const chapterIds = issue?.details.chapter_ids;
  const chapterId = Array.isArray(chapterIds) ? chapterIds.find((value) => typeof value === "string" && value) : undefined;
  const targetJob = chapterId ? { ...job, current_chapter_id: chapterId, error: null } : job;
  return (
    <nav aria-label={t("label")} className="flex min-w-0 flex-wrap gap-2">
      {destinations.map((destination) => (
        <Link key={destination} onClick={onNavigate}
          href={destination === "settings" ? `/${locale}/settings`
            : `/${locale}/writing/${encodeURIComponent(job.novel_id)}?${pauseDestinationSearch(destination, targetJob, diagnostic)}`}
          className="inline-flex min-h-10 max-w-full items-center rounded-md border border-accent/40 bg-background px-3 py-2 text-sm font-semibold text-accent hover:bg-accent/10 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
        >{t(destination)}</Link>
      ))}
    </nav>
  );
}
