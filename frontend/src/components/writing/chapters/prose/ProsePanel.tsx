"use client";

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";
import { useTranslations } from "next-intl";
import { Button } from "@heroui/react";
import { apiGet, apiPost } from "@/lib/api";
import {
  useProseStream,
  type ProseRunSnapshot,
} from "./useProseStream";
import OutlineGenerationParams, {
  EMPTY_GENERATION_PARAMS,
  toRequestParams,
  type GenerationParams,
} from "../outline/OutlineGenerationParams";
import { ContextNotices, Notice } from "../outline/outlineUi";
import { countChapterWords } from "../chapterUtils";
import {
  buildProseAcceptPayload,
  proseReasonTranslationKey,
  proseRequiresPartialAcknowledgement,
} from "./prosePresentation";

interface ProsePanelProps {
  novelId: string;
  chapterId: string;
  initialRun?: ProseRunSnapshot | null;
  /** 编辑器里当前正文是否非空。为真时接受需要二次确认（设计 §2）。 */
  hasExistingContent: boolean;
  onClose: () => void;
  onRunStateChanged?: () => void;
  onAccepted: (
    text: string,
    acceptanceState: "ai_complete" | "partial_manual_required",
  ) => void;
}

export default function ProsePanel({
  novelId,
  chapterId,
  initialRun = null,
  hasExistingContent,
  onClose,
  onRunStateChanged,
  onAccepted,
}: ProsePanelProps) {
  const t = useTranslations("writing.prose");
  const stream = useProseStream();
  const hydrateRun = stream.hydrate;
  const [params, setParams] = useState<GenerationParams>(EMPTY_GENERATION_PARAMS);
  const [overwriteArmed, setOverwriteArmed] = useState(false);
  const [partialArmed, setPartialArmed] = useState(false);
  const [uncertainRetryArmed, setUncertainRetryArmed] = useState(false);
  const [restoreLoading, setRestoreLoading] = useState(true);
  const [accepting, setAccepting] = useState(false);
  const [actionError, setActionError] = useState("");
  const [initialRunResolved, setInitialRunResolved] = useState(false);
  const dialogRef = useRef<HTMLDivElement>(null);

  const running = stream.status === "running";
  const hasText = stream.text.length > 0;
  const incomplete = hasText && (
    stream.status === "cancelled"
    || stream.status === "error"
    || stream.status === "incomplete"
    || Boolean(stream.completion && !stream.completion.can_write_formal_prose)
  );
  const partialAcceptance = Boolean(
    stream.completion
      ? proseRequiresPartialAcknowledgement(stream.completion)
      : incomplete,
  );
  const selectedInitialRun = initialRunResolved ? null : initialRun;
  const resumableDraft = Boolean(
    stream.runId
    && stream.runRevision != null
    && (selectedInitialRun ? selectedInitialRun.can_resume : incomplete),
  );
  const reasonCodes = (
    selectedInitialRun?.reason_codes?.length
      ? selectedInitialRun.reason_codes
      : stream.completion?.reason_codes
  ) ?? [];

  useEffect(() => {
    setInitialRunResolved(false);
  }, [initialRun?.run_id, initialRun?._id]);

  useEffect(() => {
    const previouslyFocused = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    const firstFocusable = dialogRef.current?.querySelector<HTMLElement>(
      'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    );
    firstFocusable?.focus();
    return () => {
      if (previouslyFocused && document.contains(previouslyFocused)) {
        previouslyFocused.focus();
      }
    };
  }, []);

  const restoreActive = useCallback(async () => {
    setRestoreLoading(true);
    try {
      const run = await apiGet<ProseRunSnapshot | null>(
        `/api/llm/prose-runs/chapter/${chapterId}`,
      );
      if (run) hydrateRun(run);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : String(error));
    } finally {
      setRestoreLoading(false);
    }
  }, [chapterId, hydrateRun]);

  useEffect(() => {
    if (initialRun) {
      hydrateRun(initialRun);
      setRestoreLoading(false);
      return;
    }
    void restoreActive();
  }, [hydrateRun, initialRun, restoreActive]);

  useEffect(() => {
    if (stream.status === "cancelled") void restoreActive();
  }, [restoreActive, stream.status]);

  const accept = async () => {
    if (!hasText || running) return;
    setActionError("");
    if (selectedInitialRun?.can_accept_partial === false) {
      setActionError(t("leftoverAcceptUnavailable"));
      return;
    }
    if (partialAcceptance && !partialArmed) {
      setPartialArmed(true);
      return;
    }
    if (hasExistingContent && !overwriteArmed) {
      setOverwriteArmed(true);
      return;
    }
    if (!stream.runId || stream.runRevision == null) {
      setActionError(t("runMissing"));
      return;
    }
    setAccepting(true);
    try {
      await apiPost(
        `/api/llm/prose-runs/${stream.runId}/accept`,
        buildProseAcceptPayload({
          novelId,
          chapterId,
          runId: stream.runId,
          runRevision: stream.runRevision,
          partial: partialAcceptance,
        }),
      );
      onAccepted(
        stream.text,
        partialAcceptance ? "partial_manual_required" : "ai_complete",
      );
      onClose();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : String(error));
      if (selectedInitialRun) {
        onRunStateChanged?.();
      } else {
        await restoreActive();
      }
    } finally {
      setAccepting(false);
    }
  };

  const startGeneration = () => {
    // 保险栓在每次重新生成时复位：上一份预览已被新的一轮取代，
    // 针对它的确认不该延续到下一份（2a Task 7 就栽在栓不复位上）。
    setOverwriteArmed(false);
    setPartialArmed(false);
    if (selectedInitialRun?.can_resume === false) {
      setActionError(t("leftoverResumeUnavailable"));
      return;
    }
    const resuming = resumableDraft;
    if (resuming && stream.hasUncertainAttempt && !uncertainRetryArmed) {
      setUncertainRetryArmed(true);
      return;
    }
    const confirmUncertainRetry = Boolean(
      stream.hasUncertainAttempt && uncertainRetryArmed,
    );
    setUncertainRetryArmed(false);
    setActionError("");
    if (selectedInitialRun) setInitialRunResolved(true);
    void stream.start({
      novel_id: novelId,
      chapter_id: chapterId,
      ...(resuming
        ? {
            resume_run_id: stream.runId,
            expected_run_revision: stream.runRevision,
            confirm_uncertain_retry: confirmUncertainRetry,
          }
        : {}),
      ...toRequestParams(params),
    });
  };

  const discard = async () => {
    setActionError("");
    if (stream.runId && stream.runRevision == null) {
      setActionError(t("runMissing"));
      return;
    }
    if (stream.runId) {
      try {
        await apiPost(`/api/llm/prose-runs/${stream.runId}/discard`, {
          novel_id: novelId,
          chapter_id: chapterId,
          expected_run_revision: stream.runRevision,
        });
      } catch (error) {
        setActionError(error instanceof Error ? error.message : String(error));
        return;
      }
    }
    // 保险栓是面板本地状态，不属于 stream，stream.reset() 清不到它——
    // 两边要一起复位，否则会同屏出现"空状态提示"与"覆盖警告"互相矛盾的界面。
    stream.reset();
    setOverwriteArmed(false);
    setPartialArmed(false);
    setUncertainRetryArmed(false);
    setInitialRunResolved(true);
    onRunStateChanged?.();
  };

  const reasonLabel = (reasonCode: string) => {
    const key = proseReasonTranslationKey(reasonCode);
    return key
      ? t(`reasons.${key}`)
      : t("reasons.unknown", { code: reasonCode });
  };

  const resumeUnavailableMessage = selectedInitialRun?.status === "stale"
    ? t("leftoverResumeStale")
    : selectedInitialRun?.continuation_exhausted
      ? t("leftoverResumeExhausted")
      : selectedInitialRun?.status === "superseded"
        ? t("leftoverResumeSuperseded")
        : t("leftoverResumeUnavailable");

  const handleDialogKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      onClose();
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = Array.from(
      dialogRef.current?.querySelectorAll<HTMLElement>(
        'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ) ?? [],
    ).filter((element) => (
      element.getAttribute("aria-hidden") !== "true"
      && element.getClientRects().length > 0
    ));
    if (focusable.length === 0) {
      event.preventDefault();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    const active = document.activeElement;
    if (event.shiftKey && (active === first || !dialogRef.current?.contains(active))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    }
  };

  return (
    <div className="absolute inset-0 z-30 flex items-center justify-center bg-black/25 px-4 py-6">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="prose-panel-title"
        onKeyDown={handleDialogKeyDown}
        className="flex max-h-full w-full max-w-5xl flex-col rounded-md border border-border bg-surface shadow-lg"
      >
        <header className="flex items-start justify-between gap-3 border-b border-border px-5 py-4">
          <div className="min-w-0">
            <h3
              id="prose-panel-title"
              className="text-base font-semibold text-foreground"
            >
              {t("title")}
            </h3>
            <p className="mt-1 text-xs leading-5 text-muted">{t("description")}</p>
          </div>
          <div className="flex shrink-0 gap-2">
            {running ? (
              <Button variant="outline" size="sm" onPress={stream.cancel}>
                {t("cancel")}
              </Button>
            ) : (
              <Button
                variant="primary"
                size="sm"
                className="bg-accent text-white hover:bg-accent-hover"
                onPress={startGeneration}
                isDisabled={
                  restoreLoading
                  || accepting
                  || selectedInitialRun?.can_resume === false
                }
              >
                {selectedInitialRun?.can_resume === false
                  ? t("resumeUnavailable")
                  : resumableDraft
                    ? t("resume")
                    : hasText
                      ? t("regenerate")
                      : t("generate")}
              </Button>
            )}
            <Button variant="ghost" size="sm" onPress={onClose}>
              {t("close")}
            </Button>
          </div>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          <div className="mb-4">
            <OutlineGenerationParams value={params} onChange={setParams} />
          </div>

          <ContextNotices report={stream.contextReport} />
          {stream.error && <Notice tone="error">{stream.error}</Notice>}
          {actionError && <Notice tone="error">{actionError}</Notice>}
          {incomplete && !selectedInitialRun && (
            <Notice tone="warning">{t("incomplete")}</Notice>
          )}
          {selectedInitialRun?.can_resume && (
            <Notice tone="warning">{t("leftoverResumeAvailable")}</Notice>
          )}
          {reasonCodes.length > 0 && (
            <Notice tone="warning">
              <span className="font-medium">{t("reasonTitle")}</span>
              <ul className="mt-1 list-disc space-y-1 ps-5">
                {reasonCodes.map((reasonCode) => (
                  <li key={reasonCode}>{reasonLabel(reasonCode)}</li>
                ))}
              </ul>
            </Notice>
          )}
          {selectedInitialRun?.can_resume === false && (
            <Notice tone="warning">{resumeUnavailableMessage}</Notice>
          )}
          {selectedInitialRun?.can_accept_partial === false && (
            <Notice tone="warning">{t("leftoverAcceptUnavailable")}</Notice>
          )}
          {partialArmed && <Notice tone="warning">{t("partialConfirm")}</Notice>}
          {uncertainRetryArmed && (
            <Notice tone="warning">{t("uncertainRetryConfirm")}</Notice>
          )}
          {overwriteArmed && <Notice tone="warning">{t("overwriteArm")}</Notice>}
          {stream.executionPlan?.mode === "scene_segments" && (
            <Notice tone="warning">
              {t("segmentedPlan", {
                scenes: stream.executionPlan.scene_count,
                calls: stream.executionPlan.call_count,
              })}
            </Notice>
          )}
          {stream.completion && (
            <div className="mb-3 grid gap-1 rounded-md border border-border bg-background px-3 py-2 text-xs text-muted sm:grid-cols-3">
              <span>
                {t("completionWords", {
                  actual: stream.completion.actual_word_count,
                  requested: stream.completion.requested_word_count,
                })}
              </span>
              <span>
                {t("completionScenes", {
                  completed: stream.completion.completed_scene_count,
                  total: stream.completion.scene_count,
                })}
              </span>
              <span>{t("completionFinish", { reason: stream.completion.finish_reason })}</span>
            </div>
          )}

          {!hasText && running && (
            <p className="py-10 text-center text-sm text-muted">{t("generating")}</p>
          )}
          {!hasText && !running && (
            <p className="py-10 text-center text-sm text-muted">{t("emptyPreview")}</p>
          )}

          {hasText && (
            <>
              <div className="mb-3 flex flex-wrap items-center gap-2">
                <span className="rounded-md border border-border bg-background px-2 py-1 text-xs tabular-nums text-muted">
                  {t("charCount", { count: countChapterWords(stream.text) })}
                </span>
                {/*
                  用量只在成功的 done 帧里到货；取消或失败时根本没有那一帧，
                  usage 保持 null、这里什么都不渲染。设计 §7.1 要求"如实报 0 或不报，
                  绝不编造估算值"——不渲染正是"不报"。
                */}
                {stream.usage && (
                  <span className="rounded-md border border-border bg-background px-2 py-1 text-xs tabular-nums text-muted">
                    {t("tokenUsage", {
                      total: stream.usage.total_tokens,
                      input: stream.usage.input_tokens,
                      output: stream.usage.output_tokens,
                    })}
                  </span>
                )}
              </div>
              {/*
                预览区刻意**只读**：流式写入与人工编辑并存必然打架。
                要改就先接受、改在编辑器里——那才是编辑正文的地方（设计 §3.2）。
              */}
              <div className="whitespace-pre-wrap rounded-md border border-border bg-background px-4 py-3 text-[15px] leading-8 text-foreground">
                {stream.text}
              </div>
            </>
          )}
        </div>

        <footer className="flex justify-end gap-2 border-t border-border px-5 py-3">
          <Button
            variant="ghost"
            size="sm"
            onPress={() => void discard()}
            isDisabled={
              (!hasText && !stream.runId)
              || (Boolean(stream.runId) && stream.runRevision == null)
              || running
              || accepting
              || selectedInitialRun?.can_discard === false
            }
          >
            {t("discard")}
          </Button>
          <Button
            variant="primary"
            size="sm"
            className="bg-accent text-white hover:bg-accent-hover"
            onPress={() => void accept()}
            isDisabled={
              !hasText
              || running
              || accepting
              || restoreLoading
              || selectedInitialRun?.can_accept_partial === false
            }
          >
            {accepting
              ? t("accepting")
              : partialAcceptance && !partialArmed
                ? t("acceptPartial")
                : overwriteArmed
                  ? t("overwriteConfirm")
                  : t("accept")}
          </Button>
        </footer>
      </div>
    </div>
  );
}
