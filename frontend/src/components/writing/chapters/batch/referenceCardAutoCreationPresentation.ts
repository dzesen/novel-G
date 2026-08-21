import type { ReferenceCardType } from "./referenceCardAutoCreation.ts";

const REFERENCE_CARD_TYPE_TRANSLATION_KEYS = {
  character: "dialogAutoCardsTypeCharacter",
  location: "dialogAutoCardsTypeLocation",
  item: "dialogAutoCardsTypeItem",
  rule: "dialogAutoCardsTypeRule",
  lore: "dialogAutoCardsTypeLore",
} as const satisfies Record<ReferenceCardType, string>;

export function referenceCardTypeTranslationKey(
  cardType: ReferenceCardType,
) {
  return REFERENCE_CARD_TYPE_TRANSLATION_KEYS[cardType];
}
