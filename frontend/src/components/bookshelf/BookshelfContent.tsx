"use client";

import { useState, useEffect, useCallback } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { apiGet, apiDelete } from "@/lib/api";
import type { NovelSummary, NovelDetail } from "@/types/novel";
import NovelList from "./NovelList";
import NovelDetailPanel from "./NovelDetail";
import NewNovelPanel from "./NewNovelPanel";
import TrashBin from "./TrashBin";

export default function BookshelfContent() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const [novels, setNovels] = useState<NovelSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedNovel, setSelectedNovel] = useState<NovelDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [trashOpen, setTrashOpen] = useState(false);
  const isCreating = searchParams.get("create") === "1";

  const fetchNovels = useCallback(async () => {
    try {
      setLoading(true);
      const res = await apiGet<{ data: NovelSummary[] }>("/api/novels/list");
      setNovels(res.data);
    } catch {
      // silently handle
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchNovels();
  }, [fetchNovels]);

  const setCreateRoute = (enabled: boolean) => {
    const params = new URLSearchParams(searchParams.toString());
    if (enabled) {
      params.set("create", "1");
    } else {
      params.delete("create");
    }
    const query = params.toString();
    router.replace(query ? `${pathname}?${query}` : pathname, {
      scroll: false,
    });
  };

  const handleSelect = async (id: string) => {
    setSelectedId(id);
    setCreateRoute(false);
    try {
      const novel = await apiGet<NovelDetail>(`/api/novels/${id}`);
      setSelectedNovel(novel);
    } catch {
      setSelectedNovel(null);
    }
  };

  const handleNewNovel = () => {
    setSelectedId(null);
    setSelectedNovel(null);
    setCreateRoute(true);
  };

  const handleDelete = async (id: string) => {
    try {
      await apiDelete(`/api/novels/${id}`);
      await fetchNovels();
      if (selectedId === id) {
        setSelectedId(null);
        setSelectedNovel(null);
      }
    } catch {
      // handle error
    }
  };

  const handleBackToList = () => {
    setSelectedId(null);
    setSelectedNovel(null);
    setCreateRoute(false);
  };

  const mobilePanelOpen = isCreating || selectedId !== null;
  const listVisibility = mobilePanelOpen ? "hidden md:flex" : "flex";

  return (
    <div className="mx-auto flex h-[calc(100dvh-3.5rem)] max-w-7xl gap-0 p-3 sm:p-4 md:gap-4">
      {/* Left: Novel List (3/10) */}
      <div
        className={[
          listVisibility,
          "w-full min-w-0 flex-col md:w-[30%] md:min-w-[280px]",
        ].join(" ")}
      >
        <NovelList
          novels={novels}
          selectedId={selectedId}
          loading={loading}
          onSelect={handleSelect}
          onNewNovel={handleNewNovel}
          onOpenTrash={() => setTrashOpen(true)}
        />
      </div>

      {/* Right: Detail / New Panel (7/10) */}
      <div
        className={[
          mobilePanelOpen ? "flex" : "hidden md:flex",
          "min-w-0 flex-1 flex-col overflow-hidden",
        ].join(" ")}
      >
        {isCreating ? (
          <NewNovelPanel onCancel={handleBackToList} />
        ) : (
          <NovelDetailPanel
            novel={selectedNovel}
            onDelete={handleDelete}
            onBack={handleBackToList}
          />
        )}
      </div>

      {/* Trash Bin Modal */}
      <TrashBin
        open={trashOpen}
        onClose={() => setTrashOpen(false)}
        onRestored={fetchNovels}
      />
    </div>
  );
}
