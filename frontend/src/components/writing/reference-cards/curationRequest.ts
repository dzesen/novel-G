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
