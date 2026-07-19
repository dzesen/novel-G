"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@heroui/react";
import { useProseStream } from "./useProseStream";
import OutlineGenerationParams, {
  EMPTY_GENERATION_PARAMS,
  toRequestParams,
  type GenerationParams,
} from "../outline/OutlineGenerationParams";
import { ContextNotices, Notice } from "../outline/outlineUi";
import { countChapterWords } from "../chapterUtils";

interface ProsePanelProps {
  novelId: string;
  chapterId: string;
  /** 编辑器里当前正文是否非空。为真时接受需要二次确认（设计 §2）。 */
  hasExistingContent: boolean;
  onClose: () => void;
  onAccepted: (text: string) => void;
}

export default function ProsePanel({
  novelId,
  chapterId,
  hasExistingContent,
  onClose,
  onAccepted,
}: ProsePanelProps) {
  const t = useTranslations("writing.prose");
  const stream = useProseStream();
  const [params, setParams] = useState<GenerationParams>(EMPTY_GENERATION_PARAMS);
  const [overwriteArmed, setOverwriteArmed] = useState(false);

  const running = stream.status === "running";
  const hasText = stream.text.length > 0;
  // 取消或出错后留在屏幕上的那半章：明确标注，但照常允许接受——
  // 已流出的 token 钱已经花了，"AI 开个头、人续写"是真实用法（设计 §2）。
  const incomplete = hasText && (stream.status === "cancelled" || stream.status === "error");

  const accept = () => {
    if (!hasText || running) return;
    if (hasExistingContent && !overwriteArmed) {
      setOverwriteArmed(true);
      return;
    }
    onAccepted(stream.text);
    onClose();
  };

  const startGeneration = () => {
    // 保险栓在每次重新生成时复位：上一份预览已被新的一轮取代，
    // 针对它的确认不该延续到下一份（2a Task 7 就栽在栓不复位上）。
    setOverwriteArmed(false);
    void stream.start({
      novel_id: novelId,
      chapter_id: chapterId,
      ...toRequestParams(params),
    });
  };

  const discard = () => {
    // 保险栓是面板本地状态，不属于 stream，stream.reset() 清不到它——
    // 两边要一起复位，否则会同屏出现"空状态提示"与"覆盖警告"互相矛盾的界面。
    stream.reset();
    setOverwriteArmed(false);
  };

  return (
    <div className="absolute inset-0 z-30 flex items-center justify-center bg-black/25 px-4 py-6">
      <div className="flex max-h-full w-full max-w-5xl flex-col rounded-md border border-border bg-surface shadow-lg">
        <header className="flex items-start justify-between gap-3 border-b border-border px-5 py-4">
          <div className="min-w-0">
            <h3 className="text-base font-semibold text-foreground">{t("title")}</h3>
            <p className="mt-1 text-xs leading-5 text-muted">{t("description")}</p>
          </div>
          <div className="flex shrink-0 gap-2">
            {running ? (
              <Button variant="outline" size="sm" onPress={stream.cancel}>
                {t("cancel")}
              </Button>
            ) : (
              <Button
                variant="primary"
                size="sm"
                className="bg-accent text-white hover:bg-accent-hover"
                onPress={startGeneration}
              >
                {hasText ? t("regenerate") : t("generate")}
              </Button>
            )}
            <Button variant="ghost" size="sm" onPress={onClose}>
              {t("close")}
            </Button>
          </div>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          <div className="mb-4">
            <OutlineGenerationParams value={params} onChange={setParams} />
          </div>

          <ContextNotices report={stream.contextReport} />
          {stream.error && <Notice tone="error">{stream.error}</Notice>}
          {incomplete && <Notice tone="warning">{t("incomplete")}</Notice>}
          {overwriteArmed && <Notice tone="warning">{t("overwriteArm")}</Notice>}

          {!hasText && running && (
            <p className="py-10 text-center text-sm text-muted">{t("generating")}</p>
          )}
          {!hasText && !running && (
            <p className="py-10 text-center text-sm text-muted">{t("emptyPreview")}</p>
          )}

          {hasText && (
            <>
              <div className="mb-3 flex flex-wrap items-center gap-2">
                <span className="rounded-md border border-border bg-background px-2 py-1 text-xs tabular-nums text-muted">
                  {t("charCount", { count: countChapterWords(stream.text) })}
                </span>
                {/*
                  用量只在成功的 done 帧里到货；取消或失败时根本没有那一帧，
                  usage 保持 null、这里什么都不渲染。设计 §7.1 要求"如实报 0 或不报，
                  绝不编造估算值"——不渲染正是"不报"。
                */}
                {stream.usage && (
                  <span className="rounded-md border border-border bg-background px-2 py-1 text-xs tabular-nums text-muted">
                    {t("tokenUsage", {
                      total: stream.usage.total_tokens,
                      input: stream.usage.input_tokens,
                      output: stream.usage.output_tokens,
                    })}
                  </span>
                )}
              </div>
              {/*
                预览区刻意**只读**：流式写入与人工编辑并存必然打架。
                要改就先接受、改在编辑器里——那才是编辑正文的地方（设计 §3.2）。
              */}
              <div className="whitespace-pre-wrap rounded-md border border-border bg-background px-4 py-3 text-[15px] leading-8 text-foreground">
                {stream.text}
              </div>
            </>
          )}
        </div>

        <footer className="flex justify-end gap-2 border-t border-border px-5 py-3">
          <Button variant="ghost" size="sm" onPress={discard} isDisabled={!hasText || running}>
            {t("discard")}
          </Button>
          <Button
            variant="primary"
            size="sm"
            className="bg-accent text-white hover:bg-accent-hover"
            onPress={accept}
            isDisabled={!hasText || running}
          >
            {overwriteArmed ? t("overwriteConfirm") : t("accept")}
          </Button>
        </footer>
      </div>
    </div>
  );
}
