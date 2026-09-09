"use client";

import { useTranslations } from "next-intl";
import type {
  DiagnosticCategory,
  DiagnosticEvidence,
  GenerationDiagnostic,
  GenerationDiagnosticsSummary,
} from "./batchTypes";
import { finishReasonTranslationKey } from "../prose/prosePresentation";
import { reviewValidationGroups } from "../prose/reviewValidationPresentation";
import {
  structuredValidationErrorKey,
  structuredValidationFieldKey,
  structuredValidationSceneNumber,
} from "./structuredValidationPresentation";
import {
  diagnosticActionTranslationKey,
  diagnosticImpactTranslationKey,
  diagnosticReasonTranslationKey,
} from "./generationReasonPresentation";

function useDiagnosticCopy() {
  const t = useTranslations("writing.batch");

  const categoryLabel = (category: DiagnosticCategory) => {
    switch (category) {
      case "model_output_incomplete":
        return t("diagnosticsCategoryModelOutput");
      case "provider_or_transport":
        return t("diagnosticsCategoryProvider");
      case "validation_logic":
        return t("diagnosticsCategoryValidation");
      case "source_changed":
        return t("diagnosticsCategorySourceChanged");
      case "context_or_budget":
        return t("diagnosticsCategoryBudget");
      case "user_action":
        return t("diagnosticsCategoryUserAction");
      default:
        return t("diagnosticsCategoryUnknown");
    }
  };

  const evidenceLabel = (evidence: DiagnosticEvidence) => {
    switch (evidence) {
      case "confirmed":
        return t("diagnosticsEvidenceConfirmed");
      case "strong_inference":
        return t("diagnosticsEvidenceInferred");
      default:
        return t("diagnosticsEvidenceInsufficient");
    }
  };

  const reasonLabel = (code: string) => {
    const key = diagnosticReasonTranslationKey(code);
    return key ? t(key) : t("diagnosticsReasonUnknown");
  };

  const stepLabel = (step: string) => {
    switch (step) {
      case "outline":
        return t("stepOutline");
      case "prose":
        return t("stepProse");
      case "state":
        return t("stepState");
      case "outline_adherence":
        return t("stepOutlineAdherence");
      case "candidate_pipeline":
        return t("diagnosticsStepCandidatePipeline");
      default:
        return t("diagnosticsStepJob");
    }
  };

  const impactLabel = (impact: string) => {
    const key = diagnosticImpactTranslationKey(impact);
    return key ? t(key) : t("diagnosticsImpactUnknown");
  };

  const actionLabel = (action: string) => {
    const key = diagnosticActionTranslationKey(action);
    return key ? t(key) : null;
  };

  return {
    actionLabel,
    categoryLabel,
    evidenceLabel,
    impactLabel,
    reasonLabel,
    stepLabel,
  };
}

export function DiagnosticEventSummary({
  event,
}: {
  event: GenerationDiagnostic;
}) {
  const t = useTranslations("writing.batch");
  const {
    actionLabel,
    categoryLabel,
    evidenceLabel,
    impactLabel,
    reasonLabel,
    stepLabel,
  } =
    useDiagnosticCopy();
  const requested = Number(event.details.requested_word_count);
  const actual = Number(event.details.actual_word_count);
  const scenes = Number(event.details.scene_count);
  const completedScenes = Number(event.details.completed_scene_count);
  const providers = Array.isArray(event.details.provider_aliases)
    ? event.details.provider_aliases
    : [];
  const finishReason = String(event.details.finish_reason ?? "");
  const rawFinishReason = String(event.details.raw_finish_reason ?? "");
  const actions = (event.action_codes ?? [])
    .map(actionLabel)
    .filter((value): value is string => Boolean(value));
  const repairCyclesUsed = Number(event.details.repair_cycles_used);
  const repairCyclesLimit = Number(event.details.repair_cycles_limit);
  const consistencyIssueCount = Number(event.details.consistency_issue_count);
  const droppedReferenceCount = Number(event.details.dropped_reference_count);
  const candidateGate = event.details.candidate_gate;
  const validationGroups = reviewValidationGroups(event.details.structured_validation);

  return (
    <div className="min-w-0">
      <p className="text-sm font-medium text-foreground">
        {event.code === "structured_output_truncated"
          ? t("diagnosticsReasonStructuredTruncatedStep", { step: stepLabel(event.step) })
          : reasonLabel(event.code)}
      </p>
      {validationGroups.map((group) => (
        <div key={group.phase} className="mt-2 min-w-0 text-xs leading-5">
          <p className="font-medium text-foreground">
            {t(group.phase === "primary" ? "diagnosticsValidationPrimary"
              : group.phase === "repair" ? "diagnosticsValidationRepair"
                : "diagnosticsValidationDetails")}
          </p>
          <ul className="list-disc space-y-1 pl-4 text-warm-700 dark:text-muted">
            {group.issues.map((issue, index) => {
              const field = t(structuredValidationFieldKey(issue.path));
              const scene = structuredValidationSceneNumber(issue.path);
              return (
                <li key={`${issue.path}:${issue.errorType}:${index}`} className="break-words">
                  {t("diagnosticsValidationIssue", {
                    field: scene === null ? field : t("diagnosticsValidationSceneField", { scene, field }),
                    reason: t(structuredValidationErrorKey(issue.errorType)),
                  })}
                  {issue.path !== "$" && (
                    <code className="block break-all text-[11px] text-muted">{issue.path}</code>
                  )}
                </li>
              );
            })}
          </ul>
          {group.truncated && <p>{t("diagnosticsValidationMore")}</p>}
        </div>
      ))}
      {validationGroups.length === 0
        && ["structured_output_invalid", "validation_rejected"].includes(event.code) && (
        <p className="mt-1 text-xs leading-5 text-warm-700 dark:text-muted">
          {t("diagnosticsValidationNotRecorded")}
        </p>
      )}
      <p className="mt-0.5 text-xs leading-5 text-warm-700 dark:text-muted">
        {t("diagnosticsEventContext", {
          category: categoryLabel(event.category),
          evidence: evidenceLabel(event.evidence),
          step: stepLabel(event.step),
        })}
      </p>
      {candidateGate && Number.isFinite(repairCyclesUsed)
        && Number.isFinite(repairCyclesLimit) && (
        <p className="mt-1 text-xs leading-5 text-foreground">
          {t("diagnosticsCandidateRepairUsage", {
            used: repairCyclesUsed,
            limit: repairCyclesLimit,
          })}
        </p>
      )}
      {candidateGate === "state"
        && Number.isFinite(consistencyIssueCount)
        && Number.isFinite(droppedReferenceCount) && (
        <p className="text-xs leading-5 text-warm-700 dark:text-muted">
          {t("diagnosticsCandidateStateEvidence", {
            issues: consistencyIssueCount,
            dropped: droppedReferenceCount,
          })}
        </p>
      )}
      {event.impact && (
        <p className="mt-1 text-xs leading-5 text-foreground">
          {t("diagnosticsImpactSummary", { impact: impactLabel(event.impact) })}
        </p>
      )}
      {actions.length > 0 && (
        <p className="text-xs leading-5 text-warm-700 dark:text-muted">
          {t("diagnosticsActionSummary", { actions: actions.join(t("diagnosticsActionSeparator")) })}
        </p>
      )}
      {Number.isFinite(requested) && Number.isFinite(actual) && requested > 0 && (
        <p className="text-xs leading-5 text-warm-700 dark:text-muted">
          {t("diagnosticsProseMetrics", { actual, requested })}
        </p>
      )}
      {Number.isFinite(scenes) && Number.isFinite(completedScenes) && scenes > 0 && (
        <p className="text-xs leading-5 text-warm-700 dark:text-muted">
          {t("diagnosticsSceneMetrics", {
            completed: completedScenes,
            total: scenes,
          })}
        </p>
      )}
      {(finishReason || rawFinishReason) && (
        <p className="break-words text-xs leading-5 text-warm-700 dark:text-muted">
          {t("diagnosticsFinishReason", {
            reason: t(finishReasonTranslationKey(finishReason || rawFinishReason)),
          })}
        </p>
      )}
      {providers.length > 0 && (
        <p className="break-words text-xs leading-5 text-warm-700 dark:text-muted">
          {t("diagnosticsProviders", { providers: providers.join(", ") })}
        </p>
      )}
    </div>
  );
}
interface GenerationDiagnosticsPanelProps {
  summary: GenerationDiagnosticsSummary | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  embedded?: boolean;
  onOpenEvent?: (
    event: GenerationDiagnosticsSummary["recent_events"][number],
  ) => void;
}

export default function GenerationDiagnosticsPanel({
  summary,
  loading,
  error,
  onRetry,
  embedded = false,
  onOpenEvent,
}: GenerationDiagnosticsPanelProps) {
  const t = useTranslations("writing.batch");
  const { categoryLabel } = useDiagnosticCopy();

  return (
    <section
      aria-labelledby="generation-diagnostics-title"
      className={embedded
        ? "min-w-0"
        : "shrink-0 border-b border-border bg-surface px-4 py-3"
      }
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <h3
            id="generation-diagnostics-title"
            className="text-sm font-semibold text-foreground"
          >
            {t("diagnosticsTitle")}
          </h3>
          <p className="mt-0.5 max-w-[70ch] text-xs leading-5 text-warm-700 dark:text-muted">
            {t("diagnosticsDescription")}
          </p>
        </div>
        <button
          type="button"
          onClick={onRetry}
          disabled={loading}
          className="shrink-0 rounded px-1 py-0.5 text-xs font-medium text-accent hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:cursor-wait disabled:opacity-60"
        >
          {loading ? t("diagnosticsLoading") : t("diagnosticsRefresh")}
        </button>
      </div>

      {error && (
        <div
          role="alert"
          className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t border-red-200 pt-3 text-xs text-red-700 dark:border-red-900/60 dark:text-red-300"
        >
          <span className="min-w-0 break-words">
            {t("diagnosticsLoadError")}
          </span>
          <button
            type="button"
            onClick={onRetry}
            className="shrink-0 font-medium underline underline-offset-2"
          >
            {t("diagnosticsRetry")}
          </button>
        </div>
      )}

      {!error && loading && !summary && (
        <p role="status" className="mt-3 text-xs text-warm-700 dark:text-muted">
          {t("diagnosticsLoading")}
        </p>
      )}

      {!error && summary && (
        <div className="mt-3">
          {summary.event_count === 0 ? (
            <p className="text-xs leading-5 text-warm-700 dark:text-muted">
              {t("diagnosticsEmpty", { jobs: summary.window_job_count })}
            </p>
          ) : (
            <>
              <p className="text-xs leading-5 text-warm-700 dark:text-muted">
                {t("diagnosticsWindowSummary", {
                  events: summary.event_count,
                  jobs: summary.affected_job_count,
                  window: summary.window_job_count,
                })}
              </p>
              {summary.inferred_event_count > 0 && (
                <p className="mt-0.5 text-[11px] leading-5 text-warm-700 dark:text-muted">
                  {t("diagnosticsInferenceSummary", {
                    count: summary.inferred_event_count,
                  })}
                </p>
              )}
              {(summary.insufficient_event_count ?? 0) > 0 && (
                <p className="mt-0.5 text-[11px] leading-5 text-amber-800 dark:text-amber-200">
                  {t("diagnosticsInsufficientSummary", {
                    count: summary.insufficient_event_count ?? 0,
                  })}
                </p>
              )}

              <dl className="mt-3 grid border-t border-border sm:grid-cols-2">
                {summary.categories.map((item) => (
                  <div
                    key={item.category}
                    className="min-w-0 border-b border-border py-2 sm:odd:pr-4 sm:even:pl-4"
                  >
                    <dt className="truncate text-xs font-medium text-foreground">
                      {categoryLabel(item.category)}
                    </dt>
                    <dd className="mt-0.5 text-[11px] leading-5 text-warm-700 dark:text-muted">
                      {t("diagnosticsCategoryCounts", {
                        events: item.event_count,
                        jobs: item.job_count,
                      })}
                    </dd>
                  </div>
                ))}
              </dl>

              {summary.recent_events.length > 0 && (
                <details className="mt-3 border-t border-border pt-3">
                  <summary className="cursor-pointer text-xs font-medium text-foreground marker:text-warm-700 dark:marker:text-muted">
                    {t("diagnosticsRecent")}
                  </summary>
                  <div className="mt-2 divide-y divide-border">
                    {summary.recent_events.slice(0, 5).map((event, index) => (
                      <div
                        key={event.event_id ?? `${event.job_id}-${event.occurred_at ?? index}`}
                        className="py-2"
                      >
                        {onOpenEvent ? (
                          <button
                            type="button"
                            onClick={() => onOpenEvent(event)}
                            className="block w-full rounded-sm px-1 py-1 text-left hover:bg-surface-secondary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
                          >
                            <DiagnosticEventSummary event={event} />
                            <span className="mt-1 block text-xs font-medium text-accent">
                              {t("diagnosticsOpenRecord")}
                            </span>
                          </button>
                        ) : (
                          <DiagnosticEventSummary event={event} />
                        )}
                      </div>
                    ))}
                  </div>
                </details>
              )}
            </>
          )}
        </div>
      )}
    </section>
  );
}
