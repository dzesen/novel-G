"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@heroui/react";
import { ApiError, apiGet, apiPost } from "@/lib/api";
import type {
  EmergentReferenceCardApplyResult,
  EmergentReferenceCardCandidate,
  EmergentReferenceCardDecisionAction,
  EmergentReferenceCardReview,
  ReferenceCard,
  ReferenceCardType,
} from "@/types/novel";

interface CandidateDecision {
  action: EmergentReferenceCardDecisionAction | "";
  targetCardId?: string;
  overwriteFields: string[];
}

interface CandidateReviewWorkspaceProps {
  novelId: string;
  initialCandidateId?: string;
  onTargetValidation: (candidateId: string, valid: boolean) => void;
}

const ACTIONS: EmergentReferenceCardDecisionAction[] = [
  "create",
  "merge",
  "restore_merge",
  "defer",
  "ignore",
];

function hasValue(value: unknown): boolean {
  return value !== null
    && value !== undefined
    && value !== ""
    && (!Array.isArray(value) || value.length > 0)
    && (typeof value !== "object"
      || Array.isArray(value)
      || Object.keys(value as Record<string, unknown>).length > 0);
}

function comparable(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value);
}

function candidateFieldValues(
  candidate: EmergentReferenceCardCandidate,
): Record<string, unknown> {
  const values: Record<string, unknown> = {
    name: candidate.name,
    subtitle: candidate.subtitle,
    description: candidate.description,
    importance: candidate.importance,
    tags: candidate.tags,
  };
  Object.entries(candidate.details ?? {}).forEach(([key, value]) => {
    values[`details.${key}`] = value;
  });
  Object.entries(candidate.character_profile ?? {}).forEach(([key, value]) => {
    values[`character_profile.${key}`] = value;
  });
  return values;
}

function cardFieldValue(card: ReferenceCard, field: string): unknown {
  if (field.startsWith("details.")) {
    return card.details?.[field.slice("details.".length)];
  }
  if (field.startsWith("character_profile.")) {
    const key = field.slice("character_profile.".length) as keyof NonNullable<
      ReferenceCard["character_profile"]
    >;
    return card.character_profile?.[key];
  }
  return card[field as keyof ReferenceCard];
}

function conflictsFor(
  candidate: EmergentReferenceCardCandidate,
  target: ReferenceCard | undefined,
) {
  if (!target) return [];
  return Object.entries(candidateFieldValues(candidate))
    .filter(([, value]) => hasValue(value))
    .map(([field, value]) => ({
      field,
      existing: cardFieldValue(target, field),
      candidate: value,
    }))
    .filter(
      (item) =>
        hasValue(item.existing)
        && comparable(item.existing) !== comparable(item.candidate),
    );
}

function displayValue(value: unknown): string {
  if (Array.isArray(value)) return value.join(" · ");
  if (value && typeof value === "object") return JSON.stringify(value);
  return String(value ?? "");
}

export default function CandidateReviewWorkspace({
  novelId,
  initialCandidateId,
  onTargetValidation,
}: CandidateReviewWorkspaceProps) {
  const t = useTranslations("writing.referenceCards.candidateReview");
  const typeT = useTranslations("writing.referenceCards.types");
  const [review, setReview] = useState<EmergentReferenceCardReview | null>(null);
  const [cards, setCards] = useState<ReferenceCard[]>([]);
  const [decisions, setDecisions] = useState<Record<string, CandidateDecision>>({});
  const [loading, setLoading] = useState(true);
  const [applying, setApplying] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const candidateRefs = useRef(new Map<string, HTMLElement>());
  const loadRequestRef = useRef(0);

  const load = useCallback(async () => {
    const requestId = ++loadRequestRef.current;
    setLoading(true);
    setError("");
    try {
      const nextReview = await apiGet<EmergentReferenceCardReview>(
        `/api/reference-cards/novel/${novelId}/candidates${
          initialCandidateId
            ? `?candidate_id=${encodeURIComponent(initialCandidateId)}`
            : ""
        }`,
      );
      const cardTypes = Array.from(
        new Set(nextReview.candidates.map((candidate) => candidate.card_type)),
      );
      const responses = await Promise.all(
        cardTypes.flatMap((cardType) => [
          apiGet<{ data: ReferenceCard[] }>(
            `/api/reference-cards/novel/${novelId}/${cardType}`,
          ),
          apiGet<{ data: ReferenceCard[] }>(
            `/api/reference-cards/novel/${novelId}/${cardType}/trash`,
          ),
        ]),
      );
      if (requestId !== loadRequestRef.current) return;
      setReview(nextReview);
      if (initialCandidateId) {
        onTargetValidation(initialCandidateId, true);
      }
      setCards(responses.flatMap((response) => response.data));
      setDecisions({});
    } catch (reason) {
      if (requestId !== loadRequestRef.current) return;
      if (
        initialCandidateId
        && reason instanceof ApiError
        && [400, 404].includes(reason.status)
      ) {
        onTargetValidation(initialCandidateId, false);
      } else {
        setError(reason instanceof Error ? reason.message : t("loadFailed"));
      }
    } finally {
      if (requestId === loadRequestRef.current) setLoading(false);
    }
  }, [initialCandidateId, novelId, onTargetValidation, t]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!initialCandidateId || !review) return;
    const target = candidateRefs.current.get(initialCandidateId);
    if (!target) return;
    target.scrollIntoView({ block: "center", behavior: "smooth" });
    target.focus({ preventScroll: true });
  }, [initialCandidateId, review]);

  const cardsByType = useMemo(() => {
    const result = new Map<ReferenceCardType, ReferenceCard[]>();
    cards.forEach((card) => {
      const current = result.get(card.card_type) ?? [];
      current.push(card);
      result.set(card.card_type, current);
    });
    return result;
  }, [cards]);
  const reviewableCandidates = useMemo(
    () => review?.candidates.filter((candidate) =>
      candidate.queue_status === "pending"
      || candidate.queue_status === "deferred"
    ) ?? [],
    [review],
  );

  const updateDecision = (
    candidate: EmergentReferenceCardCandidate,
    action: EmergentReferenceCardDecisionAction,
  ) => {
    const typeCards = cardsByType.get(candidate.card_type) ?? [];
    const targetPool = typeCards.filter((card) =>
      action === "restore_merge" ? card.is_deleted : !card.is_deleted
    );
    const recommendedTarget = targetPool.find(
      (card) => card._id === candidate.recommended_target_card_id,
    );
    const targetCardId =
      action === "merge" || action === "restore_merge"
        ? (recommendedTarget ?? targetPool[0])?._id
        : undefined;
    setDecisions((current) => ({
      ...current,
      [candidate.candidate_id]: {
        action,
        targetCardId,
        overwriteFields: [],
      },
    }));
    setError("");
    setSuccess("");
  };

  const apply = async () => {
    if (!review) return;
    const missing = reviewableCandidates.filter((candidate) => {
      const decision = decisions[candidate.candidate_id];
      if (!decision?.action) return true;
      return (
        (decision.action === "merge"
          || decision.action === "restore_merge")
        && !decision.targetCardId
      );
    });
    if (missing.length) {
      setError(t("decisionsRequired", { count: missing.length }));
      return;
    }

    setApplying(true);
    setError("");
    setSuccess("");
    try {
      const result = await apiPost<EmergentReferenceCardApplyResult>(
        `/api/reference-cards/novel/${novelId}/candidates/apply`,
        {
          review_digest: review.review_digest,
          decisions: reviewableCandidates.map((candidate) => {
            const decision = decisions[candidate.candidate_id];
            return {
              candidate_id: candidate.candidate_id,
              action: decision.action,
              ...(decision.targetCardId
                ? { target_card_id: decision.targetCardId }
                : {}),
              overwrite_fields: decision.overwriteFields,
            };
          }),
        },
      );
      setSuccess(
        result.resumed_job_ids.length
          ? t("appliedAndResumed")
          : t("applied"),
      );
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t("applyFailed"));
    } finally {
      setApplying(false);
    }
  };

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center px-6 text-sm text-muted">
        {t("loading")}
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto bg-background">
      <div className="mx-auto max-w-5xl px-4 py-5 sm:px-7 sm:py-7">
        <header className="flex flex-wrap items-start justify-between gap-4 border-b border-border pb-5">
          <div className="min-w-0 max-w-2xl">
            <h1 className="text-xl font-semibold text-foreground sm:text-2xl">
              {t("title")}
            </h1>
            <p className="mt-2 text-sm leading-6 text-muted">
              {t("description")}
            </p>
          </div>
          {review && review.candidates.length > 0 && (
            <div className="shrink-0 text-right">
              <p className="text-sm font-semibold tabular-nums text-foreground">
                {t("pendingCount", { count: reviewableCandidates.length })}
              </p>
              <p className="mt-1 text-xs text-amber-700 dark:text-amber-300">
                {t("blockingCount", { count: review.counts.blocking })}
              </p>
            </div>
          )}
        </header>

        {error && (
          <div
            role="alert"
            className="mt-5 rounded-lg border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-800 dark:border-red-900 dark:bg-red-950 dark:text-red-200"
          >
            {error}
          </div>
        )}
        {success && (
          <div
            role="status"
            className="mt-5 rounded-lg border border-emerald-300 bg-emerald-50 px-4 py-3 text-sm text-emerald-800 dark:border-emerald-900 dark:bg-emerald-950 dark:text-emerald-200"
          >
            {success}
          </div>
        )}

        {!review?.candidates.length ? (
          <div className="py-16 text-center">
            <h2 className="text-base font-semibold text-foreground">
              {t("emptyTitle")}
            </h2>
            <p className="mx-auto mt-2 max-w-lg text-sm leading-6 text-muted">
              {t("emptyDescription")}
            </p>
            <Button
              className="mt-5"
              variant="outline"
              onPress={() => void load()}
            >
              {t("refresh")}
            </Button>
          </div>
        ) : (
          <>
            <div className="mt-6 overflow-hidden rounded-xl border border-border bg-surface">
              {review.candidates.map((candidate, index) => {
                const decision = decisions[candidate.candidate_id] ?? {
                  action: "",
                  overwriteFields: [],
                };
                const typeCards = cardsByType.get(candidate.card_type) ?? [];
                const targetCards = typeCards.filter((card) =>
                  decision.action === "restore_merge"
                    ? card.is_deleted
                    : !card.is_deleted
                );
                const target = typeCards.find(
                  (card) => card._id === decision.targetCardId,
                );
                const conflicts = conflictsFor(candidate, target);
                return (
                  <article
                    key={candidate.candidate_id}
                    ref={(element) => {
                      if (element) {
                        candidateRefs.current.set(candidate.candidate_id, element);
                      } else {
                        candidateRefs.current.delete(candidate.candidate_id);
                      }
                    }}
                    tabIndex={-1}
                    className={[
                      "outline-none transition-colors focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent",
                      index ? "border-t p-4 sm:p-6" : "p-4 sm:p-6",
                      initialCandidateId === candidate.candidate_id
                        ? "border-accent bg-accent/5"
                        : "border-border",
                    ].join(" ")}
                  >
                    <div className="flex min-w-0 flex-wrap items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-2">
                          <h2 className="break-words text-base font-semibold text-foreground">
                            {candidate.name}
                          </h2>
                          <span className="rounded-full bg-surface-secondary px-2 py-1 text-[11px] font-medium text-muted">
                            {typeT(candidate.card_type)}
                          </span>
                          {candidate.requires_review_before_next_chapter && (
                            <span className="rounded-full bg-amber-100 px-2 py-1 text-[11px] font-medium text-amber-800 dark:bg-amber-950 dark:text-amber-200">
                              {t("blocksNextChapter")}
                            </span>
                          )}
                        </div>
                        <p className="mt-2 text-sm leading-6 text-foreground">
                          {candidate.description || t("noDescription")}
                        </p>
                        <p className="mt-2 text-xs leading-5 text-muted">
                          {t("source", {
                            order: candidate.evidence.chapter_order,
                            title: candidate.evidence.chapter_title,
                          })}
                          {candidate.evidence.summary
                            ? ` · ${candidate.evidence.summary}`
                            : ""}
                        </p>
                        {candidate.automation_audit && (
                          <aside className="mt-3 min-w-0 rounded-lg border border-border bg-background px-3 py-3">
                            <div className="flex min-w-0 flex-wrap items-center gap-2">
                              <span className={`rounded-full border px-2 py-0.5 text-[11px] font-semibold ${
                                candidate.automation_audit.outcome === "reverted"
                                  ? "border-border bg-surface-secondary text-foreground"
                                  : "border-emerald-300 bg-emerald-50 text-emerald-800 dark:border-emerald-900/70 dark:bg-emerald-950/35 dark:text-emerald-200"
                              }`}>
                                {t(`automationAudit.outcome.${candidate.automation_audit.outcome}`)}
                              </span>
                              <span className="text-[11px] leading-5 text-muted">
                                {t(`automationAudit.description.${candidate.automation_audit.outcome}`)}
                              </span>
                            </div>
                            <details className="mt-2 min-w-0">
                              <summary className="cursor-pointer text-xs font-medium text-accent underline-offset-4 hover:underline">
                                {t("automationAudit.openProof")}
                              </summary>
                              <dl className="mt-2 grid min-w-0 gap-2 border-t border-border pt-2 text-[11px] leading-5 text-muted sm:grid-cols-2">
                                {[
                                  ["cardId", candidate.automation_audit.card_id],
                                  ["authorization", candidate.automation_audit.authorization_digest],
                                  ["sourceJob", candidate.automation_audit.source_job_id],
                                  ["sourceReceipt", candidate.automation_audit.source_mutation_id],
                                  ["mutationReceipt", candidate.automation_audit.mutation_receipt_id],
                                ].filter(([, value]) => value).map(([label, value]) => (
                                  <div key={label} className="min-w-0">
                                    <dt className="font-medium text-foreground">
                                      {t(`automationAudit.${label}`)}
                                    </dt>
                                    <dd className="break-all font-mono">{value}</dd>
                                  </div>
                                ))}
                              </dl>
                            </details>
                          </aside>
                        )}
                      </div>
                      <span className="shrink-0 text-xs text-muted">
                        {t(`status.${candidate.queue_status}`)}
                      </span>
                    </div>

                    {Object.keys(candidate.details ?? {}).length > 0 && (
                      <dl className="mt-4 grid gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
                        {Object.entries(candidate.details).map(([key, value]) => (
                          <div key={key} className="min-w-0">
                            <dt className="text-xs text-muted">{key}</dt>
                            <dd className="mt-0.5 break-words text-foreground">
                              {displayValue(value)}
                            </dd>
                          </div>
                        ))}
                      </dl>
                    )}

                    {(candidate.queue_status === "pending"
                      || candidate.queue_status === "deferred") ? (
                      <div className="mt-5">
                        <p className="text-xs font-semibold text-foreground">
                          {t("decisionLabel")}
                        </p>
                        <div
                          className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-5"
                          role="group"
                          aria-label={t("decisionFor", { name: candidate.name })}
                        >
                          {ACTIONS.map((action) => (
                            <button
                              key={action}
                              type="button"
                              aria-pressed={decision.action === action}
                              onClick={() => updateDecision(candidate, action)}
                              className={`min-h-11 rounded-lg border px-2 py-2 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent ${
                                decision.action === action
                                  ? "border-accent bg-accent text-white"
                                  : "border-border bg-background text-muted hover:text-foreground"
                              }`}
                            >
                              {t(`actions.${action}`)}
                            </button>
                          ))}
                        </div>
                        {(candidate.recommended_target
                          || candidate.warnings.length > 0) && (
                          <p className="mt-2 text-xs leading-5 text-muted">
                            {candidate.recommended_target
                              ? t("exactMatch", {
                                  name: candidate.recommended_target.name ?? "",
                                })
                              : t("possibleMatch")}
                          </p>
                        )}
                      </div>
                    ) : (
                      <p className="mt-5 rounded-lg border border-border bg-surface-secondary px-3 py-2 text-xs leading-5 text-muted">
                        {t("resolvedReadOnly")}
                      </p>
                    )}

                    {(decision.action === "merge"
                      || decision.action === "restore_merge") && (
                      <div className="mt-4">
                        <label className="block">
                          <span className="mb-2 block text-xs font-semibold text-foreground">
                            {t("targetLabel")}
                          </span>
                          <select
                            value={decision.targetCardId ?? ""}
                            onChange={(event) =>
                              setDecisions((current) => ({
                                ...current,
                                [candidate.candidate_id]: {
                                  ...decision,
                                  targetCardId: event.target.value || undefined,
                                  overwriteFields: [],
                                },
                              }))
                            }
                            className="min-h-11 w-full rounded-lg border border-border bg-background px-3 py-2 text-sm text-foreground outline-none focus:border-accent focus:ring-2 focus:ring-accent/15"
                          >
                            <option value="">{t("targetPlaceholder")}</option>
                            {targetCards.map((card) => (
                              <option key={card._id} value={card._id}>
                                {card.name}
                              </option>
                            ))}
                          </select>
                        </label>

                        {target && conflicts.length === 0 && (
                          <p className="mt-3 text-xs leading-5 text-emerald-700 dark:text-emerald-300">
                            {t("noConflicts")}
                          </p>
                        )}
                        {conflicts.length > 0 && (
                          <fieldset className="mt-4">
                            <legend className="text-xs font-semibold text-foreground">
                              {t("conflictTitle", { count: conflicts.length })}
                            </legend>
                            <p className="mt-1 text-xs leading-5 text-muted">
                              {t("conflictDescription")}
                            </p>
                            <div className="mt-3 divide-y divide-border rounded-lg border border-border">
                              {conflicts.map((conflict) => {
                                const overwrite = decision.overwriteFields.includes(
                                  conflict.field,
                                );
                                return (
                                  <label
                                    key={conflict.field}
                                    className="grid cursor-pointer gap-3 p-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto]"
                                  >
                                    <span className="min-w-0">
                                      <span className="block text-[11px] text-muted">
                                        {t("existingValue", { field: conflict.field })}
                                      </span>
                                      <span className="mt-1 block break-words text-xs text-foreground">
                                        {displayValue(conflict.existing)}
                                      </span>
                                    </span>
                                    <span className="min-w-0">
                                      <span className="block text-[11px] text-muted">
                                        {t("candidateValue")}
                                      </span>
                                      <span className="mt-1 block break-words text-xs text-foreground">
                                        {displayValue(conflict.candidate)}
                                      </span>
                                    </span>
                                    <span className="flex min-h-11 items-center gap-2 text-xs font-medium text-foreground">
                                      <input
                                        type="checkbox"
                                        checked={overwrite}
                                        onChange={(event) =>
                                          setDecisions((current) => ({
                                            ...current,
                                            [candidate.candidate_id]: {
                                              ...decision,
                                              overwriteFields: event.target.checked
                                                ? [
                                                    ...decision.overwriteFields,
                                                    conflict.field,
                                                  ]
                                                : decision.overwriteFields.filter(
                                                    (field) =>
                                                      field !== conflict.field,
                                                  ),
                                            },
                                          }))
                                        }
                                        className="h-4 w-4 accent-accent"
                                      />
                                      {t("useCandidateValue")}
                                    </span>
                                  </label>
                                );
                              })}
                            </div>
                          </fieldset>
                        )}
                      </div>
                    )}

                    {decision.action === "defer" && (
                      <p className="mt-3 text-xs leading-5 text-amber-700 dark:text-amber-300">
                        {t("deferHint")}
                      </p>
                    )}
                    {decision.action === "ignore" && (
                      <p className="mt-3 text-xs leading-5 text-muted">
                        {t("ignoreHint")}
                      </p>
                    )}
                  </article>
                );
              })}
            </div>

            {reviewableCandidates.length > 0 && (
              <div className="sticky bottom-0 mt-5 flex flex-wrap items-center justify-between gap-3 border-t border-border bg-background/95 py-4 backdrop-blur-sm">
                <p className="text-xs leading-5 text-muted">
                  {t("applyHint")}
                </p>
                <Button
                  variant="primary"
                  className="min-h-11 bg-accent px-5 text-white hover:bg-accent-hover"
                  isDisabled={applying}
                  onPress={() => void apply()}
                >
                  {applying ? t("applying") : t("applyAll")}
                </Button>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
