/* eslint-disable react-hooks/set-state-in-effect */
"use client";

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { apiGet, apiPost, apiPut, apiDelete } from "@/lib/api";
import type { PlotThread, ThreadStatus, ThreadImportance } from "../chapters/outline/outlineTypes";
import type { ChapterSummary, VolumeSummary } from "@/types/novel";
import {
  checkpointWindow,
  type GenerationJob,
  type GenerationJobSummary,
} from "../chapters/batch/batchTypes";
import {
  summarizePlotThreadReferenceCleanup,
  type PlotThreadReferenceCleanupSummary,
} from "../chapters/batch/batchPresentation";
import {
  plotThreadDraftToPayload,
  plotThreadToDraft,
  type ThreadDraft,
} from "./plotThreadDraft";

interface Props {
  mode: "create" | "edit";
  novelId?: string;
  initialThreadId?: string;
  onTargetValidation: (threadId: string, valid: boolean) => void;
}

const STATUS_VALUES: ThreadStatus[] = ["planted", "developing", "resolved", "abandoned"];
const IMPORTANCE_VALUES: ThreadImportance[] = ["main", "sub"];

export default function PlotThreadWorkspace({
  novelId,
  initialThreadId,
  onTargetValidation,
}: Props) {
  const t = useTranslations("plotThreads");
  const metadataT = useTranslations("writing.generationMetadata");
  const [threads, setThreads] = useState<PlotThread[]>([]);
  const [chapters, setChapters] = useState<Array<ChapterSummary & { label: string }>>([]);
  const [error, setError] = useState<string | null>(null);
  const [unmatchedReferenceReview, setUnmatchedReferenceReview] = useState<PlotThreadReferenceCleanupSummary | null>(null);
  const [orphansOnly, setOrphansOnly] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState<ThreadDraft | null>(null);
  const [creating, setCreating] = useState(false);
  const [confirmingDeleteId, setConfirmingDeleteId] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  const load = useCallback(async () => {
    if (!novelId) return;
    setError(null);
    setLoaded(false);
    setUnmatchedReferenceReview(null);
    try {
      const [res, chapterRes, volumeRes, currentJob] = await Promise.all([
        apiGet<{ data: PlotThread[] }>(`/api/plot-threads/novel/${novelId}?with_reference_audit=true`),
        apiGet<{ data: ChapterSummary[] }>(`/api/chapters/novel/${novelId}`),
        apiGet<{ data: VolumeSummary[] }>(`/api/volumes/novel/${novelId}`),
        apiGet<GenerationJobSummary | null>(`/api/generation-jobs/novel/${novelId}/current`)
          .then((summary) => summary && summary.novel_id === novelId
            ? apiGet<GenerationJob>(`/api/generation-jobs/${encodeURIComponent(summary._id)}`)
            : null).catch(() => null),
      ]);
      setThreads(res.data);
      setUnmatchedReferenceReview(
        currentJob
          ? summarizePlotThreadReferenceCleanup(
              checkpointWindow(currentJob),
              res.data
                .filter((thread) => thread.status === "planted" || thread.status === "developing")
                .map((thread) => thread._id),
            )
          : null,
      );
      const volumeOrders = Object.fromEntries(volumeRes.data.map((volume) => [volume._id, volume.order_index]));
      setChapters(
        [...chapterRes.data]
          .sort((a, b) => (volumeOrders[a.volume_id] ?? 0) - (volumeOrders[b.volume_id] ?? 0) || a.order_index - b.order_index)
          .map((chapter) => ({ ...chapter, label: `第${volumeOrders[chapter.volume_id] ?? "?"}卷·第${chapter.order_index}章 ${chapter.title}` })),
      );
      setLoaded(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("loadError"));
    }
  }, [novelId, t]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!initialThreadId || !loaded) return;
    const matched = threads.some((thread) => thread._id === initialThreadId);
    onTargetValidation(initialThreadId, matched);
    if (matched) {
      document
        .getElementById(`thread-${initialThreadId}`)
        ?.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }, [initialThreadId, loaded, onTargetValidation, threads]);

  if (!novelId) {
    return <div className="p-6 text-sm text-muted">{t("needNovel")}</div>;
  }

  const isOrphan = (th: PlotThread) =>
    th.source === "outline" && (th.referenced_by_chapter_orders?.length ?? 0) === 0;
  const visible = orphansOnly ? threads.filter(isOrphan) : threads;
  const orphanCount = threads.filter(isOrphan).length;
  const recoverableThreadIds = new Set(
    unmatchedReferenceReview?.recoverableThreadIds ?? [],
  );

  const startCreate = () => {
    setCreating(true);
    setEditingId(null);
    setDraft({
      name: "", description: "", status: "planted", importance: "sub",
      planted_chapter_id: "", legacy_planted_chapter_order: null,
      due_kind: "none", due_value: "", resolved_chapter_id: "",
      legacy_resolved_chapter_order: null, notes: "",
    });
  };
  const startEdit = (th: PlotThread) => {
    setCreating(false);
    setEditingId(th._id);
    setDraft(plotThreadToDraft(th));
  };
  const cancel = () => {
    setCreating(false);
    setEditingId(null);
    setDraft(null);
  };

  const submit = async () => {
    if (!draft) return;
    try {
      if (creating) {
        await apiPost(`/api/plot-threads/novel/${novelId}`, plotThreadDraftToPayload(draft, chapters));
      } else if (editingId) {
        await apiPut(`/api/plot-threads/novel/${novelId}/${editingId}`, plotThreadDraftToPayload(draft, chapters));
      }
      cancel();
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : t("loadError"));
    }
  };

  const remove = async (id: string) => {
    try {
      await apiDelete(`/api/plot-threads/novel/${novelId}/${id}`);
      setConfirmingDeleteId(null);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : t("loadError"));
    }
  };

  const field = (label: string, node: React.ReactNode) => (
    <label className="flex flex-col gap-1 text-sm">
      <span className="text-muted">{label}</span>
      {node}
    </label>
  );

  const editor = draft && (
    <div className="grid grid-cols-1 gap-3 rounded-lg border border-border bg-surface p-4 md:grid-cols-2">
      {field(t("name"), <input className="rounded border border-border bg-surface-secondary px-2 py-1" value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} />)}
      {field(t("importance"), <select className="rounded border border-border bg-surface-secondary px-2 py-1" value={draft.importance} onChange={(e) => setDraft({ ...draft, importance: e.target.value as ThreadImportance })}>{IMPORTANCE_VALUES.map((v) => <option key={v} value={v}>{t(`importance${v === "main" ? "Main" : "Sub"}`)}</option>)}</select>)}
      {field(t("status"), <select className="rounded border border-border bg-surface-secondary px-2 py-1" value={draft.status} onChange={(e) => setDraft({ ...draft, status: e.target.value as ThreadStatus })}>{STATUS_VALUES.map((v) => <option key={v} value={v}>{t(`status${v.charAt(0).toUpperCase()}${v.slice(1)}`)}</option>)}</select>)}
      {field(t("plantedChapter"), <>
        <select className="rounded border border-border bg-surface-secondary px-2 py-1" value={draft.planted_chapter_id} onChange={(e) => setDraft({ ...draft, planted_chapter_id: e.target.value, legacy_planted_chapter_order: null })}><option value="">{t("noChapter")}</option>{chapters.map((chapter) => <option key={chapter._id} value={chapter._id}>{chapter.label}</option>)}</select>
        {draft.legacy_planted_chapter_order != null && <span className="text-xs text-amber-700">{t("legacyChapterOrder", { order: draft.legacy_planted_chapter_order })} {t("mapLegacyChapter")}</span>}
      </>)}
      {field(t("dueType"), <select className="rounded border border-border bg-surface-secondary px-2 py-1" value={draft.due_kind} onChange={(e) => setDraft({ ...draft, due_kind: e.target.value as ThreadDraft["due_kind"], due_value: "" })}><option value="none">{t("noChapter")}</option><option value="chapter">{t("existingChapter")}</option><option value="planned_ordinal">{t("plannedOrdinal")}</option></select>)}
      {draft.due_kind === "chapter" && field(t("dueChapter"), <select className="rounded border border-border bg-surface-secondary px-2 py-1" value={draft.due_value} onChange={(e) => setDraft({ ...draft, due_value: e.target.value })}><option value="">{t("noChapter")}</option>{chapters.map((chapter) => <option key={chapter._id} value={chapter._id}>{chapter.label}</option>)}</select>)}
      {draft.due_kind === "planned_ordinal" && field(t("plannedOrdinal"), <input type="number" min={1} className="rounded border border-border bg-surface-secondary px-2 py-1" value={draft.due_value} onChange={(e) => setDraft({ ...draft, due_value: e.target.value })} />)}
      {field(t("resolvedChapter"), <>
        <select className="rounded border border-border bg-surface-secondary px-2 py-1" value={draft.resolved_chapter_id} onChange={(e) => setDraft({ ...draft, resolved_chapter_id: e.target.value, legacy_resolved_chapter_order: null })}><option value="">{t("noChapter")}</option>{chapters.map((chapter) => <option key={chapter._id} value={chapter._id}>{chapter.label}</option>)}</select>
        {draft.legacy_resolved_chapter_order != null && <span className="text-xs text-amber-700">{t("legacyChapterOrder", { order: draft.legacy_resolved_chapter_order })} {t("mapLegacyChapter")}</span>}
      </>)}
      <div className="md:col-span-2">{field(t("description"), <textarea className="rounded border border-border bg-surface-secondary px-2 py-1" value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })} />)}</div>
      <div className="md:col-span-2">{field(t("notes"), <textarea className="rounded border border-border bg-surface-secondary px-2 py-1" value={draft.notes} onChange={(e) => setDraft({ ...draft, notes: e.target.value })} />)}</div>
      <div className="flex gap-2 md:col-span-2">
        <button className="rounded bg-accent px-3 py-1 text-sm text-white" onClick={submit}>{creating ? t("create") : t("save")}</button>
        <button className="rounded border border-border px-3 py-1 text-sm" onClick={cancel}>{t("cancel")}</button>
      </div>
    </div>
  );

  return (
    <div className="flex h-full flex-col gap-4 overflow-y-auto p-4 sm:p-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-lg font-semibold text-foreground">{t("title")}</h2>
        <div className="flex flex-wrap items-center justify-end gap-3">
          <label className="flex items-center gap-2 text-sm text-muted">
            <input type="checkbox" checked={orphansOnly} onChange={(e) => setOrphansOnly(e.target.checked)} />
            {t("orphanFilter")}
          </label>
          <button className="rounded bg-accent px-3 py-1 text-sm text-white" onClick={startCreate}>{t("newThread")}</button>
        </div>
      </div>

      {error && (
        <div role="alert" className="flex flex-wrap items-center gap-3 rounded border border-red-400 bg-red-50 px-3 py-2 text-sm text-red-700">
          <span className="min-w-0 flex-1 break-words">{error}</span>
          <button
            type="button"
            onClick={() => void load()}
            className="min-h-9 rounded border border-red-400 px-3 font-medium hover:bg-red-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-500"
          >
            {t("retry")}
          </button>
        </div>
      )}
      {unmatchedReferenceReview && (
        <div
          role="status"
          data-testid="unmatched-thread-reference-attention"
          className={`grid min-w-0 gap-3 rounded-lg border p-3 text-sm ${
            unmatchedReferenceReview.unresolvedCount > 0
              ? "border-amber-300 bg-amber-50 text-amber-950 dark:border-amber-900/70 dark:bg-amber-950/30 dark:text-amber-100"
              : "border-sky-300 bg-sky-50 text-sky-950 dark:border-sky-900/70 dark:bg-sky-950/30 dark:text-sky-100"
          }`}
        >
          {unmatchedReferenceReview.nowMatchedCount > 0 && (
            <div className="grid min-w-0 gap-1">
              <span className="font-semibold">
                {t("referenceRecoveryTitle", { count: unmatchedReferenceReview.nowMatchedCount })}
              </span>
              <span className="break-words leading-5">{t("referenceRecoveryBody")}</span>
            </div>
          )}
          {unmatchedReferenceReview.unresolvedCount > 0 && (
            <div className={`grid min-w-0 gap-1 ${
              unmatchedReferenceReview.nowMatchedCount > 0
                ? "border-t border-amber-300 pt-3 dark:border-amber-900/70"
                : ""
            }`}>
              <span className="font-semibold">
                {t("referenceUnresolvedTitle", { count: unmatchedReferenceReview.unresolvedCount })}
              </span>
              <span className="break-words leading-5">{t("referenceUnresolvedBody")}</span>
            </div>
          )}
          <span className="leading-5">
            {t("referenceCleanupScope", { count: unmatchedReferenceReview.chapterCount })}
          </span>
          {unmatchedReferenceReview.readableValues.length > 0 && (
            <span className="break-words text-xs">
              {t("referenceCleanupNames", {
                names: unmatchedReferenceReview.readableValues.join(metadataT("listSeparator")),
              })}
            </span>
          )}
        </div>
      )}
      {orphanCount > 0 && (
        <div
          role="status"
          data-testid="orphan-thread-attention"
          className="grid gap-1 rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-900/70 dark:bg-amber-950/30 dark:text-amber-200"
        >
          <span className="font-semibold">{t("orphanAttentionTitle", { count: orphanCount })}</span>
          <span className="leading-5">{t("orphanAttentionBody")}</span>
        </div>
      )}
      {creating && editor}
      {visible.length === 0 && !creating && <div className="text-sm text-muted">{t("empty")}</div>}

      <ul className="flex flex-col gap-2">
        {visible.map((th) => {
          const needsRecovery = recoverableThreadIds.has(th._id);
          return (
            <li
              id={`thread-${th._id}`}
              key={th._id}
              data-testid={needsRecovery ? "recoverable-thread-attention" : undefined}
              className={`min-w-0 rounded-lg border p-3 ${
                initialThreadId === th._id
                  ? "border-accent ring-2 ring-accent/20"
                  : needsRecovery
                    ? "border-amber-400 bg-amber-50/70 dark:border-amber-800 dark:bg-amber-950/25"
                    : isOrphan(th)
                      ? "border-amber-300 bg-amber-50/70 dark:border-amber-900/70 dark:bg-amber-950/20"
                      : "border-border bg-surface"
              }`}
            >
              {editingId === th._id ? editor : (
                <div className="flex flex-col gap-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="min-w-0 break-words font-medium text-foreground">{th.name}</span>
                    <span className="rounded bg-surface-secondary px-2 py-0.5 text-xs text-muted">{t(`status${th.status.charAt(0).toUpperCase()}${th.status.slice(1)}`)}</span>
                    <span className="rounded bg-surface-secondary px-2 py-0.5 text-xs text-muted">{th.importance === "main" ? t("importanceMain") : t("importanceSub")}</span>
                    {needsRecovery && <span className="rounded bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-900 dark:bg-amber-900/60 dark:text-amber-100">{t("referenceRecoveryBadge")}</span>}
                    {isOrphan(th) && <span className="rounded bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-800 dark:bg-amber-900/50 dark:text-amber-200">{t("orphanBadge")}</span>}
                  </div>
                  {th.description && <p className="text-sm text-muted">{th.description}</p>}
                  {needsRecovery && <p className="break-words text-xs text-amber-900 dark:text-amber-100">{t("referenceRecoveryThreadHint")}</p>}
                  {isOrphan(th) && <p className="text-xs text-amber-800 dark:text-amber-200">{t("notReferenced")}</p>}
                  <div className="mt-1 flex gap-3 text-sm">
                    <button className="text-accent" onClick={() => startEdit(th)}>{t("edit")}</button>
                    {confirmingDeleteId === th._id ? (
                      <span className="flex items-center gap-2">
                        <span className="text-muted">{t("confirmDelete")}</span>
                        <button className="text-red-600" onClick={() => remove(th._id)}>{t("delete")}</button>
                        <button className="text-muted" onClick={() => setConfirmingDeleteId(null)}>{t("cancel")}</button>
                      </span>
                    ) : (
                      <button className="text-red-600" onClick={() => setConfirmingDeleteId(th._id)}>{t("delete")}</button>
                    )}
                  </div>
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
