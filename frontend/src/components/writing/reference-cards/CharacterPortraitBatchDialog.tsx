"use client";

import { Button } from "@heroui/react";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, apiGet, apiPost } from "@/lib/api";
import type {
  CharacterPortraitBatch,
  CharacterPortraitBatchDraft,
  CharacterPortraitBatchPlan,
} from "@/types/image";

interface CharacterPortraitBatchDialogProps {
  novelId: string;
  isOpen: boolean;
  drafts: CharacterPortraitBatchDraft[];
  onClose: () => void;
  onRemoveDraft: (cardId: string) => void;
  onTerminal: (batch: CharacterPortraitBatch) => void;
}

const POLL_INTERVAL_MS = 1_200;

function errorCode(reason: unknown): string {
  if (!(reason instanceof ApiError) || !reason.detail) return "";
  if (typeof reason.detail !== "object") return "";
  const code = (reason.detail as { code?: unknown }).code;
  return typeof code === "string" ? code : "";
}

function planConfirmation(plan: CharacterPortraitBatchPlan) {
  return {
    plan_revision: plan.plan_revision,
    plan_digest: plan.plan_digest,
    provider_alias: plan.provider_alias,
    provider_model: plan.provider_model,
    workflow_revision: plan.workflow_revision,
    total_images: plan.total_images,
    unit_estimated_seconds: plan.unit_estimated_seconds,
    estimated_seconds: plan.estimated_seconds,
    queue_position: plan.queue_position,
    max_provider_requests: plan.max_provider_requests,
    max_concurrency: plan.max_concurrency,
    estimate_source: plan.estimate_source,
  };
}

export default function CharacterPortraitBatchDialog({
  novelId,
  isOpen,
  drafts,
  onClose,
  onRemoveDraft,
  onTerminal,
}: CharacterPortraitBatchDialogProps) {
  const t = useTranslations("writing.referenceCards.portraitBatch");
  const [plan, setPlan] = useState<CharacterPortraitBatchPlan | null>(null);
  const [batch, setBatch] = useState<CharacterPortraitBatch | null>(null);
  const [loading, setLoading] = useState(false);
  const [starting, setStarting] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const advancingRef = useRef(false);
  const terminalNotifiedRef = useRef(new Set<string>());
  const batchBase =
    `/api/reference-cards/novel/${novelId}/character/portrait-batches`;
  const draftSignature = useMemo(
    () => JSON.stringify(drafts.map(({ card_id, prompt }) => ({ card_id, prompt }))),
    [drafts],
  );
  const requestItems = useMemo(
    () => drafts.map(({ card_id, prompt }) => ({ card_id, prompt })),
    [drafts],
  );

  const close = useCallback(() => {
    setPlan(null);
    setBatch(null);
    setConfirmed(false);
    setError(null);
    onClose();
  }, [onClose]);

  const formatDuration = (seconds: number): string => {
    const rounded = Math.max(0, Math.round(seconds));
    if (rounded < 60) return t("durationSeconds", { count: rounded });
    const minutes = Math.floor(rounded / 60);
    const remainder = rounded % 60;
    return remainder
      ? t("durationMinutesSeconds", { minutes, seconds: remainder })
      : t("durationMinutes", { count: minutes });
  };

  const messageForError = (reason: unknown, fallback: string): string => {
    const code = errorCode(reason);
    if (code === "portrait_batch_plan_stale") return t("planStale");
    if (code === "portrait_batch_conflict") return t("conflict");
    return reason instanceof Error ? reason.message : fallback;
  };

  useEffect(() => {
    if (!isOpen || batch) return;
    let active = true;
    setLoading(true);
    setError(null);
    setConfirmed(false);

    void (async () => {
      try {
        const current = await apiGet<CharacterPortraitBatch | null>(
          `${batchBase}/current`,
        );
        if (!active) return;
        if (current) {
          setBatch(current);
          setPlan(null);
          return;
        }
        setBatch(null);
        if (requestItems.length === 0) {
          setPlan(null);
          return;
        }
        const nextPlan = await apiPost<CharacterPortraitBatchPlan>(
          `${batchBase}/plan`,
          { items: requestItems },
        );
        if (active) setPlan(nextPlan);
      } catch (reason) {
        if (active) setError(messageForError(reason, t("loadFailed")));
      } finally {
        if (active) setLoading(false);
      }
    })();

    return () => {
      active = false;
    };
    // draftSignature deliberately retriggers the frozen-plan preflight when
    // the user adds, updates, or removes one staged prompt.
  }, [batch, batchBase, draftSignature, isOpen]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!isOpen || !batch || batch.terminal) return;
    let active = true;

    const advance = async () => {
      if (advancingRef.current) return;
      advancingRef.current = true;
      try {
        const next = await apiPost<CharacterPortraitBatch>(
          `${batchBase}/${batch.batch_id}/advance`,
          {},
        );
        if (active) {
          setBatch(next);
          setError(null);
        }
      } catch (reason) {
        if (active) setError(messageForError(reason, t("advanceFailed")));
      } finally {
        advancingRef.current = false;
      }
    };

    void advance();
    const timer = window.setInterval(() => void advance(), POLL_INTERVAL_MS);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [batch?.batch_id, batch?.terminal, batchBase, isOpen]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!batch?.terminal || terminalNotifiedRef.current.has(batch.batch_id)) {
      return;
    }
    terminalNotifiedRef.current.add(batch.batch_id);
    onTerminal(batch);
  }, [batch, onTerminal]);

  useEffect(() => {
    if (!isOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !starting && !cancelling) close();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [cancelling, close, isOpen, starting]);

  const start = async () => {
    if (!plan || !confirmed || starting) return;
    setStarting(true);
    setError(null);
    try {
      const next = await apiPost<CharacterPortraitBatch>(batchBase, {
        items: requestItems,
        provider_alias: plan.provider_alias,
        expected_plan: planConfirmation(plan),
        confirm: true,
      });
      setBatch(next);
      setPlan(null);
    } catch (reason) {
      setError(messageForError(reason, t("startFailed")));
      if (errorCode(reason) === "portrait_batch_plan_stale") setPlan(null);
    } finally {
      setStarting(false);
    }
  };

  const cancel = async () => {
    if (!batch || batch.terminal || cancelling) return;
    if (!window.confirm(t("cancelWarning"))) return;
    setCancelling(true);
    setError(null);
    try {
      const next = await apiPost<CharacterPortraitBatch>(
        `${batchBase}/${batch.batch_id}/cancel`,
        {},
      );
      setBatch(next);
    } catch (reason) {
      setError(messageForError(reason, t("cancelFailed")));
    } finally {
      setCancelling(false);
    }
  };

  if (!isOpen) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-stretch justify-center bg-black/45 p-0 sm:items-center sm:p-5"
      role="presentation"
    >
      <section
        role="dialog"
        aria-modal="true"
        aria-labelledby="portrait-batch-title"
        className="flex h-full w-full max-w-3xl flex-col overflow-hidden bg-background shadow-2xl sm:h-[min(760px,calc(100vh-2.5rem))] sm:rounded-2xl sm:border sm:border-border"
      >
        <header className="flex shrink-0 items-start justify-between gap-4 border-b border-border px-5 py-4 sm:px-7">
          <div className="min-w-0">
            <p className="text-xs font-semibold uppercase tracking-[0.16em] text-accent">
              {t("eyebrow")}
            </p>
            <h2 id="portrait-batch-title" className="mt-1 text-xl font-semibold text-foreground">
              {t("title")}
            </h2>
            <p className="mt-1 max-w-2xl text-sm leading-6 text-muted">
              {t("description")}
            </p>
          </div>
          <Button
            variant="ghost"
            isDisabled={starting || cancelling}
            onPress={close}
            aria-label={t("close")}
          >
            ✕
          </Button>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-5 sm:px-7">
          {loading && <p role="status" className="text-sm text-muted">{t("loading")}</p>}
          {error && (
            <div role="alert" className="rounded-lg border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-800 dark:border-red-900 dark:bg-red-950 dark:text-red-200">
              {error}
            </div>
          )}

          {!loading && !batch && drafts.length === 0 && (
            <div className="rounded-xl border border-dashed border-border p-6 text-center">
              <p className="font-medium text-foreground">{t("emptyTitle")}</p>
              <p className="mt-2 text-sm leading-6 text-muted">{t("emptyDescription")}</p>
            </div>
          )}

          {!batch && drafts.length > 0 && (
            <div className="space-y-5">
              <section aria-labelledby="portrait-batch-items-title">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <h3 id="portrait-batch-items-title" className="font-semibold text-foreground">
                    {t("prepared", { count: drafts.length })}
                  </h3>
                  <p className="text-xs text-muted">{t("firstAnchorOnly")}</p>
                </div>
                <ul className="mt-3 divide-y divide-border rounded-xl border border-border bg-surface">
                  {drafts.map((draft, index) => (
                    <li key={draft.card_id} className="flex min-w-0 items-center gap-3 px-4 py-3">
                      <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-accent/10 text-xs font-semibold text-accent">
                        {index + 1}
                      </span>
                      <span className="min-w-0 flex-1 truncate text-sm font-medium text-foreground" title={draft.card_name}>
                        {draft.card_name}
                      </span>
                      <Button size="sm" variant="ghost" onPress={() => onRemoveDraft(draft.card_id)}>
                        {t("remove")}
                      </Button>
                    </li>
                  ))}
                </ul>
              </section>

              {plan && (
                <section aria-labelledby="portrait-batch-plan-title" className="rounded-xl border border-accent/25 bg-accent/5 p-4">
                  <h3 id="portrait-batch-plan-title" className="font-semibold text-foreground">{t("planTitle")}</h3>
                  <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-2">
                    <div><dt className="text-muted">{t("totalImages")}</dt><dd className="mt-1 font-semibold text-foreground">{plan.total_images}</dd></div>
                    <div><dt className="text-muted">{t("estimatedTime")}</dt><dd className="mt-1 font-semibold text-foreground">{formatDuration(plan.estimated_seconds)}</dd></div>
                    <div><dt className="text-muted">{t("maxRequests")}</dt><dd className="mt-1 font-semibold text-foreground">{plan.max_provider_requests}</dd></div>
                    <div><dt className="text-muted">{t("maxConcurrency")}</dt><dd className="mt-1 font-semibold text-foreground">{plan.max_concurrency}</dd></div>
                    <div className="min-w-0"><dt className="text-muted">{t("provider")}</dt><dd className="mt-1 truncate font-semibold text-foreground" title={plan.provider_alias}>{plan.provider_alias}</dd></div>
                    <div className="min-w-0"><dt className="text-muted">{t("model")}</dt><dd className="mt-1 truncate font-semibold text-foreground" title={plan.provider_model}>{plan.provider_model}</dd></div>
                    <div><dt className="text-muted">{t("queuePosition")}</dt><dd className="mt-1 font-semibold text-foreground">{plan.queue_position}</dd></div>
                    <div><dt className="text-muted">{t("estimateSource")}</dt><dd className="mt-1 font-semibold text-foreground">{t(`estimateSources.${plan.estimate_source}`)}</dd></div>
                  </dl>
                  {plan.warnings.length > 0 && (
                    <ul className="mt-4 list-disc space-y-1 pl-5 text-xs leading-5 text-amber-800 dark:text-amber-200">
                      {plan.warnings.map((warning) => <li key={warning} className="break-words">{warning}</li>)}
                    </ul>
                  )}
                  <label className="mt-5 flex cursor-pointer items-start gap-3 rounded-lg border border-border bg-background px-3 py-3 text-sm leading-6 text-foreground">
                    <input
                      type="checkbox"
                      checked={confirmed}
                      onChange={(event) => setConfirmed(event.target.checked)}
                      className="mt-1 h-4 w-4 shrink-0 accent-[var(--accent)]"
                    />
                    <span>{t("confirmPlan", { count: plan.max_provider_requests })}</span>
                  </label>
                </section>
              )}
            </div>
          )}

          {batch && (
            <div className="space-y-5">
              <section className="rounded-xl border border-border bg-surface p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <p className="text-xs font-semibold uppercase tracking-[0.14em] text-accent">{t(`status.${batch.status}`)}</p>
                    <p className="mt-1 text-lg font-semibold text-foreground">{t("progress", { completed: batch.completed_images, total: batch.total_images })}</p>
                  </div>
                  <p className="text-sm text-muted">{t("elapsed", { duration: formatDuration(batch.elapsed_seconds) })}</p>
                </div>
                <div className="mt-4 h-2 overflow-hidden rounded-full bg-surface-secondary" aria-label={t("progressLabel")}>
                  <div className="h-full bg-accent transition-[width]" style={{ width: `${Math.min(100, (batch.completed_images / Math.max(1, batch.total_images)) * 100)}%` }} />
                </div>
                <dl className="mt-4 grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
                  <div><dt className="text-muted">{t("submitted")}</dt><dd className="mt-1 font-semibold text-foreground">{batch.submitted_requests}/{batch.max_provider_requests}</dd></div>
                  <div><dt className="text-muted">{t("succeeded")}</dt><dd className="mt-1 font-semibold text-foreground">{batch.succeeded_items}</dd></div>
                  <div><dt className="text-muted">{t("failed")}</dt><dd className="mt-1 font-semibold text-foreground">{batch.failed_items}</dd></div>
                  <div><dt className="text-muted">{t("cancelled")}</dt><dd className="mt-1 font-semibold text-foreground">{batch.cancelled_items}</dd></div>
                </dl>
                {batch.queue_position !== null && <p className="mt-3 text-xs text-muted">{t("runningQueue", { count: batch.queue_position })}</p>}
              </section>

              {(batch.failure || batch.request_upper_bound_exceeded) && (
                <div role="alert" className="rounded-lg border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-800 dark:border-red-900 dark:bg-red-950 dark:text-red-200">
                  <p className="font-semibold">{batch.failure?.message ?? t("upperBoundExceeded")}</p>
                  {batch.failure?.action && <p className="mt-1 leading-6">{batch.failure.action}</p>}
                </div>
              )}

              <ol className="divide-y divide-border rounded-xl border border-border bg-surface">
                {batch.items.map((item, index) => (
                  <li key={item.card_id} className="min-w-0 px-4 py-3">
                    <div className="flex min-w-0 items-center gap-3">
                      <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-surface-secondary text-xs font-semibold text-muted">{index + 1}</span>
                      <span className="min-w-0 flex-1 truncate text-sm font-medium text-foreground" title={item.card_name}>{item.card_name}</span>
                      <span className="shrink-0 rounded-full bg-surface-secondary px-2 py-1 text-xs text-muted">{t(`itemStatus.${item.status}`)}</span>
                    </div>
                    {item.failure && (
                      <p className="mt-2 break-words pl-10 text-xs leading-5 text-red-700 dark:text-red-300">{item.failure.message} {item.failure.action}</p>
                    )}
                  </li>
                ))}
              </ol>
            </div>
          )}
        </div>

        <footer className="flex shrink-0 flex-col-reverse gap-2 border-t border-border bg-surface px-5 py-4 sm:flex-row sm:items-center sm:justify-end sm:px-7">
          <Button variant="ghost" isDisabled={starting || cancelling} onPress={close}>{batch?.terminal ? t("done") : t("close")}</Button>
          {!batch && plan && (
            <Button className="bg-accent text-white hover:bg-accent-hover" variant="primary" isDisabled={!confirmed || starting} onPress={() => void start()}>
              {starting ? t("starting") : t("start", { count: plan.total_images })}
            </Button>
          )}
          {batch && !batch.terminal && (
            <Button variant="outline" className="border-red-300 text-red-700 dark:border-red-800 dark:text-red-300" isDisabled={cancelling} onPress={() => void cancel()}>
              {cancelling ? t("cancelling") : t("cancelBatch")}
            </Button>
          )}
        </footer>
      </section>
    </div>
  );
}
