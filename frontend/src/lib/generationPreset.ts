import type { GenerationPresetPreview } from "@/types/generationPreset";

export const MAX_CUSTOM_AGENT_INSTRUCTION_CHARS = 12_000;
export const MAX_CUSTOM_AGENT_OUTPUT_TOKENS = 32_768;

export function presetSelectionKey(
  profileIndex: number,
  orderIndex: number,
): string {
  return `${profileIndex}:${orderIndex}`;
}

export function defaultPresetSelection(
  preview: GenerationPresetPreview,
  profileIndex: number,
): string[] {
  const profile = preview.order_profiles.find(
    (item) => item.profile_index === profileIndex,
  );
  if (!profile) return [];
  return profile.items
    .filter((item) => item.selected_by_default)
    .map((item) => presetSelectionKey(profileIndex, item.order_index));
}

export function composePresetInstruction(
  preview: GenerationPresetPreview,
  profileIndex: number,
  selectedKeys: ReadonlySet<string>,
): string {
  const profile = preview.order_profiles.find(
    (item) => item.profile_index === profileIndex,
  );
  if (!profile) return "";
  const contents = profile.items.flatMap((item) => {
    const prompt = item.prompt;
    if (
      !selectedKeys.has(
        presetSelectionKey(profileIndex, item.order_index),
      ) ||
      !prompt ||
      prompt.marker ||
      !prompt.content.trim()
    ) {
      return [];
    }
    return [prompt.content.trim()];
  });
  if (!contents.length) return "";
  return `${preview.instruction_prefix}\n\n${contents.join("\n\n")}`;
}

export function presetInstructionCharacterCount(value: string): number {
  return Array.from(value).length;
}
