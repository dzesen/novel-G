"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import type { ChapterDraft } from "@/types/novel";

export type ChapterSaveState = "idle" | "dirty" | "saving" | "saved" | "error";

interface ChapterEditorPaneProps {
  chapterId: string | null;
  draft: ChapterDraft | null;
  wordCount: number;
  updatedAt?: string;
  loading: boolean;
  loadError: boolean;
  saveState: ChapterSaveState;
  onChange: (patch: Partial<ChapterDraft>) => void;
  onSave: () => void;
  onRetryLoad: () => void;
  onDelete: () => Promise<void>;
  onExport: () => void;
  onExportNovel: () => void;
  onOpenChapterOutline: () => void;
  onOpenProse: () => void;
  /** 无已接受细纲时禁用 AI 写正文：没有 outline 上下文包会退化（设计 §6）。 */
  canGenerateProse: boolean;
  onOpenStateBackfill: () => void;
  /** chapter.content 为空时禁用 AI 状态回填：后端读库里的正文，没正文就没得回填（2b-2 设计 §4.1）。 */
  hasContent: boolean;
  /** flushDraft 因标题为空被拒绝、面板未能打开时的提示文案；非空时渲染在按钮下方。 */
  stateBackfillBlocked: string;
}

function SaveStateLabel({ state }: { state: ChapterSaveState }) {
  const t = useTranslations("writing.chapterEditor");
  const tone = state === "error" ? "text-red-600 dark:text-red-400" : state === "saved" ? "text-green-700 dark:text-green-400" : "text-muted";
  return (
    <span className={`inline-flex items-center gap-1.5 text-xs ${tone}`} role="status" aria-live="polite">
      {state === "saving" && <span className="h-3 w-3 animate-spin rounded-full border border-current border-t-transparent" />}
      {t(`saveState.${state}`)}
    </span>
  );
}

export default function ChapterEditorPane({
  chapterId,
  draft,
  wordCount,
  updatedAt,
  loading,
  loadError,
  saveState,
  onChange,
  onSave,
  onRetryLoad,
  onDelete,
  onExport,
  onExportNovel,
  onOpenChapterOutline,
  onOpenProse,
  canGenerateProse,
  onOpenStateBackfill,
  hasContent,
  stateBackfillBlocked,
}: ChapterEditorPaneProps) {
  const t = useTranslations("writing.chapterEditor");
  const tOutline = useTranslations("writing.outline");
  const tProse = useTranslations("writing.prose");
  // stateBackfill 是顶层命名空间，与上面几个 writing.* 的 t() 不同源，需单独取。
  const tStateBackfill = useTranslations("stateBackfill");
  const [showSummary, setShowSummary] = useState(false);
  const [deleteArmed, setDeleteArmed] = useState(false);
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    setDeleteArmed(false);
    setShowSummary(false);
  }, [chapterId]);

  if (loading) {
    return (
      <main className="flex min-h-0 flex-1 flex-col bg-surface" aria-label={t("loading")}>
        <div className="border-b border-border px-6 py-5">
          <div className="h-8 w-2/5 animate-pulse rounded-lg bg-border/45" />
          <div className="mt-3 h-4 w-1/4 animate-pulse rounded bg-border/30" />
        </div>
        <div className="mx-auto w-full max-w-[76ch] flex-1 space-y-3 px-6 py-8">
          {[80, 100, 92, 70, 98, 88].map((width, index) => (
            <div key={index} className="h-4 animate-pulse rounded bg-border/30" style={{ width: `${width}%` }} />
          ))}
        </div>
      </main>
    );
  }

  if (loadError) {
    return (
      <main className="flex min-h-80 flex-1 flex-col items-center justify-center bg-surface px-6 text-center">
        <p className="text-sm font-medium text-foreground">{t("loadFailed")}</p>
        <p className="mt-1 text-xs text-muted">{t("loadFailedDescription")}</p>
        <button type="button" onClick={onRetryLoad} className="mt-4 rounded-lg border border-border px-3 py-1.5 text-sm text-foreground hover:bg-surface-secondary">
          {t("retry")}
        </button>
      </main>
    );
  }

  if (!chapterId || !draft) {
    return (
      <main className="flex min-h-80 flex-1 flex-col items-center justify-center bg-surface px-6 text-center">
        <svg aria-hidden="true" width="38" height="38" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.4" className="text-muted/70">
          <path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H19a1 1 0 0 1 1 1v18a1 1 0 0 1-1 1H6.5a1 1 0 0 1 0-5H20" />
        </svg>
        <p className="mt-4 text-sm font-medium text-foreground">{t("selectChapterTitle")}</p>
        <p className="mt-1 max-w-sm text-xs leading-5 text-muted">{t("selectChapterDescription")}</p>
      </main>
    );
  }

  const confirmDelete = async () => {
    if (!deleteArmed || deleting) return;
    setDeleting(true);
    try {
      await onDelete();
    } finally {
      setDeleting(false);
      setDeleteArmed(false);
    }
  };

  return (
    <main className="flex min-h-0 flex-1 flex-col bg-surface">
      <header className="border-b border-border px-4 py-3 sm:px-6">
        <div className="flex flex-wrap items-start gap-x-4 gap-y-2">
          <input
            value={draft.title}
            onChange={(event) => onChange({ title: event.target.value })}
            className="min-w-[16rem] flex-1 border-0 bg-transparent p-0 text-xl font-semibold text-foreground outline-none placeholder:text-muted focus:ring-0"
            placeholder={t("chapterTitlePlaceholder")}
            aria-label={t("chapterTitle")}
          />
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onOpenChapterOutline}
              className="rounded-lg border border-border px-2.5 py-1.5 text-xs font-medium text-foreground transition-colors hover:bg-surface-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            >
              {tOutline("chapterTitle")}
            </button>
            <button
              type="button"
              onClick={onOpenProse}
              disabled={!canGenerateProse}
              title={canGenerateProse ? undefined : tProse("needOutline")}
              className="rounded-lg border border-border px-2.5 py-1.5 text-xs font-medium text-foreground transition-colors hover:bg-surface-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
            >
              {tProse("title")}
            </button>
            <button
              type="button"
              onClick={onOpenStateBackfill}
              disabled={!hasContent}
              title={hasContent ? undefined : tStateBackfill("needContent")}
              className="rounded-lg border border-border px-2.5 py-1.5 text-xs font-medium text-foreground transition-colors hover:bg-surface-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
            >
              {tStateBackfill("openButton")}
            </button>
            <button
              type="button"
              onClick={onExportNovel}
              className="rounded-lg px-2.5 py-1.5 text-xs font-medium text-muted transition-colors hover:bg-surface-secondary hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            >
              {t("exportNovel")}
            </button>
            <button
              type="button"
              onClick={onExport}
              className="rounded-lg px-2.5 py-1.5 text-xs font-medium text-muted transition-colors hover:bg-surface-secondary hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            >
              {t("exportChapter")}
            </button>
            <button
              type="button"
              onClick={onSave}
              disabled={saveState === "saving" || !draft.title.trim()}
              className="rounded-lg bg-accent px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-accent-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
            >
              {t("saveNow")}
            </button>
          </div>
        </div>

        {stateBackfillBlocked && (
          <p role="alert" className="mt-1.5 text-xs text-red-600 dark:text-red-400">
            {stateBackfillBlocked}
          </p>
        )}

        <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2">
          <label className="flex items-center gap-2 text-xs text-muted">
            <span>{t("status")}</span>
            <select
              value={draft.status}
              onChange={(event) => onChange({ status: event.target.value as ChapterDraft["status"] })}
              className="rounded-md border border-border bg-background px-2 py-1 text-xs text-foreground outline-none focus:border-accent focus:ring-2 focus:ring-accent/15"
            >
              <option value="draft">{t("statuses.draft")}</option>
              <option value="writing">{t("statuses.writing")}</option>
              <option value="completed">{t("statuses.completed")}</option>
            </select>
          </label>
          <span className="text-xs tabular-nums text-muted">{t("wordCount", { count: wordCount })}</span>
          <SaveStateLabel state={saveState} />
          {updatedAt && (
            <span className="text-[11px] text-muted/90">
              {t("lastSaved", { time: new Date(updatedAt).toLocaleString() })}
            </span>
          )}
          <button
            type="button"
            onClick={() => setShowSummary((value) => !value)}
            className="ml-auto text-xs text-muted hover:text-foreground"
          >
            {showSummary ? t("hideSummary") : t("showSummary")}
          </button>
        </div>

        {showSummary && (
          <textarea
            value={draft.summary}
            onChange={(event) => onChange({ summary: event.target.value })}
            rows={2}
            className="mt-3 w-full resize-y rounded-lg border border-border bg-background px-3 py-2 text-sm leading-6 text-foreground outline-none placeholder:text-muted focus:border-accent focus:ring-2 focus:ring-accent/15"
            placeholder={t("summaryPlaceholder")}
            aria-label={t("summary")}
          />
        )}
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto bg-background/35">
        <div className="mx-auto flex min-h-full w-full max-w-[78ch] flex-col px-5 py-6 sm:px-8 sm:py-8">
          <textarea
            value={draft.content}
            onChange={(event) => onChange({ content: event.target.value })}
            className="min-h-[65vh] w-full flex-1 resize-none border-0 bg-transparent p-0 text-[16px] leading-8 text-foreground outline-none placeholder:text-muted focus:ring-0"
            placeholder={t("contentPlaceholder")}
            spellCheck
            aria-label={t("content")}
          />
        </div>
      </div>

      <footer className="flex min-h-11 flex-wrap items-center justify-between gap-2 border-t border-border bg-surface px-4 py-2 sm:px-6">
        <p className="text-[11px] text-muted">{t("autosaveHint")}</p>
        {deleteArmed ? (
          <div className="flex items-center gap-2">
            <span className="text-xs text-red-600 dark:text-red-400">{t("deleteConfirm")}</span>
            <button type="button" onClick={() => setDeleteArmed(false)} className="px-2 py-1 text-xs text-muted hover:text-foreground">
              {t("cancel")}
            </button>
            <button type="button" onClick={() => void confirmDelete()} disabled={deleting} className="rounded-md bg-red-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-red-700 disabled:opacity-50">
              {deleting ? t("deleting") : t("confirmDelete")}
            </button>
          </div>
        ) : (
          <button type="button" onClick={() => setDeleteArmed(true)} className="px-2 py-1 text-xs text-muted hover:text-red-600 dark:hover:text-red-400">
            {t("moveToTrash")}
          </button>
        )}
      </footer>
    </main>
  );
}
