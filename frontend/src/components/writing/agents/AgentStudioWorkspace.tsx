"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";

import { useAuth } from "@/components/auth/AuthProvider";
import { apiDelete, apiGet, apiPost, apiPut } from "@/lib/api";
import type {
  AgentCapability,
  AgentCapabilityId,
  AgentProfile,
  AgentProviderOption,
  AgentScope,
  AgentToolMetadata,
  ContinuityEvidenceReference,
  ContinuityReviewResult,
  CreativeInspirationResult,
  StyleConsistencyResult,
} from "@/types/agent";
import type { ChapterSummary, VolumeSummary } from "@/types/novel";
import AgentRevisionWorkspace, {
  type AgentRevisionSourceSelection,
} from "./AgentRevisionWorkspace";

interface Props {
  mode: "create" | "edit";
  novelId?: string;
  onNavigateReference?: (reference: ContinuityEvidenceReference) => void;
}

type StudioTab =
  | "creative"
  | "continuity"
  | "style"
  | "history"
  | "management";

interface AgentDraft {
  label: string;
  description: string;
  capability: AgentCapabilityId;
  instruction: string;
  providerAlias: string;
  temperature: string;
  topP: string;
  maxTokens: string;
  visibility: "private" | "shared";
  enabled: boolean;
}

const fieldClass =
  "w-full rounded-lg border border-border bg-surface px-3 py-2 text-sm text-foreground outline-none transition-colors placeholder:text-muted focus:border-accent";

function emptyDraft(): AgentDraft {
  return {
    label: "",
    description: "",
    capability: "creative_inspiration",
    instruction: "",
    providerAlias: "",
    temperature: "",
    topP: "",
    maxTokens: "",
    visibility: "private",
    enabled: true,
  };
}

function profileToDraft(profile: AgentProfile): AgentDraft {
  return {
    label: profile.label,
    description: profile.description,
    capability: profile.capabilities[0],
    instruction: profile.instruction,
    providerAlias: profile.provider_alias ?? "",
    temperature:
      profile.generation_params.temperature == null
        ? ""
        : String(profile.generation_params.temperature),
    topP:
      profile.generation_params.top_p == null
        ? ""
        : String(profile.generation_params.top_p),
    maxTokens:
      profile.generation_params.max_tokens == null
        ? ""
        : String(profile.generation_params.max_tokens),
    visibility: profile.visibility,
    enabled: profile.enabled,
  };
}

function draftPayload(draft: AgentDraft) {
  return {
    label: draft.label.trim(),
    description: draft.description.trim(),
    capability: draft.capability,
    instruction: draft.instruction.trim(),
    provider_alias: draft.providerAlias || null,
    generation_params: {
      temperature:
        draft.temperature === "" ? null : Number(draft.temperature),
      top_p: draft.topP === "" ? null : Number(draft.topP),
      max_tokens: draft.maxTokens === "" ? null : Number(draft.maxTokens),
    },
    visibility: draft.visibility,
    enabled: draft.enabled,
  };
}

export default function AgentStudioWorkspace({
  novelId,
  onNavigateReference,
}: Props) {
  const t = useTranslations("writing.agentStudio");
  const { user } = useAuth();
  const [tab, setTab] = useState<StudioTab>("creative");
  const [agents, setAgents] = useState<AgentProfile[]>([]);
  const [capabilities, setCapabilities] = useState<AgentCapability[]>([]);
  const [providers, setProviders] = useState<AgentProviderOption[]>([]);
  const [volumes, setVolumes] = useState<VolumeSummary[]>([]);
  const [chapters, setChapters] = useState<ChapterSummary[]>([]);
  const [loadingCatalog, setLoadingCatalog] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [scope, setScope] = useState<AgentScope>("novel");
  const [volumeId, setVolumeId] = useState("");
  const [chapterId, setChapterId] = useState("");
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
  const [toolMetadata, setToolMetadata] =
    useState<AgentToolMetadata | null>(null);
  const [revisionSource, setRevisionSource] =
    useState<AgentRevisionSourceSelection | null>(null);

  const [managementFilter, setManagementFilter] = useState<
    AgentCapabilityId | "all"
  >("all");
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);
  const [creatingAgent, setCreatingAgent] = useState(false);
  const [draft, setDraft] = useState<AgentDraft>(emptyDraft);
  const [savingAgent, setSavingAgent] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);

  const loadCatalog = useCallback(async () => {
    setLoadingCatalog(true);
    setError(null);
    try {
      const [agentResponse, capabilityResponse, providerResponse] =
        await Promise.all([
          apiGet<{ data: AgentProfile[] }>("/api/agents?include_disabled=true"),
          apiGet<{ data: AgentCapability[] }>("/api/agents/capabilities"),
          apiGet<{ data: AgentProviderOption[] }>("/api/agents/providers"),
        ]);
      setAgents(agentResponse.data);
      setCapabilities(capabilityResponse.data);
      setProviders(providerResponse.data);
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
    if (!novelId) return;
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

  const activeCapability =
    tab === "style"
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
      chapterId &&
      !visibleChapters.some((chapter) => chapter._id === chapterId)
    ) {
      setChapterId("");
    }
  }, [chapterId, visibleChapters]);

  const capabilityMap = useMemo(
    () =>
      new Map(
        capabilities.map((capability) => [
          capability.capability,
          capability,
        ]),
      ),
    [capabilities],
  );
  const selectedAgent = agents.find(
    (agent) => agent.agent_id === selectedAgentId,
  );
  const filteredAgents = agents.filter(
    (agent) =>
      managementFilter === "all" ||
      agent.capabilities.includes(managementFilter),
  );

  const selectManagedAgent = (profile: AgentProfile) => {
    setCreatingAgent(false);
    setSelectedAgentId(profile.agent_id);
    setDraft(profileToDraft(profile));
    setConfirmingDelete(false);
    setNotice(null);
  };

  const startCreate = () => {
    setCreatingAgent(true);
    setSelectedAgentId(null);
    setDraft(emptyDraft());
    setConfirmingDelete(false);
    setNotice(null);
  };

  const reloadAndSelect = async (agentId: string) => {
    const response = await apiGet<{ data: AgentProfile[] }>(
      "/api/agents?include_disabled=true",
    );
    setAgents(response.data);
    const profile = response.data.find((agent) => agent.agent_id === agentId);
    if (profile) selectManagedAgent(profile);
  };

  const saveAgent = async () => {
    setSavingAgent(true);
    setError(null);
    setNotice(null);
    try {
      const payload = draftPayload(draft);
      let response: { agent: AgentProfile };
      if (creatingAgent) {
        response = await apiPost<{ agent: AgentProfile }>(
          "/api/agents",
          payload,
        );
      } else if (selectedAgent?.editable) {
        response = await apiPut<{ agent: AgentProfile }>(
          `/api/agents/${selectedAgent.agent_id}`,
          { ...payload, expected_version: selectedAgent.version },
        );
      } else {
        return;
      }
      await reloadAndSelect(response.agent.agent_id);
      setNotice(t("management.saved"));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("saveFailed"));
    } finally {
      setSavingAgent(false);
    }
  };

  const cloneAgent = async (profile: AgentProfile) => {
    setSavingAgent(true);
    setError(null);
    setNotice(null);
    try {
      const response = await apiPost<{ agent: AgentProfile }>(
        `/api/agents/${profile.agent_id}/clone`,
        {},
      );
      await reloadAndSelect(response.agent.agent_id);
      setNotice(t("management.cloned"));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("saveFailed"));
    } finally {
      setSavingAgent(false);
    }
  };

  const deleteAgent = async () => {
    if (!selectedAgent?.editable) return;
    setSavingAgent(true);
    setError(null);
    try {
      await apiDelete(`/api/agents/${selectedAgent.agent_id}`);
      setSelectedAgentId(null);
      setCreatingAgent(false);
      setConfirmingDelete(false);
      setNotice(t("management.deleted"));
      await loadCatalog();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t("saveFailed"));
    } finally {
      setSavingAgent(false);
    }
  };

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
        setCreativeResult(response.result);
        setContinuityResult(null);
        setStyleResult(null);
        setToolMetadata(response);
      } else if (tab === "continuity") {
        const response = await apiPost<
          { result: ContinuityReviewResult } & AgentToolMetadata
        >("/api/llm/agent-continuity-review", {
          ...base,
          focus: focus.trim(),
        });
        setContinuityResult(response.result);
        setCreativeResult(null);
        setStyleResult(null);
        setToolMetadata(response);
      } else {
        const response = await apiPost<
          { result: StyleConsistencyResult } & AgentToolMetadata
        >("/api/llm/agent-style-consistency", {
          ...base,
          focus: styleFocus.trim(),
        });
        setStyleResult(response.result);
        setCreativeResult(null);
        setContinuityResult(null);
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
          : styleResult;
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
    setRevisionSource({
      runId: toolMetadata.run_id,
      sourceKind,
      sourceIndex,
      label,
      contextSnapshot: toolMetadata.context_snapshot,
    });
    setTab("history");
    setError(null);
    setNotice(null);
  };

  const scopeControls = (
    <div className="grid gap-4 md:grid-cols-3">
      <label className="space-y-1.5 text-sm">
        <span className="text-muted">{t("tool.scope")}</span>
        <select
          className={fieldClass}
          value={scope}
          onChange={(event) => setScope(event.target.value as AgentScope)}
        >
          {tab !== "style" && (
            <option value="novel">{t("tool.scopeNovel")}</option>
          )}
          <option value="volume">{t("tool.scopeVolume")}</option>
          <option value="chapter">{t("tool.scopeChapter")}</option>
        </select>
      </label>
      {scope === "volume" && (
        <label className="space-y-1.5 text-sm md:col-span-2">
          <span className="text-muted">{t("tool.volume")}</span>
          <select
            className={fieldClass}
            value={volumeId}
            onChange={(event) => setVolumeId(event.target.value)}
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
              onChange={(event) => setVolumeId(event.target.value)}
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
              onChange={(event) => setChapterId(event.target.value)}
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

  const resultHeader = (title: string) => (
    <div className="flex flex-wrap items-start justify-between gap-3 border-b border-border pb-4">
      <div>
        <p className="text-xs font-semibold uppercase tracking-[0.14em] text-accent">
          {t("previewBadge")}
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
                  : t("style.eyebrow")}
            </p>
            <h2 className="mt-1 text-xl font-semibold text-foreground">
              {tab === "creative"
                ? t("creative.title")
                : tab === "continuity"
                  ? t("continuity.title")
                  : t("style.title")}
            </h2>
            <p className="mt-2 text-sm leading-6 text-muted">
              {tab === "creative"
                ? t("creative.description")
                : tab === "continuity"
                  ? t("continuity.description")
                  : t("style.description")}
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
            ) : (
              <label className="block space-y-1.5 text-sm">
                <span className="text-muted">{t("style.focus")}</span>
                <textarea
                  className={`${fieldClass} min-h-28 resize-y`}
                  value={styleFocus}
                  onChange={(event) => setStyleFocus(event.target.value)}
                  placeholder={t("style.focusPlaceholder")}
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
                    sections:
                      toolMetadata.context_report.truncated_sections.join("、"),
                  })}
                </p>
              )}
            </div>
          )}
        </section>
      </div>
    );
  };

  const renderManagement = () => {
    const selectedCapability = selectedAgent
      ? capabilityMap.get(selectedAgent.capabilities[0])
      : capabilityMap.get(draft.capability);
    const formLocked = !creatingAgent && !selectedAgent?.editable;

    return (
      <div className="grid min-h-0 gap-5 lg:grid-cols-[minmax(16rem,0.7fr)_minmax(0,1.3fr)]">
        <section className="rounded-lg border border-border bg-surface p-4">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h2 className="font-semibold text-foreground">
                {t("management.catalogTitle")}
              </h2>
              <p className="mt-1 text-xs text-muted">
                {t("management.catalogHint")}
              </p>
            </div>
            <button
              type="button"
              onClick={startCreate}
              className="shrink-0 rounded-lg bg-accent px-3 py-2 text-sm font-semibold text-white"
            >
              {t("management.new")}
            </button>
          </div>
          <select
            className={`${fieldClass} mt-4`}
            value={managementFilter}
            onChange={(event) =>
              setManagementFilter(
                event.target.value as AgentCapabilityId | "all",
              )
            }
          >
            <option value="all">{t("management.allCapabilities")}</option>
            {capabilities.map((capability) => (
              <option key={capability.capability} value={capability.capability}>
                {capability.label}
              </option>
            ))}
          </select>
          <div className="mt-4 max-h-[58vh] space-y-1 overflow-y-auto pr-1">
            {filteredAgents.map((agent) => {
              const active = agent.agent_id === selectedAgentId;
              return (
                <button
                  key={agent.agent_id}
                  type="button"
                  onClick={() => selectManagedAgent(agent)}
                  className={`w-full rounded-lg border px-3 py-3 text-left transition-colors ${
                    active
                      ? "border-accent bg-accent/5"
                      : "border-transparent hover:border-border hover:bg-surface-secondary"
                  }`}
                >
                  <div className="flex items-start justify-between gap-2">
                    <span className="text-sm font-medium text-foreground">
                      {agent.label}
                    </span>
                    {!agent.enabled && (
                      <span className="rounded bg-surface-secondary px-1.5 py-0.5 text-[11px] text-muted">
                        {t("management.disabled")}
                      </span>
                    )}
                  </div>
                  <div className="mt-1 flex flex-wrap gap-2 text-xs text-muted">
                    <span>
                      {capabilityMap.get(agent.capabilities[0])?.label ??
                        agent.capabilities[0]}
                    </span>
                    <span>·</span>
                    <span>
                      {agent.origin === "builtin"
                        ? t("management.builtin")
                        : agent.editable
                          ? t("management.mine")
                          : t("management.shared")}
                    </span>
                  </div>
                </button>
              );
            })}
          </div>
        </section>

        <section className="rounded-lg border border-border bg-surface p-5">
          {!creatingAgent && !selectedAgent ? (
            <div className="flex min-h-72 items-center justify-center text-center">
              <div>
                <h3 className="font-semibold text-foreground">
                  {t("management.selectTitle")}
                </h3>
                <p className="mt-2 text-sm text-muted">
                  {t("management.selectDescription")}
                </p>
              </div>
            </div>
          ) : (
            <div className="space-y-5">
              <div className="flex flex-wrap items-start justify-between gap-3 border-b border-border pb-4">
                <div>
                  <p className="text-xs font-semibold uppercase tracking-[0.14em] text-accent">
                    {creatingAgent
                      ? t("management.new")
                      : selectedAgent?.origin === "builtin"
                        ? t("management.builtin")
                        : t("management.custom")}
                  </p>
                  <h2 className="mt-1 text-xl font-semibold text-foreground">
                    {creatingAgent
                      ? t("management.createTitle")
                      : selectedAgent?.label}
                  </h2>
                  {selectedCapability && (
                    <p className="mt-1 text-sm text-muted">
                      {selectedCapability.description}
                    </p>
                  )}
                </div>
                {selectedAgent &&
                  selectedCapability?.customizable &&
                  !selectedAgent.editable && (
                    <button
                      type="button"
                      disabled={savingAgent}
                      onClick={() => void cloneAgent(selectedAgent)}
                      className="rounded-lg border border-accent px-3 py-2 text-sm font-medium text-accent disabled:opacity-50"
                    >
                      {t("management.clone")}
                    </button>
                  )}
              </div>

              {formLocked && (
                <div className="rounded-lg bg-surface-secondary px-4 py-3 text-sm leading-6 text-muted">
                  {selectedCapability?.customizable
                    ? t("management.readonlyCloneHint")
                    : t("management.pipelineLockedHint")}
                </div>
              )}

              <div className="grid gap-4 md:grid-cols-2">
                <label className="space-y-1.5 text-sm">
                  <span className="text-muted">{t("management.name")}</span>
                  <input
                    className={fieldClass}
                    disabled={formLocked}
                    value={draft.label}
                    onChange={(event) =>
                      setDraft({ ...draft, label: event.target.value })
                    }
                  />
                </label>
                <label className="space-y-1.5 text-sm">
                  <span className="text-muted">
                    {t("management.capability")}
                  </span>
                  <select
                    className={fieldClass}
                    disabled={formLocked || !creatingAgent}
                    value={draft.capability}
                    onChange={(event) =>
                      setDraft({
                        ...draft,
                        capability: event.target
                          .value as AgentCapabilityId,
                      })
                    }
                  >
                    {(creatingAgent
                      ? capabilities.filter((item) => item.customizable)
                      : capabilities
                    ).map((capability) => (
                      <option
                        key={capability.capability}
                        value={capability.capability}
                      >
                        {capability.label}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="space-y-1.5 text-sm md:col-span-2">
                  <span className="text-muted">
                    {t("management.description")}
                  </span>
                  <input
                    className={fieldClass}
                    disabled={formLocked}
                    value={draft.description}
                    onChange={(event) =>
                      setDraft({ ...draft, description: event.target.value })
                    }
                  />
                </label>
                <label className="space-y-1.5 text-sm md:col-span-2">
                  <span className="text-muted">
                    {t("management.instruction")}
                  </span>
                  <textarea
                    className={`${fieldClass} min-h-40 resize-y font-mono text-[13px] leading-6`}
                    disabled={formLocked}
                    value={draft.instruction}
                    onChange={(event) =>
                      setDraft({ ...draft, instruction: event.target.value })
                    }
                    placeholder={t("management.instructionPlaceholder")}
                  />
                  {!formLocked && (
                    <span className="block text-xs text-muted">
                      {t("management.instructionHint")}
                    </span>
                  )}
                </label>
              </div>

              <div className="border-t border-border pt-5">
                <h3 className="text-sm font-semibold text-foreground">
                  {t("management.runtimeTitle")}
                </h3>
                <p className="mt-1 text-xs text-muted">
                  {t("management.runtimeHint")}
                </p>
                <div className="mt-4 grid gap-4 md:grid-cols-2 xl:grid-cols-4">
                  <label className="space-y-1.5 text-sm">
                    <span className="text-muted">
                      {t("management.provider")}
                    </span>
                    <select
                      className={fieldClass}
                      disabled={formLocked}
                      value={draft.providerAlias}
                      onChange={(event) =>
                        setDraft({
                          ...draft,
                          providerAlias: event.target.value,
                        })
                      }
                    >
                      <option value="">{t("management.inheritProvider")}</option>
                      {providers.map((provider) => (
                        <option key={provider.alias} value={provider.alias}>
                          {provider.alias} · {provider.model || provider.type}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="space-y-1.5 text-sm">
                    <span className="text-muted">temperature</span>
                    <input
                      type="number"
                      min={0}
                      max={2}
                      step={0.1}
                      className={fieldClass}
                      disabled={formLocked}
                      value={draft.temperature}
                      onChange={(event) =>
                        setDraft({
                          ...draft,
                          temperature: event.target.value,
                        })
                      }
                    />
                  </label>
                  <label className="space-y-1.5 text-sm">
                    <span className="text-muted">top_p</span>
                    <input
                      type="number"
                      min={0}
                      max={1}
                      step={0.1}
                      className={fieldClass}
                      disabled={formLocked}
                      value={draft.topP}
                      onChange={(event) =>
                        setDraft({ ...draft, topP: event.target.value })
                      }
                    />
                  </label>
                  <label className="space-y-1.5 text-sm">
                    <span className="text-muted">max_tokens</span>
                    <input
                      type="number"
                      min={1}
                      step={100}
                      className={fieldClass}
                      disabled={formLocked}
                      value={draft.maxTokens}
                      onChange={(event) =>
                        setDraft({
                          ...draft,
                          maxTokens: event.target.value,
                        })
                      }
                    />
                  </label>
                </div>
              </div>

              {!formLocked && (
                <div className="flex flex-wrap items-center justify-between gap-4 border-t border-border pt-5">
                  <div className="flex flex-wrap items-center gap-4">
                    <label className="flex items-center gap-2 text-sm text-muted">
                      <input
                        type="checkbox"
                        checked={draft.enabled}
                        onChange={(event) =>
                          setDraft({ ...draft, enabled: event.target.checked })
                        }
                      />
                      {t("management.enabled")}
                    </label>
                    <label className="flex items-center gap-2 text-sm text-muted">
                      <span>{t("management.visibility")}</span>
                      <select
                        className="rounded-lg border border-border bg-surface px-2 py-1.5 text-sm text-foreground"
                        value={draft.visibility}
                        onChange={(event) =>
                          setDraft({
                            ...draft,
                            visibility: event.target.value as
                              | "private"
                              | "shared",
                          })
                        }
                      >
                        <option value="private">
                          {t("management.private")}
                        </option>
                        {user?.role === "admin" && (
                          <option value="shared">
                            {t("management.shared")}
                          </option>
                        )}
                      </select>
                    </label>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    {selectedAgent?.editable &&
                      (confirmingDelete ? (
                        <>
                          <span className="text-sm text-muted">
                            {t("management.confirmDelete")}
                          </span>
                          <button
                            type="button"
                            disabled={savingAgent}
                            onClick={() => void deleteAgent()}
                            className="rounded-lg border border-red-300 px-3 py-2 text-sm font-medium text-red-600 disabled:opacity-50"
                          >
                            {t("management.delete")}
                          </button>
                          <button
                            type="button"
                            onClick={() => setConfirmingDelete(false)}
                            className="rounded-lg border border-border px-3 py-2 text-sm text-muted"
                          >
                            {t("management.cancel")}
                          </button>
                        </>
                      ) : (
                        <button
                          type="button"
                          onClick={() => setConfirmingDelete(true)}
                          className="rounded-lg px-3 py-2 text-sm text-red-600"
                        >
                          {t("management.delete")}
                        </button>
                      ))}
                    <button
                      type="button"
                      disabled={
                        savingAgent ||
                        draft.label.trim().length < 2 ||
                        draft.instruction.trim().length < 20
                      }
                      onClick={() => void saveAgent()}
                      className="rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {savingAgent
                        ? t("management.saving")
                        : t("management.save")}
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}
        </section>
      </div>
    );
  };

  return (
    <div className="h-full overflow-y-auto bg-surface-secondary/40 p-4 md:p-6">
      <div className="mx-auto max-w-7xl">
        <header className="mb-5 flex flex-wrap items-end justify-between gap-4">
          <div>
            <p className="text-xs font-semibold uppercase tracking-[0.16em] text-accent">
              {t("eyebrow")}
            </p>
            <h1 className="mt-1 text-2xl font-semibold tracking-tight text-foreground">
              {t("title")}
            </h1>
            <p className="mt-2 max-w-3xl text-sm leading-6 text-muted">
              {t("description")}
            </p>
          </div>
          <span className="rounded-full border border-border bg-surface px-3 py-1.5 text-xs text-muted">
            {t("skillBoundary")}
          </span>
        </header>

        <div
          className="mb-5 flex w-fit max-w-full gap-1 overflow-x-auto rounded-lg border border-border bg-surface p-1"
          role="tablist"
          aria-label={t("tabsLabel")}
        >
          {(
            [
              "creative",
              "continuity",
              "style",
              "history",
              "management",
            ] as StudioTab[]
          ).map(
            (item) => (
              <button
                key={item}
                type="button"
                role="tab"
                aria-selected={tab === item}
                onClick={() => {
                  setTab(item);
                  if (item === "style" && scope === "novel") {
                    setScope("chapter");
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
        ) : tab === "management" ? (
          renderManagement()
        ) : tab === "history" ? (
          novelId ? (
            <AgentRevisionWorkspace
              novelId={novelId}
              volumes={volumes}
              chapters={chapters}
              source={revisionSource}
              onClearSource={() => setRevisionSource(null)}
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
