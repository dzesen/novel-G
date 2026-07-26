/* eslint-disable react-hooks/set-state-in-effect */
"use client";

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { apiGet, apiPost, apiPut, apiDelete } from "@/lib/api";
import type { PlotThread, ThreadStatus, ThreadImportance } from "../chapters/outline/outlineTypes";
import type { ChapterSummary, VolumeSummary } from "@/types/novel";
import {
  plotThreadDraftToPayload,
  plotThreadToDraft,
  type ThreadDraft,
} from "./plotThreadDraft";

interface Props {
  mode: "create" | "edit";
  novelId?: string;
  initialThreadId?: string;
}

const STATUS_VALUES: ThreadStatus[] = ["planted", "developing", "resolved", "abandoned"];
const IMPORTANCE_VALUES: ThreadImportance[] = ["main", "sub"];

export default function PlotThreadWorkspace({
  novelId,
  initialThreadId,
}: Props) {
  const t = useTranslations("plotThreads");
  const [threads, setThreads] = useState<PlotThread[]>([]);
  const [chapters, setChapters] = useState<Array<ChapterSummary & { label: string }>>([]);
  const [error, setError] = useState<string | null>(null);
  const [orphansOnly, setOrphansOnly] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState<ThreadDraft | null>(null);
  const [creating, setCreating] = useState(false);
  const [confirmingDeleteId, setConfirmingDeleteId] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!novelId) return;
    setError(null);
    try {
      const query = orphansOnly ? "?with_reference_audit=true" : "";
      const [res, chapterRes, volumeRes] = await Promise.all([
        apiGet<{ data: PlotThread[] }>(`/api/plot-threads/novel/${novelId}${query}`),
        apiGet<{ data: ChapterSummary[] }>(`/api/chapters/novel/${novelId}`),
        apiGet<{ data: VolumeSummary[] }>(`/api/volumes/novel/${novelId}`),
      ]);
      setThreads(res.data);
      const volumeOrders = Object.fromEntries(volumeRes.data.map((volume) => [volume._id, volume.order_index]));
      setChapters(
        [...chapterRes.data]
          .sort((a, b) => (volumeOrders[a.volume_id] ?? 0) - (volumeOrders[b.volume_id] ?? 0) || a.order_index - b.order_index)
          .map((chapter) => ({ ...chapter, label: `第${volumeOrders[chapter.volume_id] ?? "?"}卷·第${chapter.order_index}章 ${chapter.title}` })),
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : t("loadError"));
    }
  }, [novelId, orphansOnly, t]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!initialThreadId || threads.length === 0) return;
    document
      .getElementById(`thread-${initialThreadId}`)
      ?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [initialThreadId, threads]);

  if (!novelId) {
    return <div className="p-6 text-sm text-muted">{t("needNovel")}</div>;
  }

  const isOrphan = (th: PlotThread) =>
    th.source === "outline" && (th.referenced_by_chapter_orders?.length ?? 0) === 0;
  const visible = orphansOnly ? threads.filter(isOrphan) : threads;

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
    <div className="flex h-full flex-col gap-4 overflow-y-auto p-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-lg font-semibold text-foreground">{t("title")}</h2>
        <div className="flex items-center gap-4">
          <label className="flex items-center gap-2 text-sm text-muted">
            <input type="checkbox" checked={orphansOnly} onChange={(e) => setOrphansOnly(e.target.checked)} />
            {t("orphanFilter")}
          </label>
          <button className="rounded bg-accent px-3 py-1 text-sm text-white" onClick={startCreate}>{t("newThread")}</button>
        </div>
      </div>

      {error && <div className="rounded border border-red-400 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>}
      {creating && editor}
      {visible.length === 0 && !creating && <div className="text-sm text-muted">{t("empty")}</div>}

      <ul className="flex flex-col gap-2">
        {visible.map((th) => (
          <li
            id={`thread-${th._id}`}
            key={th._id}
            className={`rounded-lg border bg-surface p-3 ${
              initialThreadId === th._id
                ? "border-accent ring-2 ring-accent/20"
                : "border-border"
            }`}
          >
            {editingId === th._id ? editor : (
              <div className="flex flex-col gap-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium text-foreground">{th.name}</span>
                  <span className="rounded bg-surface-secondary px-2 py-0.5 text-xs text-muted">{t(`status${th.status.charAt(0).toUpperCase()}${th.status.slice(1)}`)}</span>
                  <span className="rounded bg-surface-secondary px-2 py-0.5 text-xs text-muted">{th.importance === "main" ? t("importanceMain") : t("importanceSub")}</span>
                  {orphansOnly && isOrphan(th) && <span className="rounded bg-amber-100 px-2 py-0.5 text-xs text-amber-700">{t("orphanBadge")}</span>}
                </div>
                {th.description && <p className="text-sm text-muted">{th.description}</p>}
                {orphansOnly && (
                  <p className="text-xs text-muted">
                    {(th.referenced_by_chapters?.length ?? 0) > 0
                      ? `${t("referencedBy")}${th.referenced_by_chapters!.map((item) => item.label).join(", ")}`
                      : t("notReferenced")}
                  </p>
                )}
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
        ))}
      </ul>
    </div>
  );
}
