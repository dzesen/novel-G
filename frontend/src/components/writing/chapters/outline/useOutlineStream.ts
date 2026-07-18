"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { apiPostSSE } from "@/lib/api";
import type { ContextReport, DroppedIds } from "./outlineTypes";

export type OutlineStreamStatus = "idle" | "running" | "done" | "error";

interface UseOutlineStreamOptions {
  /** SSE 端点路径，如 "/api/llm/create-volume-outline-by-ai"。 */
  path: string;
  /** 工作流步骤名，如 "volume_outline" / "chapter_outline"。 */
  stepKey: string;
}

/**
 * 两条细纲链共用的 SSE 事件机。
 *
 * 处理 6 类帧：step running / step done / step error / done / context /
 * id_validation。keepalive 注释帧由 apiPostSSE 自动忽略（无 data: 行）。
 */
export function useOutlineStream<T>({ path, stepKey }: UseOutlineStreamOptions) {
  const [status, setStatus] = useState<OutlineStreamStatus>("idle");
  const [result, setResult] = useState<T | null>(null);
  const [contextReport, setContextReport] = useState<ContextReport | null>(null);
  const [droppedIds, setDroppedIds] = useState<DroppedIds | null>(null);
  const [error, setError] = useState("");

  const abortRef = useRef<AbortController | null>(null);

  const cancel = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
  }, []);

  // 面板卸载时必须断流，否则后端会继续烧 token 直到 LLM 调用自然结束。
  useEffect(() => cancel, [cancel]);

  const reset = useCallback(() => {
    cancel();
    setStatus("idle");
    setResult(null);
    setContextReport(null);
    setDroppedIds(null);
    setError("");
  }, [cancel]);

  const start = useCallback(
    async (payload: Record<string, unknown>) => {
      cancel();
      const controller = new AbortController();
      abortRef.current = controller;

      setStatus("running");
      setError("");
      setContextReport(null);
      setDroppedIds(null);
      // 刻意不清空 result：流中途失败时保留上一次的预览，
      // 清空等于让用户白等一场（设计 §8）。

      try {
        await apiPostSSE(
          path,
          payload,
          (event, data) => {
            if (event === "context") {
              // 设计 §7.1：截断在 LLM 调用之前就已知，必须**立刻**渲染。
              // 攒到结束才显示，等于把后端专门前置这一帧的设计抵消掉。
              setContextReport({
                truncated_sections: (data.truncated_sections as string[]) ?? [],
                dropped_item_counts: (data.dropped_item_counts as Record<string, number>) ?? {},
              });
              return;
            }

            if (event === "id_validation") {
              // 设计 §7.2：不上报的话，"AI 漏了个人物"会以"预览里少一行"无声通过。
              setDroppedIds((data.dropped as DroppedIds) ?? {});
              return;
            }

            if (event === "step") {
              if (data.step !== stepKey) return;
              if (data.status === "error") {
                setError(typeof data.error === "string" ? data.error : "生成失败");
                setStatus("error");
              } else if (data.status === "done" && data.data) {
                setResult(data.data as T);
              }
              return;
            }

            if (event === "done") {
              if (data.success) {
                const bag = data.result as Record<string, unknown> | undefined;
                const produced = bag?.[stepKey];
                if (produced) setResult(produced as T);
                setStatus("done");
              } else {
                const failed = typeof data.failed_step === "string" ? data.failed_step : stepKey;
                setError((current) => current || `步骤 ${failed} 失败`);
                setStatus("error");
              }
            }
          },
          controller.signal
        );
      } catch (err) {
        // 主动取消不是错误：AbortError 只把状态收回 idle。
        if (err instanceof DOMException && err.name === "AbortError") {
          setStatus("idle");
          return;
        }
        setError(err instanceof Error ? err.message : String(err));
        setStatus("error");
      } finally {
        if (abortRef.current === controller) abortRef.current = null;
      }
    },
    [cancel, path, stepKey]
  );

  return { status, result, contextReport, droppedIds, error, start, cancel, reset, setResult };
}
