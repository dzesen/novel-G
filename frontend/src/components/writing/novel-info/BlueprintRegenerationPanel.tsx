"use client";

import {
  useEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";
import { Button } from "@heroui/react";
import { useTranslations } from "next-intl";
import BlueprintRunControls from "@/components/shared/BlueprintRunControls";
import { blueprintStepOrder, blueprintGenerationParams, normalizeBlueprintExecution } from "@/lib/blueprintRunClient";
import {
  buildBlueprintRegenerationRequest,
  inspectBlueprintRegeneration,
} from "@/lib/blueprintGeneration";
import type {
  AICreateResponse,
  AICreateStepKey,
  WritingDraft,
  BlueprintGenerationSource,
  BlueprintExecutionRef,
} from "@/types/novel";

interface BlueprintRegenerationPanelProps {
  draft: WritingDraft;
  onAccept: (candidate: AICreateResponse, source: BlueprintGenerationSource) => void;
  onBound: (execution: BlueprintExecutionRef) => void;
}

type DialogStage = "confirm" | "running" | "review";
type StepStatus = "pending" | "running" | "done" | "error";

const FOCUSABLE_SELECTOR = [
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "a[href]",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

const STEP_KEYS: AICreateStepKey[] = [
  "expand_idea",
  "extract_idea",
  "core_seed",
  "novel_meta",
  "blueprint",
];

function isStepKey(value: unknown): value is AICreateStepKey {
  return (
    typeof value === "string" &&
    STEP_KEYS.includes(value as AICreateStepKey)
  );
}

export default function BlueprintRegenerationPanel({
  draft,
  onAccept,
  onBound,
}: BlueprintRegenerationPanelProps) {
  const t = useTranslations("writing.novelInfo.regeneration");
  const inspection = inspectBlueprintRegeneration(draft);
  const originSupportsRegeneration =
    draft._creationOrigin === "ai_idea" ||
    draft._creationOrigin === "tavern_cards";
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const [stage, setStage] = useState<DialogStage>("confirm");
  const [candidate, setCandidate] = useState<AICreateResponse | null>(null);
  const [candidateSource, setCandidateSource] = useState<BlueprintGenerationSource | null>(null);
  const [stepStatuses, setStepStatuses] = useState<
    Record<AICreateStepKey, StepStatus>
  >(() => ({
    expand_idea: "pending",
    extract_idea: "pending",
    core_seed: "pending",
    novel_meta: "pending",
    blueprint: "pending",
  }));

  useEffect(() => {
    if (!open) return;
    headingRef.current?.focus();
  }, [open, stage]);

  if (!originSupportsRegeneration) return null;

  const openDialog = () => {
    returnFocusRef.current = document.activeElement as HTMLElement | null;
    setStage("confirm");
    setCandidate(null);
    setCandidateSource(null);
    setStepStatuses({
      expand_idea: "pending",
      extract_idea: "pending",
      core_seed: "pending",
      novel_meta: "pending",
      blueprint: "pending",
    });
    setOpen(true);
  };

  const closeDialog = () => {
    if (stage === "running") return;
    setOpen(false);
    queueMicrotask(() => (returnFocusRef.current ?? triggerRef.current)?.focus());
  };

  const handleDialogKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape" && stage !== "running") {
      event.preventDefault();
      closeDialog();
      return;
    }
    if (event.key !== "Tab") return;
    const dialog = dialogRef.current;
    if (!dialog) return;
    const focusable = Array.from(
      dialog.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
    ).filter((element) => element.getClientRects().length > 0);
    if (focusable.length === 0) {
      event.preventDefault();
      dialog.focus();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    const active = document.activeElement;
    if (event.shiftKey && (active === first || !dialog.contains(active))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (active === last || !dialog.contains(active))) {
      event.preventDefault();
      first.focus();
    }
  };

  const acceptCandidate = () => {
    if (!candidate || !candidateSource) return;
    onAccept(candidate, candidateSource);
    setOpen(false);
    queueMicrotask(() => (returnFocusRef.current ?? triggerRef.current)?.focus());
  };

  return (
    <section className="rounded-xl border border-accent/30 bg-accent/[0.04] p-4 sm:p-5">
      <div className="flex min-w-0 flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <p className="text-xs font-semibold uppercase tracking-[0.14em] text-accent">
            {t("eyebrow")}
          </p>
          <h3 className="mt-1 text-base font-semibold text-foreground">
            {t("title")}
          </h3>
          <p className="mt-1 max-w-2xl text-sm leading-6 text-muted">
            {inspection.allowed
              ? t("description")
              : t(`blocked.${inspection.blocked_code}`)}
          </p>
        </div>
        <Button
          ref={triggerRef}
          variant="secondary"
          className="w-full shrink-0 sm:w-auto"
          isDisabled={!inspection.allowed}
          onPress={openDialog}
        >
          {t("open")}
        </Button>
      </div>

      {open && inspection.allowed && (
        <div
          ref={dialogRef}
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/45 px-3 py-4 sm:px-6"
          role="dialog"
          aria-modal="true"
          aria-labelledby="blueprint-regeneration-title"
          tabIndex={-1}
          onKeyDown={handleDialogKeyDown}
        >
          <div
            className="flex max-h-full w-full max-w-4xl flex-col overflow-hidden rounded-xl border border-border bg-background shadow-xl"
            aria-busy={stage === "running"}
          >
            <header className="flex items-start justify-between gap-3 border-b border-border px-4 py-4 sm:px-6">
              <div className="min-w-0">
                <p className="text-xs font-semibold uppercase tracking-[0.14em] text-accent">
                  {t(`stage.${stage}.eyebrow`)}
                </p>
                <h2
                  ref={headingRef}
                  id="blueprint-regeneration-title"
                  tabIndex={-1}
                  className="mt-1 text-lg font-semibold text-foreground outline-none"
                >
                  {t(`stage.${stage}.title`)}
                </h2>
              </div>
              <Button
                variant="ghost"
                size="sm"
                isDisabled={stage === "running"}
                onPress={closeDialog}
              >
                {t("close")}
              </Button>
            </header>

            <div className="min-h-0 flex-1 overflow-y-auto px-4 py-5 sm:px-6">
              <BlueprintRunControls
                entry="regenerate-blueprint"
                request={{
                  ...buildBlueprintRegenerationRequest(inspection.source),
                  card_imports: inspection.source.card_imports,
                  draft_id: normalizeBlueprintExecution(draft._blueprintRun)?.draft_id ?? inspection.source.execution?.draft_id,
                }}
                initialRunId={normalizeBlueprintExecution(draft._blueprintRun)?.run_id}
                onBound={(ref) => onBound(ref)}
                onBusyChange={(busy) => setStage(busy ? "running" : "confirm")}
                onRead={(run) => setStepStatuses({
                  expand_idea: run.completed_steps.includes("expand_idea") ? "done" : "pending",
                  extract_idea: run.completed_steps.includes("extract_idea") ? "done" : "pending",
                  core_seed: run.completed_steps.includes("core_seed") ? "done" : "pending",
                  novel_meta: run.completed_steps.includes("novel_meta") ? "done" : "pending",
                  blueprint: run.completed_steps.includes("blueprint") ? "done" : "pending",
                })}
                onEvent={(event, data) => {
                  if (event !== "step" || !isStepKey(data.step)) return;
                  const status = data.status;
                  if (status === "running" || status === "done" || status === "error" || status === "pending")
                    setStepStatuses((previous) => ({ ...previous, [data.step as AICreateStepKey]: status }));
                }}
                onComplete={(result, request, execution) => {
                  setCandidate(result);
                  setCandidateSource({
                    ...inspection.source,
                    strategy: request.strategy,
                    generation_params: blueprintGenerationParams(request), execution,
                  });
                  setStage("review");
                }}
              />

              {stage === "confirm" && (
                <div className="space-y-5">
                  <p className="text-sm leading-6 text-foreground">
                    {t("confirmDescription")}
                  </p>
                  <dl className="grid gap-3 rounded-xl border border-border bg-surface-secondary/30 p-4 sm:grid-cols-3">
                    <SourceFact
                      label={t("source.idea")}
                      value={inspection.source.user_idea}
                    />
                    <SourceFact
                      label={t("source.chapters")}
                      value={String(inspection.source.number_of_chapters)}
                    />
                    <SourceFact
                      label={t("source.words")}
                      value={String(inspection.source.words_per_chapter)}
                    />
                  </dl>
                  <div className="rounded-xl border border-warning/40 bg-warning/5 p-4 text-sm leading-6 text-foreground">
                    {t("replacementWarning")}
                  </div>

                </div>
              )}

              {stage === "running" && (
                <div role="status" aria-live="polite" className="space-y-5">
                  <p className="text-sm leading-6 text-muted">
                    {t("runningDescription")}
                  </p>
                  <ol className="grid gap-2">
                    {blueprintStepOrder(inspection.source.strategy).map((key, index) => (
                      <li
                        key={key}
                        className="flex items-center gap-3 rounded-lg border border-border px-3 py-3"
                      >
                        <span
                          className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-xs font-semibold ${
                            stepStatuses[key] === "done"
                              ? "bg-success/15 text-success"
                              : stepStatuses[key] === "running"
                                ? "bg-accent/15 text-accent"
                                : stepStatuses[key] === "error"
                                  ? "bg-danger/15 text-danger"
                                  : "bg-surface-secondary text-muted"
                          }`}
                        >
                          {stepStatuses[key] === "done" ? "✓" : index + 1}
                        </span>
                        <span className="min-w-0 text-sm font-medium text-foreground">
                          {t(`steps.${key}`)}
                        </span>
                      </li>
                    ))}
                  </ol>
                </div>
              )}

              {stage === "review" && candidate && (
                <div className="space-y-5">
                  <p className="text-sm leading-6 text-foreground">
                    {t("reviewDescription")}
                  </p>
                  <div className="grid gap-4 lg:grid-cols-2">
                    <BlueprintPreview
                      label={t("current")}
                      title={draft.title}
                      summary={draft.summary ?? ""}
                      plot={draft.plot ?? ""}
                    />
                    <BlueprintPreview
                      label={t("candidate")}
                      title={candidate.novel_meta.title}
                      summary={candidate.novel_meta.summary}
                      plot={
                        candidate.expand_idea?.plot ??
                        candidate.extract_idea.plot ??
                        ""
                      }
                      emphasized
                    />
                  </div>
                </div>
              )}
            </div>

            <footer className="flex flex-col-reverse gap-2 border-t border-border px-4 py-4 sm:flex-row sm:justify-end sm:px-6">
              {stage === "confirm" && (
                <>
                  <Button variant="ghost" onPress={closeDialog}>
                    {t("keepCurrent")}
                  </Button>

                </>
              )}
              {stage === "running" && (
                <p className="w-full text-center text-xs leading-5 text-muted sm:text-right">
                  {t("runningHint")}
                </p>
              )}
              {stage === "review" && (
                <>
                  <Button variant="ghost" onPress={closeDialog}>
                    {t("keepCurrent")}
                  </Button>
                  <Button
                    variant="primary"
                    className="bg-accent text-white hover:bg-accent-hover"
                    onPress={acceptCandidate}
                  >
                    {t("acceptCandidate")}
                  </Button>
                </>
              )}
            </footer>
          </div>
        </div>
      )}
    </section>
  );
}

function SourceFact({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs font-medium text-muted">{label}</dt>
      <dd className="mt-1 break-words text-sm text-foreground">{value}</dd>
    </div>
  );
}

function BlueprintPreview({
  label,
  title,
  summary,
  plot,
  emphasized = false,
}: {
  label: string;
  title: string;
  summary: string;
  plot: string;
  emphasized?: boolean;
}) {
  return (
    <article
      className={`min-w-0 rounded-xl border p-4 ${
        emphasized
          ? "border-accent/45 bg-accent/[0.04]"
          : "border-border bg-surface-secondary/20"
      }`}
    >
      <p className="text-xs font-semibold uppercase tracking-[0.12em] text-muted">
        {label}
      </p>
      <h3 className="mt-2 break-words text-base font-semibold text-foreground">
        {title}
      </h3>
      <p className="mt-3 whitespace-pre-wrap break-words text-sm leading-6 text-muted">
        {summary}
      </p>
      <p className="mt-3 max-h-48 overflow-y-auto whitespace-pre-wrap break-words border-t border-border pt-3 text-xs leading-5 text-muted">
        {plot}
      </p>
    </article>
  );
}
