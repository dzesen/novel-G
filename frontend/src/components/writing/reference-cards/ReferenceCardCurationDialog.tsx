"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Button } from "@heroui/react";
import { useTranslations } from "next-intl";
import { OptionalNumberParam } from "@/components/shared/OptionalParamControls";
import {
  buildReferenceCardCurationDiscardPath,
  buildReferenceCardCurationPrepareRequest,
  clearedReferenceCardCurationState,
} from "./curationRequest";
import { ApiError, apiGet, apiPost } from "@/lib/api";
import type {
  ReferenceCardCandidate,
  ReferenceCardCurationAction,
  ReferenceCardCurationProposal,
  ReferenceCardCurationResult,
} from "@/types/novel";

const GROUPS = ["characters", "locations", "items", "rules", "lores"] as const;
type CandidateGroup = (typeof GROUPS)[number];

interface DecisionDraft {
  action: ReferenceCardCurationAction;
  candidate: ReferenceCardCandidate;
  overwriteFields: string[];
}

interface ReferenceCardCurationDialogProps {
  novelId: string;
  isOpen: boolean;
  onClose: () => void;
  onApplied: () => void | Promise<void>;
}

function flattenCandidates(proposal: ReferenceCardCurationProposal): ReferenceCardCandidate[] {
  return GROUPS.flatMap((group) => proposal.candidates[group] ?? []);
}

function initializeDecisions(
  proposal: ReferenceCardCurationProposal,
): Record<string, DecisionDraft> {
  return Object.fromEntries(
    flattenCandidates(proposal).map((candidate) => [
      candidate.candidate_id,
      {
        action: candidate.recommended_action,
        candidate: {
          ...candidate,
          details: { ...candidate.details },
          tags: [...candidate.tags],
          character_profile: candidate.character_profile
            ? {
                ...candidate.character_profile,
                aliases: [...candidate.character_profile.aliases],
                dialogue_examples: [
                  ...candidate.character_profile.dialogue_examples,
                ],
                scene_opening_examples: [
                  ...candidate.character_profile.scene_opening_examples,
                ],
              }
            : undefined,
        },
        overwriteFields: [],
      },
    ]),
  );
}

function unknownValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === null || value === undefined) return "";
  return JSON.stringify(value);
}

function apiErrorCode(error: ApiError): string | null {
  if (!error.detail || typeof error.detail !== "object") return null;
  const code = (error.detail as { code?: unknown }).code;
  return typeof code === "string" ? code : null;
}

export default function ReferenceCardCurationDialog({
  novelId,
  isOpen,
  onClose,
  onApplied,
}: ReferenceCardCurationDialogProps) {
  const t = useTranslations("writing.referenceCards.curation");
  const [proposal, setProposal] = useState<ReferenceCardCurationProposal | null>(null);
  const [decisions, setDecisions] = useState<Record<string, DecisionDraft>>({});
  const [activeGroup, setActiveGroup] = useState<CandidateGroup>("characters");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [applying, setApplying] = useState(false);
  const [discarding, setDiscarding] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ReferenceCardCurationResult | null>(null);
  const [maxTokens, setMaxTokens] = useState<number | null>(null);

  const clearProposalState = useCallback(() => {
    const cleared = clearedReferenceCardCurationState();
    setProposal(cleared.proposal);
    setDecisions(cleared.decisions);
    setResult(cleared.result);
    setActiveGroup("characters");
    setEditingId(null);
  }, []);

  const adoptProposal = useCallback((next: ReferenceCardCurationProposal) => {
    setProposal(next);
    setDecisions(initializeDecisions(next));
    setResult(next.apply_result ?? null);
    const firstNonEmpty = GROUPS.find((group) => next.candidates[group]?.length);
    setActiveGroup(firstNonEmpty ?? "characters");
    setEditingId(null);
  }, []);

  useEffect(() => {
    if (!isOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (
        event.key === "Escape" &&
        !generating &&
        !applying &&
        !discarding
      ) {
        onClose();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [applying, discarding, generating, isOpen, onClose]);

  useEffect(() => {
    if (!isOpen) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    void apiGet<ReferenceCardCurationProposal>(
      `/api/reference-cards/novel/${novelId}/curation/proposal`,
    )
      .then((next) => {
        if (!cancelled) adoptProposal(next);
      })
      .catch((reason: unknown) => {
        if (cancelled) return;
        if (reason instanceof ApiError && reason.status === 404) {
          clearProposalState();
          return;
        }
        setError(reason instanceof Error ? reason.message : t("loadFailed"));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [adoptProposal, clearProposalState, isOpen, novelId, t]);

  const currentCandidates = proposal?.candidates[activeGroup] ?? [];
  const summary = useMemo(() => {
    const values = Object.values(decisions);
    return {
      create: values.filter((item) => item.action === "create").length,
      merge: values.filter((item) => item.action === "merge").length,
      restore: values.filter((item) => item.action === "restore_merge").length,
      skip: values.filter((item) => item.action === "skip").length,
    };
  }, [decisions]);

  const generate = async (forceRegenerate = false) => {
    if (forceRegenerate && !window.confirm(t("regenerateConfirm"))) return;
    setGenerating(true);
    setError(null);
    setResult(null);
    try {
      const next = await apiPost<ReferenceCardCurationProposal>(
        `/api/reference-cards/novel/${novelId}/curation/prepare`,
        buildReferenceCardCurationPrepareRequest(
          forceRegenerate,
          maxTokens,
        ),
      );
      adoptProposal(next);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t("generateFailed"));
    } finally {
      setGenerating(false);
    }
  };

  const discard = async () => {
    if (!proposal || !window.confirm(t("discardConfirm"))) return;
    setDiscarding(true);
    setError(null);
    try {
      await apiPost(
        buildReferenceCardCurationDiscardPath(novelId, proposal.proposal_id),
        {},
      );
      clearProposalState();
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 404) {
        clearProposalState();
        setError(t("discardMissing"));
      } else if (reason instanceof ApiError && reason.status === 409) {
        setError(t("discardConflict"));
      } else {
        setError(t("discardFailed"));
      }
    } finally {
      setDiscarding(false);
    }
  };

  const apply = async () => {
    if (!proposal) return;
    setApplying(true);
    setError(null);
    try {
      const applied = await apiPost<ReferenceCardCurationResult>(
        `/api/reference-cards/novel/${novelId}/curation/${proposal.proposal_id}/apply`,
        {
          acceptance_token: proposal.acceptance_token,
          decisions: flattenCandidates(proposal).map((candidate) => {
            const decision = decisions[candidate.candidate_id];
            return {
              candidate_id: candidate.candidate_id,
              action: decision.action,
              target_card_id:
                decision.action === "merge" || decision.action === "restore_merge"
                  ? candidate.recommended_target_card_id
                  : undefined,
              overrides: {
                name: decision.candidate.name,
                subtitle: decision.candidate.subtitle,
                description: decision.candidate.description,
                details: decision.candidate.details,
                tags: decision.candidate.tags,
                importance: decision.candidate.importance,
                character_profile: decision.candidate.character_profile,
              },
              overwrite_fields: decision.overwriteFields,
            };
          }),
        },
      );
      setResult(applied);
      setProposal((current) =>
        current ? { ...current, status: "applied", apply_result: applied } : current,
      );
      await onApplied();
    } catch (reason) {
      if (
        reason instanceof ApiError &&
        reason.status === 409 &&
        apiErrorCode(reason) === "stale_reference_card_proposal"
      ) {
        clearProposalState();
        setError(t("staleConflict"));
      } else {
        setError(reason instanceof Error ? reason.message : t("applyFailed"));
      }
    } finally {
      setApplying(false);
    }
  };

  const patchDecision = (
    candidateId: string,
    patch: Partial<DecisionDraft>,
  ) => {
    setDecisions((current) => ({
      ...current,
      [candidateId]: { ...current[candidateId], ...patch },
    }));
  };

  const patchCandidate = (
    candidateId: string,
    patch: Partial<ReferenceCardCandidate>,
  ) => {
    setDecisions((current) => ({
      ...current,
      [candidateId]: {
        ...current[candidateId],
        candidate: { ...current[candidateId].candidate, ...patch },
      },
    }));
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
        aria-labelledby="reference-card-curation-title"
        className="flex h-full w-full max-w-6xl flex-col overflow-hidden bg-background shadow-2xl sm:h-[min(880px,calc(100vh-2.5rem))] sm:rounded-2xl sm:border sm:border-border"
      >
        <header className="flex shrink-0 items-start justify-between gap-5 border-b border-border px-5 py-4 sm:px-7">
          <div>
            <p className="text-xs font-semibold uppercase tracking-[0.16em] text-accent">
              {t("eyebrow")}
            </p>
            <h2
              id="reference-card-curation-title"
              className="mt-1 text-xl font-semibold text-foreground"
            >
              {t("title")}
            </h2>
            <p className="mt-1 max-w-3xl text-sm leading-6 text-muted">
              {t("description")}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={generating || applying || discarding}
            aria-label={t("close")}
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-border text-xl text-muted transition-colors hover:bg-surface-secondary hover:text-foreground disabled:opacity-40"
          >
            ×
          </button>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto">
          {!loading && !generating && !result && (
            <div className="border-b border-border bg-surface px-5 py-4 sm:px-7">
              <div className="max-w-3xl space-y-2">
                <OptionalNumberParam
                  label={t("maxTokens")}
                  value={maxTokens}
                  onToggle={(enabled) => setMaxTokens(enabled ? 6000 : null)}
                  onValueChange={setMaxTokens}
                  min={1}
                  max={Number.MAX_SAFE_INTEGER}
                  step={1}
                />
                <p className="text-xs leading-5 text-muted">
                  {t("maxTokensHint")}
                </p>
              </div>
            </div>
          )}
          {loading ? (
            <CenteredState title={t("loading")} detail={t("loadingDetail")} />
          ) : generating ? (
            <CenteredState title={t("generating")} detail={t("generatingDetail")} />
          ) : result ? (
            <div className="mx-auto max-w-2xl px-6 py-14 text-center">
              <span className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-emerald-100 text-xl text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300">
                ✓
              </span>
              <h3 className="mt-5 text-xl font-semibold text-foreground">
                {t("appliedTitle")}
              </h3>
              <p className="mt-2 text-sm leading-6 text-muted">
                {t("appliedDetail", {
                  created: result.counts.created,
                  merged: result.counts.merged,
                  restored: result.counts.restored_merged,
                  skipped: result.counts.skipped,
                })}
              </p>
              <Button
                className="mt-7 bg-accent text-white hover:bg-accent-hover"
                variant="primary"
                onPress={onClose}
              >
                {t("done")}
              </Button>
            </div>
          ) : !proposal ? (
            <div className="mx-auto max-w-2xl px-6 py-14 text-center">
              <div className="mx-auto flex h-14 w-14 items-center justify-center rounded-2xl border border-accent/25 bg-accent/10 text-2xl text-accent">
                ✦
              </div>
              <h3 className="mt-5 text-xl font-semibold text-foreground">
                {t("emptyTitle")}
              </h3>
              <p className="mt-2 text-sm leading-6 text-muted">{t("emptyDetail")}</p>
              <Button
                className="mt-7 bg-accent text-white hover:bg-accent-hover"
                variant="primary"
                onPress={() => void generate(false)}
              >
                {t("generate")}
              </Button>
            </div>
          ) : (
            <>
              <div className="border-b border-border bg-surface px-5 py-4 sm:px-7">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div className="flex flex-wrap gap-2">
                    {GROUPS.map((group) => (
                      <button
                        key={group}
                        type="button"
                        onClick={() => setActiveGroup(group)}
                        className={`rounded-full px-3 py-1.5 text-sm transition-colors ${
                          activeGroup === group
                            ? "bg-foreground text-background"
                            : "bg-surface-secondary text-muted hover:text-foreground"
                        }`}
                      >
                        {t(`groups.${group}`)} · {proposal.candidates[group]?.length ?? 0}
                      </button>
                    ))}
                  </div>
                  <p className="text-xs text-muted">
                    {proposal.generation_audit.provider_alias
                      ? t("provider", {
                          provider: proposal.generation_audit.provider_alias,
                        })
                      : t("proposalReady")}
                  </p>
                </div>
              </div>

              <div className="mx-auto max-w-5xl space-y-4 px-5 py-5 sm:px-7">
                {currentCandidates.length ? (
                  currentCandidates.map((candidate) => {
                    const decision = decisions[candidate.candidate_id];
                    if (!decision) return null;
                    const canMerge = Boolean(candidate.recommended_target_card_id);
                    const isEditing = editingId === candidate.candidate_id;
                    return (
                      <article
                        key={candidate.candidate_id}
                        className="rounded-xl border border-border bg-surface p-4 sm:p-5"
                      >
                        <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
                          <div className="min-w-0">
                            <div className="flex flex-wrap items-center gap-2">
                              <h3 className="text-base font-semibold text-foreground">
                                {decision.candidate.name}
                              </h3>
                              <span className="rounded-full bg-accent/10 px-2 py-0.5 text-xs font-medium text-accent">
                                {decision.candidate.importance === "main"
                                  ? t("importanceMain")
                                  : t("importanceSub")}
                              </span>
                            </div>
                            <p className="mt-1 text-sm text-muted">
                              {decision.candidate.subtitle ||
                                decision.candidate.description ||
                                t("noDescription")}
                            </p>
                          </div>
                          <label className="shrink-0">
                            <span className="sr-only">{t("action")}</span>
                            <select
                              value={decision.action}
                              onChange={(event) =>
                                patchDecision(candidate.candidate_id, {
                                  action: event.target.value as ReferenceCardCurationAction,
                                })
                              }
                              className="rounded-lg border border-border bg-background px-3 py-2 text-sm font-medium text-foreground outline-none focus:border-accent focus:ring-2 focus:ring-accent/15"
                            >
                              <option value="create">{t("actions.create")}</option>
                              {canMerge && (
                                <option value="merge">{t("actions.merge")}</option>
                              )}
                              {canMerge && (
                                <option value="restore_merge">
                                  {t("actions.restore_merge")}
                                </option>
                              )}
                              <option value="skip">{t("actions.skip")}</option>
                            </select>
                          </label>
                        </div>

                        {candidate.warnings.map((warning) => (
                          <p
                            key={`${candidate.candidate_id}-${warning.code}`}
                            className="mt-3 rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200"
                          >
                            {warning.message}
                          </p>
                        ))}

                        {(decision.action === "merge" ||
                          decision.action === "restore_merge") &&
                          candidate.recommended_target && (
                            <div className="mt-4 rounded-lg border border-border bg-background p-3">
                              <p className="text-xs font-semibold uppercase tracking-[0.12em] text-muted">
                                {t("mergeTarget")}
                              </p>
                              <p className="mt-1 text-sm font-medium text-foreground">
                                {candidate.recommended_target.name}
                              </p>
                              {candidate.field_conflicts.length ? (
                                <div className="mt-3 space-y-2">
                                  {candidate.field_conflicts.map((conflict) => {
                                    const checked = decision.overwriteFields.includes(
                                      conflict.field,
                                    );
                                    return (
                                      <label
                                        key={conflict.field}
                                        className="flex cursor-pointer gap-3 rounded-md bg-surface-secondary p-3"
                                      >
                                        <input
                                          type="checkbox"
                                          checked={checked}
                                          onChange={(event) => {
                                            const next = event.target.checked
                                              ? [
                                                  ...decision.overwriteFields,
                                                  conflict.field,
                                                ]
                                              : decision.overwriteFields.filter(
                                                  (field) => field !== conflict.field,
                                                );
                                            patchDecision(candidate.candidate_id, {
                                              overwriteFields: next,
                                            });
                                          }}
                                          className="mt-0.5 h-4 w-4 accent-[var(--color-accent)]"
                                        />
                                        <span className="min-w-0 text-xs leading-5">
                                          <span className="font-medium text-foreground">
                                            {conflict.field}
                                          </span>
                                          <span className="block text-muted">
                                            {t("keepExisting")}: {unknownValue(conflict.existing)}
                                          </span>
                                          <span className="block text-muted">
                                            {t("useCandidate")}: {unknownValue(conflict.candidate)}
                                          </span>
                                        </span>
                                      </label>
                                    );
                                  })}
                                </div>
                              ) : (
                                <p className="mt-2 text-xs text-muted">
                                  {t("fillEmptyOnly")}
                                </p>
                              )}
                            </div>
                          )}

                        <button
                          type="button"
                          onClick={() =>
                            setEditingId(isEditing ? null : candidate.candidate_id)
                          }
                          className="mt-4 text-sm font-medium text-accent hover:underline"
                        >
                          {isEditing ? t("finishEditing") : t("editCandidate")}
                        </button>

                        {isEditing && (
                          <CandidateEditor
                            candidate={decision.candidate}
                            onChange={(patch) =>
                              patchCandidate(candidate.candidate_id, patch)
                            }
                            labels={{
                              name: t("fields.name"),
                              subtitle: t("fields.subtitle"),
                              description: t("fields.description"),
                              importance: t("fields.importance"),
                              tags: t("fields.tags"),
                              details: t("fields.details"),
                              characterProfile: t("fields.characterProfile"),
                              aliases: t("fields.aliases"),
                              portrayalContext: t("fields.portrayalContext"),
                              portrayalNotes: t("fields.portrayalNotes"),
                              dialogueExamples: t("fields.dialogueExamples"),
                              sceneOpeningExamples: t("fields.sceneOpeningExamples"),
                              addExample: t("fields.addExample"),
                              removeExample: t("fields.removeExample"),
                              main: t("importanceMain"),
                              sub: t("importanceSub"),
                            }}
                          />
                        )}
                      </article>
                    );
                  })
                ) : (
                  <p className="py-10 text-center text-sm text-muted">
                    {t("groupEmpty")}
                  </p>
                )}
              </div>
            </>
          )}
        </div>

        {error && (
          <div
            role="alert"
            className="mx-5 mb-3 rounded-lg border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-800 dark:border-red-900 dark:bg-red-950 dark:text-red-200 sm:mx-7"
          >
            {error}
          </div>
        )}

        {proposal && !result && !loading && !generating && (
          <footer className="flex shrink-0 flex-col gap-3 border-t border-border bg-surface px-5 py-4 sm:flex-row sm:items-center sm:justify-between sm:px-7">
            <p className="text-xs leading-5 text-muted">
              {t("summary", {
                create: summary.create,
                merge: summary.merge,
                restore: summary.restore,
                skip: summary.skip,
              })}
            </p>
            <div className="flex flex-wrap justify-end gap-2">
              <Button
                className="text-red-700 dark:text-red-300"
                variant="ghost"
                isDisabled={generating || applying || discarding}
                onPress={() => void discard()}
              >
                {discarding ? t("discarding") : t("discard")}
              </Button>
              <Button
                variant="ghost"
                isDisabled={generating || applying || discarding}
                onPress={() => void generate(true)}
              >
                {t("regenerate")}
              </Button>
              <Button
                className="bg-accent text-white hover:bg-accent-hover"
                variant="primary"
                isDisabled={generating || applying || discarding}
                onPress={() => void apply()}
              >
                {applying ? t("applying") : t("apply")}
              </Button>
            </div>
          </footer>
        )}
      </section>
    </div>
  );
}

function CenteredState({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="flex min-h-80 items-center justify-center px-6 text-center">
      <div>
        <span className="mx-auto block h-8 w-8 animate-spin rounded-full border-2 border-border border-t-accent" />
        <h3 className="mt-5 font-semibold text-foreground">{title}</h3>
        <p className="mt-2 text-sm leading-6 text-muted">{detail}</p>
      </div>
    </div>
  );
}

function CandidateEditor({
  candidate,
  onChange,
  labels,
}: {
  candidate: ReferenceCardCandidate;
  onChange: (patch: Partial<ReferenceCardCandidate>) => void;
  labels: Record<
    | "name"
    | "subtitle"
    | "description"
    | "importance"
    | "tags"
    | "details"
    | "characterProfile"
    | "aliases"
    | "portrayalContext"
    | "portrayalNotes"
    | "dialogueExamples"
    | "sceneOpeningExamples"
    | "addExample"
    | "removeExample"
    | "main"
    | "sub",
    string
  >;
}) {
  return (
    <div className="mt-4 grid gap-4 border-t border-border pt-4 sm:grid-cols-2">
      <DialogField
        label={labels.name}
        value={candidate.name}
        onChange={(name) => onChange({ name })}
      />
      <DialogField
        label={labels.subtitle}
        value={candidate.subtitle}
        onChange={(subtitle) => onChange({ subtitle })}
      />
      <label className="block">
        <span className="mb-1.5 block text-xs font-medium text-foreground">
          {labels.importance}
        </span>
        <select
          value={candidate.importance}
          onChange={(event) =>
            onChange({ importance: event.target.value as "main" | "sub" })
          }
          className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm text-foreground outline-none focus:border-accent focus:ring-2 focus:ring-accent/15"
        >
          <option value="main">{labels.main}</option>
          <option value="sub">{labels.sub}</option>
        </select>
      </label>
      <DialogField
        label={labels.tags}
        value={candidate.tags.join(", ")}
        onChange={(value) =>
          onChange({
            tags: value
              .split(/[,，、;\n]/)
              .map((item) => item.trim())
              .filter(Boolean),
          })
        }
      />
      <label className="block sm:col-span-2">
        <span className="mb-1.5 block text-xs font-medium text-foreground">
          {labels.description}
        </span>
        <textarea
          value={candidate.description}
          onChange={(event) => onChange({ description: event.target.value })}
          rows={3}
          className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm leading-6 text-foreground outline-none focus:border-accent focus:ring-2 focus:ring-accent/15"
        />
      </label>
      <div className="sm:col-span-2">
        <p className="mb-2 text-xs font-medium text-foreground">{labels.details}</p>
        <div className="grid gap-3 sm:grid-cols-2">
          {Object.entries(candidate.details).map(([key, value]) => (
            <DialogField
              key={key}
              label={key}
              value={value}
              onChange={(next) =>
                onChange({ details: { ...candidate.details, [key]: next } })
              }
            />
          ))}
        </div>
      </div>
      {candidate.character_profile && (
        <div className="border-t border-border pt-4 sm:col-span-2">
          <p className="mb-3 text-xs font-semibold uppercase tracking-[0.12em] text-muted">
            {labels.characterProfile}
          </p>
          <div className="grid gap-3 sm:grid-cols-2">
            <DialogField
              label={labels.aliases}
              value={candidate.character_profile.aliases.join(", ")}
              onChange={(value) =>
                onChange({
                  character_profile: {
                    ...candidate.character_profile!,
                    aliases: value
                      .split(/[,，、;\n]/)
                      .map((item) => item.trim())
                      .filter(Boolean),
                  },
                })
              }
            />
            <DialogTextArea
              label={labels.portrayalNotes}
              value={candidate.character_profile.portrayal_notes}
              onChange={(portrayal_notes) =>
                onChange({
                  character_profile: {
                    ...candidate.character_profile!,
                    portrayal_notes,
                  },
                })
              }
            />
            <DialogTextArea
              className="sm:col-span-2"
              label={labels.portrayalContext}
              value={candidate.character_profile.portrayal_context}
              onChange={(portrayal_context) =>
                onChange({
                  character_profile: {
                    ...candidate.character_profile!,
                    portrayal_context,
                  },
                })
              }
            />
            <CandidateExampleEditor
              className="sm:col-span-2"
              label={labels.dialogueExamples}
              values={candidate.character_profile.dialogue_examples}
              maxItems={12}
              addLabel={labels.addExample}
              removeLabel={labels.removeExample}
              onChange={(dialogue_examples) =>
                onChange({
                  character_profile: {
                    ...candidate.character_profile!,
                    dialogue_examples,
                  },
                })
              }
            />
            <CandidateExampleEditor
              className="sm:col-span-2"
              label={labels.sceneOpeningExamples}
              values={candidate.character_profile.scene_opening_examples}
              maxItems={8}
              addLabel={labels.addExample}
              removeLabel={labels.removeExample}
              onChange={(scene_opening_examples) =>
                onChange({
                  character_profile: {
                    ...candidate.character_profile!,
                    scene_opening_examples,
                  },
                })
              }
            />
          </div>
        </div>
      )}
    </div>
  );
}

function DialogTextArea({
  label,
  value,
  onChange,
  className = "",
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  className?: string;
}) {
  return (
    <label className={`block ${className}`}>
      <span className="mb-1.5 block text-xs font-medium text-foreground">{label}</span>
      <textarea
        value={value}
        onChange={(event) => onChange(event.target.value)}
        rows={3}
        className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm leading-6 text-foreground outline-none focus:border-accent focus:ring-2 focus:ring-accent/15"
      />
    </label>
  );
}

function CandidateExampleEditor({
  label,
  values,
  maxItems,
  addLabel,
  removeLabel,
  onChange,
  className = "",
}: {
  label: string;
  values: string[];
  maxItems: number;
  addLabel: string;
  removeLabel: string;
  onChange: (values: string[]) => void;
  className?: string;
}) {
  return (
    <fieldset className={className}>
      <legend className="text-xs font-medium text-foreground">{label}</legend>
      <div className="mt-2 space-y-2">
        {values.map((value, index) => (
          <div key={index} className="flex items-start gap-2">
            <textarea
              value={value}
              onChange={(event) =>
                onChange(
                  values.map((item, itemIndex) =>
                    itemIndex === index ? event.target.value : item,
                  ),
                )
              }
              rows={2}
              className="min-w-0 flex-1 rounded-lg border border-border bg-background px-3 py-2 text-sm leading-6 text-foreground outline-none focus:border-accent focus:ring-2 focus:ring-accent/15"
            />
            <button
              type="button"
              aria-label={removeLabel}
              onClick={() =>
                onChange(values.filter((_, itemIndex) => itemIndex !== index))
              }
              className="px-2 py-1 text-lg leading-none text-red-600 hover:text-red-700"
            >
              ×
            </button>
          </div>
        ))}
      </div>
      <button
        type="button"
        disabled={values.length >= maxItems}
        onClick={() => onChange([...values, ""])}
        className="mt-2 text-xs font-semibold text-accent hover:underline disabled:cursor-not-allowed disabled:opacity-40"
      >
        + {addLabel}
      </button>
    </fieldset>
  );
}

function DialogField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="block">
      <span className="mb-1.5 block text-xs font-medium text-foreground">{label}</span>
      <input
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm text-foreground outline-none focus:border-accent focus:ring-2 focus:ring-accent/15"
      />
    </label>
  );
}
