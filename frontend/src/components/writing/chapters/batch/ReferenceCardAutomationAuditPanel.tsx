"use client";

import { useEffect, useRef } from "react";
import { useLocale, useTranslations } from "next-intl";
import type { GenerationJob } from "./batchTypes";
import type { ReferenceCardType } from "./referenceCardAutoCreation";
import {
  buildReferenceCardAutomationAudit,
  type ReferenceCardAutomationAuditItem,
  type ReferenceCardAutomationOutcome,
} from "./referenceCardAutomationAudit";

interface ReferenceCardAutomationAuditProps {
  job: GenerationJob;
  titleForChapter: (chapterId: string) => string;
  onJumpToChapter: (chapterId: string) => void;
  onNavigateToReferenceCards?: (
    cardType?: ReferenceCardType,
    cardId?: string,
  ) => void;
  onNavigateToReferenceCardCandidates?: (candidateId?: string) => void;
  highlightedEventId?: string;
}

const MAX_VISIBLE_EVENTS = 8;

function outcomeTone(outcome: ReferenceCardAutomationOutcome): string {
  switch (outcome) {
    case "auto_created":
    case "rewritten_unique_new":
      return "border-emerald-300 bg-emerald-50 text-emerald-800 dark:border-emerald-900/70 dark:bg-emerald-950/35 dark:text-emerald-200";
    case "dependency_removed":
      return "border-blue-300 bg-blue-50 text-blue-800 dark:border-blue-900/70 dark:bg-blue-950/35 dark:text-blue-200";
    case "manual_review_required":
      return "border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-900/70 dark:bg-amber-950/35 dark:text-amber-200";
    case "reverted":
      return "border-border bg-surface-secondary text-foreground";
  }
}

function AuditRow({
  item,
  titleForChapter,
  onJumpToChapter,
  onNavigateToReferenceCards,
  onNavigateToReferenceCardCandidates,
  highlighted,
}: {
  item: ReferenceCardAutomationAuditItem;
  titleForChapter: (chapterId: string) => string;
  onJumpToChapter: (chapterId: string) => void;
  onNavigateToReferenceCards?: (
    cardType?: ReferenceCardType,
    cardId?: string,
  ) => void;
  onNavigateToReferenceCardCandidates?: (candidateId?: string) => void;
  highlighted: boolean;
}) {
  const t = useTranslations("writing.batch");
  const typeT = useTranslations("writing.referenceCards.types");
  const locale = useLocale();
  const date = new Date(item.occurredAt);
  const occurredAt = Number.isNaN(date.getTime())
    ? ""
    : new Intl.DateTimeFormat(locale, {
        dateStyle: "medium",
        timeStyle: "short",
      }).format(date);
  const firstCandidateId = item.candidateIds[0];
  const chapterTitle = titleForChapter(item.chapterId);
  const hasReceiptDetails = Boolean(
    item.authorizationDigest
    || item.readinessDigest
    || item.authorizationRevision !== null
    || item.policyRevision !== null
    || item.sourceMutationId
    || item.mutationReceiptId,
  );
  const rowRef = useRef<HTMLLIElement>(null);
  useEffect(() => {
    if (highlighted) {
      rowRef.current?.scrollIntoView({ block: "nearest" });
    }
  }, [highlighted]);

  return (
    <li
      ref={rowRef}
      data-automation-event-id={item.eventId}
      aria-current={highlighted ? "true" : undefined}
      className={`grid min-w-0 gap-2 border-t border-border px-3 py-3 first:border-t-0 sm:px-4 ${
        highlighted ? "bg-accent/10 ring-2 ring-inset ring-accent" : ""
      }`}
    >
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <span className={`rounded-full border px-2 py-0.5 text-[11px] font-semibold ${outcomeTone(item.outcome)}`}>
          {t(`referenceCardAuditOutcome.${item.outcome}`)}
        </span>
        {occurredAt && (
          <time dateTime={item.occurredAt} className="text-[11px] text-muted">
            {occurredAt}
          </time>
        )}
      </div>
      <p className="min-w-0 break-words text-xs leading-5 text-foreground">
        {t(`referenceCardAuditDescription.${item.outcome}`, {
          count: item.createdCount,
        })}
      </p>
      <div className="flex min-w-0 flex-wrap gap-x-4 gap-y-2">
        <button
          type="button"
          onClick={() => onJumpToChapter(item.chapterId)}
          className="min-h-9 break-words text-left text-xs font-medium text-accent underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          {t("referenceCardAuditOpenSource", { chapter: chapterTitle })}
        </button>
        {firstCandidateId && onNavigateToReferenceCardCandidates && (
          <button
            type="button"
            onClick={() => onNavigateToReferenceCardCandidates(firstCandidateId)}
            className="min-h-9 text-xs font-medium text-accent underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            {t("referenceCardAuditOpenCandidate", {
              count: item.candidateIds.length,
            })}
          </button>
        )}
        {onNavigateToReferenceCards && item.formalCardTargets.map((target) => (
          <button
            key={`${target.cardType}:${target.cardId}`}
            type="button"
            onClick={() => onNavigateToReferenceCards(
              target.cardType,
              target.cardId,
            )}
            className="min-h-9 text-xs font-medium text-accent underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            {t("referenceCardAuditOpenFormalCard", {
              type: typeT(target.cardType),
            })}
          </button>
        ))}
      </div>
      {hasReceiptDetails && (
        <details className="min-w-0 rounded-md bg-background px-3 py-2">
          <summary className="cursor-pointer text-xs font-medium text-muted underline-offset-4 hover:text-foreground">
            {t("referenceCardAuditOpenProof")}
          </summary>
          <dl className="mt-2 grid min-w-0 gap-2 border-t border-border pt-2 text-[11px] leading-5 text-muted">
            {item.authorizationDigest && (
              <div className="min-w-0">
                <dt className="font-medium text-foreground">
                  {t("referenceCardAuditAuthorization")}
                </dt>
                <dd className="break-all font-mono">{item.authorizationDigest}</dd>
              </div>
            )}
            {item.readinessDigest && (
              <div className="min-w-0">
                <dt className="font-medium text-foreground">
                  {t("referenceCardAuditReadiness")}
                </dt>
                <dd className="break-all font-mono">{item.readinessDigest}</dd>
              </div>
            )}
            {item.authorizationRevision !== null && (
              <div className="min-w-0">
                <dt className="font-medium text-foreground">
                  {t("referenceCardAuditAuthorizationRevision")}
                </dt>
                <dd className="break-all font-mono">
                  {item.authorizationRevision}
                </dd>
              </div>
            )}
            {item.policyRevision !== null && (
              <div className="min-w-0">
                <dt className="font-medium text-foreground">
                  {t("referenceCardAuditPolicyRevision")}
                </dt>
                <dd className="break-all font-mono">{item.policyRevision}</dd>
              </div>
            )}
            {item.sourceMutationId && item.sourceMutationId !== item.mutationReceiptId && (
              <div className="min-w-0">
                <dt className="font-medium text-foreground">
                  {t("referenceCardAuditSourceReceipt")}
                </dt>
                <dd className="break-all font-mono">{item.sourceMutationId}</dd>
              </div>
            )}
            {item.mutationReceiptId && (
              <div className="min-w-0">
                <dt className="font-medium text-foreground">
                  {t("referenceCardAuditMutationReceipt")}
                </dt>
                <dd className="break-all font-mono">{item.mutationReceiptId}</dd>
              </div>
            )}
          </dl>
        </details>
      )}
    </li>
  );
}

export default function ReferenceCardAutomationAuditPanel({
  job,
  titleForChapter,
  onJumpToChapter,
  onNavigateToReferenceCards,
  onNavigateToReferenceCardCandidates,
  highlightedEventId,
}: ReferenceCardAutomationAuditProps) {
  const t = useTranslations("writing.batch");
  const items = buildReferenceCardAutomationAudit(job);
  if (items.length === 0) return null;
  const highlightedItem = items.find(
    (item) => item.eventId === highlightedEventId,
  );
  const orderedItems = highlightedItem
    ? [highlightedItem, ...items.filter((item) => item !== highlightedItem)]
    : items;
  const hiddenCount = Math.max(0, orderedItems.length - MAX_VISIBLE_EVENTS);

  return (
    <section
      aria-labelledby="reference-card-automation-audit-title"
      className="m-3 min-w-0 overflow-hidden rounded-md border border-border bg-surface sm:m-4"
    >
      <div className="flex min-w-0 flex-wrap items-start justify-between gap-2 bg-background px-3 py-2.5 sm:px-4">
        <div className="min-w-0">
          <h4
            id="reference-card-automation-audit-title"
            className="text-xs font-semibold text-foreground"
          >
            {t("referenceCardAuditTitle")}
          </h4>
          <p className="mt-0.5 text-[11px] leading-5 text-muted">
            {t("referenceCardAuditSummary", { count: items.length })}
          </p>
        </div>
      </div>
      <ol>
        {orderedItems.slice(0, MAX_VISIBLE_EVENTS).map((item) => (
          <AuditRow
            key={`${item.outcome}:${item.eventId}`}
            item={item}
            titleForChapter={titleForChapter}
            onJumpToChapter={onJumpToChapter}
            onNavigateToReferenceCards={onNavigateToReferenceCards}
            onNavigateToReferenceCardCandidates={
              onNavigateToReferenceCardCandidates
            }
            highlighted={item.eventId === highlightedEventId}
          />
        ))}
      </ol>
      {hiddenCount > 0 && (
        <details className="border-t border-border">
          <summary className="cursor-pointer px-3 py-2 text-[11px] font-medium leading-5 text-accent underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent sm:px-4">
            {t("referenceCardAuditOlderHidden", { count: hiddenCount })}
          </summary>
          <ol className="border-t border-border">
            {orderedItems.slice(MAX_VISIBLE_EVENTS).map((item) => (
              <AuditRow
                key={`${item.outcome}:${item.eventId}`}
                item={item}
                titleForChapter={titleForChapter}
                onJumpToChapter={onJumpToChapter}
                onNavigateToReferenceCards={onNavigateToReferenceCards}
                onNavigateToReferenceCardCandidates={
                  onNavigateToReferenceCardCandidates
                }
                highlighted={item.eventId === highlightedEventId}
              />
            ))}
          </ol>
        </details>
      )}
    </section>
  );
}
