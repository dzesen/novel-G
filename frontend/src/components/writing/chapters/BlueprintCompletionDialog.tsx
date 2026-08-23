"use client";

import { useEffect, useRef } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@heroui/react";

const FOCUSABLE_SELECTOR = [
  "button:not([disabled])",
  "a[href]",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

interface BlueprintCompletionDialogProps {
  volumeCount: number;
  chapterCount: number;
  onManual: () => void;
  onVolume: () => void;
  onBook: () => void;
  onReviewWorld: () => void;
}

export default function BlueprintCompletionDialog({
  volumeCount,
  chapterCount,
  onManual,
  onVolume,
  onBook,
  onReviewWorld,
}: BlueprintCompletionDialogProps) {
  const t = useTranslations("writing.outline");
  const dialogRef = useRef<HTMLDivElement>(null);
  const manualRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    manualRef.current?.focus();
    const containFocus = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onManual();
        return;
      }
      if (event.key !== "Tab") return;
      const dialog = dialogRef.current;
      if (!dialog) return;
      const focusable = Array.from(
        dialog.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
      ).filter((element) => element.getClientRects().length > 0);
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;
      if (event.shiftKey && (active === first || !dialog.contains(active))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (active === last || !dialog.contains(active))) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", containFocus);
    return () => window.removeEventListener("keydown", containFocus);
  }, [onManual]);

  const choices = [
    {
      key: "manual",
      title: t("completionManualTitle"),
      description: t("completionManualDescription"),
      action: onManual,
      ref: manualRef,
    },
    {
      key: "volume",
      title: t("completionVolumeTitle"),
      description: t("completionVolumeDescription"),
      action: onVolume,
    },
    {
      key: "book",
      title: t("completionBookTitle"),
      description: t("completionBookDescription"),
      action: onBook,
    },
  ] as const;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/25 px-3 py-4 sm:px-4 sm:py-6">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="blueprint-completion-title"
        className="flex max-h-[calc(100dvh-2rem)] w-full max-w-xl flex-col overflow-hidden rounded-md border border-border bg-surface shadow-lg sm:max-h-[calc(100dvh-3rem)]"
      >
        <header className="border-b border-border px-4 py-4 sm:px-5">
          <h3
            id="blueprint-completion-title"
            className="text-balance text-lg font-semibold tracking-[-0.02em] text-foreground"
          >
            {t("completionTitle")}
          </h3>
          <p className="mt-1.5 text-sm leading-6 text-warm-700 dark:text-muted">
            {t("completionDescription", {
              volumes: volumeCount,
              chapters: chapterCount,
            })}
          </p>
        </header>

        <div className="min-h-0 overflow-y-auto px-4 py-4 sm:px-5">
          <div className="grid divide-y divide-border rounded-md border border-border bg-background">
            {choices.map((choice) => (
              <button
                key={choice.key}
                ref={"ref" in choice ? choice.ref : undefined}
                type="button"
                onClick={choice.action}
                className="group flex min-w-0 items-start justify-between gap-4 px-3 py-3 text-left transition-colors first:rounded-t-md last:rounded-b-md hover:bg-surface-secondary focus-visible:z-10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent sm:px-4"
              >
                <span className="min-w-0">
                  <span className="block text-sm font-semibold text-foreground">
                    {choice.title}
                  </span>
                  <span className="mt-1 block text-xs leading-5 text-warm-700 dark:text-muted">
                    {choice.description}
                  </span>
                </span>
                <svg
                  aria-hidden="true"
                  viewBox="0 0 20 20"
                  className="mt-0.5 size-5 shrink-0 text-warm-500 transition-transform group-hover:translate-x-0.5 dark:text-muted"
                >
                  <path
                    d="m7 4 6 6-6 6"
                    fill="none"
                    stroke="currentColor"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth="1.75"
                  />
                </svg>
              </button>
            ))}
          </div>

          <p className="mt-3 rounded-md border border-accent/30 bg-accent/5 px-3 py-2.5 text-xs leading-5 text-warm-800 dark:text-muted">
            {t("completionAutoSupplementHint")}
          </p>
        </div>

        <footer className="flex flex-wrap items-center justify-between gap-2 border-t border-border px-4 py-3 sm:px-5">
          <Button variant="ghost" size="sm" onPress={onReviewWorld}>
            {t("completionReviewWorld")}
          </Button>
          <Button variant="ghost" size="sm" onPress={onManual}>
            {t("completionLater")}
          </Button>
        </footer>
      </div>
    </div>
  );
}
