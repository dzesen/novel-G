"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { apiGet, apiPost } from "@/lib/api";
import type {
  WritingTargetKey,
  WritingTargetValidationSource,
} from "@/lib/writingRoute";
import type {
  AgentProfile,
  AgentScope,
  AgentToolMetadata,
  ContinuityEvidenceReference,
  ContinuityReviewResult,
  CreativeInspirationResult,
  StyleConsistencyResult,
  VolumeRetrospectiveResult,
} from "@/types/agent";
import type {
  ChapterSummary,
  VolumeSummary,
} from "@/types/novel";
import AgentRevisionWorkspace, {
  type AgentRevisionSourceSelection,
} from "./AgentRevisionWorkspace";
import { contextSectionKind } from "../generationMetadataPresentation";

interface Props {
  novelId: string;
  tools: readonly ContextualToolTab[];
  initialVolumeId?: string;
  initialChapterId?: string;
  onTargetValidation?: (
    key: WritingTargetKey,
    value: string,
    valid: boolean,
    source?: WritingTargetValidationSource,
  ) => void;
  onScopeTargetChange?: (targets: {
    volume: string | undefined;
    chapter: string | undefined;
  }) => void;
  onOpenHistory?: (runId: string) => void;
  onNavigateReference?: (reference: ContinuityEvidenceReference) => void;
}

export type ContextualToolTab =
  | "creative"
  | "continuity"
  | "style"
  | "retrospective";

type StudioTab = ContextualToolTab | "composer";

type ToolScope = Exclude<AgentScope, "character">;

const fieldClass =
  "w-full rounded-lg border border-border bg-surface px-3 py-2 text-sm text-foreground outline-none transition-colors placeholder:text-muted focus:border-accent";

export default function GenerationToolWorkspace({
  novelId,
  tools,
  initialVolumeId = "",
  initialChapterId = "",
  onTargetValidation,
  onScopeTargetChange,
  onOpenHistory,
  onNavigateReference,
}: Props) {
  const t = useTranslations("writing.agentStudio");
  const metadataT = useTranslations("writing.generationMetadata");
  const primaryTool = tools[0] ?? "creative";
  const [tab, setTab] = useState<StudioTab>(primaryTool);
  const [agents, setAgents] = useState<AgentProfile[]>([]);
  const [volumes, setVolumes] = useState<VolumeSummary[]>([]);
  const [chapters, setChapters] = useState<ChapterSummary[]>([]);
  const [scopeTargetsLoaded, setScopeTargetsLoaded] = useState(false);
  const [loadingCatalog, setLoadingCatalog] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [scope, setScope] = useState<ToolScope>(
    primaryTool === "retrospective"
      ? "volume"
      : primaryTool === "style" || initialChapterId
        ? "chapter"
        : initialVolumeId
          ? "volume"
          : "novel",
  );
  const [volumeId, setVolumeId] = useState(initialVolumeId);
  const [chapterId, setChapterId] = useState(initialChapterId);
  const [toolAgentId, setToolAgentId] = useState("");
  const [question, setQuestion] = useState("");
  const [constraints, setConstraints] = useState("");
  const [focus, setFocus] = useState("");
  const [extraInstruction, setExtraInstruction] = useState("");
  const [ideaCount, setIdeaCount] = useState(4);
  const [running, setRunning] = useState(false);
  const [creativeResult, setCreativeResult] =
    useState<CreativeInspirationResult | null>(null);
  const [continuityResult, setContinuityResult] =
    useState<ContinuityReviewResult | null>(null);
  const [styleResult, setStyleResult] =
    useState<StyleConsistencyResult | null>(null);
  const [styleFocus, setStyleFocus] = useState("");
  const [retrospectiveResult, setRetrospectiveResult] =
    useState<VolumeRetrospectiveResult | null>(null);
  const [retrospectiveFocus, setRetrospectiveFocus] = useState("");
  const [toolMetadata, setToolMetadata] =
    useState<AgentToolMetadata | null>(null);
  const [revisionSource, setRevisionSource] =
    useState<AgentRevisionSourceSelection | null>(null);
  const routeTargetIdentity = `${initialVolumeId}:${initialChapterId}`;
  const previousRouteTargetIdentity = useRef(routeTargetIdentity);
  const targetRevision = useRef(0);
  const revisionReturnTab = useRef<ContextualToolTab>(primaryTool);

  const clearGeneratedOutput = useCallback(() => {
    targetRevision.current += 1;
    setCreativeResult(null);
    setContinuityResult(null);
    setStyleResult(null);
    setRetrospectiveResult(null);
    setToolMetadata(null);
    setRevisionSource(null);
    setError(null);
    setNotice(null);
  }, []);

  const loadCatalog = useCallback(async () => {
    setLoadingCatalog(true);
    setError(null);
    try {
      const response = await apiGet<{ data: AgentProfile[] }>(
        "/api/agents?include_disabled=true",
      );
      setAgents(response.data);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("loadFailed"));
    } finally {
      setLoadingCatalog(false);
    }
  }, [t]);

  useEffect(() => {
    void loadCatalog();
  }, [loadCatalog]);

  useEffect(() => {
    if (tab === "composer") return;
    if (!tools.includes(tab as ContextualToolTab)) {
      setTab(tools[0] ?? "creative");
    }
  }, [tab, tools]);

  useEffect(() => {
    const targetChanged =
      previousRouteTargetIdentity.current !== routeTargetIdentity;
    previousRouteTargetIdentity.current = routeTargetIdentity;
    setVolumeId(initialVolumeId);
    setChapterId(initialChapterId);
    if (targetChanged) clearGeneratedOutput();
    if (tab === "composer") return;
    setScope(
      primaryTool === "retrospective"
        ? "volume"
        : tab === "style"
          ? initialVolumeId && !initialChapterId
            ? "volume"
            : "chapter"
          : initialChapterId
            ? "chapter"
            : initialVolumeId
              ? "volume"
              : "novel",
    );
  }, [
    clearGeneratedOutput,
    initialChapterId,
    initialVolumeId,
    primaryTool,
    routeTargetIdentity,
    tab,
  ]);

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      apiGet<{ data: VolumeSummary[] }>(`/api/volumes/novel/${novelId}`),
      apiGet<{ data: ChapterSummary[] }>(`/api/chapters/novel/${novelId}`),
    ])
      .then(([volumeResponse, chapterResponse]) => {
        if (cancelled) return;
        setVolumes(
          [...volumeResponse.data].sort(
            (left, right) => left.order_index - right.order_index,
          ),
        );
        setChapters(chapterResponse.data);
        setScopeTargetsLoaded(true);
      })
      .catch((caught: unknown) => {
        if (!cancelled) {
          setError(caught instanceof Error ? caught.message : t("loadFailed"));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [novelId, t]);

  useEffect(() => {
    if (!scopeTargetsLoaded || !onTargetValidation) return;
    const selectedVolume = initialVolumeId
      ? volumes.find((volume) => volume._id === initialVolumeId)
      : undefined;
    const selectedChapter = initialChapterId
      ? chapters.find((chapter) => chapter._id === initialChapterId)
      : undefined;
    if (initialVolumeId) {
      onTargetValidation("volume", initialVolumeId, Boolean(selectedVolume));
    }
    if (initialChapterId) {
      onTargetValidation("chapter", initialChapterId, Boolean(selectedChapter));
    }
    if (initialVolumeId && initialChapterId && selectedVolume && selectedChapter) {
      onTargetValidation(
        "chapter",
        initialChapterId,
        selectedChapter.volume_id === selectedVolume._id,
        "chapter-volume",
      );
    }
  }, [
    chapters,
    initialChapterId,
    initialVolumeId,
    onTargetValidation,
    scopeTargetsLoaded,
    volumes,
  ]);

  const activeCapability =
    tab === "retrospective"
      ? "volume_retrospective"
      : tab === "style"
        ? "style_consistency"
        : tab === "continuity"
          ? "continuity_review"
          : "creative_inspiration";
  const toolAgents = useMemo(
    () =>
      agents.filter(
        (agent) =>
          agent.enabled && agent.capabilities.includes(activeCapability),
      ),
    [activeCapability, agents],
  );

  useEffect(() => {
    if (!toolAgents.some((agent) => agent.agent_id === toolAgentId)) {
      setToolAgentId(toolAgents[0]?.agent_id ?? "");
    }
  }, [toolAgentId, toolAgents]);

  const volumeOrders = useMemo(
    () =>
      Object.fromEntries(
        volumes.map((volume) => [volume._id, volume.order_index]),
      ),
    [volumes],
  );
  const visibleChapters = useMemo(
    () =>
      [...chapters]
        .filter((chapter) => !volumeId || chapter.volume_id === volumeId)
        .sort(
          (left, right) =>
            (volumeOrders[left.volume_id] ?? 0) -
              (volumeOrders[right.volume_id] ?? 0) ||
            left.order_index - right.order_index,
        ),
    [chapters, volumeId, volumeOrders],
  );

  useEffect(() => {
    if (
      scopeTargetsLoaded &&
      chapterId &&
      !visibleChapters.some((chapter) => chapter._id === chapterId)
    ) {
      setChapterId("");
    }
  }, [chapterId, scopeTargetsLoaded, visibleChapters]);

  const runTool = async () => {
    if (!novelId || !toolAgentId) return;
    if (scope === "volume" && !volumeId) {
      setError(t("tool.chooseVolume"));
      return;
    }
    if (scope === "chapter" && !chapterId) {
      setError(t("tool.chooseChapter"));
      return;
    }
    setRunning(true);
    setError(null);
    setNotice(null);
    setToolMetadata(null);
    setRevisionSource(null);
    const expectedTargetRevision = targetRevision.current;
    try {
      const base = {
        novel_id: novelId,
        scope,
        volume_id: scope === "volume" ? volumeId : null,
        chapter_id: scope === "chapter" ? chapterId : null,
        agent_id: toolAgentId,
        instruction: extraInstruction.trim(),
      };
      if (tab === "creative") {
        const response = await apiPost<
          { result: CreativeInspirationResult } & AgentToolMetadata
        >("/api/llm/agent-inspiration", {
          ...base,
          question: question.trim(),
          constraints: constraints.trim(),
          idea_count: ideaCount,
        });
        if (expectedTargetRevision !== targetRevision.current) return;
        setCreativeResult(response.result);
        setContinuityResult(null);
        setStyleResult(null);
        setRetrospectiveResult(null);
        setToolMetadata(response);
      } else if (tab === "continuity") {
        const response = await apiPost<
          { result: ContinuityReviewResult } & AgentToolMetadata
        >("/api/llm/agent-continuity-review", {
          ...base,
          focus: focus.trim(),
        });
        if (expectedTargetRevision !== targetRevision.current) return;
        setContinuityResult(response.result);
        setCreativeResult(null);
        setStyleResult(null);
        setRetrospectiveResult(null);
        setToolMetadata(response);
      } else if (tab === "style") {
        const response = await apiPost<
          { result: StyleConsistencyResult } & AgentToolMetadata
        >("/api/llm/agent-style-consistency", {
          ...base,
          focus: styleFocus.trim(),
        });
        if (expectedTargetRevision !== targetRevision.current) return;
        setStyleResult(response.result);
        setCreativeResult(null);
        setContinuityResult(null);
        setRetrospectiveResult(null);
        setToolMetadata(response);
      } else {
        const response = await apiPost<
          { result: VolumeRetrospectiveResult } & AgentToolMetadata
        >("/api/llm/agent-volume-retrospective", {
          ...base,
          focus: retrospectiveFocus.trim(),
        });
        if (expectedTargetRevision !== targetRevision.current) return;
        setRetrospectiveResult(response.result);
        setCreativeResult(null);
        setContinuityResult(null);
        setStyleResult(null);
        setToolMetadata(response);
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("runFailed"));
    } finally {
      setRunning(false);
    }
  };

  const copyResult = async () => {
    const result =
      tab === "creative"
        ? creativeResult
        : tab === "continuity"
          ? continuityResult
          : tab === "style"
            ? styleResult
            : retrospectiveResult;
    if (!result) return;
    await navigator.clipboard.writeText(JSON.stringify(result, null, 2));
    setNotice(t("copied"));
  };

  const startRevision = (
    sourceKind: AgentRevisionSourceSelection["sourceKind"],
    sourceIndex: number,
    label: string,
  ) => {
    if (!toolMetadata) return;
    revisionReturnTab.current = tab as ContextualToolTab;
    setRevisionSource({
      runId: toolMetadata.run_id,
      sourceKind,
      sourceIndex,
      label,
      contextSnapshot: toolMetadata.context_snapshot,
    });
    setTab("composer");
    setError(null);
    setNotice(null);
  };

  const leaveRevisionComposer = () => {
    setRevisionSource(null);
    setTab(revisionReturnTab.current);
  };

  const changeScope = (nextScope: ToolScope) => {
    clearGeneratedOutput();
    setScope(nextScope);
    if (nextScope === "novel") {
      setVolumeId("");
      setChapterId("");
      onScopeTargetChange?.({ volume: undefined, chapter: undefined });
      return;
    }
    if (nextScope === "volume") {
      setChapterId("");
      onScopeTargetChange?.({
        volume: volumeId || undefined,
        chapter: undefined,
      });
      return;
    }
    onScopeTargetChange?.({
      volume: volumeId || undefined,
      chapter: chapterId || undefined,
    });
  };

  const changeVolume = (nextVolumeId: string) => {
    clearGeneratedOutput();
    setVolumeId(nextVolumeId);
    if (scope === "volume") {
      onScopeTargetChange?.({
        volume: nextVolumeId || undefined,
        chapter: undefined,
      });
      return;
    }
    const selectedChapter = chapters.find(
      (chapter) => chapter._id === chapterId,
    );
    const nextChapterId =
      nextVolumeId && selectedChapter?.volume_id !== nextVolumeId
        ? ""
        : chapterId;
    if (nextChapterId !== chapterId) setChapterId(nextChapterId);
    onScopeTargetChange?.({
      volume: nextVolumeId || undefined,
      chapter: nextChapterId || undefined,
    });
  };

  const changeChapter = (nextChapterId: string) => {
    clearGeneratedOutput();
    setChapterId(nextChapterId);
    onScopeTargetChange?.({
      volume: volumeId || undefined,
      chapter: nextChapterId || undefined,
    });
  };

  const scopeControls = (
    <div className="grid gap-4 md:grid-cols-3">
      <label className="space-y-1.5 text-sm">
        <span className="text-muted">{t("tool.scope")}</span>
        <select
          className={fieldClass}
          value={scope}
          disabled={running}
          onChange={(event) => changeScope(event.target.value as ToolScope)}
        >
          {tab !== "style" && tab !== "retrospective" && (
            <option value="novel">{t("tool.scopeNovel")}</option>
          )}
          <option value="volume">{t("tool.scopeVolume")}</option>
          {tab !== "retrospective" && (
            <option value="chapter">{t("tool.scopeChapter")}</option>
          )}
        </select>
      </label>
      {scope === "volume" && (
        <label className="space-y-1.5 text-sm md:col-span-2">
          <span className="text-muted">{t("tool.volume")}</span>
          <select
            className={fieldClass}
            value={volumeId}
            disabled={running}
            onChange={(event) => changeVolume(event.target.value)}
          >
            <option value="">{t("tool.selectVolume")}</option>
            {volumes.map((volume) => (
              <option key={volume._id} value={volume._id}>
                {t("tool.volumeLabel", {
                  order: volume.order_index,
                  title: volume.title,
                })}
              </option>
            ))}
          </select>
        </label>
      )}
      {scope === "chapter" && (
        <>
          <label className="space-y-1.5 text-sm">
            <span className="text-muted">{t("tool.volumeFilter")}</span>
            <select
              className={fieldClass}
              value={volumeId}
              disabled={running}
              onChange={(event) => changeVolume(event.target.value)}
            >
              <option value="">{t("tool.allVolumes")}</option>
              {volumes.map((volume) => (
                <option key={volume._id} value={volume._id}>
                  {t("tool.volumeLabel", {
                    order: volume.order_index,
                    title: volume.title,
                  })}
                </option>
              ))}
            </select>
          </label>
          <label className="space-y-1.5 text-sm">
            <span className="text-muted">{t("tool.chapter")}</span>
            <select
              className={fieldClass}
              value={chapterId}
              disabled={running}
              onChange={(event) => changeChapter(event.target.value)}
            >
              <option value="">{t("tool.selectChapter")}</option>
              {visibleChapters.map((chapter) => (
                <option key={chapter._id} value={chapter._id}>
                  {t("tool.chapterLabel", {
                    volume: volumeOrders[chapter.volume_id] ?? "?",
                    order: chapter.order_index,
                    title: chapter.title,
                  })}
                </option>
              ))}
            </select>
          </label>
        </>
      )}
    </div>
  );

  const resultHeader = (title: string, badge = t("previewBadge")) => (
    <div className="flex flex-wrap items-start justify-between gap-3 border-b border-border pb-4">
      <div>
        <p className="text-xs font-semibold uppercase tracking-[0.14em] text-accent">
          {badge}
        </p>
        <h3 className="mt-1 text-lg font-semibold text-foreground">{title}</h3>
      </div>
      <button
        type="button"
        onClick={() => void copyResult()}
        className="rounded-lg border border-border px-3 py-1.5 text-sm text-muted transition-colors hover:border-accent hover:text-accent"
      >
        {t("copy")}
      </button>
    </div>
  );

  const renderTool = () => {
    if (!novelId) {
      return (
        <div className="rounded-lg border border-dashed border-border bg-surface px-6 py-12 text-center">
          <h3 className="font-semibold text-foreground">{t("needNovelTitle")}</h3>
          <p className="mx-auto mt-2 max-w-xl text-sm text-muted">
            {t("needNovelDescription")}
          </p>
        </div>
      );
    }

    return (
      <div className="grid min-h-0 gap-6 xl:grid-cols-[minmax(0,0.88fr)_minmax(0,1.12fr)]">
        <section className="self-start rounded-lg border border-border bg-surface p-5">
          <div className="mb-5">
            <p className="text-xs font-semibold uppercase tracking-[0.14em] text-accent">
              {tab === "creative"
                ? t("creative.eyebrow")
                : tab === "continuity"
                  ? t("continuity.eyebrow")
                  : tab === "style"
                    ? t("style.eyebrow")
                    : t("retrospective.eyebrow")}
            </p>
            <h2 className="mt-1 text-xl font-semibold text-foreground">
              {tab === "creative"
                ? t("creative.title")
                : tab === "continuity"
                  ? t("continuity.title")
                  : tab === "style"
                    ? t("style.title")
                    : t("retrospective.title")}
            </h2>
            <p className="mt-2 text-sm leading-6 text-muted">
              {tab === "creative"
                ? t("creative.description")
                : tab === "continuity"
                  ? t("continuity.description")
                  : tab === "style"
                    ? t("style.description")
                    : t("retrospective.description")}
            </p>
          </div>

          <div className="space-y-4">
            {scopeControls}
            <label className="block space-y-1.5 text-sm">
              <span className="text-muted">{t("tool.agent")}</span>
              <select
                className={fieldClass}
                value={toolAgentId}
                onChange={(event) => setToolAgentId(event.target.value)}
              >
                {toolAgents.map((agent) => (
                  <option key={agent.agent_id} value={agent.agent_id}>
                    {agent.label}
                    {agent.origin === "custom"
                      ? ` · ${t("management.custom")}`
                      : ""}
                  </option>
                ))}
              </select>
            </label>

            {tab === "creative" ? (
              <>
                <label className="block space-y-1.5 text-sm">
                  <span className="text-muted">{t("creative.question")}</span>
                  <textarea
                    className={`${fieldClass} min-h-28 resize-y`}
                    value={question}
                    onChange={(event) => setQuestion(event.target.value)}
                    placeholder={t("creative.questionPlaceholder")}
                  />
                </label>
                <label className="block space-y-1.5 text-sm">
                  <span className="text-muted">{t("creative.constraints")}</span>
                  <textarea
                    className={`${fieldClass} min-h-20 resize-y`}
                    value={constraints}
                    onChange={(event) => setConstraints(event.target.value)}
                    placeholder={t("creative.constraintsPlaceholder")}
                  />
                </label>
                <label className="block space-y-1.5 text-sm">
                  <span className="text-muted">{t("creative.ideaCount")}</span>
                  <input
                    type="number"
                    min={2}
                    max={8}
                    className={fieldClass}
                    value={ideaCount}
                    onChange={(event) =>
                      setIdeaCount(Number(event.target.value) || 2)
                    }
                  />
                </label>
              </>
            ) : tab === "continuity" ? (
              <label className="block space-y-1.5 text-sm">
                <span className="text-muted">{t("continuity.focus")}</span>
                <textarea
                  className={`${fieldClass} min-h-28 resize-y`}
                  value={focus}
                  onChange={(event) => setFocus(event.target.value)}
                  placeholder={t("continuity.focusPlaceholder")}
                />
              </label>
            ) : tab === "style" ? (
              <label className="block space-y-1.5 text-sm">
                <span className="text-muted">{t("style.focus")}</span>
                <textarea
                  className={`${fieldClass} min-h-28 resize-y`}
                  value={styleFocus}
                  onChange={(event) => setStyleFocus(event.target.value)}
                  placeholder={t("style.focusPlaceholder")}
                />
              </label>
            ) : (
              <label className="block space-y-1.5 text-sm">
                <span className="text-muted">
                  {t("retrospective.focus")}
                </span>
                <textarea
                  className={`${fieldClass} min-h-28 resize-y`}
                  value={retrospectiveFocus}
                  onChange={(event) =>
                    setRetrospectiveFocus(event.target.value)
                  }
                  placeholder={t("retrospective.focusPlaceholder")}
                />
              </label>
            )}

            <label className="block space-y-1.5 text-sm">
              <span className="text-muted">{t("tool.extraInstruction")}</span>
              <textarea
                className={`${fieldClass} min-h-20 resize-y`}
                value={extraInstruction}
                onChange={(event) => setExtraInstruction(event.target.value)}
                placeholder={t("tool.extraInstructionPlaceholder")}
              />
            </label>
            <button
              type="button"
              disabled={
                running ||
                !toolAgentId ||
                (tab === "creative" && question.trim().length < 2)
              }
              onClick={() => void runTool()}
              className="w-full rounded-lg bg-accent px-4 py-2.5 text-sm font-semibold text-white transition-opacity disabled:cursor-not-allowed disabled:opacity-50"
            >
              {running ? t("running") : t("run")}
            </button>
            <p className="text-xs leading-5 text-muted">
              {tab === "style"
                ? t("style.previewOnlyHint")
                : tab === "retrospective"
                  ? t("retrospective.previewOnlyHint")
                  : t("previewOnlyHint")}
            </p>
          </div>
        </section>

        <section className="min-h-80 rounded-lg border border-border bg-surface p-5">
          {tab === "creative" && creativeResult ? (
            <div className="space-y-5">
              {resultHeader(t("creative.resultTitle"))}
              <p className="text-sm leading-6 text-muted">
                {creativeResult.framing}
              </p>
              <ol className="space-y-4">
                {creativeResult.ideas.map((idea, index) => (
                  <li
                    key={`${idea.title}-${index}`}
                    className="border-l-2 border-accent/40 pl-4"
                  >
                    <div className="flex items-baseline gap-2">
                      <span className="text-xs font-semibold text-accent">
                        {String(index + 1).padStart(2, "0")}
                      </span>
                      <h4 className="font-semibold text-foreground">
                        {idea.title}
                      </h4>
                    </div>
                    <p className="mt-2 text-sm leading-6 text-foreground">
                      {idea.concept}
                    </p>
                    <p className="mt-2 text-sm leading-6 text-muted">
                      <span className="font-medium text-foreground">
                        {t("creative.fitReason")}：
                      </span>
                      {idea.fit_reason}
                    </p>
                    {idea.affected_elements.length > 0 && (
                      <div className="mt-3 flex flex-wrap gap-1.5">
                        {idea.affected_elements.map((item) => (
                          <span
                            key={item}
                            className="rounded bg-surface-secondary px-2 py-1 text-xs text-muted"
                          >
                            {item}
                          </span>
                        ))}
                      </div>
                    )}
                    {(idea.risks.length > 0 ||
                      idea.suggested_changes.length > 0) && (
                      <div className="mt-3 grid gap-3 text-sm md:grid-cols-2">
                        <div>
                          <p className="font-medium text-foreground">
                            {t("creative.risks")}
                          </p>
                          <ul className="mt-1 space-y-1 text-muted">
                            {idea.risks.map((item) => (
                              <li key={item}>— {item}</li>
                            ))}
                          </ul>
                        </div>
                        <div>
                          <p className="font-medium text-foreground">
                            {t("creative.suggestedChanges")}
                          </p>
                          <ul className="mt-1 space-y-1 text-muted">
                            {idea.suggested_changes.map((item) => (
                              <li key={item}>— {item}</li>
                            ))}
                          </ul>
                        </div>
                      </div>
                    )}
                    {toolMetadata && (
                      <button
                        type="button"
                        onClick={() =>
                          startRevision(
                            "creative_idea",
                            index,
                            idea.title,
                          )
                        }
                        className="mt-4 rounded-lg border border-accent px-3 py-1.5 text-sm font-medium text-accent transition-colors hover:bg-accent hover:text-white"
                      >
                        {t("revisions.createFromResult")}
                      </button>
                    )}
                  </li>
                ))}
              </ol>
            </div>
          ) : tab === "continuity" && continuityResult ? (
            <div className="space-y-5">
              {resultHeader(t("continuity.resultTitle"))}
              <div>
                <p className="text-sm leading-6 text-foreground">
                  {continuityResult.summary}
                </p>
                <p className="mt-2 text-xs leading-5 text-muted">
                  {continuityResult.coverage}
                </p>
              </div>
              {continuityResult.issues.length === 0 ? (
                <div className="rounded-lg bg-surface-secondary p-4 text-sm text-muted">
                  {t("continuity.noIssues")}
                </div>
              ) : (
                <ol className="space-y-4">
                  {continuityResult.issues.map((issue, index) => (
                    <li
                      key={`${issue.location}-${index}`}
                      className="border-l-2 border-border pl-4"
                    >
                      <div className="flex flex-wrap items-center gap-2">
                        <span
                          className={`rounded px-2 py-0.5 text-xs font-semibold ${
                            issue.severity === "high"
                              ? "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300"
                              : issue.severity === "medium"
                                ? "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300"
                                : "bg-surface-secondary text-muted"
                          }`}
                        >
                          {t(`continuity.severity.${issue.severity}`)}
                        </span>
                        <span className="text-xs text-muted">
                          {t(`continuity.category.${issue.category}`)}
                        </span>
                        <span className="text-xs text-muted">
                          {Math.round(issue.confidence * 100)}%
                        </span>
                      </div>
                      <p className="mt-2 text-sm font-medium text-foreground">
                        {issue.location}
                      </p>
                      <p className="mt-1 text-sm leading-6 text-foreground">
                        {issue.problem}
                      </p>
                      <div className="mt-3 bg-surface-secondary px-3 py-2">
                        <p className="text-xs font-semibold uppercase tracking-wide text-muted">
                          {t("continuity.evidence")}
                        </p>
                        <ul className="mt-1 space-y-1 text-sm text-muted">
                          {issue.evidence.map((item) => (
                            <li key={item}>— {item}</li>
                          ))}
                        </ul>
                      </div>
                      <div className="mt-3">
                        <p className="text-xs font-semibold text-muted">
                          {t("continuity.references")}
                        </p>
                        <div className="mt-2 flex flex-wrap gap-2">
                          {issue.references.map((reference, refIndex) => (
                            <button
                              key={`${reference.kind}-${reference.label}-${refIndex}`}
                              type="button"
                              onClick={() =>
                                onNavigateReference?.(reference)
                              }
                              disabled={!onNavigateReference}
                              title={reference.excerpt || reference.label}
                              className="max-w-full rounded-lg border border-border px-2.5 py-1 text-left text-xs text-muted transition-colors hover:border-accent hover:text-accent disabled:cursor-default disabled:opacity-70"
                            >
                              <span className="font-medium text-foreground">
                                {t(
                                  `continuity.referenceKind.${reference.kind}`,
                                )}
                              </span>
                              {" · "}
                              <span className="break-words">
                                {reference.label}
                              </span>
                            </button>
                          ))}
                        </div>
                      </div>
                      <p className="mt-3 text-sm leading-6 text-muted">
                        <span className="font-medium text-foreground">
                          {t("continuity.suggestion")}：
                        </span>
                        {issue.suggestion}
                      </p>
                      {toolMetadata && (
                        <button
                          type="button"
                          onClick={() =>
                            startRevision(
                              "continuity_issue",
                              index,
                              issue.location,
                            )
                          }
                          className="mt-4 rounded-lg border border-accent px-3 py-1.5 text-sm font-medium text-accent transition-colors hover:bg-accent hover:text-white"
                        >
                          {t("revisions.createFromResult")}
                        </button>
                      )}
                    </li>
                  ))}
                </ol>
              )}
            </div>
          ) : tab === "style" && styleResult ? (
            <div className="space-y-5">
              {resultHeader(t("style.resultTitle"))}
              <div>
                <p className="text-sm leading-6 text-foreground">
                  {styleResult.summary}
                </p>
                <p className="mt-2 text-xs leading-5 text-muted">
                  {styleResult.coverage}
                </p>
              </div>
              {styleResult.issues.length === 0 ? (
                <div className="rounded-lg bg-surface-secondary p-4 text-sm text-muted">
                  {t("style.noIssues")}
                </div>
              ) : (
                <ol className="divide-y divide-border">
                  {styleResult.issues.map((issue, index) => (
                    <li
                      key={`${issue.location}-${index}`}
                      className="py-5 first:pt-0 last:pb-0"
                    >
                      <div className="flex flex-wrap items-center gap-2">
                        <span
                          className={`rounded px-2 py-0.5 text-xs font-semibold ${
                            issue.severity === "high"
                              ? "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300"
                              : issue.severity === "medium"
                                ? "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300"
                                : "bg-surface-secondary text-muted"
                          }`}
                        >
                          {t(`continuity.severity.${issue.severity}`)}
                        </span>
                        <span className="text-xs text-muted">
                          {t(`style.category.${issue.category}`)}
                        </span>
                        <span className="text-xs tabular-nums text-muted">
                          {Math.round(issue.confidence * 100)}%
                        </span>
                      </div>
                      <h4 className="mt-2 text-sm font-semibold text-foreground">
                        {issue.location}
                      </h4>
                      <p className="mt-2 text-sm leading-6 text-foreground">
                        {issue.deviation}
                      </p>
                      <div className="mt-3">
                        <p className="text-xs font-semibold text-muted">
                          {t("style.evidence")}
                        </p>
                        <ul className="mt-1 list-disc space-y-1 pl-5 text-sm leading-6 text-muted">
                          {issue.evidence.map((item) => (
                            <li key={item}>{item}</li>
                          ))}
                        </ul>
                      </div>

                      <div className="mt-4 grid gap-3 md:grid-cols-2">
                        <div className="bg-surface-secondary px-3 py-3">
                          <p className="text-xs font-semibold text-muted">
                            {t("style.targetEvidence")}
                          </p>
                          <div className="mt-2 space-y-3">
                            {issue.references
                              .filter(
                                (reference) =>
                                  reference.role === "target",
                              )
                              .map((reference) => (
                                <blockquote
                                  key={reference.evidence_id}
                                  className="text-sm leading-6 text-foreground"
                                >
                                  <p className="text-xs text-muted">
                                    {reference.label}
                                  </p>
                                  <p className="mt-1">
                                    {reference.excerpt}
                                  </p>
                                </blockquote>
                              ))}
                          </div>
                        </div>
                        <div className="bg-surface-secondary px-3 py-3">
                          <p className="text-xs font-semibold text-muted">
                            {t("style.baselineEvidence")}
                          </p>
                          <div className="mt-2 space-y-3">
                            {issue.references
                              .filter(
                                (reference) =>
                                  reference.role === "baseline",
                              )
                              .map((reference) => (
                                <blockquote
                                  key={reference.evidence_id}
                                  className="text-sm leading-6 text-foreground"
                                >
                                  <p className="text-xs text-muted">
                                    {reference.label}
                                  </p>
                                  <p className="mt-1">
                                    {reference.excerpt}
                                  </p>
                                </blockquote>
                              ))}
                          </div>
                        </div>
                      </div>

                      <dl className="mt-4 grid gap-3 text-sm md:grid-cols-2">
                        <div>
                          <dt className="font-medium text-foreground">
                            {t("style.baseline")}
                          </dt>
                          <dd className="mt-1 leading-6 text-muted">
                            {issue.baseline}
                          </dd>
                        </div>
                        <div>
                          <dt className="font-medium text-foreground">
                            {t("style.suggestion")}
                          </dt>
                          <dd className="mt-1 leading-6 text-muted">
                            {issue.suggestion}
                          </dd>
                        </div>
                      </dl>
                    </li>
                  ))}
                </ol>
              )}
            </div>
          ) : tab === "retrospective" && retrospectiveResult ? (
            <div className="space-y-5">
              {resultHeader(t("retrospective.resultTitle"))}
              <div>
                <p className="text-sm leading-6 text-foreground">
                  {retrospectiveResult.summary}
                </p>
                <p className="mt-2 text-xs leading-5 text-muted">
                  {retrospectiveResult.coverage}
                </p>
              </div>
              {retrospectiveResult.issues.length === 0 ? (
                <div className="rounded-lg bg-surface-secondary p-4 text-sm text-muted">
                  {t("retrospective.noIssues")}
                </div>
              ) : (
                <ol className="divide-y divide-border">
                  {retrospectiveResult.issues.map((issue, index) => (
                    <li
                      key={`${issue.location}-${index}`}
                      className="py-5 first:pt-0 last:pb-0"
                    >
                      <div className="flex flex-wrap items-center gap-2">
                        <span
                          className={`rounded px-2 py-0.5 text-xs font-semibold ${
                            issue.severity === "high"
                              ? "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300"
                              : issue.severity === "medium"
                                ? "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300"
                                : "bg-surface-secondary text-muted"
                          }`}
                        >
                          {t(`continuity.severity.${issue.severity}`)}
                        </span>
                        <span className="text-xs text-muted">
                          {t(
                            `retrospective.category.${issue.category}`,
                          )}
                        </span>
                        <span className="text-xs tabular-nums text-muted">
                          {Math.round(issue.confidence * 100)}%
                        </span>
                      </div>
                      <h4 className="mt-2 text-sm font-semibold text-foreground">
                        {issue.location}
                      </h4>
                      <p className="mt-2 text-sm leading-6 text-foreground">
                        {issue.problem}
                      </p>
                      <div className="mt-3">
                        <p className="text-xs font-semibold text-muted">
                          {t("retrospective.evidence")}
                        </p>
                        <ul className="mt-1 list-disc space-y-1 pl-5 text-sm leading-6 text-muted">
                          {issue.evidence.map((item) => (
                            <li key={item}>{item}</li>
                          ))}
                        </ul>
                      </div>
                      <div className="mt-4 space-y-3">
                        <p className="text-xs font-semibold text-muted">
                          {t("retrospective.references")}
                        </p>
                        {issue.references.map((reference) => (
                          <div
                            key={reference.evidence_id}
                            className="border-l-2 border-accent/30 bg-surface-secondary px-3 py-3"
                          >
                            <p className="text-xs font-medium text-muted">
                              {t(
                                `retrospective.referenceKind.${reference.kind}`,
                              )}
                              {" · "}
                              {reference.label}
                            </p>
                            <p className="mt-1 break-words text-sm leading-6 text-foreground">
                              {reference.excerpt}
                            </p>
                          </div>
                        ))}
                      </div>
                      <p className="mt-4 text-sm leading-6 text-muted">
                        <span className="font-medium text-foreground">
                          {t("retrospective.suggestion")}
                          {t("retrospective.labelSeparator")}
                        </span>
                        {issue.suggestion}
                      </p>
                    </li>
                  ))}
                </ol>
              )}
            </div>
          ) : (
            <div className="flex min-h-72 items-center justify-center text-center">
              <div className="max-w-md">
                <div className="mx-auto flex h-11 w-11 items-center justify-center rounded-full bg-accent/10 text-accent">
                  ✦
                </div>
                <h3 className="mt-4 font-semibold text-foreground">
                  {t("emptyResultTitle")}
                </h3>
                <p className="mt-2 text-sm leading-6 text-muted">
                  {t("emptyResultDescription")}
                </p>
              </div>
            </div>
          )}
          {toolMetadata && (
            <div className="mt-5 border-t border-border pt-3 text-xs leading-5 text-muted">
              {t("metadata", {
                agent: toolMetadata.agent_id,
                version: toolMetadata.agent_version,
                provider: toolMetadata.provider_alias,
                tokens: toolMetadata.usage.total_tokens ?? 0,
              })}
              {toolMetadata.context_report.truncated_sections.length > 0 && (
                <p className="text-amber-700 dark:text-amber-300">
                  {t("truncated", {
                    sections: Array.from(new Set(
                      toolMetadata.context_report.truncated_sections.map(
                        contextSectionKind,
                      ),
                    )).map((section) => metadataT(`contextSections.${section}`))
                      .join(metadataT("listSeparator")),
                  })}
                </p>
              )}
            </div>
          )}
        </section>
      </div>
    );
  };

  const visibleTabs: StudioTab[] = [...tools];

  return (
    <div className="h-full overflow-y-auto bg-surface-secondary/40 p-4 md:p-6">
      <div className="mx-auto max-w-7xl">
        {visibleTabs.length > 1 && (
          <div
            className="mb-5 flex w-fit max-w-full gap-1 overflow-x-auto rounded-lg border border-border bg-surface p-1"
            role="tablist"
            aria-label={t("tabsLabel")}
          >
            {visibleTabs.map(
            (item) => (
              <button
                key={item}
                type="button"
                role="tab"
                aria-selected={tab === item}
                onClick={() => {
                  setTab(item);
                  setRevisionSource(null);
                  if (item === "style" && scope === "novel") {
                    changeScope("chapter");
                  } else if (item === "retrospective") {
                    changeScope("volume");
                  }
                  setError(null);
                  setNotice(null);
                }}
                className={`shrink-0 rounded-md px-3 py-2 text-sm font-medium transition-colors ${
                  tab === item
                    ? "bg-accent text-white"
                    : "text-muted hover:bg-surface-secondary hover:text-foreground"
                }`}
              >
                {t(`tabs.${item}`)}
              </button>
            ),
            )}
          </div>
        )}

        {error && (
          <div
            role="alert"
            className="mb-4 rounded-lg border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-800 dark:border-red-900 dark:bg-red-950 dark:text-red-200"
          >
            {error}
          </div>
        )}
        {notice && (
          <div
            role="status"
            className="mb-4 rounded-lg border border-green-300 bg-green-50 px-4 py-3 text-sm text-green-800 dark:border-green-900 dark:bg-green-950 dark:text-green-200"
          >
            {notice}
          </div>
        )}

        {loadingCatalog ? (
          <div className="rounded-lg border border-border bg-surface px-6 py-16 text-center text-sm text-muted">
            {t("loading")}
          </div>
        ) : tab === "composer" ? (
          novelId ? (
            <AgentRevisionWorkspace
              novelId={novelId}
              volumes={volumes}
              chapters={chapters}
              source={revisionSource}
              onClearSource={leaveRevisionComposer}
              onProposalCreated={() => {
                const runId = revisionSource?.runId;
                leaveRevisionComposer();
                if (runId) onOpenHistory?.(runId);
              }}
            />
          ) : (
            <div className="rounded-lg border border-dashed border-border bg-surface px-6 py-12 text-center text-sm text-muted">
              {t("needNovelDescription")}
            </div>
          )
        ) : (
          renderTool()
        )}

      </div>
    </div>
  );
}
