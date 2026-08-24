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
  Scene,
} from "./outlineTypes";
import { isSceneTransitionContract } from "./outlineTypes";

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

export function SceneContractDetails({ scene }: { scene: Scene }) {
  const t = useTranslations("writing.outline");
  if (!isSceneTransitionContract(scene)) return null;

  const conditionList = (
    title: string,
    conditions: typeof scene.preconditions,
  ) => (
    <div className="grid min-w-0 gap-1">
      <h6 className="text-xs font-semibold text-foreground">{title}</h6>
      {conditions.length > 0 ? (
        <ul className="grid gap-1">
          {conditions.map((condition) => (
            <li key={condition.condition_id} className="min-w-0 break-words text-xs leading-5 text-muted">
              <code className="mr-1 break-all text-[11px] text-foreground">
                {condition.condition_id}
              </code>
              {condition.description}
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-xs text-muted">{t("sceneContractNone")}</p>
      )}
    </div>
  );

  return (
    <details open className="min-w-0 rounded-md border border-border bg-background px-3 py-2">
      <summary className="cursor-pointer break-words text-xs font-semibold text-foreground">
        {t("sceneContractSummary", {
          sceneId: scene.scene_id,
          min: scene.word_budget.min,
          target: scene.word_budget.target,
          max: scene.word_budget.max,
        })}
      </summary>
      <div className="mt-3 grid min-w-0 gap-3">
        <dl className="grid min-w-0 gap-2 text-xs sm:grid-cols-2">
          <div className="min-w-0">
            <dt className="font-medium text-muted">{t("sceneContractEventKey")}</dt>
            <dd className="break-all text-foreground">{scene.event_key}</dd>
          </div>
          <div className="min-w-0">
            <dt className="font-medium text-muted">{t("sceneContractRepetition")}</dt>
            <dd className="break-words text-foreground">
              {t(`sceneContractRepetitionPolicies.${scene.repetition_policy}`)}
            </dd>
          </div>
        </dl>

        {conditionList(t("sceneContractPreconditions"), scene.preconditions)}

        <div className="grid min-w-0 gap-1">
          <h6 className="text-xs font-semibold text-foreground">{t("sceneContractBeats")}</h6>
          <ol className="grid gap-2">
            {scene.beats.map((beat) => (
              <li key={beat.beat_id} className="min-w-0 rounded-md bg-surface px-2 py-1.5 text-xs leading-5">
                <div className="flex min-w-0 flex-wrap items-center gap-1.5">
                  <code className="break-all text-[11px] text-foreground">{beat.beat_id}</code>
                  <span className="rounded border border-border px-1 text-[10px] text-muted">
                    {beat.required ? t("sceneContractRequired") : t("sceneContractOptional")}
                  </span>
                </div>
                <p className="break-words text-foreground">{beat.description}</p>
                <p className="break-words text-muted">
                  {t("sceneContractExpectedTransition", {
                    transition: beat.expected_transition,
                  })}
                </p>
              </li>
            ))}
          </ol>
        </div>

        {conditionList(t("sceneContractPostconditions"), scene.postconditions)}
        {conditionList(t("sceneContractForbidden"), scene.forbidden_conditions)}

        <div className="grid min-w-0 gap-1">
          <h6 className="text-xs font-semibold text-foreground">{t("sceneContractDeltas")}</h6>
          <ul className="grid gap-1">
            {scene.narrative_delta.map((delta) => (
              <li key={delta.delta_id} className="min-w-0 break-words text-xs leading-5 text-muted">
                <code className="mr-1 break-all text-[11px] text-foreground">{delta.delta_id}</code>
                {t("sceneContractDelta", {
                  dimension: t(`sceneContractDimensions.${delta.dimension}`),
                  before: delta.before,
                  after: delta.after,
                })}
              </li>
            ))}
          </ul>
        </div>
      </div>
    </details>
  );
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
  structureLocked = false,
  onChange,
  render,
}: {
  title: string;
  rows: T[];
  addLabel: string;
  removeLabel: string;
  blank: T;
  structureLocked?: boolean;
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
          disabled={structureLocked}
          className="rounded-md border border-border px-2 py-1 text-xs text-muted hover:bg-surface-secondary hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
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
                disabled={structureLocked}
                className="text-xs text-red-600 hover:underline disabled:cursor-not-allowed disabled:opacity-50 dark:text-red-400"
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
