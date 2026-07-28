"use client";

import { useEffect, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import type { ContinuityEvidenceReference } from "@/types/agent";
import type { WritingSidebarItem } from "@/types/novel";
import WritingSidebar from "./WritingSidebar";
import NovelInfoWorkspace from "./novel-info/NovelInfoWorkspace";
import FactionCardsWorkspace from "./factions/FactionCardsWorkspace";
import ChapterWorkspace from "./chapters/ChapterWorkspace";
import ReferenceCardsWorkspace from "./reference-cards/ReferenceCardsWorkspace";
import RelationshipWorkspace from "./relationships/RelationshipWorkspace";
import PlotThreadWorkspace from "./plot-threads/PlotThreadWorkspace";
import CharacterMemoryWorkspace from "./character-memory/CharacterMemoryWorkspace";
import StoryHealthWorkspace from "./story-health/StoryHealthWorkspace";
import AgentStudioWorkspace from "./agents/AgentStudioWorkspace";

interface WritingContentProps {
  mode: "create" | "edit";
  novelId?: string;
}

export default function WritingContent({ mode, novelId }: WritingContentProps) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const requestedCardCuration =
    mode === "edit" && searchParams.get("curateCards") === "1";
  const [openCardCuration, setOpenCardCuration] = useState(requestedCardCuration);
  const [activeItem, setActiveItem] = useState<WritingSidebarItem>(
    requestedCardCuration ? "character-cards" : "novel-info",
  );
  const [evidenceReference, setEvidenceReference] =
    useState<ContinuityEvidenceReference | null>(null);

  useEffect(() => {
    if (requestedCardCuration) {
      router.replace(pathname);
    }
  }, [pathname, requestedCardCuration, router]);

  const renderMainArea = () => {
    if (activeItem === "novel-info") {
      return <NovelInfoWorkspace mode={mode} novelId={novelId} />;
    }
    if (activeItem === "faction-cards") {
      return <FactionCardsWorkspace mode={mode} novelId={novelId} />;
    }
    if (activeItem === "chapter-editor") {
      return (
        <ChapterWorkspace
          mode={mode}
          novelId={novelId}
          onNavigateToMemory={() => setActiveItem("character-memory")}
          onNavigateToReferenceCards={() => {
            setOpenCardCuration(true);
            setActiveItem("character-cards");
          }}
          initialChapterId={evidenceReference?.chapter_id}
          initialSceneIndex={evidenceReference?.scene_index}
        />
      );
    }
    if (activeItem === "agent-studio") {
      return (
        <AgentStudioWorkspace
          mode={mode}
          novelId={novelId}
          onNavigateReference={(reference) => {
            setEvidenceReference(reference);
            if (reference.kind === "fact") {
              setActiveItem("character-memory");
            } else if (reference.kind === "thread") {
              setActiveItem("plot-threads");
            } else {
              setActiveItem("chapter-editor");
            }
          }}
        />
      );
    }
    if (activeItem === "character-cards") {
      return (
        <ReferenceCardsWorkspace
          key="character"
          mode={mode}
          novelId={novelId}
          cardType="character"
          openCurationOnMount={openCardCuration}
          onCurationOpened={() => setOpenCardCuration(false)}
        />
      );
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
    if (activeItem === "lore-cards") {
      return <ReferenceCardsWorkspace key="lore" mode={mode} novelId={novelId} cardType="lore" />;
    }
    if (activeItem === "relationship-map") {
      return <RelationshipWorkspace mode={mode} novelId={novelId} />;
    }
    if (activeItem === "plot-threads") {
      return (
        <PlotThreadWorkspace
          mode={mode}
          novelId={novelId}
          initialThreadId={evidenceReference?.thread_id}
        />
      );
    }
    if (activeItem === "story-health") {
      return <StoryHealthWorkspace mode={mode} novelId={novelId} />;
    }
    if (activeItem === "character-memory") {
      return (
        <CharacterMemoryWorkspace
          mode={mode}
          novelId={novelId}
          initialFactId={evidenceReference?.fact_id}
        />
      );
    }
    const unreachable: never = activeItem;
    return unreachable;
  };

  return (
    <div className="flex h-[calc(100vh-3.5rem)] flex-col md:flex-row">
      <WritingSidebar
        activeItem={activeItem}
        onSelect={(item) => {
          setEvidenceReference(null);
          setActiveItem(item);
        }}
      />
      <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
        {renderMainArea()}
      </div>
    </div>
  );
}
