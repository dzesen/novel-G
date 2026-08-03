/* eslint-disable react-hooks/set-state-in-effect */
"use client";

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { apiGet, apiPut, apiDelete } from "@/lib/api";
import type { CharacterState, PermanentFact, FactKind } from "@/types/memory";
import type { ChapterSummary, VolumeSummary } from "@/types/novel";

interface Props {
  mode: "create" | "edit";
  novelId?: string;
  initialFactId?: string;
}

const FACT_KINDS: FactKind[] = ["death", "injury", "identity", "relation", "ability"];

interface CharacterCard {
  _id: string;
  name: string;
}

export default function CharacterMemoryWorkspace({
  novelId,
  initialFactId,
}: Props) {
  const t = useTranslations("characterMemory");
  const [states, setStates] = useState<CharacterState[]>([]);
  const [names, setNames] = useState<Record<string, string>>({});
  const [chapters, setChapters] = useState<Array<ChapterSummary & { label: string }>>([]);
  const [error, setError] = useState<string | null>(null);
  const [stateDraft, setStateDraft] = useState<Record<string, { current_state: string; as_of: string }>>({});
  const [editingFactId, setEditingFactId] = useState<string | null>(null);
  const [factDraft, setFactDraft] = useState<{ fact: string; kind: FactKind; source_chapter_id: string } | null>(null);
  const [confirmDeleteFactId, setConfirmDeleteFactId] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!novelId) return;
    setError(null);
    try {
      const [stateRes, cardRes, chapterRes, volumeRes] = await Promise.all([
        apiGet<{ data: CharacterState[] }>(`/api/character-states/novel/${novelId}`),
        apiGet<{ data: CharacterCard[] }>(`/api/reference-cards/novel/${novelId}/character`),
        apiGet<{ data: ChapterSummary[] }>(`/api/chapters/novel/${novelId}`),
        apiGet<{ data: VolumeSummary[] }>(`/api/volumes/novel/${novelId}`),
      ]);
      setStates(stateRes.data);
      setNames(Object.fromEntries(cardRes.data.map((c) => [c._id, c.name])));
      const volumeOrders = Object.fromEntries(volumeRes.data.map((v) => [v._id, v.order_index]));
      setChapters(
        [...chapterRes.data]
          .sort((a, b) => (volumeOrders[a.volume_id] ?? 0) - (volumeOrders[b.volume_id] ?? 0) || a.order_index - b.order_index)
          .map((chapter) => ({
            ...chapter,
            label: `第${volumeOrders[chapter.volume_id] ?? "?"}卷·第${chapter.order_index}章 ${chapter.title}`,
          })),
      );
      setStateDraft((prev) => {
        const next = { ...prev };
        for (const s of stateRes.data) {
          if (!(s.card_id in next)) {
            next[s.card_id] = { current_state: s.current_state, as_of: s.as_of_chapter_id ?? "" };
          }
        }
        return next;
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : t("loadError"));
    }
  }, [novelId, t]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!initialFactId || states.length === 0) return;
    document
      .getElementById(`fact-${initialFactId}`)
      ?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [initialFactId, states]);

  if (!novelId) {
    return <div className="p-6 text-sm text-muted">{t("needNovel")}</div>;
  }

  const saveState = async (cardId: string) => {
    const d = stateDraft[cardId];
    if (!d) return;
    const selected = chapters.find((chapter) => chapter._id === d.as_of);
    if (!selected) {
      setError(t("selectChapter"));
      return;
    }
    try {
      await apiPut(`/api/character-states/novel/${novelId}/card/${cardId}/current-state`, {
        current_state: d.current_state,
        as_of_chapter_id: selected._id,
        as_of_chapter_order: selected.order_index,
      });
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : t("loadError"));
    }
  };

  const saveFact = async (cardId: string, factId: string) => {
    if (!factDraft) return;
    const selected = chapters.find((chapter) => chapter._id === factDraft.source_chapter_id);
    if (!selected) {
      setError(t("selectChapter"));
      return;
    }
    try {
      await apiPut(`/api/character-states/novel/${novelId}/card/${cardId}/facts/${factId}`, {
        fact: factDraft.fact,
        kind: factDraft.kind,
        source_chapter_id: selected._id,
        chapter_order: selected.order_index,
      });
      setEditingFactId(null);
      setFactDraft(null);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : t("loadError"));
    }
  };

  const deleteFact = async (cardId: string, factId: string) => {
    try {
      await apiDelete(`/api/character-states/novel/${novelId}/card/${cardId}/facts/${factId}`);
      setConfirmDeleteFactId(null);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : t("loadError"));
    }
  };

  const startEditFact = (f: PermanentFact) => {
    setEditingFactId(f.id);
    setFactDraft({ fact: f.fact, kind: f.kind, source_chapter_id: f.source_chapter_id ?? "" });
  };

  return (
    <div className="flex h-full flex-col gap-4 overflow-y-auto p-6">
      <h2 className="text-lg font-semibold text-foreground">{t("title")}</h2>
      {error && <div className="rounded border border-red-400 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>}
      {states.length === 0 && <div className="text-sm text-muted">{t("empty")}</div>}

      {states.map((s) => {
        const draft = stateDraft[s.card_id] ?? { current_state: s.current_state, as_of: s.as_of_chapter_id ?? "" };
        return (
          <section key={s._id} className="rounded-lg border border-border bg-surface p-4">
            <h3 className="mb-2 font-medium text-foreground">{names[s.card_id] ?? t("unknownCharacter")}</h3>

            <div className="mb-3 flex flex-col gap-2">
              <label className="text-sm text-muted">{t("currentState")}</label>
              <textarea
                className="rounded border border-border bg-surface-secondary px-2 py-1 text-sm"
                value={draft.current_state}
                onChange={(e) => setStateDraft({ ...stateDraft, [s.card_id]: { ...draft, current_state: e.target.value } })}
              />
              <div className="flex items-center gap-2">
                <span className="text-sm text-muted">{t("asOfChapter")}</span>
                <select
                  className="min-w-56 rounded border border-border bg-surface-secondary px-2 py-1 text-sm"
                  value={draft.as_of}
                  onChange={(e) => setStateDraft({ ...stateDraft, [s.card_id]: { ...draft, as_of: e.target.value } })}
                >
                  <option value="">{s.as_of_chapter_id ? t("selectChapter") : `${t("legacyChapter")}${s.as_of_chapter_order}`}</option>
                  {chapters.map((chapter) => <option key={chapter._id} value={chapter._id}>{chapter.label}</option>)}
                </select>
                <button className="rounded bg-accent px-3 py-1 text-sm text-white" onClick={() => saveState(s.card_id)}>{t("saveState")}</button>
              </div>
            </div>

            <div className="text-sm font-medium text-foreground">{t("permanentFacts")}</div>
            {s.permanent_facts.length === 0 && <div className="text-sm text-muted">{t("noFacts")}</div>}
            <ul className="mt-1 flex flex-col gap-2">
              {s.permanent_facts.map((f) => (
                <li
                  id={`fact-${f.id}`}
                  key={f.id}
                  className={`rounded border bg-surface-secondary p-2 text-sm ${
                    initialFactId === f.id
                      ? "border-accent ring-2 ring-accent/20"
                      : "border-border"
                  }`}
                >
                  {editingFactId === f.id && factDraft ? (
                    <div className="flex flex-wrap items-center gap-2">
                      <input className="flex-1 rounded border border-border bg-surface px-2 py-1" value={factDraft.fact} onChange={(e) => setFactDraft({ ...factDraft, fact: e.target.value })} />
                      <select className="rounded border border-border bg-surface px-2 py-1" value={factDraft.kind} onChange={(e) => setFactDraft({ ...factDraft, kind: e.target.value as FactKind })}>
                        {FACT_KINDS.map((k) => <option key={k} value={k}>{t(`kind${k.charAt(0).toUpperCase()}${k.slice(1)}`)}</option>)}
                      </select>
                      <select className="min-w-52 rounded border border-border bg-surface px-2 py-1" value={factDraft.source_chapter_id} onChange={(e) => setFactDraft({ ...factDraft, source_chapter_id: e.target.value })}>
                        <option value="">{f.source_chapter_id ? t("selectChapter") : `${t("legacyChapter")}${f.chapter_order}`}</option>
                        {chapters.map((chapter) => <option key={chapter._id} value={chapter._id}>{chapter.label}</option>)}
                      </select>
                      <button className="text-accent" onClick={() => saveFact(s.card_id, f.id)}>{t("save")}</button>
                      <button className="text-muted" onClick={() => { setEditingFactId(null); setFactDraft(null); }}>{t("cancel")}</button>
                    </div>
                  ) : (
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="rounded bg-surface px-2 py-0.5 text-xs text-muted">{t(`kind${f.kind.charAt(0).toUpperCase()}${f.kind.slice(1)}`)}</span>
                      <span className="text-xs text-muted">{t("factChapter")}{f.chapter_order}</span>
                      <span className="flex-1 text-foreground">{f.fact}</span>
                      <button className="text-accent" onClick={() => startEditFact(f)}>{t("edit")}</button>
                      {confirmDeleteFactId === f.id ? (
                        <span className="flex items-center gap-2">
                          <span className="text-muted">{t("confirmDeleteFact")}</span>
                          <button className="text-red-600" onClick={() => deleteFact(s.card_id, f.id)}>{t("delete")}</button>
                          <button className="text-muted" onClick={() => setConfirmDeleteFactId(null)}>{t("cancel")}</button>
                        </span>
                      ) : (
                        <button className="text-red-600" onClick={() => setConfirmDeleteFactId(f.id)}>{t("delete")}</button>
                      )}
                    </div>
                  )}
                </li>
              ))}
            </ul>
          </section>
        );
      })}
    </div>
  );
}
