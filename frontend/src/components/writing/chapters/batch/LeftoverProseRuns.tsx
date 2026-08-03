"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@heroui/react";
import { apiGet, apiPost } from "@/lib/api";
import type { ChapterSummary } from "@/types/novel";
import { proseReasonTranslationKey } from "../prose/prosePresentation";
import type { LeftoverProseRun } from "./batchTypes";

interface LeftoverProseRunsProps {
  novelId: string;
  chapters: ChapterSummary[];
  refreshKey: string;
  onOpenRun: (run: LeftoverProseRun) => void;
  onStartFresh: (chapterId: string) => void;
}

export default function LeftoverProseRuns({
  novelId,
  chapters,
  refreshKey,
  onOpenRun,
  onStartFresh,
}: LeftoverProseRunsProps) {
  const t = useTranslations("writing.batch");
  const tProse = useTranslations("writing.prose");
  const [runs, setRuns] = useState<LeftoverProseRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [actionError, setActionError] = useState("");
  const [discardArmed, setDiscardArmed] = useState<string | null>(null);
  const [busyRunId, setBusyRunId] = useState<string | null>(null);
  const loadSequenceRef = useRef(0);
  const mountedRef = useRef(true);

  const chapterById = useMemo(
    () => new Map(chapters.map((chapter) => [chapter._id, chapter])),
    [chapters],
  );

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      loadSequenceRef.current += 1;
    };
  }, []);

  const load = useCallback(async () => {
    const sequence = ++loadSequenceRef.current;
    setLoading(true);
    setLoadError("");
    try {
      const response = await apiGet<LeftoverProseRun[]>(
        `/api/llm/prose-runs/novel/${novelId}/leftovers`,
      );
      if (!mountedRef.current || sequence !== loadSequenceRef.current) return;
      setRuns(response);
    } catch (error) {
      if (!mountedRef.current || sequence !== loadSequenceRef.current) return;
      setLoadError(error instanceof Error ? error.message : String(error));
    } finally {
      if (mountedRef.current && sequence === loadSequenceRef.current) {
        setLoading(false);
      }
    }
  }, [novelId]);

  useEffect(() => {
    void load();
    return () => {
      loadSequenceRef.current += 1;
    };
  }, [load, refreshKey]);

  const reasonLabel = (reasonCode: string) => {
    const key = proseReasonTranslationKey(reasonCode);
    return key
      ? tProse(`reasons.${key}`)
      : tProse("reasons.unknown", { code: reasonCode });
  };

  const availabilityText = (run: LeftoverProseRun) => {
    if (run.status === "stale") return t("leftoverStale");
    if (run.status === "superseded") return t("leftoverSuperseded");
    if (run.continuation_exhausted) return t("leftoverResumeExhausted");
    if (run.has_uncertain_attempt) return t("leftoverUncertain");
    return t("leftoverIncomplete");
  };

  const availableActionsText = (run: LeftoverProseRun) => {
    const actions = [t("leftoverView")];
    if (run.can_resume) actions.push(t("leftoverResume"));
    if (run.can_accept_partial) actions.push(t("leftoverAcceptPartial"));
    if (run.can_discard) actions.push(t("leftoverDiscardRestart"));
    return t("leftoverAvailableActions", {
      actions: actions.join(t("leftoverActionSeparator")),
    });
  };

  const discardAndStartFresh = async (run: LeftoverProseRun) => {
    if (!run.can_discard || busyRunId) return;
    setBusyRunId(run.run_id);
    setActionError("");
    try {
      await apiPost(`/api/llm/prose-runs/${run.run_id}/discard`, {
        novel_id: novelId,
        chapter_id: run.chapter_id,
        expected_run_revision: run.revision,
      });
      if (!mountedRef.current) return;
      setRuns((current) => current.filter((item) => item.run_id !== run.run_id));
      setDiscardArmed(null);
      onStartFresh(run.chapter_id);
    } catch (error) {
      if (!mountedRef.current) return;
      setActionError(error instanceof Error ? error.message : String(error));
    } finally {
      if (mountedRef.current) setBusyRunId(null);
    }
  };

  if (loading && runs.length === 0) {
    return (
      <div className="shrink-0 border-b border-border bg-surface px-4 py-3">
        <p className="text-xs text-muted">{t("leftoverLoading")}</p>
      </div>
    );
  }

  if (!loading && runs.length === 0 && !loadError) return null;

  return (
    <section
      aria-labelledby="leftover-prose-title"
      className="shrink-0 border-b border-amber-200 bg-amber-50/70 px-4 py-3 dark:border-amber-900/60 dark:bg-amber-950/25"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3
            id="leftover-prose-title"
            className="text-sm font-semibold text-amber-950 dark:text-amber-100"
          >
            {t("leftoverTitle")}
          </h3>
          <p className="mt-1 max-w-3xl text-xs leading-5 text-amber-900/80 dark:text-amber-100/75">
            {t("leftoverDescription", { count: runs.length })}
          </p>
        </div>
        {loadError && (
          <Button
            variant="outline"
            size="sm"
            onPress={() => void load()}
            isDisabled={loading}
          >
            {loading ? t("leftoverRetrying") : t("leftoverRetry")}
          </Button>
        )}
      </div>

      {loadError && (
        <p
          role="alert"
          className="mt-2 text-xs text-red-700 dark:text-red-300"
        >
          {t("leftoverLoadError", { message: loadError })}
        </p>
      )}
      {actionError && (
        <p
          role="alert"
          className="mt-2 text-xs text-red-700 dark:text-red-300"
        >
          {t("leftoverActionError", { message: actionError })}
        </p>
      )}

      {runs.length > 0 && (
        <ul className="mt-3 max-h-80 divide-y divide-amber-200 overflow-y-auto overscroll-contain rounded-md border border-amber-200 bg-surface sm:max-h-96 dark:divide-amber-900/60 dark:border-amber-900/60">
          {runs.map((run) => {
            const chapter = chapterById.get(run.chapter_id);
            const title = chapter
              ? t("chapterRowTitle", {
                  order: chapter.order_index,
                  title: chapter.title,
                })
              : t("leftoverUnknownChapter");
            const reasons = run.reason_codes.length > 0
              ? run.reason_codes.map(reasonLabel)
              : [tProse("reasons.unreported")];
            const busy = busyRunId === run.run_id;
            const armed = discardArmed === run.run_id;

            return (
              <li key={run.run_id} className="px-3 py-3">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <button
                      type="button"
                      onClick={() => onOpenRun(run)}
                      disabled={Boolean(busyRunId)}
                      className="max-w-full break-words text-left text-sm font-medium text-foreground hover:text-accent hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-not-allowed disabled:opacity-50 disabled:no-underline"
                    >
                      {title}
                    </button>
                    <p className="mt-1 text-xs leading-5 text-muted">
                      {t("leftoverWritten", { count: run.draft_word_count })}
                      <span aria-hidden="true"> · </span>
                      {t("leftoverReason", { reasons: reasons.join(t("reasonSeparator")) })}
                    </p>
                    <p className="mt-1 text-xs leading-5 text-amber-800 dark:text-amber-200">
                      {availabilityText(run)} {availableActionsText(run)}
                    </p>
                  </div>

                  <div className="flex flex-wrap justify-end gap-2">
                    <Button
                      variant="ghost"
                      size="sm"
                      onPress={() => onOpenRun(run)}
                      isDisabled={Boolean(busyRunId)}
                    >
                      {t("leftoverView")}
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      onPress={() => onOpenRun(run)}
                      isDisabled={!run.can_resume || Boolean(busyRunId)}
                    >
                      {t("leftoverResume")}
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      onPress={() => onOpenRun(run)}
                      isDisabled={!run.can_accept_partial || Boolean(busyRunId)}
                    >
                      {t("leftoverAcceptPartial")}
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="text-red-600 dark:text-red-400"
                      onPress={() => setDiscardArmed(run.run_id)}
                      isDisabled={!run.can_discard || Boolean(busyRunId)}
                    >
                      {t("leftoverDiscardRestart")}
                    </Button>
                  </div>
                </div>

                {armed && (
                  <div
                    role="alert"
                    className="mt-3 flex flex-wrap items-center justify-between gap-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 dark:border-red-900/60 dark:bg-red-950/30"
                  >
                    <p className="min-w-0 flex-1 text-xs leading-5 text-red-800 dark:text-red-200">
                      {t("leftoverDiscardConfirm")}
                    </p>
                    <div className="flex shrink-0 gap-2">
                      <Button
                        variant="ghost"
                        size="sm"
                        onPress={() => setDiscardArmed(null)}
                        isDisabled={busy}
                      >
                        {t("leftoverCancel")}
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        className="border-red-300 text-red-700 dark:border-red-800 dark:text-red-300"
                        onPress={() => void discardAndStartFresh(run)}
                        isDisabled={busy}
                      >
                        {busy ? t("leftoverDiscarding") : t("leftoverConfirmDiscard")}
                      </Button>
                    </div>
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
