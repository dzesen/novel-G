import type { IllustrationPromptResult } from "@/types/agent";

export const ILLUSTRATION_FIELD_LIMITS = {
  subject: 1200,
  appearance: 1200,
  scene: 1600,
  style: 800,
  negative: 800,
} as const satisfies Record<keyof IllustrationPromptResult, number>;

export const ILLUSTRATION_TOTAL_LIMIT = 4800;

export function illustrationPromptCharacterCount(
  prompt: IllustrationPromptResult,
): number {
  return Object.values(prompt).reduce(
    (total, value) => total + Array.from(value).length,
    0,
  );
}

export function constrainIllustrationPromptEdit(
  prompt: IllustrationPromptResult,
  field: keyof IllustrationPromptResult,
  rawValue: string,
): IllustrationPromptResult {
  const otherCharacterCount = Object.entries(prompt).reduce(
    (total, [key, value]) =>
      key === field ? total : total + Array.from(value).length,
    0,
  );
  const available = Math.max(
    0,
    ILLUSTRATION_TOTAL_LIMIT - otherCharacterCount,
  );
  const limit = Math.min(ILLUSTRATION_FIELD_LIMITS[field], available);
  const value = Array.from(rawValue).slice(0, limit).join("");
  return { ...prompt, [field]: value };
}
