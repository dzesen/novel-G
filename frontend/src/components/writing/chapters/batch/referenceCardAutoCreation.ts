export const REFERENCE_CARD_TYPES = [
  "character",
  "location",
  "item",
  "rule",
  "lore",
] as const;

export type ReferenceCardType = (typeof REFERENCE_CARD_TYPES)[number];

export interface ReferenceCardAutoCreationPolicy {
  enabled: boolean;
  allowed_card_types: ReferenceCardType[];
  max_auto_creates_per_chapter: number;
  max_auto_creates_per_book: number;
  max_candidate_repair_cycles_per_chapter: number;
}

export const REFERENCE_CARD_AUTO_CREATION_HARD_LIMITS = {
  perChapter: 3,
  perBook: 20,
  repairCyclesPerChapter: 2,
} as const;

export const DEFAULT_REFERENCE_CARD_AUTO_CREATION_POLICY: ReferenceCardAutoCreationPolicy = {
  enabled: false,
  allowed_card_types: [...REFERENCE_CARD_TYPES],
  max_auto_creates_per_chapter: REFERENCE_CARD_AUTO_CREATION_HARD_LIMITS.perChapter,
  max_auto_creates_per_book: REFERENCE_CARD_AUTO_CREATION_HARD_LIMITS.perBook,
  max_candidate_repair_cycles_per_chapter: 0,
};

export function initialReferenceCardAutoCreationPolicy(
  preferWorldAutoSupplement = false,
): ReferenceCardAutoCreationPolicy {
  return {
    ...DEFAULT_REFERENCE_CARD_AUTO_CREATION_POLICY,
    enabled: preferWorldAutoSupplement,
    allowed_card_types: [
      ...DEFAULT_REFERENCE_CARD_AUTO_CREATION_POLICY.allowed_card_types,
    ],
  };
}
