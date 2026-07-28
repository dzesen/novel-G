"use client";

import { useTranslations } from "next-intl";
import { useRouter, usePathname } from "next/navigation";
import type { WritingSidebarItem } from "@/types/novel";

interface WritingSidebarProps {
  activeItem: WritingSidebarItem;
  onSelect: (item: WritingSidebarItem) => void;
}

interface NavItem {
  key: WritingSidebarItem;
  labelKey: string;
  icon: React.ReactNode;
}

const MAIN_ITEMS: NavItem[] = [
  {
    key: "novel-info",
    labelKey: "sidebar.novelInfo",
    icon: (
      <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H19a1 1 0 0 1 1 1v18a1 1 0 0 1-1 1H6.5a1 1 0 0 1 0-5H20" />
      </svg>
    ),
  },
  {
    key: "chapter-editor",
    labelKey: "sidebar.chapterEditor",
    icon: (
      <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 20h9" /><path d="M16.376 3.622a1 1 0 0 1 3.002 3.002L7.368 18.635a2 2 0 0 1-.855.506l-2.872.838a.5.5 0 0 1-.62-.62l.838-2.872a2 2 0 0 1 .506-.854z" />
      </svg>
    ),
  },
  {
    key: "agent-studio",
    labelKey: "sidebar.agentStudio",
    icon: (
      <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 8V4H8" /><rect width="16" height="12" x="4" y="8" rx="2" /><path d="M2 14h2" /><path d="M20 14h2" /><path d="M15 13v2" /><path d="M9 13v2" />
      </svg>
    ),
  },
];

const ENTITY_ITEMS: NavItem[] = [
  {
    key: "reference-cards",
    labelKey: "sidebar.referenceCards",
    icon: (
      <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <rect width="16" height="14" x="4" y="5" rx="2" /><path d="M8 9h8" /><path d="M8 13h5" /><path d="M7 2h10" /><path d="M7 22h10" />
      </svg>
    ),
  },
  {
    key: "faction-cards",
    labelKey: "sidebar.factionCards",
    icon: (
      <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z" /><line x1="4" x2="4" y1="22" y2="15" />
      </svg>
    ),
  },
  {
    key: "relationship-map",
    labelKey: "sidebar.relationshipMap",
    icon: (
      <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="18" cy="18" r="3" /><circle cx="6" cy="6" r="3" /><path d="M13 6h3a2 2 0 0 1 2 2v7" /><path d="M11 18H8a2 2 0 0 1-2-2V9" />
      </svg>
    ),
  },
  {
    key: "plot-threads",
    labelKey: "sidebar.plotThreads",
    icon: (
      <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M4 7h16" /><path d="M4 12h16" /><path d="M4 17h10" /><circle cx="19" cy="17" r="2" />
      </svg>
    ),
  },
  {
    key: "story-health",
    labelKey: "sidebar.storyHealth",
    icon: (
      <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3 12h4l2.5-6 5 12 2.5-6h4" />
      </svg>
    ),
  },
  {
    key: "character-memory",
    labelKey: "sidebar.characterMemory",
    icon: (
      <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 8V4H8" /><rect width="16" height="12" x="4" y="8" rx="2" /><path d="M2 14h2" /><path d="M20 14h2" /><path d="M15 13v2" /><path d="M9 13v2" />
      </svg>
    ),
  },
];

export default function WritingSidebar({ activeItem, onSelect }: WritingSidebarProps) {
  const t = useTranslations("writing");
  const router = useRouter();
  const pathname = usePathname();
  const locale = pathname.startsWith("/en") ? "en" : "zh";

  const renderItem = (item: NavItem) => {
    const isActive = activeItem === item.key;
    return (
      <button
        key={item.key}
        onClick={() => onSelect(item.key)}
        className={`flex w-auto shrink-0 items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors md:w-full ${
          isActive
            ? "bg-accent/10 text-accent"
            : "text-muted hover:text-foreground hover:bg-surface-secondary"
        }`}
      >
        <span className={isActive ? "text-accent" : "text-muted"}>{item.icon}</span>
        {t(item.labelKey)}
      </button>
    );
  };

  return (
    <aside className="flex w-full shrink-0 flex-col border-b border-border bg-surface md:h-full md:w-56 md:border-b-0 md:border-r">
      {/* Back button */}
      <div className="px-3 pb-1 pt-3 md:pb-2 md:pt-4">
        <button
          onClick={() => router.push(`/${locale}`)}
          className="flex w-auto items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium text-muted transition-colors hover:bg-surface-secondary hover:text-foreground md:w-full"
        >
          <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M19 12H5" /><path d="m12 19-7-7 7-7" />
          </svg>
          {t("backToShelf")}
        </button>
      </div>

      {/* Main nav */}
      <nav className="flex gap-1 overflow-x-auto px-3 py-2 md:block md:flex-1 md:space-y-1 md:overflow-y-auto">
        {MAIN_ITEMS.map(renderItem)}

        {/* Divider */}
        <div className="my-3 hidden border-t border-dashed border-border md:block" />

        {/* Entity group label */}
        <div className="hidden px-3 py-1 md:block">
          <span className="text-xs font-semibold text-muted uppercase tracking-wider">
            {t("sidebar.entityGroup")}
          </span>
        </div>

        {ENTITY_ITEMS.map(renderItem)}
      </nav>
    </aside>
  );
}
