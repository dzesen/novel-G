"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useLocale, useTranslations } from "next-intl";
import { apiGet, apiPost } from "@/lib/api";
import { WORLD_BASELINE_DOMAINS, hasCompleteWorldBaselineDecisions, reusableWorldBaselineDecisions } from "@/lib/worldBaseline";
import type { WorldBaselineDecisionDraft, WorldBaselineView } from "@/types/novel";

export default function WorldBaselineReviewFollowUp({ novelId }: { novelId: string }) {
  const t = useTranslations("writing.worldBaseline");
  const locale = useLocale();
  const [baseline, setBaseline] = useState<WorldBaselineView | null>(null);
  const [decisions, setDecisions] = useState<WorldBaselineDecisionDraft>({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [reload, setReload] = useState(0);
  useEffect(() => {
    let active = true;
    apiGet<WorldBaselineView>(`/api/novels/${novelId}/world-baseline`).then((result) => {
      if (!active) return;
      setBaseline(result);
      setDecisions(reusableWorldBaselineDecisions(result));
      setError("");
    }).catch(() => { if (active) setError(t("loadFailed")); });
    return () => { active = false; };
  }, [novelId, reload, t]);
  const basePath = `/${locale}/writing/${encodeURIComponent(novelId)}`;
  const linkClass = "inline-flex min-h-10 items-center rounded-md border border-accent/40 px-3 py-2 text-sm font-semibold text-accent focus-visible:outline-2 focus-visible:outline-accent";
  if (!baseline && !error) return null;
  if (baseline?.state === "not_required_legacy") return null;
  const needsReview = baseline?.state === "stale";
  const retained = baseline ? reusableWorldBaselineDecisions(baseline) : {};
  const changed = WORLD_BASELINE_DOMAINS.filter((domain) => !retained[domain]);
  const confirm = async () => {
    if (!baseline || !hasCompleteWorldBaselineDecisions(decisions) || busy) return;
    setBusy(true);
    setError("");
    try {
      const result = await apiPost<WorldBaselineView>(`/api/novels/${novelId}/world-baseline/confirm`, {
        decisions, expected_review_digest: baseline.review_digest,
      });
      setBaseline(result);
    } catch {
      setError(t("confirmChanged"));
      // Clear all draft approvals; the next read supplies only proven unchanged ones.
      setDecisions({});
    } finally { setBusy(false); }
  };
  return (
    <section aria-label={t("followUpTitle")} className="mt-5 min-w-0 border-y border-border py-5">
      <h2 className="text-base font-semibold text-foreground">{t("followUpTitle")}</h2>
      {needsReview ? <>
        <p className="mt-2 text-sm leading-6 text-muted">{t("incrementalReview", { count: Object.keys(retained).length })}</p>
        <div className="mt-3 divide-y divide-border">
          {changed.map((domain) => <fieldset key={domain} className="flex min-w-0 flex-wrap items-center gap-x-5 gap-y-2 py-3">
            <legend className="text-sm font-semibold text-foreground">{t(`domains.${domain}.label`)}</legend>
            {(["reviewed", "not_applicable"] as const).map((value) => <label key={value} className="flex min-h-10 items-center gap-2 text-sm text-foreground">
              <input type="radio" name={`followup-${domain}`} checked={decisions[domain] === value} disabled={busy}
                onChange={() => setDecisions((current) => ({...current, [domain]: value}))} />
              {t(value === "reviewed" ? "reviewed" : "notApplicable")}
            </label>)}
          </fieldset>)}
        </div>
        <button type="button" onClick={() => void confirm()} disabled={busy || Boolean(error) || !hasCompleteWorldBaselineDecisions(decisions)}
          className="mt-3 min-h-11 max-w-full rounded-md bg-accent px-4 py-2 text-sm font-semibold text-white disabled:opacity-45 focus-visible:outline-2 focus-visible:outline-accent">
          {t(busy ? "confirming" : "confirmChanges")}
        </button>
      </> : <p className="mt-2 text-sm leading-6 text-muted">{t(baseline?.state === "current" ? "reviewReady" : "reviewPending")}</p>}
      {error && <div className="mt-3" role="alert"><p className="text-sm text-danger">{error}</p>
        <button type="button" className={linkClass} onClick={() => setReload((value) => value + 1)}>{t("retry")}</button>
      </div>}
      <div className="mt-4 flex min-w-0 flex-wrap gap-2">
        {baseline?.state === "current"
          ? <Link className={linkClass} href={`${basePath}?area=auto-book&view=runs`}>{t("returnAutoBook")}</Link>
          : <Link className={linkClass} href={`${basePath}?area=world&view=baseline`}>{t("openBaseline")}</Link>}
      </div>
    </section>
  );
}
