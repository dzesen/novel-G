"use client";

import { useTranslations } from "next-intl";
import { usePathname, useRouter } from "next/navigation";
import { IconButton } from "@/components/ui/IconButton";
import type { WritingArea } from "@/lib/writingRoute";
import WritingUtilities from "./WritingUtilities";

interface WritingNavigationProps {
  activeArea: WritingArea;
  novelTitle: string;
  onSelectArea: (area: WritingArea) => void;
}

const PRIMARY_AREAS: WritingArea[] = ["blueprint", "writing", "auto-book"];
const SUPPORT_AREAS: WritingArea[] = ["world", "continuity"];

function AreaIcon({ area }: { area: WritingArea }) {
  const common = {
    "aria-hidden": true,
    width: 18,
    height: 18,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.8,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
  };

  if (area === "blueprint") {
    return (
      <svg {...common}>
        <path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v16H6.5A2.5 2.5 0 0 0 4 21.5z" />
        <path d="M4 5.5v16M8 7h8M8 11h6" />
      </svg>
    );
  }
  if (area === "writing") {
    return (
      <svg {...common}>
        <path d="m4 20 4.2-1 10.6-10.6a2 2 0 0 0-2.8-2.8L5.4 16.2z" />
        <path d="m14.5 7.1 2.8 2.8M4 20h6" />
      </svg>
    );
  }
  if (area === "auto-book") {
    return (
      <svg {...common}>
        <path d="M5 4h10a3 3 0 0 1 3 3v13H8a3 3 0 0 1-3-3z" />
        <path d="M8 16h10M12 8v5M9.5 10.5h5" />
      </svg>
    );
  }
  if (area === "world") {
    return (
      <svg {...common}>
        <circle cx="12" cy="12" r="9" />
        <path d="M3.5 9h17M3.5 15h17M12 3c2.2 2.4 3.3 5.4 3.3 9S14.2 18.6 12 21M12 3C9.8 5.4 8.7 8.4 8.7 12s1.1 6.6 3.3 9" />
      </svg>
    );
  }
  return (
    <svg {...common}>
      <path d="M8 6h8M8 12h8M8 18h5" />
      <circle cx="5" cy="6" r="1" />
      <circle cx="5" cy="12" r="1" />
      <circle cx="5" cy="18" r="1" />
      <path d="m16 17 1.5 1.5L21 15" />
    </svg>
  );
}

export default function WritingNavigation({
  activeArea,
  novelTitle,
  onSelectArea,
}: WritingNavigationProps) {
  const t = useTranslations("writing.navigation");
  const router = useRouter();
  const pathname = usePathname();
  const locale = pathname.startsWith("/en") ? "en" : "zh";

  return (
    <header role="banner" data-testid="writing-shell" className="shrink-0 border-b border-border bg-background">
      <div className="flex min-w-0 items-center gap-1.5 border-b border-border px-2 py-1.5 sm:gap-2 sm:px-4">
        <IconButton
          onClick={() => router.push(`/${locale}`)}
          label={t("backToShelf")}
        >
          <svg aria-hidden="true" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M19 12H5" />
            <path d="m12 19-7-7 7-7" />
          </svg>
        </IconButton>
        <div className="min-w-0 flex-1 px-1">
          <p className="truncate text-sm font-semibold text-foreground sm:text-[15px]">
            {novelTitle || t("untitled")}
          </p>
          <p className="hidden truncate text-[11px] text-muted lg:block">{t("workspaceHint")}</p>
        </div>
        <nav aria-label={t("supportAria")} className="flex shrink-0 items-center gap-0.5 sm:gap-1">
          {SUPPORT_AREAS.map((area) => {
            const active = activeArea === area;
            return (
              <button
                key={area}
                type="button"
                onClick={() => onSelectArea(area)}
                aria-label={t(`areas.${area}.short`)}
                aria-current={active ? "page" : undefined}
                className={[
                  "inline-flex h-10 min-w-10 items-center justify-center gap-2 rounded-md px-2 text-xs font-semibold transition-colors",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus",
                  active
                    ? "bg-accent/10 text-accent"
                    : "text-muted hover:bg-surface-secondary hover:text-foreground",
                ].join(" ")}
              >
                <AreaIcon area={area} />
                <span className="hidden xl:inline">{t(`areas.${area}.short`)}</span>
              </button>
            );
          })}
        </nav>
        <WritingUtilities />
      </div>

      <nav
        aria-label={t("primaryAria")}
        className="grid grid-cols-3 bg-surface"
      >
        {PRIMARY_AREAS.map((area, index) => {
          const active = activeArea === area;
          return (
            <button
              key={area}
              type="button"
              onClick={() => onSelectArea(area)}
              aria-label={t(`areas.${area}.short`)}
              aria-current={active ? "step" : undefined}
              className={[
                "relative min-h-11 min-w-0 border-r border-border px-2 py-2 text-center transition-colors last:border-r-0 sm:min-h-12 sm:px-5",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent",
                active
                  ? "bg-accent/10 text-accent after:absolute after:inset-x-3 after:bottom-0 after:h-0.5 after:rounded-full after:bg-accent"
                  : "text-muted hover:bg-surface-secondary hover:text-foreground",
              ].join(" ")}
            >
              <span
                className={[
                  "hidden text-[10px] font-medium sm:block",
                  active ? "text-accent" : "text-muted",
                ].join(" ")}
              >
                {t("stage", { number: index + 1 })}
              </span>
              <span className="flex min-w-0 items-center justify-center gap-1.5 sm:mt-0.5 sm:gap-2">
                <span className="hidden shrink-0 sm:inline-flex">
                  <AreaIcon area={area} />
                </span>
                <span className="truncate text-xs font-semibold sm:text-sm">
                  {t(`areas.${area}.short`)}
                </span>
              </span>
            </button>
          );
        })}
      </nav>

    </header>
  );
}
