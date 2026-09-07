import type { AuthorConstraints } from "@/types/novel";

export const AUTHOR_CONSTRAINT_KEYS = ["must_keep", "do_not_change", "style_boundaries"] as const;

/** Validate a closed, bounded author-input field; never recover requirements from summaries. */
export function normalizeAuthorConstraints(value: unknown): AuthorConstraints | null {
  if (value === undefined) return { must_keep: [], do_not_change: [], style_boundaries: [] };
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  if (Object.keys(record).some((key) => !AUTHOR_CONSTRAINT_KEYS.includes(key as typeof AUTHOR_CONSTRAINT_KEYS[number]))) return null;
  const result: AuthorConstraints = { must_keep: [], do_not_change: [], style_boundaries: [] };
  for (const key of AUTHOR_CONSTRAINT_KEYS) {
    const items = record[key] ?? [];
    if (!Array.isArray(items) || !items.every((item) => typeof item === "string")) return null;
    const normalized = (items as string[]).map((item) => item.trim()).filter(Boolean);
    if (normalized.length > 20 || normalized.some((item) => item.length > 500)) return null;
    result[key] = normalized;
  }
  return result;
}
