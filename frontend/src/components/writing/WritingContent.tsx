"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { apiGet } from "@/lib/api";
import {
  buildAreaSearch,
  buildViewSearch,
  defaultWritingRoute,
  legacyWritingRouteSignal,
  reduceLocatedTargetFailure,
  resolveWritingRoute,
  type InvalidWritingTarget,
  type LocatedTargetFailure,
  type WritingArea,
  type WritingRouteTargets,
  type WritingTargetKey,
  type WritingTargetValidationSource,
  type WritingView,
} from "@/lib/writingRoute";
import { buildUserStorageKey } from "@/lib/userStorage";
import type { NovelDetail } from "@/types/novel";
import WritingNavigation from "./WritingNavigation";
import WorkspaceViewTabs, {
  type WorkspaceViewTab,
} from "./WorkspaceViewTabs";
import NovelInfoWorkspace from "./novel-info/NovelInfoWorkspace";
import ChapterWorkspace, {
  type ProseOpenRequest,
} from "./chapters/ChapterWorkspace";
import type { ProseRunSnapshot } from "./chapters/prose/useProseStream";
import AutoBookWorkspace, {
  type AutoBookStartRequest,
} from "./auto-book/AutoBookWorkspace";
import AgentStudioWorkspace from "./agents/AgentStudioWorkspace";
import WorldWorkspace from "./world/WorldWorkspace";
import ContinuityWorkspace from "./continuity/ContinuityWorkspace";

interface WritingContentProps {
  mode: "create" | "edit";
  novelId?: string;
}

function parseSceneIndex(value?: string): number | undefined {
  if (!value) return undefined;
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed >= 0 ? parsed : undefined;
}

function RouteFailure({
  target,
  onBack,
  onOpenDefault,
}: {
  target: InvalidWritingTarget;
  onBack: () => void;
  onOpenDefault: () => void;
}) {
  const t = useTranslations("writing.navigation");
  const kind = target.kind === "target" ? target.key : target.kind;
  return (
    <div className="grid h-full place-items-center overflow-y-auto bg-background px-5 py-10">
      <section className="w-full max-w-xl border-y border-border py-8">
        <h1 className="text-xl font-semibold text-foreground">{t("targetMissingTitle")}</h1>
        <p className="mt-3 text-sm leading-6 text-muted">
          {t("targetMissingBody", {
            kind: t(`targetKinds.${kind}`),
            value: target.value,
          })}
        </p>
        <code className="mt-4 block overflow-x-auto bg-surface-secondary px-3 py-2 text-xs text-foreground">
          {target.value}
        </code>
        <div className="mt-5 flex flex-wrap gap-2">
          <button
            type="button"
            onClick={onOpenDefault}
            className="min-h-10 rounded-md bg-accent px-4 text-sm font-semibold text-white hover:bg-accent-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2"
          >
            {t("openDefault")}
          </button>
          <button
            type="button"
            onClick={onBack}
            className="min-h-10 rounded-md border border-border bg-surface px-4 text-sm font-semibold text-foreground hover:bg-surface-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            {t("backToSource")}
          </button>
        </div>
      </section>
    </div>
  );
}

function NovelLoadFailure({
  novelId,
  onRetry,
  onBackToShelf,
}: {
  novelId: string;
  onRetry: () => void;
  onBackToShelf: () => void;
}) {
  const t = useTranslations("writing.navigation");
  return (
    <div className="grid h-[calc(100vh-3.5rem)] place-items-center overflow-y-auto bg-background px-5 py-10">
      <section className="w-full max-w-xl border-y border-border py-8">
        <h1 className="text-xl font-semibold text-foreground">
          {t("novelUnavailableTitle")}
        </h1>
        <p className="mt-3 text-sm leading-6 text-muted">
          {t("novelUnavailableBody")}
        </p>
        <code className="mt-4 block overflow-x-auto bg-surface-secondary px-3 py-2 text-xs text-foreground">
          {novelId}
        </code>
        <div className="mt-5 flex flex-wrap gap-2">
          <button
            type="button"
            onClick={onRetry}
            className="min-h-10 rounded-md bg-accent px-4 text-sm font-semibold text-white hover:bg-accent-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2"
          >
            {t("retry")}
          </button>
          <button
            type="button"
            onClick={onBackToShelf}
            className="min-h-10 rounded-md border border-border bg-surface px-4 text-sm font-semibold text-foreground hover:bg-surface-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            {t("backToShelf")}
          </button>
        </div>
      </section>
    </div>
  );
}

export default function WritingContent({ mode, novelId }: WritingContentProps) {
  const t = useTranslations("writing.navigation");
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const [novel, setNovel] = useState<NovelDetail | null>(null);
  const [novelLookupComplete, setNovelLookupComplete] = useState(mode === "create");
  const [loadedNovelId, setLoadedNovelId] = useState<string | null>(null);
  const [novelLoadFailed, setNovelLoadFailed] = useState(false);
  const [novelLoadRevision, setNovelLoadRevision] = useState(0);
  const [locatedTargetFailure, setLocatedTargetFailure] =
    useState<LocatedTargetFailure | null>(null);
  const [autoBookStartRequest, setAutoBookStartRequest] =
    useState<AutoBookStartRequest | null>(null);
  const [proseOpenRequest, setProseOpenRequest] =
    useState<ProseOpenRequest | null>(null);
  const recordedLegacySearchesRef = useRef(new Set<string>());

  useEffect(() => {
    if (mode !== "edit" || !novelId) return;
    let cancelled = false;
    void apiGet<NovelDetail>(`/api/novels/${novelId}`)
      .then((result) => {
        if (!cancelled) {
          setNovel(result);
          setNovelLoadFailed(false);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setNovel(null);
          setNovelLoadFailed(true);
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoadedNovelId(novelId);
          setNovelLookupComplete(true);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [mode, novelId, novelLoadRevision]);

  const currentSearch = useMemo(
    () => new URLSearchParams(searchParams.toString()),
    [searchParams],
  );
  const routeReady =
    mode === "create" ||
    (novelLookupComplete && loadedNovelId === novelId);
  const fallbackRoute = defaultWritingRoute(novel?.stats.chapter_count ?? 1);
  const resolved = useMemo(
    () => resolveWritingRoute(currentSearch, fallbackRoute),
    [currentSearch, fallbackRoute],
  );
  const { route, invalidTarget } = resolved;
  const routeValidationContext = JSON.stringify([
    route.area,
    route.view,
    route.targets,
  ]);
  const runtimeInvalidTarget =
    locatedTargetFailure &&
    locatedTargetFailure.routeContext === routeValidationContext &&
    route.targets[locatedTargetFailure.key] === locatedTargetFailure.value
      ? locatedTargetFailure
      : null;

  useEffect(() => {
    if (mode === "create" || !routeReady || !resolved.canonicalSearch) return;
    const nextHref = `${pathname}?${resolved.canonicalSearch}`;
    const currentHref = `${pathname}${window.location.search}`;
    if (nextHref !== currentHref) {
      window.history.replaceState(null, "", nextHref);
    }
  }, [mode, pathname, resolved.canonicalSearch, routeReady]);

  useEffect(() => {
    if (mode !== "edit" || resolved.source !== "legacy") return;
    const serializedSearch = currentSearch.toString();
    if (recordedLegacySearchesRef.current.has(serializedSearch)) return;
    const signal = legacyWritingRouteSignal(currentSearch);
    const storageKey = buildUserStorageKey(
      "migration",
      "writing-route-compat-v1",
    );
    if (!signal || !storageKey) return;
    try {
      const stored = window.localStorage.getItem(storageKey);
      const parsed = stored ? JSON.parse(stored) : {};
      const counts =
        parsed && typeof parsed === "object" && parsed.counts
          ? parsed.counts as Record<string, unknown>
          : {};
      const previous = counts[signal];
      window.localStorage.setItem(
        storageKey,
        JSON.stringify({
          version: 1,
          counts: {
            ...counts,
            [signal]:
              typeof previous === "number" && Number.isFinite(previous)
                ? previous + 1
                : 1,
          },
          last_seen_at: new Date().toISOString(),
        }),
      );
      recordedLegacySearchesRef.current.add(serializedSearch);
    } catch {
      // 兼容计数只用于迁移观测；浏览器存储不可用时不得阻断写作入口。
    }
  }, [currentSearch, mode, resolved.source]);

  const pushSearch = useCallback(
    (search: string, replace = false) => {
      const href = search ? `${pathname}?${search}` : pathname;
      if (`${window.location.pathname}${window.location.search}` === href) return;
      if (replace) window.history.replaceState(null, "", href);
      else window.history.pushState(null, "", href);
    },
    [pathname],
  );

  const navigateArea = useCallback(
    (area: WritingArea, replace = false) => {
      pushSearch(
        buildAreaSearch(new URLSearchParams(window.location.search), area),
        replace,
      );
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
  const retryNovelLoad = useCallback(() => {
    setNovel(null);
    setNovelLoadFailed(false);
    setNovelLookupComplete(false);
    setNovelLoadRevision((current) => current + 1);
  }, []);
  const validateLocatedTarget = useCallback(
    (
      key: WritingTargetKey,
      value: string,
      valid: boolean,
      source: WritingTargetValidationSource = "target",
    ) => {
      setLocatedTargetFailure((current) =>
        reduceLocatedTargetFailure(current, {
          key,
          value,
          valid,
          routeContext: routeValidationContext,
          source,
        }),
      );
    },
    [routeValidationContext],
  );
  const validateChapterTarget = useCallback(
    (chapterId: string, valid: boolean) =>
      validateLocatedTarget("chapter", chapterId, valid),
    [validateLocatedTarget],
  );
  const validateVolumeTarget = useCallback(
    (volumeId: string, valid: boolean) =>
      validateLocatedTarget("volume", volumeId, valid),
    [validateLocatedTarget],
  );
  const validateRunTarget = useCallback(
    (runId: string, valid: boolean) =>
      validateLocatedTarget("run", runId, valid),
    [validateLocatedTarget],
  );
  const validateSceneTarget = useCallback(
    (sceneIndex: number, valid: boolean) =>
      validateLocatedTarget("scene", String(sceneIndex), valid),
    [validateLocatedTarget],
  );

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

  if (novelLoadFailed || !novel) {
    const localeRoot = pathname.startsWith("/en") ? "/en" : "/zh";
    return (
      <NovelLoadFailure
        novelId={novelId}
        onRetry={retryNovelLoad}
        onBackToShelf={() => router.push(localeRoot)}
      />
    );
  }

  const writingTabs: WorkspaceViewTab[] = [
    { view: "chapter", label: t("views.chapter") },
    { view: "revision", label: t("views.revision") },
  ];

  const renderWorkspace = () => {
    const failure = invalidTarget ?? runtimeInvalidTarget;
    if (failure) {
      return (
        <RouteFailure
          target={failure}
          onBack={() => router.back()}
          onOpenDefault={() => navigateArea(route.area, true)}
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
            initialVolumeId={route.targets.volume}
            initialChapterId={route.targets.chapter}
            initialSceneIndex={parseSceneIndex(route.targets.scene)}
            initialRunId={route.targets.run}
            onChapterTargetChange={(chapterId) =>
              navigateView("writing", "chapter", {
                volume: undefined,
                chapter: chapterId,
                scene: undefined,
                run: undefined,
                visual: undefined,
              })
            }
            onChapterTargetValidation={validateChapterTarget}
            onVolumeTargetValidation={validateVolumeTarget}
            onSceneTargetValidation={validateSceneTarget}
            onRunTargetValidation={validateRunTarget}
            onRunTargetChange={(runId) =>
              navigateView(
                "writing",
                "chapter",
                { run: runId },
                true,
              )
            }
            onOpenRunAudit={(chapterId, runId) =>
              navigateView("auto-book", "generation-runs", {
                chapter: chapterId,
                run: runId,
              })
            }
            onOpenStateProposal={(chapterId) =>
              navigateView("continuity", "proposals", {
                chapter: chapterId,
                issue: undefined,
                run: undefined,
                suggestion: undefined,
              })
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
            if (reference.kind === "fact") {
              navigateView("continuity", "facts", {
                chapter: reference.chapter_id,
                issue: reference.fact_id,
              });
            } else if (reference.kind === "thread") {
              navigateView("continuity", "threads", {
                chapter: reference.chapter_id,
                issue: reference.thread_id,
              });
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
          onTargetValidation={validateLocatedTarget}
          onOpenWriting={openWriting}
          onOpenWorld={(view, cardType) =>
            navigateView("world", view, cardType ? { cardType } : {})
          }
          onOpenContinuity={(view) => navigateView("continuity", view)}
        />
      );
    }

    if (route.area === "world") {
      return (
        <WorldWorkspace
          novelId={novelId}
          view={route.view}
          targets={route.targets}
          onNavigateView={(view, targets, replace) =>
            navigateView("world", view, targets, replace)
          }
          onTargetValidation={validateLocatedTarget}
        />
      );
    }
    return (
      <ContinuityWorkspace
        novelId={novelId}
        view={route.view}
        targets={route.targets}
        onNavigateView={(view, targets, replace) =>
          navigateView("continuity", view, targets, replace)
        }
        onOpenWriting={(chapterId) => openWriting(chapterId)}
        onTargetValidation={validateLocatedTarget}
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
      {route.area === "writing" && !invalidTarget && !runtimeInvalidTarget && (
        <WorkspaceViewTabs
          label={t("viewAria")}
          activeView={route.view}
          tabs={writingTabs}
          onSelect={(view) => navigateView("writing", view)}
        />
      )}
      <main className="min-h-0 min-w-0 flex-1 overflow-hidden">
        {renderWorkspace()}
      </main>
    </div>
  );
}
