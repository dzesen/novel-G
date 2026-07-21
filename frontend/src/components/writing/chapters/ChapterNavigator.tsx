"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import type { ChapterSummary, VolumeSummary } from "@/types/novel";

interface ChapterNavigatorProps {
  volumes: VolumeSummary[];
  chapters: ChapterSummary[];
  trash: ChapterSummary[];
  selectedChapterId: string | null;
  selectedVolumeId: string | null;
  loading: boolean;
  onSelectChapter: (chapterId: string) => void;
  onSelectVolume: (volumeId: string) => void;
  onCreateVolume: (title: string) => Promise<void>;
  onCreateChapter: (volumeId: string) => Promise<void>;
  onRestoreChapter: (chapterId: string) => Promise<void>;
  onOpenVolumeOutline: () => void;
  onStartVolumeJob: () => void;
  onStartBookJob: () => void;
}

function PlusIcon() {
  return (
    <svg aria-hidden="true" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <path d="M12 5v14M5 12h14" />
    </svg>
  );
}

export default function ChapterNavigator({
  volumes,
  chapters,
  trash,
  selectedChapterId,
  selectedVolumeId,
  loading,
  onSelectChapter,
  onSelectVolume,
  onCreateVolume,
  onCreateChapter,
  onRestoreChapter,
  onOpenVolumeOutline,
  onStartVolumeJob,
  onStartBookJob,
}: ChapterNavigatorProps) {
  const t = useTranslations("writing.chapterEditor");
  const tOutline = useTranslations("writing.outline");
  const tBatch = useTranslations("writing.batch");
  const selectedVolumeChapterCount = chapters.filter((c) => c.volume_id === selectedVolumeId).length;
  const bookFillableCount = chapters.filter((c) => !(c.word_count > 0 && c.summary.trim())).length;
  const [showVolumeForm, setShowVolumeForm] = useState(false);
  const [volumeTitle, setVolumeTitle] = useState("");
  const [creatingVolume, setCreatingVolume] = useState(false);
  const [creatingChapterFor, setCreatingChapterFor] = useState<string | null>(null);
  const [restoringId, setRestoringId] = useState<string | null>(null);

  const submitVolume = async () => {
    const title = volumeTitle.trim();
    if (!title || creatingVolume) return;
    setCreatingVolume(true);
    try {
      await onCreateVolume(title);
      setVolumeTitle("");
      setShowVolumeForm(false);
    } finally {
      setCreatingVolume(false);
    }
  };

  const createChapter = async (volumeId: string) => {
    if (creatingChapterFor) return;
    setCreatingChapterFor(volumeId);
    try {
      await onCreateChapter(volumeId);
    } finally {
      setCreatingChapterFor(null);
    }
  };

  const restoreChapter = async (chapterId: string) => {
    setRestoringId(chapterId);
    try {
      await onRestoreChapter(chapterId);
    } finally {
      setRestoringId(null);
    }
  };

  return (
    <aside className="flex max-h-[42vh] w-full shrink-0 flex-col border-b border-border bg-surface-secondary/45 md:max-h-none md:h-full md:w-72 md:border-b-0 md:border-r">
      <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3">
        <div className="min-w-0">
          <h2 className="text-sm font-semibold text-foreground">{t("structureTitle")}</h2>
          <p className="mt-0.5 text-xs text-muted">
            {t("structureMeta", { volumes: volumes.length, chapters: chapters.length })}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <button
            type="button"
            onClick={onOpenVolumeOutline}
            className="rounded-md border border-border px-2.5 py-1.5 text-xs font-medium text-foreground transition-colors hover:bg-surface-secondary"
          >
            {tOutline("volumeTitle")}
          </button>
          <button
            type="button"
            onClick={onStartVolumeJob}
            disabled={!selectedVolumeId || selectedVolumeChapterCount === 0}
            title={tBatch("startButtonTitle")}
            className="rounded-md border border-border px-2.5 py-1.5 text-xs font-medium text-foreground transition-colors hover:bg-surface-secondary disabled:opacity-50"
          >
            {tBatch("startButton")}
          </button>
          <button
            type="button"
            onClick={onStartBookJob}
            disabled={bookFillableCount === 0}
            title={tBatch("startBookButtonTitle")}
            className="rounded-md border border-border px-2.5 py-1.5 text-xs font-medium text-foreground transition-colors hover:bg-surface-secondary disabled:opacity-50"
          >
            {tBatch("startBookButton")}
          </button>
          <button
            type="button"
            onClick={() => setShowVolumeForm((value) => !value)}
            className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-accent transition-colors hover:bg-accent/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
            aria-label={t("newVolume")}
          >
            <PlusIcon />
          </button>
        </div>
      </div>

      {showVolumeForm && (
        <div className="flex gap-2 border-b border-border bg-surface px-3 py-3">
          <input
            autoFocus
            value={volumeTitle}
            onChange={(event) => setVolumeTitle(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") void submitVolume();
              if (event.key === "Escape") setShowVolumeForm(false);
            }}
            placeholder={t("volumeTitlePlaceholder")}
            className="min-w-0 flex-1 rounded-lg border border-border bg-background px-3 py-1.5 text-sm text-foreground outline-none placeholder:text-muted focus:border-accent focus:ring-2 focus:ring-accent/15"
          />
          <button
            type="button"
            onClick={() => void submitVolume()}
            disabled={!volumeTitle.trim() || creatingVolume}
            className="rounded-lg bg-accent px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-accent-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
          >
            {creatingVolume ? t("creating") : t("create")}
          </button>
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-y-auto px-2 py-2">
        {loading ? (
          <div className="space-y-3 p-2" aria-label={t("loading")}>
            {[0, 1, 2].map((item) => (
              <div key={item} className="space-y-2">
                <div className="h-8 animate-pulse rounded-lg bg-border/45" />
                <div className="ml-4 h-7 animate-pulse rounded-lg bg-border/30" />
              </div>
            ))}
          </div>
        ) : volumes.length === 0 ? (
          <div className="flex min-h-40 flex-col items-center justify-center px-5 text-center">
            <div className="mb-3 flex h-10 w-10 items-center justify-center rounded-full bg-accent/10 text-accent">
              <PlusIcon />
            </div>
            <p className="text-sm font-medium text-foreground">{t("emptyStructureTitle")}</p>
            <p className="mt-1 text-xs leading-5 text-muted">{t("emptyStructureDescription")}</p>
            <button
              type="button"
              onClick={() => setShowVolumeForm(true)}
              className="mt-3 text-xs font-medium text-accent hover:underline"
            >
              {t("createFirstVolume")}
            </button>
          </div>
        ) : (
          <div className="space-y-1">
            {volumes.map((volume) => {
              const volumeChapters = chapters
                .filter((chapter) => chapter.volume_id === volume._id)
                .sort((a, b) => a.order_index - b.order_index);
              const active = selectedVolumeId === volume._id;
              return (
                <section key={volume._id} className="rounded-lg">
                  <div className={`group flex items-center rounded-lg ${active ? "bg-accent/8" : "hover:bg-surface"}`}>
                    <button
                      type="button"
                      onClick={() => onSelectVolume(volume._id)}
                      className="min-w-0 flex-1 px-2.5 py-2 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent"
                    >
                      <span className="block truncate text-sm font-medium text-foreground">{volume.title}</span>
                      <span className="mt-0.5 block text-[11px] text-muted">
                        {t("volumeMeta", { chapters: volumeChapters.length, words: volume.word_count || 0 })}
                      </span>
                    </button>
                    <button
                      type="button"
                      onClick={() => void createChapter(volume._id)}
                      disabled={Boolean(creatingChapterFor)}
                      className="mr-1 inline-flex h-7 w-7 items-center justify-center rounded-md text-muted opacity-100 transition-colors hover:bg-accent/10 hover:text-accent focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent md:opacity-0 md:group-hover:opacity-100"
                      aria-label={t("addChapterTo", { volume: volume.title })}
                    >
                      {creatingChapterFor === volume._id ? (
                        <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-current border-t-transparent" />
                      ) : (
                        <PlusIcon />
                      )}
                    </button>
                  </div>

                  {(active || volumeChapters.some((chapter) => chapter._id === selectedChapterId)) && (
                    <div className="ml-3 border-l border-border pl-2">
                      {volumeChapters.length === 0 ? (
                        <button
                          type="button"
                          onClick={() => void createChapter(volume._id)}
                          className="my-1 w-full rounded-md px-2 py-2 text-left text-xs text-muted hover:bg-surface hover:text-accent"
                        >
                          {t("emptyVolume")}
                        </button>
                      ) : (
                        volumeChapters.map((chapter) => (
                          <button
                            key={chapter._id}
                            type="button"
                            onClick={() => onSelectChapter(chapter._id)}
                            className={`my-0.5 flex w-full items-start gap-2 rounded-md px-2 py-2 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent ${
                              selectedChapterId === chapter._id
                                ? "bg-surface text-accent shadow-sm"
                                : "text-foreground hover:bg-surface/75"
                            }`}
                          >
                            <span className="mt-0.5 w-5 shrink-0 text-right text-[11px] tabular-nums text-muted">
                              {chapter.order_index}
                            </span>
                            <span className="min-w-0 flex-1">
                              <span className="block truncate text-xs font-medium">{chapter.title}</span>
                              <span className="mt-0.5 block text-[10px] text-muted">
                                {t("wordCountShort", { count: chapter.word_count })}
                              </span>
                            </span>
                          </button>
                        ))
                      )}
                    </div>
                  )}
                </section>
              );
            })}
          </div>
        )}

        {trash.length > 0 && (
          <details className="mt-3 border-t border-border pt-2">
            <summary className="cursor-pointer rounded-md px-2 py-1.5 text-xs font-medium text-muted hover:bg-surface hover:text-foreground">
              {t("trash", { count: trash.length })}
            </summary>
            <div className="mt-1 space-y-1">
              {trash.map((chapter) => (
                <div key={chapter._id} className="flex items-center gap-2 rounded-md px-2 py-1.5 hover:bg-surface">
                  <span className="min-w-0 flex-1 truncate text-xs text-muted">{chapter.title}</span>
                  <button
                    type="button"
                    onClick={() => void restoreChapter(chapter._id)}
                    disabled={restoringId === chapter._id}
                    className="shrink-0 text-[11px] font-medium text-accent hover:underline disabled:opacity-50"
                  >
                    {restoringId === chapter._id ? t("restoring") : t("restore")}
                  </button>
                </div>
              ))}
            </div>
          </details>
        )}
      </div>
    </aside>
  );
}
