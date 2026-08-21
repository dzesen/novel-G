export interface MissingCharacterMetadataMessages {
  aiGeneratedIllustration: string;
  metadataStripped: string;
  otherTextMetadata: (keywords: string) => string;
  generationPreset: string;
}

type MissingCharacterMetadataKind =
  | "ai_generation_metadata"
  | "no_text_chunks"
  | "other_text_chunks";

interface MissingCharacterMetadataDetail {
  code?: unknown;
  missing_metadata_kind?: unknown;
  text_keywords?: unknown;
}

const GENERATION_PRESET_ROUTING_CODES = new Set([
  "generation_preset_requires_generation_roles",
  // Keep recognizing responses from an older backend during rolling upgrades.
  "generation_preset_requires_agent_studio",
]);

export function isGenerationPresetRoutingError(error: unknown): boolean {
  if (!error || typeof error !== "object") return false;
  const detail = (error as { detail?: unknown }).detail;
  if (!detail || typeof detail !== "object" || Array.isArray(detail)) {
    return false;
  }
  return GENERATION_PRESET_ROUTING_CODES.has(
    String((detail as { code?: unknown }).code ?? ""),
  );
}

function missingCharacterMetadataDetail(
  error: unknown,
): MissingCharacterMetadataDetail | null {
  if (!error || typeof error !== "object") return null;
  const detail = (error as { detail?: unknown }).detail;
  if (!detail || typeof detail !== "object" || Array.isArray(detail)) {
    return null;
  }
  const structured = detail as MissingCharacterMetadataDetail;
  return structured.code === "missing_character_metadata" ? structured : null;
}

export function cardImportErrorMessage(
  error: unknown,
  fallback: string,
  messages: MissingCharacterMetadataMessages,
): string {
  if (isGenerationPresetRoutingError(error)) {
    return messages.generationPreset;
  }
  const detail = missingCharacterMetadataDetail(error);
  if (!detail) return fallback;

  const kind = detail.missing_metadata_kind as
    | MissingCharacterMetadataKind
    | undefined;
  let reason: string;
  if (kind === "ai_generation_metadata") {
    reason = messages.aiGeneratedIllustration;
  } else if (kind === "no_text_chunks") {
    reason = messages.metadataStripped;
  } else if (kind === "other_text_chunks") {
    const keywords = Array.isArray(detail.text_keywords)
      ? detail.text_keywords.filter(
          (keyword): keyword is string =>
            typeof keyword === "string" && keyword.trim().length > 0,
        )
      : [];
    if (!keywords.length) return fallback;
    reason = messages.otherTextMetadata(keywords.join(", "));
  } else {
    return fallback;
  }

  return reason;
}
