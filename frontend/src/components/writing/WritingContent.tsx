"use client";

import { useState } from "react";
import type { WritingSidebarItem } from "@/types/novel";
import WritingSidebar from "./WritingSidebar";
import NovelInfoWorkspace from "./novel-info/NovelInfoWorkspace";
import FactionCardsWorkspace from "./factions/FactionCardsWorkspace";
import ChapterWorkspace from "./chapters/ChapterWorkspace";
import ReferenceCardsWorkspace from "./reference-cards/ReferenceCardsWorkspace";
import RelationshipWorkspace from "./relationships/RelationshipWorkspace";
import PlotThreadWorkspace from "./plot-threads/PlotThreadWorkspace";
import CharacterMemoryWorkspace from "./character-memory/CharacterMemoryWorkspace";

interface WritingContentProps {
  mode: "create" | "edit";
  novelId?: string;
}

export default function WritingContent({ mode, novelId }: WritingContentProps) {
  const [activeItem, setActiveItem] = useState<WritingSidebarItem>("novel-info");

  const renderMainArea = () => {
    if (activeItem === "novel-info") {
      return <NovelInfoWorkspace mode={mode} novelId={novelId} />;
    }
    if (activeItem === "faction-cards") {
      return <FactionCardsWorkspace mode={mode} novelId={novelId} />;
    }
    if (activeItem === "chapter-editor") {
      return <ChapterWorkspace mode={mode} novelId={novelId} />;
    }
    if (activeItem === "character-cards") {
      return <ReferenceCardsWorkspace key="character" mode={mode} novelId={novelId} cardType="character" />;
    }
    if (activeItem === "location-cards") {
      return <ReferenceCardsWorkspace key="location" mode={mode} novelId={novelId} cardType="location" />;
    }
    if (activeItem === "item-cards") {
      return <ReferenceCardsWorkspace key="item" mode={mode} novelId={novelId} cardType="item" />;
    }
    if (activeItem === "rule-cards") {
      return <ReferenceCardsWorkspace key="rule" mode={mode} novelId={novelId} cardType="rule" />;
    }
    if (activeItem === "relationship-map") {
      return <RelationshipWorkspace mode={mode} novelId={novelId} />;
    }
    if (activeItem === "plot-threads") {
      return <PlotThreadWorkspace mode={mode} novelId={novelId} />;
    }
    if (activeItem === "character-memory") {
      return <CharacterMemoryWorkspace mode={mode} novelId={novelId} />;
    }
    const unreachable: never = activeItem;
    return unreachable;
  };

  return (
    <div className="flex h-[calc(100vh-3.5rem)] flex-col md:flex-row">
      <WritingSidebar activeItem={activeItem} onSelect={setActiveItem} />
      <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
        {renderMainArea()}
      </div>
    </div>
  );
}
