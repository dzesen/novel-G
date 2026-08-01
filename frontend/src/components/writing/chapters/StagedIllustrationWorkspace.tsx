"use client";

import { Button } from "@heroui/react";
import Image from "next/image";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useMemo, useState } from "react";
import { apiGet, apiPatch, apiPost, apiPostRaw, getImageUrl } from "@/lib/api";
import type { IllustrationPromptResult } from "@/types/agent";
import type { ConfigView, ImagePipelineStatusView } from "@/types/config";
import type {
  CharacterVisualProfile,
  IllustrationBrief,
  IllustrationCandidate,
  IllustrationReadiness,
  IllustrationRun,
  IllustrationStageJob,
  IllustrationStageName,
  SceneIllustrationCharacter,
  ImageAsset,
} from "@/types/image";
import type { StoredChapterOutline } from "./outline/outlineTypes";

const STAGES: IllustrationStageName[] = ["compose", "identity_edit", "refine"];
type CandidateMap = Record<IllustrationStageName, IllustrationCandidate[]>;
const EMPTY_CANDIDATES: CandidateMap = { compose: [], identity_edit: [], refine: [] };

type Props = {
  novelId: string;
  chapterId: string;
  chapterTitle: string;
  outline: StoredChapterOutline;
  characters: SceneIllustrationCharacter[];
  selectedCharacterIds: string[];
  referenceCharacterId: string;
  prompt: IllustrationPromptResult | null;
  legacyAssets: ImageAsset[];
};

function message(reason: unknown, fallback: string): string {
  return reason instanceof Error ? reason.message : fallback;
}

export default function StagedIllustrationWorkspace(props: Props) {
  const {
    novelId, chapterId, chapterTitle, outline, characters,
    selectedCharacterIds, referenceCharacterId, prompt, legacyAssets,
  } = props;
  const t = useTranslations("writing.sceneIllustration.staged");
  const briefsPath = `/api/novels/${novelId}/chapters/${chapterId}/illustration-briefs`;
  const [briefs, setBriefs] = useState<IllustrationBrief[]>([]);
  const [briefId, setBriefId] = useState("");
  const [runs, setRuns] = useState<IllustrationRun[]>([]);
  const [runId, setRunId] = useState("");
  const [candidates, setCandidates] = useState<CandidateMap>(EMPTY_CANDIDATES);
  const [pipelines, setPipelines] = useState<ImagePipelineStatusView[]>([]);
  const [pipelineAlias, setPipelineAlias] = useState("");
  const [profile, setProfile] = useState<CharacterVisualProfile | null>(null);
  const [referenceAssetId, setReferenceAssetId] = useState("");
  const [readiness, setReadiness] = useState<IllustrationReadiness | null>(null);
  const [job, setJob] = useState<IllustrationStageJob | null>(null);
  const [sceneIndex, setSceneIndex] = useState(0);
  const [showArchived, setShowArchived] = useState(false);
  const [ackQuality, setAckQuality] = useState(false);
  const [useAdapter, setUseAdapter] = useState(false);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [finalizeOpen, setFinalizeOpen] = useState(false);
  const [externalFile, setExternalFile] = useState<File | null>(null);
  const [identityInstruction, setIdentityInstruction] = useState(t("identityInstructionDefault"));
  const [mustPreserve, setMustPreserve] = useState(t("mustPreserveDefault"));
  const [refineStrength, setRefineStrength] = useState(0.28);

  const brief = useMemo(() => briefs.find((item) => item.brief_id === briefId) ?? null, [briefId, briefs]);
  const run = useMemo(() => runs.find((item) => item.run_id === runId) ?? null, [runId, runs]);
  const pipeline = useMemo(() => pipelines.find((item) => item.alias === pipelineAlias) ?? null, [pipelineAlias, pipelines]);
  const referenceAssets = useMemo(() => {
    if (!profile) return [];
    const ids = profile.references.map((item) => item.asset_id);
    const anchor = profile.appearance_anchor?.reference_asset;
    if (anchor && !ids.includes(anchor)) ids.unshift(anchor);
    return ids;
  }, [profile]);

  const loadBriefs = useCallback(async () => {
    const result = await apiGet<{ data: IllustrationBrief[] }>(`${briefsPath}?include_archived=${showArchived}`);
    setBriefs(result.data);
    setBriefId((current) => result.data.some((item) => item.brief_id === current) ? current : result.data[0]?.brief_id ?? "");
  }, [briefsPath, showArchived]);

  const loadRuns = useCallback(async () => {
    if (!briefId) {
      setRuns([]);
      setRunId("");
      return;
    }
    const result = await apiGet<{ data: IllustrationRun[] }>(`${briefsPath}/${briefId}/runs`);
    setRuns(result.data);
    setRunId((current) => result.data.some((item) => item.run_id === current) ? current : result.data[0]?.run_id ?? "");
  }, [briefId, briefsPath]);

  const loadCandidates = useCallback(async (targetRunId: string) => {
    const results = await Promise.all(STAGES.map((stage) => apiGet<{ data: IllustrationCandidate[] }>(
      `/api/novels/${novelId}/illustration-runs/${targetRunId}/candidates?stage=${stage}`,
    )));
    setCandidates({ compose: results[0].data, identity_edit: results[1].data, refine: results[2].data });
  }, [novelId]);

  const replaceRun = useCallback((next: IllustrationRun) => {
    setRuns((current) => [next, ...current.filter((item) => item.run_id !== next.run_id)]);
    setRunId(next.run_id);
  }, []);

  useEffect(() => {
    let active = true;
    void Promise.all([loadBriefs(), apiGet<ConfigView>("/api/config")])
      .then(([, config]) => {
        if (!active) return;
        const statuses = config.image_pipeline_statuses ?? [];
        setPipelines(statuses);
        setPipelineAlias(config.editable_data.image_providers.default_scene_pipeline || statuses[0]?.alias || "");
      })
      .catch((reason) => active && setError(message(reason, t("loadFailed"))));
    return () => { active = false; };
  }, [loadBriefs, t]);

  useEffect(() => {
    setReadiness(null);
    setFinalizeOpen(false);
    void loadRuns().catch((reason) => setError(message(reason, t("runsLoadFailed"))));
  }, [loadRuns, t]);

  useEffect(() => {
    const cardId = brief?.default_reference_character_card_id;
    if (!cardId) {
      setProfile(null);
      setReferenceAssetId("");
      return;
    }
    let active = true;
    void apiGet<CharacterVisualProfile>(`/api/reference-cards/novel/${novelId}/character/${cardId}/visual-profile`)
      .then((next) => {
        if (!active) return;
        setProfile(next);
        setReferenceAssetId(next.appearance_anchor?.reference_asset || next.references[0]?.asset_id || "");
        setUseAdapter(false);
      })
      .catch((reason) => active && setError(message(reason, t("profileLoadFailed"))));
    return () => { active = false; };
  }, [brief, novelId, t]);

  useEffect(() => {
    if (!runId) {
      setCandidates(EMPTY_CANDIDATES);
      setJob(null);
      return;
    }
    void loadCandidates(runId).catch((reason) => setError(message(reason, t("candidatesLoadFailed"))));
    const current = runs.find((item) => item.run_id === runId);
    const stage = STAGES.find((name) => current?.stages[name].status === "running");
    const jobId = stage ? current?.stages[stage].latest_job_id : null;
    if (current && jobId) {
      void apiGet<IllustrationStageJob>(`/api/novels/${novelId}/illustration-runs/${current.run_id}/jobs/${jobId}`)
        .then(setJob).catch(() => undefined);
    } else setJob(null);
  }, [loadCandidates, novelId, runId, runs, t]);

  useEffect(() => {
    if (!job || job.job.terminal) return;
    const timer = window.setTimeout(() => {
      void apiGet<IllustrationStageJob>(`/api/novels/${novelId}/illustration-runs/${job.run.run_id}/jobs/${job.job.job_id}`)
        .then((next) => {
          setJob(next);
          replaceRun(next.run);
          if (next.job.terminal) void loadCandidates(next.run.run_id);
        })
        .catch((reason) => setError(message(reason, t("pollFailed"))));
    }, 1200);
    return () => window.clearTimeout(timer);
  }, [job, loadCandidates, novelId, replaceRun, t]);

  const perform = async (label: string, action: () => Promise<void>) => {
    if (busy) return;
    setBusy(label); setError(null); setNotice(null);
    try { await action(); }
    catch (reason) { setError(message(reason, t("actionFailed"))); }
    finally { setBusy(""); }
  };
  const createBrief = () => perform("create-brief", async () => {
    const scene = outline.scenes[sceneIndex];
    if (!scene) throw new Error(t("sceneMissing"));
    const cardIds = selectedCharacterIds.length ? selectedCharacterIds : outline.present_character_card_ids;
    if (!cardIds.length) throw new Error(t("charactersRequired"));
    if (!referenceCharacterId || !cardIds.includes(referenceCharacterId)) throw new Error(t("referenceRequired"));
    const created = await apiPost<IllustrationBrief>(briefsPath, {
      title: scene.summary.trim().slice(0, 200) || `${chapterTitle} #${sceneIndex + 1}`,
      source_scene_index: sceneIndex,
      scene_character_card_ids: cardIds,
      default_reference_character_card_id: referenceCharacterId,
      default_pipeline_alias: pipelineAlias || null,
      sort_order: briefs.length,
    });
    setBriefs((current) => [...current, created].sort((a, b) => a.sort_order - b.sort_order));
    setBriefId(created.brief_id);
    setNotice(t("briefCreated"));
  });

  const patchBrief = (changes: Record<string, unknown>) => {
    if (!brief) return;
    void perform("patch-brief", async () => {
      const updated = await apiPatch<IllustrationBrief>(`${briefsPath}/${brief.brief_id}`, {
        expected_revision: brief.revision, ...changes,
      });
      setBriefs((current) => current.map((item) => item.brief_id === updated.brief_id ? updated : item));
      setNotice(t("briefUpdated"));
    });
  };

  const refreshBrief = () => {
    if (!brief) return;
    void perform("refresh-brief", async () => {
      const updated = await apiPost<IllustrationBrief>(`${briefsPath}/${brief.brief_id}/refresh-from-outline`, {
        expected_revision: brief.revision,
        source_scene_index: brief.scene_snapshot.source_scene_index,
        scene_character_card_ids: brief.scene_character_card_ids,
        default_reference_character_card_id: brief.default_reference_character_card_id,
      });
      setBriefs((current) => current.map((item) => item.brief_id === updated.brief_id ? updated : item));
      setNotice(t("briefRefreshed"));
    });
  };

  const createRun = (parent?: IllustrationRun) => perform("create-run", async () => {
    if (!brief || !referenceAssetId || !pipelineAlias) throw new Error(t("runPrerequisites"));
    if (pipeline?.quality_status !== "accepted" && !ackQuality) throw new Error(t("qualityAcknowledgeRequired"));
    const created = await apiPost<IllustrationRun>(`${briefsPath}/${brief.brief_id}/runs`, {
      pipeline_alias: pipelineAlias,
      reference_asset_id: referenceAssetId,
      parent_run_id: parent?.run_id ?? null,
      branch_from_asset_id: parent?.final_asset_id ?? null,
    });
    setRuns((current) => [created, ...current]);
    setRunId(created.run_id); setReadiness(null);
    setNotice(t(parent ? "branchCreated" : "runCreated"));
  });

  const inspectReadiness = () => {
    if (!run) return;
    void perform("readiness", async () => {
      const report = await apiGet<IllustrationReadiness>(`/api/novels/${novelId}/illustration-runs/${run.run_id}/readiness`);
      setReadiness(report);
      setNotice(t(report.status === "passed" ? "readinessPassed" : "readinessBlocked"));
    });
  };

  const startStage = (stage: IllustrationStageName) => {
    if (!run) return;
    void perform(`start-${stage}`, async () => {
      if (run.pipeline_snapshot.kind === "consistency" &&
          (!readiness || readiness.status !== "passed" || readiness.run_revision !== run.revision)) {
        throw new Error(t("readinessRequired"));
      }
      const payload: Record<string, unknown> = {
        expected_revision: run.revision,
        attempt_id: crypto.randomUUID(),
        readiness_digest: run.pipeline_snapshot.kind === "consistency" ? readiness?.readiness_digest ?? null : null,
        seed: null,
        use_external_adapter: stage === "compose" && useAdapter,
      };
      if (stage === "compose") {
        if (!prompt) throw new Error(t("promptRequired"));
        payload.prompt = prompt;
      } else if (stage === "identity_edit") {
        payload.identity_instruction = {
          edit_instruction: identityInstruction,
          must_preserve: mustPreserve,
          allowed_changes: "",
        };
      } else {
        payload.refine_instruction = {
          goal: "final_polish",
          strength: refineStrength,
          supplemental_instruction: "",
        };
      }
      const result = await apiPost<IllustrationStageJob>(
        `/api/novels/${novelId}/illustration-runs/${run.run_id}/stages/${stage}/start`, payload,
      );
      setJob(result); replaceRun(result.run);
      setNotice(t("stageStarted", { stage: t(`stage.${stage}`) }));
    });
  };

  const cancelStageJob = () => {
    if (!job || job.job.terminal) return;
    void perform("cancel-job", async () => {
      const result = await apiPost<IllustrationStageJob>(
        `/api/novels/${novelId}/illustration-runs/${job.run.run_id}/jobs/${job.job.job_id}/cancel`,
        {},
      );
      setJob(result);
      replaceRun(result.run);
      if (result.job.terminal) await loadCandidates(result.run.run_id);
      setNotice(t("jobCancelled"));
    });
  };

  const selectCandidate = (candidate: IllustrationCandidate) => {
    if (!run) return;
    void perform(`select-${candidate.asset_id}`, async () => {
      const result = await apiPost<{ run: IllustrationRun; candidate: IllustrationCandidate }>(
        `/api/novels/${novelId}/illustration-runs/${run.run_id}/candidates/${candidate.asset_id}/select`,
        { expected_revision: run.revision },
      );
      replaceRun(result.run); await loadCandidates(result.run.run_id); setReadiness(null);
      setNotice(t("candidateSelected"));
    });
  };

  const mutateCandidate = (candidate: IllustrationCandidate, action: "discard" | "restore") => {
    if (!run) return;
    void perform(`${action}-${candidate.asset_id}`, async () => {
      const result = await apiPost<{ run: IllustrationRun; candidate: IllustrationCandidate }>(
        `/api/novels/${novelId}/illustration-candidates/${candidate.asset_id}/${action}`,
        action === "discard"
          ? { expected_revision: run.revision, reason: "discarded_from_chapter_workspace" }
          : { expected_revision: run.revision },
      );
      replaceRun(result.run); await loadCandidates(result.run.run_id); setReadiness(null);
      setNotice(t(action === "discard" ? "candidateDiscarded" : "candidateRestored"));
    });
  };

  const advance = (stage: "compose" | "identity_edit") => {
    if (!run) return;
    void perform(`advance-${stage}`, async () => {
      const next = await apiPost<IllustrationRun>(
        `/api/novels/${novelId}/illustration-runs/${run.run_id}/stages/${stage}/advance`,
        { expected_revision: run.revision },
      );
      replaceRun(next); setReadiness(null); setNotice(t("stageAdvanced"));
    });
  };

  const finalize = () => {
    if (!run || !brief) return;
    void perform("finalize", async () => {
      const next = await apiPost<IllustrationRun>(
        `/api/novels/${novelId}/illustration-runs/${run.run_id}/finalize`,
        { expected_revision: run.revision, expected_brief_revision: brief.revision },
      );
      replaceRun(next); await loadBriefs(); setFinalizeOpen(false); setNotice(t("finalized"));
    });
  };

  const importExternal = () => {
    if (!run || !externalFile) return;
    void perform("external-import", async () => {
      const parent = run.stages.refine.selected_asset_id || run.stages.identity_edit.selected_asset_id || run.stages.compose.selected_asset_id;
      if (!parent) throw new Error(t("externalParentRequired"));
      const target: IllustrationStageName = run.stages.refine.status !== "locked"
        ? "refine" : run.stages.identity_edit.status !== "locked" ? "identity_edit" : "compose";
      await apiPostRaw<IllustrationCandidate>(
        `/api/novels/${novelId}/illustration-runs/${run.run_id}/external-edits?target_stage=${target}&parent_asset_id=${parent}&expected_revision=${run.revision}`,
        externalFile, externalFile.type as "image/png" | "image/jpeg" | "image/webp",
      );
      await loadRuns(); await loadCandidates(run.run_id); setExternalFile(null); setNotice(t("externalImported"));
    });
  };

  const adoptLegacy = (assetId: string) => {
    if (!brief) return;
    void perform(`adopt-${assetId}`, async () => {
      await apiPost(`${briefsPath}/${brief.brief_id}/adopt-legacy-asset`, {
        expected_revision: brief.revision, asset_id: assetId,
      });
      await loadBriefs(); setNotice(t("legacyAdopted"));
    });
  };

  const stats = useMemo(() => {
    const all = STAGES.flatMap((stage) => candidates[stage]);
    return {
      count: all.length,
      discarded: all.filter((item) => item.candidate_state === "discarded").length,
      megabytes: (all.reduce((sum, item) => sum + item.byte_size, 0) / 1024 / 1024).toFixed(1),
    };
  }, [candidates]);

  const canFinalize = Boolean(run && (
    run.pipeline_snapshot.kind === "quick"
      ? run.stages.compose.status === "selected"
      : run.stages.refine.status === "selected" || run.stages.identity_edit.status === "selected"
  ));

  return (
    <section data-testid="staged-illustration-workspace" className="mt-6 min-w-0 border-t border-border pt-5">
      <div className="flex min-w-0 flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h4 className="text-sm font-semibold text-foreground">{t("title")}</h4>
          <p className="mt-1 max-w-3xl text-xs leading-5 text-muted">{t("description")}</p>
        </div>
        <label className="flex shrink-0 items-center gap-2 text-xs text-muted">
          <input type="checkbox" checked={showArchived} onChange={(event) => setShowArchived(event.target.checked)} />
          {t("showArchived")}
        </label>
      </div>
      {error && <p role="alert" className="mt-3 rounded-lg border border-red-300 bg-red-50 p-3 text-sm text-red-800 dark:border-red-900 dark:bg-red-950 dark:text-red-200">{error}</p>}
      {notice && <p role="status" className="mt-3 rounded-lg border border-emerald-300 bg-emerald-50 p-3 text-sm text-emerald-900 dark:border-emerald-900 dark:bg-emerald-950 dark:text-emerald-100">{notice}</p>}

      <div className="mt-4 grid min-w-0 gap-4 lg:grid-cols-[minmax(0,0.85fr)_minmax(0,1.65fr)]">
        <aside className="min-w-0 rounded-lg border border-border bg-surface-secondary/40 p-3">
          <label className="block min-w-0 text-xs font-medium text-muted">
            {t("briefLabel")}
            <select data-testid="illustration-brief-select" className="mt-1 w-full min-w-0 truncate rounded-lg border border-border bg-surface px-3 py-2 text-sm text-foreground" value={briefId} onChange={(event) => setBriefId(event.target.value)}>
              <option value="">{t("briefNone")}</option>
              {briefs.map((item) => <option key={item.brief_id} value={item.brief_id}>{item.title}</option>)}
            </select>
          </label>
          {!brief && <div className="mt-3 space-y-3">
            <label className="block text-xs text-muted">{t("sceneLabel")}
              <select className="mt-1 w-full rounded-lg border border-border bg-surface px-3 py-2 text-sm text-foreground" value={sceneIndex} onChange={(event) => setSceneIndex(Number(event.target.value))}>
                {outline.scenes.map((scene, index) => <option key={`${index}-${scene.summary}`} value={index}>{index + 1}. {scene.summary}</option>)}
              </select>
            </label>
            <Button size="sm" variant="primary" className="w-full bg-accent text-white" isDisabled={Boolean(busy) || !referenceCharacterId} onPress={() => void createBrief()}>{t("createBrief")}</Button>
            {!referenceCharacterId && <p className="text-xs leading-5 text-amber-700 dark:text-amber-300">{t("createBriefReferenceHint")}</p>}
          </div>}
          {brief && <div className="mt-3 min-w-0 space-y-3 text-xs">
            <div className="min-w-0"><p className="truncate font-medium text-foreground" title={brief.title}>{brief.title}</p><p className="mt-1 line-clamp-3 leading-5 text-muted">{brief.scene_snapshot.summary}</p></div>
            {brief.stale && <div className="rounded-lg border border-amber-300 bg-amber-50 p-3 text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-100"><p className="font-medium">{t("staleTitle")}</p><p className="mt-1 leading-5">{brief.diff?.scene_missing ? t("staleSceneMissing") : t("staleChanged")}</p><Button size="sm" variant="secondary" className="mt-2" onPress={refreshBrief}>{t("refreshBrief")}</Button></div>}
            <p className="break-words text-muted">{t("declaredCharacters", { count: brief.scene_character_card_ids.length })}</p>
            <p className="break-words text-muted">{t("mainReference", { name: characters.find((item) => item.card_id === brief.default_reference_character_card_id)?.name ?? "—" })}</p>
            <Button size="sm" variant="secondary" onPress={() => patchBrief({ status: brief.status === "active" ? "archived" : "active" })}>{brief.status === "active" ? t("archiveBrief") : t("restoreBrief")}</Button>
          </div>}
        </aside>
        <div className="min-w-0 space-y-4">
          {brief && <section className="min-w-0 rounded-lg border border-border p-3">
            <div className="grid min-w-0 gap-3 sm:grid-cols-2">
              <label className="min-w-0 text-xs font-medium text-muted">{t("pipelineLabel")}
                <select className="mt-1 w-full min-w-0 truncate rounded-lg border border-border bg-surface px-3 py-2 text-sm text-foreground" value={pipelineAlias} onChange={(event) => { setPipelineAlias(event.target.value); setAckQuality(false); }}>
                  {pipelines.map((item) => <option key={item.alias} value={item.alias}>{item.alias} · {item.kind}</option>)}
                </select>
              </label>
              <label className="min-w-0 text-xs font-medium text-muted">{t("referenceAssetLabel")}
                <select className="mt-1 w-full min-w-0 truncate rounded-lg border border-border bg-surface px-3 py-2 text-sm text-foreground" value={referenceAssetId} onChange={(event) => setReferenceAssetId(event.target.value)}>
                  <option value="">{t("referenceAssetNone")}</option>
                  {referenceAssets.map((assetId, index) => <option key={assetId} value={assetId}>{t("referenceAssetOption", { index: index + 1 })} · {assetId.slice(-8)}</option>)}
                </select>
              </label>
            </div>
            {pipeline && <div className={`mt-3 rounded-lg border p-3 text-xs ${pipeline.quality_status === "accepted" ? "border-emerald-300 bg-emerald-50 text-emerald-900 dark:border-emerald-900 dark:bg-emerald-950 dark:text-emerald-100" : "border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-100"}`}>
              <p className="font-medium">{t("qualityStatus", { status: pipeline.quality_status })}</p>
              {pipeline.quality_status !== "accepted" && <label className="mt-2 flex items-start gap-2"><input className="mt-0.5" type="checkbox" checked={ackQuality} onChange={(event) => setAckQuality(event.target.checked)} /><span>{t("qualityAcknowledge")}</span></label>}
            </div>}
            {profile?.external_adapter && <div className="mt-3 rounded-lg border border-border bg-surface-secondary p-3 text-xs text-muted">
              <p className="font-medium text-foreground">{t("externalAdapterRegistered", { name: profile.external_adapter.lora_name })}</p>
              <p className="mt-1 leading-5">{t("externalAdapterNoTraining")}</p>
              <label className="mt-2 flex items-start gap-2"><input className="mt-0.5" type="checkbox" checked={useAdapter} onChange={(event) => setUseAdapter(event.target.checked)} /><span>{t("useExternalAdapter")}</span></label>
            </div>}
            <div className="mt-3 flex min-w-0 flex-wrap gap-2">
              <Button size="sm" variant="primary" className="bg-accent text-white" isDisabled={Boolean(busy) || !referenceAssetId || runs.some((item) => item.status === "active")} onPress={() => void createRun()}>{t("createRun")}</Button>
              {runs.length > 0 && <label className="min-w-0 flex-1 text-xs text-muted"><span className="sr-only">{t("runLabel")}</span><select data-testid="illustration-run-select" className="w-full min-w-0 truncate rounded-lg border border-border bg-surface px-3 py-2 text-sm text-foreground" value={runId} onChange={(event) => setRunId(event.target.value)}>{runs.map((item) => <option key={item.run_id} value={item.run_id}>{item.pipeline_snapshot.alias} · {item.status} · r{item.revision}</option>)}</select></label>}
            </div>
          </section>}

          {run && <>
            <section aria-label={t("timelineLabel")} className="min-w-0 overflow-x-auto rounded-lg border border-border p-3">
              <ol className="grid min-w-[36rem] grid-cols-3 gap-2" data-testid="illustration-stage-timeline">
                {STAGES.map((stage) => <li key={stage} className="min-w-0 rounded-lg bg-surface-secondary p-3 text-xs"><p className="truncate font-medium text-foreground">{t(`stage.${stage}`)}</p><p className="mt-1 truncate text-muted">{t(`stageStatus.${run.stages[stage].status}`)}</p></li>)}
              </ol>
            </section>

            {run.pipeline_snapshot.kind === "consistency" && <section className="rounded-lg border border-border p-3 text-xs">
              <div className="flex flex-wrap items-center justify-between gap-2"><div><p className="font-medium text-foreground">{t("readinessTitle")}</p><p className="mt-1 text-muted">{t("readinessDescription")}</p></div><Button size="sm" variant="secondary" onPress={inspectReadiness}>{t("inspectReadiness")}</Button></div>
              {readiness && <div className={`mt-3 rounded-lg p-3 ${readiness.status === "passed" ? "bg-emerald-50 text-emerald-900 dark:bg-emerald-950 dark:text-emerald-100" : "bg-amber-50 text-amber-900 dark:bg-amber-950 dark:text-amber-100"}`}><p className="font-medium">{t(`readiness.${readiness.status}`)}</p><p className="mt-1">{t("readinessCalls", { calls: readiness.max_provider_calls })}</p>{readiness.issues.map((issue) => <p key={`${issue.stage}-${issue.code}`} className="mt-1 break-words">{issue.message}</p>)}</div>}
            </section>}

            <section className="min-w-0 space-y-4">
              {STAGES.map((stage) => {
                const stageState = run.stages[stage];
                return <div key={stage} className="min-w-0 rounded-lg border border-border p-3" data-testid={`illustration-stage-${stage}`}>
                  <div className="flex min-w-0 flex-wrap items-start justify-between gap-2"><div className="min-w-0"><h5 className="truncate text-sm font-semibold text-foreground">{t(`stage.${stage}`)}</h5><p className="mt-1 text-xs text-muted">{t(`stageStatus.${stageState.status}`)}</p></div>{["ready", "failed", "cancelled"].includes(stageState.status) && <Button size="sm" variant="primary" className="bg-accent text-white" isDisabled={Boolean(busy) || Boolean(job && !job.job.terminal) || (stage === "compose" && !prompt)} onPress={() => startStage(stage)}>{t("startStage")}</Button>}</div>
                  {stage === "identity_edit" && stageState.status === "ready" && <div className="mt-3 grid gap-2 sm:grid-cols-2"><label className="text-xs text-muted">{t("identityInstruction")}<textarea className="mt-1 min-h-24 w-full rounded-lg border border-border bg-surface p-2 text-sm text-foreground" value={identityInstruction} onChange={(event) => setIdentityInstruction(event.target.value)} /></label><label className="text-xs text-muted">{t("mustPreserve")}<textarea className="mt-1 min-h-24 w-full rounded-lg border border-border bg-surface p-2 text-sm text-foreground" value={mustPreserve} onChange={(event) => setMustPreserve(event.target.value)} /></label></div>}
                  {stage === "refine" && stageState.status === "ready" && <label className="mt-3 block text-xs text-muted">{t("refineStrength", { value: refineStrength.toFixed(2) })}<input className="mt-2 w-full" type="range" min="0" max="1" step="0.01" value={refineStrength} onChange={(event) => setRefineStrength(Number(event.target.value))} /></label>}
                  {candidates[stage].length > 0 && <ul className="mt-3 grid min-w-0 gap-3 sm:grid-cols-2 xl:grid-cols-3">{candidates[stage].map((candidate) => <li key={candidate.asset_id} className={`min-w-0 overflow-hidden rounded-lg border ${candidate.selected ? "border-accent" : "border-border"}`}><div className="relative aspect-[4/3] bg-surface-secondary"><Image src={getImageUrl(candidate.content_url)} alt={t("candidateAlt", { stage: t(`stage.${stage}`) })} fill unoptimized sizes="(min-width: 1024px) 20rem, 90vw" className="object-cover" /></div><div className="space-y-2 p-2 text-xs"><p className="truncate text-muted">{candidate.source} · {candidate.candidate_state} · {(candidate.byte_size / 1024 / 1024).toFixed(1)} MB</p><div className="flex flex-wrap gap-2">{candidate.candidate_state !== "discarded" && !candidate.selected && <Button size="sm" variant="secondary" onPress={() => selectCandidate(candidate)}>{t("selectCandidate")}</Button>}{candidate.candidate_state === "discarded" ? <Button size="sm" variant="secondary" onPress={() => mutateCandidate(candidate, "restore")}>{t("restoreCandidate")}</Button> : candidate.candidate_state !== "finalized" && <Button size="sm" variant="ghost" onPress={() => mutateCandidate(candidate, "discard")}>{t("discardCandidate")}</Button>}</div></div></li>)}</ul>}
                  {stageState.status === "selected" && stage === "compose" && run.pipeline_snapshot.kind === "consistency" && <Button size="sm" variant="secondary" className="mt-3" onPress={() => advance("compose")}>{t("advanceIdentity")}</Button>}
                  {stageState.status === "selected" && stage === "identity_edit" && run.pipeline_snapshot.kind === "consistency" && run.stages.refine.status === "locked" && <Button size="sm" variant="secondary" className="mt-3" onPress={() => advance("identity_edit")}>{t("advanceRefine")}</Button>}
                </div>;
              })}
            </section>

            {job && <div role="status" className="flex min-w-0 flex-wrap items-center justify-between gap-2 rounded-lg border border-border bg-surface-secondary p-3 text-sm text-muted"><span>{t("jobStatus", { status: job.job.status })}</span>{!job.job.terminal && <Button size="sm" variant="secondary" isDisabled={Boolean(busy)} onPress={cancelStageJob}>{t("cancelJob")}</Button>}</div>}
            <section className="rounded-lg border border-border p-3 text-xs"><p className="font-medium text-foreground">{t("candidateStorageTitle")}</p><p className="mt-1 leading-5 text-muted">{t("candidateStorage", { count: stats.count, discarded: stats.discarded, size: stats.megabytes })}</p><p className="mt-1 leading-5 text-amber-700 dark:text-amber-300">{t("discardDoesNotFreeDisk")}</p></section>
            <section className="rounded-lg border border-border p-3"><div className="flex min-w-0 flex-wrap items-center gap-2"><input aria-label={t("externalFile")} className="min-w-0 max-w-full text-xs" type="file" accept="image/png,image/jpeg,image/webp" onChange={(event) => setExternalFile(event.target.files?.[0] ?? null)} /><Button size="sm" variant="secondary" isDisabled={!externalFile || Boolean(busy)} onPress={importExternal}>{t("importExternal")}</Button></div></section>
            <div className="flex min-w-0 flex-wrap justify-end gap-2">
              {run.status === "finalized" && run.final_asset_id && <Button size="sm" variant="secondary" onPress={() => void createRun(run)}>{t("branchFromFinal")}</Button>}
              {run.status === "active" && canFinalize && <Button size="sm" variant="primary" className="bg-accent text-white" onPress={() => setFinalizeOpen(true)}>{t("finalize")}</Button>}
            </div>
          </>}

          {legacyAssets.length > 0 && brief && <section className="rounded-lg border border-dashed border-border p-3 text-xs"><p className="font-medium text-foreground">{t("legacyTitle")}</p><p className="mt-1 leading-5 text-muted">{t("legacyDescription")}</p><ul className="mt-3 grid min-w-0 gap-2 sm:grid-cols-2">{legacyAssets.map((asset) => <li key={asset.asset_id} className="flex min-w-0 items-center justify-between gap-2 rounded-lg bg-surface-secondary p-2"><span className="min-w-0 truncate">{asset.asset_id}</span><Button size="sm" variant="secondary" onPress={() => adoptLegacy(asset.asset_id)}>{t("adoptLegacy")}</Button></li>)}</ul></section>}
        </div>
      </div>

      {finalizeOpen && run && brief && <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-3"><div role="dialog" aria-modal="true" aria-labelledby="illustration-finalize-title" className="max-h-[calc(100vh-1.5rem)] w-full max-w-lg overflow-y-auto rounded-xl border border-border bg-surface p-4 shadow-xl"><h5 id="illustration-finalize-title" className="text-base font-semibold text-foreground">{t("finalizeTitle")}</h5><p className="mt-2 text-sm leading-6 text-muted">{t("finalizeDescription")}</p><dl className="mt-3 grid gap-2 rounded-lg bg-surface-secondary p-3 text-xs"><div className="flex justify-between gap-3"><dt>{t("finalizePipeline")}</dt><dd className="min-w-0 truncate font-medium">{run.pipeline_snapshot.alias}</dd></div><div className="flex justify-between gap-3"><dt>{t("finalizeRevision")}</dt><dd>run r{run.revision} / brief r{brief.revision}</dd></div><div className="flex justify-between gap-3"><dt>{t("finalizeAsset")}</dt><dd className="min-w-0 truncate">{run.stages.refine.selected_asset_id || run.stages.identity_edit.selected_asset_id || run.stages.compose.selected_asset_id}</dd></div></dl><p className="mt-3 text-xs leading-5 text-muted">{t("finalizeNoProviderCall")}</p><div className="mt-4 flex flex-wrap justify-end gap-2"><Button variant="secondary" onPress={() => setFinalizeOpen(false)}>{t("cancelFinalize")}</Button><Button variant="primary" className="bg-accent text-white" isDisabled={Boolean(busy)} onPress={finalize}>{t("confirmFinalize")}</Button></div></div></div>}
    </section>
  );
}