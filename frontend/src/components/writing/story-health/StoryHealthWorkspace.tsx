"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { apiGet } from "@/lib/api";
import type {
  ChapterWordCountHealth,
  PlotThreadHealth,
  StoryHealthChapterPosition,
  StoryHealthDeviationState,
  StoryHealthDueState,
  StoryHealthReport,
  VolumeWordCountHealth,
} from "@/types/storyHealth";

interface Props {
  mode: "create" | "edit";
  novelId?: string;
}

const DUE_BADGE_CLASSES: Record<StoryHealthDueState, string> = {
  overdue:
    "bg-red-50 text-red-700 dark:bg-red-950/40 dark:text-red-200",
  due: "bg-amber-100 text-amber-800 dark:bg-amber-950/50 dark:text-amber-200",
  upcoming: "bg-surface-secondary text-muted",
  unscheduled: "bg-surface-secondary text-muted",
  unmapped:
    "bg-amber-100 text-amber-800 dark:bg-amber-950/50 dark:text-amber-200",
  not_started: "bg-surface-secondary text-muted",
};

const DEVIATION_BADGE_CLASSES: Record<StoryHealthDeviationState, string> = {
  under:
    "bg-amber-100 text-amber-800 dark:bg-amber-950/50 dark:text-amber-200",
  over: "bg-blue-50 text-blue-700 dark:bg-blue-950/40 dark:text-blue-200",
  within_target:
    "bg-emerald-50 text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-200",
  no_content: "bg-surface-secondary text-muted",
  no_chapters: "bg-surface-secondary text-muted",
};

export default function StoryHealthWorkspace({ novelId }: Props) {
  const t = useTranslations("storyHealth");
  const locale = useLocale();
  const [report, setReport] = useState<StoryHealthReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const numberFormatter = useMemo(
    () => new Intl.NumberFormat(locale),
    [locale],
  );
  const load = useCallback(async () => {
    if (!novelId) return;
    setLoading(true);
    setError(null);
    try {
      setReport(
        await apiGet<StoryHealthReport>(
          `/api/story-health/novel/${novelId}`,
        ),
      );
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("loadError"));
    } finally {
      setLoading(false);
    }
  }, [novelId, t]);

  useEffect(() => {
    void load();
  }, [load]);

  if (!novelId) {
    return <div className="p-6 text-sm text-muted">{t("needNovel")}</div>;
  }

  const positionLabel = (position: StoryHealthChapterPosition | null) => {
    if (!position) return t("unmappedPosition");
    return t("chapterPosition", {
      volume: numberFormatter.format(position.volume_order),
      chapter: numberFormatter.format(position.chapter_order),
      title: position.chapter_title,
    });
  };

  const dueLabel = (thread: PlotThreadHealth) => {
    if (thread.due_state === "overdue") {
      return t("plotThreads.states.overdue", {
        count: numberFormatter.format(thread.overdue_by_chapters ?? 0),
      });
    }
    if (thread.due_state === "upcoming") {
      return t("plotThreads.states.upcoming", {
        count: numberFormatter.format(thread.chapters_until_due ?? 0),
      });
    }
    return t(`plotThreads.states.${thread.due_state}`);
  };

  const writtenChapters =
    report?.word_counts.chapters.filter(
      (chapter) => chapter.actual_word_count > 0,
    ) ?? [];
  const dueCount = report
    ? report.summary.due_plot_thread_count +
      report.summary.overdue_plot_thread_count
    : 0;

  return (
    <div className="h-full overflow-y-auto">
      <header className="border-b border-border bg-surface px-6 py-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="max-w-3xl">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-xl font-semibold tracking-tight text-foreground">
                {t("title")}
              </h2>
              <span className="rounded-full bg-surface-secondary px-2.5 py-1 text-xs font-medium text-muted">
                {t("offlineBadge")}
              </span>
            </div>
            <p className="mt-2 max-w-[72ch] text-sm leading-6 text-muted">
              {t("description")}
            </p>
          </div>
          <button
            type="button"
            onClick={() => void load()}
            disabled={loading}
            className="rounded-md border border-border bg-surface px-3 py-2 text-sm font-medium text-foreground transition-colors hover:bg-surface-secondary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-60"
          >
            {loading ? t("loading") : t("refresh")}
          </button>
        </div>

        {report && (
          <div className="mt-5 flex flex-wrap gap-x-6 gap-y-2 border-t border-dashed border-border pt-4 text-sm">
            <span className="text-foreground">
              {t("summary.dueThreads", {
                count: numberFormatter.format(dueCount),
              })}
            </span>
            <span className="text-foreground">
              {t("summary.absentCharacters", {
                count: numberFormatter.format(
                  report.summary.currently_absent_character_count,
                ),
              })}
            </span>
            <span className="text-foreground">
              {t("summary.wordDeviations", {
                count: numberFormatter.format(
                  report.summary.chapter_word_deviation_count +
                    report.summary.volume_word_deviation_count,
                ),
              })}
            </span>
            <span className="text-muted">
              {report.as_of
                ? t("summary.asOf", {
                    position: positionLabel(report.as_of),
                  })
                : t("summary.noProgress")}
            </span>
          </div>
        )}
      </header>

      <main className="flex flex-col gap-8 p-6">
        {error && (
          <div
            role="alert"
            className="rounded-md border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-200"
          >
            {t("errorWithRecovery", { error })}
          </div>
        )}

        {!report && loading && (
          <div className="py-10 text-center text-sm text-muted">
            {t("loading")}
          </div>
        )}

        {report && (
          <>
            <section aria-labelledby="story-health-threads">
              <div className="mb-3 flex flex-wrap items-end justify-between gap-2">
                <div>
                  <h3
                    id="story-health-threads"
                    className="text-base font-semibold text-foreground"
                  >
                    {t("plotThreads.title")}
                  </h3>
                  <p className="mt-1 text-sm text-muted">
                    {t("plotThreads.description")}
                  </p>
                </div>
                <span className="text-xs text-muted">
                  {t("plotThreads.activeCount", {
                    count: numberFormatter.format(
                      report.summary.active_plot_thread_count,
                    ),
                  })}
                </span>
              </div>

              {report.plot_threads.length === 0 ? (
                <p className="rounded-md border border-dashed border-border px-4 py-6 text-sm text-muted">
                  {t("plotThreads.empty")}
                </p>
              ) : (
                <div className="overflow-x-auto rounded-md border border-border bg-surface">
                  <table className="min-w-[760px] w-full border-collapse text-left text-sm">
                    <thead className="bg-surface-secondary text-xs text-muted">
                      <tr>
                        <th scope="col" className="px-4 py-3 font-medium">
                          {t("plotThreads.columns.name")}
                        </th>
                        <th scope="col" className="px-4 py-3 font-medium">
                          {t("plotThreads.columns.planted")}
                        </th>
                        <th scope="col" className="px-4 py-3 font-medium">
                          {t("plotThreads.columns.age")}
                        </th>
                        <th scope="col" className="px-4 py-3 font-medium">
                          {t("plotThreads.columns.due")}
                        </th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border">
                      {report.plot_threads.map((thread) => (
                        <tr key={thread.thread_id}>
                          <td className="px-4 py-3">
                            <div className="font-medium text-foreground">
                              {thread.name}
                            </div>
                            <div className="mt-0.5 text-xs text-muted">
                              {thread.importance === "main"
                                ? t("plotThreads.importanceMain")
                                : t("plotThreads.importanceSub")}
                            </div>
                          </td>
                          <td className="px-4 py-3 text-muted">
                            {positionLabel(thread.planted_at)}
                          </td>
                          <td className="px-4 py-3 tabular-nums text-foreground">
                            {thread.age_in_chapters === null
                              ? t("unavailable")
                              : t("plotThreads.ageChapters", {
                                  count: numberFormatter.format(
                                    thread.age_in_chapters,
                                  ),
                                })}
                          </td>
                          <td className="px-4 py-3">
                            <span
                              className={`inline-flex rounded-full px-2.5 py-1 text-xs font-medium ${DUE_BADGE_CLASSES[thread.due_state]}`}
                            >
                              {dueLabel(thread)}
                            </span>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>

            <section aria-labelledby="story-health-characters">
              <div className="mb-3">
                <h3
                  id="story-health-characters"
                  className="text-base font-semibold text-foreground"
                >
                  {t("characters.title")}
                </h3>
                <p className="mt-1 text-sm text-muted">
                  {t("characters.description", {
                    count: numberFormatter.format(
                      report.observation.outlined_chapter_count,
                    ),
                  })}
                </p>
              </div>

              {report.character_absences.length === 0 ? (
                <p className="rounded-md border border-dashed border-border px-4 py-6 text-sm text-muted">
                  {t("characters.empty")}
                </p>
              ) : (
                <div className="overflow-x-auto rounded-md border border-border bg-surface">
                  <table className="min-w-[680px] w-full border-collapse text-left text-sm">
                    <thead className="bg-surface-secondary text-xs text-muted">
                      <tr>
                        <th scope="col" className="px-4 py-3 font-medium">
                          {t("characters.columns.name")}
                        </th>
                        <th scope="col" className="px-4 py-3 font-medium">
                          {t("characters.columns.absence")}
                        </th>
                        <th scope="col" className="px-4 py-3 font-medium">
                          {t("characters.columns.lastPresent")}
                        </th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border">
                      {report.character_absences.map((character) => (
                        <tr key={character.card_id}>
                          <td className="px-4 py-3">
                            <span className="font-medium text-foreground">
                              {character.name}
                            </span>
                          </td>
                          <td className="px-4 py-3">
                            <span
                              className={
                                character.currently_absent
                                  ? "font-medium text-foreground"
                                  : "text-muted"
                              }
                            >
                              {character.currently_absent
                                ? t("characters.absentChapters", {
                                    count: numberFormatter.format(
                                      character.consecutive_absent_chapters,
                                    ),
                                  })
                                : t("characters.presentLatest")}
                            </span>
                          </td>
                          <td className="px-4 py-3 text-muted">
                            {character.never_present
                              ? t("characters.neverPresent")
                              : positionLabel(character.last_present_at)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>

            <section aria-labelledby="story-health-words">
              <div className="mb-3">
                <h3
                  id="story-health-words"
                  className="text-base font-semibold text-foreground"
                >
                  {t("wordCounts.title")}
                </h3>
                <p className="mt-1 text-sm text-muted">
                  {t("wordCounts.description", {
                    threshold: numberFormatter.format(
                      report.policies.word_deviation_attention_ratio * 100,
                    ),
                  })}
                </p>
              </div>

              <div className="grid grid-cols-1 gap-6 xl:grid-cols-[minmax(0,0.85fr)_minmax(0,1.15fr)]">
                <WordCountTable
                  title={t("wordCounts.volumes")}
                  emptyText={t("wordCounts.emptyVolumes")}
                  rows={report.word_counts.volumes}
                  locale={locale}
                  t={t}
                />
                <WordCountTable
                  title={t("wordCounts.writtenChapters")}
                  emptyText={t("wordCounts.emptyChapters")}
                  rows={writtenChapters}
                  locale={locale}
                  t={t}
                />
              </div>
            </section>

            <p className="border-t border-dashed border-border pt-4 text-xs leading-5 text-muted">
              {t("readOnlyNote")}
            </p>
          </>
        )}
      </main>
    </div>
  );
}

type WordCountRow = VolumeWordCountHealth | ChapterWordCountHealth;

interface WordCountTableProps {
  title: string;
  emptyText: string;
  rows: WordCountRow[];
  locale: string;
  t: ReturnType<typeof useTranslations<"storyHealth">>;
}

function WordCountTable({
  title,
  emptyText,
  rows,
  locale,
  t,
}: WordCountTableProps) {
  const numberFormatter = new Intl.NumberFormat(locale);
  const percentFormatter = new Intl.NumberFormat(locale, {
    style: "percent",
    maximumFractionDigits: 1,
    signDisplay: "always",
  });
  const isVolume = (row: WordCountRow): row is VolumeWordCountHealth =>
    "volume_title" in row;

  return (
    <div>
      <h4 className="mb-2 text-sm font-medium text-foreground">{title}</h4>
      {rows.length === 0 ? (
        <p className="rounded-md border border-dashed border-border px-4 py-6 text-sm text-muted">
          {emptyText}
        </p>
      ) : (
        <div className="overflow-x-auto rounded-md border border-border bg-surface">
          <table className="min-w-[520px] w-full border-collapse text-left text-sm">
            <thead className="bg-surface-secondary text-xs text-muted">
              <tr>
                <th scope="col" className="px-4 py-3 font-medium">
                  {t("wordCounts.columns.scope")}
                </th>
                <th scope="col" className="px-4 py-3 font-medium">
                  {t("wordCounts.columns.actualTarget")}
                </th>
                <th scope="col" className="px-4 py-3 font-medium">
                  {t("wordCounts.columns.deviation")}
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {rows.map((row) => (
                <tr
                  key={isVolume(row) ? row.volume_id : row.chapter_id}
                >
                  <td className="px-4 py-3">
                    <div className="font-medium text-foreground">
                      {isVolume(row)
                        ? t("wordCounts.volumeLabel", {
                            volume: numberFormatter.format(row.volume_order),
                            title: row.volume_title,
                          })
                        : t("wordCounts.chapterLabel", {
                            volume: numberFormatter.format(row.volume_order),
                            chapter: numberFormatter.format(row.chapter_order),
                            title: row.chapter_title,
                          })}
                    </div>
                    {isVolume(row) && (
                      <div className="mt-0.5 text-xs text-muted">
                        {t("wordCounts.volumeCoverage", {
                          written: numberFormatter.format(
                            row.chapters_with_content,
                          ),
                          total: numberFormatter.format(row.chapter_count),
                        })}
                      </div>
                    )}
                  </td>
                  <td className="px-4 py-3 tabular-nums text-foreground">
                    {t("wordCounts.actualTarget", {
                      actual: numberFormatter.format(row.actual_word_count),
                      target: numberFormatter.format(row.target_word_count),
                    })}
                  </td>
                  <td className="px-4 py-3">
                    <span
                      className={`inline-flex rounded-full px-2.5 py-1 text-xs font-medium ${DEVIATION_BADGE_CLASSES[row.deviation_state]}`}
                    >
                      {row.deviation_ratio === null
                        ? t(`wordCounts.states.${row.deviation_state}`)
                        : t("wordCounts.deviationValue", {
                            percent: percentFormatter.format(
                              row.deviation_ratio,
                            ),
                          })}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
