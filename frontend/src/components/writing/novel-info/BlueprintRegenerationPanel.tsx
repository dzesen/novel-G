"use client";

import { useState } from "react";
import { Button } from "@heroui/react";
import { useTranslations } from "next-intl";
import { apiPostSSE } from "@/lib/api";
import {
  buildBlueprintRegenerationRequest,
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
  const inspection = inspectBlueprintRegeneration(draft);
  const originSupportsRegeneration =
    draft._creationOrigin === "ai_idea" ||
    draft._creationOrigin === "tavern_cards";
  const [open, setOpen] = useState(false);
  const [stage, setStage] = useState<DialogStage>("confirm");
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

  if (!originSupportsRegeneration) return null;

  const openDialog = () => {
    setStage("confirm");
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
  };

  const startRegeneration = async () => {
    const currentInspection = inspectBlueprintRegeneration(draft);
    if (!currentInspection.allowed) {
      setError(t(`blocked.${currentInspection.blocked_code}`));
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
    let receivedTerminalEvent = false;

    try {
      await apiPostSSE(
        "/api/llm/create-novel-by-ai",
        buildBlueprintRegenerationRequest(currentInspection.source),
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
          receivedTerminalEvent = true;
          if (data.success && data.result) {
            setCandidate(data.result as AICreateResponse);
            setStage("review");
            return;
          }
          setStage("confirm");
          setError(
            typeof data.error === "string" && data.error.trim()
              ? data.error
              : t("failed"),
          );
        },
      );
      if (!receivedTerminalEvent) {
        setStage("confirm");
        setError(t("connectionEnded"));
      }
    } catch (cause) {
      setStage("confirm");
      setError(cause instanceof Error ? cause.message : t("failed"));
    }
  };

  const acceptCandidate = () => {
    if (!candidate) return;
    onAccept(candidate);
    setOpen(false);
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
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/45 px-3 py-4 sm:px-6"
          role="dialog"
          aria-modal="true"
          aria-labelledby="blueprint-regeneration-title"
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
                  id="blueprint-regeneration-title"
                  className="mt-1 text-lg font-semibold text-foreground"
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
                  <Button
                    variant="primary"
                    className="bg-accent text-white hover:bg-accent-hover"
                    onPress={() => void startRegeneration()}
                  >
                    {t("confirmPaidCall")}
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
