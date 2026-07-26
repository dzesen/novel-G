import type {
  CoreFactionsPayload,
  GeneratedCoreFaction,
  GeneratedFactionRelation,
} from "../../../types/novel";

function cleanList(values: string[] | undefined): string[] {
  return (values ?? []).map((value) => value.trim()).filter(Boolean);
}

function normalizeFaction(faction: GeneratedCoreFaction): GeneratedCoreFaction {
  return {
    name: faction.name.trim(),
    faction_type: faction.faction_type.trim(),
    positioning: faction.positioning.trim(),
    public_stance: faction.public_stance.trim(),
    core_goal: faction.core_goal.trim(),
    hidden_goal: faction.hidden_goal?.trim() ?? "",
    resources_and_advantages: cleanList(faction.resources_and_advantages),
    organization_style: faction.organization_style.trim(),
    core_values: cleanList(faction.core_values),
    conflict_with_mainline: faction.conflict_with_mainline.trim(),
    is_public: faction.is_public ?? true,
    influence_scope: faction.influence_scope.trim(),
    expandability: faction.expandability.trim(),
    tags: cleanList(faction.tags),
  };
}

function normalizeRelation(
  relation: GeneratedFactionRelation,
): GeneratedFactionRelation {
  return {
    source_faction_name: relation.source_faction_name.trim(),
    target_faction_name: relation.target_faction_name.trim(),
    relation_type: relation.relation_type,
    current_state: relation.current_state.trim(),
    core_conflict: relation.core_conflict.trim(),
    hidden_tension: relation.hidden_tension?.trim() ?? "",
    possible_change: relation.possible_change.trim(),
    intensity: relation.intensity ?? 3,
    is_active: relation.is_active ?? true,
  };
}

/**
 * Convert an LLM response into editable preview state without mixing in
 * persistence-only fields rejected by the strict bulk-create schema.
 */
export function normalizeGeneratedPayload(
  payload: CoreFactionsPayload,
): CoreFactionsPayload {
  return {
    core_factions: payload.core_factions.map(normalizeFaction),
    faction_relations: payload.faction_relations.map(normalizeRelation),
  };
}

/**
 * Rebuild the request at the API boundary. This keeps accidental database/UI
 * metadata out even if preview state was populated from a wider object.
 */
export function buildCoreFactionsSaveRequest(
  preview: CoreFactionsPayload,
): CoreFactionsPayload {
  return normalizeGeneratedPayload(preview);
}
