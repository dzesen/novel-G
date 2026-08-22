"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { apiGet, apiPost } from "@/lib/api";
import {
  WORLD_BASELINE_DOMAINS,
  countPendingWorldBaselineDecisions,
  hasCompleteWorldBaselineDecisions,
} from "@/lib/worldBaseline";
import type {
  WorldBaselineDecision,
  WorldBaselineDecisionDraft,
  WorldBaselineDomain,
  WorldBaselineState,
  WorldBaselineView,
} from "@/types/novel";

type PendingDestination = "curation" | "candidates" | "library";

interface WorldBaselineWorkspaceProps {
  novelId: string;
  onOpenDomain: (domain: WorldBaselineDomain) => void;
  onOpenPending: (destination: PendingDestination) => void;
  onContinueToAutoBook: () => void;
  onOpenWriting: () => void;
}

const STATE_COPY: Record<
  WorldBaselineState,
  { title: string; body: string }
> = {
  required: { title: "requiredTitle", body: "requiredBody" },
  stale: { title: "staleTitle", body: "staleBody" },
  blocked_pending_decisions: {
    title: "blockedTitle",
    body: "blockedBody",
  },
  current: { title: "currentTitle", body: "currentBody" },
  not_required_legacy: { title: "legacyTitle", body: "legacyBody" },
};

function pendingDestination(
  baseline: WorldBaselineView,
): PendingDestination {
  if (baseline.pending_decisions.reference_card_proposals > 0) {
    return "curation";
  }
  if (baseline.pending_decisions.emergent_candidates > 0) {
    return "candidates";
  }
  return "library";
}

export default function WorldBaselineWorkspace({
  novelId,
  onOpenDomain,
  onOpenPending,
  onContinueToAutoBook,
  onOpenWriting,
}: WorldBaselineWorkspaceProps) {
  const t = useTranslations("writing.worldBaseline");
  const locale = useLocale();
  const [baseline, setBaseline] = useState<WorldBaselineView | null>(null);
  const [decisions, setDecisions] = useState<WorldBaselineDecisionDraft>({});
  const [loading, setLoading] = useState(true);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState("");

  const loadBaseline = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const result = await apiGet<WorldBaselineView>(
        `/api/novels/${novelId}/world-baseline`,
      );
      setBaseline(result);
      setDecisions(result.state === "stale" ? {} : result.decisions);
    } catch {
      setBaseline(null);
      setError(t("loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [novelId, t]);

  useEffect(() => {
    void loadBaseline();
  }, [loadBaseline]);

  const pendingCount = baseline
    ? countPendingWorldBaselineDecisions(baseline.pending_decisions)
    : 0;
  const decidedCount = WORLD_BASELINE_DOMAINS.filter(
    (domain) => decisions[domain] !== undefined,
  ).length;
  const remainingCount = WORLD_BASELINE_DOMAINS.length - decidedCount;
  const decisionsComplete = hasCompleteWorldBaselineDecisions(decisions);
  const isCurrent =
    baseline?.state === "current" ||
    baseline?.state === "not_required_legacy";
  const canConfirm =
    Boolean(baseline) &&
    !isCurrent &&
    pendingCount === 0 &&
    decisionsComplete &&
    !confirming;

  const confirmedAt = useMemo(() => {
    if (!baseline?.confirmed_at) return null;
    const date = new Date(baseline.confirmed_at);
    if (Number.isNaN(date.getTime())) return null;
    return new Intl.DateTimeFormat(locale, {
      dateStyle: "medium",
      timeStyle: "short",
    }).format(date);
  }, [baseline?.confirmed_at, locale]);

  const chooseDecision = (
    domain: WorldBaselineDomain,
    decision: WorldBaselineDecision,
  ) => {
    setDecisions((current) => ({ ...current, [domain]: decision }));
    setError("");
  };

  const confirmBaseline = async () => {
    if (!canConfirm || !hasCompleteWorldBaselineDecisions(decisions)) return;
    setConfirming(true);
    setError("");
    try {
      const result = await apiPost<WorldBaselineView>(
        `/api/novels/${novelId}/world-baseline/confirm`,
        { decisions },
      );
      setBaseline(result);
      setDecisions(result.decisions);
    } catch (cause) {
      const detail = cause instanceof Error ? cause.message : "";
      setError(detail ? `${t("confirmFailed")} ${detail}` : t("confirmFailed"));
    } finally {
      setConfirming(false);
    }
  };

  if (loading) {
    return (
      <div className="grid h-full place-items-center overflow-y-auto px-5 py-10">
        <p role="status" className="text-sm text-muted">
          {t("loading")}
        </p>
      </div>
    );
  }

  if (!baseline) {
    return (
      <div className="grid h-full place-items-center overflow-y-auto px-5 py-10">
        <section className="w-full max-w-xl border-y border-border py-8">
          <h1 className="text-xl font-semibold text-foreground">{t("title")}</h1>
          <p role="alert" className="mt-3 text-sm leading-6 text-danger">
            {error || t("loadFailed")}
          </p>
          <button
            type="button"
            onClick={() => void loadBaseline()}
            className="mt-5 min-h-10 rounded-md bg-accent px-4 text-sm font-semibold text-white transition-colors hover:bg-accent-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2"
          >
            {t("retry")}
          </button>
        </section>
      </div>
    );
  }

  const copy = STATE_COPY[baseline.state];
  const stateTone =
    baseline.state === "current" || baseline.state === "not_required_legacy"
      ? "border-success/40 bg-success/5"
      : baseline.state === "stale" ||
          baseline.state === "blocked_pending_decisions"
        ? "border-warning/45 bg-warning/5"
        : "border-accent/35 bg-accent/[0.04]";

  return (
    <main className="h-full overflow-y-auto bg-background">
      <div className="mx-auto w-full max-w-6xl px-4 py-6 sm:px-6 sm:py-8">
        <header className="max-w-3xl">
          <h1 className="text-balance text-2xl font-semibold tracking-[-0.02em] text-foreground sm:text-3xl">
            {t("title")}
          </h1>
          <p className="mt-3 max-w-[70ch] text-sm leading-6 text-muted">
            {t("description")}
          </p>
        </header>

        <ol
          aria-label={t("stageAria")}
          className="mt-6 grid overflow-hidden rounded-lg border border-border bg-surface sm:grid-cols-3"
        >
          <ProgressStage number={1} label={t("stages.structure")} status="done" />
          <ProgressStage
            number={2}
            label={t("stages.review")}
            status={isCurrent ? "done" : "active"}
          />
          <ProgressStage
            number={3}
            label={t("stages.ready")}
            status={isCurrent ? "active" : "pending"}
          />
        </ol>

        <section className={`mt-5 rounded-lg border px-4 py-4 sm:px-5 ${stateTone}`}>
          <h2 className="text-base font-semibold text-foreground">
            {t(`states.${copy.title}`)}
          </h2>
          <p className="mt-1 max-w-[75ch] text-sm leading-6 text-muted">
            {t(`states.${copy.body}`)}
          </p>
          {baseline.stale_reasons.length > 0 && (
            <ul className="mt-3 flex flex-wrap gap-2 text-xs font-medium text-foreground">
              {baseline.stale_reasons.map((reason) => (
                <li key={reason} className="rounded-md bg-background/70 px-2.5 py-1.5">
                  {t.has(`staleReasons.${reason}`)
                    ? t(`staleReasons.${reason}`)
                    : reason}
                </li>
              ))}
            </ul>
          )}
        </section>

        {pendingCount > 0 && (
          <section className="mt-5 border-y border-warning/50 bg-warning/5 px-1 py-5 sm:px-4">
            <div className="flex min-w-0 flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
              <div className="min-w-0">
                <h2 className="text-base font-semibold text-foreground">
                  {t("pendingTitle", { count: pendingCount })}
                </h2>
                <p className="mt-1 max-w-[70ch] text-sm leading-6 text-muted">
                  {t("pendingBody")}
                </p>
                <ul className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-foreground">
                  <li>
                    {t("pending.referenceCardProposals", {
                      count: baseline.pending_decisions.reference_card_proposals,
                    })}
                  </li>
                  <li>
                    {t("pending.emergentCandidates", {
                      count: baseline.pending_decisions.emergent_candidates,
                    })}
                  </li>
                  <li>
                    {t("pending.cardImportProposals", {
                      count: baseline.pending_decisions.card_import_proposals,
                    })}
                  </li>
                </ul>
              </div>
              <button
                type="button"
                onClick={() => onOpenPending(pendingDestination(baseline))}
                className="min-h-10 w-full shrink-0 rounded-md border border-warning/60 bg-background px-4 text-sm font-semibold text-foreground transition-colors hover:bg-warning/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-warning sm:w-auto"
              >
                {t("openPending")}
              </button>
            </div>
          </section>
        )}

        <div className="mt-7 grid min-w-0 gap-6 lg:grid-cols-[minmax(0,1fr)_18rem] lg:items-start">
          <section className="min-w-0">
            <h2 className="text-lg font-semibold text-foreground">
              {t("decisionsTitle")}
            </h2>
            <p className="mt-2 max-w-[70ch] text-sm leading-6 text-muted">
              {t("decisionsDescription")}
            </p>
            <div className="mt-4 divide-y divide-border border-y border-border">
              {WORLD_BASELINE_DOMAINS.map((domain) => {
                const label = t(`domains.${domain}.label`);
                const decision = decisions[domain];
                return (
                  <div
                    key={domain}
                    className="grid min-w-0 gap-4 py-4 md:grid-cols-[minmax(0,1fr)_auto] md:items-center"
                  >
                    <div className="min-w-0">
                      <div className="flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-1">
                        <h3 className="text-sm font-semibold text-foreground">{label}</h3>
                        <span className="text-xs tabular-nums text-muted">
                          {t("count", { count: baseline.counts[domain] })}
                        </span>
                      </div>
                      <p className="mt-1 text-xs leading-5 text-muted">
                        {t(`domains.${domain}.description`)}
                      </p>
                      <button
                        type="button"
                        onClick={() => onOpenDomain(domain)}
                        className="mt-2 text-xs font-semibold text-accent underline decoration-accent/35 underline-offset-4 hover:decoration-accent focus-visible:rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                      >
                        {t("openDomain", { domain: label })}
                      </button>
                    </div>

                    {isCurrent ? (
                      <p className="w-fit rounded-md bg-surface-secondary px-3 py-2 text-xs font-semibold text-foreground">
                        {baseline.state === "not_required_legacy"
                          ? t("legacyDecision")
                          : decision === "reviewed"
                            ? t("reviewed")
                            : t("notApplicable")}
                      </p>
                    ) : (
                      <fieldset className="min-w-0">
                        <legend className="sr-only">
                          {t("decisionAria", { domain: label })}
                        </legend>
                        <div className="grid grid-cols-2 gap-2 sm:flex sm:flex-wrap">
                          <DecisionOption
                            name={`world-baseline-${domain}`}
                            label={t("reviewed")}
                            value="reviewed"
                            checked={decision === "reviewed"}
                            onChange={() => chooseDecision(domain, "reviewed")}
                          />
                          <DecisionOption
                            name={`world-baseline-${domain}`}
                            label={t("notApplicable")}
                            value="not_applicable"
                            checked={decision === "not_applicable"}
                            onChange={() => chooseDecision(domain, "not_applicable")}
                          />
                        </div>
                      </fieldset>
                    )}
                  </div>
                );
              })}
            </div>
          </section>

          <aside className="min-w-0 border-t border-border pt-5 lg:sticky lg:top-5 lg:border-l lg:border-t-0 lg:pl-6 lg:pt-0">
            {!isCurrent && (
              <p className="text-sm font-semibold text-foreground" aria-live="polite">
                {t("remaining", { count: remainingCount })}
              </p>
            )}
            <p className={`${isCurrent ? "" : "mt-2"} text-xs leading-5 text-muted`}>
              {isCurrent ? t("manualWritingHint") : t("confirmHint")}
            </p>
            {confirmedAt && (
              <p className="mt-3 text-xs font-medium text-foreground">
                {t("confirmedAt", { time: confirmedAt })}
              </p>
            )}
            {error && (
              <p
                role="alert"
                className="mt-4 rounded-md border border-danger/35 bg-danger/5 px-3 py-2.5 text-sm leading-5 text-danger"
              >
                {error}
              </p>
            )}
            <div className="mt-5 grid gap-2">
              {isCurrent ? (
                <button
                  type="button"
                  onClick={onContinueToAutoBook}
                  className="min-h-11 w-full rounded-md bg-accent px-4 text-sm font-semibold text-white transition-colors hover:bg-accent-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2"
                >
                  {t("continueAutoBook")}
                </button>
              ) : (
                <button
                  type="button"
                  onClick={() => void confirmBaseline()}
                  disabled={!canConfirm}
                  className="min-h-11 w-full rounded-md bg-accent px-4 text-sm font-semibold text-white transition-colors hover:bg-accent-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-45"
                >
                  {confirming ? t("confirming") : t("confirm")}
                </button>
              )}
              <button
                type="button"
                onClick={onOpenWriting}
                className="min-h-11 w-full rounded-md border border-border bg-surface px-4 text-sm font-semibold text-foreground transition-colors hover:bg-surface-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
              >
                {t("openWriting")}
              </button>
            </div>
          </aside>
        </div>
      </div>
    </main>
  );
}

function ProgressStage({
  number,
  label,
  status,
}: {
  number: number;
  label: string;
  status: "done" | "active" | "pending";
}) {
  return (
    <li
      aria-current={status === "active" ? "step" : undefined}
      className={`flex min-w-0 items-center gap-3 border-b border-border px-4 py-3 last:border-b-0 sm:border-b-0 sm:border-r sm:last:border-r-0 ${
        status === "active" ? "bg-accent/[0.06]" : ""
      }`}
    >
      <span
        className={`grid h-7 w-7 shrink-0 place-items-center rounded-full text-xs font-semibold tabular-nums ${
          status === "done"
            ? "bg-success/15 text-success"
            : status === "active"
              ? "bg-accent text-white"
              : "bg-surface-secondary text-muted"
        }`}
      >
        {number}
      </span>
      <span
        className={`min-w-0 text-xs font-semibold ${
          status === "pending" ? "text-muted" : "text-foreground"
        }`}
      >
        {label}
      </span>
    </li>
  );
}

function DecisionOption({
  name,
  label,
  value,
  checked,
  onChange,
}: {
  name: string;
  label: string;
  value: WorldBaselineDecision;
  checked: boolean;
  onChange: () => void;
}) {
  return (
    <label
      className={`flex min-h-10 cursor-pointer items-center justify-center rounded-md border px-3 text-center text-xs font-semibold transition-colors focus-within:ring-2 focus-within:ring-accent ${
        checked
          ? "border-accent bg-accent/10 text-accent"
          : "border-border bg-surface text-foreground hover:bg-surface-secondary"
      }`}
    >
      <input
        type="radio"
        name={name}
        value={value}
        checked={checked}
        onChange={onChange}
        className="sr-only"
      />
      {label}
    </label>
  );
}
