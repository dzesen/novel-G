"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { useFormatter, useTranslations } from "next-intl";
import { apiGet, apiPost, apiDelete } from "@/lib/api";
import { Button } from "@/components/ui/Button";
import { Dialog } from "@/components/ui/Dialog";
import type { NovelSummary } from "@/types/novel";

interface DeletedNovel extends NovelSummary {
  deleted_at?: string;
}
interface TrashBinProps {
  open: boolean;
  onClose: () => void;
  onRestored: () => void;
}
type TrashAction = "restore" | "delete";

export default function TrashBin({ open, onClose, onRestored }: TrashBinProps) {
  const t = useTranslations("bookshelf");
  const tn = useTranslations("novel");
  const format = useFormatter();
  const [novels, setNovels] = useState<DeletedNovel[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadFailed, setLoadFailed] = useState(false);
  const [pending, setPending] = useState<Record<string, TrashAction>>({});
  const [errors, setErrors] = useState<Record<string, TrashAction>>({});
  const pendingIds = useRef(new Set<string>());
  const listRequest = useRef<AbortController | null>(null);
  const mounted = useRef(false);
  const isOpen = useRef(open);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      listRequest.current?.abort();
    };
  }, []);

  const fetchDeleted = useCallback(async () => {
    listRequest.current?.abort();
    const request = new AbortController();
    listRequest.current = request;
    setLoading(true);
    setLoadFailed(false);
    try {
      const res = await apiGet<{ data: DeletedNovel[] }>("/api/novels/deleted/list", {
        signal: request.signal,
      });
      if (!request.signal.aborted) setNovels(res.data);
    } catch {
      if (!request.signal.aborted) setLoadFailed(true);
    } finally {
      if (listRequest.current === request) listRequest.current = null;
      if (!request.signal.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    isOpen.current = open;
    if (open) void fetchDeleted();
    return () => listRequest.current?.abort();
  }, [open, fetchDeleted]);

  const handleAction = async (novel: DeletedNovel, action: TrashAction) => {
    const id = novel._id;
    if (pendingIds.current.has(id)) return;
    if (action === "delete" && !confirm(t("hardDeleteConfirm", { title: novel.title }))) return;
    pendingIds.current.add(id);
    setPending((previous) => ({ ...previous, [id]: action }));
    setErrors((previous) => {
      const next = { ...previous };
      delete next[id];
      return next;
    });
    try {
      if (action === "restore") await apiPost(`/api/novels/${id}/restore`, {});
      else await apiDelete(`/api/novels/${id}/hard`);
    } catch {
      if (mounted.current) setErrors((previous) => ({ ...previous, [id]: action }));
      return;
    } finally {
      pendingIds.current.delete(id);
      if (mounted.current) {
        setPending((previous) => {
          const next = { ...previous };
          delete next[id];
          return next;
        });
      }
    }
    if (!mounted.current) return;
    setNovels((previous) => previous.filter((item) => item._id !== id));
    // Replace an in-flight list with a fresh read so reopening retains other
    // windows' additions without bringing this successfully removed item back.
    if (isOpen.current && listRequest.current) void fetchDeleted();
    if (action === "restore") onRestored();
  };

  return (
    <Dialog open={open} onClose={onClose} title={t("trashBin")} closeLabel={t("closeTrash")} className="max-w-2xl">
      <div className="p-4 sm:p-5">
        {loading ? (
          <p role="status" className="py-8 text-center text-sm text-muted">{t("loading")}</p>
        ) : loadFailed ? (
          <div role="alert" className="space-y-3 py-6 text-center">
            <p className="text-sm text-foreground">{t("trashLoadFailed")}</p>
            <Button onClick={() => void fetchDeleted()}>{t("retry")}</Button>
          </div>
        ) : novels.length === 0 ? (
          <p role="status" className="py-8 text-center text-sm text-muted">{t("trashEmpty")}</p>
        ) : (
          <ul className="space-y-3">
            {novels.map((novel) => {
              const action = pending[novel._id];
              const deletedAt = novel.deleted_at ? new Date(novel.deleted_at) : null;
              return (
                <li key={novel._id} className="rounded-lg border border-border p-3 sm:p-4">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0 flex-1 basis-60">
                      <h3 className="break-words text-base font-medium text-foreground">{novel.title}</h3>
                      {novel.genre !== "unclassified" && <p className="mt-1 break-words text-sm text-muted">{novel.genre}</p>}
                      <dl className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-sm text-muted">
                        <div className="flex gap-1"><dt>{tn("chapterCount")}</dt><dd>{format.number(novel.stats?.chapter_count ?? 0)}</dd></div>
                        <div className="flex gap-1"><dt>{tn("totalWords")}</dt><dd>{format.number(novel.stats?.total_word_count ?? 0)}</dd></div>
                        {deletedAt && Number.isFinite(deletedAt.getTime()) && (
                          <div className="flex gap-1"><dt>{t("deletedAt")}</dt><dd>{format.dateTime(deletedAt, { year: "numeric", month: "numeric", day: "numeric" })}</dd></div>
                        )}
                      </dl>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      <Button size="sm" disabled={!!action} onClick={() => void handleAction(novel, "restore")}>
                        {action === "restore" ? t("restoring") : t("restore")}
                      </Button>
                      <Button size="sm" variant="danger" disabled={!!action} onClick={() => void handleAction(novel, "delete")}>
                        {action === "delete" ? t("hardDeleting") : t("hardDelete")}
                      </Button>
                    </div>
                  </div>
                  {errors[novel._id] && <p role="alert" className="mt-3 text-sm text-red-700 dark:text-red-300">{t(errors[novel._id] === "restore" ? "restoreFailed" : "hardDeleteFailed")}</p>}
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </Dialog>
  );
}
