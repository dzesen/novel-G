export interface ProseCompletionSummary {
  status: "complete" | "degraded" | "incomplete" | "stale";
  can_write_formal_prose: boolean;
}

export function proseRequiresPartialAcknowledgement(
  completion: Pick<ProseCompletionSummary, "status" | "can_write_formal_prose">,
): boolean {
  return !completion.can_write_formal_prose || completion.status === "incomplete";
}

export function proseRunHasUncertainAttempt(
  run: { segments?: Array<{ status?: string }> },
): boolean {
  return (run.segments ?? []).some((segment) => segment.status === "uncertain");
}

interface AcceptPayloadInput {
  novelId: string;
  chapterId: string;
  runId: string;
  runRevision: number;
  partial: boolean;
}

export function buildProseAcceptPayload({
  novelId,
  chapterId,
  runId,
  runRevision,
  partial,
}: AcceptPayloadInput) {
  if (!runId.trim()) throw new Error("runId is required");
  return {
    novel_id: novelId,
    chapter_id: chapterId,
    expected_run_revision: runRevision,
    accept_partial: partial,
    partial_acknowledgement: partial,
  };
}
