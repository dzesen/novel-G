"use client";

import type { ReactNode } from "react";
import { useTranslations } from "next-intl";
import type { ContextReport } from "./outlineTypes";

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="grid gap-1 text-sm">
      <span className="text-xs font-medium text-muted">{label}</span>
      {children}
    </label>
  );
}

export function Notice({ tone, children }: { tone: "warning" | "error"; children: ReactNode }) {
  const className =
    tone === "warning"
      ? "mb-3 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800 dark:border-amber-900/60 dark:bg-amber-950/40 dark:text-amber-200"
      : "mb-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900/60 dark:bg-red-950/40 dark:text-red-300";
  return <div className={className}>{children}</div>;
}

/**
 * 截断上报（设计 §7.1）。
 *
 * **两个字段都渲染**：只显示 truncated_sections 会让"段名没出现在里面"被读成
 * "这段是完整的"——那正是阶段 1 整分支评审在 minor_cards 上抓到的假象。
 */
export function ContextNotices({ report }: { report: ContextReport | null }) {
  const t = useTranslations("writing.outline");
  if (!report) return null;

  const droppedEntries = Object.entries(report.dropped_item_counts);

  return (
    <>
      {report.truncated_sections.length > 0 && (
        <Notice tone="warning">
          {t("truncationWarning", { sections: report.truncated_sections.join("、") })}
        </Notice>
      )}
      {droppedEntries.length > 0 && (
        <Notice tone="warning">
          {t("droppedItems", {
            detail: droppedEntries.map(([section, count]) => `${section} ${count}`).join("、"),
          })}
        </Notice>
      )}
    </>
  );
}
