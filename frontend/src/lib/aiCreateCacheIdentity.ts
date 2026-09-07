import type { BlueprintGenerationSource } from "@/types/novel";
import { normalizeAuthorConstraints } from "./authorInput.ts";

export type AICreateCacheInput = Omit<BlueprintGenerationSource, "schema_version">;

export interface AICreateCacheIdentityRecord {
  input: AICreateCacheInput;
}

/**
 * Cache identity includes the confirmed Creative Director Agent, version,
 * direction, and user adjustments so stale paid results are never resumed.
 */
export function isSameAICreateInput(
  record: AICreateCacheIdentityRecord | null,
  input: AICreateCacheInput,
): boolean {
  return (
    record?.input.user_idea === input.user_idea &&
    record.input.number_of_chapters === input.number_of_chapters &&
    record.input.words_per_chapter === input.words_per_chapter &&
    normalizeAuthorConstraints(record.input.author_constraints) !== null &&
    JSON.stringify(normalizeAuthorConstraints(record.input.author_constraints)) ===
      JSON.stringify(normalizeAuthorConstraints(input.author_constraints)) &&
    JSON.stringify(record.input.creative_direction) ===
      JSON.stringify(input.creative_direction) &&
    JSON.stringify(record.input.card_imports) ===
      JSON.stringify(input.card_imports)
  );
}
