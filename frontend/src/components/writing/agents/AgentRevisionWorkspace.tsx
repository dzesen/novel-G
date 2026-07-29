"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";

import { apiGet, apiPost } from "@/lib/api";
import type {
  AgentRevisionPatch,
  AgentRevisionProposal,
  AgentRevisionTarget,
  AgentRevisionTargetKind,
  AgentRun,
} from "@/types/agent";
import type {
  ChapterDetail,
  ChapterSummary,
  VolumeSummary,
} from "@/types/novel";

export interface AgentRevisionSourceSelection {
  runId: string;
  sourceKind: "creative_idea" | "continuity_issue";
  sourceIndex: number;
  label: string;
  contextSnapshot: AgentRun["context_snapshot"];
}

interface Props {
  novelId: string;
  volumes: VolumeSummary[];
  chapters: ChapterSummary[];
  source: AgentRevisionSourceSelection | null;
  onClearSource: () => void;
}

interface VolumeDetail extends VolumeSummary {
  arc?: string;
}

const fieldClass =
  "w-full rounded-lg border border-border bg-surface px-3 py-2 text-sm text-foreground outline-none transition-colors placeholder:text-muted focus:border-accent";

const RUN_CAPABILITY_LABEL_KEYS = {
  creative_inspiration: "creativeRun",
  continuity_review: "continuityRun",
  style_consistency: "styleRun",
  illustration_prompt: "illustrationRun",
  volume_retrospective: "retrospectiveRun",
} as const satisfies Record<AgentRun["capability"], string>;

function compactPatch(
  kind: AgentRevisionTargetKind,
  draft: AgentRevisionPatch,
): AgentRevisionPatch {
  if (kind === "volume_outline") {
    return { summary: draft.summary ?? "", arc: draft.arc ?? "" };
  }
  if (kind === "chapter_outline") {
    return {
      core_conflict: draft.core_conflict ?? "",
      ending_hook: draft.ending_hook ?? "",
    };
  }
  if (kind === "scene") {
    return {
      scene_summary: draft.scene_summary ?? "",
      scene_purpose: draft.scene_purpose ?? "",
    };
  }
  return { content: draft.content ?? "" };
}

function samePatch(
  left: AgentRevisionPatch,
  right: AgentRevisionPatch,
): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

function proposalSourceLabel(proposal: AgentRevisionProposal): string {
  if ("title" in proposal.source) return proposal.source.title;
  return proposal.source.location;
}

export default function AgentRevisionWorkspace({
  novelId,
  volumes,
  chapters,
  source,
  onClearSource,
}: Props) {
  const t = useTranslations("writing.agentStudio.revisions");
  const [runs, setRuns] = useState<AgentRun[]>([]);
  const [proposals, setProposals] = useState<AgentRevisionProposal[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const [targetKind, setTargetKind] =
    useState<AgentRevisionTargetKind>("scene");
  const [targetVolumeId, setTargetVolumeId] = useState("");
  const [targetChapterId, setTargetChapterId] = useState("");
  const [sceneIndex, setSceneIndex] = useState(0);
  const [chapterDetail, setChapterDetail] = useState<ChapterDetail | null>(
    null,
  );
  const [patch, setPatch] = useState<AgentRevisionPatch>({});
  const [baselinePatch, setBaselinePatch] = useState<AgentRevisionPatch>({});
  const [targetLoading, setTargetLoading] = useState(false);

  const loadHistory = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const encodedNovelId = encodeURIComponent(novelId);
      const [runResponse, proposalResponse] = await Promise.all([
        apiGet<{ data: AgentRun[] }>(
          `/api/agent-tools/runs?novel_id=${encodedNovelId}`,
        ),
        apiGet<{ data: AgentRevisionProposal[] }>(
          `/api/agent-tools/proposals?novel_id=${encodedNovelId}`,
        ),
      ]);
      setRuns(runResponse.data);
      setProposals(proposalResponse.data);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [novelId, t]);

  useEffect(() => {
    void loadHistory();
  }, [loadHistory]);

  const sourceRun = runs.find((run) => run.run_id === source?.runId);
  const allowedChapters = useMemo(() => {
    const snapshot = source?.contextSnapshot;
    if (!snapshot) return chapters;
    if (snapshot.scope === "chapter") {
      return chapters.filter(
        (chapter) => chapter._id === snapshot.chapter_id,
      );
    }
    if (snapshot.scope === "volume") {
      return chapters.filter(
        (chapter) => chapter.volume_id === snapshot.volume_id,
      );
    }
    return chapters;
  }, [chapters, source]);

  const allowedVolumes = useMemo(() => {
    const snapshot = source?.contextSnapshot;
    if (!snapshot || snapshot.scope === "novel") return volumes;
    const volumeId =
      snapshot.volume_id ??
      chapters.find((chapter) => chapter._id === snapshot.chapter_id)
        ?.volume_id;
    return volumes.filter((volume) => volume._id === volumeId);
  }, [chapters, source, volumes]);

  const targetKinds = useMemo<AgentRevisionTargetKind[]>(() => {
    if (!source) return [];
    if (source.sourceKind === "continuity_issue") {
      return ["scene", "chapter_prose"];
    }
    if (source.contextSnapshot.scope === "chapter") {
      return ["chapter_outline", "scene"];
    }
    return ["volume_outline", "chapter_outline", "scene"];
  }, [source]);

  useEffect(() => {
    if (!source) return;
    const preferred =
      source.sourceKind === "continuity_issue"
        ? "scene"
        : source.contextSnapshot.scope === "chapter"
          ? "scene"
          : "volume_outline";
    setTargetKind(preferred);
    setTargetVolumeId(
      source.contextSnapshot.volume_id ??
        allowedVolumes[0]?._id ??
        "",
    );
    setTargetChapterId(
      source.contextSnapshot.chapter_id ??
        allowedChapters[0]?._id ??
        "",
    );
    setSceneIndex(0);
    setNotice(null);
    setError(null);
  }, [allowedChapters, allowedVolumes, source]);

  useEffect(() => {
    if (!source) return;
    let cancelled = false;
    setTargetLoading(true);
    const load = async () => {
      try {
        let next: AgentRevisionPatch;
        if (targetKind === "volume_outline") {
          if (!targetVolumeId) {
            next = {};
          } else {
            const volume = await apiGet<VolumeDetail>(
              `/api/volumes/${targetVolumeId}`,
            );
            next = {
              summary: volume.summary ?? "",
              arc: volume.arc ?? "",
            };
          }
          if (!cancelled) setChapterDetail(null);
        } else if (!targetChapterId) {
          next = {};
          if (!cancelled) setChapterDetail(null);
        } else {
          const chapter = await apiGet<ChapterDetail>(
            `/api/chapters/${targetChapterId}`,
          );
          if (!cancelled) setChapterDetail(chapter);
          if (targetKind === "chapter_outline") {
            next = {
              core_conflict: chapter.outline?.core_conflict ?? "",
              ending_hook: chapter.outline?.ending_hook ?? "",
            };
          } else if (targetKind === "scene") {
            const scene = chapter.outline?.scenes?.[sceneIndex];
            next = {
              scene_summary: scene?.summary ?? "",
              scene_purpose: scene?.purpose ?? "",
            };
          } else {
            next = { content: chapter.content ?? "" };
          }
        }
        if (!cancelled) {
          const compact = compactPatch(targetKind, next);
          setPatch(compact);
          setBaselinePatch(compact);
        }
      } catch (caught) {
        if (!cancelled) {
          setError(caught instanceof Error ? caught.message : t("targetFailed"));
          setPatch({});
          setBaselinePatch({});
        }
      } finally {
        if (!cancelled) setTargetLoading(false);
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [
    sceneIndex,
    source,
    t,
    targetChapterId,
    targetKind,
    targetVolumeId,
  ]);

  const sceneCount = chapterDetail?.outline?.scenes?.length ?? 0;
  const preparedPatch = compactPatch(targetKind, patch);
  const changed = !samePatch(preparedPatch, baselinePatch);
  const targetReady =
    targetKind === "volume_outline"
      ? Boolean(targetVolumeId)
      : Boolean(targetChapterId) &&
        (targetKind !== "scene" || sceneIndex < sceneCount);

  const createProposal = async () => {
    if (!source || !targetReady || !changed) return;
    setBusyId("create");
    setError(null);
    setNotice(null);
    const target: AgentRevisionTarget =
      targetKind === "volume_outline"
        ? { kind: targetKind, volume_id: targetVolumeId }
        : targetKind === "scene"
          ? {
              kind: targetKind,
              chapter_id: targetChapterId,
              scene_index: sceneIndex,
            }
          : { kind: targetKind, chapter_id: targetChapterId };
    try {
      await apiPost("/api/agent-tools/proposals", {
        novel_id: novelId,
        run_id: source.runId,
        source_kind: source.sourceKind,
        source_index: source.sourceIndex,
        target,
        patch: preparedPatch,
      });
      setNotice(t("created"));
      onClearSource();
      await loadHistory();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("createFailed"));
    } finally {
      setBusyId(null);
    }
  };

  const decide = async (
    proposal: AgentRevisionProposal,
    action: "apply" | "reject",
  ) => {
    setBusyId(proposal.proposal_id);
    setError(null);
    setNotice(null);
    try {
      await apiPost(
        `/api/agent-tools/proposals/${proposal.proposal_id}/${action}`,
        {
          expected_version: proposal.version,
          reason: action === "reject" ? t("rejectedByAuthor") : "",
        },
      );
      setNotice(action === "apply" ? t("applied") : t("rejected"));
      await loadHistory();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("decisionFailed"));
      await loadHistory();
    } finally {
      setBusyId(null);
    }
  };

  const patchFields = () => {
    if (targetLoading) {
      return <p className="text-sm text-muted">{t("loadingTarget")}</p>;
    }
    if (targetKind === "volume_outline") {
      return (
        <>
          <TextArea
            label={t("fields.summary")}
            value={patch.summary ?? ""}
            onChange={(value) => setPatch({ ...patch, summary: value })}
          />
          <TextArea
            label={t("fields.arc")}
            value={patch.arc ?? ""}
            onChange={(value) => setPatch({ ...patch, arc: value })}
          />
        </>
      );
    }
    if (targetKind === "chapter_outline") {
      return (
        <>
          <TextArea
            label={t("fields.coreConflict")}
            value={patch.core_conflict ?? ""}
            onChange={(value) =>
              setPatch({ ...patch, core_conflict: value })
            }
          />
          <TextArea
            label={t("fields.endingHook")}
            value={patch.ending_hook ?? ""}
            onChange={(value) => setPatch({ ...patch, ending_hook: value })}
          />
        </>
      );
    }
    if (targetKind === "scene") {
      return (
        <>
          <TextArea
            label={t("fields.sceneSummary")}
            value={patch.scene_summary ?? ""}
            onChange={(value) =>
              setPatch({ ...patch, scene_summary: value })
            }
          />
          <TextArea
            label={t("fields.scenePurpose")}
            value={patch.scene_purpose ?? ""}
            onChange={(value) =>
              setPatch({ ...patch, scene_purpose: value })
            }
          />
        </>
      );
    }
    return (
      <label className="block space-y-1.5 text-sm">
        <span className="text-muted">{t("fields.fullProse")}</span>
        <textarea
          className={`${fieldClass} min-h-64 resize-y font-mono leading-6`}
          value={patch.content ?? ""}
          onChange={(event) => setPatch({ content: event.target.value })}
        />
        <span className="block text-xs leading-5 text-amber-700 dark:text-amber-300">
          {t("fullProseWarning")}
        </span>
      </label>
    );
  };

  return (
    <div className="space-y-5">
      {error && (
        <div
          role="alert"
          className="rounded-lg border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-800 dark:border-red-900 dark:bg-red-950 dark:text-red-200"
        >
          {error}
        </div>
      )}
      {notice && (
        <div
          role="status"
          className="rounded-lg border border-green-300 bg-green-50 px-4 py-3 text-sm text-green-800 dark:border-green-900 dark:bg-green-950 dark:text-green-200"
        >
          {notice}
        </div>
      )}

      {source && (
        <section className="rounded-lg border border-accent/40 bg-surface p-5">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <p className="text-xs font-semibold uppercase tracking-[0.14em] text-accent">
                {t("composerEyebrow")}
              </p>
              <h2 className="mt-1 text-lg font-semibold text-foreground">
                {source.label}
              </h2>
              <p className="mt-1 text-xs text-muted">
                {t("sourceMeta", {
                  agent: sourceRun?.agent_id ?? "Agent",
                  version: sourceRun?.agent_version ?? 1,
                  provider: sourceRun?.provider_alias ?? "—",
                  revision: source.contextSnapshot.narrative_revision,
                })}
              </p>
            </div>
            <button
              type="button"
              onClick={onClearSource}
              className="rounded-lg border border-border px-3 py-1.5 text-sm text-muted"
            >
              {t("cancel")}
            </button>
          </div>

          <div className="mt-5 grid gap-4 md:grid-cols-2">
            <label className="space-y-1.5 text-sm">
              <span className="text-muted">{t("targetKind")}</span>
              <select
                className={fieldClass}
                value={targetKind}
                onChange={(event) => {
                  setTargetKind(
                    event.target.value as AgentRevisionTargetKind,
                  );
                  setSceneIndex(0);
                }}
              >
                {targetKinds.map((kind) => (
                  <option key={kind} value={kind}>
                    {t(`target.${kind}`)}
                  </option>
                ))}
              </select>
            </label>

            {targetKind === "volume_outline" ? (
              <label className="space-y-1.5 text-sm">
                <span className="text-muted">{t("targetVolume")}</span>
                <select
                  className={fieldClass}
                  value={targetVolumeId}
                  onChange={(event) => setTargetVolumeId(event.target.value)}
                >
                  {allowedVolumes.map((volume) => (
                    <option key={volume._id} value={volume._id}>
                      {t("volumeLabel", {
                        order: volume.order_index,
                        title: volume.title,
                      })}
                    </option>
                  ))}
                </select>
              </label>
            ) : (
              <label className="space-y-1.5 text-sm">
                <span className="text-muted">{t("targetChapter")}</span>
                <select
                  className={fieldClass}
                  value={targetChapterId}
                  onChange={(event) => {
                    setTargetChapterId(event.target.value);
                    setSceneIndex(0);
                  }}
                >
                  {allowedChapters.map((chapter) => (
                    <option key={chapter._id} value={chapter._id}>
                      {t("chapterLabel", {
                        order: chapter.order_index,
                        title: chapter.title,
                      })}
                    </option>
                  ))}
                </select>
              </label>
            )}

            {targetKind === "scene" && (
              <label className="space-y-1.5 text-sm md:col-span-2">
                <span className="text-muted">{t("targetScene")}</span>
                <select
                  className={fieldClass}
                  value={sceneIndex}
                  onChange={(event) =>
                    setSceneIndex(Number(event.target.value))
                  }
                >
                  {Array.from({ length: sceneCount }, (_, index) => (
                    <option key={index} value={index}>
                      {t("sceneLabel", {
                        order: index + 1,
                        summary:
                          chapterDetail?.outline?.scenes?.[index]?.summary ??
                          "",
                      })}
                    </option>
                  ))}
                </select>
                {sceneCount === 0 && (
                  <span className="block text-xs text-amber-700 dark:text-amber-300">
                    {t("noScenes")}
                  </span>
                )}
              </label>
            )}
          </div>

          <div className="mt-4 grid gap-4 md:grid-cols-2">
            {patchFields()}
          </div>
          <div className="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-border pt-4">
            <p className="text-xs leading-5 text-muted">{t("staleHint")}</p>
            <button
              type="button"
              disabled={
                busyId !== null ||
                targetLoading ||
                !targetReady ||
                !changed
              }
              onClick={() => void createProposal()}
              className="rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-50"
            >
              {busyId === "create" ? t("creating") : t("create")}
            </button>
          </div>
        </section>
      )}

      <section className="rounded-lg border border-border bg-surface p-5">
        <h2 className="text-lg font-semibold text-foreground">
          {t("proposalTitle")}
        </h2>
        <p className="mt-1 text-sm text-muted">{t("proposalDescription")}</p>
        {loading ? (
          <p className="mt-5 text-sm text-muted">{t("loading")}</p>
        ) : proposals.length === 0 ? (
          <p className="mt-5 rounded-lg bg-surface-secondary px-4 py-6 text-center text-sm text-muted">
            {t("proposalEmpty")}
          </p>
        ) : (
          <ol className="mt-4 space-y-3">
            {proposals.map((proposal) => (
              <li
                key={proposal.proposal_id}
                className="rounded-lg border border-border p-4"
              >
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <div className="flex flex-wrap items-center gap-2">
                      <h3 className="font-medium text-foreground">
                        {proposalSourceLabel(proposal)}
                      </h3>
                      <span className="rounded bg-surface-secondary px-2 py-0.5 text-xs text-muted">
                        {t(`status.${proposal.status}`)}
                      </span>
                    </div>
                    <p className="mt-1 text-xs text-muted">
                      {t("proposalMeta", {
                        target: t(`target.${proposal.target.kind}`),
                        agent: proposal.agent.agent_id,
                        version: proposal.agent.agent_version,
                        provider: proposal.agent.provider_alias ?? "—",
                      })}
                    </p>
                  </div>
                  {proposal.status === "proposed" && (
                    <div className="flex gap-2">
                      <button
                        type="button"
                        disabled={busyId !== null}
                        onClick={() => void decide(proposal, "reject")}
                        className="rounded-lg border border-border px-3 py-1.5 text-sm text-muted disabled:opacity-50"
                      >
                        {t("reject")}
                      </button>
                      <button
                        type="button"
                        disabled={busyId !== null}
                        onClick={() => void decide(proposal, "apply")}
                        className="rounded-lg bg-accent px-3 py-1.5 text-sm font-semibold text-white disabled:opacity-50"
                      >
                        {t("apply")}
                      </button>
                    </div>
                  )}
                </div>
                <pre className="mt-3 overflow-x-auto whitespace-pre-wrap rounded bg-surface-secondary px-3 py-2 text-xs leading-5 text-muted">
                  {JSON.stringify(proposal.patch, null, 2)}
                </pre>
                {proposal.stale_reason && (
                  <p className="mt-2 text-sm text-amber-700 dark:text-amber-300">
                    {proposal.stale_reason}
                  </p>
                )}
                {proposal.acceptance && (
                  <p className="mt-2 text-xs text-muted">
                    {t("acceptedAudit", {
                      before:
                        proposal.acceptance.narrative_revision_before,
                      after: proposal.acceptance.narrative_revision_after,
                    })}
                  </p>
                )}
              </li>
            ))}
          </ol>
        )}
      </section>

      <section className="rounded-lg border border-border bg-surface p-5">
        <h2 className="text-lg font-semibold text-foreground">
          {t("runTitle")}
        </h2>
        <p className="mt-1 text-sm text-muted">{t("runDescription")}</p>
        {!loading && runs.length === 0 ? (
          <p className="mt-5 rounded-lg bg-surface-secondary px-4 py-6 text-center text-sm text-muted">
            {t("runEmpty")}
          </p>
        ) : (
          <ol className="mt-4 divide-y divide-border">
            {runs.map((run) => (
              <li
                key={run.run_id}
                className="flex flex-wrap items-start justify-between gap-3 py-3 first:pt-0 last:pb-0"
              >
                <div>
                  <p className="text-sm font-medium text-foreground">
                    {t(RUN_CAPABILITY_LABEL_KEYS[run.capability])}
                  </p>
                  <p className="mt-1 text-xs text-muted">
                    {t("runMeta", {
                      agent: run.agent_id,
                      version: run.agent_version,
                      provider: run.provider_alias ?? "—",
                      revision: run.context_snapshot.narrative_revision,
                    })}
                  </p>
                  {run.error?.message && (
                    <p className="mt-1 text-xs text-red-600">
                      {run.error.message}
                    </p>
                  )}
                </div>
                <div className="text-right text-xs text-muted">
                  <p>{t(`runStatus.${run.status}`)}</p>
                  <p className="mt-1">
                    {t("usage", {
                      tokens: run.usage.total_tokens ?? 0,
                      attempts: run.attempts.length,
                    })}
                  </p>
                </div>
              </li>
            ))}
          </ol>
        )}
      </section>
    </div>
  );
}

function TextArea({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="block space-y-1.5 text-sm">
      <span className="text-muted">{label}</span>
      <textarea
        className={`${fieldClass} min-h-28 resize-y`}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  );
}
