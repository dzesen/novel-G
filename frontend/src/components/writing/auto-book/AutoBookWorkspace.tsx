"use client";

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { apiGet } from "@/lib/api";
import type { WritingRouteTargets, WritingView } from "@/lib/writingRoute";
import type { ChapterSummary, VolumeSummary } from "@/types/novel";
import type { ProseRunSnapshot } from "../chapters/prose/useProseStream";
import BatchGenerationPanel from "../chapters/batch/BatchGenerationPanel";
import GenerationRunsWorkspace from "../chapters/batch/GenerationRunsWorkspace";
import type {
  GenerationRunsNavigationTarget,
  LeftoverProseRun,
} from "../chapters/batch/batchTypes";

interface ListResponse<T> {
  data: T[];
}

type AutoBookView = Extract<
  WritingView,
  "readiness" | "runs" | "generation-runs" | "diagnostics"
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
  onOpenWriting: (chapterId: string, run?: ProseRunSnapshot | null) => void;
  onOpenWorld: (
    view: "library" | "curation" | "candidates",
    cardType?: string,
  ) => void;
  onOpenContinuity: (view: "facts" | "threads") => void;
}

const AUTO_BOOK_VIEWS: AutoBookView[] = [
  "readiness",
  "runs",
  "generation-runs",
  "diagnostics",
];

export default function AutoBookWorkspace({
  novelId,
  view,
  targets,
  startRequest,
  onStartRequestConsumed,
  onNavigateView,
  onOpenWriting,
  onOpenWorld,
  onOpenContinuity,
}: AutoBookWorkspaceProps) {
  const t = useTranslations("writing.autoBook");
  const [volumes, setVolumes] = useState<VolumeSummary[]>([]);
  const [chapters, setChapters] = useState<ChapterSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [selectedVolumeId, setSelectedVolumeId] = useState<string | null>(
    targets.volume ?? null,
  );
  const [startScope, setStartScope] = useState<"volume" | "book" | null>(null);
  const [proseRunsRevision, setProseRunsRevision] = useState(0);

  const loadStructure = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    setLoadError("");
    try {
      const [volumeResponse, chapterResponse] = await Promise.all([
        apiGet<ListResponse<VolumeSummary>>(`/api/volumes/novel/${novelId}`),
        apiGet<ListResponse<ChapterSummary>>(`/api/chapters/novel/${novelId}`),
      ]);
      setVolumes(volumeResponse.data);
      setChapters(chapterResponse.data);
      setSelectedVolumeId((current) => {
        const requested = targets.volume;
        if (requested && volumeResponse.data.some((item) => item._id === requested)) {
          return requested;
        }
        if (current && volumeResponse.data.some((item) => item._id === current)) {
          return current;
        }
        return volumeResponse.data[0]?._id ?? null;
      });
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : t("loadFailed"));
    } finally {
      if (!silent) setLoading(false);
    }
  }, [novelId, t, targets.volume]);

  useEffect(() => {
    void loadStructure();
  }, [loadStructure]);

  useEffect(() => {
    if (!startRequest) return;
    if (startRequest.volumeId) setSelectedVolumeId(startRequest.volumeId);
    setStartScope(startRequest.scope);
    onStartRequestConsumed();
  }, [onStartRequestConsumed, startRequest]);

  const generationTarget: GenerationRunsNavigationTarget = {
    jobId: targets.job,
    chapterId: targets.chapter,
    eventId: targets.event,
  };

  if (view === "generation-runs" || view === "diagnostics") {
    return (
      <GenerationRunsWorkspace
        novelId={novelId}
        chapters={chapters}
        chaptersLoading={loading}
        volumes={volumes}
        target={generationTarget}
        proseRunsRevision={proseRunsRevision}
        onNavigate={(target) =>
          onNavigateView(
            view,
            {
              job: target.jobId,
              chapter: target.chapterId,
              event: target.eventId,
            },
            true,
          )
        }
        onClose={() => onNavigateView("runs", {}, true)}
        onJumpToChapter={(chapterId) => onOpenWriting(chapterId)}
        onOpenProseRun={(run) => onOpenWriting(run.chapter_id, run)}
        onStartFreshProse={(chapterId) => onOpenWriting(chapterId, null)}
      />
    );
  }

  return (
    <div className="h-full min-h-0 overflow-y-auto bg-background">
      <div className="mx-auto w-full max-w-7xl px-4 py-5 sm:px-6 sm:py-7">
        <div className="flex flex-wrap items-start justify-between gap-4 border-b border-border pb-5">
          <div className="max-w-3xl min-w-0">
            <h1 className="text-xl font-semibold tracking-[-0.02em] text-foreground sm:text-2xl">
              {t("title")}
            </h1>
            <p className="mt-2 text-sm leading-6 text-muted">{t("description")}</p>
          </div>
          <div className="flex shrink-0 items-center gap-2 text-xs text-muted">
            <span className="tabular-nums">
              {t("structureCount", {
                volumes: volumes.length,
                chapters: chapters.length,
              })}
            </span>
          </div>
        </div>

        <nav
          aria-label={t("viewAria")}
          className="mt-4 flex min-w-0 gap-1 overflow-x-auto border-b border-border pb-2"
        >
          {AUTO_BOOK_VIEWS.map((item) => (
            <button
              key={item}
              type="button"
              onClick={() => onNavigateView(item)}
              aria-current={view === item ? "page" : undefined}
              className={[
                "min-h-9 shrink-0 rounded-md px-3 text-xs font-medium transition-colors",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent",
                view === item
                  ? "bg-accent/10 text-accent"
                  : "text-muted hover:bg-surface-secondary hover:text-foreground",
              ].join(" ")}
            >
              {t(`views.${item}`)}
            </button>
          ))}
        </nav>

        <section className="py-5" aria-labelledby="auto-book-start-title">
          <div className="flex flex-wrap items-end gap-3">
            <div className="min-w-0 flex-1 sm:max-w-sm">
              <label htmlFor="auto-book-volume" className="block text-xs font-medium text-muted">
                {t("volumeLabel")}
              </label>
              <select
                id="auto-book-volume"
                value={selectedVolumeId ?? ""}
                onChange={(event) => setSelectedVolumeId(event.target.value || null)}
                disabled={loading || volumes.length === 0}
                className="mt-1.5 min-h-10 w-full rounded-md border border-border bg-surface px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
              >
                {volumes.length === 0 ? (
                  <option value="">{t("noVolumes")}</option>
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
              onClick={() => setStartScope("volume")}
              disabled={!selectedVolumeId || loading}
              className="min-h-10 rounded-md bg-accent px-4 text-sm font-semibold text-white transition-colors hover:bg-accent-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {t("startVolume")}
            </button>
            <button
              type="button"
              onClick={() => setStartScope("book")}
              disabled={loading}
              className="min-h-10 rounded-md border border-border bg-surface px-4 text-sm font-semibold text-foreground transition-colors hover:bg-surface-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-not-allowed disabled:opacity-50"
            >
              {t("startBook")}
            </button>
          </div>
          <p id="auto-book-start-title" className="mt-2 text-xs leading-5 text-muted">
            {t("explicitCostHint")}
          </p>
        </section>

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
            novelId={novelId}
            selectedVolumeId={selectedVolumeId}
            volumes={volumes}
            chapters={chapters}
            startScope={startScope}
            onStartClose={() => setStartScope(null)}
            onJumpToChapter={(chapterId) => onOpenWriting(chapterId)}
            onQuietRefresh={() => void loadStructure(true)}
            onNavigateToMemory={() => onOpenContinuity("facts")}
            onNavigateToReferenceCards={() => onOpenWorld("curation", "character")}
            onNavigateToReferenceCardCandidates={() => onOpenWorld("candidates", "character")}
            onNavigateToPlotThreads={() => onOpenContinuity("threads")}
            proseRunsRevision={proseRunsRevision}
            onOpenProseRun={(run: LeftoverProseRun) => {
              setProseRunsRevision((current) => current + 1);
              onOpenWriting(run.chapter_id, run);
            }}
            onStartFreshProse={(chapterId) => onOpenWriting(chapterId, null)}
            onOpenGenerationRuns={(target) =>
              onNavigateView("generation-runs", {
                job: target?.jobId,
                chapter: target?.chapterId,
                event: target?.eventId,
              })
            }
          />
        </section>
      </div>
    </div>
  );
}
