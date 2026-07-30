import type { ReferenceCardType } from "@/types/novel";

export interface ReferenceCardCurationPrepareRequest {
  force_regenerate: boolean;
  card_types: ReferenceCardType[];
  max_tokens?: number;
}

export function buildReferenceCardCurationPrepareRequest(
  forceRegenerate: boolean,
  maxTokens: number | null,
  cardTypes: readonly ReferenceCardType[],
): ReferenceCardCurationPrepareRequest {
  if (cardTypes.length === 0) {
    throw new Error("At least one reference-card type must be selected");
  }
  if (new Set(cardTypes).size !== cardTypes.length) {
    throw new Error("Reference-card types must be unique");
  }
  const request: ReferenceCardCurationPrepareRequest = {
    force_regenerate: forceRegenerate,
    card_types: [...cardTypes],
  };
  if (maxTokens !== null) request.max_tokens = maxTokens;
  return request;
}

export function buildReferenceCardCurationDiscardPath(
  novelId: string,
  proposalId: string,
): string {
  return (
    `/api/reference-cards/novel/${encodeURIComponent(novelId)}/curation/` +
    `${encodeURIComponent(proposalId)}/discard`
  );
}

export function clearedReferenceCardCurationState() {
  return {
    proposal: null,
    decisions: {},
    result: null,
  } as const;
}
