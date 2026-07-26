import type { CreativeDirectionSelection } from "@/types/agent";

export interface AICreateCacheInput {
  user_idea: string;
  number_of_chapters: number;
  words_per_chapter: number;
  creative_direction: CreativeDirectionSelection | null;
}

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
    JSON.stringify(record.input.creative_direction) ===
      JSON.stringify(input.creative_direction)
  );
}
