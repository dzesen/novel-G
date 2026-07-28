"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { apiGet } from "@/lib/api";
import type { ReferenceCard } from "@/types/novel";
import type { PlotThread } from "./outlineTypes";

export interface RosterEntry {
  id: string;
  name: string;
  /** 一句简介，仅用于选择器里帮人辨认。 */
  hint: string;
}

const WORLDBOOK_TYPES = ["location", "item", "rule", "lore"] as const;

/**
 * 拉取本书的人物卡、世界卡与伏笔，建 id→名 映射。
 *
 * 这与后端装配给 AI 的 roster 是两份独立取数（设计 §6.2），不保证逐字节一致；
 * 本 hook 的产物只用于显示与人工挑选，判据仍在后端。
 */
export function useRoster(novelId: string | null) {
  const t = useTranslations("writing.outline");
  const [characters, setCharacters] = useState<RosterEntry[]>([]);
  const [worldbook, setWorldbook] = useState<RosterEntry[]>([]);
  const [threads, setThreads] = useState<RosterEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    if (!novelId) return;
    setLoading(true);
    setError("");
    try {
      // 六个请求彼此独立，必须一并发出：把伏笔那条单独 await 会白白多一个往返，
      // 面板每次打开都慢一拍。
      const [characterRes, threadRes, ...worldbookRes] = await Promise.all([
        apiGet<{ data: ReferenceCard[] }>(`/api/reference-cards/novel/${novelId}/character`),
        apiGet<{ data: PlotThread[] }>(`/api/plot-threads/novel/${novelId}`),
        ...WORLDBOOK_TYPES.map((type) =>
          apiGet<{ data: ReferenceCard[] }>(`/api/reference-cards/novel/${novelId}/${type}`)
        ),
      ]);

      setCharacters(
        characterRes.data.map((card) => ({
          id: card._id,
          name: card.name,
          hint: card.subtitle || card.description.slice(0, 40),
        }))
      );
      setWorldbook(
        worldbookRes.flatMap((res, index) =>
          res.data.map((card) => ({
            id: card._id,
            name: card.name,
            hint: `${t(`worldbookTypes.${WORLDBOOK_TYPES[index]}`)} · ${
              card.subtitle || card.description.slice(0, 30)
            }`,
          }))
        )
      );
      setThreads(
        threadRes.data.map((thread) => {
          // 状态是后端枚举（planted/developing/resolved/abandoned），直接显示等于
          // 把内部标识符漏给用户，且英文界面下也不会翻译，故一律过 i18n 映射。
          const status = t(`threadStatus.${thread.status}`);
          return {
            id: thread._id,
            name: thread.name,
            hint: thread.due_chapter_order
              ? t("threadHintWithDue", { status, chapter: thread.due_chapter_order })
              : status,
          };
        })
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [novelId, t]);

  useEffect(() => {
    void load();
  }, [load]);

  const nameById = useMemo(() => {
    const map: Record<string, string> = {};
    for (const entry of [...characters, ...worldbook, ...threads]) {
      map[entry.id] = entry.name;
    }
    return map;
  }, [characters, worldbook, threads]);

  return { characters, worldbook, threads, nameById, loading, error, reload: load };
}
