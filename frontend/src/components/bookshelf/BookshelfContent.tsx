"use client";

import { useState, useEffect, useCallback } from "react";
import { apiGet, apiDelete } from "@/lib/api";
import type { NovelSummary, NovelDetail } from "@/types/novel";
import NovelList from "./NovelList";
import NovelDetailPanel from "./NovelDetail";
import NewNovelPanel from "./NewNovelPanel";
import CardDrivenCreatePanel from "./CardDrivenCreatePanel";
import TrashBin from "./TrashBin";

type RightPanel = "detail" | "new" | "card-new";

export default function BookshelfContent() {
  const [novels, setNovels] = useState<NovelSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedNovel, setSelectedNovel] = useState<NovelDetail | null>(null);
  const [rightPanel, setRightPanel] = useState<RightPanel>("detail");
  const [loading, setLoading] = useState(true);
  const [trashOpen, setTrashOpen] = useState(false);

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

  const handleSelect = async (id: string) => {
    setSelectedId(id);
    setRightPanel("detail");
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
    setRightPanel("new");
  };

  const handleCardDrivenNovel = () => {
    setSelectedId(null);
    setSelectedNovel(null);
    setRightPanel("card-new");
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

  const handleNovelCreated = async (novelId: string) => {
    await fetchNovels();
    await handleSelect(novelId);
  };

  const handleBackToList = () => {
    setSelectedId(null);
    setSelectedNovel(null);
    setRightPanel("detail");
  };

  const mobilePanelOpen =
    rightPanel !== "detail" || selectedId !== null;
  const listVisibility = mobilePanelOpen
    ? rightPanel === "card-new"
      ? "hidden lg:flex"
      : "hidden md:flex"
    : "flex";

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
          onCardDrivenNovel={handleCardDrivenNovel}
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
        {rightPanel === "new" ? (
          <NewNovelPanel
            onCreated={handleNovelCreated}
            onCancel={handleBackToList}
          />
        ) : rightPanel === "card-new" ? (
          <CardDrivenCreatePanel
            onCancel={handleBackToList}
          />
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
