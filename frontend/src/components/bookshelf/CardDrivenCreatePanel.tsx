"use client";

import { useMemo, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { Button, Card } from "@heroui/react";
import AICreateStepper from "./AICreateStepper";
import { apiPostForm } from "@/lib/api";
import { clearAICreateCache } from "@/lib/aiCreateCache";
import { cardImportErrorMessage } from "@/lib/cardImportErrors";
import { saveWritingDraft } from "@/lib/writingDraft";
import type {
  AICreateResponse,
  CardImportCandidate,
  CardImportCreationSelection,
  CardImportDecision,
  CardImportDirectionReference,
  CardImportProposal,
  WritingDraft,
} from "@/types/novel";
import type { CreativeDirectionSelection } from "@/types/agent";

interface CardDrivenCreatePanelProps {
  onCancel: () => void;
}

const PAGE_SIZE = 30;

function decisionKey(proposalId: string, candidateId: string): string {
  return `${proposalId}:${candidateId}`;
}

function recommendedDecision(
  candidate: CardImportCandidate,
): CardImportDecision {
  const conflict = candidate.conflicts[0];
  if (
    (candidate.recommended_action === "merge" ||
      candidate.recommended_action === "restore_merge") &&
    conflict
  ) {
    return {
      candidate_id: candidate.candidate_id,
      action: candidate.recommended_action,
      target_card_id: conflict.target_card_id,
      overwrite_fields: Object.keys(conflict.field_diffs),
    };
  }
  return {
    candidate_id: candidate.candidate_id,
    action:
      candidate.recommended_action === "skip" ? "skip" : "create",
  };
}

function parseDecisionValue(
  candidate: CardImportCandidate,
  value: string,
): CardImportDecision {
  const [action, targetCardId] = value.split(":", 2);
  if (action === "merge" || action === "restore_merge") {
    const conflict = candidate.conflicts.find(
      (item) => item.target_card_id === targetCardId,
    );
    return {
      candidate_id: candidate.candidate_id,
      action,
      target_card_id: targetCardId,
      overwrite_fields: Object.keys(conflict?.field_diffs ?? {}),
    };
  }
  return {
    candidate_id: candidate.candidate_id,
    action: action === "skip" ? "skip" : "create",
  };
}

function decisionValue(decision: CardImportDecision): string {
  return decision.target_card_id
    ? `${decision.action}:${decision.target_card_id}`
    : decision.action;
}

function newCreationId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `cards_${Date.now()}_${Math.random().toString(36).slice(2)}`;
}

export default function CardDrivenCreatePanel({
  onCancel,
}: CardDrivenCreatePanelProps) {
  const t = useTranslations("create.cardDriven");
  const tc = useTranslations("create");
  const tb = useTranslations("bookshelf");
  const tm = useTranslations("interopErrors.missingCharacterMetadata");
  const router = useRouter();
  const pathname = usePathname();
  const locale = pathname.startsWith("/en") ? "en" : "zh";
  const [characterFiles, setCharacterFiles] = useState<File[]>([]);
  const [worldFile, setWorldFile] = useState<File | null>(null);
  const [proposals, setProposals] = useState<CardImportProposal[]>([]);
  const [decisions, setDecisions] = useState<
    Record<string, CardImportDecision>
  >({});
  const [stage, setStage] = useState<"upload" | "review" | "direction">(
    "upload",
  );
  const [uploading, setUploading] = useState(false);
  const [redirecting, setRedirecting] = useState(false);
  const [error, setError] = useState("");
  const [page, setPage] = useState(0);

  const allCandidates = useMemo(
    () =>
      proposals.flatMap((proposal) =>
        proposal.proposed_cards.map((candidate) => ({
          proposal,
          candidate,
        })),
      ),
    [proposals],
  );
  const pageCount = Math.max(1, Math.ceil(allCandidates.length / PAGE_SIZE));
  const visibleCandidates = allCandidates.slice(
    page * PAGE_SIZE,
    (page + 1) * PAGE_SIZE,
  );

  const directionReferences: CardImportDirectionReference[] = proposals.map(
    (proposal) => ({
      proposal_id: proposal.proposal_id,
      digest: proposal.digest,
    }),
  );

  const creationSelections: CardImportCreationSelection[] = proposals.map(
    (proposal) => ({
      proposal_id: proposal.proposal_id,
      digest: proposal.digest,
      decisions: proposal.proposed_cards.map(
        (candidate) =>
          decisions[
            decisionKey(proposal.proposal_id, candidate.candidate_id)
          ] ?? recommendedDecision(candidate),
      ),
    }),
  );

  const stageFile = async (file: File): Promise<CardImportProposal> => {
    const form = new FormData();
    form.append("file", file);
    return apiPostForm<CardImportProposal>("/api/card-imports/preview", form);
  };

  const handlePreview = async () => {
    if (characterFiles.length === 0) {
      setError(t("characterRequired"));
      return;
    }
    if (characterFiles.length + (worldFile ? 1 : 0) > 32) {
      setError(t("tooManyFiles"));
      return;
    }
    setUploading(true);
    setError("");
    const staged: CardImportProposal[] = [];
    try {
      for (const file of characterFiles) {
        const proposal = await stageFile(file);
        if (
          !proposal.proposed_cards.some(
            (candidate) => candidate.target_type === "character",
          )
        ) {
          throw new Error(t("notCharacterCard", { name: file.name }));
        }
        staged.push(proposal);
      }
      if (worldFile) {
        const proposal = await stageFile(worldFile);
        if (proposal.source_format !== "worldbook_standalone") {
          throw new Error(t("notWorldBook", { name: worldFile.name }));
        }
        staged.push(proposal);
      }
      const nextDecisions: Record<string, CardImportDecision> = {};
      for (const proposal of staged) {
        for (const candidate of proposal.proposed_cards) {
          nextDecisions[
            decisionKey(proposal.proposal_id, candidate.candidate_id)
          ] = recommendedDecision(candidate);
        }
      }
      setProposals(staged);
      setDecisions(nextDecisions);
      setPage(0);
      setStage("review");
    } catch (cause) {
      const fallback =
        cause instanceof Error ? cause.message : t("previewFailed");
      setError(
        cardImportErrorMessage(cause, fallback, {
          aiGeneratedIllustration: tm("aiGeneratedIllustration"),
          metadataStripped: tm("metadataStripped"),
          otherTextMetadata: (keywords) =>
            tm("otherTextMetadata", { keywords }),
        }),
      );
    } finally {
      setUploading(false);
    }
  };

  const setBulkAction = (action: "create" | "skip") => {
    const next: Record<string, CardImportDecision> = {};
    for (const proposal of proposals) {
      for (const candidate of proposal.proposed_cards) {
        next[decisionKey(proposal.proposal_id, candidate.candidate_id)] = {
          candidate_id: candidate.candidate_id,
          action,
        };
      }
    }
    setDecisions(next);
  };

  const handleAIComplete = (
    result: AICreateResponse,
    chapters: number,
    wordsPerChapter: number,
    creativeDirection: CreativeDirectionSelection | null,
  ) => {
    if (!creativeDirection?.card_context_digest) {
      setError(t("directionBindingMissing"));
      return;
    }
    const meta = result.novel_meta;
    const plot = result.expand_idea?.plot ?? result.extract_idea.plot ?? "";
    const draft: WritingDraft = {
      _fromAI: true,
      title: meta.title,
      subtitle: meta.subtitle,
      genre: result.extract_idea.genre,
      tags: meta.tags,
      introduction: meta.introduction,
      summary: meta.summary,
      core_seed: result.core_seed.core_seed,
      worldview: meta.worldview,
      writing_style: meta.writing_style,
      narrative_pov: meta.narrative_pov,
      era_background: meta.era_background,
      plot,
      tone: result.extract_idea.tone,
      target_audience: result.extract_idea.target_audience,
      core_idea: result.extract_idea.core_idea,
      number_of_chapters: chapters,
      words_per_chapter: wordsPerChapter,
      creation_mode: "ai",
      creative_direction: creativeDirection,
      card_creation_id: newCreationId(),
      card_imports: creationSelections,
    };
    const draftId = saveWritingDraft(draft);
    clearAICreateCache();
    setRedirecting(true);
    router.push(`/${locale}/writing/new?draft=${encodeURIComponent(draftId)}`);
  };

  return (
    <div className="h-full flex flex-col">
      <Card className="h-full flex flex-col overflow-hidden">
        <Card.Header className="shrink-0">
          <div className="flex items-center justify-between gap-4 w-full">
            <div>
              <h2 className="text-lg font-bold text-foreground">{t("title")}</h2>
              <p className="mt-1 text-xs text-muted">{t("subtitle")}</p>
            </div>
            <Button variant="ghost" size="sm" onPress={onCancel}>
              {tc("back")}
            </Button>
          </div>
        </Card.Header>

        <Card.Content className="flex-1 overflow-y-auto">
          {redirecting ? (
            <div className="flex items-center justify-center h-32">
              <p className="text-sm text-muted">{tb("aiCompleteRedirect")}</p>
            </div>
          ) : stage === "upload" ? (
            <div className="space-y-5 p-1">
              <div className="rounded-xl border border-border p-4">
                <label
                  htmlFor="card-driven-characters"
                  className="block text-sm font-semibold text-foreground"
                >
                  {t("characterFiles")}
                </label>
                <p className="mt-1 text-xs leading-5 text-muted">
                  {t("characterFilesHint")}
                </p>
                <input
                  id="card-driven-characters"
                  type="file"
                  multiple
                  accept=".json,.png,application/json,image/png"
                  className="mt-3 block w-full text-sm text-foreground file:mr-3 file:rounded-lg file:border-0 file:bg-primary/10 file:px-3 file:py-2 file:text-primary"
                  onChange={(event) =>
                    setCharacterFiles(Array.from(event.target.files ?? []))
                  }
                />
                {characterFiles.length > 0 && (
                  <p className="mt-2 text-xs text-muted">
                    {t("selectedFiles", { count: characterFiles.length })}
                  </p>
                )}
              </div>

              <div className="rounded-xl border border-border p-4">
                <label
                  htmlFor="card-driven-world"
                  className="block text-sm font-semibold text-foreground"
                >
                  {t("worldFile")}
                </label>
                <p className="mt-1 text-xs leading-5 text-muted">
                  {t("worldFileHint")}
                </p>
                <input
                  id="card-driven-world"
                  type="file"
                  accept=".json,application/json"
                  className="mt-3 block w-full text-sm text-foreground file:mr-3 file:rounded-lg file:border-0 file:bg-primary/10 file:px-3 file:py-2 file:text-primary"
                  onChange={(event) =>
                    setWorldFile(event.target.files?.[0] ?? null)
                  }
                />
              </div>

              <div className="rounded-xl border border-warning/40 bg-warning/5 p-4">
                <h3 className="text-sm font-semibold text-foreground">
                  {t("safetyTitle")}
                </h3>
                <p className="mt-1 text-xs leading-5 text-muted">
                  {t("safetyDescription")}
                </p>
              </div>

              {error && (
                <p
                  role="alert"
                  className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950/30 dark:text-red-300"
                >
                  {error}
                </p>
              )}

              <Button
                variant="primary"
                className="w-full"
                isDisabled={uploading || characterFiles.length === 0}
                onPress={handlePreview}
              >
                {uploading ? t("parsing") : t("preview")}
              </Button>
            </div>
          ) : stage === "review" ? (
            <div className="space-y-5 p-1">
              <div className="rounded-xl border border-border p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <h3 className="text-sm font-semibold text-foreground">
                      {t("reviewTitle")}
                    </h3>
                    <p className="mt-1 text-xs leading-5 text-muted">
                      {t("reviewHint", {
                        files: proposals.length,
                        candidates: allCandidates.length,
                      })}
                    </p>
                  </div>
                  <div className="flex gap-2">
                    <Button
                      variant="secondary"
                      size="sm"
                      onPress={() => setBulkAction("create")}
                    >
                      {t("bulkCreate")}
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      onPress={() => setBulkAction("skip")}
                    >
                      {t("bulkSkip")}
                    </Button>
                  </div>
                </div>
              </div>

              {proposals.map((proposal) => (
                <div
                  key={proposal.proposal_id}
                  className="rounded-xl border border-border p-4"
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <h4 className="text-sm font-semibold text-foreground">
                      {proposal.source_name || t("unnamedSource")}
                    </h4>
                    <span className="rounded-full bg-muted/20 px-2 py-1 text-xs text-muted">
                      {t("sourceBadge", {
                        format: proposal.source_format,
                        container: proposal.source_container,
                      })}
                    </span>
                  </div>
                  {proposal.container_preview.png_preview_label && (
                    <p className="mt-2 text-xs text-muted">
                      {proposal.container_preview.png_preview_label}
                    </p>
                  )}
                  {proposal.container_preview.image_data_discarded && (
                    <p className="mt-2 text-xs text-warning">
                      {t("imageDiscarded")}
                    </p>
                  )}
                  {proposal.duplicate_source && (
                    <p className="mt-2 text-xs text-warning">
                      {t("duplicateSource")}
                    </p>
                  )}
                  {proposal.detected_warnings.length > 0 && (
                    <ul className="mt-2 list-disc space-y-1 pl-5 text-xs text-warning">
                      {proposal.detected_warnings.map((warning, index) => (
                        <li key={`${proposal.proposal_id}-warning-${index}`}>
                          {warning}
                        </li>
                      ))}
                    </ul>
                  )}
                  {(proposal.prompt_risk_fields.length > 0 ||
                    proposal.decorators.length > 0 ||
                    proposal.assets.length > 0) && (
                    <p className="mt-2 text-xs leading-5 text-warning">
                      {t("isolatedRiskSummary", {
                        prompts: proposal.prompt_risk_fields.length,
                        decorators: proposal.decorators.length,
                        assets: proposal.assets.length,
                      })}
                    </p>
                  )}
                </div>
              ))}

              <div className="space-y-3">
                {visibleCandidates.map(({ proposal, candidate }) => {
                  const key = decisionKey(
                    proposal.proposal_id,
                    candidate.candidate_id,
                  );
                  const current = decisions[key];
                  return (
                    <div
                      key={key}
                      className="rounded-xl border border-border bg-surface-secondary/10 p-4"
                    >
                      <div className="grid gap-4 md:grid-cols-[1fr_220px]">
                        <div className="min-w-0">
                          <div className="flex flex-wrap items-center gap-2">
                            <h4 className="text-sm font-semibold text-foreground">
                              {candidate.fields.name}
                            </h4>
                            <span className="rounded-full bg-primary/10 px-2 py-0.5 text-[11px] text-primary">
                              {t(`type.${candidate.target_type}`)}
                            </span>
                          </div>
                          {candidate.fields.description && (
                            <p className="mt-2 line-clamp-4 text-xs leading-5 text-muted">
                              {candidate.fields.description}
                            </p>
                          )}
                          {candidate.target_type === "character" &&
                            candidate.fields.interop && (
                              <p className="mt-2 text-xs leading-5 text-warning">
                                {t("greyFieldsNotice")}
                              </p>
                            )}
                          {candidate.interop_preview?.unsupported_features &&
                            candidate.interop_preview.unsupported_features
                              .length > 0 && (
                                <p className="mt-2 text-xs leading-5 text-warning">
                                  {t("advancedFeaturesIsolated", {
                                    count:
                                      candidate.interop_preview
                                        .unsupported_features.length,
                                  })}
                                </p>
                              )}
                        </div>
                        <div>
                          <label
                            htmlFor={`decision-${key}`}
                            className="mb-1 block text-xs font-medium text-foreground"
                          >
                            {t("decisionLabel")}
                          </label>
                          <select
                            id={`decision-${key}`}
                            className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm text-foreground"
                            value={current ? decisionValue(current) : "create"}
                            onChange={(event) =>
                              setDecisions((previous) => ({
                                ...previous,
                                [key]: parseDecisionValue(
                                  candidate,
                                  event.target.value,
                                ),
                              }))
                            }
                          >
                            <option value="create">
                              {t("decision.create")}
                            </option>
                            {candidate.conflicts.map((conflict) => (
                              <option
                                key={`merge-${conflict.target_card_id}`}
                                value={`${conflict.is_deleted ? "restore_merge" : "merge"}:${conflict.target_card_id}`}
                              >
                                {t(
                                  conflict.is_deleted
                                    ? "decision.restore_merge"
                                    : "decision.merge",
                                )}
                              </option>
                            ))}
                            <option value="skip">
                              {t("decision.skip")}
                            </option>
                          </select>
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>

              {pageCount > 1 && (
                <div className="flex items-center justify-between gap-3">
                  <Button
                    variant="ghost"
                    size="sm"
                    isDisabled={page === 0}
                    onPress={() => setPage((current) => current - 1)}
                  >
                    {t("previousPage")}
                  </Button>
                  <p className="text-xs text-muted">
                    {t("page", { current: page + 1, total: pageCount })}
                  </p>
                  <Button
                    variant="ghost"
                    size="sm"
                    isDisabled={page + 1 >= pageCount}
                    onPress={() => setPage((current) => current + 1)}
                  >
                    {t("nextPage")}
                  </Button>
                </div>
              )}

              {error && (
                <p role="alert" className="text-sm text-danger">
                  {error}
                </p>
              )}

              <div className="flex flex-col gap-2 sm:flex-row">
                <Button
                  variant="ghost"
                  className="sm:flex-1"
                  onPress={() => setStage("upload")}
                >
                  {t("chooseAgain")}
                </Button>
                <Button
                  variant="primary"
                  className="sm:flex-[2]"
                  onPress={() => {
                    clearAICreateCache();
                    setError("");
                    setStage("direction");
                  }}
                >
                  {t("confirmReview")}
                </Button>
              </div>
            </div>
          ) : (
            <div className="space-y-4">
              <div className="rounded-xl border border-warning/40 bg-warning/5 p-4">
                <h3 className="text-sm font-semibold text-foreground">
                  {t("directionTitle")}
                </h3>
                <p className="mt-1 text-xs leading-5 text-muted">
                  {t("directionHint")}
                </p>
              </div>
              {error && (
                <p role="alert" className="text-sm text-danger">
                  {error}
                </p>
              )}
              <AICreateStepper
                onComplete={handleAIComplete}
                cardImports={directionReferences}
                requireDirector
                initialIdea={t("ideaSeed")}
              />
            </div>
          )}
        </Card.Content>
      </Card>
    </div>
  );
}
