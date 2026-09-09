"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import type { ChapterSummary, VolumeSummary } from "@/types/novel";

interface ChapterNavigatorProps {
  volumes: VolumeSummary[];
  deletedVolumes: VolumeSummary[];
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
  onDeleteVolume: (volumeId: string) => Promise<void>;
  onRestoreVolume: (volumeId: string) => Promise<void>;
  onHardDeleteVolume: (volumeId: string) => Promise<void>;
  onBulkDeleteChapters: (chapterIds: string[]) => Promise<void>;
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

function TrashIcon() {
  return (
    <svg aria-hidden="true" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6M10 11v5M14 11v5" />
    </svg>
  );
}

export default function ChapterNavigator({
  volumes,
  deletedVolumes,
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
  onDeleteVolume,
  onRestoreVolume,
  onHardDeleteVolume,
  onBulkDeleteChapters,
  onOpenVolumeOutline,
  onStartVolumeJob,
  onStartBookJob,
}: ChapterNavigatorProps) {
  const t = useTranslations("writing.chapterEditor");
  const tOutline = useTranslations("writing.outline");
  const tBatch = useTranslations("writing.batch");
  const selectedVolumeChapterCount = chapters.filter((c) => c.volume_id === selectedVolumeId).length;
  const [showVolumeForm, setShowVolumeForm] = useState(false);
  const [volumeTitle, setVolumeTitle] = useState("");
  const [creatingVolume, setCreatingVolume] = useState(false);
  const [creatingChapterFor, setCreatingChapterFor] = useState<string | null>(null);
  const [restoringId, setRestoringId] = useState<string | null>(null);
  const [deletingVolumeId, setDeletingVolumeId] = useState<string | null>(null);
  const [deleteVolumeArmedId, setDeleteVolumeArmedId] = useState<string | null>(null);
  const [restoringVolumeId, setRestoringVolumeId] = useState<string | null>(null);
  const [hardDeletingVolumeId, setHardDeletingVolumeId] = useState<string | null>(null);
  const [hardDeleteArmedId, setHardDeleteArmedId] = useState<string | null>(null);
  const [selectedChapterIds, setSelectedChapterIds] = useState<Set<string>>(new Set());
  const [bulkDeleteArmed, setBulkDeleteArmed] = useState(false);
  const [bulkDeleting, setBulkDeleting] = useState(false);

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

  const toggleChapterSelection = (chapterId: string) => {
    setBulkDeleteArmed(false);
    setSelectedChapterIds((current) => {
      const next = new Set(current);
      if (next.has(chapterId)) next.delete(chapterId);
      else next.add(chapterId);
      return next;
    });
  };

  const selectCurrentVolumeChapters = () => {
    setBulkDeleteArmed(false);
    setSelectedChapterIds(
      new Set(
        chapters
          .filter((chapter) => chapter.volume_id === selectedVolumeId)
          .map((chapter) => chapter._id),
      ),
    );
  };

  const deleteSelectedChapters = async () => {
    if (!bulkDeleteArmed || bulkDeleting || selectedChapterIds.size === 0) return;
    setBulkDeleting(true);
    try {
      await onBulkDeleteChapters([...selectedChapterIds]);
      setSelectedChapterIds(new Set());
      setBulkDeleteArmed(false);
    } finally {
      setBulkDeleting(false);
    }
  };

  return (
    <div className="studio-directory flex h-full min-h-0 w-full flex-col bg-surface-secondary/45">
      <div className="border-b border-border px-3 py-3">
        <div className="flex items-center justify-between gap-3">
          <div className="min-w-0">
            <h2 className="text-sm font-semibold text-foreground">{t("structureTitle")}</h2>
            <p className="mt-0.5 text-xs text-muted">
              {t("structureMeta", { volumes: volumes.length, chapters: chapters.length })}
            </p>
          </div>
          <button
            type="button"
            onClick={() => setShowVolumeForm((value) => !value)}
            className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-accent transition-colors hover:bg-accent/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            aria-label={t("newVolume")}
          >
            <PlusIcon />
          </button>
        </div>
        <div className="mt-3 grid grid-cols-2 gap-1.5">
          <button
            type="button"
            onClick={onOpenVolumeOutline}
            className="min-h-9 rounded-md border border-border px-2 py-1.5 text-[11px] font-medium leading-4 text-foreground transition-colors hover:bg-surface-secondary"
          >
            {tOutline("volumeTitle")}
          </button>
          <button
            type="button"
            onClick={onStartVolumeJob}
            disabled={!selectedVolumeId || selectedVolumeChapterCount === 0}
            title={tBatch("startButtonTitle")}
            className="min-h-9 rounded-md border border-border px-2 py-1.5 text-[11px] font-medium leading-4 text-foreground transition-colors hover:bg-surface-secondary disabled:opacity-50"
          >
            {tBatch("startButton")}
          </button>
          <button
            type="button"
            onClick={onStartBookJob}
            disabled={chapters.length === 0}
            title={tBatch("startBookButtonTitle")}
            className="min-h-9 rounded-md border border-border px-2 py-1.5 text-[11px] font-medium leading-4 text-foreground transition-colors hover:bg-surface-secondary disabled:opacity-50"
          >
            {tBatch("startBookButton")}
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
            className="rounded-lg bg-accent px-3 py-1.5 text-xs font-medium text-on-accent transition-colors hover:bg-accent-hover hover:text-on-accent-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
          >
            {creatingVolume ? t("creating") : t("create")}
          </button>
        </div>
      )}

      {chapters.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5 border-b border-border bg-surface px-3 py-2">
          <span className="mr-auto text-[11px] text-muted">
            {t("selectedChapters", { count: selectedChapterIds.size })}
          </span>
          <button
            type="button"
            onClick={selectCurrentVolumeChapters}
            disabled={!selectedVolumeId}
            className="rounded px-1.5 py-1 text-[11px] text-accent hover:bg-accent/10 disabled:opacity-40"
          >
            {t("selectCurrentVolume")}
          </button>
          {selectedChapterIds.size > 0 && (
            <>
              <button
                type="button"
                onClick={() => {
                  setSelectedChapterIds(new Set());
                  setBulkDeleteArmed(false);
                }}
                className="rounded px-1.5 py-1 text-[11px] text-muted hover:bg-surface-secondary"
              >
                {t("clearSelection")}
              </button>
              {bulkDeleteArmed ? (
                <>
                  <button
                    type="button"
                    onClick={() => setBulkDeleteArmed(false)}
                    className="rounded px-1.5 py-1 text-[11px] text-muted hover:bg-surface-secondary"
                  >
                    {t("cancel")}
                  </button>
                  <button
                    type="button"
                    onClick={() => void deleteSelectedChapters()}
                    disabled={bulkDeleting}
                    className="rounded-md bg-red-600 px-2 py-1 text-[11px] font-medium text-white hover:bg-red-700 disabled:opacity-50"
                  >
                    {bulkDeleting ? t("deleting") : t("confirmBulkDelete")}
                  </button>
                </>
              ) : (
                <button
                  type="button"
                  onClick={() => setBulkDeleteArmed(true)}
                  className="rounded px-1.5 py-1 text-[11px] font-medium text-red-600 hover:bg-red-50 dark:text-red-400 dark:hover:bg-red-950/30"
                >
                  {t("bulkDelete")}
                </button>
              )}
            </>
          )}
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
                    <input
                      type="checkbox"
                      checked={volumeChapters.length > 0 && volumeChapters.every((chapter) => selectedChapterIds.has(chapter._id))}
                      onChange={() => {
                        setBulkDeleteArmed(false);
                        setSelectedChapterIds((current) => {
                          const next = new Set(current);
                          const allSelected = volumeChapters.length > 0 && volumeChapters.every((chapter) => next.has(chapter._id));
                          volumeChapters.forEach((chapter) => {
                            if (allSelected) next.delete(chapter._id);
                            else next.add(chapter._id);
                          });
                          return next;
                        });
                      }}
                      disabled={volumeChapters.length === 0}
                      aria-label={t("selectVolumeChapters", { volume: volume.title })}
                      className="ml-2 h-4 w-4 shrink-0 accent-[var(--accent)] disabled:opacity-40"
                    />
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
                    {deleteVolumeArmedId === volume._id ? (
                      <div className="mr-1 flex shrink-0 items-center gap-0.5">
                        <button
                          type="button"
                          onClick={() => setDeleteVolumeArmedId(null)}
                          className="rounded px-1 py-1 text-[10px] text-muted hover:bg-surface-secondary"
                        >
                          {t("cancel")}
                        </button>
                        <button
                          type="button"
                          onClick={async () => {
                            setDeletingVolumeId(volume._id);
                            try {
                              await onDeleteVolume(volume._id);
                              setDeleteVolumeArmedId(null);
                              setSelectedChapterIds((current) => {
                                const next = new Set(current);
                                volumeChapters.forEach((chapter) => next.delete(chapter._id));
                                return next;
                              });
                            } finally {
                              setDeletingVolumeId(null);
                            }
                          }}
                          disabled={deletingVolumeId === volume._id}
                          className="rounded bg-red-600 px-1.5 py-1 text-[10px] font-medium text-white disabled:opacity-50"
                        >
                          {deletingVolumeId === volume._id ? t("deleting") : t("confirmDeleteVolume")}
                        </button>
                      </div>
                    ) : (
                      <button
                        type="button"
                        onClick={() => setDeleteVolumeArmedId(volume._id)}
                        className="mr-1 inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-muted transition-colors hover:bg-red-50 hover:text-red-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-500 dark:hover:bg-red-950/30 dark:hover:text-red-400"
                        aria-label={t("deleteVolume", { volume: volume.title })}
                      >
                        <TrashIcon />
                      </button>
                    )}
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
                          <div
                            key={chapter._id}
                            className={`my-0.5 flex items-start rounded-md transition-colors ${
                              selectedChapterId === chapter._id
                                ? "bg-surface text-accent shadow-sm"
                                : "text-foreground hover:bg-surface/75"
                            }`}
                          >
                            <input
                              type="checkbox"
                              checked={selectedChapterIds.has(chapter._id)}
                              onChange={() => toggleChapterSelection(chapter._id)}
                              aria-label={t("selectChapter", { chapter: chapter.title })}
                              className="ml-2 mt-2.5 h-4 w-4 shrink-0 accent-[var(--accent)]"
                            />
                            <button
                              type="button"
                              onClick={() => onSelectChapter(chapter._id)}
                              className="flex min-w-0 flex-1 items-start gap-2 px-2 py-2 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent"
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
                          </div>
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

        {deletedVolumes.length > 0 && (
          <details className="mt-3 border-t border-border pt-2">
            <summary className="cursor-pointer rounded-md px-2 py-1.5 text-xs font-medium text-muted hover:bg-surface hover:text-foreground">
              {t("volumeTrash", { count: deletedVolumes.length })}
            </summary>
            <div className="mt-1 space-y-1">
              {deletedVolumes.map((volume) => (
                <div key={volume._id} className="rounded-md px-2 py-2 hover:bg-surface">
                  <p className="truncate text-xs font-medium text-muted">{volume.title}</p>
                  <div className="mt-1 flex items-center gap-2">
                    <button
                      type="button"
                      onClick={async () => {
                        setRestoringVolumeId(volume._id);
                        try {
                          await onRestoreVolume(volume._id);
                        } finally {
                          setRestoringVolumeId(null);
                        }
                      }}
                      disabled={restoringVolumeId === volume._id}
                      className="text-[11px] font-medium text-accent hover:underline disabled:opacity-50"
                    >
                      {restoringVolumeId === volume._id ? t("restoring") : t("restoreVolume")}
                    </button>
                    {hardDeleteArmedId === volume._id ? (
                      <>
                        <button
                          type="button"
                          onClick={() => setHardDeleteArmedId(null)}
                          className="text-[11px] text-muted hover:underline"
                        >
                          {t("cancel")}
                        </button>
                        <button
                          type="button"
                          onClick={async () => {
                            setHardDeletingVolumeId(volume._id);
                            try {
                              await onHardDeleteVolume(volume._id);
                              setHardDeleteArmedId(null);
                            } finally {
                              setHardDeletingVolumeId(null);
                            }
                          }}
                          disabled={hardDeletingVolumeId === volume._id}
                          className="text-[11px] font-medium text-red-600 hover:underline disabled:opacity-50 dark:text-red-400"
                        >
                          {hardDeletingVolumeId === volume._id ? t("deleting") : t("confirmHardDeleteVolume")}
                        </button>
                      </>
                    ) : (
                      <button
                        type="button"
                        onClick={() => setHardDeleteArmedId(volume._id)}
                        className="text-[11px] text-red-600 hover:underline dark:text-red-400"
                      >
                        {t("hardDeleteVolume")}
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </details>
        )}
      </div>
    </div>
  );
}
