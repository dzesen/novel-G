import { hasLocale } from "next-intl";
import { notFound } from "next/navigation";
import { setRequestLocale } from "next-intl/server";
import { routing } from "./routing";

export type LocalePageProps = { params: Promise<{ locale: string }> };

// Layouts and pages can render concurrently; validate before setting page context.
export async function prepareLocalePage<T extends { locale: string }>(params: Promise<T>) {
  const resolved = await params;
  if (!hasLocale(routing.locales, resolved.locale)) notFound();
  setRequestLocale(resolved.locale);
  return resolved;
}
