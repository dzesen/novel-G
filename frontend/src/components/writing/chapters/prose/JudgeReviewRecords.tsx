"use client";

import { useEffect, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { apiGet } from "@/lib/api";
import ReviewValidationDetails from "./ReviewValidationDetails";

type Usage = { input_tokens: number; output_tokens: number; total_tokens: number };
type ReviewStatus = "pass" | "findings" | "needs_review" | "review_incomplete" | "running";

interface ReviewSummary {
  id: string;
  origin: "archive" | "legacy_job";
  status: ReviewStatus;
  created_at: string | null;
  source_run_id: string | null;
  source_run_revision: number | null;
  source_content_digest: string | null;
  outline_digest: string | null;
  review_protocol: string | null;
  provider_alias: string | null;
  model: string | null;
  duration_ms: number | null;
  round_count: number;
  usage: Usage | null;
  failure_code: string | null;
}

interface ReviewRound {
  ordinal: number;
  phase: string;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  provider_alias: string | null;
  model: string | null;
  accounting_state: string;
  usage: Usage | null;
  finish_reason: string;
  visible_text: string | null;
  parsed_json: unknown;
  representation: string;
  local_validation: "valid" | "invalid" | "not_checked" | "not_recorded";
  validation_issues: unknown;
  response_complete: boolean;
  truncated: boolean;
  redacted: boolean;
}

interface ReviewDetail extends ReviewSummary {
  evidence: Record<string, unknown> | null;
  evidence_excerpts: Array<{ path: string; start: number; end: number; quote: string }>;
  diagnostics: unknown;
  rounds: ReviewRound[];
}

interface ReviewPage {
  records: ReviewSummary[];
  legacy_records: ReviewSummary[];
  next_cursor: string | null;
}

const textButton = "min-h-11 rounded-md px-2 text-sm font-medium text-foreground hover:bg-surface-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus disabled:cursor-not-allowed disabled:opacity-50";
const dataBlock = "mt-2 max-h-80 min-w-0 overflow-y-auto whitespace-pre-wrap break-all rounded-md bg-surface-secondary/60 p-3 text-xs leading-6 text-foreground";

function statusKey(value: string): ReviewStatus {
  return ["pass", "findings", "needs_review", "review_incomplete", "running"].includes(value) ? value as ReviewStatus : "review_incomplete";
}

function validationKey(value: string): ReviewRound["local_validation"] {
  return ["valid", "invalid", "not_checked", "not_recorded"].includes(value) ? value as ReviewRound["local_validation"] : "not_recorded";
}

function ReviewRecordDetail({ record, label }: { record: ReviewDetail; label: string }) {
  const t = useTranslations("writing.judgeReviews");
  const locale = useLocale();
  const unknown = t("unknown");
  const duration = (value: number | null) => value == null ? unknown : t("seconds", { seconds: (value / 1000).toFixed(1) });
  const timestamp = (value: string | null) => value ? new Date(value).toLocaleString(locale) : unknown;
  const summary = typeof record.evidence?.summary === "string" ? record.evidence.summary : null;
  const explanations = ["beat_evidence", "findings", "unknowns"].flatMap((field) => {
    const items = record.evidence?.[field];
    return Array.isArray(items) ? items.flatMap((item, index) => (
      item && typeof item === "object" && typeof item.explanation === "string"
        ? [{ path: `${field}[${index}]`, text: item.explanation as string, status: String(item.status ?? "") }]
        : []
    )) : [];
  });

  return (
    <article className="min-w-0 space-y-4" aria-label={label} data-testid="judge-review-detail">
      <div>
        <h4 className="text-sm font-semibold text-foreground">{label}</h4>
        <p className="mt-1 text-sm font-medium">{t(`statuses.${statusKey(record.status)}`)}</p>
        <p className="mt-1 text-xs leading-5 text-muted">{t(record.status === "review_incomplete" ? "incompleteHint" : record.status === "running" ? "unfinishedHint" : "conclusionHint")}</p>
      </div>
      <dl className="grid min-w-0 grid-cols-2 gap-x-4 gap-y-3 text-xs leading-5">
        {[
          [t("provider"), record.provider_alias ?? unknown],
          [t("model"), record.model ?? unknown],
          [t("duration"), duration(record.duration_ms)],
          [t("requests"), String(record.round_count)],
          [t("tokens"), record.usage?.total_tokens?.toLocaleString(locale) ?? unknown],
          [t("cost"), t("costUnknown")],
          [t("version"), record.source_run_revision == null ? unknown : t("revision", { revision: record.source_run_revision })],
          [t("recordedAt"), timestamp(record.created_at)],
        ].map(([name, value]) => <div className="min-w-0" key={name}><dt className="text-muted">{name}</dt><dd className="mt-0.5 break-words text-foreground [overflow-wrap:anywhere]">{value}</dd></div>)}
      </dl>
      {record.origin === "legacy_job" && <p className="text-sm leading-6">{t("legacyMissing")}</p>}
      {record.failure_code && <p className="break-all text-xs text-muted">{t("failureCode")} <code>{record.failure_code}</code></p>}
      {summary && <p className="whitespace-pre-wrap break-words text-sm leading-7">{summary}</p>}
      {record.diagnostics != null && <ReviewValidationDetails diagnostics={record.diagnostics} />}
      {explanations.length > 0 && (
        <details className="min-w-0">
          <summary className="cursor-pointer py-2 text-sm font-medium">{t("explanations", { count: explanations.length })}</summary>
          <ol className="mt-2 max-h-80 space-y-4 overflow-y-auto text-sm leading-7">
            {explanations.map((item) => <li className="min-w-0" key={item.path}><code className="break-all text-xs text-muted">{item.path} · {item.status}</code><p className="whitespace-pre-wrap break-words">{item.text}</p></li>)}
          </ol>
        </details>
      )}
      {record.evidence_excerpts?.length > 0 && (
        <details className="min-w-0">
          <summary className="cursor-pointer py-2 text-sm font-medium">{t("excerpts", { count: record.evidence_excerpts.length })}</summary>
          <div className="mt-2 max-h-80 space-y-4 overflow-y-auto">
            {record.evidence_excerpts.map((excerpt, index) => <div className="min-w-0" key={`${excerpt.path}-${index}`}><code className="break-all text-xs text-muted">{excerpt.path} · {excerpt.start}–{excerpt.end}</code><blockquote className="mt-1 whitespace-pre-wrap break-words text-sm leading-7">{excerpt.quote}</blockquote></div>)}
          </div>
        </details>
      )}
      <details className="min-w-0">
        <summary className="cursor-pointer py-2 text-sm font-medium">{t("source")}</summary>
        <dl className="mt-2 space-y-2 text-xs leading-6">
          {[[t("runId"), record.source_run_id], [t("proseDigest"), record.source_content_digest], [t("outlineDigest"), record.outline_digest], [t("protocol"), record.review_protocol]].map(([name, value]) => <div key={name}><dt className="text-muted">{name}</dt><dd className="break-all font-mono">{value ?? unknown}</dd></div>)}
        </dl>
        <p className="mt-2 text-xs leading-5 text-muted">{t("sourceHint")}</p>
      </details>
      {record.evidence && <details className="min-w-0"><summary className="cursor-pointer py-2 text-sm font-medium">{t("localResult")}</summary><pre className={dataBlock}>{JSON.stringify(record.evidence, null, 2)}</pre></details>}
      <div className="space-y-5 border-t border-border pt-4">
        {record.rounds.map((round) => (
          <section className="min-w-0" key={round.ordinal} aria-label={t("round", { round: round.ordinal })}>
            <h5 className="text-sm font-semibold">{t("round", { round: round.ordinal })} · {t(round.phase === "primary" ? "primary" : round.phase === "repair" ? "repair" : "otherRound")}</h5>
            <p className="mt-1 break-words text-xs leading-5 text-muted">{round.provider_alias ?? unknown} · {round.model ?? unknown} · {duration(round.duration_ms)}</p>
            <p className="mt-1 text-xs leading-5">{t(`validation.${validationKey(round.local_validation)}`)}</p>
            <p className="mt-1 text-xs leading-5 text-muted">{t("roundUsage", { input: round.usage?.input_tokens ?? unknown, output: round.usage?.output_tokens ?? unknown })}</p>
            {round.validation_issues != null && <ReviewValidationDetails diagnostics={round.validation_issues} />}
            {round.visible_text == null ? <p className="mt-2 text-xs leading-5 text-muted">{t("rawMissing")}</p> : (
              <>
                {(!round.response_complete || round.truncated) && <p className="mt-2 text-sm font-medium">{t(round.truncated ? "truncated" : "partial")}</p>}
                {round.redacted && <p className="mt-1 text-xs text-muted">{t("redacted")}</p>}
                <details className="mt-2 min-w-0"><summary className="cursor-pointer py-2 text-sm font-medium">{t(round.representation === "normalized_json" ? "normalized" : "raw")}</summary><pre className={dataBlock}>{round.visible_text || t("emptyResponse")}</pre></details>
                <details className="min-w-0"><summary className="cursor-pointer py-2 text-sm font-medium">{t("parsed")}</summary><p className="mt-1 text-xs leading-5 text-muted">{t("parsedHint")}</p>{round.parsed_json == null ? <p className="mt-2 text-sm">{t("notParsed")}</p> : <pre className={dataBlock}>{JSON.stringify(round.parsed_json, null, 2)}</pre>}</details>
              </>
            )}
            <details className="min-w-0"><summary className="cursor-pointer py-2 text-xs text-muted">{t("requestDetails")}</summary><dl className="mt-2 space-y-1 text-xs leading-5"><div><dt>{t("started")}</dt><dd>{timestamp(round.started_at)}</dd></div><div><dt>{t("finished")}</dt><dd>{timestamp(round.finished_at)}</dd></div><div><dt>{t("finishReason")}</dt><dd className="break-all">{round.finish_reason}</dd></div><div><dt>{t("accounting")}</dt><dd className="break-all">{round.accounting_state}</dd></div></dl></details>
          </section>
        ))}
      </div>
    </article>
  );
}

export default function JudgeReviewRecords({ chapterId, refreshKey = "" }: { chapterId: string; refreshKey?: string }) {
  const t = useTranslations("writing.judgeReviews");
  const locale = useLocale();
  const [open, setOpen] = useState(false);
  const [reload, setReload] = useState(0);
  const [page, setPage] = useState<ReviewPage | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [details, setDetails] = useState<Record<string, ReviewDetail>>({});
  const [error, setError] = useState(false);
  const [detailError, setDetailError] = useState(false);
  const [loading, setLoading] = useState(false);
  const [moreLoading, setMoreLoading] = useState(false);
  const endpoint = `/api/llm/prose-runs/chapter/${encodeURIComponent(chapterId)}/judge-reviews`;

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setLoading(true);
    setError(false);
    setDetails({});
    apiGet<ReviewPage>(endpoint, { signal: controller.signal }).then((result) => {
      if (controller.signal.aborted) return;
      setPage(result);
      const available = [...result.records, ...result.legacy_records];
      setSelected((current) => {
        const retained = current.filter((id) => available.some((item) => item.id === id));
        return retained.length ? retained : available[0] ? [available[0].id] : [];
      });
    }).catch(() => { if (!controller.signal.aborted) setError(true); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [endpoint, open, refreshKey, reload]);

  useEffect(() => {
    if (!open || !selected.length || loading) return;
    const controller = new AbortController();
    setDetailError(false);
    Promise.all(selected.map(async (id) => [id, await apiGet<ReviewDetail>(`${endpoint}/${encodeURIComponent(id)}`, { signal: controller.signal })] as const))
      .then((results) => { if (!controller.signal.aborted) setDetails(Object.fromEntries(results)); })
      .catch(() => { if (!controller.signal.aborted) setDetailError(true); });
    return () => controller.abort();
  }, [endpoint, open, selected, reload, loading]);

  const rows = page ? [...page.records, ...page.legacy_records] : [];
  const label = (record: ReviewSummary) => record.created_at ? new Date(record.created_at).toLocaleString(locale) : t("unknown");
  const loadMore = async () => {
    if (!page?.next_cursor || moreLoading) return;
    setMoreLoading(true);
    setError(false);
    try {
      const next = await apiGet<ReviewPage>(`${endpoint}?before=${encodeURIComponent(page.next_cursor)}`);
      setPage((current) => current ? { ...current, records: [...current.records, ...next.records], next_cursor: next.next_cursor } : next);
    } catch { setError(true); }
    finally { setMoreLoading(false); }
  };

  return (
    <details className="@container min-w-0 border-t border-border py-2" onToggle={(event) => setOpen(event.currentTarget.open)} data-testid="judge-review-records">
      <summary className="cursor-pointer py-3 text-sm font-medium text-foreground">{t("title")}</summary>
      {open && <div className="min-w-0 space-y-4 pb-4">
        <div className="flex flex-wrap items-start justify-between gap-2"><p className="max-w-prose text-xs leading-6 text-muted">{t("description")}</p><button type="button" className={textButton} disabled={loading || moreLoading} onClick={() => setReload((value) => value + 1)}>{t("refresh")}</button></div>
        {loading && <p role="status" className="text-sm">{t("loading")}</p>}
        {error && <p role="alert" className="text-sm">{t("loadError")}</p>}
        {!loading && !error && !rows.length && <p className="py-3 text-sm leading-6">{t("empty")}</p>}
        {rows.length > 0 && <>
          <p className="text-xs leading-5 text-muted">{t("compareHint")}</p>
          <ul className="max-h-72 min-w-0 divide-y divide-border overflow-y-auto">
            {rows.map((record) => <li key={record.id} className="flex min-w-0 items-start gap-2 py-1">
              <label className="flex min-h-11 w-9 shrink-0 cursor-pointer items-center justify-center"><input type="checkbox" className="size-4 accent-accent focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-focus" checked={selected.includes(record.id)} disabled={!selected.includes(record.id) && selected.length >= 2} aria-label={t("select", { time: label(record) })} onChange={(event) => setSelected((current) => event.target.checked ? [...current, record.id].slice(0, 2) : current.filter((id) => id !== record.id))} /></label>
              <button type="button" className={`${textButton} min-w-0 flex-1 py-2 text-left`} aria-pressed={selected.includes(record.id)} onClick={() => setSelected([record.id])}>
                <span className="block break-words text-xs text-muted">{label(record)} · {record.source_run_revision == null ? t("unknownVersion") : t("revision", { revision: record.source_run_revision })}</span>
                <span className="mt-1 block text-sm">{t(`statuses.${statusKey(record.status)}`)}</span>
                <span className="mt-1 block break-words text-xs text-muted [overflow-wrap:anywhere]">{record.model ?? record.provider_alias ?? t("unknown")}{record.origin === "legacy_job" ? ` · ${t("legacy")}` : ""}</span>
              </button>
            </li>)}
          </ul>
          {page?.next_cursor && <button type="button" className={textButton} disabled={moreLoading} onClick={() => void loadMore()}>{t(moreLoading ? "loading" : "more")}</button>}
        </>}
        {detailError && <p role="alert" className="text-sm">{t("detailError")}</p>}
        {!loading && !detailError && selected.length > 0 && <div className={`grid min-w-0 gap-6 border-t border-border pt-5 ${selected.length === 2 ? "@2xl:grid-cols-2" : ""}`} data-testid="judge-review-comparison">
          {selected.map((id) => details[id] ? <ReviewRecordDetail key={id} record={details[id]} label={label(details[id])} /> : <p key={id} role="status" className="text-sm">{t("loadingDetail")}</p>)}
        </div>}
      </div>}
    </details>
  );
}
