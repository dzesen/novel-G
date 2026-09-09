"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { useTranslations } from "next-intl";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { apiGet, apiDelete } from "@/lib/api";
import { Button } from "@/components/ui/Button";
import type { NovelSummary, NovelDetail } from "@/types/novel";
import NovelList from "./NovelList";
import NovelDetailPanel from "./NovelDetail";
import NewNovelPanel from "./NewNovelPanel";
import TrashBin from "./TrashBin";

export default function BookshelfContent() {
  const t = useTranslations("bookshelf");
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const [novels, setNovels] = useState<NovelSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedNovel, setSelectedNovel] = useState<NovelDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState(false);
  const [detailError, setDetailError] = useState(false);
  const [deleteError, setDeleteError] = useState(false);
  const [trashOpen, setTrashOpen] = useState(false);
  const selectionRequest = useRef(0);
  const returnFocus = useRef<HTMLElement | null>(null);
  const restoreFocusPending = useRef(false);
  const detailStatus = useRef<HTMLHeadingElement>(null);
  const isCreating = searchParams.get("create") === "1";

  const fetchNovels = useCallback(async () => {
    setLoading(true);
    setError(false);
    try {
      const res = await apiGet<{ data: NovelSummary[] }>("/api/novels/list");
      setNovels(res.data);
    } catch { setError(true); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void fetchNovels(); }, [fetchNovels]);
  useEffect(() => () => { selectionRequest.current += 1; }, []);
  useEffect(() => {
    if (!isCreating && !selectedId && restoreFocusPending.current) {
      restoreFocusPending.current = false;
      if (returnFocus.current?.isConnected) returnFocus.current.focus();
    }
    if (selectedId && (detailLoading || detailError)) detailStatus.current?.focus();
  }, [isCreating, selectedId, detailLoading, detailError]);

  const setCreateRoute = (enabled: boolean) => {
    const params = new URLSearchParams(searchParams.toString());
    if (enabled) params.set("create", "1"); else params.delete("create");
    const query = params.toString();
    router.replace(query ? `${pathname}?${query}` : pathname, { scroll: false });
  };
  const handleSelect = async (id: string) => {
    if (!selectedId) {
      returnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    }
    const request = ++selectionRequest.current;
    setSelectedId(id);
    setSelectedNovel(null);
    setDetailLoading(true);
    setDetailError(false);
    setDeleteError(false);
    setCreateRoute(false);
    try {
      const novel = await apiGet<NovelDetail>(`/api/novels/${id}`);
      if (selectionRequest.current === request) setSelectedNovel(novel);
    } catch {
      if (selectionRequest.current === request) setDetailError(true);
    } finally {
      if (selectionRequest.current === request) setDetailLoading(false);
    }
  };
  const handleBackToList = () => {
    restoreFocusPending.current = true;
    selectionRequest.current += 1;
    setSelectedId(null);
    setSelectedNovel(null);
    setDetailError(false);
    setDeleteError(false);
    setCreateRoute(false);
  };
  const handleNewNovel = () => {
    returnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    selectionRequest.current += 1;
    setSelectedId(null);
    setSelectedNovel(null);
    setCreateRoute(true);
  };
  const handleDelete = async (id: string) => {
    setDeleteError(false);
    try {
      await apiDelete(`/api/novels/${id}`);
      handleBackToList();
      await fetchNovels();
    } catch { setDeleteError(true); }
  };

  return (
    <div className="bookshelf-workspace">
      <div hidden={isCreating || selectedId !== null}>
        {error ? <div className="library-empty" role="alert"><h1>{t("loadFailed")}</h1><p>{t("loadFailedHint")}</p><Button onClick={() => void fetchNovels()}>{t("retry")}</Button></div> :
          <NovelList novels={novels} loading={loading} onSelect={handleSelect} onNewNovel={handleNewNovel} onOpenTrash={() => setTrashOpen(true)} />}
      </div>
      {isCreating ? <div className="library-creation"><NewNovelPanel onCancel={handleBackToList} /></div> : selectedId && (
        <div className="library-detail">
          {deleteError && <p role="alert" className="mb-3 text-sm text-red-600 dark:text-red-400">{t("deleteFailed")}</p>}
          {detailLoading || detailError ? <div className="library-empty" role={detailError ? "alert" : "status"}>
            <h2 ref={detailStatus} tabIndex={-1}>{detailError ? t("detailLoadFailed") : t("loading")}</h2>
            {detailError && <Button onClick={() => void handleSelect(selectedId)}>{t("retry")}</Button>}
            <Button variant="quiet" onClick={handleBackToList}>{t("backToShelf")}</Button>
          </div> : <NovelDetailPanel novel={selectedNovel} onDelete={handleDelete} onBack={handleBackToList} />}
        </div>
      )}
      <TrashBin open={trashOpen} onClose={() => setTrashOpen(false)} onRestored={fetchNovels} />
    </div>
  );
}
