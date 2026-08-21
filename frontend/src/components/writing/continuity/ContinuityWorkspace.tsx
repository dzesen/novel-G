"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { ApiError, apiGet } from "@/lib/api";
import type {
  WritingRouteTargets,
  WritingTargetKey,
  WritingTargetValidationSource,
  WritingView,
} from "@/lib/writingRoute";
import type { ChapterDetail, VolumeSummary } from "@/types/novel";
import type { AgentRun } from "@/types/agent";
import WorkspaceViewTabs, {
  type WorkspaceViewTab,
} from "../WorkspaceViewTabs";
import CharacterMemoryWorkspace from "../character-memory/CharacterMemoryWorkspace";
import { StateBackfillPanel } from "../chapters/state/StateBackfillPanel";
import StateCompletenessAuditPanel from "../chapters/state/StateCompletenessAuditPanel";
import PlotThreadWorkspace from "../plot-threads/PlotThreadWorkspace";
import StoryHealthWorkspace from "../story-health/StoryHealthWorkspace";
import GenerationToolWorkspace from "../agents/GenerationToolWorkspace";
import AgentRevisionWorkspace from "../agents/AgentRevisionWorkspace";

interface ContinuityWorkspaceProps {
  novelId: string;
  view: WritingView;
  targets: WritingRouteTargets;
  onNavigateView: (
    view: WritingView,
    targets?: WritingRouteTargets,
    replace?: boolean,
  ) => void;
  onOpenWriting: (chapterId: string, sceneIndex?: number) => void;
  onTargetValidation: (
    key: WritingTargetKey,
    value: string,
    valid: boolean,
    source?: WritingTargetValidationSource,
  ) => void;
}

type OwnedRecord = { novel_id?: string };
type OwnedTargetState<T> =
  | { key: string; status: "idle" | "loading" | "missing"; data: null; error: "" }
  | { key: string; status: "ready"; data: T; error: "" }
  | { key: string; status: "error"; data: null; error: string };

function useOwnedTarget<T extends OwnedRecord, TResponse = T>({
  novelId,
  targetKey,
  targetId,
  path,
  selectRecord,
  onTargetValidation,
}: {
  novelId: string;
  targetKey: "chapter" | "volume" | "run";
  targetId?: string;
  path: (id: string) => string;
  selectRecord?: (response: TResponse) => T;
  onTargetValidation: ContinuityWorkspaceProps["onTargetValidation"];
}) {
  const [revision, setRevision] = useState(0);
  const lookupKey = targetId ? `${novelId}:${targetId}:${revision}` : "";
  const [state, setState] = useState<OwnedTargetState<T>>({
    key: "",
    status: "idle",
    data: null,
    error: "",
  });

  useEffect(() => {
    if (!targetId) return;
    let cancelled = false;
    void apiGet<TResponse>(path(targetId))
      .then((response) => {
        if (cancelled) return;
        const record = selectRecord
          ? selectRecord(response)
          : response as unknown as T;
        const valid = String(record.novel_id ?? "") === novelId;
        onTargetValidation(targetKey, targetId, valid);
        setState(
          valid
            ? { key: lookupKey, status: "ready", data: record, error: "" }
            : { key: lookupKey, status: "missing", data: null, error: "" },
        );
      })
      .catch((reason) => {
        if (cancelled) return;
        if (reason instanceof ApiError && [400, 404].includes(reason.status)) {
          onTargetValidation(targetKey, targetId, false);
          setState({ key: lookupKey, status: "missing", data: null, error: "" });
          return;
        }
        setState({
          key: lookupKey,
          status: "error",
          data: null,
          error: reason instanceof Error ? reason.message : String(reason),
        });
      });
    return () => {
      cancelled = true;
    };
  }, [lookupKey, novelId, onTargetValidation, path, revision, selectRecord, targetId, targetKey]);

  const current = state.key === lookupKey
    ? state
    : ({ key: lookupKey, status: targetId ? "loading" : "idle", data: null, error: "" } as OwnedTargetState<T>);
  return { ...current, retry: () => setRevision((value) => value + 1) };
}

export default function ContinuityWorkspace({
  novelId,
  view,
  targets,
  onNavigateView,
  onOpenWriting,
  onTargetValidation,
}: ContinuityWorkspaceProps) {
  const t = useTranslations("writing.navigation");
  const continuityT = useTranslations("writing.continuity");
  const chapterPath = useCallback((id: string) => `/api/chapters/${id}`, []);
  const volumePath = useCallback((id: string) => `/api/volumes/${id}`, []);
  const runPath = useCallback(
    (id: string) => `/api/agent-tools/runs/${encodeURIComponent(id)}`,
    [],
  );
  const selectAgentRun = useCallback(
    (response: { run: AgentRun }) => response.run,
    [],
  );
  const chapterTarget = useOwnedTarget<ChapterDetail>({
    novelId,
    targetKey: "chapter",
    targetId: view === "reviews" ? undefined : targets.chapter,
    path: chapterPath,
    onTargetValidation,
  });
  const volumeTarget = useOwnedTarget<VolumeSummary>({
    novelId,
    targetKey: "volume",
    targetId: view === "reviews" ? undefined : targets.volume,
    path: volumePath,
    onTargetValidation,
  });
  const runTarget = useOwnedTarget<AgentRun, { run: AgentRun }>({
    novelId,
    targetKey: "run",
    targetId: view === "review-history" ? targets.run : undefined,
    path: runPath,
    selectRecord: selectAgentRun,
    onTargetValidation,
  });
  const chapterMatchesVolume =
    targets.chapter &&
    targets.volume &&
    chapterTarget.status === "ready" &&
    volumeTarget.status === "ready"
      ? chapterTarget.data.volume_id === volumeTarget.data._id
      : null;

  useEffect(() => {
    if (!targets.chapter || chapterMatchesVolume === null) return;
    onTargetValidation(
      "chapter",
      targets.chapter,
      chapterMatchesVolume,
      "chapter-volume",
    );
  }, [chapterMatchesVolume, onTargetValidation, targets.chapter]);

  const tabs: WorkspaceViewTab[] = useMemo(
    () => [
      { view: "overview", label: t("views.continuityOverview") },
      { view: "state-issues", label: t("views.stateIssues") },
      { view: "facts", label: t("views.facts") },
      { view: "threads", label: t("views.threads") },
      { view: "reviews", label: t("views.reviews") },
      { view: "review-history", label: t("views.reviewHistory") },
    ],
    [t],
  );
  const activeView = view === "proposals"
    ? "state-issues"
    : view === "health"
      ? "overview"
      : view;

  useEffect(() => {
    if (targets.suggestion) {
      onTargetValidation("suggestion", targets.suggestion, false);
    }
  }, [onTargetValidation, targets.suggestion]);

  const targetFailure = [chapterTarget, volumeTarget, runTarget].find(
    (target) => target.status === "error",
  );
  if (targetFailure?.status === "error") {
    return (
      <div className="grid h-full place-items-center overflow-y-auto px-5 py-10">
        <section className="w-full max-w-xl border-y border-border py-8">
          <h2 className="text-lg font-semibold text-foreground">
            {continuityT("targetLoadFailed")}
          </h2>
          <p role="alert" className="mt-2 break-words text-sm leading-6 text-muted">
            {targetFailure.error}
          </p>
          <button
            type="button"
            onClick={targetFailure.retry}
            className="mt-4 min-h-10 rounded-md bg-accent px-4 text-sm font-semibold text-white hover:bg-accent-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            {t("retry")}
          </button>
        </section>
      </div>
    );
  }
  if (
    chapterTarget.status === "loading" ||
    volumeTarget.status === "loading" ||
    runTarget.status === "loading" ||
    chapterTarget.status === "missing" ||
    volumeTarget.status === "missing" ||
    runTarget.status === "missing"
  ) {
    return (
      <div className="grid h-full place-items-center px-6 text-sm text-muted">
        {continuityT("locatingTarget")}
      </div>
    );
  }

  const content = (() => {
    if (view === "reviews") {
      return (
        <GenerationToolWorkspace
          novelId={novelId}
          tools={["continuity", "style"]}
          initialVolumeId={targets.volume}
          initialChapterId={targets.chapter}
          onTargetValidation={onTargetValidation}
          onScopeTargetChange={(nextTargets) =>
            onNavigateView("reviews", nextTargets, true)
          }
          onOpenHistory={(runId) =>
            onNavigateView("review-history", { run: runId })
          }
          onNavigateReference={(reference) => {
            if (reference.kind === "fact") {
              onNavigateView("facts", {
                chapter: reference.chapter_id,
                issue: reference.fact_id,
              });
            } else if (reference.kind === "thread") {
              onNavigateView("threads", {
                chapter: reference.chapter_id,
                issue: reference.thread_id,
              });
            } else if (reference.chapter_id) {
              onOpenWriting(reference.chapter_id, reference.scene_index);
            }
          }}
        />
      );
    }
    if (view === "review-history") {
      return (
        <div className="h-full overflow-y-auto bg-surface-secondary/40 p-4 md:p-6">
          <div className="mx-auto max-w-7xl">
            <AgentRevisionWorkspace
              novelId={novelId}
              volumes={[]}
              chapters={[]}
              source={null}
              onClearSource={() => undefined}
              initialRunId={targets.run}
              initialRun={
                runTarget.status === "ready" ? runTarget.data : undefined
              }
            />
          </div>
        </div>
      );
    }
    if (view === "facts") {
      return (
        <CharacterMemoryWorkspace
          mode="edit"
          novelId={novelId}
          initialCardId={targets.card}
          initialFactId={targets.issue}
          onCardTargetValidation={(cardId, valid) =>
            onTargetValidation("card", cardId, valid)
          }
          onFactTargetValidation={(factId, valid) =>
            onTargetValidation("issue", factId, valid)
          }
        />
      );
    }
    if (view === "threads") {
      return (
        <PlotThreadWorkspace
          mode="edit"
          novelId={novelId}
          initialThreadId={targets.issue}
          onTargetValidation={(threadId, valid) =>
            onTargetValidation("issue", threadId, valid)
          }
        />
      );
    }
    if (view === "state-issues") {
      return (
        <StateCompletenessAuditPanel
          key={targets.volume ?? "book"}
          novelId={novelId}
          selectedVolumeId={targets.volume ?? null}
          initialChapterId={targets.chapter}
          initialIssueId={targets.issue}
          onIssueTargetValidation={(issueId, valid) =>
            onTargetValidation("issue", issueId, valid)
          }
          onLocate={(chapterId, repair) => {
            if (repair) {
              onNavigateView("proposals", {
                chapter: chapterId,
                issue: undefined,
                run: undefined,
                suggestion: undefined,
              });
            } else {
              onOpenWriting(chapterId);
            }
          }}
        />
      );
    }
    if (view === "proposals") {
      if (!targets.chapter) {
        return (
          <div className="grid h-full place-items-center overflow-y-auto px-5 py-10">
            <section className="w-full max-w-xl border-y border-border py-8">
              <h2 className="text-lg font-semibold text-foreground">
                {continuityT("proposalNeedsChapterTitle")}
              </h2>
              <p className="mt-2 text-sm leading-6 text-muted">
                {continuityT("proposalNeedsChapterBody")}
              </p>
              <button
                type="button"
                onClick={() => onNavigateView("state-issues")}
                className="mt-4 min-h-10 rounded-md bg-accent px-4 text-sm font-semibold text-white hover:bg-accent-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
              >
                {continuityT("openStateIssues")}
              </button>
            </section>
          </div>
        );
      }
      const targetChapter = chapterTarget.status === "ready"
        ? chapterTarget.data
        : null;
      if (!targetChapter) return null;
      if (!targetChapter.content.trim()) {
        return (
          <div className="grid h-full place-items-center overflow-y-auto px-5 py-10">
            <section className="w-full max-w-xl border-y border-border py-8">
              <h2 className="text-lg font-semibold text-foreground">
                {continuityT("proposalNeedsProseTitle")}
              </h2>
              <p className="mt-2 text-sm leading-6 text-muted">
                {continuityT("proposalNeedsProseBody")}
              </p>
              <button
                type="button"
                onClick={() => onOpenWriting(targetChapter._id)}
                className="mt-4 min-h-10 rounded-md bg-accent px-4 text-sm font-semibold text-white hover:bg-accent-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
              >
                {continuityT("openWriting")}
              </button>
            </section>
          </div>
        );
      }
      return (
        <StateBackfillPanel
          novelId={novelId}
          chapterId={targetChapter._id}
          chapterLabel={targetChapter.title}
          closeLabel={continuityT("backToStateIssues")}
          onClose={() =>
            onNavigateView(
              "state-issues",
              {
                issue: undefined,
                run: undefined,
                suggestion: undefined,
              },
              true,
            )
          }
          onAccepted={() => undefined}
        />
      );
    }
    return (
      <StoryHealthWorkspace
        mode="edit"
        novelId={novelId}
        initialIssueId={targets.issue}
        onIssueTargetValidation={(issueId, valid) =>
          onTargetValidation("issue", issueId, valid)
        }
        onOpenThread={(threadId) =>
          onNavigateView("threads", {
            card: undefined,
            chapter: undefined,
            issue: threadId,
            run: undefined,
            suggestion: undefined,
          })
        }
        onOpenChapter={onOpenWriting}
        onOpenCharacterState={(cardId) =>
          onNavigateView("facts", {
            card: cardId,
            chapter: undefined,
            issue: undefined,
            run: undefined,
            suggestion: undefined,
          })
        }
      />
    );
  })();

  return (
    <div className="flex h-full min-h-0 min-w-0 flex-col">
      <WorkspaceViewTabs
        label={t("viewAria")}
        activeView={activeView}
        tabs={tabs}
        onSelect={(nextView) =>
          onNavigateView(nextView, {
            volume: undefined,
            chapter: undefined,
            card: undefined,
            issue: undefined,
            run: undefined,
            suggestion: undefined,
          })
        }
      />
      {runTarget.status === "ready" && (
        <div
          data-testid="continuity-run-audit"
          className="min-w-0 border-b border-border bg-surface-secondary/45 px-4 py-2 text-xs text-muted sm:px-6"
        >
          {continuityT("runAudit", {
            id: runTarget.data.run_id,
            status: runTarget.data.status,
          })}
        </div>
      )}
      <div className="min-h-0 min-w-0 flex-1 overflow-hidden">{content}</div>
    </div>
  );
}
