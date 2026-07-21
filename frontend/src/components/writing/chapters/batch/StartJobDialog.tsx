"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@heroui/react";
import { apiPost } from "@/lib/api";
import type { GenerationJob } from "./batchTypes";

interface StartJobDialogProps {
  scope: "volume" | "book";
  targetId: string;         // volume: volume_id；book: novel_id
  title: string;            // 对话框标题（调用方按 scope 解析好的 i18n 文案）
  targetHeading: string;    // 目标区小标题（"目标卷" / "目标"）
  targetLabel: string;      // 目标展示名（卷名 / "全书"）
  fillableCount: number;
  onSubmitted: (job: GenerationJob) => void;
  onClose: () => void;
}

export default function StartJobDialog({
  scope,
  targetId,
  title,
  targetHeading,
  targetLabel,
  fillableCount,
  onSubmitted,
  onClose,
}: StartJobDialogProps) {
  const t = useTranslations("writing.batch");
  const [checkpointInterval, setCheckpointInterval] = useState(5);
  const [tokenBudget, setTokenBudget] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  const submit = async () => {
    setSubmitting(true);
    setError("");
    try {
      // 客户端夹到后端约束区间，避免越界值触发 422（其 detail 是数组、原样展示会成 [object Object]）。
      // 后端：checkpoint_interval ge=1 le=1000；token_budget ge=1（空=不限）。
      const parsedBudget = Number(tokenBudget);
      const budget =
        tokenBudget.trim() && Number.isFinite(parsedBudget) && parsedBudget >= 1
          ? Math.floor(parsedBudget)
          : null;
      const job = await apiPost<GenerationJob>(`/api/generation-jobs/${scope}/${targetId}`, {
        checkpoint_interval: Math.min(1000, Math.max(1, Math.floor(checkpointInterval) || 1)),
        token_budget: budget,
      });
      onSubmitted(job);
    } catch (err) {
      // 409（已有在跑作业）/400（无可填章）原样展示后端 detail（设计 §4.3）。
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="absolute inset-0 z-30 flex items-center justify-center bg-black/25 px-4 py-6">
      <div className="flex w-full max-w-md flex-col rounded-md border border-border bg-surface shadow-lg">
        <header className="border-b border-border px-5 py-4">
          <h3 className="text-base font-semibold text-foreground">{title}</h3>
        </header>

        <div className="grid gap-4 px-5 py-4">
          <div className="grid gap-1 text-sm">
            <span className="text-xs font-medium text-muted">{targetHeading}</span>
            <div className="rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground">
              <span className="font-medium">{targetLabel}</span>
              <span className="ml-2 text-xs text-muted">{t("dialogFillable", { count: fillableCount })}</span>
            </div>
          </div>

          <label className="grid gap-1 text-sm">
            <span className="text-xs font-medium text-muted">{t("dialogCheckpointLabel")}</span>
            <input
              type="number"
              min={1}
              max={1000}
              value={checkpointInterval}
              onChange={(e) => setCheckpointInterval(Number(e.target.value))}
              className="min-h-9 w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground outline-none focus:border-accent"
            />
            <span className="text-xs text-muted">{t("dialogCheckpointHint")}</span>
          </label>

          <label className="grid gap-1 text-sm">
            <span className="text-xs font-medium text-muted">{t("dialogTokenLabel")}</span>
            <input
              type="number"
              min={1}
              value={tokenBudget}
              onChange={(e) => setTokenBudget(e.target.value)}
              placeholder={t("dialogTokenPlaceholder")}
              className="min-h-9 w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground outline-none focus:border-accent"
            />
            <span className="text-xs text-muted">{t("dialogTokenHint")}</span>
          </label>

          {error && (
            <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900/60 dark:bg-red-950/40 dark:text-red-300">
              {error}
            </div>
          )}
        </div>

        <footer className="flex justify-end gap-2 border-t border-border px-5 py-3">
          <Button variant="ghost" size="sm" onPress={onClose} isDisabled={submitting}>
            {t("dialogCancel")}
          </Button>
          <Button
            variant="primary"
            size="sm"
            className="bg-accent text-white hover:bg-accent-hover"
            onPress={() => void submit()}
            isDisabled={submitting || fillableCount === 0}
          >
            {submitting ? t("dialogStarting") : t("dialogStart")}
          </Button>
        </footer>
      </div>
    </div>
  );
}
