"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { usePathname, useSearchParams } from "next/navigation";
import { apiGet } from "@/lib/api";
import {
  buildAreaSearch,
  buildViewSearch,
  defaultWritingRoute,
  resolveWritingRoute,
  type WritingArea,
  type WritingRouteTargets,
  type WritingView,
} from "@/lib/writingRoute";
import type { ContinuityEvidenceReference } from "@/types/agent";
import type { NovelDetail, ReferenceCardType } from "@/types/novel";
import WritingNavigation from "./WritingNavigation";
import NovelInfoWorkspace from "./novel-info/NovelInfoWorkspace";
import ChapterWorkspace, {
  type ProseOpenRequest,
} from "./chapters/ChapterWorkspace";
import type { ProseRunSnapshot } from "./chapters/prose/useProseStream";
import AutoBookWorkspace, {
  type AutoBookStartRequest,
} from "./auto-book/AutoBookWorkspace";
import ReferenceCardsDestination from "./reference-cards/ReferenceCardsDestination";
import FactionCardsWorkspace from "./factions/FactionCardsWorkspace";
import RelationshipWorkspace from "./relationships/RelationshipWorkspace";
import PlotThreadWorkspace from "./plot-threads/PlotThreadWorkspace";
import CharacterMemoryWorkspace from "./character-memory/CharacterMemoryWorkspace";
import StoryHealthWorkspace from "./story-health/StoryHealthWorkspace";
import AgentStudioWorkspace from "./agents/AgentStudioWorkspace";

interface WritingContentProps {
  mode: "create" | "edit";
  novelId?: string;
}

const REFERENCE_CARD_TYPES: ReferenceCardType[] = [
  "character",
  "location",
  "item",
  "rule",
  "lore",
];

function parseReferenceCardType(value?: string): ReferenceCardType {
  return REFERENCE_CARD_TYPES.find((cardType) => cardType === value) ?? "character";
}

function parseSceneIndex(value?: string): number | undefined {
  if (!value) return undefined;
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed >= 0 ? parsed : undefined;
}

function hasLegacyRouteSignal(search: URLSearchParams): boolean {
  return ["view", "cardType", "curateCards", "reviewCards"].some((key) =>
    search.has(key),
  );
}

interface ViewTab {
  view: WritingView;
  label: string;
}

function ViewTabs({
  label,
  activeView,
  tabs,
  onSelect,
}: {
  label: string;
  activeView: WritingView;
  tabs: ViewTab[];
  onSelect: (view: WritingView) => void;
}) {
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

function RouteFailure({
  kind,
  value,
  onOpenDefault,
}: {
  kind: "area" | "view";
  value: string;
  onOpenDefault: () => void;
}) {
  const t = useTranslations("writing.navigation");
  return (
    <div className="grid h-full place-items-center overflow-y-auto bg-background px-5 py-10">
      <section className="w-full max-w-xl border-y border-border py-8">
        <h1 className="text-xl font-semibold text-foreground">{t("targetMissingTitle")}</h1>
        <p className="mt-3 text-sm leading-6 text-muted">
          {t("targetMissingBody", { kind: t(`targetKinds.${kind}`), value })}
        </p>
        <code className="mt-4 block overflow-x-auto bg-surface-secondary px-3 py-2 text-xs text-foreground">
          {value}
        </code>
        <button
          type="button"
          onClick={onOpenDefault}
          className="mt-5 min-h-10 rounded-md bg-accent px-4 text-sm font-semibold text-white hover:bg-accent-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2"
        >
          {t("openDefault")}
        </button>
      </section>
    </div>
  );
}

function DeferredContinuityView({
  onOpenHealth,
  onOpenFacts,
}: {
  onOpenHealth: () => void;
  onOpenFacts: () => void;
}) {
  const t = useTranslations("writing.navigation");
  return (
    <div className="grid h-full place-items-center overflow-y-auto bg-background px-5 py-10">
      <section className="w-full max-w-xl border-y border-border py-8">
        <h1 className="text-xl font-semibold text-foreground">
          {t("continuityTargetTitle")}
        </h1>
        <p className="mt-3 text-sm leading-6 text-muted">
          {t("continuityTargetBody")}
        </p>
        <div className="mt-5 flex flex-wrap gap-2">
          <button type="button" onClick={onOpenHealth} className="min-h-10 rounded-md bg-accent px-4 text-sm font-semibold text-white hover:bg-accent-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent">
            {t("openHealth")}
          </button>
          <button type="button" onClick={onOpenFacts} className="min-h-10 rounded-md border border-border bg-surface px-4 text-sm font-semibold text-foreground hover:bg-surface-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent">
            {t("openFacts")}
          </button>
        </div>
      </section>
    </div>
  );
}

export default function WritingContent({ mode, novelId }: WritingContentProps) {
  const t = useTranslations("writing.navigation");
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const [novel, setNovel] = useState<NovelDetail | null>(null);
  const [novelLookupComplete, setNovelLookupComplete] = useState(mode === "create");
  const [evidenceReference, setEvidenceReference] =
    useState<ContinuityEvidenceReference | null>(null);
  const [autoBookStartRequest, setAutoBookStartRequest] =
    useState<AutoBookStartRequest | null>(null);
  const [proseOpenRequest, setProseOpenRequest] =
    useState<ProseOpenRequest | null>(null);

  useEffect(() => {
    if (mode !== "edit" || !novelId) return;
    let cancelled = false;
    void apiGet<NovelDetail>(`/api/novels/${novelId}`)
      .then((result) => {
        if (!cancelled) setNovel(result);
      })
      .catch(() => {
        if (!cancelled) setNovel(null);
      })
      .finally(() => {
        if (!cancelled) setNovelLookupComplete(true);
      });
    return () => {
      cancelled = true;
    };
  }, [mode, novelId]);

  const currentSearch = useMemo(
    () => new URLSearchParams(searchParams.toString()),
    [searchParams],
  );
  const needsDefaultLookup =
    mode === "edit" &&
    !currentSearch.has("area") &&
    !hasLegacyRouteSignal(currentSearch);
  const routeReady = !needsDefaultLookup || novelLookupComplete;
  const fallbackRoute = defaultWritingRoute(novel?.stats.chapter_count ?? 1);
  const resolved = useMemo(
    () => resolveWritingRoute(currentSearch, fallbackRoute),
    [currentSearch, fallbackRoute],
  );

  useEffect(() => {
    if (mode === "create" || !routeReady || !resolved.canonicalSearch) return;
    const nextHref = `${pathname}?${resolved.canonicalSearch}`;
    const currentHref = `${pathname}${window.location.search}`;
    if (nextHref !== currentHref) {
      window.history.replaceState(null, "", nextHref);
    }
  }, [mode, pathname, resolved.canonicalSearch, routeReady]);

  const pushSearch = useCallback(
    (search: string, replace = false) => {
      const href = search ? `${pathname}?${search}` : pathname;
      if (replace) window.history.replaceState(null, "", href);
      else window.history.pushState(null, "", href);
    },
    [pathname],
  );

  const navigateArea = useCallback(
    (area: WritingArea) => {
      setEvidenceReference(null);
      pushSearch(buildAreaSearch(new URLSearchParams(window.location.search), area));
    },
    [pushSearch],
  );

  const navigateView = useCallback(
    (
      area: WritingArea,
      view: WritingView,
      targets: WritingRouteTargets = {},
      replace = false,
    ) => {
      setEvidenceReference(null);
      pushSearch(
        buildViewSearch(
          new URLSearchParams(window.location.search),
          area,
          view,
          targets,
        ),
        replace,
      );
    },
    [pushSearch],
  );

  const consumeAutoBookStart = useCallback(() => setAutoBookStartRequest(null), []);
  const consumeProseOpen = useCallback(() => setProseOpenRequest(null), []);

  const openAutoBook = useCallback(
    (scope: "volume" | "book", volumeId?: string) => {
      setAutoBookStartRequest({ requestId: Date.now(), scope, volumeId });
      navigateView("auto-book", "readiness", volumeId ? { volume: volumeId } : {});
    },
    [navigateView],
  );

  const openWriting = useCallback(
    (chapterId: string, run?: ProseRunSnapshot | null) => {
      if (run !== undefined) {
        setProseOpenRequest({ requestId: Date.now(), chapterId, run });
      }
      navigateView("writing", "chapter", {
        chapter: chapterId,
        run: run?.run_id ?? run?._id,
      });
    },
    [navigateView],
  );

  if (mode === "create") {
    return (
      <div className="h-[calc(100vh-3.5rem)] min-h-0 overflow-hidden">
        <NovelInfoWorkspace mode="create" />
      </div>
    );
  }

  if (!novelId) return null;

  if (!routeReady) {
    return (
      <div className="grid h-[calc(100vh-3.5rem)] place-items-center bg-background px-6 text-center text-sm text-muted">
        {t("loadingRoute")}
      </div>
    );
  }

  const { route, invalidTarget } = resolved;
  const referenceCardType = parseReferenceCardType(route.targets.cardType);
  const writingTabs: ViewTab[] = [
    { view: "chapter", label: t("views.chapter") },
    { view: "revision", label: t("views.revision") },
  ];
  const worldTabs: ViewTab[] = [
    { view: "library", label: t("views.library") },
    { view: "factions", label: t("views.factions") },
    { view: "relationships", label: t("views.relationships") },
    { view: "candidates", label: t("views.candidates") },
  ];
  const continuityTabs: ViewTab[] = [
    { view: "overview", label: t("views.continuityOverview") },
    { view: "facts", label: t("views.facts") },
    { view: "threads", label: t("views.threads") },
    { view: "health", label: t("views.health") },
  ];

  const renderWorkspace = () => {
    if (invalidTarget) {
      return (
        <RouteFailure
          kind={invalidTarget.kind}
          value={invalidTarget.value}
          onOpenDefault={() => navigateView("writing", "chapter")}
        />
      );
    }

    if (route.area === "blueprint") {
      return <NovelInfoWorkspace mode="edit" novelId={novelId} />;
    }

    if (route.area === "writing") {
      if (route.view === "chapter") {
        return (
          <ChapterWorkspace
            mode="edit"
            novelId={novelId}
            onNavigateToReferenceCards={() =>
              navigateView("world", "curation", { cardType: "character" })
            }
            initialChapterId={route.targets.chapter ?? evidenceReference?.chapter_id}
            initialSceneIndex={
              parseSceneIndex(route.targets.scene) ?? evidenceReference?.scene_index
            }
            onStartAutoBook={openAutoBook}
            proseOpenRequest={proseOpenRequest}
            onProseOpenRequestConsumed={consumeProseOpen}
          />
        );
      }
      return (
        <AgentStudioWorkspace
          mode="edit"
          novelId={novelId}
          onNavigateReference={(reference) => {
            setEvidenceReference(reference);
            if (reference.kind === "fact") {
              navigateView("continuity", "facts", { chapter: reference.chapter_id });
            } else if (reference.kind === "thread") {
              navigateView("continuity", "threads", { chapter: reference.chapter_id });
            } else {
              navigateView("writing", "chapter", {
                chapter: reference.chapter_id,
                scene:
                  reference.scene_index === undefined
                    ? undefined
                    : String(reference.scene_index),
              });
            }
          }}
        />
      );
    }

    if (route.area === "auto-book") {
      return (
        <AutoBookWorkspace
          novelId={novelId}
          view={route.view as "readiness" | "runs" | "generation-runs" | "diagnostics"}
          targets={route.targets}
          startRequest={autoBookStartRequest}
          onStartRequestConsumed={consumeAutoBookStart}
          onNavigateView={(view, targets, replace) =>
            navigateView("auto-book", view, targets, replace)
          }
          onOpenWriting={openWriting}
          onOpenWorld={(view, cardType) =>
            navigateView("world", view, cardType ? { cardType } : {})
          }
          onOpenContinuity={(view) => navigateView("continuity", view)}
        />
      );
    }

    if (route.area === "world") {
      if (route.view === "factions") {
        return <FactionCardsWorkspace mode="edit" novelId={novelId} />;
      }
      if (route.view === "relationships") {
        return <RelationshipWorkspace mode="edit" novelId={novelId} />;
      }
      return (
        <ReferenceCardsDestination
          mode="edit"
          novelId={novelId}
          cardType={referenceCardType}
          onCardTypeChange={(cardType) =>
            navigateView(
              "world",
              route.view === "candidates" ? "candidates" : "library",
              { cardType },
              true,
            )
          }
          openCurationOnMount={route.view === "curation"}
          onCurationOpened={() => undefined}
          reviewCandidatesOnMount={route.view === "candidates"}
          onCandidateReviewChange={(open) =>
            navigateView(
              "world",
              open ? "candidates" : "library",
              { cardType: referenceCardType },
              true,
            )
          }
        />
      );
    }

    if (route.view === "facts") {
      return (
        <CharacterMemoryWorkspace
          mode="edit"
          novelId={novelId}
          initialFactId={evidenceReference?.fact_id}
        />
      );
    }
    if (route.view === "threads") {
      return (
        <PlotThreadWorkspace
          mode="edit"
          novelId={novelId}
          initialThreadId={evidenceReference?.thread_id}
        />
      );
    }
    if (route.view === "overview" || route.view === "health") {
      return <StoryHealthWorkspace mode="edit" novelId={novelId} />;
    }
    return (
      <DeferredContinuityView
        onOpenHealth={() => navigateView("continuity", "health")}
        onOpenFacts={() => navigateView("continuity", "facts")}
      />
    );
  };

  return (
    <div className="flex h-[calc(100vh-3.5rem)] min-h-0 flex-col bg-background">
      <WritingNavigation
        activeArea={route.area}
        novelTitle={novel?.title ?? t("untitled")}
        onSelectArea={navigateArea}
      />
      {route.area === "writing" && !invalidTarget && (
        <ViewTabs
          label={t("viewAria")}
          activeView={route.view}
          tabs={writingTabs}
          onSelect={(view) => navigateView("writing", view)}
        />
      )}
      {route.area === "world" && !invalidTarget && (
        <ViewTabs
          label={t("viewAria")}
          activeView={route.view}
          tabs={worldTabs}
          onSelect={(view) =>
            navigateView("world", view, { cardType: referenceCardType })
          }
        />
      )}
      {route.area === "continuity" && !invalidTarget && (
        <ViewTabs
          label={t("viewAria")}
          activeView={route.view}
          tabs={continuityTabs}
          onSelect={(view) => navigateView("continuity", view)}
        />
      )}
      <main className="min-h-0 min-w-0 flex-1 overflow-hidden">
        {renderWorkspace()}
      </main>
    </div>
  );
}
