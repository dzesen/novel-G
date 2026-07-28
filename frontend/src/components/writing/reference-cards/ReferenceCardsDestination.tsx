"use client";

import { useEffect, useRef, type KeyboardEvent } from "react";
import { useTranslations } from "next-intl";
import type { ReferenceCardType } from "@/types/novel";
import ReferenceCardsWorkspace from "./ReferenceCardsWorkspace";

const CARD_TYPES: ReferenceCardType[] = [
  "character",
  "location",
  "item",
  "rule",
  "lore",
];

interface ReferenceCardsDestinationProps {
  mode: "create" | "edit";
  novelId?: string;
  cardType: ReferenceCardType;
  onCardTypeChange: (cardType: ReferenceCardType) => void;
  openCurationOnMount?: boolean;
  onCurationOpened?: () => void;
}

export default function ReferenceCardsDestination({
  mode,
  novelId,
  cardType,
  onCardTypeChange,
  openCurationOnMount = false,
  onCurationOpened,
}: ReferenceCardsDestinationProps) {
  const t = useTranslations("writing.referenceCards");
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);

  useEffect(() => {
    const selectedIndex = CARD_TYPES.indexOf(cardType);
    tabRefs.current[selectedIndex]?.scrollIntoView({
      block: "nearest",
      inline: "nearest",
    });
  }, [cardType]);

  const handleTabKeyDown = (
    event: KeyboardEvent<HTMLButtonElement>,
    index: number,
  ) => {
    let nextIndex: number | null = null;
    if (event.key === "ArrowRight") {
      nextIndex = (index + 1) % CARD_TYPES.length;
    } else if (event.key === "ArrowLeft") {
      nextIndex = (index - 1 + CARD_TYPES.length) % CARD_TYPES.length;
    } else if (event.key === "Home") {
      nextIndex = 0;
    } else if (event.key === "End") {
      nextIndex = CARD_TYPES.length - 1;
    }
    if (nextIndex === null) return;

    event.preventDefault();
    tabRefs.current[nextIndex]?.focus();
    onCardTypeChange(CARD_TYPES[nextIndex]);
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
          {CARD_TYPES.map((item, index) => (
            <button
              id={`reference-card-type-${item}`}
              key={item}
              ref={(element) => {
                tabRefs.current[index] = element;
              }}
              type="button"
              role="tab"
              aria-selected={cardType === item}
              aria-controls="reference-card-panel"
              tabIndex={cardType === item ? 0 : -1}
              onClick={() => onCardTypeChange(item)}
              onKeyDown={(event) => handleTabKeyDown(event, index)}
              className={`min-h-11 shrink-0 whitespace-nowrap rounded-md px-3 py-2 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent ${
                cardType === item
                  ? "bg-accent text-white"
                  : "text-muted hover:bg-surface-secondary hover:text-foreground"
              }`}
            >
              {t(`types.${item}`)}
            </button>
          ))}
        </div>
      </div>
      <div
        id="reference-card-panel"
        role="tabpanel"
        aria-labelledby={`reference-card-type-${cardType}`}
        className="min-h-0 min-w-0 flex-1"
      >
        <ReferenceCardsWorkspace
          key={cardType}
          mode={mode}
          novelId={novelId}
          cardType={cardType}
          openCurationOnMount={openCurationOnMount}
          onCurationOpened={onCurationOpened}
        />
      </div>
    </div>
  );
}
