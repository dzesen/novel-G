/* eslint-disable react-hooks/set-state-in-effect */
"use client";

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { apiGet, apiPost, apiPut, apiDelete } from "@/lib/api";
import type { PlotThread, ThreadStatus, ThreadImportance } from "../chapters/outline/outlineTypes";

interface Props {
  mode: "create" | "edit";
  novelId?: string;
}

const STATUS_VALUES: ThreadStatus[] = ["planted", "developing", "resolved", "abandoned"];
const IMPORTANCE_VALUES: ThreadImportance[] = ["main", "sub"];

type ThreadDraft = {
  name: string;
  description: string;
  status: ThreadStatus;
  importance: ThreadImportance;
  planted_chapter_order: string;
  due_chapter_order: string;
  resolved_chapter_order: string;
  notes: string;
};

function toDraft(t: PlotThread): ThreadDraft {
  return {
    name: t.name,
    description: t.description ?? "",
    status: t.status,
    importance: t.importance,
    planted_chapter_order: t.planted_chapter_order != null ? String(t.planted_chapter_order) : "",
    due_chapter_order: t.due_chapter_order != null ? String(t.due_chapter_order) : "",
    resolved_chapter_order: t.resolved_chapter_order != null ? String(t.resolved_chapter_order) : "",
    notes: t.notes ?? "",
  };
}

function draftToPayload(d: ThreadDraft) {
  const num = (s: string) => (s.trim() === "" ? null : Number(s));
  return {
    name: d.name,
    description: d.description,
    status: d.status,
    importance: d.importance,
    planted_chapter_order: num(d.planted_chapter_order),
    due_chapter_order: num(d.due_chapter_order),
    resolved_chapter_order: num(d.resolved_chapter_order),
    notes: d.notes,
  };
}

export default function PlotThreadWorkspace({ novelId }: Props) {
  const t = useTranslations("plotThreads");
  const [threads, setThreads] = useState<PlotThread[]>([]);
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
      const res = await apiGet<{ data: PlotThread[] }>(`/api/plot-threads/novel/${novelId}${query}`);
      setThreads(res.data);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("loadError"));
    }
  }, [novelId, orphansOnly, t]);

  useEffect(() => {
    void load();
  }, [load]);

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
      planted_chapter_order: "", due_chapter_order: "", resolved_chapter_order: "", notes: "",
    });
  };
  const startEdit = (th: PlotThread) => {
    setCreating(false);
    setEditingId(th._id);
    setDraft(toDraft(th));
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
        await apiPost(`/api/plot-threads/novel/${novelId}`, draftToPayload(draft));
      } else if (editingId) {
        await apiPut(`/api/plot-threads/novel/${novelId}/${editingId}`, draftToPayload(draft));
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
      {field(t("dueChapter"), <input type="number" className="rounded border border-border bg-surface-secondary px-2 py-1" value={draft.due_chapter_order} onChange={(e) => setDraft({ ...draft, due_chapter_order: e.target.value })} />)}
      {field(t("plantedChapter"), <input type="number" className="rounded border border-border bg-surface-secondary px-2 py-1" value={draft.planted_chapter_order} onChange={(e) => setDraft({ ...draft, planted_chapter_order: e.target.value })} />)}
      {field(t("resolvedChapter"), <input type="number" className="rounded border border-border bg-surface-secondary px-2 py-1" value={draft.resolved_chapter_order} onChange={(e) => setDraft({ ...draft, resolved_chapter_order: e.target.value })} />)}
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
          <li key={th._id} className="rounded-lg border border-border bg-surface p-3">
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
                    {(th.referenced_by_chapter_orders?.length ?? 0) > 0
                      ? `${t("referencedBy")}${th.referenced_by_chapter_orders!.join(", ")}`
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
