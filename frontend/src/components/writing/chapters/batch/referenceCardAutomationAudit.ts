import type {
  GenerationJob,
  ReferenceCardAutoCreationEvent,
  ReferenceCardRepairEvent,
} from "./batchTypes.ts";
import {
  REFERENCE_CARD_TYPES,
  type ReferenceCardType,
} from "./referenceCardAutoCreation.ts";

export type ReferenceCardAutomationOutcome =
  | "auto_created"
  | "rewritten_unique_new"
  | "dependency_removed"
  | "manual_review_required"
  | "reverted";

export interface ReferenceCardAutomationAuditItem {
  eventId: string;
  chapterId: string;
  outcome: ReferenceCardAutomationOutcome;
  createdCount: number;
  candidateIds: string[];
  formalCardTargets: Array<{
    cardType: ReferenceCardType;
    cardId: string;
  }>;
  authorizationDigest: string;
  readinessDigest: string;
  authorizationRevision: number | null;
  policyRevision: number | null;
  sourceMutationId: string;
  mutationReceiptId: string;
  occurredAt: string;
}

type AutomationAuditSource = Pick<
  GenerationJob,
  "reference_card_auto_creation_events" | "reference_card_repair_events"
>;

function stringField(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function uniqueStrings(values: unknown[]): string[] {
  return [...new Set(values.map(stringField).filter(Boolean))];
}

function formalCardTargets(
  event: ReferenceCardAutoCreationEvent,
): ReferenceCardAutomationAuditItem["formalCardTargets"] {
  const targets: ReferenceCardAutomationAuditItem["formalCardTargets"] = [];
  const seen = new Set<string>();
  for (const mapping of event.mappings) {
    const cardType = stringField(mapping.card_type);
    const cardId = stringField(mapping.card_id);
    if (
      cardId
      && REFERENCE_CARD_TYPES.includes(cardType as ReferenceCardType)
    ) {
      const key = `${cardType}:${cardId}`;
      if (!seen.has(key)) {
        targets.push({ cardType: cardType as ReferenceCardType, cardId });
        seen.add(key);
      }
    }
  }
  return targets;
}

function autoCreationItem(
  event: ReferenceCardAutoCreationEvent,
): ReferenceCardAutomationAuditItem | null {
  if (event.outcome === "not_applicable") return null;
  const outcome: ReferenceCardAutomationOutcome = event.outcome;
  return {
    eventId: event.event_id,
    chapterId: event.chapter_id,
    outcome,
    createdCount: event.created_count,
    candidateIds: uniqueStrings([
      ...event.mappings.map((mapping) => mapping.candidate_id),
      ...event.denials.map((denial) => denial.candidate_id),
    ]),
    formalCardTargets: formalCardTargets(event),
    authorizationDigest: event.authorization_digest,
    readinessDigest: event.readiness_digest ?? "",
    authorizationRevision: Number.isInteger(event.authorization_revision)
      ? event.authorization_revision ?? null
      : null,
    policyRevision: Number.isInteger(event.policy_revision)
      ? event.policy_revision ?? null
      : null,
    sourceMutationId: event.source_mutation_id ?? "",
    mutationReceiptId: event.mutation_receipt_id ?? "",
    occurredAt: event.occurred_at,
  };
}

function repairItem(
  event: ReferenceCardRepairEvent,
): ReferenceCardAutomationAuditItem {
  return {
    eventId: event.event_id,
    chapterId: event.chapter_id,
    outcome: event.resolution ?? "manual_review_required",
    createdCount: 0,
    candidateIds: uniqueStrings(
      event.created_reference_card_candidate_ids ?? [],
    ),
    formalCardTargets: [],
    authorizationDigest: event.authorization_digest,
    readinessDigest: event.readiness_digest ?? "",
    authorizationRevision: Number.isInteger(event.authorization_revision)
      ? event.authorization_revision ?? null
      : null,
    policyRevision: Number.isInteger(event.policy_revision)
      ? event.policy_revision ?? null
      : null,
    sourceMutationId: event.source_mutation_id,
    mutationReceiptId: event.source_mutation_id,
    occurredAt: event.occurred_at,
  };
}

export function buildReferenceCardAutomationAudit(
  source: AutomationAuditSource,
): ReferenceCardAutomationAuditItem[] {
  const items = [
    ...(source.reference_card_auto_creation_events ?? [])
      .map(autoCreationItem)
      .filter((item): item is ReferenceCardAutomationAuditItem => item !== null),
    ...(source.reference_card_repair_events ?? []).map(repairItem),
  ];
  return items.sort((left, right) => (
    right.occurredAt.localeCompare(left.occurredAt)
    || right.eventId.localeCompare(left.eventId)
  ));
}
