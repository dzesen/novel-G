"use client";

import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Button } from "@heroui/react";
import { useTranslations } from "next-intl";
import { ApiError, apiPost, apiPostForm, apiPostRaw } from "@/lib/api";
import {
  CardAvatarSourceUnavailable,
  isPermanentCardAvatarTransferFailure,
} from "@/lib/cardAvatarTransfer";
import { cardImportErrorMessage } from "@/lib/cardImportErrors";
import type {
  CardImportCandidate,
  CardImportConflict,
  CardImportDecision,
  CardImportProposal,
  CharacterCardAvatarImportResult,
  ReferenceCardCurationResult,
} from "@/types/novel";

const MAX_FILES = 32;
const PAGE_SIZE = 24;
const VALUE_PREVIEW_CHARS = 900;

interface CardImportDialogProps {
  novelId: string;
  isOpen: boolean;
  onClose: () => void;
  onApplied: () => void | Promise<void>;
}

interface UploadFailure {
  file: File;
  error: ApiError | Error;
}

interface StructuredErrorDetail {
  code?: string;
  path?: string;
  message?: string;
  limit_name?: string;
  current_value?: number;
  max_value?: number;
  current_bytes?: number;
  max_bytes?: number;
}

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
      overwrite_fields: [],
    };
  }
  return {
    candidate_id: candidate.candidate_id,
    action:
      candidate.recommended_action === "skip" ? "skip" : "create",
    overwrite_fields: [],
  };
}

function decisionValue(decision: CardImportDecision): string {
  return decision.target_card_id
    ? `${decision.action}:${decision.target_card_id}`
    : decision.action;
}

function parseDecisionValue(
  candidate: CardImportCandidate,
  value: string,
): CardImportDecision {
  const [action, targetCardId] = value.split(":", 2);
  if (action === "merge" || action === "restore_merge") {
    return {
      candidate_id: candidate.candidate_id,
      action,
      target_card_id: targetCardId,
      overwrite_fields: [],
    };
  }
  return {
    candidate_id: candidate.candidate_id,
    action: action === "skip" ? "skip" : "create",
    overwrite_fields: [],
  };
}

function initializeDecisions(
  proposals: CardImportProposal[],
): Record<string, CardImportDecision> {
  return Object.fromEntries(
    proposals.flatMap((proposal) =>
      proposal.proposed_cards.map((candidate) => [
        decisionKey(proposal.proposal_id, candidate.candidate_id),
        recommendedDecision(candidate),
      ]),
    ),
  );
}

function isBlank(value: unknown): boolean {
  return (
    value === null ||
    value === undefined ||
    value === "" ||
    (Array.isArray(value) && value.length === 0) ||
    (typeof value === "object" &&
      !Array.isArray(value) &&
      Object.keys(value as Record<string, unknown>).length === 0)
  );
}

function valueText(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === null || value === undefined) return "";
  return JSON.stringify(value, null, 2);
}

function errorDetail(error: ApiError | Error): StructuredErrorDetail | null {
  if (!(error instanceof ApiError)) return null;
  if (!error.detail || typeof error.detail !== "object") return null;
  return error.detail as StructuredErrorDetail;
}

function addCounts(
  left: ReferenceCardCurationResult["counts"],
  right: ReferenceCardCurationResult["counts"],
): ReferenceCardCurationResult["counts"] {
  return {
    created: left.created + right.created,
    merged: left.merged + right.merged,
    restored_merged: left.restored_merged + right.restored_merged,
    skipped: left.skipped + right.skipped,
  };
}

function requiresAvatarTransfer(
  proposal: CardImportProposal,
  result: ReferenceCardCurationResult | undefined,
): boolean {
  if (!proposal.avatar_preview?.importable || !result) return false;
  const character = result.mappings.find(
    (mapping) => mapping.candidate_id === "character:0",
  );
  return Boolean(character && character.action !== "skip" && character.card_id);
}

export default function CardImportDialog({
  novelId,
  isOpen,
  onClose,
  onApplied,
}: CardImportDialogProps) {
  const t = useTranslations("writing.referenceCards.import");
  const tc = useTranslations("writing.referenceCards.curation");
  const [stage, setStage] = useState<"upload" | "review" | "complete">(
    "upload",
  );
  const [files, setFiles] = useState<File[]>([]);
  const [proposals, setProposals] = useState<CardImportProposal[]>([]);
  const [proposalFiles, setProposalFiles] = useState<Record<string, File>>({});
  const [decisions, setDecisions] = useState<
    Record<string, CardImportDecision>
  >({});
  const [uploadFailures, setUploadFailures] = useState<UploadFailure[]>([]);
  const [applyErrors, setApplyErrors] = useState<Record<string, string>>({});
  const [results, setResults] = useState<
    Record<string, ReferenceCardCurationResult>
  >({});
  const [avatarResults, setAvatarResults] = useState<
    Record<string, CharacterCardAvatarImportResult>
  >({});
  const [avatarRejections, setAvatarRejections] = useState<
    Record<string, true>
  >({});
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState({ current: 0, total: 0 });
  const [applying, setApplying] = useState(false);
  const [applyProgress, setApplyProgress] = useState({ current: 0, total: 0 });
  const [page, setPage] = useState(0);

  const reset = () => {
    setStage("upload");
    setFiles([]);
    setProposals([]);
    setProposalFiles({});
    setDecisions({});
    setUploadFailures([]);
    setApplyErrors({});
    setResults({});
    setAvatarResults({});
    setAvatarRejections({});
    setUploadProgress({ current: 0, total: 0 });
    setApplyProgress({ current: 0, total: 0 });
    setPage(0);
  };

  const close = () => {
    if (uploading || applying) return;
    reset();
    onClose();
  };

  useEffect(() => {
    if (!isOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !uploading && !applying) close();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  });

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
  const summary = useMemo(() => {
    const values = Object.values(decisions);
    return {
      create: values.filter((item) => item.action === "create").length,
      merge: values.filter((item) => item.action === "merge").length,
      restore: values.filter((item) => item.action === "restore_merge").length,
      skip: values.filter((item) => item.action === "skip").length,
    };
  }, [decisions]);
  const finalCounts = useMemo(
    () =>
      Object.values(results).reduce(
        (counts, result) => addCounts(counts, result.counts),
        { created: 0, merged: 0, restored_merged: 0, skipped: 0 },
      ),
    [results],
  );
  const importedAvatarCount = Object.values(avatarResults).filter(
    (result) => result.status === "imported",
  ).length;
  const rejectedAvatarCount = Object.keys(avatarRejections).length;
  const allTransfersComplete =
    proposals.length > 0 &&
    proposals.every((proposal) => {
      const result = results[proposal.proposal_id];
      return Boolean(
        result &&
          (!requiresAvatarTransfer(proposal, result) ||
            avatarResults[proposal.proposal_id] ||
            avatarRejections[proposal.proposal_id]),
      );
    });

  const preview = async () => {
    if (!files.length || files.length > MAX_FILES) return;
    setUploading(true);
    setUploadFailures([]);
    setUploadProgress({ current: 0, total: files.length });
    const nextProposals: CardImportProposal[] = [];
    const nextProposalFiles: Record<string, File> = {};
    const nextFailures: UploadFailure[] = [];
    for (const [index, file] of files.entries()) {
      setUploadProgress({ current: index + 1, total: files.length });
      const form = new FormData();
      form.append("file", file);
      try {
        const proposal = await apiPostForm<CardImportProposal>(
          `/api/card-imports/novel/${novelId}/preview`,
          form,
        );
        nextProposals.push(proposal);
        nextProposalFiles[proposal.proposal_id] = file;
      } catch (reason) {
        nextFailures.push({
          file,
          error:
            reason instanceof Error
              ? reason
              : new Error(t("errors.previewFailed")),
        });
      }
    }
    setUploadFailures(nextFailures);
    if (nextProposals.length) {
      setProposals(nextProposals);
      setProposalFiles(nextProposalFiles);
      setDecisions(initializeDecisions(nextProposals));
      setPage(0);
      setStage("review");
    }
    setUploading(false);
  };

  const patchDecision = (
    proposalId: string,
    candidateId: string,
    next: CardImportDecision,
  ) => {
    setDecisions((current) => ({
      ...current,
      [decisionKey(proposalId, candidateId)]: next,
    }));
  };

  const setBulkAction = (action: "create" | "skip") => {
    setDecisions(
      Object.fromEntries(
        allCandidates.map(({ proposal, candidate }) => [
          decisionKey(proposal.proposal_id, candidate.candidate_id),
          {
            candidate_id: candidate.candidate_id,
            action,
            overwrite_fields: [],
          } satisfies CardImportDecision,
        ]),
      ),
    );
  };

  const apply = async () => {
    const pending = proposals.filter(
      (proposal) => {
        const result = results[proposal.proposal_id];
        return (
          !result ||
          (requiresAvatarTransfer(proposal, result) &&
            !avatarResults[proposal.proposal_id] &&
            !avatarRejections[proposal.proposal_id])
        );
      },
    );
    if (!pending.length) return;
    setApplying(true);
    setApplyErrors({});
    setApplyProgress({ current: 0, total: pending.length });
    const nextResults = { ...results };
    const nextAvatarResults = { ...avatarResults };
    const nextAvatarRejections = { ...avatarRejections };
    const nextErrors: Record<string, string> = {};
    for (const [index, proposal] of pending.entries()) {
      setApplyProgress({ current: index + 1, total: pending.length });
      let phase: "apply" | "avatar" = "apply";
      try {
        const result =
          nextResults[proposal.proposal_id] ??
          (await apiPost<ReferenceCardCurationResult>(
            `/api/card-imports/proposals/${proposal.proposal_id}/apply`,
            {
              digest: proposal.digest,
              decisions: proposal.proposed_cards.map(
                (candidate) =>
                  decisions[
                    decisionKey(
                      proposal.proposal_id,
                      candidate.candidate_id,
                    )
                  ] ?? recommendedDecision(candidate),
                ),
            },
          ));
        nextResults[proposal.proposal_id] = result;
        if (
          requiresAvatarTransfer(proposal, result) &&
          !nextAvatarResults[proposal.proposal_id] &&
          !nextAvatarRejections[proposal.proposal_id]
        ) {
          phase = "avatar";
          const source = proposalFiles[proposal.proposal_id];
          if (!source) {
            throw new CardAvatarSourceUnavailable();
          }
          nextAvatarResults[proposal.proposal_id] =
            await apiPostRaw<CharacterCardAvatarImportResult>(
              `/api/card-imports/proposals/${proposal.proposal_id}/avatar`,
              source,
              proposal.source_container === "png"
                ? "image/png"
                : "application/json",
            );
        }
      } catch (reason) {
        if (
          phase === "avatar" &&
          isPermanentCardAvatarTransferFailure(reason)
        ) {
          nextAvatarRejections[proposal.proposal_id] = true;
        } else {
          nextErrors[proposal.proposal_id] =
            phase === "avatar"
              ? t("errors.avatarImportFailed")
              : reason instanceof Error
                ? reason.message
                : t("errors.applyFailed");
        }
      }
    }
    setResults(nextResults);
    setAvatarResults(nextAvatarResults);
    setAvatarRejections(nextAvatarRejections);
    setApplyErrors(nextErrors);
    if (Object.keys(nextResults).length > Object.keys(results).length) {
      await onApplied();
    }
    if (
      Object.keys(nextErrors).length === 0 &&
      proposals.every((proposal) => {
        const result = nextResults[proposal.proposal_id];
        return Boolean(
          result &&
            (!requiresAvatarTransfer(proposal, result) ||
              nextAvatarResults[proposal.proposal_id] ||
              nextAvatarRejections[proposal.proposal_id]),
        );
      })
    ) {
      setStage("complete");
    }
    setApplying(false);
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
        aria-labelledby="card-import-title"
        className="flex h-full w-full max-w-6xl flex-col overflow-hidden bg-background shadow-2xl sm:h-[min(900px,calc(100vh-2.5rem))] sm:rounded-2xl sm:border sm:border-border"
      >
        <header className="flex shrink-0 items-start justify-between gap-5 border-b border-border px-5 py-4 sm:px-7">
          <div>
            <p className="text-xs font-semibold uppercase tracking-[0.16em] text-accent">
              {t("eyebrow")}
            </p>
            <h2
              id="card-import-title"
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
            onClick={close}
            disabled={uploading || applying}
            aria-label={t("close")}
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-border text-xl text-muted transition-colors hover:bg-surface-secondary hover:text-foreground disabled:opacity-40"
          >
            ×
          </button>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto">
          {stage === "upload" ? (
            <UploadStage
              files={files}
              failures={uploadFailures}
              uploading={uploading}
              progress={uploadProgress}
              onFiles={setFiles}
              onPreview={() => void preview()}
            />
          ) : stage === "complete" ? (
            <div className="mx-auto max-w-2xl px-6 py-14 text-center">
              <span className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-emerald-100 text-xl text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300">
                ✓
              </span>
              <h3 className="mt-5 text-xl font-semibold text-foreground">
                {t("complete.title")}
              </h3>
              <p className="mt-2 text-sm leading-6 text-muted">
                {tc("appliedDetail", {
                  created: finalCounts.created,
                  merged: finalCounts.merged,
                  restored: finalCounts.restored_merged,
                  skipped: finalCounts.skipped,
                })}
              </p>
              <p className="mt-3 text-xs leading-5 text-muted">
                {t("complete.refreshHint")}
              </p>
              {importedAvatarCount > 0 && (
                <p className="mt-2 text-sm font-medium text-emerald-700 dark:text-emerald-300">
                  {t("complete.avatarsImported", {
                    count: importedAvatarCount,
                  })}
                </p>
              )}
              {rejectedAvatarCount > 0 && (
                <p className="mt-2 text-sm font-medium text-amber-800 dark:text-amber-200">
                  {t("complete.avatarsRejected", {
                    count: rejectedAvatarCount,
                  })}
                </p>
              )}
              <Button
                className="mt-7 bg-accent text-white hover:bg-accent-hover"
                variant="primary"
                onPress={close}
              >
                {tc("done")}
              </Button>
            </div>
          ) : (
            <div className="space-y-5 px-5 py-5 sm:px-7">
              <div className="flex flex-wrap items-start justify-between gap-3 rounded-xl border border-border bg-surface p-4">
                <div>
                  <h3 className="text-base font-semibold text-foreground">
                    {t("review.title")}
                  </h3>
                  <p className="mt-1 text-sm leading-6 text-muted">
                    {t("review.description", {
                      files: proposals.length,
                      candidates: allCandidates.length,
                    })}
                  </p>
                </div>
                <div className="flex flex-wrap gap-2">
                  <Button
                    variant="secondary"
                    size="sm"
                    isDisabled={applying}
                    onPress={() => setBulkAction("create")}
                  >
                    {t("review.bulkCreate")}
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    isDisabled={applying}
                    onPress={() => setBulkAction("skip")}
                  >
                    {t("review.bulkSkip")}
                  </Button>
                </div>
              </div>

              {uploadFailures.length > 0 && (
                <UploadFailureList failures={uploadFailures} />
              )}

              <div className="space-y-3">
                {proposals.map((proposal) => (
                  <SourceReview
                    key={proposal.proposal_id}
                    proposal={proposal}
                    result={results[proposal.proposal_id]}
                    avatarResult={avatarResults[proposal.proposal_id]}
                    avatarRejected={avatarRejections[proposal.proposal_id]}
                    applyError={applyErrors[proposal.proposal_id]}
                  />
                ))}
              </div>

              <div className="space-y-4">
                {visibleCandidates.map(({ proposal, candidate }) => {
                  const key = decisionKey(
                    proposal.proposal_id,
                    candidate.candidate_id,
                  );
                  return (
                    <CandidateReview
                      key={key}
                      proposalId={proposal.proposal_id}
                      candidate={candidate}
                      decision={
                        decisions[key] ?? recommendedDecision(candidate)
                      }
                      disabled={
                        applying || Boolean(results[proposal.proposal_id])
                      }
                      applied={Boolean(results[proposal.proposal_id])}
                      onDecision={(next) =>
                        patchDecision(
                          proposal.proposal_id,
                          candidate.candidate_id,
                          next,
                        )
                      }
                    />
                  );
                })}
              </div>

              {pageCount > 1 && (
                <div className="flex items-center justify-between gap-3">
                  <Button
                    variant="ghost"
                    size="sm"
                    isDisabled={page === 0 || applying}
                    onPress={() => setPage((current) => current - 1)}
                  >
                    {t("review.previousPage")}
                  </Button>
                  <p className="text-xs text-muted">
                    {t("review.page", {
                      current: page + 1,
                      total: pageCount,
                    })}
                  </p>
                  <Button
                    variant="ghost"
                    size="sm"
                    isDisabled={page + 1 >= pageCount || applying}
                    onPress={() => setPage((current) => current + 1)}
                  >
                    {t("review.nextPage")}
                  </Button>
                </div>
              )}
            </div>
          )}
        </div>

        {stage === "review" && (
          <footer className="flex shrink-0 flex-col gap-3 border-t border-border bg-surface px-5 py-4 sm:flex-row sm:items-center sm:justify-between sm:px-7">
            <p className="text-xs leading-5 text-muted">
              {tc("summary", {
                create: summary.create,
                merge: summary.merge,
                restore: summary.restore,
                skip: summary.skip,
              })}
            </p>
            <div className="flex gap-2">
              <Button
                variant="ghost"
                isDisabled={applying}
                onPress={() => {
                  setStage("upload");
                  setProposals([]);
                  setDecisions({});
                  setResults({});
                  setAvatarResults({});
                  setAvatarRejections({});
                  setProposalFiles({});
                  setApplyErrors({});
                  setPage(0);
                }}
              >
                {t("review.chooseAgain")}
              </Button>
              <Button
                className="bg-accent text-white hover:bg-accent-hover"
                variant="primary"
                isDisabled={
                  applying ||
                  allTransfersComplete
                }
                onPress={() => void apply()}
              >
                {applying
                  ? t("review.applying", {
                      current: applyProgress.current,
                      total: applyProgress.total,
                    })
                  : tc("apply")}
              </Button>
            </div>
          </footer>
        )}
      </section>
    </div>
  );
}

function UploadStage({
  files,
  failures,
  uploading,
  progress,
  onFiles,
  onPreview,
}: {
  files: File[];
  failures: UploadFailure[];
  uploading: boolean;
  progress: { current: number; total: number };
  onFiles: (files: File[]) => void;
  onPreview: () => void;
}) {
  const t = useTranslations("writing.referenceCards.import");
  return (
    <div className="mx-auto max-w-4xl space-y-5 px-5 py-6 sm:px-7 sm:py-8">
      <div className="rounded-xl border border-border bg-surface p-5">
        <label
          htmlFor="existing-novel-card-import"
          className="block text-sm font-semibold text-foreground"
        >
          {t("upload.label")}
        </label>
        <p className="mt-1 text-sm leading-6 text-muted">
          {t("upload.hint")}
        </p>
        <input
          id="existing-novel-card-import"
          type="file"
          multiple
          accept=".json,.png,application/json,image/png"
          disabled={uploading}
          className="mt-4 block w-full text-sm text-foreground file:mr-3 file:rounded-lg file:border-0 file:bg-accent/10 file:px-3 file:py-2 file:font-semibold file:text-accent"
          onChange={(event) =>
            onFiles(Array.from(event.target.files ?? []))
          }
        />
        {files.length > 0 && (
          <ul className="mt-4 divide-y divide-border rounded-lg border border-border bg-background">
            {files.map((file, index) => (
              <li
                key={`${file.name}:${file.size}:${file.lastModified}:${index}`}
                className="flex items-center justify-between gap-4 px-3 py-2.5"
              >
                <span className="min-w-0">
                  <span className="block truncate text-sm font-medium text-foreground">
                    {file.name}
                  </span>
                  <span className="block text-xs text-muted">
                    {t("upload.fileSize", {
                      bytes: file.size.toLocaleString(),
                    })}
                  </span>
                </span>
                <button
                  type="button"
                  disabled={uploading}
                  onClick={() =>
                    onFiles(files.filter((_, itemIndex) => itemIndex !== index))
                  }
                  className="shrink-0 text-xs font-semibold text-red-600 hover:underline disabled:opacity-40"
                >
                  {t("upload.remove")}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <div className="rounded-xl border border-border bg-surface p-4">
          <h3 className="text-sm font-semibold text-foreground">
            {t("upload.formatsTitle")}
          </h3>
          <p className="mt-1 text-xs leading-5 text-muted">
            {t("upload.formatsDetail")}
          </p>
        </div>
        <div className="rounded-xl border border-amber-300 bg-amber-50 p-4 dark:border-amber-900 dark:bg-amber-950">
          <h3 className="text-sm font-semibold text-amber-950 dark:text-amber-100">
            {t("upload.safetyTitle")}
          </h3>
          <p className="mt-1 text-xs leading-5 text-amber-800 dark:text-amber-200">
            {t("upload.safetyDetail")}
          </p>
        </div>
      </div>

      {failures.length > 0 && <UploadFailureList failures={failures} />}

      {files.length > MAX_FILES && (
        <p
          role="alert"
          className="rounded-lg border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-800 dark:border-red-900 dark:bg-red-950 dark:text-red-200"
        >
          {t("errors.tooManyFiles", { max: MAX_FILES })}
        </p>
      )}

      <Button
        className="w-full bg-accent text-white hover:bg-accent-hover"
        variant="primary"
        isDisabled={
          uploading || files.length === 0 || files.length > MAX_FILES
        }
        onPress={onPreview}
      >
        {uploading
          ? t("upload.parsing", {
              current: progress.current,
              total: progress.total,
            })
          : t("upload.preview")}
      </Button>
    </div>
  );
}

function UploadFailureList({ failures }: { failures: UploadFailure[] }) {
  const t = useTranslations("writing.referenceCards.import");
  const tm = useTranslations("interopErrors.missingCharacterMetadata");
  return (
    <section
      aria-labelledby="card-import-rejections"
      className="rounded-xl border border-red-300 bg-red-50 p-4 dark:border-red-900 dark:bg-red-950"
    >
      <h3
        id="card-import-rejections"
        className="text-sm font-semibold text-red-900 dark:text-red-100"
      >
        {t("errors.rejectedTitle", { count: failures.length })}
      </h3>
      <div className="mt-3 space-y-3">
        {failures.map((failure, index) => {
          const detail = errorDetail(failure.error);
          const current =
            detail?.current_value ??
            detail?.current_bytes ??
            (detail?.code === "file_too_large"
              ? failure.file.size
              : undefined);
          const maximum = detail?.max_value ?? detail?.max_bytes;
          const isLimit =
            current !== undefined ||
            maximum !== undefined ||
            detail?.code?.includes("too_large") ||
            detail?.code?.includes("too_many");
          return (
            <article
              key={`${failure.file.name}:${failure.file.lastModified}:${index}`}
              className="rounded-lg border border-red-200 bg-white/70 p-3 dark:border-red-900 dark:bg-black/10"
            >
              <p className="text-sm font-semibold text-red-900 dark:text-red-100">
                {failure.file.name}
              </p>
              <p className="mt-1 text-xs leading-5 text-red-800 dark:text-red-200">
                {cardImportErrorMessage(
                  failure.error,
                  failure.error.message.trim() || t("errors.previewFailed"),
                  {
                    aiGeneratedIllustration: tm("aiGeneratedIllustration"),
                    metadataStripped: tm("metadataStripped"),
                    otherTextMetadata: (keywords) =>
                      tm("otherTextMetadata", { keywords }),
                  },
                )}
              </p>
              {detail && (
                <dl className="mt-2 grid gap-2 text-xs text-red-800 dark:text-red-200 sm:grid-cols-3">
                  <div>
                    <dt className="font-semibold">{t("errors.item")}</dt>
                    <dd className="mt-0.5 break-all">
                      {detail.limit_name ||
                        detail.path ||
                        detail.code ||
                        t("errors.unknownItem")}
                    </dd>
                  </div>
                  {isLimit && (
                    <>
                      <div>
                        <dt className="font-semibold">
                          {t("errors.currentValue")}
                        </dt>
                        <dd className="mt-0.5">
                          {current !== undefined
                            ? current.toLocaleString()
                            : t("errors.valueUnavailable")}
                        </dd>
                      </div>
                      <div>
                        <dt className="font-semibold">
                          {t("errors.maximumValue")}
                        </dt>
                        <dd className="mt-0.5">
                          {maximum !== undefined
                            ? maximum.toLocaleString()
                            : t("errors.seeServerMessage")}
                        </dd>
                      </div>
                    </>
                  )}
                </dl>
              )}
            </article>
          );
        })}
      </div>
    </section>
  );
}

function SourceReview({
  proposal,
  result,
  avatarResult,
  avatarRejected,
  applyError,
}: {
  proposal: CardImportProposal;
  result?: ReferenceCardCurationResult;
  avatarResult?: CharacterCardAvatarImportResult;
  avatarRejected?: boolean;
  applyError?: string;
}) {
  const t = useTranslations("writing.referenceCards.import");
  const pngClass = proposal.container_preview.png_chunk_classification;
  const pngClassification =
    pngClass === "v3"
      ? t("source.pngClassification.v3")
      : pngClass === "pseudo_v3"
        ? t("source.pngClassification.pseudoV3")
        : pngClass === "v2"
          ? t("source.pngClassification.v2")
          : pngClass === "v1"
            ? t("source.pngClassification.v1")
            : pngClass
              ? t("source.pngClassification.other", { value: pngClass })
              : null;
  const worldbookWarnings = proposal.worldbook_preview?.detected_warnings ?? [];
  const topUnknown =
    proposal.worldbook_preview?.unrecognized_top_level_fields ?? [];
  return (
    <article className="rounded-xl border border-border bg-surface p-4 sm:p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-foreground">
            {proposal.source_name || t("source.unnamed")}
          </h3>
          <p className="mt-1 text-xs text-muted">
            {t("source.format", {
              format: proposal.source_format,
              container: proposal.source_container,
              count: proposal.proposed_cards.length,
            })}
          </p>
        </div>
        {result ? (
          <span className="rounded-full bg-emerald-100 px-2.5 py-1 text-xs font-semibold text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200">
            {t("source.applied")}
          </span>
        ) : (
          <span className="rounded-full bg-accent/10 px-2.5 py-1 text-xs font-semibold text-accent">
            {t("source.pending")}
          </span>
        )}
      </div>

      {proposal.source_container === "png" && (
        <div className="mt-3 rounded-lg border border-border bg-background p-3">
          <p className="text-xs font-semibold text-foreground">
            {t("source.pngTitle")}
          </p>
          <dl className="mt-2 grid gap-2 text-xs text-muted sm:grid-cols-2">
            <div>
              <dt>{t("source.pngChunk")}</dt>
              <dd className="mt-0.5 font-semibold text-foreground">
                {proposal.container_preview.selected_png_chunk ||
                  t("source.notAvailable")}
              </dd>
            </div>
            <div>
              <dt>{t("source.pngJudgement")}</dt>
              <dd className="mt-0.5 font-semibold text-foreground">
                {pngClassification || t("source.notAvailable")}
              </dd>
            </div>
          </dl>
          {avatarRejected ? (
            <p className="mt-2 text-xs leading-5 text-amber-800 dark:text-amber-200">
              {t("source.avatarRejected")}
            </p>
          ) : avatarResult?.status === "imported" ? (
            <p className="mt-2 text-xs leading-5 text-emerald-700 dark:text-emerald-300">
              {t("source.avatarImported")}
            </p>
          ) : proposal.avatar_preview?.importable ? (
            <p className="mt-2 text-xs leading-5 text-amber-800 dark:text-amber-200">
              {t("source.avatarPending")}
            </p>
          ) : proposal.avatar_preview?.source_kind === "remote_url" ? (
            <p className="mt-2 text-xs leading-5 text-amber-800 dark:text-amber-200">
              {t("source.avatarRemoteNotDownloaded")}
            </p>
          ) : null}
        </div>
      )}

      {proposal.worldbook_preview && (
        <div className="mt-3 rounded-lg border border-border bg-background p-3">
          <p className="text-xs font-semibold text-foreground">
            {t("source.worldbookTitle")}
          </p>
          <p className="mt-1 text-xs text-muted">
            {t("source.worldbookDetail", {
              kind: proposal.worldbook_preview.source_kind,
              format: proposal.worldbook_preview.source_format,
              count: proposal.worldbook_preview.entry_count,
            })}
          </p>
          {topUnknown.length > 0 && (
            <LabeledValues
              className="mt-3"
              label={t("source.unrecognizedTopFields")}
              values={topUnknown}
            />
          )}
        </div>
      )}

      {(proposal.prompt_risk_fields.length > 0 ||
        proposal.decorators.length > 0 ||
        proposal.assets.length > 0) && (
        <div className="mt-3 rounded-lg border border-amber-300 bg-amber-50 p-3 dark:border-amber-900 dark:bg-amber-950">
          <p className="text-xs font-semibold text-amber-950 dark:text-amber-100">
            {t("source.riskTitle")}
          </p>
          <p className="mt-1 text-xs leading-5 text-amber-800 dark:text-amber-200">
            {t("source.riskDetail")}
          </p>
          <ul className="mt-2 space-y-1.5 text-xs text-amber-900 dark:text-amber-100">
            {proposal.prompt_risk_fields.map((risk, index) => (
              <li
                key={`${risk.path}:${risk.kind}:${index}`}
                className="flex flex-wrap items-center gap-2"
              >
                <code className="break-all">{risk.kind}</code>
                <span className="break-all text-amber-700 dark:text-amber-300">
                  {risk.path}
                </span>
                <InertBadge>{t("source.isolatedDisabled")}</InertBadge>
              </li>
            ))}
            {proposal.decorators.map((decorator, index) => (
              <li
                key={`${decorator.path}:${index}`}
                className="flex flex-wrap items-center gap-2"
              >
                <code className="break-all">
                  {t("source.decorator", { name: decorator.name })}
                </code>
                <span className="break-all text-amber-700 dark:text-amber-300">
                  {decorator.path}
                </span>
                <InertBadge>{t("source.isolatedDisabled")}</InertBadge>
              </li>
            ))}
            {proposal.assets.map((asset, index) => (
              <li
                key={`${asset.path}:${index}`}
                className="flex flex-wrap items-center gap-2"
              >
                <code className="break-all">
                  {t("source.asset", { type: asset.type })}
                </code>
                <span className="break-all text-amber-700 dark:text-amber-300">
                  {asset.path}
                </span>
                <InertBadge>
                  {asset.path === proposal.avatar_preview?.asset_path &&
                  proposal.avatar_preview.source_kind === "data_uri"
                    ? avatarRejected
                      ? t("source.avatarRejected")
                      : avatarResult?.status === "imported"
                      ? t("source.avatarImported")
                      : t("source.avatarPending")
                    : t("source.notDownloaded")}
                </InertBadge>
              </li>
            ))}
          </ul>
        </div>
      )}

      {proposal.duplicate_source && (
        <p className="mt-3 rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200">
          {t("source.duplicate")}
        </p>
      )}

      {[...proposal.detected_warnings, ...worldbookWarnings].length > 0 && (
        <div className="mt-3">
          <p className="text-xs font-semibold text-foreground">
            {t("source.warnings")}
          </p>
          <ul className="mt-1 list-disc space-y-1 pl-5 text-xs leading-5 text-muted">
            {[...proposal.detected_warnings, ...worldbookWarnings].map(
              (warning, index) => (
                <li key={`${proposal.proposal_id}:warning:${index}`}>
                  {warning}
                </li>
              ),
            )}
          </ul>
        </div>
      )}

      {applyError && (
        <p
          role="alert"
          className="mt-3 rounded-lg border border-red-300 bg-red-50 px-3 py-2 text-xs leading-5 text-red-800 dark:border-red-900 dark:bg-red-950 dark:text-red-200"
        >
          {t("source.applyError", { message: applyError })}
        </p>
      )}
    </article>
  );
}

function CandidateReview({
  proposalId,
  candidate,
  decision,
  disabled,
  applied,
  onDecision,
}: {
  proposalId: string;
  candidate: CardImportCandidate;
  decision: CardImportDecision;
  disabled: boolean;
  applied: boolean;
  onDecision: (decision: CardImportDecision) => void;
}) {
  const t = useTranslations("writing.referenceCards.import");
  const tc = useTranslations("writing.referenceCards.curation");
  const selectedConflict =
    decision.target_card_id &&
    candidate.conflicts.find(
      (conflict) => conflict.target_card_id === decision.target_card_id,
    );
  const preview = candidate.interop_preview;
  const regexNoticePaths =
    preview?.preview_notices
      ?.filter((notice) => notice.code === "regex_present")
      .map((notice) => notice.path) ?? [];
  const regexPaths = Array.from(
    new Set([...(preview?.regex_fields ?? []), ...regexNoticePaths]),
  );
  const candidateId = `${proposalId}-${candidate.candidate_id}`.replaceAll(
    ":",
    "-",
  );
  const participation =
    candidate.fields.interop?.writing_participation;
  return (
    <article className="rounded-xl border border-border bg-surface p-4 sm:p-5">
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_250px]">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-base font-semibold text-foreground">
              {candidate.fields.name}
            </h3>
            <span className="rounded-full bg-accent/10 px-2 py-0.5 text-xs font-medium text-accent">
              {t(`candidate.type.${candidate.target_type}`)}
            </span>
            {applied && (
              <span className="rounded-full bg-emerald-100 px-2 py-0.5 text-xs font-medium text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200">
                {t("candidate.applied")}
              </span>
            )}
          </div>
          <p className="mt-1 text-xs text-muted">
            {t("candidate.mappingTarget", {
              type: t(`candidate.type.${candidate.target_type}`),
            })}
          </p>
        </div>
        <label htmlFor={`decision-${candidateId}`} className="block">
          <span className="mb-1.5 block text-xs font-medium text-foreground">
            {tc("action")}
          </span>
          <select
            id={`decision-${candidateId}`}
            disabled={disabled}
            value={decisionValue(decision)}
            onChange={(event) =>
              onDecision(parseDecisionValue(candidate, event.target.value))
            }
            className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm font-medium text-foreground outline-none focus:border-accent focus:ring-2 focus:ring-accent/15 disabled:cursor-not-allowed disabled:opacity-60"
          >
            <option value="create">{tc("actions.create")}</option>
            {candidate.conflicts.map((conflict) => (
              <option
                key={conflict.target_card_id}
                value={`${conflict.is_deleted ? "restore_merge" : "merge"}:${conflict.target_card_id}`}
              >
                {conflict.is_deleted
                  ? tc("actions.restore_merge")
                  : tc("actions.merge")}{" "}
                · {conflict.match_kind} ·{" "}
                {conflict.target_card_id.slice(-6)}
              </option>
            ))}
            <option value="skip">{tc("actions.skip")}</option>
          </select>
        </label>
      </div>

      <MappedFields candidate={candidate} />

      {participation && (
        <div
          role="status"
          className={`mt-4 rounded-lg border px-3 py-2.5 text-xs leading-5 ${
            participation.status === "not_participating"
              ? "border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200"
              : "border-emerald-300 bg-emerald-50 text-emerald-900 dark:border-emerald-900 dark:bg-emerald-950 dark:text-emerald-200"
          }`}
        >
          <p className="font-semibold">
            {participation.status === "not_participating"
              ? t("candidate.participation.notParticipating")
              : t("candidate.participation.active")}
          </p>
          <p className="mt-0.5">
            {participation.status === "not_participating"
              ? t("candidate.participation.notParticipatingHint")
              : t("candidate.participation.activeHint", {
                  fields: participation.projected_fields.join(", "),
                })}
          </p>
        </div>
      )}

      {regexPaths.length > 0 && (
        <div className="mt-4 rounded-lg border border-amber-300 bg-amber-50 p-3 dark:border-amber-900 dark:bg-amber-950">
          <p className="text-xs font-semibold text-amber-950 dark:text-amber-100">
            {t("candidate.regexTitle")}
          </p>
          <p className="mt-1 text-xs leading-5 text-amber-800 dark:text-amber-200">
            {t("candidate.regexDetail")}
          </p>
          <ul className="mt-2 list-disc space-y-1 pl-5 text-xs text-amber-900 dark:text-amber-100">
            {regexPaths.map((path) => (
              <li key={path} className="break-all">
                {path}
              </li>
            ))}
          </ul>
        </div>
      )}

      {(preview?.unrecognized_fields?.length ?? 0) > 0 && (
        <LabeledValues
          className="mt-4"
          label={t("candidate.unrecognizedFields")}
          values={preview?.unrecognized_fields ?? []}
        />
      )}

      {(preview?.unsupported_features?.length ?? 0) > 0 && (
        <div className="mt-4">
          <p className="text-xs font-semibold text-foreground">
            {t("candidate.unsupportedFeatures")}
          </p>
          <ul className="mt-2 flex flex-wrap gap-2">
            {preview?.unsupported_features?.map((feature) => (
              <li
                key={`${feature.field}:${feature.category}`}
                className="rounded-md bg-surface-secondary px-2 py-1 text-xs text-muted"
              >
                {feature.field} · {feature.category} ·{" "}
                {t("candidate.disabled")}
              </li>
            ))}
          </ul>
        </div>
      )}

      {(preview?.preview_notices?.length ?? 0) > 0 && (
        <div className="mt-4">
          <p className="text-xs font-semibold text-foreground">
            {t("candidate.notices")}
          </p>
          <ul className="mt-2 space-y-1.5 text-xs leading-5 text-muted">
            {preview?.preview_notices?.map((notice, index) => (
              <li
                key={`${notice.code}:${notice.path}:${index}`}
                className="rounded-md bg-surface-secondary px-2.5 py-2"
              >
                <span className="font-medium text-foreground">
                  {notice.code === "regex_present"
                    ? t("candidate.notice.regex")
                    : notice.code === "decorator_present"
                      ? t("candidate.notice.decorator")
                      : notice.code === "unrecognized_field"
                        ? t("candidate.notice.unrecognized")
                        : notice.code === "unsupported_feature"
                          ? t("candidate.notice.unsupported")
                          : notice.code === "unknown_position"
                            ? t("candidate.notice.unknownPosition")
                            : t("candidate.notice.other", {
                                code: notice.code,
                              })}
                </span>
                <span className="ml-2 break-all">{notice.path}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {(decision.action === "merge" ||
        decision.action === "restore_merge") &&
        selectedConflict && (
          <MergeDiff
            conflict={selectedConflict}
            overwriteFields={decision.overwrite_fields ?? []}
            disabled={disabled}
            onChange={(overwriteFields) =>
              onDecision({
                ...decision,
                overwrite_fields: overwriteFields,
              })
            }
          />
        )}
    </article>
  );
}

function MappedFields({ candidate }: { candidate: CardImportCandidate }) {
  const t = useTranslations("writing.referenceCards.import");
  const fields = Object.entries(candidate.fields).filter(
    ([key, value]) => key !== "interop" && !isBlank(value),
  );
  return (
    <div className="mt-4 rounded-lg border border-border bg-background p-3">
      <p className="text-xs font-semibold text-foreground">
        {t("candidate.mappedFields")}
      </p>
      <dl className="mt-2 divide-y divide-border">
        {fields.map(([field, value]) => (
          <div
            key={field}
            className="grid gap-1 py-2 first:pt-0 last:pb-0 sm:grid-cols-[180px_minmax(0,1fr)]"
          >
            <dt className="break-all text-xs font-medium text-muted">{field}</dt>
            <dd className="min-w-0">
              <DiffValue value={value} />
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function MergeDiff({
  conflict,
  overwriteFields,
  disabled,
  onChange,
}: {
  conflict: CardImportConflict;
  overwriteFields: string[];
  disabled: boolean;
  onChange: (fields: string[]) => void;
}) {
  const t = useTranslations("writing.referenceCards.import");
  const tc = useTranslations("writing.referenceCards.curation");
  const diffs = Object.entries(conflict.field_diffs);
  return (
    <section className="mt-4 rounded-lg border border-border bg-background p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.12em] text-muted">
            {tc("mergeTarget")}
          </p>
          <p className="mt-1 break-all text-sm font-medium text-foreground">
            {t("merge.target", {
              match: conflict.match_kind,
              id: conflict.target_card_id,
            })}
          </p>
        </div>
        {conflict.is_deleted && (
          <span className="rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-800 dark:bg-amber-950 dark:text-amber-200">
            {t("merge.inTrash")}
          </span>
        )}
      </div>
      {diffs.length > 0 ? (
        <div className="mt-3 space-y-2">
          {diffs.map(([field, diff]) => {
            const checked = overwriteFields.includes(field);
            const mergesAutomatically = field === "tags";
            const fillsAutomatically = isBlank(diff.existing);
            const canOverwrite =
              !mergesAutomatically && !fillsAutomatically;
            return (
              <div
                key={field}
                className="rounded-lg border border-border bg-surface-secondary p-3"
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <p className="break-all text-xs font-semibold text-foreground">
                    {field}
                  </p>
                  {mergesAutomatically ? (
                    <span className="text-xs font-medium text-accent">
                      {t("merge.tagsCombined")}
                    </span>
                  ) : fillsAutomatically ? (
                    <span className="text-xs font-medium text-accent">
                      {t("merge.emptyFilled")}
                    </span>
                  ) : (
                    <label className="flex cursor-pointer items-center gap-2 text-xs font-medium text-foreground">
                      <input
                        type="checkbox"
                        checked={checked}
                        disabled={disabled}
                        onChange={(event) =>
                          onChange(
                            event.target.checked
                              ? [...overwriteFields, field]
                              : overwriteFields.filter(
                                  (item) => item !== field,
                                ),
                          )
                        }
                        className="h-4 w-4 accent-[var(--color-accent)]"
                      />
                      {t("merge.overwrite")}
                    </label>
                  )}
                </div>
                <div className="mt-2 grid gap-2 md:grid-cols-2">
                  <div className="rounded-md bg-background p-2.5">
                    <p className="text-[11px] font-semibold text-muted">
                      {tc("keepExisting")}
                    </p>
                    <div className="mt-1">
                      <DiffValue value={diff.existing} />
                    </div>
                  </div>
                  <div className="rounded-md bg-background p-2.5">
                    <p className="text-[11px] font-semibold text-muted">
                      {tc("useCandidate")}
                    </p>
                    <div className="mt-1">
                      <DiffValue value={diff.imported} />
                    </div>
                  </div>
                </div>
                {canOverwrite && !checked && (
                  <p className="mt-2 text-xs text-muted">
                    {t("merge.keepExistingHint")}
                  </p>
                )}
              </div>
            );
          })}
        </div>
      ) : (
        <p className="mt-2 text-xs text-muted">{tc("fillEmptyOnly")}</p>
      )}
    </section>
  );
}

function DiffValue({ value }: { value: unknown }) {
  const t = useTranslations("writing.referenceCards.import");
  const [expanded, setExpanded] = useState(false);
  const text = valueText(value);
  const truncated = text.length > VALUE_PREVIEW_CHARS;
  const visible =
    truncated && !expanded
      ? text.slice(0, VALUE_PREVIEW_CHARS)
      : text || t("merge.emptyValue");
  return (
    <div className="min-w-0">
      <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-words font-sans text-xs leading-5 text-foreground">
        {visible}
      </pre>
      {truncated && (
        <button
          type="button"
          onClick={() => setExpanded((current) => !current)}
          className="mt-1 text-xs font-semibold text-accent hover:underline"
        >
          {expanded ? t("merge.collapseValue") : t("merge.showFullValue")}
        </button>
      )}
    </div>
  );
}

function LabeledValues({
  label,
  values,
  className = "",
}: {
  label: string;
  values: string[];
  className?: string;
}) {
  return (
    <div className={className}>
      <p className="text-xs font-semibold text-foreground">{label}</p>
      <ul className="mt-2 flex flex-wrap gap-2">
        {values.map((value, index) => (
          <li
            key={`${value}:${index}`}
            className="break-all rounded-md bg-surface-secondary px-2 py-1 text-xs text-muted"
          >
            {value}
          </li>
        ))}
      </ul>
    </div>
  );
}

function InertBadge({ children }: { children: ReactNode }) {
  return (
    <span className="rounded-full bg-amber-200/70 px-2 py-0.5 text-[11px] font-semibold text-amber-900 dark:bg-amber-900 dark:text-amber-100">
      {children}
    </span>
  );
}
