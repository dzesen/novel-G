"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { apiGet } from "@/lib/api";
import { type GenerationJob, isActive } from "./batchTypes";

interface UseGenerationJobArgs {
  onProgress?: (job: GenerationJob) => void;
}

interface UseGenerationJobResult {
  job: GenerationJob | null;
  error: string | null;
  setJob: (job: GenerationJob | null) => void;
  refetch: () => Promise<void>;
}

const POLL_MS = 3000;

export function useGenerationJob({ onProgress }: UseGenerationJobArgs = {}): UseGenerationJobResult {
  const [job, setJobState] = useState<GenerationJob | null>(null);
  const [error, setError] = useState<string | null>(null);

  const jobIdRef = useRef<string | null>(null);
  const requestGenerationRef = useRef(0);
  const inFlightRef = useRef<{
    controller: AbortController;
    promise: Promise<void>;
  } | null>(null);
  const progressLenRef = useRef<number>(0);
  const onProgressRef = useRef(onProgress);
  // 用 effect 而非 render 期赋值，避免 React Compiler 把 render 期 ref 变更视作副作用。
  useEffect(() => {
    onProgressRef.current = onProgress;
  }, [onProgress]);

  const invalidateRequest = useCallback(() => {
    requestGenerationRef.current += 1;
    inFlightRef.current?.controller.abort();
    inFlightRef.current = null;
  }, []);

  useEffect(() => () => {
    invalidateRequest();
    jobIdRef.current = null;
  }, [invalidateRequest]);

  // Discovery and control responses supersede polls even for the same job ID.
  const setJob = useCallback((next: GenerationJob | null) => {
    invalidateRequest();
    jobIdRef.current = next?._id ?? null;
    progressLenRef.current = next?.progress.length ?? 0;
    setError(null);
    setJobState(next);
  }, [invalidateRequest]);

  const refetch = useCallback(async () => {
    const id = jobIdRef.current;
    if (!id) return;
    if (inFlightRef.current) return inFlightRef.current.promise;
    const generation = requestGenerationRef.current;
    const controller = new AbortController();
    const request = { controller, promise: Promise.resolve() };
    inFlightRef.current = request;
    const isCurrent = () => !controller.signal.aborted
      && jobIdRef.current === id
      && requestGenerationRef.current === generation;
    request.promise = (async () => {
      try {
        const next = await apiGet<GenerationJob>(`/api/generation-jobs/${encodeURIComponent(id)}`, {
          signal: controller.signal,
        });
        if (!isCurrent() || next._id !== id) return;
        setError(null);
        if (next.progress.length > progressLenRef.current) {
          progressLenRef.current = next.progress.length;
          onProgressRef.current?.(next);
        }
        if (isCurrent()) setJobState(next);
      } catch (err) {
        // Retain the panel, but never attach an obsolete request's error to it.
        if (isCurrent()) setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (inFlightRef.current === request) inFlightRef.current = null;
      }
    })();
    return request.promise;
  }, []);

  // 仅 active（running/pending）时开定时器；转 paused/终态即停。
  // job 每拍变新对象会重建定时器——3s 粒度下无害，且天然在状态转出 active 时收手。
  useEffect(() => {
    if (!job || !isActive(job.status)) return;
    const timer = window.setInterval(() => { void refetch(); }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [job, refetch]);

  return { job, error, setJob, refetch };
}
