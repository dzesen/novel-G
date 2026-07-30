export interface ReferenceCardCurationPrepareRequest {
  force_regenerate: boolean;
  max_tokens?: number;
}

export function buildReferenceCardCurationPrepareRequest(
  forceRegenerate: boolean,
  maxTokens: number | null,
): ReferenceCardCurationPrepareRequest {
  const request: ReferenceCardCurationPrepareRequest = {
    force_regenerate: forceRegenerate,
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
