export type GenerationStepKind =
  | "outline"
  | "prose"
  | "outline_adherence"
  | "state"
  | "job";

export type ContextSectionKind =
  | "core_settings"
  | "chapter_outline"
  | "present_cards"
  | "present_states"
  | "portrayal_context"
  | "dialogue_examples"
  | "permanent_facts"
  | "volume"
  | "recent_chapters"
  | "threads_to_resolve"
  | "other_threads"
  | "minor_cards"
  | "roster"
  | "faction_index"
  | "faction_cards"
  | "world_entry_index"
  | "other";

export type ReferenceKind =
  | "character_card"
  | "pov_character"
  | "present_character"
  | "mentioned_character"
  | "faction"
  | "world_entry"
  | "resolved_thread"
  | "thread_due_chapter"
  | "character_state"
  | "thread_state"
  | "reference";

export type ReferenceActionTarget = "reference_cards" | "plot_threads" | "factions";
export type ReferenceMatchKind = "name" | "alias" | "existing_record";

export type ReferenceCardFieldKind =
  | "name"
  | "subtitle"
  | "description"
  | "importance"
  | "tags"
  | "details"
  | "character_profile"
  | "aliases"
  | "portrayal_context"
  | "portrayal_notes"
  | "dialogue_examples"
  | "scene_opening_examples"
  | "role"
  | "age"
  | "appearance"
  | "personality"
  | "motivation"
  | "arc"
  | "abilities"
  | "relationships"
  | "category"
  | "atmosphere"
  | "geography"
  | "history"
  | "story_importance"
  | "function"
  | "dangers"
  | "origin"
  | "limitations"
  | "owner"
  | "principle"
  | "scope"
  | "cost"
  | "exceptions"
  | "examples"
  | "era"
  | "background"
  | "story_relevance"
  | "related_entities"
  | "uncertainties"
  | "other";

export type WorldBookBehaviorKind =
  | "matching"
  | "priority"
  | "selective"
  | "vectorized"
  | "probability"
  | "recursion_control"
  | "depth_role"
  | "grouping"
  | "matching_source"
  | "character_filter"
  | "trigger_type"
  | "automation"
  | "timed_effects"
  | "extension_behavior"
  | "other";

export interface ReferenceCleanupPresentation {
  kind: ReferenceKind;
  count: number;
  readableValues: string[];
  actionTarget: ReferenceActionTarget;
}

export interface ReferenceRemapPresentation {
  kind: ReferenceKind;
  source: string | null;
  targetName: string | null;
  matchedBy: ReferenceMatchKind;
}

const CONTEXT_SECTIONS = new Set<ContextSectionKind>([
  "core_settings",
  "chapter_outline",
  "present_cards",
  "present_states",
  "portrayal_context",
  "dialogue_examples",
  "permanent_facts",
  "volume",
  "recent_chapters",
  "threads_to_resolve",
  "other_threads",
  "minor_cards",
  "roster",
  "faction_index",
  "faction_cards",
  "world_entry_index",
]);

export function generationStepKind(value: unknown): GenerationStepKind {
  if (value === "outline" || value === "prose" || value === "state") return value;
  return value === "outline_adherence" ? "outline_adherence" : "job";
}

export function contextSectionKind(value: unknown): ContextSectionKind {
  const normalized = String(value ?? "") as ContextSectionKind;
  return CONTEXT_SECTIONS.has(normalized) ? normalized : "other";
}

export function referenceFieldKind(value: unknown): ReferenceKind {
  const field = String(value ?? "");
  if (field === "pov_character_card_id") return "pov_character";
  if (field === "present_character_card_ids") return "present_character";
  if (field === "mentioned_character_card_ids") return "mentioned_character";
  if (field === "referenced_faction_card_ids") return "faction";
  if (field === "referenced_worldbook_card_ids") return "world_entry";
  if (field === "threads_resolved") return "resolved_thread";
  if (/^new_threads\[\d+\]\.due_target\.chapter_id$/.test(field)) {
    return "thread_due_chapter";
  }
  if (field === "character_updates") return "character_state";
  if (field === "thread_updates" || field === "accepted_thread_updates") {
    return "thread_state";
  }
  return "reference";
}

export function referenceActionTarget(
  kind: ReferenceKind,
): ReferenceActionTarget {
  if (kind === "faction") return "factions";
  return kind === "resolved_thread"
    || kind === "thread_due_chapter"
    || kind === "thread_state"
    ? "plot_threads"
    : "reference_cards";
}

function asStrings(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value.map(String).map((item) => item.trim()).filter(Boolean);
  }
  const normalized = String(value ?? "").trim();
  return normalized ? [normalized] : [];
}

/**
 * Model-produced references are expected to be internal IDs.  Preserve an
 * actual human label when one slipped through validation, but never surface
 * opaque IDs, hashes, UUIDs, or implementation-style slugs as user guidance.
 */
export function readableReferenceValue(value: unknown): string | null {
  const text = String(value ?? "").trim();
  if (!text) return null;
  if (/^[0-9a-f]{20,}$/i.test(text)) return null;
  if (/^[0-9a-f]{8}-[0-9a-f-]{27,}$/i.test(text)) return null;
  if (/^[a-z0-9]+(?:[_-][a-z0-9]+)+$/i.test(text)) return null;
  if (/^[a-z0-9]{18,}$/i.test(text)) return null;
  return text.slice(0, 80);
}

const REFERENCE_CARD_FIELDS = new Set<ReferenceCardFieldKind>([
  "name", "subtitle", "description", "importance", "tags", "details",
  "character_profile", "aliases", "portrayal_context", "portrayal_notes",
  "dialogue_examples", "scene_opening_examples", "role", "age", "appearance",
  "personality", "motivation", "arc", "abilities", "relationships", "category",
  "atmosphere", "geography", "history", "story_importance", "function", "dangers",
  "origin", "limitations", "owner", "principle", "scope", "cost", "exceptions",
  "examples", "era", "background", "story_relevance", "related_entities",
  "uncertainties",
]);

export function referenceCardFieldKind(value: unknown): ReferenceCardFieldKind {
  const field = String(value ?? "");
  const leaf = field.split(".").at(-1) as ReferenceCardFieldKind | undefined;
  return leaf && REFERENCE_CARD_FIELDS.has(leaf) ? leaf : "other";
}

export function referenceMatchKind(value: unknown): ReferenceMatchKind {
  if (value === "name" || value === "exact_name") return "name";
  if (value === "alias" || value === "confirmed_alias") return "alias";
  return "existing_record";
}

export function worldBookBehaviorKind(value: unknown): WorldBookBehaviorKind {
  const category = String(value ?? "");
  if (category === "automation_id") return "automation";
  const supported = new Set<WorldBookBehaviorKind>([
    "matching", "priority", "selective", "vectorized", "probability",
    "recursion_control", "depth_role", "grouping", "matching_source",
    "character_filter", "trigger_type", "timed_effects", "extension_behavior",
  ]);
  return supported.has(category as WorldBookBehaviorKind)
    ? category as WorldBookBehaviorKind
    : "other";
}

export function referenceCleanupForDisplay(
  dropped: Record<string, unknown> | null | undefined,
): ReferenceCleanupPresentation[] {
  const grouped = new Map<ReferenceKind, ReferenceCleanupPresentation>();
  for (const [field, rawValues] of Object.entries(dropped ?? {})) {
    const values = asStrings(rawValues);
    if (values.length === 0) continue;
    const kind = referenceFieldKind(field);
    const existing = grouped.get(kind) ?? {
      kind,
      count: 0,
      readableValues: [],
      actionTarget: referenceActionTarget(kind),
    };
    existing.count += values.length;
    existing.readableValues = Array.from(new Set([
      ...existing.readableValues,
      ...values.map(readableReferenceValue).filter((item): item is string => Boolean(item)),
    ])).slice(0, 5);
    grouped.set(kind, existing);
  }
  return Array.from(grouped.values());
}

export function referenceRemapForDisplay(
  item: {
    field?: unknown;
    from?: unknown;
    to?: unknown;
    matched_by?: unknown;
  },
  nameById: Record<string, string> = {},
): ReferenceRemapPresentation {
  const targetId = String(item.to ?? "");
  return {
    kind: item.field === "character_updates"
      ? "character_card"
      : referenceFieldKind(item.field),
    source: readableReferenceValue(item.from),
    targetName: readableReferenceValue(nameById[targetId]),
    matchedBy: referenceMatchKind(item.matched_by),
  };
}

export function contextCountsForDisplay(
  counts: Record<string, number>,
): Partial<Record<ContextSectionKind, number>> {
  const result: Partial<Record<ContextSectionKind, number>> = {};
  for (const [section, rawCount] of Object.entries(counts)) {
    const count = Number(rawCount);
    if (!Number.isFinite(count) || count <= 0) continue;
    const kind = contextSectionKind(section);
    result[kind] = (result[kind] ?? 0) + count;
  }
  return result;
}
