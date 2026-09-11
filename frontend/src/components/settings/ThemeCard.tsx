"use client";

import { useId, useState } from "react";
import { useTranslations } from "next-intl";
import { isHexColor } from "@/lib/themeColorInput";
import { useThemeCustomization } from "@/components/ThemeCustomizationProvider";
import {
  THEME_PRESETS,
  SEMANTIC_COLOR_KEYS,
  SCALE_COLOR_KEYS,
  getPreset,
  type ThemeColors,
} from "@/lib/themes";

/* 颜色输入行 */
function ColorRow({
  label,
  pickerLabel,
  value,
  onChange,
}: {
  label: string;
  pickerLabel: string;
  value: string;
  onChange: (v: string) => void;
}) {
  const inputId = useId();
  const [draft, setDraft] = useState({ source: value, text: value });
  const text = draft.source === value ? draft.text : value;
  const valid = isHexColor(text);
  const changeColor = (next: string) => {
    const complete = isHexColor(next);
    setDraft({ source: complete ? next : value, text: next });
    if (complete) onChange(next);
  };

  return (
    <div className="grid min-w-0 grid-cols-[2rem_minmax(0,1fr)_6rem] items-center gap-2">
      <input
        type="color"
        value={value}
        aria-label={pickerLabel}
        onChange={(e) => changeColor(e.target.value)}
        className="h-8 w-8 shrink-0 cursor-pointer rounded border border-border bg-transparent p-0"
      />
      <label htmlFor={inputId} className="min-w-0 break-words text-sm text-foreground">{label}</label>
      <input
        id={inputId}
        type="text"
        value={text}
        aria-invalid={!valid}
        spellCheck={false}
        maxLength={7}
        onChange={(e) => changeColor(e.target.value)}
        onBlur={() => setDraft({ source: value, text: value })}
        className="w-full min-w-0 rounded-md border border-border bg-surface px-2 py-1 text-base font-mono text-foreground sm:text-sm"
      />
    </div>
  );
}

/* 主题卡片组件 */
export function ThemeCard() {
  const t = useTranslations("settings.theme");

  const {
    presetId,
    selectPreset,
    customLight,
    customDark,
    setCustomColors,
  } = useThemeCustomization();

  const [editMode, setEditMode] = useState<"light" | "dark">("light");
  const [showScale, setShowScale] = useState(false);

  /* 选择预设，或切换到自定义配色 */
  const handleSelect = (id: string) => {
    if (id === "custom") {
      const base =
        presetId === "custom"
          ? { light: customLight, dark: customDark }
          : (getPreset(presetId) ?? THEME_PRESETS[0]);

      setCustomColors({ ...base.light }, { ...base.dark });
    } else {
      selectPreset(id);
    }
  };

  /* 更新单个颜色，并立即同步到全局主题 */
  const handleColor = (key: keyof ThemeColors, value: string) => {
    if (editMode === "light") {
      setCustomColors({ ...customLight, [key]: value }, customDark);
    } else {
      setCustomColors(customLight, { ...customDark, [key]: value });
    }
  };

  const draft = editMode === "light" ? customLight : customDark;

  return (
    <div className="space-y-6">
      {/* 预设选择区 */}
      <section>
        <h3 className="mb-3 text-sm font-medium text-foreground">
          {t("presets")}
        </h3>
        <div className="flex flex-wrap gap-3">
          {THEME_PRESETS.map((preset) => (
            <button
              key={preset.id}
              type="button"
              aria-pressed={presetId === preset.id}
              onClick={() => handleSelect(preset.id)}
              className={`flex flex-col items-center gap-2 rounded-lg border-2 px-4 py-3 transition-all ${
                presetId === preset.id
                  ? "border-accent bg-accent/5 shadow-sm"
                  : "border-border hover:border-muted"
              }`}
            >
              <div className="flex gap-1">
                {preset.swatches.map((c, i) => (
                  <span
                    key={i}
                    className="block h-6 w-6 rounded-full border border-black/10"
                    style={{ backgroundColor: c }}
                  />
                ))}
              </div>
              <span className="text-xs font-medium text-foreground">
                {t(`preset_${preset.id}`)}
              </span>
            </button>
          ))}

          {/* 自定义选项 */}
          <button
            type="button"
            aria-pressed={presetId === "custom"}
            onClick={() => handleSelect("custom")}
            className={`flex flex-col items-center gap-2 rounded-lg border-2 px-4 py-3 transition-all ${
              presetId === "custom"
                ? "border-accent bg-accent/5 shadow-sm"
                : "border-border hover:border-muted"
            }`}
          >
            <div className="flex h-6 w-[104px] items-center justify-center">
              <svg
                xmlns="http://www.w3.org/2000/svg"
                width="20"
                height="20"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
                className="text-muted"
              >
                <circle cx="13.5" cy="6.5" r="2.5" />
                <circle cx="17.5" cy="10.5" r="2.5" />
                <circle cx="8.5" cy="7.5" r="2.5" />
                <circle cx="6.5" cy="12.5" r="2.5" />
                <path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.926 0 1.648-.746 1.648-1.688 0-.437-.18-.835-.437-1.125-.29-.289-.438-.652-.438-1.125a1.64 1.64 0 0 1 1.668-1.668h1.996c3.051 0 5.555-2.503 5.555-5.554C21.965 6.012 17.461 2 12 2z" />
              </svg>
            </div>
            <span className="text-xs font-medium text-foreground">
              {t("custom")}
            </span>
          </button>
        </div>
      </section>

      {/* 自定义颜色编辑区 */}
      {presetId === "custom" && (
        <section className="rounded-lg border border-border bg-surface p-4">
          {/* 明暗模式切换标签 */}
          <div className="mb-4 flex gap-2">
            {(["light", "dark"] as const).map((m) => (
              <button
                key={m}
                type="button"
                aria-pressed={editMode === m}
                onClick={() => setEditMode(m)}
                className={`rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
                  editMode === m
                    ? "bg-accent text-white"
                    : "bg-surface-secondary text-muted hover:text-foreground"
                }`}
              >
                {t(m === "light" ? "lightMode" : "darkMode")}
              </button>
            ))}
          </div>

          {/* 语义颜色 */}
          <h4 className="mb-2 text-xs font-medium text-muted">
            {t("semanticColors")}
          </h4>
          <p className="mb-3 text-sm text-muted">{t("colorInputHint")}</p>
          <div className="mb-4 grid grid-cols-1 gap-3 lg:grid-cols-2">
            {SEMANTIC_COLOR_KEYS.map((key) => (
              <ColorRow
                key={`${editMode}-${key}`}
                label={t(`colors.${key}`)}
                pickerLabel={t("pickerLabel", { name: t(`colors.${key}`) })}
                value={draft[key]}
                onChange={(v) => handleColor(key, v)}
              />
            ))}
          </div>

          {/* 高级色阶颜色 */}
          <button
            type="button"
            aria-expanded={showScale}
            onClick={() => setShowScale((s) => !s)}
            className="flex items-center gap-1.5 text-xs font-medium text-muted hover:text-foreground transition-colors"
          >
            <svg
              xmlns="http://www.w3.org/2000/svg"
              width="14"
              height="14"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              className={`transition-transform ${showScale ? "rotate-90" : ""}`}
            >
              <path d="m9 18 6-6-6-6" />
            </svg>
            {t("scaleColors")}
          </button>

          {showScale && (
            <div className="mt-2 grid grid-cols-1 gap-3 lg:grid-cols-2">
              {SCALE_COLOR_KEYS.map((key) => (
                <ColorRow
                  key={`${editMode}-${key}`}
                  label={t(`colors.${key}`)}
                  pickerLabel={t("pickerLabel", { name: t(`colors.${key}`) })}
                  value={draft[key]}
                  onChange={(v) => handleColor(key, v)}
                />
              ))}
            </div>
          )}
        </section>
      )}
    </div>
  );
}
