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
    <header role="banner" data-testid="writing-shell" className="studio-writing-nav">
      <div className="studio-writing-identity">
        <IconButton onClick={() => router.push(`/${locale}`)} label={t("backToShelf")}>
          <svg aria-hidden="true" width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M4 5h7v15H4zM14 5l5-1 3 15-5 1zM6.5 8h2M6.5 16h2" /></svg>
        </IconButton>
        <p title={novelTitle}>{novelTitle || t("untitled")}</p>
      </div>
      <nav aria-label={t("primaryAria")} className="studio-primary-nav">
        {PRIMARY_AREAS.map((area) => (
          <button key={area} type="button" onClick={() => onSelectArea(area)} aria-label={t(`areas.${area}.short`)} aria-current={activeArea === area ? "step" : undefined}>
            <AreaIcon area={area} /><span>{t(`areas.${area}.short`)}</span>
          </button>
        ))}
      </nav>
      <nav aria-label={t("supportAria")} className="studio-support-nav">
        {SUPPORT_AREAS.map((area) => (
          <button key={area} type="button" onClick={() => onSelectArea(area)} aria-label={t(`areas.${area}.short`)} aria-current={activeArea === area ? "page" : undefined}>
            <AreaIcon area={area} /><span>{t(`areas.${area}.short`)}</span>
          </button>
        ))}
      </nav>
      <div className="studio-writing-tools"><WritingUtilities /></div>
    </header>
  );
}
