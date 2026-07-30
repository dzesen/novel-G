"use client";

import { useMemo, useState } from "react";
import { Button, Switch } from "@heroui/react";
import { useTranslations } from "next-intl";
import type {
  ComfyUIWorkflowConfig,
  ImageConfigScalar,
  ImageProviderWorkflowInput,
} from "@/types/config";

type ImageUsage = "character_portrait" | "cover" | "scene_illustration";
type Translator = ReturnType<typeof useTranslations>;

interface Props {
  workflow: ComfyUIWorkflowConfig;
  controls: ImageProviderWorkflowInput[];
  inspected: boolean;
  affectedUsages: ImageUsage[];
  onChange: (workflow: ComfyUIWorkflowConfig) => void;
  onCloneForUsage: (usage: ImageUsage) => void;
  t: Translator;
}

function targetKey(nodeId: string, input: string) {
  return `${nodeId}\u0000${input}`;
}

function displayScalar(value: ImageConfigScalar | null) {
  return value === null ? "null" : JSON.stringify(value);
}

function overrideValue(
  workflow: ComfyUIWorkflowConfig,
  control: ImageProviderWorkflowInput,
): ImageConfigScalar | null {
  return workflow.overrides.find(
    (item) => item.node_id === control.node_id && item.input === control.input,
  )?.value ?? control.template_value;
}

export function ComfyUIWorkflowOverrides({
  workflow,
  controls,
  inspected,
  affectedUsages,
  onChange,
  onCloneForUsage,
  t,
}: Props) {
  const [sharedEditConfirmed, setSharedEditConfirmed] = useState(false);
  const [cloneUsage, setCloneUsage] = useState<ImageUsage>(
    affectedUsages[0] || "character_portrait",
  );

  const selectedCloneUsage = affectedUsages.includes(cloneUsage)
    ? cloneUsage
    : affectedUsages[0] || "character_portrait";
  const controlsByGroup = useMemo(() => ({
    common: controls.filter((control) => control.group === "common"),
    advanced: controls.filter((control) => control.group === "advanced"),
  }), [controls]);
  const visibleTargets = useMemo(
    () => new Set(controls.map((control) => targetKey(control.node_id, control.input))),
    [controls],
  );
  const unresolvedOverrides = inspected
    ? workflow.overrides.filter(
      (item) => !visibleTargets.has(targetKey(item.node_id, item.input)),
    )
    : [];

  const allowSharedEdit = () => {
    if (affectedUsages.length <= 1 || sharedEditConfirmed) return true;
    if (!window.confirm(t("overrides.sharedConfirm", { count: affectedUsages.length }))) {
      return false;
    }
    setSharedEditConfirmed(true);
    return true;
  };

  const removeOverride = (nodeId: string, input: string) => {
    if (!allowSharedEdit()) return;
    onChange({
      ...workflow,
      overrides: workflow.overrides.filter(
        (item) => item.node_id !== nodeId || item.input !== input,
      ),
    });
  };

  const setOverride = (
    control: ImageProviderWorkflowInput,
    value: ImageConfigScalar | null,
  ) => {
    if (!allowSharedEdit()) return;
    const remaining = workflow.overrides.filter(
      (item) => item.node_id !== control.node_id || item.input !== control.input,
    );
    if (value === null || Object.is(value, control.template_value)) {
      onChange({ ...workflow, overrides: remaining });
      return;
    }
    onChange({
      ...workflow,
      overrides: [
        ...remaining,
        { node_id: control.node_id, input: control.input, value },
      ],
    });
  };

  if (!inspected || (controls.length === 0 && workflow.overrides.length === 0)) {
    return (
      <section className="rounded-lg border border-dashed border-border bg-surface-secondary/20 p-3 text-sm text-muted">
        {t("overrides.inspectHint")}
      </section>
    );
  }

  return (
    <section className="rounded-lg border border-border bg-surface-secondary/20 p-3">
      <div>
        <h4 className="text-base font-semibold text-foreground">{t("overrides.title")}</h4>
        <p className="mt-1 max-w-3xl text-xs text-muted">{t("overrides.description")}</p>
      </div>

      {affectedUsages.length > 1 && (
        <div className="mt-3 rounded-lg border border-amber-300 bg-amber-50 p-3 dark:border-amber-800 dark:bg-amber-950/30">
          <p className="text-sm font-medium text-amber-900 dark:text-amber-100">
            {t("overrides.sharedTitle", { count: affectedUsages.length })}
          </p>
          <p className="mt-1 text-xs text-amber-800 dark:text-amber-200">
            {affectedUsages.map((usage) => t(`assignments.${usage}`)).join(" · ")}
          </p>
          <div className="mt-3 flex min-w-0 flex-col gap-2 sm:flex-row sm:items-end">
            <label className="min-w-0 flex-1 text-xs text-amber-900 dark:text-amber-100">
              <span>{t("overrides.cloneUsage")}</span>
              <select
                value={selectedCloneUsage}
                onChange={(event) => setCloneUsage(event.target.value as ImageUsage)}
                className="mt-1 w-full rounded-lg border border-amber-300 bg-white px-3 py-2 text-sm text-foreground dark:border-amber-800 dark:bg-surface"
              >
                {affectedUsages.map((usage) => (
                  <option key={usage} value={usage}>{t(`assignments.${usage}`)}</option>
                ))}
              </select>
            </label>
            <Button
              size="sm"
              variant="outline"
              onPress={() => onCloneForUsage(selectedCloneUsage)}
              className="shrink-0 border-amber-400 text-amber-900 dark:text-amber-100"
            >
              {t("overrides.cloneAction")}
            </Button>
          </div>
        </div>
      )}

      <div className="mt-4 space-y-3">
        <ControlGroup
          title={t("overrides.common")}
          controls={controlsByGroup.common}
          workflow={workflow}
          onSet={setOverride}
          onReset={removeOverride}
          t={t}
        />
        {controlsByGroup.advanced.length > 0 && (
          <details className="rounded-lg border border-border bg-surface">
            <summary className="cursor-pointer px-3 py-2 text-sm font-semibold text-foreground">
              {t("overrides.advanced", { count: controlsByGroup.advanced.length })}
            </summary>
            <div className="space-y-3 border-t border-border p-3">
              {controlsByGroup.advanced.map((control) => (
                <ControlRow
                  key={targetKey(control.node_id, control.input)}
                  control={control}
                  value={overrideValue(workflow, control)}
                  onSet={(value) => setOverride(control, value)}
                  onReset={() => removeOverride(control.node_id, control.input)}
                  t={t}
                />
              ))}
            </div>
          </details>
        )}
      </div>

      {unresolvedOverrides.length > 0 && (
        <div className="mt-4 rounded-lg border border-red-300 bg-red-50 p-3 dark:border-red-800 dark:bg-red-950/30">
          <h5 className="text-sm font-semibold text-red-800 dark:text-red-200">
            {t("overrides.unresolvedTitle")}
          </h5>
          <p className="mt-1 text-xs text-red-700 dark:text-red-300">
            {t("overrides.unresolvedDescription")}
          </p>
          <div className="mt-3 space-y-2">
            {unresolvedOverrides.map((item) => (
              <div
                key={targetKey(item.node_id, item.input)}
                className="flex min-w-0 flex-col gap-2 rounded-lg border border-red-200 bg-white p-2 sm:flex-row sm:items-center sm:justify-between dark:border-red-900 dark:bg-surface"
              >
                <code className="min-w-0 break-all text-xs text-foreground">
                  {item.node_id}.{item.input} = {displayScalar(item.value)}
                </code>
                <Button
                  size="sm"
                  variant="outline"
                  onPress={() => removeOverride(item.node_id, item.input)}
                  className="shrink-0 border-red-300 text-red-700"
                >
                  {t("overrides.removeInvalid")}
                </Button>
              </div>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}

function ControlGroup({
  title,
  controls,
  workflow,
  onSet,
  onReset,
  t,
}: {
  title: string;
  controls: ImageProviderWorkflowInput[];
  workflow: ComfyUIWorkflowConfig;
  onSet: (control: ImageProviderWorkflowInput, value: ImageConfigScalar | null) => void;
  onReset: (nodeId: string, input: string) => void;
  t: Translator;
}) {
  if (controls.length === 0) return null;
  return (
    <div>
      <h5 className="mb-2 text-sm font-semibold text-foreground">{title}</h5>
      <div className="space-y-3">
        {controls.map((control) => (
          <ControlRow
            key={targetKey(control.node_id, control.input)}
            control={control}
            value={overrideValue(workflow, control)}
            onSet={(value) => onSet(control, value)}
            onReset={() => onReset(control.node_id, control.input)}
            t={t}
          />
        ))}
      </div>
    </div>
  );
}

function ControlRow({
  control,
  value,
  onSet,
  onReset,
  t,
}: {
  control: ImageProviderWorkflowInput;
  value: ImageConfigScalar | null;
  onSet: (value: ImageConfigScalar | null) => void;
  onReset: () => void;
  t: Translator;
}) {
  const [manual, setManual] = useState(false);
  const overridden = !Object.is(value, control.template_value);
  const serializedValue = displayScalar(value);
  const valueIsListed = control.choices.some(
    (choice) => Object.is(choice, value),
  );
  return (
    <div
      data-testid={`workflow-control-${control.node_id}-${control.input}`}
      className={`rounded-lg border bg-surface p-3 ${
      control.status === "missing" ? "border-red-300 dark:border-red-800" : "border-border"
    }`}>
      <div className="flex min-w-0 flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="break-all text-sm font-semibold text-foreground">
              {control.node_title} · {control.input}
            </span>
            <span className="rounded-full bg-warm-200 px-2 py-0.5 text-[11px] text-muted dark:bg-warm-300/30">
              {control.node_id} / {control.class_type}
            </span>
            {overridden && (
              <span className="rounded-full bg-accent/10 px-2 py-0.5 text-[11px] text-accent">
                {t("overrides.changed")}
              </span>
            )}
          </div>
          <div className="mt-1 grid min-w-0 gap-x-4 gap-y-1 text-xs text-muted sm:grid-cols-2">
            <span className="min-w-0 break-all">
              {t("overrides.templateValue")}: {displayScalar(control.template_value)}
            </span>
            <span className="min-w-0 break-all">
              {t("overrides.effectiveValue")}: {displayScalar(value)}
            </span>
          </div>
        </div>
        {overridden && (
          <Button
            size="sm"
            variant="outline"
            onPress={onReset}
            className="shrink-0 border-border text-foreground"
          >
            {t("overrides.reset")}
          </Button>
        )}
      </div>

      <div className="mt-3">
        {control.value_type === "boolean" ? (
          <Switch isSelected={Boolean(value)} onChange={(next) => onSet(next)}>
            <Switch.Control><Switch.Thumb /></Switch.Control>
            <Switch.Content className="text-sm">{t("overrides.booleanValue")}</Switch.Content>
          </Switch>
        ) : control.choices.length > 0 && !manual ? (
          <div className="flex min-w-0 flex-col gap-2 sm:flex-row">
            <select
              aria-label={t("overrides.effectiveValue")}
              value={serializedValue}
              onChange={(event) => onSet(JSON.parse(event.target.value) as ImageConfigScalar | null)}
              className="min-w-0 flex-1 rounded-lg border border-border bg-surface px-3 py-2 text-sm text-foreground"
            >
              {!valueIsListed && (
                <option value={serializedValue} disabled>
                  {t("overrides.unavailableValue", { value: serializedValue })}
                </option>
              )}
              {control.choices.map((choice) => (
                <option key={displayScalar(choice)} value={displayScalar(choice)}>
                  {displayScalar(choice)}
                </option>
              ))}
            </select>
            <Button
              size="sm"
              variant="outline"
              onPress={() => setManual(true)}
              className="shrink-0 border-border text-foreground"
            >
              {t("overrides.manual")}
            </Button>
          </div>
        ) : (
          <ManualScalarInput
            key={`${targetKey(control.node_id, control.input)}:${displayScalar(value)}`}
            control={control}
            value={value}
            onApply={onSet}
            onBack={control.choices.length > 0 ? () => setManual(false) : undefined}
            t={t}
          />
        )}
      </div>
      {control.issue && (
        <p className={`mt-2 text-xs ${
          control.status === "missing" ? "text-red-600 dark:text-red-300" : "text-amber-700 dark:text-amber-300"
        }`}>
          {control.issue}
        </p>
      )}
    </div>
  );
}

function ManualScalarInput({
  control,
  value,
  onApply,
  onBack,
  t,
}: {
  control: ImageProviderWorkflowInput;
  value: ImageConfigScalar | null;
  onApply: (value: ImageConfigScalar | null) => void;
  onBack?: () => void;
  t: Translator;
}) {
  const [draft, setDraft] = useState(value === null ? "" : String(value));
  const [error, setError] = useState("");
  const apply = () => {
    let next: ImageConfigScalar;
    if (control.value_type === "integer") {
      const parsed = Number(draft);
      if (!Number.isSafeInteger(parsed)) {
        setError(t("overrides.invalidInteger"));
        return;
      }
      next = parsed;
    } else if (control.value_type === "number") {
      const parsed = Number(draft);
      if (!Number.isFinite(parsed)) {
        setError(t("overrides.invalidNumber"));
        return;
      }
      next = parsed;
    } else {
      next = draft;
    }
    setError("");
    onApply(next);
  };
  return (
    <div>
      <div className="flex min-w-0 flex-col gap-2 sm:flex-row">
        <input
          aria-label={t("overrides.effectiveValue")}
          value={draft}
          type={control.value_type === "integer" || control.value_type === "number" ? "number" : "text"}
          min={control.minimum ?? undefined}
          max={control.maximum ?? undefined}
          step={control.step ?? undefined}
          onChange={(event) => {
            setDraft(event.target.value);
            setError("");
          }}
          className={`min-w-0 flex-1 rounded-lg border bg-surface px-3 py-2 text-sm text-foreground ${
            error ? "border-red-400" : "border-border"
          }`}
        />
        <Button size="sm" onPress={apply} className="shrink-0 bg-accent text-white">
          {t("overrides.apply")}
        </Button>
        {onBack && (
          <Button size="sm" variant="outline" onPress={onBack} className="shrink-0 border-border">
            {t("overrides.useList")}
          </Button>
        )}
      </div>
      {error && <p className="mt-1 text-xs text-red-600">{error}</p>}
    </div>
  );
}
