import type { ProseStreamStatus } from "./useProseStream";

interface ProseRunActionGateInput {
  restoreLoading: boolean;
  streamStatus: ProseStreamStatus;
  conflictRequiresSync: boolean;
  mutationPending: boolean;
}

export function proseRunActionsBlocked({
  restoreLoading,
  streamStatus,
  conflictRequiresSync,
  mutationPending,
}: ProseRunActionGateInput): boolean {
  return restoreLoading
    || streamStatus === "cancelled"
    || conflictRequiresSync
    || mutationPending;
}

interface ProseDiscardPayloadInput {
  novelId: string;
  chapterId: string;
  runRevision: number;
}

export function buildProseDiscardPayload({
  novelId,
  chapterId,
  runRevision,
}: ProseDiscardPayloadInput) {
  return {
    novel_id: novelId,
    chapter_id: chapterId,
    expected_run_revision: runRevision,
  };
}

type ProseRunMutationResult<TValue, TCurrent> =
  | { status: "success"; value: TValue }
  | { status: "conflict_refreshed"; current: TCurrent }
  | { status: "conflict_refresh_failed"; error: unknown };

export function proseRunConfirmationResetRequired(
  status: ProseRunMutationResult<unknown, unknown>["status"],
): boolean {
  return status === "conflict_refreshed";
}

function isConflict(error: unknown): boolean {
  return Boolean(
    error
    && typeof error === "object"
    && "status" in error
    && (error as { status?: unknown }).status === 409,
  );
}

/**
 * Submit once. A revision conflict may refresh once, but it never retries the
 * mutation because the refreshed state still needs an explicit user decision.
 */
export async function submitProseRunMutation<TValue, TCurrent>({
  mutate,
  refresh,
}: {
  mutate: () => Promise<TValue>;
  refresh: () => Promise<TCurrent>;
}): Promise<ProseRunMutationResult<TValue, TCurrent>> {
  try {
    return { status: "success", value: await mutate() };
  } catch (error) {
    if (!isConflict(error)) throw error;
    try {
      return { status: "conflict_refreshed", current: await refresh() };
    } catch (refreshError) {
      return { status: "conflict_refresh_failed", error: refreshError };
    }
  }
}
