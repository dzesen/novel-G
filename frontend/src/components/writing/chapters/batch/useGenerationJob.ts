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
  const progressLenRef = useRef<number>(0);
  const onProgressRef = useRef(onProgress);
  // 用 effect 而非 render 期赋值，避免 React Compiler 把 render 期 ref 变更视作副作用。
  useEffect(() => {
    onProgressRef.current = onProgress;
  }, [onProgress]);

  // 外部落地一份作业（发现/启动/控制返回）；同步 refs 供轮询比较。
  const setJob = useCallback((next: GenerationJob | null) => {
    jobIdRef.current = next?._id ?? null;
    progressLenRef.current = next?.progress.length ?? 0;
    setError(null);
    setJobState(next);
  }, []);

  const refetch = useCallback(async () => {
    const id = jobIdRef.current;
    if (!id) return;
    try {
      const next = await apiGet<GenerationJob>(`/api/generation-jobs/${id}`);
      setError(null);
      if (next.progress.length > progressLenRef.current) {
        progressLenRef.current = next.progress.length;
        onProgressRef.current?.(next);
      }
      setJobState(next);
    } catch (err) {
      // 保面板，仅记错误；下一拍继续（设计 §5.2）。
      setError(err instanceof Error ? err.message : String(err));
    }
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
