import type {
  WorldBaselineView,
  WorldBaselineDecision,
  WorldBaselineDecisions,
  WorldBaselinePendingDecisions,
} from "@/types/novel";

export const WORLD_BASELINE_DOMAINS = [
  "character",
  "location",
  "item",
  "rule",
  "lore",
  "factions",
  "relationships",
] as const;

const WORLD_BASELINE_DECISION_VALUES = new Set<WorldBaselineDecision>([
  "reviewed",
  "not_applicable",
]);

export function hasCompleteWorldBaselineDecisions(
  decisions: Readonly<Record<string, unknown>>,
): decisions is WorldBaselineDecisions {
  return (
    Object.keys(decisions).length === WORLD_BASELINE_DOMAINS.length &&
    WORLD_BASELINE_DOMAINS.every((domain) =>
      WORLD_BASELINE_DECISION_VALUES.has(
        decisions[domain] as WorldBaselineDecision,
      ),
    )
  );
}

export function countPendingWorldBaselineDecisions(
  pending: WorldBaselinePendingDecisions,
): number {
  return (
    pending.reference_card_proposals +
    pending.emergent_candidates +
    pending.card_import_proposals
  );
}


export function reusableWorldBaselineDecisions(baseline: WorldBaselineView) {
  return baseline.reusable_decisions
    ?? (baseline.state === "current" ? baseline.decisions : {});
}
