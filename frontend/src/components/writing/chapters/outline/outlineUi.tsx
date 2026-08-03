"use client";

import type { ReactNode } from "react";
import { useTranslations } from "next-intl";
import {
  contextCountsForDisplay,
  contextSectionKind,
  referenceCleanupForDisplay,
  referenceRemapForDisplay,
} from "../../generationMetadataPresentation";
import type {
  ContextReport,
  DroppedIds,
  RemappedReference,
} from "./outlineTypes";

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="grid gap-1 text-sm">
      <span className="text-xs font-medium text-muted">{label}</span>
      {children}
    </label>
  );
}

export function Notice({
  tone,
  children,
}: {
  tone: "info" | "warning" | "error";
  children: ReactNode;
}) {
  const className =
    tone === "info"
      ? "mb-3 rounded-md border border-blue-200 bg-blue-50 px-3 py-2 text-sm text-blue-800 dark:border-blue-900/60 dark:bg-blue-950/40 dark:text-blue-200"
      : tone === "warning"
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
  const t = useTranslations("writing.generationMetadata");
  if (!report) return null;

  const truncatedSections = Array.from(new Set(
    report.truncated_sections.map(contextSectionKind),
  ));
  const droppedEntries = Object.entries(
    contextCountsForDisplay(report.dropped_item_counts),
  );

  return (
    <>
      {truncatedSections.length > 0 && (
        <Notice tone="warning">
          {t("contextTruncated", {
            step: t("steps.job"),
            sections: truncatedSections
              .map((section) => t(`contextSections.${section}`))
              .join(t("listSeparator")),
          })}
        </Notice>
      )}
      {droppedEntries.length > 0 && (
        <Notice tone="warning">
          {t("contextReduced", {
            step: t("steps.job"),
            detail: droppedEntries
              .map(([section, count]) => t("contextItemCount", {
                section: t(`contextSections.${section}`),
                count,
              }))
              .join(t("listSeparator")),
          })}
        </Notice>
      )}
    </>
  );
}

export function ReferenceCleanupNotice({
  droppedIds,
}: {
  droppedIds: DroppedIds | null;
}) {
  const t = useTranslations("writing.generationMetadata");
  const groups = referenceCleanupForDisplay(droppedIds);
  if (groups.length === 0) return null;
  return (
    <Notice tone="warning">
      <div className="grid gap-1.5">
        <p>{t("referenceCleanupIntro")}</p>
        {groups.map((group) => (
          <div key={group.kind}>
            <span className="font-medium">
              {t("referenceCleanupCount", {
                count: group.count,
                kind: t(`referenceKinds.${group.kind}`),
              })}
            </span>
            <span className="ml-1">
              {group.readableValues.length > 0
                ? t("referenceCleanupNames", {
                    names: group.readableValues.join(t("listSeparator")),
                  })
                : t("referenceCleanupOpaque")}
            </span>
          </div>
        ))}
      </div>
    </Notice>
  );
}

export function ReferenceRemapNotice({
  remappedReferences,
  nameById,
}: {
  remappedReferences: RemappedReference[];
  nameById?: Record<string, string>;
}) {
  const t = useTranslations("writing.generationMetadata");
  if (remappedReferences.length === 0) return null;
  return (
    <Notice tone="info">
      <div className="grid gap-1">
        <p>{t("referenceRemapIntro", { count: remappedReferences.length })}</p>
        {remappedReferences.map((item, index) => {
          const display = referenceRemapForDisplay(item, nameById);
          const method = t(`matchMethods.${display.matchedBy}`);
          const kind = t(`referenceKinds.${display.kind}`);
          return (
            <div key={`${display.source ?? "reference"}-${index}`}>
              {display.source
                ? display.targetName
                  ? t("referenceRemapNamed", {
                      source: display.source,
                      method,
                      target: display.targetName,
                    })
                  : t("referenceRemapWithoutTarget", {
                      source: display.source,
                      method,
                      kind,
                    })
                : t("referenceRemapGeneric", { method, kind })}
            </div>
          );
        })}
      </div>
    </Notice>
  );
}

export function RowEditor<T>({
  title,
  rows,
  addLabel,
  removeLabel,
  blank,
  onChange,
  render,
}: {
  title: string;
  rows: T[];
  addLabel: string;
  removeLabel: string;
  blank: T;
  onChange: (rows: T[]) => void;
  render: (row: T, update: (patch: Partial<T>) => void, index: number) => ReactNode;
}) {
  return (
    <section className="rounded-md border border-border bg-background p-4">
      <div className="mb-3 flex items-center justify-between">
        <h4 className="text-sm font-semibold text-foreground">{title}</h4>
        <button
          type="button"
          onClick={() => onChange([...rows, blank])}
          className="rounded-md border border-border px-2 py-1 text-xs text-muted hover:bg-surface-secondary hover:text-foreground"
        >
          {addLabel}
        </button>
      </div>
      <div className="grid gap-3">
        {rows.map((row, index) => (
          <div key={index} className="rounded-md border border-border bg-surface p-3">
            {render(
              row,
              (rowPatch) => {
                const next = [...rows];
                next[index] = { ...next[index], ...rowPatch };
                onChange(next);
              },
              index,
            )}
            <div className="mt-2 flex justify-end">
              <button
                type="button"
                onClick={() => onChange(rows.filter((_, i) => i !== index))}
                className="text-xs text-red-600 hover:underline dark:text-red-400"
              >
                {removeLabel}
              </button>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

export function ReadOnlyIds({
  label,
  ids,
  nameById,
}: {
  label: string;
  ids: string[];
  nameById: Record<string, string>;
}) {
  const t = useTranslations("writing.generationMetadata");
  if (ids.length === 0) return null;
  return (
    <div className="grid gap-1">
      <dt className="text-xs font-medium text-muted">{label}</dt>
      <dd className="flex flex-wrap gap-1.5">
        {ids.map((id) => (
          <span key={id} className="rounded-md border border-border px-2 py-0.5 text-xs text-foreground">
            {nameById[id] ?? t("unavailableReference")}
          </span>
        ))}
      </dd>
    </div>
  );
}
