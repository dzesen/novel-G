"use client";

import { useTranslations } from "next-intl";
import type { RosterEntry } from "./useRoster";

interface RosterPickerProps {
  label: string;
  options: RosterEntry[];
  /** 单选时传 string | null，多选时传 string[]。 */
  value: string | null | string[];
  onChange: (next: string | null | string[]) => void;
  mode: "single" | "multi";
  emptyText: string;
}

/**
 * id 选择器：显示名称、提交 id。
 *
 * AI 直接吐 ObjectId 字符串（2a 设计 §2 的决策——卡名无唯一索引，按名查会重蹈
 * 同名歧义），所以人工修正也必须落到 id 上，不能让人输名字。
 */
export default function RosterPicker({ label, options, value, onChange, mode, emptyText }: RosterPickerProps) {
  const t = useTranslations("writing.outline");
  const selected = mode === "multi" ? (value as string[]) : value ? [value as string] : [];

  const toggle = (id: string) => {
    if (mode === "single") {
      onChange(selected.includes(id) ? null : id);
      return;
    }
    onChange(selected.includes(id) ? selected.filter((item) => item !== id) : [...selected, id]);
  };

  return (
    <div className="grid gap-1.5">
      <span className="text-xs font-medium text-muted">{label}</span>
      {options.length === 0 ? (
        <p className="rounded-md border border-dashed border-border px-3 py-2 text-xs text-muted">{emptyText}</p>
      ) : (
        <div className="flex flex-wrap gap-1.5">
          {options.map((option) => {
            const active = selected.includes(option.id);
            return (
              <button
                key={option.id}
                type="button"
                title={option.hint}
                onClick={() => toggle(option.id)}
                className={`rounded-md border px-2 py-1 text-xs transition-colors ${
                  active
                    ? "border-accent bg-accent/10 text-accent"
                    : "border-border text-muted hover:bg-surface-secondary hover:text-foreground"
                }`}
              >
                {option.name}
              </button>
            );
          })}
        </div>
      )}
      {selected.some((id) => !options.some((option) => option.id === id)) && (
        <p className="text-xs text-amber-700 dark:text-amber-300">
          {selected
            .filter((id) => !options.some((option) => option.id === id))
            .map((id) => t("unknownId", { id }))
            .join("；")}
        </p>
      )}
    </div>
  );
}
