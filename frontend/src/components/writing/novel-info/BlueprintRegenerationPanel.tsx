"use client";

import {
  useEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";
import { Button } from "@heroui/react";
import { useTranslations } from "next-intl";
import { apiPost, apiPostSSE, SSEError } from "@/lib/api";
import { blueprintGenerationStream } from "@/lib/generationStreamContracts";
import {
  buildBlueprintRegenerationReadinessRequest,
  buildBlueprintRegenerationStartRequest,
  inspectBlueprintRegeneration,
} from "@/lib/blueprintGeneration";
import type {
  AICreateResponse,
  AICreateStepKey,
  WritingDraft,
} from "@/types/novel";

interface BlueprintRegenerationPanelProps {
  draft: WritingDraft;
  onAccept: (candidate: AICreateResponse) => void;
}

type DialogStage = "confirm" | "running" | "review";
type StepStatus = "pending" | "running" | "done" | "error";

interface BlueprintRegenerationReadiness {
  version: 2;
  status: "ready" | "warning_requires_ack" | "blocked";
  digest: string;
  token_budget: number | null;
  uses_system_token_budget: boolean;
  maximum_provider_attempts: number;
  maximum_tokens_total: number;
  token_bound_known: boolean;
  budget_covers_conservative_maximum: boolean;
  providers: Array<{
    step: AICreateStepKey;
    provider_alias: string;
    provider_model: string;
    maximum_attempts: number;
  }>;
  issues: Array<{ code: string; level: string }>;
}

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
}: BlueprintRegenerationPanelProps) {
  const t = useTranslations("writing.novelInfo.regeneration");
  const tStream = useTranslations("streamErrors");
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
  const [tokenBudget, setTokenBudget] = useState("");
  const [readiness, setReadiness] =
    useState<BlueprintRegenerationReadiness | null>(null);
  const [readinessLoading, setReadinessLoading] = useState(false);
  const [automaticBudgetConfirmed, setAutomaticBudgetConfirmed] =
    useState(false);
  const [candidate, setCandidate] = useState<AICreateResponse | null>(null);
  const [error, setError] = useState("");
  const [stepStatuses, setStepStatuses] = useState<
    Record<AICreateStepKey, StepStatus>
  >(() => ({
    expand_idea: "pending",
    extract_idea: "pending",
    core_seed: "pending",
    novel_meta: "pending",
  }));

  const parsedTokenBudget = tokenBudget === ""
    ? null
    : /^\d+$/.test(tokenBudget)
      ? Number(tokenBudget)
      : Number.NaN;
  const validTokenBudget = parsedTokenBudget === null || (
    Number.isSafeInteger(parsedTokenBudget) && parsedTokenBudget > 0
  );

  useEffect(() => {
    if (!open) return;
    headingRef.current?.focus();
  }, [open, stage]);

  if (!originSupportsRegeneration) return null;

  const openDialog = () => {
    returnFocusRef.current = document.activeElement as HTMLElement | null;
    setStage("confirm");
    setTokenBudget("");
    setReadiness(null);
    setReadinessLoading(false);
    setAutomaticBudgetConfirmed(false);
    setCandidate(null);
    setError("");
    setStepStatuses({
      expand_idea: "pending",
      extract_idea: "pending",
      core_seed: "pending",
      novel_meta: "pending",
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

  const inspectReadiness = async () => {
    const currentInspection = inspectBlueprintRegeneration(draft);
    if (!currentInspection.allowed || !validTokenBudget) return;
    setReadinessLoading(true);
    setReadiness(null);
    setError("");
    try {
      const report = await apiPost<BlueprintRegenerationReadiness>(
        "/api/llm/regenerate-blueprint/readiness",
        buildBlueprintRegenerationReadinessRequest(
          currentInspection.source,
          parsedTokenBudget,
        ),
      );
      setReadiness(report);
      setAutomaticBudgetConfirmed(false);
      if (report.status === "blocked") {
        setError(t("readinessBlocked"));
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : t("readinessFailed"));
    } finally {
      setReadinessLoading(false);
    }
  };

  const startRegeneration = async () => {
    const currentInspection = inspectBlueprintRegeneration(draft);
    if (!currentInspection.allowed) {
      setError(t(`blocked.${currentInspection.blocked_code}`));
      return;
    }
    if (!readiness || readiness.status === "blocked" || !validTokenBudget) {
      setError(t("readinessRequired"));
      return;
    }
    if (readiness.uses_system_token_budget && !automaticBudgetConfirmed) {
      setError(t("automaticBudgetConfirmationRequired"));
      return;
    }

    setError("");
    setCandidate(null);
    setStage("running");
    setStepStatuses({
      expand_idea: "pending",
      extract_idea: "pending",
      core_seed: "pending",
      novel_meta: "pending",
    });

    try {
      await apiPostSSE(
        "/api/llm/regenerate-blueprint",
        buildBlueprintRegenerationStartRequest(
          currentInspection.source,
          parsedTokenBudget,
          readiness.digest,
          automaticBudgetConfirmed,
        ),
        (event, data) => {
          if (event === "step" && isStepKey(data.step)) {
            const status = data.status;
            if (
              status === "pending" ||
              status === "running" ||
              status === "done" ||
              status === "error"
            ) {
              setStepStatuses((current) => ({
                ...current,
                [data.step as AICreateStepKey]: status,
              }));
            }
            return;
          }
          if (event !== "done") return;
          if (data.success && data.result) {
            setCandidate(data.result as AICreateResponse);
            setStage("review");
            return;
          }
          setStage("confirm");
          setReadiness(null);
          setError(
            typeof data.error === "string" && data.error.trim()
              ? data.error
              : t("failed"),
          );
        },
        blueprintGenerationStream,
      );
    } catch (cause) {
      setStage("confirm");
      setReadiness(null);
      setError(cause instanceof SSEError ? tStream(cause.code)
        : cause instanceof Error ? cause.message : t("failed"));
    }
  };

  const acceptCandidate = () => {
    if (!candidate) return;
    onAccept(candidate);
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
                  <section className="rounded-xl border border-accent/35 bg-accent/[0.04] p-4">
                    <h3 className="text-sm font-semibold text-foreground">
                      {t("authorizationTitle")}
                    </h3>
                    <p className="mt-1 text-xs leading-5 text-muted">
                      {t("authorizationDescription")}
                    </p>
                    <label
                      htmlFor="blueprint-regeneration-token-budget"
                      className="mt-4 block text-xs font-medium text-foreground"
                    >
                      {t("tokenBudgetLabel")}
                    </label>
                    <input
                      id="blueprint-regeneration-token-budget"
                      type="number"
                      min={1}
                      value={tokenBudget}
                      onChange={(event) => {
                        setTokenBudget(event.target.value);
                        setReadiness(null);
                        setAutomaticBudgetConfirmed(false);
                        setError("");
                      }}
                      aria-invalid={tokenBudget !== "" && !validTokenBudget}
                      aria-describedby="blueprint-regeneration-token-budget-hint"
                      className="mt-1 min-h-10 w-full rounded-md border border-border bg-background px-3 py-2 text-base text-foreground outline-none focus:border-accent sm:text-sm"
                    />
                    <p
                      id="blueprint-regeneration-token-budget-hint"
                      className="mt-1 text-xs leading-5 text-muted"
                    >
                      {t("tokenBudgetHint")}
                    </p>
                    {tokenBudget !== "" && !validTokenBudget && (
                      <p role="note" className="mt-2 text-xs text-danger">
                        {t("tokenBudgetInvalid")}
                      </p>
                    )}
                    {readiness && (
                      <dl className="mt-4 grid gap-2 border-t border-border pt-4 text-xs sm:grid-cols-3">
                        <div>
                          <dt className="text-muted">{t("maximumCalls")}</dt>
                          <dd className="mt-1 font-semibold tabular-nums text-foreground">
                            {readiness.maximum_provider_attempts}
                          </dd>
                        </div>
                        <div>
                          <dt className="text-muted">{t("providerCount")}</dt>
                          <dd className="mt-1 font-semibold tabular-nums text-foreground">
                            {new Set(readiness.providers.map((item) => item.provider_alias)).size}
                          </dd>
                        </div>
                        <div>
                          <dt className="text-muted">{t("conservativeMaximum")}</dt>
                          <dd className="mt-1 font-semibold tabular-nums text-foreground">
                            {readiness.maximum_tokens_total.toLocaleString()}
                          </dd>
                        </div>
                      </dl>
                    )}
                    {readiness && !readiness.budget_covers_conservative_maximum && (
                      <p role="note" className="mt-3 text-xs leading-5 text-amber-800 dark:text-amber-200">
                        {t("budgetMayStop")}
                      </p>
                    )}
                    {readiness?.uses_system_token_budget && readiness.token_budget != null && (
                      <div className="mt-3 rounded-lg border border-warning/40 bg-warning/5 p-3">
                        <p className="text-xs leading-5 text-foreground">
                          {t("automaticBudgetNotice", {
                            budget: readiness.token_budget.toLocaleString(),
                          })}
                        </p>
                        <label className="mt-2 flex min-w-0 items-start gap-2 text-xs leading-5 text-foreground">
                          <input
                            type="checkbox"
                            checked={automaticBudgetConfirmed}
                            onChange={(event) => {
                              setAutomaticBudgetConfirmed(event.target.checked);
                              setError("");
                            }}
                            className="mt-1 h-4 w-4 shrink-0 accent-[var(--color-accent)]"
                          />
                          <span>{t("automaticBudgetConfirm")}</span>
                        </label>
                      </div>
                    )}
                  </section>
                  {error && (
                    <p
                      role="alert"
                      className="rounded-xl border border-danger/30 bg-danger/5 px-4 py-3 text-sm text-danger"
                    >
                      {error}
                    </p>
                  )}
                </div>
              )}

              {stage === "running" && (
                <div role="status" aria-live="polite" className="space-y-5">
                  <p className="text-sm leading-6 text-muted">
                    {t("runningDescription")}
                  </p>
                  <ol className="grid gap-2">
                    {STEP_KEYS.map((key, index) => (
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
                  {readiness && readiness.status !== "blocked" ? (
                    <Button
                      variant="primary"
                      className="bg-accent text-white hover:bg-accent-hover"
                      isDisabled={
                        readiness.uses_system_token_budget
                        && !automaticBudgetConfirmed
                      }
                      onPress={() => void startRegeneration()}
                    >
                      {t("confirmPaidCall")}
                    </Button>
                  ) : (
                    <Button
                      variant="primary"
                      className="bg-accent text-white hover:bg-accent-hover"
                      isDisabled={!validTokenBudget || readinessLoading}
                      onPress={() => void inspectReadiness()}
                    >
                      {readinessLoading ? t("checkingReadiness") : t("checkReadiness")}
                    </Button>
                  )}
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
