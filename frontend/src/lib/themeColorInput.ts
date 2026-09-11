import type { ThemeColors } from "./themes";

export function isHexColor(value: unknown): value is string {
  return typeof value === "string" && /^#[0-9a-fA-F]{6}$/.test(value);
}

export function normalizeThemeColors(value: unknown, fallback: ThemeColors): ThemeColors {
  const result = { ...fallback };
  if (!value || typeof value !== "object" || Array.isArray(value)) return result;
  const saved = value as Record<string, unknown>;
  for (const key of Object.keys(fallback) as Array<keyof ThemeColors>) {
    if (isHexColor(saved[key])) result[key] = saved[key];
  }
  return result;
}
