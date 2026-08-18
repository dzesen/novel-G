"use client";

import { useTranslations } from "next-intl";
import {
  WRITING_REFERENCE_CARD_TYPES,
  type WritingRouteTargets,
  type WritingTargetKey,
  type WritingView,
} from "@/lib/writingRoute";
import type { ReferenceCardType } from "@/types/novel";
import WorkspaceViewTabs, {
  type WorkspaceViewTab,
} from "../WorkspaceViewTabs";
import FactionCardsWorkspace from "../factions/FactionCardsWorkspace";
import ReferenceCardsDestination from "../reference-cards/ReferenceCardsDestination";
import RelationshipWorkspace from "../relationships/RelationshipWorkspace";

interface WorldWorkspaceProps {
  novelId: string;
  view: WritingView;
  targets: WritingRouteTargets;
  onNavigateView: (
    view: WritingView,
    targets?: WritingRouteTargets,
    replace?: boolean,
  ) => void;
  onTargetValidation: (
    key: WritingTargetKey,
    value: string,
    valid: boolean,
  ) => void;
}

function parseReferenceCardType(value?: string): ReferenceCardType {
  return (
    WRITING_REFERENCE_CARD_TYPES.find((cardType) => cardType === value) ??
    "character"
  );
}

export default function WorldWorkspace({
  novelId,
  view,
  targets,
  onNavigateView,
  onTargetValidation,
}: WorldWorkspaceProps) {
  const t = useTranslations("writing.navigation");
  const referenceCardType = parseReferenceCardType(targets.cardType);
  const tabs: WorkspaceViewTab[] = [
    { view: "library", label: t("views.library") },
    { view: "factions", label: t("views.factions") },
    { view: "relationships", label: t("views.relationships") },
  ];
  const activeView = ["curation", "candidates"].includes(view)
    ? "library"
    : view;

  const content = (() => {
    if (view === "factions") {
      return (
        <FactionCardsWorkspace
          mode="edit"
          novelId={novelId}
          initialFactionId={targets.card}
          onTargetValidation={(factionId, valid) =>
            onTargetValidation("card", factionId, valid)
          }
        />
      );
    }
    if (view === "relationships") {
      return (
        <RelationshipWorkspace
          mode="edit"
          novelId={novelId}
          initialRelationId={targets.card}
          onTargetValidation={(relationId, valid) =>
            onTargetValidation("card", relationId, valid)
          }
        />
      );
    }
    return (
      <ReferenceCardsDestination
        mode="edit"
        novelId={novelId}
        cardType={referenceCardType}
        initialCardId={targets.card}
        initialCandidateId={targets.candidate}
        onTargetValidation={onTargetValidation}
        onCardTargetChange={(cardId) =>
          onNavigateView(
            "library",
            {
              cardType: referenceCardType,
              card: cardId,
            },
            true,
          )
        }
        onCardTypeChange={(cardType) =>
          onNavigateView(
            view === "candidates" ? "candidates" : "library",
            {
              cardType,
              card: undefined,
              candidate: undefined,
              visual: undefined,
            },
            true,
          )
        }
        openCurationOnMount={view === "curation"}
        reviewCandidatesOnMount={view === "candidates"}
        onCandidateReviewChange={(open) =>
          onNavigateView(
            open ? "candidates" : "library",
            { cardType: referenceCardType },
            true,
          )
        }
      />
    );
  })();

  return (
    <div className="flex h-full min-h-0 min-w-0 flex-col">
      <WorkspaceViewTabs
        label={t("viewAria")}
        activeView={activeView}
        tabs={tabs}
        onSelect={(nextView) =>
          onNavigateView(
            nextView,
            {
              cardType:
                nextView === "library" ? referenceCardType : undefined,
              card: undefined,
              candidate: undefined,
              visual: undefined,
            },
          )
        }
      />
      <div className="min-h-0 min-w-0 flex-1 overflow-hidden">{content}</div>
    </div>
  );
}
