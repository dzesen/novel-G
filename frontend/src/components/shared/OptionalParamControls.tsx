"use client";

import {
  Switch,
  Slider,
  NumberField,
  TextField,
  TextArea,
} from "@heroui/react";

export function SwitchParam({
  label,
  description,
  value,
  onChange,
}: {
  label: string;
  description?: string;
  value: boolean;
  onChange: (enabled: boolean) => void;
}) {
  return (
    <div className="space-y-1">
      <Switch isSelected={value} onChange={onChange}>
        <Switch.Control>
          <Switch.Thumb />
        </Switch.Control>
        <Switch.Content className="text-sm">{label}</Switch.Content>
      </Switch>
      {description && (
        <p className="max-w-2xl text-xs leading-5 text-muted">{description}</p>
      )}
    </div>
  );
}

export function OptionalSliderParam({
  label,
  value,
  onToggle,
  onValueChange,
  min,
  max,
  step,
}: {
  label: string;
  value: number | null | undefined;
  onToggle: (enabled: boolean) => void;
  onValueChange: (v: number) => void;
  min: number;
  max: number;
  step: number;
}) {
  const enabled = value != null;
  return (
    <div className="flex items-center gap-4">
      <Switch isSelected={enabled} onChange={(v) => onToggle(v)} className="shrink-0">
        <Switch.Control>
          <Switch.Thumb />
        </Switch.Control>
        <Switch.Content className="text-sm w-40">{label}</Switch.Content>
      </Switch>
      {enabled && (
        <Slider
          aria-label={label}
          value={value!}
          onChange={(v) => onValueChange(v as number)}
          minValue={min}
          maxValue={max}
          step={step}
          className="flex-1 max-w-xs"
        >
          <Slider.Track>
            <Slider.Fill />
            <Slider.Thumb />
          </Slider.Track>
        </Slider>
      )}
      {enabled && (
        <span className="text-xs text-muted font-mono tabular-nums w-12 text-right shrink-0">
          {value!.toFixed(2)}
        </span>
      )}
    </div>
  );
}

export function OptionalNumberParam({
  label,
  value,
  onToggle,
  onValueChange,
  min,
  max,
  step,
}: {
  label: string;
  value: number | null | undefined;
  onToggle: (enabled: boolean) => void;
  onValueChange: (v: number) => void;
  min: number;
  max: number;
  step: number;
}) {
  const enabled = value != null;
  return (
    <div className="flex flex-col items-stretch gap-3 sm:flex-row sm:items-center sm:gap-4">
      <Switch
        isSelected={enabled}
        onChange={(v) => onToggle(v)}
        className="min-h-11 w-full shrink-0 sm:w-auto"
      >
        <Switch.Control>
          <Switch.Thumb />
        </Switch.Control>
        <Switch.Content className="w-auto min-w-0 text-sm sm:w-40">{label}</Switch.Content>
      </Switch>
      {enabled && (
        <NumberField
          aria-label={label}
          value={value!}
          onChange={(v) => onValueChange(Math.max(min, Math.min(max, v)))}
          minValue={min}
          maxValue={max}
          step={step}
          className="w-full sm:max-w-[180px]"
        >
          <NumberField.Group>
            <NumberField.DecrementButton />
            <NumberField.Input className="border-border" />
            <NumberField.IncrementButton />
          </NumberField.Group>
        </NumberField>
      )}
    </div>
  );
}

export function OptionalTextParam({
  label,
  value,
  onToggle,
  onValueChange,
  placeholder,
}: {
  label: string;
  value: string | null | undefined;
  onToggle: (enabled: boolean) => void;
  onValueChange: (v: string) => void;
  placeholder?: string;
}) {
  const enabled = value != null;
  return (
    <div className="space-y-2">
      <Switch isSelected={enabled} onChange={(v) => onToggle(v)}>
        <Switch.Control>
          <Switch.Thumb />
        </Switch.Control>
        <Switch.Content className="text-sm">{label}</Switch.Content>
      </Switch>
      {enabled && (
        <TextField aria-label={label} value={value ?? ""} onChange={(v) => onValueChange(v)}>
          <TextArea
            placeholder={placeholder}
            className="border-border min-h-[80px]"
            rows={3}
          />
        </TextField>
      )}
    </div>
  );
}
