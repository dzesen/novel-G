"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { apiPostSSE } from "@/lib/api";
import type { ContextReport } from "../outline/outlineTypes";

/**
 * `cancelled` 与 `error` 是**两种不同的终态**：主动取消不是失败，
 * 但也不是 `idle`——面板要据此把已生成的半章标成"未完成"并仍然允许接受
 * （设计 §2「取消后的部分结果」）。细纲那条链没有这个区分，因为它的部分
 * 结果没有意义；正文有。
 */
export type ProseStreamStatus = "idle" | "running" | "done" | "error" | "cancelled";

export interface ProseUsage {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
}

/**
 * 正文流的 SSE 事件机。
 *
 * **不复用 useOutlineStream**：帧形状完全不同——这里是 delta 累加，那里是
 * step/done 整体替换。但 runIdRef 单调陈旧守卫那套手法照搬，它修的是
 * 2a Task 3 那个 Critical：abort() 不同步中断挂起的 await，被取代的旧一轮
 * 会在稍后 reject 并把新一轮的状态写坏。
 */
export function useProseStream() {
  const [status, setStatus] = useState<ProseStreamStatus>("idle");
  const [text, setText] = useState("");
  const [contextReport, setContextReport] = useState<ContextReport | null>(null);
  const [error, setError] = useState("");
  const [usage, setUsage] = useState<ProseUsage | null>(null);

  const abortRef = useRef<AbortController | null>(null);
  // 单调递增的"第几轮"：只被 start() 推进，cancel() 不动它。
  // 直接比 abortRef.current 不行——cancel() 会把它置 null，于是"单纯取消"
  // 和"被下一轮取代"长得一模一样，而前者必须正常收到终态、后者必须被挡住。
  const runIdRef = useRef(0);
  // 累加缓冲区。用 ref 而非直接 setText(prev => prev + chunk)，是为了让
  // done 帧能拿全文一次性覆盖，同时避免每个 chunk 都读一次旧 state。
  const bufferRef = useRef("");

  const cancel = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
  }, []);

  // 面板卸载必须断流，否则后端会继续烧 token 直到 LLM 调用自然结束。
  useEffect(() => cancel, [cancel]);

  const reset = useCallback(() => {
    cancel();
    // reset() 和 cancel() 都会 abort，但 reset() 还要同步把 status 定成
    // "idle"——这一步 cancel() 自己不做。问题在于 abort() 不同步：旧一轮的
    // reader.read() 会在稍后的微任务里 reject，跑进 start() 的 catch，
    // 那里的守卫只挡"被新一轮取代"（runIdRef 前进），不挡"被 reset() 收场"。
    // 于是 catch 会在 reset() 刚设完 idle 之后，把 status 又覆盖成
    // cancelled/error，且 text 已经被这里清空——面板就会显示一个"可接受的
    // 未完成章节"，但其实内容是空的。
    // 因此 reset() 也要推进 runIdRef，让那个迟到的 catch 认出自己已经作废。
    // 注意 cancel() 本身依然不能推进它：面板上单纯点"取消"要正常收到
    // cancelled 终态（保留半章可接受），只有 reset() 这种"连状态一起清空
    // 重来"的收场才需要拦住迟到的回调。把这两者的 runIdRef 行为合并，
    // 会让单纯取消也收不到终态，面板的取消按钮会卡死在 running。
    runIdRef.current += 1;
    setStatus("idle");
    setText("");
    setContextReport(null);
    setError("");
    setUsage(null);
    bufferRef.current = "";
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
      setUsage(null);
      // 新一轮从空白开始：正文与细纲不同，把新章的 token 追加在旧章后面
      // 会拼出一段没人想要的东西。
      setText("");
      bufferRef.current = "";

      try {
        // 本 hook 只服务一个端点（不像 useOutlineStream 要服务两条链），
        // 故路径写死，不做成选项。
        await apiPostSSE(
          "/api/llm/write-chapter-by-ai",
          payload,
          (event, data) => {
            // abort() 是异步的：调用时已排队的 chunk 仍会触发本回调。
            // runIdRef 已前进说明本轮被取代，一律不许写状态。
            if (runIdRef.current !== runId) return;

            if (event === "context") {
              setContextReport({
                truncated_sections: (data.truncated_sections as string[]) ?? [],
                dropped_item_counts: (data.dropped_item_counts as Record<string, number>) ?? {},
              });
              return;
            }

            if (event === "delta") {
              const chunk = typeof data.text === "string" ? data.text : "";
              if (!chunk) return;
              bufferRef.current += chunk;
              setText(bufferRef.current);
              return;
            }

            if (event === "done") {
              if (data.success) {
                // done 带全文：用它覆盖累加结果，累加逻辑因此不再是唯一真相源
                // （设计 §4）。注意这只覆盖成功路径——取消/失败时没有这一帧，
                // 那半章仍然只有 bufferRef 这一份（设计 §7.3）。
                if (typeof data.text === "string") {
                  bufferRef.current = data.text;
                  setText(data.text);
                }
                if (data.usage && typeof data.usage === "object") {
                  setUsage(data.usage as ProseUsage);
                }
                setStatus("done");
                return;
              } else {
                setError(typeof data.error === "string" ? data.error : "生成失败");
                setStatus("error");
              }
            }
          },
          controller.signal
        );
      } catch (err) {
        if (runIdRef.current !== runId) return;

        // 主动取消不是错误，但也不能归 idle：已生成的半章要留着并可接受。
        if (err instanceof DOMException && err.name === "AbortError") {
          setStatus("cancelled");
          return;
        }
        setError(err instanceof Error ? err.message : String(err));
        setStatus("error");
      } finally {
        if (abortRef.current === controller) abortRef.current = null;
      }
    },
    [cancel]
  );

  return { status, text, contextReport, error, usage, start, cancel, reset };
}
