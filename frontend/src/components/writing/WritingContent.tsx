"use client";

import { useCallback, useEffect, useState } from "react";
import { usePathname, useSearchParams } from "next/navigation";
import type { ContinuityEvidenceReference } from "@/types/agent";
import type { ReferenceCardType, WritingSidebarItem } from "@/types/novel";
import WritingSidebar from "./WritingSidebar";
import NovelInfoWorkspace from "./novel-info/NovelInfoWorkspace";
import FactionCardsWorkspace from "./factions/FactionCardsWorkspace";
import ChapterWorkspace from "./chapters/ChapterWorkspace";
import type { GenerationRunsNavigationTarget } from "./chapters/batch/batchTypes";
import ReferenceCardsDestination from "./reference-cards/ReferenceCardsDestination";
import RelationshipWorkspace from "./relationships/RelationshipWorkspace";
import PlotThreadWorkspace from "./plot-threads/PlotThreadWorkspace";
import CharacterMemoryWorkspace from "./character-memory/CharacterMemoryWorkspace";
import StoryHealthWorkspace from "./story-health/StoryHealthWorkspace";
import AgentStudioWorkspace from "./agents/AgentStudioWorkspace";

interface WritingContentProps {
  mode: "create" | "edit";
  novelId?: string;
}

type NonReferenceSidebarItem = Exclude<
  WritingSidebarItem,
  "reference-cards"
>;

const REFERENCE_CARD_TYPES: ReferenceCardType[] = [
  "character",
  "location",
  "item",
  "rule",
  "lore",
];

function parseReferenceCardType(value: string | null): ReferenceCardType | null {
  return REFERENCE_CARD_TYPES.find((cardType) => cardType === value) ?? null;
}

export default function WritingContent({ mode, novelId }: WritingContentProps) {
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const requestedCardType =
    mode === "edit"
      ? parseReferenceCardType(searchParams.get("cardType"))
      : null;
  const requestedCardCuration =
    mode === "edit" && searchParams.get("curateCards") === "1";
  const requestedCandidateReview =
    mode === "edit" && searchParams.get("reviewCards") === "1";
  const requestedGenerationRuns =
    mode === "edit" && searchParams.get("view") === "generation-runs";
  const requestedGenerationRunsTarget: GenerationRunsNavigationTarget = {
    jobId: searchParams.get("job") || undefined,
    chapterId: searchParams.get("chapter") || undefined,
    eventId: searchParams.get("event") || undefined,
  };
  const [openCardCuration, setOpenCardCuration] = useState(requestedCardCuration);
  const [activeItem, setActiveItem] =
    useState<NonReferenceSidebarItem>("novel-info");
  const [evidenceReference, setEvidenceReference] =
    useState<ContinuityEvidenceReference | null>(null);
  const referenceCardType = requestedCardType ?? "character";
  const resolvedActiveItem: WritingSidebarItem =
    requestedGenerationRuns ? "chapter-editor"
      : requestedCardCuration || requestedCandidateReview || requestedCardType
      ? "reference-cards"
      : activeItem;

  const replaceCardTypeQuery = useCallback(
    (
      cardType: ReferenceCardType | null,
      reviewCandidates = false,
    ) => {
      const currentSearch = window.location.search.replace(/^\?/, "");
      const next = new URLSearchParams(currentSearch);
      next.delete("curateCards");
      if (cardType) next.set("cardType", cardType);
      else next.delete("cardType");
      if (reviewCandidates) next.set("reviewCards", "1");
      else next.delete("reviewCards");

      const nextSearch = next.toString();
      const nextHref = nextSearch ? `${pathname}?${nextSearch}` : pathname;
      const currentHref = currentSearch
        ? `${pathname}?${currentSearch}`
        : pathname;
      if (nextHref !== currentHref) {
        window.history.replaceState(null, "", nextHref);
      }
    },
    [pathname],
  );

  const replaceGenerationRunsQuery = useCallback(
    (target: GenerationRunsNavigationTarget | null) => {
      const currentSearch = window.location.search.replace(/^\?/, "");
      const next = new URLSearchParams(currentSearch);
      if (target) {
        next.set("view", "generation-runs");
        if (target.jobId) next.set("job", target.jobId);
        else next.delete("job");
        if (target.chapterId) next.set("chapter", target.chapterId);
        else next.delete("chapter");
        if (target.eventId) next.set("event", target.eventId);
        else next.delete("event");
      } else {
        next.delete("view");
        next.delete("job");
        next.delete("chapter");
        next.delete("event");
      }

      const nextSearch = next.toString();
      const nextHref = nextSearch ? `${pathname}?${nextSearch}` : pathname;
      const currentHref = currentSearch
        ? `${pathname}?${currentSearch}`
        : pathname;
      if (nextHref !== currentHref) {
        window.history.replaceState(null, "", nextHref);
      }
    },
    [pathname],
  );

  const navigateToWorkspace = useCallback(
    (item: NonReferenceSidebarItem) => {
      setEvidenceReference(null);
      setOpenCardCuration(false);
      setActiveItem(item);
      replaceCardTypeQuery(null);
      replaceGenerationRunsQuery(null);
    },
    [replaceCardTypeQuery, replaceGenerationRunsQuery],
  );

  const navigateToGenerationRuns = useCallback(
    (target: GenerationRunsNavigationTarget = {}) => {
      setEvidenceReference(null);
      setOpenCardCuration(false);
      setActiveItem("chapter-editor");
      replaceCardTypeQuery(null);
      replaceGenerationRunsQuery(target);
    },
    [replaceCardTypeQuery, replaceGenerationRunsQuery],
  );

  const closeGenerationRuns = useCallback(() => {
    setActiveItem("chapter-editor");
    replaceCardTypeQuery(null);
    replaceGenerationRunsQuery(null);
  }, [replaceCardTypeQuery, replaceGenerationRunsQuery]);

  const navigateToReferenceCards = useCallback(
    (cardType: ReferenceCardType, openCuration = false) => {
      setEvidenceReference(null);
      setOpenCardCuration(openCuration);
      replaceCardTypeQuery(cardType);
      replaceGenerationRunsQuery(null);
    },
    [replaceCardTypeQuery, replaceGenerationRunsQuery],
  );

  const navigateToReferenceCardCandidates = useCallback(() => {
    setEvidenceReference(null);
    setOpenCardCuration(false);
    replaceCardTypeQuery(referenceCardType, true);
    replaceGenerationRunsQuery(null);
  }, [
    referenceCardType,
    replaceCardTypeQuery,
    replaceGenerationRunsQuery,
  ]);

  useEffect(() => {
    if (!requestedCardCuration) return;
    const cardType = requestedCardType ?? "character";
    replaceCardTypeQuery(cardType);
  }, [
    replaceCardTypeQuery,
    requestedCardCuration,
    requestedCardType,
  ]);

  const renderMainArea = () => {
    if (resolvedActiveItem === "novel-info") {
      return <NovelInfoWorkspace mode={mode} novelId={novelId} />;
    }
    if (resolvedActiveItem === "faction-cards") {
      return <FactionCardsWorkspace mode={mode} novelId={novelId} />;
    }
    if (resolvedActiveItem === "chapter-editor") {
      return (
        <ChapterWorkspace
          mode={mode}
          novelId={novelId}
          onNavigateToMemory={() => navigateToWorkspace("character-memory")}
          onNavigateToReferenceCards={() =>
            navigateToReferenceCards("character", true)
          }
          onNavigateToPlotThreads={() => navigateToWorkspace("plot-threads")}
          initialChapterId={evidenceReference?.chapter_id}
          initialSceneIndex={evidenceReference?.scene_index}
          generationRunsOpen={requestedGenerationRuns}
          onNavigateToReferenceCardCandidates={
            navigateToReferenceCardCandidates
          }
          generationRunsTarget={requestedGenerationRunsTarget}
          onOpenGenerationRuns={navigateToGenerationRuns}
          onCloseGenerationRuns={closeGenerationRuns}
        />
      );
    }
    if (resolvedActiveItem === "agent-studio") {
      return (
        <AgentStudioWorkspace
          mode={mode}
          novelId={novelId}
          onNavigateReference={(reference) => {
            if (reference.kind === "fact") {
              navigateToWorkspace("character-memory");
            } else if (reference.kind === "thread") {
              navigateToWorkspace("plot-threads");
            } else {
              navigateToWorkspace("chapter-editor");
            }
            setEvidenceReference(reference);
          }}
        />
      );
    }
    if (resolvedActiveItem === "reference-cards") {
      return (
        <ReferenceCardsDestination
          mode={mode}
          novelId={novelId}
          cardType={referenceCardType}
          onCardTypeChange={(cardType) =>
            navigateToReferenceCards(cardType)
          }
          openCurationOnMount={
            openCardCuration || requestedCardCuration
          }
          onCurationOpened={() => setOpenCardCuration(false)}
          reviewCandidatesOnMount={requestedCandidateReview}
          onCandidateReviewChange={(open) => {
            if (open) navigateToReferenceCardCandidates();
            else replaceCardTypeQuery(referenceCardType);
          }}
        />
      );
    }
    if (resolvedActiveItem === "relationship-map") {
      return <RelationshipWorkspace mode={mode} novelId={novelId} />;
    }
    if (resolvedActiveItem === "plot-threads") {
      return (
        <PlotThreadWorkspace
          mode={mode}
          novelId={novelId}
          initialThreadId={evidenceReference?.thread_id}
        />
      );
    }
    if (resolvedActiveItem === "story-health") {
      return <StoryHealthWorkspace mode={mode} novelId={novelId} />;
    }
    if (resolvedActiveItem === "character-memory") {
      return (
        <CharacterMemoryWorkspace
          mode={mode}
          novelId={novelId}
          initialFactId={evidenceReference?.fact_id}
        />
      );
    }
    const unreachable: never = resolvedActiveItem;
    return unreachable;
  };

  return (
    <div className="flex h-[calc(100vh-3.5rem)] flex-col md:flex-row">
      <WritingSidebar
        activeItem={resolvedActiveItem}
        onSelect={(item) => {
          if (item === "reference-cards") {
            navigateToReferenceCards(referenceCardType);
          } else {
            navigateToWorkspace(item);
          }
        }}
      />
      <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
        {renderMainArea()}
      </div>
    </div>
  );
}
