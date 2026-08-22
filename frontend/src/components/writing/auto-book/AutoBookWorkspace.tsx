"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { apiGet } from "@/lib/api";
import type {
  WritingRouteTargets,
  WritingTargetKey,
  WritingView,
} from "@/lib/writingRoute";
import type {
  ChapterSummary,
  ReferenceCardType,
  VolumeSummary,
} from "@/types/novel";
import type { ProseRunSnapshot } from "../chapters/prose/useProseStream";
import BatchGenerationPanel from "../chapters/batch/BatchGenerationPanel";
import GenerationRunsWorkspace from "../chapters/batch/GenerationRunsWorkspace";
import GenerationDiagnosticsWorkspace from "../chapters/batch/GenerationDiagnosticsWorkspace";
import type {
  GenerationRunsNavigationTarget,
  LeftoverProseRun,
} from "../chapters/batch/batchTypes";
import GenerationToolWorkspace from "../agents/GenerationToolWorkspace";
import { autoBookPurpose } from "./autoBookPresentation";

interface ListResponse<T> {
  data: T[];
}

type AutoBookView = Extract<
  WritingView,
  "readiness" | "runs" | "generation-runs" | "diagnostics" | "retrospective"
>;

export interface AutoBookStartRequest {
  requestId: number;
  scope: "volume" | "book";
  volumeId?: string;
}

interface AutoBookWorkspaceProps {
  novelId: string;
  view: AutoBookView;
  targets: WritingRouteTargets;
  startRequest?: AutoBookStartRequest | null;
  onStartRequestConsumed: () => void;
  onNavigateView: (
    view: AutoBookView,
    targets?: WritingRouteTargets,
    replace?: boolean,
  ) => void;
  onTargetValidation: (
    key: WritingTargetKey,
    value: string,
    valid: boolean,
  ) => void;
  onOpenWriting: (
    chapterId: string,
    run?: ProseRunSnapshot | null,
    runId?: string,
  ) => void;
  onOpenBlueprint: () => void;
  onOpenWorld: (
    view: "library" | "curation" | "candidates" | "baseline",
    targets?: {
      cardType?: ReferenceCardType;
      card?: string;
      candidate?: string;
    },
  ) => void;
  onOpenContinuity: (view: "facts" | "threads") => void;
}

const AUTO_BOOK_VIEWS: AutoBookView[] = [
  "readiness",
  "runs",
  "generation-runs",
  "diagnostics",
  "retrospective",
];

export default function AutoBookWorkspace({
  novelId,
  view,
  targets,
  startRequest,
  onStartRequestConsumed,
  onNavigateView,
  onTargetValidation,
  onOpenWriting,
  onOpenBlueprint,
  onOpenWorld,
  onOpenContinuity,
}: AutoBookWorkspaceProps) {
  const t = useTranslations("writing.autoBook");
  const [volumes, setVolumes] = useState<VolumeSummary[]>([]);
  const [chapters, setChapters] = useState<ChapterSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [structureLoadedNovelId, setStructureLoadedNovelId] =
    useState<string | null>(null);
  const [selectedVolumeId, setSelectedVolumeId] = useState<string | null>(
    targets.volume ?? null,
  );
  const [startScope, setStartScope] = useState<"volume" | "book" | null>(null);
  const startTriggerRef = useRef<HTMLElement | null>(null);
  const [proseRunsRevision, setProseRunsRevision] = useState(0);
  const structureRequestRef = useRef(0);

  const loadStructure = useCallback(async (silent = false) => {
    const requestId = ++structureRequestRef.current;
    if (!silent) {
      setLoading(true);
      setStructureLoadedNovelId(null);
    }
    setLoadError("");
    try {
      const [volumeResponse, chapterResponse] = await Promise.all([
        apiGet<ListResponse<VolumeSummary>>(`/api/volumes/novel/${novelId}`),
        apiGet<ListResponse<ChapterSummary>>(`/api/chapters/novel/${novelId}`),
      ]);
      if (requestId !== structureRequestRef.current) return;
      setVolumes(volumeResponse.data);
      setChapters(chapterResponse.data);
      setStructureLoadedNovelId(novelId);
      setSelectedVolumeId((current) => {
        if (current && volumeResponse.data.some((item) => item._id === current)) {
          return current;
        }
        return volumeResponse.data[0]?._id ?? null;
      });
    } catch (error) {
      if (requestId === structureRequestRef.current) {
        setLoadError(error instanceof Error ? error.message : t("loadFailed"));
      }
    } finally {
      if (requestId === structureRequestRef.current) {
        setLoading(false);
      }
    }
  }, [novelId, t]);

  useEffect(() => {
    void loadStructure();
  }, [loadStructure]);

  useEffect(() => {
    if (structureLoadedNovelId !== novelId) return;
    const requestedVolumeId = targets.volume;
    if (requestedVolumeId) {
      const valid = volumes.some((item) => item._id === requestedVolumeId);
      onTargetValidation("volume", requestedVolumeId, valid);
      setSelectedVolumeId(valid ? requestedVolumeId : null);
      return;
    }
    setSelectedVolumeId((current) => {
      if (current && volumes.some((item) => item._id === current)) return current;
      return volumes[0]?._id ?? null;
    });
  }, [
    novelId,
    onTargetValidation,
    structureLoadedNovelId,
    targets.volume,
    volumes,
  ]);

  useEffect(() => {
    if (!startRequest) return;
    if (startRequest.volumeId) setSelectedVolumeId(startRequest.volumeId);
    startTriggerRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    setStartScope(startRequest.scope);
    onStartRequestConsumed();
  }, [onStartRequestConsumed, startRequest]);

  const generationTarget: GenerationRunsNavigationTarget = {
    jobId: targets.job,
    chapterId: targets.chapter,
    eventId: targets.event,
    runId: targets.run,
  };
  const purpose = autoBookPurpose(view);
  const validateJobTarget = useCallback(
    (jobId: string, valid: boolean) =>
      onTargetValidation("job", jobId, valid),
    [onTargetValidation],
  );
  const openStartDialog = (scope: "volume" | "book") => {
    startTriggerRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    setStartScope(scope);
  };
  const closeStartDialog = () => {
    setStartScope(null);
    window.requestAnimationFrame(() => {
      if (startTriggerRef.current?.isConnected) startTriggerRef.current.focus();
    });
  };
  const openSuccessorReadiness = useCallback((
    scope: "volume" | "book",
    volumeId?: string,
  ) => {
    startTriggerRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    if (volumeId) setSelectedVolumeId(volumeId);
    setStartScope(scope === "volume" && !volumeId ? null : scope);
    onNavigateView(
      "readiness",
      scope === "volume" && volumeId ? { volume: volumeId } : {},
    );
  }, [onNavigateView]);

  const viewNav = (
    <nav
      aria-label={t("viewAria")}
      className="grid min-w-0 grid-cols-4 gap-1 border-b border-border pb-2 sm:flex sm:overflow-x-auto"
    >
      {AUTO_BOOK_VIEWS.map((item, index) => (
        <button
          key={item}
          type="button"
          onClick={() => onNavigateView(item)}
          aria-label={t(`views.${item}`)}
          aria-current={view === item ? "page" : undefined}
          className={[
            "flex min-w-0 items-center justify-center rounded-md font-medium transition-colors",
            index < 4
              ? "min-h-12 flex-col gap-1 px-1 text-[10px] sm:min-h-9 sm:shrink-0 sm:flex-row sm:gap-2 sm:px-3 sm:text-xs"
              : "col-span-4 min-h-8 flex-row gap-2 border-t border-border/70 px-2 pt-1 text-[10px] sm:col-auto sm:min-h-9 sm:shrink-0 sm:border-0 sm:px-3 sm:pt-0 sm:text-xs",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent",
            view === item
              ? "bg-accent/10 text-accent"
              : "text-warm-700 hover:bg-surface-secondary hover:text-foreground dark:text-muted",
          ].join(" ")}
        >
          <span
            aria-hidden="true"
            className={[
              "grid size-5 place-items-center rounded-full text-[10px] tabular-nums",
              view === item
                ? "bg-accent text-white"
                : "bg-surface-secondary text-warm-700 dark:text-muted",
            ].join(" ")}
          >
            {index < 4 ? index + 1 : "β"}
          </span>
          <span className="whitespace-nowrap">{t(`views.${item}`)}</span>
        </button>
      ))}
    </nav>
  );

  if (purpose === "history") {
    return (
      <div className="flex h-full min-h-0 flex-col bg-surface">
        <div className="shrink-0 px-4 pt-3 sm:px-5">{viewNav}</div>
        <div className="min-h-0 flex-1">
          <GenerationRunsWorkspace
            novelId={novelId}
            chapters={chapters}
            chaptersLoading={loading}
            chaptersError={loadError}
            onRetryChapters={() => void loadStructure()}
            volumes={volumes}
            target={generationTarget}
            proseRunsRevision={proseRunsRevision}
            onTargetValidation={onTargetValidation}
            onNavigate={(target) =>
              onNavigateView(
                "generation-runs",
                {
                  job: target.jobId,
                  chapter: target.chapterId,
                  event: target.eventId,
                  run: target.runId,
                },
                true,
              )
            }
            onClose={() => onNavigateView("runs", {}, true)}
            onOpenReadiness={(job) => onNavigateView(
              "readiness",
              job.scope === "volume" && job.volume_id
                ? { volume: job.volume_id }
                : {},
              true,
            )}
            onJumpToChapter={(chapterId, runId) =>
              onOpenWriting(chapterId, undefined, runId)
            }
            readOnly
          />
        </div>
      </div>
    );
  }

  if (purpose === "diagnostics") {
    return (
      <div className="flex h-full min-h-0 flex-col bg-surface">
        <div className="shrink-0 px-4 pt-3 sm:px-5">{viewNav}</div>
        <div className="min-h-0 flex-1">
          <GenerationDiagnosticsWorkspace
            novelId={novelId}
            onOpenRecord={(target) => onNavigateView("generation-runs", {
              job: target.jobId,
              chapter: target.chapterId,
              event: target.eventId,
            })}
            onClose={() => onNavigateView("runs", {}, true)}
          />
        </div>
      </div>
    );
  }

  if (purpose === "retrospective") {
    return (
      <div className="flex h-full min-h-0 flex-col bg-background">
        <div className="shrink-0 px-4 pt-4 sm:px-6">{viewNav}</div>
        <div className="min-h-0 flex-1">
          <GenerationToolWorkspace
            novelId={novelId}
            tools={["retrospective"]}
            initialVolumeId={targets.volume ?? selectedVolumeId ?? undefined}
            onScopeTargetChange={({ volume }) =>
              onNavigateView(
                "retrospective",
                { volume, chapter: undefined },
                true,
              )
            }
          />
        </div>
      </div>
    );
  }

  return (
    <div className="h-full min-h-0 overflow-y-auto bg-background">
      <div className="mx-auto w-full max-w-7xl px-4 py-5 sm:px-6 sm:py-7">
        <div className="flex flex-wrap items-start justify-between gap-4 border-b border-border pb-5">
          <div className="max-w-3xl min-w-0">
            <h1 className="text-xl font-semibold tracking-[-0.02em] text-foreground sm:text-2xl">
              {t(`pages.${view}.title`)}
            </h1>
            <p className="mt-2 text-sm leading-6 text-warm-700 dark:text-muted">
              {t(`pages.${view}.description`)}
            </p>
          </div>
          <div
            role={loading ? "status" : undefined}
            className="flex shrink-0 items-center gap-2 text-xs text-warm-700 dark:text-muted"
          >
            <span className="tabular-nums">
              {loading
                ? t("structureLoading")
                : t("structureCount", {
                    volumes: volumes.length,
                    chapters: chapters.length,
                  })}
            </span>
          </div>
        </div>

        <div className="mt-4">{viewNav}</div>

        {purpose === "start" && (
        <section className="py-5" aria-labelledby="auto-book-start-title">
          <div className="flex flex-wrap items-end gap-3">
            <div className="min-w-0 flex-1 sm:max-w-sm">
              <label htmlFor="auto-book-volume" className="block text-xs font-medium text-warm-700 dark:text-muted">
                {t("volumeLabel")}
              </label>
              <select
                id="auto-book-volume"
                value={selectedVolumeId ?? ""}
                onChange={(event) => {
                  const volumeId = event.target.value || null;
                  setSelectedVolumeId(volumeId);
                  onNavigateView(
                    view,
                    { volume: volumeId ?? undefined },
                    true,
                  );
                }}
                disabled={loading || volumes.length === 0}
                className="mt-1.5 min-h-10 w-full rounded-md border border-border bg-surface px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
              >
                {volumes.length === 0 ? (
                  <option value="">
                    {loading ? t("structureLoadingOption") : t("noVolumes")}
                  </option>
                ) : (
                  volumes.map((volume) => (
                    <option key={volume._id} value={volume._id}>
                      {volume.title}
                    </option>
                  ))
                )}
              </select>
            </div>
            <button
              type="button"
              onClick={() => openStartDialog("volume")}
              disabled={!selectedVolumeId || loading}
              className="min-h-10 rounded-md bg-accent px-4 text-sm font-semibold text-white transition-colors hover:bg-accent-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {t("startVolume")}
            </button>
            <button
              type="button"
              onClick={() => openStartDialog("book")}
              disabled={loading}
              className="min-h-10 rounded-md border border-border bg-surface px-4 text-sm font-semibold text-foreground transition-colors hover:bg-surface-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-not-allowed disabled:opacity-50"
            >
              {t("startBook")}
            </button>
          </div>
          <p id="auto-book-start-title" className="mt-2 text-xs leading-5 text-warm-700 dark:text-muted">
            {t("explicitCostHint")}
          </p>
        </section>
        )}

        {purpose === "operations" && (
          <section className="py-5" aria-labelledby="auto-book-runs-title">
            <h2 id="auto-book-runs-title" className="text-sm font-semibold text-foreground">
              {t("runsTitle")}
            </h2>
            <p className="mt-1 max-w-3xl text-xs leading-5 text-warm-700 dark:text-muted">
              {t("runsDescription")}
            </p>
          </section>
        )}

        {loadError && (
          <div role="alert" className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-md border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-800 dark:border-red-900 dark:bg-red-950 dark:text-red-200">
            <span className="min-w-0">{loadError}</span>
            <button type="button" onClick={() => void loadStructure()} className="shrink-0 font-medium underline underline-offset-4">
              {t("retry")}
            </button>
          </div>
        )}

        <section className="min-w-0 border-t border-border" aria-label={t("operationAria")}>
          <BatchGenerationPanel
            key={`${novelId}:${targets.job ?? "latest"}`}
            surface={purpose === "start" ? "start" : "runs"}
            novelId={novelId}
            initialJobId={targets.job}
            onJobTargetValidation={validateJobTarget}
            selectedVolumeId={selectedVolumeId}
            volumes={volumes}
            chapters={chapters}
            startScope={purpose === "start" ? startScope : null}
            onStartClose={closeStartDialog}
            onJumpToChapter={(chapterId) => onOpenWriting(chapterId)}
            onQuietRefresh={() => void loadStructure(true)}
            onNavigateToMemory={() => onOpenContinuity("facts")}
            onNavigateToBlueprint={onOpenBlueprint}
            onNavigateToWorldBaseline={() => onOpenWorld("baseline")}
            onNavigateToReferenceCards={(cardType = "character", cardId) =>
              onOpenWorld(cardId ? "library" : "curation", {
                cardType,
                card: cardId,
              })
            }
            onNavigateToReferenceCardCandidates={(candidateId) =>
              onOpenWorld("candidates", {
                cardType: "character",
                candidate: candidateId,
              })
            }
            onNavigateToPlotThreads={() => onOpenContinuity("threads")}
            proseRunsRevision={proseRunsRevision}
            onOpenProseRun={(run: LeftoverProseRun) => {
              setProseRunsRevision((current) => current + 1);
              onOpenWriting(run.chapter_id, run);
            }}
            onStartFreshProse={(chapterId) => onOpenWriting(chapterId, null)}
            onOpenSuccessorReadiness={openSuccessorReadiness}
            onOpenGenerationRuns={(target) =>
              onNavigateView("generation-runs", {
                job: target?.jobId,
                chapter: target?.chapterId,
                event: target?.eventId,
              })
            }
            onJobStarted={(started) => onNavigateView("runs", {
              job: started._id,
            })}
          />
        </section>
      </div>
    </div>
  );
}
