"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/Button";
import { IconButton } from "@/components/ui/IconButton";
import { useDismissableLayer } from "@/components/ui/useDismissableLayer";
import type { ChapterDraft } from "@/types/novel";
import type { ChapterWorkspaceLayoutControls } from "./ChapterWorkspaceLayout";
import JudgeReviewRecords from "./prose/JudgeReviewRecords";

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
  /** flushDraft 因标题为空被拒绝、面板未能打开时的提示文案；非空时渲染在按钮下方。 */
  stateBackfillBlocked: string;
  workspaceControls: ChapterWorkspaceLayoutControls;
}

function DirectoryIcon() {
  return (
    <svg aria-hidden="true" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M4 5h5M4 12h5M4 19h5M12 5h8M12 12h8M12 19h8" />
    </svg>
  );
}

function ContextIcon() {
  return (
    <svg aria-hidden="true" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="9" />
      <path d="M12 11v5M12 8h.01" />
    </svg>
  );
}

function AssistantIcon() {
  return (
    <svg aria-hidden="true" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="m12 3 1.1 3.3a4 4 0 0 0 2.6 2.6L19 10l-3.3 1.1a4 4 0 0 0-2.6 2.6L12 17l-1.1-3.3a4 4 0 0 0-2.6-2.6L5 10l3.3-1.1a4 4 0 0 0 2.6-2.6Z" />
      <path d="m18.5 16 .5 1.5a2 2 0 0 0 1.5 1.5l-1.5.5a2 2 0 0 0-1.5 1.5l-.5-1.5a2 2 0 0 0-1.5-1.5l1.5-.5a2 2 0 0 0 1.5-1.5Z" />
    </svg>
  );
}

function MoreIcon() {
  return (
    <svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
      <circle cx="5" cy="12" r="1.5" />
      <circle cx="12" cy="12" r="1.5" />
      <circle cx="19" cy="12" r="1.5" />
    </svg>
  );
}

function DirectoryAccess({
  workspaceControls,
}: {
  workspaceControls: ChapterWorkspaceLayoutControls;
}) {
  const tw = useTranslations("writing.chapterEditor.workspace");
  return (
    <>
      <IconButton
        label={tw("openDirectory")}
        size="sm"
        className="md:hidden"
        onClick={workspaceControls.openDirectory}
      >
        <DirectoryIcon />
      </IconButton>
      <span className="hidden md:inline-flex">
        <IconButton
          label={workspaceControls.directoryVisible ? tw("hideDirectory") : tw("showDirectory")}
          size="sm"
          selected={workspaceControls.directoryVisible}
          onClick={workspaceControls.toggleDirectory}
        >
          <DirectoryIcon />
        </IconButton>
      </span>
    </>
  );
}

function EditorPlaceholder({
  label,
  workspaceControls,
  centered = false,
  children,
}: {
  label: string;
  workspaceControls: ChapterWorkspaceLayoutControls;
  centered?: boolean;
  children: ReactNode;
}) {
  return (
    <section className="flex min-h-0 flex-1 flex-col bg-surface" aria-label={label}>
      <header className="flex min-h-12 shrink-0 items-center border-b border-border px-3 sm:px-5">
        <DirectoryAccess workspaceControls={workspaceControls} />
      </header>
      <div className={centered ? "flex min-h-80 flex-1 flex-col items-center justify-center px-6 text-center" : "min-h-0 flex-1"}>
        {children}
      </div>
    </section>
  );
}

function EditorActionsMenu({
  onExport,
  onExportNovel,
}: {
  onExport: () => void;
  onExportNovel: () => void;
}) {
  const t = useTranslations("writing.chapterEditor");
  const tw = useTranslations("writing.chapterEditor.workspace");
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const closeMenu = useCallback(() => setOpen(false), []);

  useDismissableLayer(open, rootRef, triggerRef, closeMenu);

  return (
    <div ref={rootRef} className="relative shrink-0">
      <IconButton
        ref={triggerRef}
        label={tw("moreActions")}
        size="sm"
        selected={open}
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <MoreIcon />
      </IconButton>
      {open && (
        <div className="absolute right-0 top-[calc(100%+0.4rem)] z-30 w-44 rounded-lg border border-border bg-surface p-1.5 shadow-dialog">
          <button
            type="button"
            onClick={() => {
              onExport();
              closeMenu();
            }}
            className="min-h-9 w-full rounded-md px-3 text-left text-xs font-medium text-foreground hover:bg-surface-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
          >
            {t("exportChapter")}
          </button>
          <button
            type="button"
            onClick={() => {
              onExportNovel();
              closeMenu();
            }}
            className="min-h-9 w-full rounded-md px-3 text-left text-xs font-medium text-foreground hover:bg-surface-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
          >
            {t("exportNovel")}
          </button>
        </div>
      )}
    </div>
  );
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
  stateBackfillBlocked,
  workspaceControls,
}: ChapterEditorPaneProps) {
  const t = useTranslations("writing.chapterEditor");
  const tw = useTranslations("writing.chapterEditor.workspace");
  const [showSummary, setShowSummary] = useState(false);
  const [deleteArmed, setDeleteArmed] = useState(false);
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    setDeleteArmed(false);
    setShowSummary(false);
  }, [chapterId]);

  if (loading) {
    return (
      <EditorPlaceholder label={t("loading")} workspaceControls={workspaceControls}>
        <div className="border-b border-border px-6 py-5">
          <div className="h-8 w-2/5 animate-pulse rounded-lg bg-border/45" />
          <div className="mt-3 h-4 w-1/4 animate-pulse rounded bg-border/30" />
        </div>
        <div className="mx-auto w-full max-w-[76ch] space-y-3 px-6 py-8">
          {[80, 100, 92, 70, 98, 88].map((width, index) => (
            <div key={index} className="h-4 animate-pulse rounded bg-border/30" style={{ width: `${width}%` }} />
          ))}
        </div>
      </EditorPlaceholder>
    );
  }

  if (loadError) {
    return (
      <EditorPlaceholder label={t("loadFailed")} workspaceControls={workspaceControls} centered>
        <p className="text-sm font-medium text-foreground">{t("loadFailed")}</p>
        <p className="mt-1 text-xs text-muted">{t("loadFailedDescription")}</p>
        <button type="button" onClick={onRetryLoad} className="mt-4 rounded-lg border border-border px-3 py-1.5 text-sm text-foreground hover:bg-surface-secondary">
          {t("retry")}
        </button>
      </EditorPlaceholder>
    );
  }

  if (!chapterId || !draft) {
    return (
      <EditorPlaceholder label={t("selectChapterTitle")} workspaceControls={workspaceControls} centered>
        <svg aria-hidden="true" width="38" height="38" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.4" className="text-muted/70">
          <path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H19a1 1 0 0 1 1 1v18a1 1 0 0 1-1 1H6.5a1 1 0 0 1 0-5H20" />
        </svg>
        <p className="mt-4 text-sm font-medium text-foreground">{t("selectChapterTitle")}</p>
        <p className="mt-1 max-w-sm text-xs leading-5 text-muted">{t("selectChapterDescription")}</p>
      </EditorPlaceholder>
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
    <section className="flex h-full min-h-0 flex-1 flex-col bg-surface">
      <header className="border-b border-border px-3 py-3 sm:px-5">
        <div className="flex min-w-0 items-center gap-2">
          <input
            value={draft.title}
            onChange={(event) => onChange({ title: event.target.value })}
            className="min-w-0 flex-1 border-0 bg-transparent p-0 text-lg font-semibold text-foreground outline-none placeholder:text-muted focus:ring-0 sm:text-xl"
            placeholder={t("chapterTitlePlaceholder")}
            aria-label={t("chapterTitle")}
          />
          <Button
            variant="primary"
            size="sm"
            onClick={onSave}
            disabled={saveState === "saving" || !draft.title.trim()}
          >
            {t("saveNow")}
          </Button>
          <EditorActionsMenu onExport={onExport} onExportNovel={onExportNovel} />
        </div>

        {stateBackfillBlocked && (
          <p role="alert" className="mt-1.5 text-xs text-red-600 dark:text-red-400">
            {stateBackfillBlocked}
          </p>
        )}

        <div className="mt-2.5 flex flex-wrap items-center gap-x-2 gap-y-2">
          <div className="flex items-center gap-0.5 border-r border-border pr-2">
            <DirectoryAccess workspaceControls={workspaceControls} />
            <IconButton
              label={tw("openContext")}
              size="sm"
              className="xl:hidden"
              onClick={workspaceControls.openContext}
            >
              <ContextIcon />
            </IconButton>
            <span className="hidden xl:inline-flex">
              <IconButton
                label={workspaceControls.contextVisible ? tw("hideContext") : tw("showContext")}
                size="sm"
                selected={workspaceControls.contextVisible}
                onClick={workspaceControls.toggleContext}
              >
                <ContextIcon />
              </IconButton>
            </span>
            <IconButton
              label={tw("openAssistant")}
              size="sm"
              onClick={workspaceControls.openAssistant}
            >
              <AssistantIcon />
            </IconButton>
          </div>
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
            <span className="text-[11px] text-muted">
              {t("lastSaved", { time: new Date(updatedAt).toLocaleString() })}
            </span>
          )}
          <button
            type="button"
            onClick={() => setShowSummary((value) => !value)}
            className="ml-auto min-h-8 rounded-md px-2 text-xs text-muted hover:bg-surface-secondary hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
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
        <div className="mx-auto flex min-h-full w-full max-w-[72ch] flex-col px-5 py-6 sm:px-8 sm:py-9">
          <JudgeReviewRecords key={chapterId} chapterId={chapterId} />
          <textarea
            value={draft.content}
            onChange={(event) => onChange({ content: event.target.value })}
            className="min-h-[65vh] w-full flex-1 resize-none border-0 bg-transparent p-0 font-writing text-[17px] leading-[2.05] text-foreground outline-none placeholder:text-muted focus:ring-0"
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
    </section>
  );
}
