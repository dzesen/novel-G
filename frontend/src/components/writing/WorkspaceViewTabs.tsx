"use client";

import type { WritingView } from "@/lib/writingRoute";

export interface WorkspaceViewTab {
  view: WritingView;
  label: string;
}

interface WorkspaceViewTabsProps {
  label: string;
  activeView: WritingView;
  tabs: WorkspaceViewTab[];
  onSelect: (view: WritingView) => void;
}

export default function WorkspaceViewTabs({
  label,
  activeView,
  tabs,
  onSelect,
}: WorkspaceViewTabsProps) {
  return (
    <nav
      aria-label={label}
      className="flex shrink-0 gap-1 overflow-x-auto border-b border-border bg-surface px-3 py-1.5 sm:px-5"
    >
      {tabs.map((tab) => (
        <button
          key={tab.view}
          type="button"
          onClick={() => onSelect(tab.view)}
          aria-current={activeView === tab.view ? "page" : undefined}
          className={[
            "min-h-9 shrink-0 rounded-md px-3 text-xs font-medium transition-colors",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent",
            activeView === tab.view
              ? "bg-accent/10 text-accent"
              : "text-muted hover:bg-surface-secondary hover:text-foreground",
          ].join(" ")}
        >
          {tab.label}
        </button>
      ))}
    </nav>
  );
}
