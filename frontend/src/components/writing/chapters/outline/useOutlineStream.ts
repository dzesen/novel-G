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
  // 每当**流**送来一份新结果就 +1；调用方经 setResult 自己改内容时不动。
  // 消费方用它判断"手上这份是不是刚换的新货"——不能用"点了生成"来判断，
  // 因为生成可能被取消或失败，那时屏幕上留着的仍是旧的那一份（见 start 里
  // 刻意不清 result 的注释）。
  const [resultVersion, setResultVersion] = useState(0);

  const acceptStreamResult = useCallback((next: T) => {
    setResult(next);
    setResultVersion((current) => current + 1);
  }, []);

  const abortRef = useRef<AbortController | null>(null);
  // 单调递增的"第几轮"标记：只被 start() 推进，cancel() 不动它。
  // 用它而不是直接比 abortRef.current，是因为 cancel() 会把 abortRef.current
  // 置 null——单纯取消而不重启的这一轮，此时 abortRef.current 和"被下一轮取代"
  // 长得一模一样（都是与 controller 不相等），但前者仍要正常收到 idle，
  // 后者才必须被挡住。runIdRef 只在真正开新一轮时前进，能把两者分开。
  const runIdRef = useRef(0);

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
      const runId = ++runIdRef.current;

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
            // abort() 是异步的：调用时已经排队的 chunk 仍会触发这个回调。
            // 若 runIdRef 已经前进到更新的一轮，说明本轮已被取代，不能再写
            // result/contextReport/droppedIds 等状态覆盖新一轮的结果。
            if (runIdRef.current !== runId) return;

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
                acceptStreamResult(data.data as T);
              }
              return;
            }

            if (event === "done") {
              if (data.success) {
                const bag = data.result as Record<string, unknown> | undefined;
                const produced = bag?.[stepKey];
                if (produced) acceptStreamResult(produced as T);
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
        // abort() 不会同步中断挂起的 await：旧一轮的 reader.read() 会在
        // 稍后的微任务里才 reject。若 runIdRef 已经前进到更新的一轮，说明本轮
        // 已被取代，任何状态写入（包括下面的 idle/error）都必须跳过，否则会把
        // 已经 done 的新一轮结果又拨回 idle/error。
        //
        // 这里特意不用 `abortRef.current !== controller` 来判断：cancel() 会把
        // abortRef.current 置 null，单纯取消不重启的这一轮和"被下一轮取代"在
        // abortRef.current 上长得一样，会把前者也错误地挡住，导致面板上直接绑
        // stream.cancel 的取消按钮永远卡在 running（见 Task 4 VolumeOutlinePanel
        // 的取消按钮）。runIdRef 只在 start() 里前进，cancel() 不动它，才能把
        // "单纯取消" 和 "被取代" 分开。
        if (runIdRef.current !== runId) return;

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
    [acceptStreamResult, cancel, path, stepKey]
  );

  return {
    status,
    result,
    resultVersion,
    contextReport,
    droppedIds,
    error,
    start,
    cancel,
    reset,
    setResult,
  };
}
