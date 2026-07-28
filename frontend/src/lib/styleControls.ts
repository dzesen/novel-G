import type { StyleControls } from "@/types/novel";

export const CUSTOM_STYLE_NOTE_CHARACTER_LIMIT = 500;
export const STYLE_CONTROLS_TOTAL_CHARACTER_LIMIT = 520;

export const STYLE_CONTROL_OPTIONS = {
  narrative_person: ["first", "third"],
  narrative_distance: ["close", "medium", "omniscient"],
  pacing: ["tight", "balanced", "relaxed"],
  prose_density: ["sparse", "balanced", "rich"],
  dialogue_ratio: ["low", "medium", "high"],
  content_rating: ["general", "moderate", "mature"],
} as const satisfies Record<
  Exclude<keyof StyleControls, "custom_style_note">,
  readonly string[]
>;

export function normalizeStyleControls(value: unknown): StyleControls {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return {};
  }

  const source = value as Record<string, unknown>;
  const normalized: StyleControls = {};
  for (const [field, options] of Object.entries(STYLE_CONTROL_OPTIONS)) {
    const selected = source[field];
    if (
      typeof selected === "string" &&
      (options as readonly string[]).includes(selected)
    ) {
      (normalized as Record<string, string>)[field] = selected;
    }
  }
  if (typeof source.custom_style_note === "string") {
    const noteLimit = getCustomStyleNoteLimit(normalized);
    const note = source.custom_style_note.slice(
      0,
      noteLimit,
    );
    if (note.length > 0) {
      normalized.custom_style_note = note;
    }
  }
  return normalized;
}

export function getCustomStyleNoteLimit(value: StyleControls): number {
  const enumCharacters = Object.entries(value).reduce(
    (total, [field, selected]) =>
      field !== "custom_style_note" && typeof selected === "string"
        ? total + selected.length
        : total,
    0,
  );
  return Math.min(
    CUSTOM_STYLE_NOTE_CHARACTER_LIMIT,
    Math.max(0, STYLE_CONTROLS_TOTAL_CHARACTER_LIMIT - enumCharacters),
  );
}

export function hasStyleControls(value: StyleControls): boolean {
  return Object.values(value).some(
    (item) => typeof item === "string" && item.trim().length > 0,
  );
}
