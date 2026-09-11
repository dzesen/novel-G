"use client";

import {
  useCallback,
  useEffect,
  useRef,
  type KeyboardEvent,
} from "react";
import { useTranslations } from "next-intl";
import {
  WRITING_REFERENCE_CARD_TYPES,
  type WritingTargetKey,
} from "@/lib/writingRoute";
import type { ReferenceCardType } from "@/types/novel";
import ReferenceCardsWorkspace from "./ReferenceCardsWorkspace";
import CandidateReviewWorkspace from "./CandidateReviewWorkspace";

const CARD_TYPES: ReferenceCardType[] = [...WRITING_REFERENCE_CARD_TYPES];

type ReferenceCardsTab = ReferenceCardType | "candidates";
const EDIT_TABS: ReferenceCardsTab[] = [...CARD_TYPES, "candidates"];
interface ReferenceCardsDestinationProps {
  mode: "create" | "edit";
  novelId?: string;
  cardType: ReferenceCardType;
  initialCardId?: string;
  initialCandidateId?: string;
  onCardTargetChange?: (cardId?: string) => void;
  onTargetValidation: (
    key: WritingTargetKey,
    value: string,
    valid: boolean,
  ) => void;
  onCardTypeChange: (cardType: ReferenceCardType) => void;
  openCurationOnMount?: boolean;
  reviewCandidatesOnMount?: boolean;
  onCandidateReviewChange?: (open: boolean) => void;
  onCurationOpened?: () => void;
}

export default function ReferenceCardsDestination({
  mode,
  novelId,
  cardType,
  initialCardId,
  initialCandidateId,
  onCardTargetChange,
  onTargetValidation,
  onCardTypeChange,
  openCurationOnMount = false,
  onCurationOpened,
  reviewCandidatesOnMount = false,
  onCandidateReviewChange,
}: ReferenceCardsDestinationProps) {
  const t = useTranslations("writing.referenceCards");
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const tabs = mode === "edit" ? EDIT_TABS : CARD_TYPES;
  const activeTab: ReferenceCardsTab =
    reviewCandidatesOnMount ? "candidates" : cardType;
  const validateCardTarget = useCallback(
    (cardId: string, valid: boolean) =>
      onTargetValidation("card", cardId, valid),
    [onTargetValidation],
  );
  const validateCandidateTarget = useCallback(
    (candidateId: string, valid: boolean) =>
      onTargetValidation("candidate", candidateId, valid),
    [onTargetValidation],
  );

  useEffect(() => {
    const selectedIndex = tabs.indexOf(activeTab);
    tabRefs.current[selectedIndex]?.scrollIntoView({
      block: "nearest",
      inline: "nearest",
    });
  }, [activeTab, tabs]);

  const selectTab = (tab: ReferenceCardsTab) => {
    if (tab === "candidates") {
      onCandidateReviewChange?.(true);
      return;
    }
    // The card-type callback changes the view and type in one navigation.
    onCardTypeChange(tab);
  };

  const handleTabKeyDown = (
    event: KeyboardEvent<HTMLButtonElement>,
    index: number,
  ) => {
    let nextIndex: number | null = null;
    if (event.key === "ArrowRight") {
      nextIndex = (index + 1) % tabs.length;
    } else if (event.key === "ArrowLeft") {
      nextIndex = (index - 1 + tabs.length) % tabs.length;
    } else if (event.key === "Home") {
      nextIndex = 0;
    } else if (event.key === "End") {
      nextIndex = tabs.length - 1;
    }
    if (nextIndex === null) return;

    event.preventDefault();
    tabRefs.current[nextIndex]?.focus();
    selectTab(tabs[nextIndex]);
  };

  return (
    <div className="flex h-full min-h-0 min-w-0 flex-col bg-background">
      <div className="min-w-0 shrink-0 overflow-hidden border-b border-border bg-background px-4 py-2">
        <div
          className="flex w-full max-w-full gap-1 overflow-x-auto rounded-lg border border-border bg-surface p-1"
          role="tablist"
          aria-orientation="horizontal"
          aria-label={t("typeSwitcherLabel")}
        >
          {tabs.map((item, index) => (
            <button
              id={`reference-card-type-${item}`}
              key={item}
              ref={(element) => {
                tabRefs.current[index] = element;
              }}
              type="button"
              role="tab"
              aria-selected={activeTab === item}
              aria-controls="reference-card-panel"
              tabIndex={activeTab === item ? 0 : -1}
              onClick={() => selectTab(item)}
              onKeyDown={(event) => handleTabKeyDown(event, index)}
              className={`min-h-11 shrink-0 whitespace-nowrap rounded-md px-3 py-2 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent ${
                activeTab === item
                  ? item === "candidates"
                    ? "bg-amber-700 text-white dark:bg-amber-600"
                    : "bg-accent text-white"
                  : "text-muted hover:bg-surface-secondary hover:text-foreground"
              }`}
            >
              {item === "candidates"
                ? t("candidateReview.tab")
                : t(`types.${item}`)}
            </button>
          ))}
        </div>
      </div>
      <div
        id="reference-card-panel"
        role="tabpanel"
        aria-labelledby={`reference-card-type-${activeTab}`}
        className="min-h-0 min-w-0 flex-1"
      >
        {activeTab === "candidates" && novelId ? (
          <CandidateReviewWorkspace
            novelId={novelId}
            initialCandidateId={initialCandidateId}
            onTargetValidation={validateCandidateTarget}
          />
        ) : (
          <ReferenceCardsWorkspace
            key={cardType}
            mode={mode}
            novelId={novelId}
            cardType={cardType}
            initialCardId={initialCardId}
            onCardTargetChange={onCardTargetChange}
            onTargetValidation={validateCardTarget}
            openCurationOnMount={openCurationOnMount}
            onCurationOpened={onCurationOpened}
          />
        )}
      </div>
    </div>
  );
}
