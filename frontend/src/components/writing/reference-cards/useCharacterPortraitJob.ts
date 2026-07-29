"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { apiGet, apiPost } from "@/lib/api";
import type { CharacterPortraitJob } from "@/types/image";

const POLL_INTERVAL_MS = 1_000;

interface UseCharacterPortraitJobOptions {
  novelId: string;
  cardId: string;
}

export function useCharacterPortraitJob({
  novelId,
  cardId,
}: UseCharacterPortraitJobOptions) {
  const [job, setJob] = useState<CharacterPortraitJob | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const [pollGeneration, setPollGeneration] = useState(0);
  const requestSequence = useRef(0);
  const cancellingRef = useRef(false);

  const jobBase = `/api/reference-cards/novel/${novelId}/character/${cardId}/portrait/jobs`;
  const jobId = job?.job_id ?? null;
  const jobShouldPoll = Boolean(
    job &&
      !job.terminal &&
      !(
        job.cleanup_pending &&
        job.failure &&
        !job.failure.retryable
      ),
  );

  const adoptJob = useCallback((next: CharacterPortraitJob | null) => {
    requestSequence.current += 1;
    setPollError(null);
    setJob(next);
    setPollGeneration((current) => current + 1);
  }, []);

  useEffect(() => {
    if (!jobId || !jobShouldPoll || cancelling) return;

    let active = true;
    let timeoutId: number | null = null;

    const schedule = () => {
      timeoutId = window.setTimeout(async () => {
        if (cancellingRef.current) return;
        const sequence = ++requestSequence.current;
        try {
          const next = await apiGet<CharacterPortraitJob>(
            `${jobBase}/${jobId}`,
          );
          if (!active || sequence !== requestSequence.current) return;
          setJob(next);
          setPollError(null);
          const shouldContinue =
            !next.terminal &&
            !(
              next.cleanup_pending &&
              next.failure &&
              !next.failure.retryable
            );
          if (shouldContinue) schedule();
        } catch (reason) {
          if (!active || sequence !== requestSequence.current) return;
          setPollError(
            reason instanceof Error ? reason.message : "poll_failed",
          );
          schedule();
        }
      }, POLL_INTERVAL_MS);
    };

    schedule();
    return () => {
      active = false;
      if (timeoutId !== null) window.clearTimeout(timeoutId);
    };
  }, [cancelling, jobBase, jobId, jobShouldPoll, pollGeneration]);

  const cancel = useCallback(async () => {
    if (!job || job.terminal || cancelling) return;
    const sequence = ++requestSequence.current;
    cancellingRef.current = true;
    setCancelling(true);
    setPollError(null);
    try {
      const next = await apiPost<CharacterPortraitJob>(
        `${jobBase}/${job.job_id}/cancel`,
        {},
      );
      if (sequence === requestSequence.current) {
        setJob(next);
      }
    } catch (reason) {
      if (sequence === requestSequence.current) {
        setPollError(
          reason instanceof Error ? reason.message : "cancel_failed",
        );
      }
    } finally {
      cancellingRef.current = false;
      setCancelling(false);
      setPollGeneration((current) => current + 1);
    }
  }, [cancelling, job, jobBase]);

  return {
    job,
    pollError,
    cancelling,
    adoptJob,
    cancel,
  };
}
