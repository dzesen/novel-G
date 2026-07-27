"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { Button, Switch } from "@heroui/react";
import { apiGet, apiPost, apiPostSSE } from "@/lib/api";
import {
  clearAICreateCache,
  hasAICreateCachedSteps,
  isSameAICreateInput,
  loadAICreateCache,
  saveAICreateCache,
  trimCachedStepsToPrefix,
  type AICreateCacheInput,
} from "@/lib/aiCreateCache";
import {
  OptionalNumberParam,
  OptionalSliderParam,
  OptionalTextParam,
  SwitchParam,
} from "@/components/shared/OptionalParamControls";
import type { AICreateCachedSteps, AICreateRequest, AICreateResponse, AICreateStepKey } from "@/types/novel";
import type {
  AgentProfile,
  CreativeDirection,
  CreativeDirectionSelection,
  CreativeDirectorResponse,
} from "@/types/agent";

interface AICreateStepperProps {
  onComplete: (
    result: AICreateResponse,
    chapters: number,
    wordsPerChapter: number,
    creativeDirection: CreativeDirectionSelection | null,
  ) => void;
}

type StepStatus = "pending" | "running" | "done" | "error";

interface StepState {
  key: AICreateStepKey;
  status: StepStatus;
  cached?: boolean;
  error?: string;
}

const STEPS: AICreateStepKey[] = ["expand_idea", "extract_idea", "core_seed", "novel_meta"];

function isStepKey(value: unknown): value is AICreateStepKey {
  return typeof value === "string" && STEPS.includes(value as AICreateStepKey);
}

function buildStepStates(cachedSteps: AICreateCachedSteps, failedStep?: AICreateStepKey): StepState[] {
  return STEPS.map((key) => {
    if (cachedSteps[key]) {
      return { key, status: "done", cached: true };
    }
    if (failedStep === key) {
      return { key, status: "error", error: "" };
    }
    return { key, status: "pending" };
  });
}

function mergeStepData(
  current: AICreateCachedSteps,
  step: AICreateStepKey,
  data: unknown,
): AICreateCachedSteps {
  const next: AICreateCachedSteps = { ...current };

  // 每个 step 的返回结构不同，按明确分支合并，避免把错误字段写入缓存。
  if (step === "expand_idea") {
    next.expand_idea = data as AICreateCachedSteps["expand_idea"];
  } else if (step === "extract_idea") {
    next.extract_idea = data as AICreateCachedSteps["extract_idea"];
  } else if (step === "core_seed") {
    next.core_seed = data as AICreateCachedSteps["core_seed"];
  } else if (step === "novel_meta") {
    next.novel_meta = data as AICreateCachedSteps["novel_meta"];
  }

  return trimCachedStepsToPrefix(next);
}

function mergePartialResult(current: AICreateCachedSteps, data: unknown): AICreateCachedSteps {
  if (!data || typeof data !== "object" || Array.isArray(data)) {
    return current;
  }

  return trimCachedStepsToPrefix({
    ...current,
    ...(data as AICreateCachedSteps),
  });
}

export default function AICreateStepper({ onComplete }: AICreateStepperProps) {
  const t = useTranslations("create");
  const [initialCache] = useState(() => loadAICreateCache());
  const initialSteps = initialCache?.steps ?? {};
  const initialCreativeDirection =
    initialCache?.input.creative_direction ?? null;
  const [idea, setIdea] = useState(initialCache?.input.user_idea ?? "");
  const [chapters, setChapters] = useState(initialCache?.input.number_of_chapters ?? 600);
  const [wordsPerChapter, setWordsPerChapter] = useState(initialCache?.input.words_per_chapter ?? 3000);
  const [directorEnabled, setDirectorEnabled] = useState(
    initialCreativeDirection !== null,
  );
  const [directorAgents, setDirectorAgents] = useState<AgentProfile[]>([]);
  const [selectedDirectorId, setSelectedDirectorId] = useState(
    initialCreativeDirection?.agent_id ?? "creative_director",
  );
  const [directorInstruction, setDirectorInstruction] = useState("");
  const [directorPreview, setDirectorPreview] =
    useState<CreativeDirectorResponse | null>(null);
  const [selectedDirectionIndex, setSelectedDirectionIndex] = useState<
    number | null
  >(initialCreativeDirection ? 0 : null);
  const [confirmedDirection, setConfirmedDirection] =
    useState<CreativeDirectionSelection | null>(initialCreativeDirection);
  const [directorAdjustments, setDirectorAdjustments] = useState(
    initialCreativeDirection?.user_adjustments ?? "",
  );
  const [isLoadingDirectorAgents, setIsLoadingDirectorAgents] = useState(false);
  const [isDirecting, setIsDirecting] = useState(false);
  const [directorAgentError, setDirectorAgentError] = useState("");
  const [directorError, setDirectorError] = useState("");
  const [showGenParams, setShowGenParams] = useState(false);
  const [temperature, setTemperature] = useState<number | null>(null);
  const [topP, setTopP] = useState<number | null>(null);
  const [maxTokens, setMaxTokens] = useState<number | null>(null);
  const [presencePenalty, setPresencePenalty] = useState<number | null>(null);
  const [frequencyPenalty, setFrequencyPenalty] = useState<number | null>(null);
  const [systemPrompt, setSystemPrompt] = useState<string | null>(null);
  const [allowFailureRetry, setAllowFailureRetry] = useState(true);
  const [cachedSteps, setCachedSteps] = useState<AICreateCachedSteps>(initialSteps);
  const [steps, setSteps] = useState<StepState[]>(
    buildStepStates(initialSteps, initialCache?.failed_step),
  );
  const [isRunning, setIsRunning] = useState(false);
  const [result, setResult] = useState<AICreateResponse | null>(null);
  const cachedStepsRef = useRef<AICreateCachedSteps>(initialSteps);

  useEffect(() => {
    if (!directorEnabled || directorAgents.length > 0) return;

    let active = true;
    setIsLoadingDirectorAgents(true);
    setDirectorAgentError("");
    apiGet<{ data: AgentProfile[] }>("/api/agents?capability=novel_direction")
      .then((response) => {
        if (!active) return;
        setDirectorAgents(response.data);
        if (response.data.length === 0) {
          setDirectorAgentError(t("director.noAgents"));
        }
      })
      .catch((error) => {
        if (!active) return;
        setDirectorAgentError(
          error instanceof Error ? error.message : t("director.loadFailed"),
        );
      })
      .finally(() => {
        if (active) setIsLoadingDirectorAgents(false);
      });

    return () => {
      active = false;
    };
  }, [directorAgents.length, directorEnabled, t]);

  const stepLabelMap: Record<AICreateStepKey, string> = {
    expand_idea: t("stepExpandIdea"),
    extract_idea: t("stepExtractIdea"),
    core_seed: t("stepCoreSeed"),
    novel_meta: t("stepNovelMeta"),
  };

  const hasFailedStep = steps.some((step) => step.status === "error");
  const hasCachedSteps = hasAICreateCachedSteps(cachedSteps);

  const getCurrentInput = (
    nextIdea = idea,
    nextChapters = chapters,
    nextWordsPerChapter = wordsPerChapter,
    nextCreativeDirection = directorEnabled ? confirmedDirection : null,
  ): AICreateCacheInput => ({
    user_idea: nextIdea.trim(),
    number_of_chapters: nextChapters,
    words_per_chapter: nextWordsPerChapter,
    creative_direction: nextCreativeDirection,
  });

  const setCachedStepsState = (nextSteps: AICreateCachedSteps) => {
    cachedStepsRef.current = nextSteps;
    setCachedSteps(nextSteps);
  };

  const resetGenerationState = () => {
    clearAICreateCache();
    setResult(null);
    setCachedStepsState({});
    setSteps(buildStepStates({}));
  };

  const resetDirectorPreview = () => {
    setDirectorPreview(null);
    setSelectedDirectionIndex(null);
    setConfirmedDirection(null);
    setDirectorAdjustments("");
    setDirectorError("");
  };

  const handleIdeaChange = (value: string) => {
    if (value !== idea) {
      resetGenerationState();
      resetDirectorPreview();
    }
    setIdea(value);
  };

  const handleChaptersChange = (value: number) => {
    if (value !== chapters) {
      resetGenerationState();
      resetDirectorPreview();
    }
    setChapters(value);
  };

  const handleWordsPerChapterChange = (value: number) => {
    if (value !== wordsPerChapter) {
      resetGenerationState();
      resetDirectorPreview();
    }
    setWordsPerChapter(value);
  };

  const handleDirectorToggle = (enabled: boolean) => {
    if (enabled === directorEnabled) return;
    resetGenerationState();
    resetDirectorPreview();
    setDirectorEnabled(enabled);
  };

  const handleDirectorAgentChange = (agentId: string) => {
    if (agentId === selectedDirectorId) return;
    resetGenerationState();
    resetDirectorPreview();
    setSelectedDirectorId(agentId);
  };

  const handleDirectorInstructionChange = (value: string) => {
    if (value !== directorInstruction && (directorPreview || confirmedDirection)) {
      resetGenerationState();
      resetDirectorPreview();
    }
    setDirectorInstruction(value);
  };

  const startCreativeDirector = async () => {
    const originalIdea = idea.trim();
    if (!originalIdea || !selectedDirectorId) return;

    setDirectorError("");
    setIsDirecting(true);

    try {
      const response = await apiPost<CreativeDirectorResponse>(
        "/api/llm/creative-director",
        {
          user_idea: originalIdea,
          number_of_chapters: chapters,
          words_per_chapter: wordsPerChapter,
          agent_id: selectedDirectorId,
          direction_count: 3,
          instruction: directorInstruction.trim(),
          ...(temperature != null && { temperature }),
          ...(topP != null && { top_p: topP }),
          ...(maxTokens != null && { max_tokens: maxTokens }),
          ...(presencePenalty != null && { presence_penalty: presencePenalty }),
          ...(frequencyPenalty != null && { frequency_penalty: frequencyPenalty }),
          ...(systemPrompt != null && { system_prompt: systemPrompt }),
          allow_failure_retry: allowFailureRetry,
        },
      );
      resetGenerationState();
      setDirectorPreview(response);
      setSelectedDirectionIndex(null);
      setConfirmedDirection(null);
      setDirectorAdjustments("");
    } catch (error) {
      setDirectorError(
        error instanceof Error ? error.message : t("director.generateFailed"),
      );
    } finally {
      setIsDirecting(false);
    }
  };

  const selectCreativeDirection = (
    direction: CreativeDirection,
    index: number,
  ) => {
    if (!directorPreview) return;
    resetGenerationState();
    const selection: CreativeDirectionSelection = {
      agent_id: directorPreview.agent_id,
      agent_version: directorPreview.agent_version,
      provider_alias: directorPreview.provider_alias || null,
      direction,
      user_adjustments: "",
    };
    setSelectedDirectionIndex(index);
    setConfirmedDirection(selection);
    setDirectorAdjustments("");
    saveAICreateCache(
      getCurrentInput(idea, chapters, wordsPerChapter, selection),
      {},
    );
  };

  const handleDirectorAdjustmentsChange = (value: string) => {
    setDirectorAdjustments(value);
    if (!confirmedDirection) return;

    resetGenerationState();
    const selection = {
      ...confirmedDirection,
      user_adjustments: value,
    };
    setConfirmedDirection(selection);
    saveAICreateCache(
      getCurrentInput(idea, chapters, wordsPerChapter, selection),
      {},
    );
  };

  const startGeneration = async () => {
    const input = getCurrentInput();
    if (!input.user_idea || (directorEnabled && !input.creative_direction)) {
      return;
    }

    const storedCache = loadAICreateCache();
    const reusableCachedSteps = storedCache && isSameAICreateInput(storedCache, input)
      ? storedCache.steps
      : cachedStepsRef.current;
    const normalizedCachedSteps = trimCachedStepsToPrefix(reusableCachedSteps);

    setIsRunning(true);
    setResult(null);
    setCachedStepsState(normalizedCachedSteps);
    setSteps(buildStepStates(normalizedCachedSteps));

    const payload: AICreateRequest = {
      user_idea: input.user_idea,
      number_of_chapters: input.number_of_chapters,
      words_per_chapter: input.words_per_chapter,
      ...(input.creative_direction && {
        creative_direction: input.creative_direction,
      }),
      ...(hasAICreateCachedSteps(normalizedCachedSteps) && { cached_steps: normalizedCachedSteps }),
      ...(temperature != null && { temperature }),
      ...(topP != null && { top_p: topP }),
      ...(maxTokens != null && { max_tokens: maxTokens }),
      ...(presencePenalty != null && { presence_penalty: presencePenalty }),
      ...(frequencyPenalty != null && { frequency_penalty: frequencyPenalty }),
      ...(systemPrompt != null && { system_prompt: systemPrompt }),
      allow_failure_retry: allowFailureRetry,
    };

    try {
      await apiPostSSE(
        "/api/llm/create-novel-by-ai",
        payload,
        (event, data) => {
          if (event === "step") {
            const stepName = data.step;
            const status = data.status as StepStatus;
            if (!isStepKey(stepName)) {
              return;
            }

            if (status === "done" && data.data) {
              const nextCachedSteps = mergeStepData(cachedStepsRef.current, stepName, data.data);
              setCachedStepsState(nextCachedSteps);
              saveAICreateCache(input, nextCachedSteps);
            } else if (status === "error") {
              saveAICreateCache(input, cachedStepsRef.current, stepName);
            }

            setSteps((prev) =>
              prev.map((step) =>
                step.key === stepName
                  ? {
                      ...step,
                      status,
                      cached: status === "done" ? true : step.cached && status !== "running",
                      error: typeof data.error === "string" ? data.error : undefined,
                    }
                  : step,
              ),
            );
          } else if (event === "done") {
            const failedStep = isStepKey(data.failed_step) ? data.failed_step : undefined;
            if (data.partial_result) {
              const nextCachedSteps = mergePartialResult(cachedStepsRef.current, data.partial_result);
              setCachedStepsState(nextCachedSteps);
              saveAICreateCache(input, nextCachedSteps, failedStep);
            }

            if (data.success && data.result) {
              const res = data.result as AICreateResponse;
              setResult(res);
              onComplete(
                res,
                input.number_of_chapters,
                input.words_per_chapter,
                input.creative_direction,
              );
            }
          }
        },
      );
    } catch (err) {
      // 网络或浏览器层异常没有后端 step 事件，只能标记当前第一个未完成步骤。
      setSteps((prev) => {
        const firstPending = prev.findIndex(
          (step) => step.status === "pending" || step.status === "running",
        );
        if (firstPending < 0) return prev;
        const failedStep = prev[firstPending].key;
        saveAICreateCache(input, cachedStepsRef.current, failedStep);
        return prev.map((step, index) =>
          index === firstPending
            ? { ...step, status: "error", error: err instanceof Error ? err.message : String(err) }
            : step,
        );
      });
    } finally {
      setIsRunning(false);
    }
  };

  const buttonLabel = isRunning
    ? t("generating")
    : hasFailedStep
      ? t("retryFailedStep")
      : hasCachedSteps
        ? t("continueAI")
        : t("startAI");

  const displayedDirections = directorPreview?.result.directions ??
    (confirmedDirection ? [confirmedDirection.direction] : []);
  const directorLocked = isRunning || isDirecting;
  const requiresDirection = directorEnabled && confirmedDirection === null;

  return (
    <div className="space-y-6 p-1">
      {/* Idea Input */}
      <div>
        <label className="block text-sm font-medium text-foreground mb-2">
          {t("ideaLabel")}
        </label>
        <textarea
          className="w-full rounded-lg border border-border bg-background p-3 text-sm text-foreground resize-y min-h-[120px] focus:outline-none focus:ring-2 focus:ring-primary"
          placeholder={t("ideaPlaceholder")}
          value={idea}
          onChange={(e) => handleIdeaChange(e.target.value)}
          disabled={directorLocked}
        />
      </div>

      {/* Chapter / Words Config */}
      <div className="grid grid-cols-2 gap-4">
        <div>
          <label className="block text-sm font-medium text-foreground mb-1">
            {t("chaptersLabel")}
          </label>
          <input
            type="number"
            className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary"
            value={chapters}
            onChange={(e) => handleChaptersChange(Number(e.target.value) || 1)}
            min={1}
            max={1000}
            disabled={directorLocked}
          />
        </div>
        <div>
          <label className="block text-sm font-medium text-foreground mb-1">
            {t("wordsPerChapterLabel")}
          </label>
          <input
            type="number"
            className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary"
            value={wordsPerChapter}
            onChange={(e) => handleWordsPerChapterChange(Number(e.target.value) || 1000)}
            min={500}
            max={10000}
            disabled={directorLocked}
          />
        </div>
      </div>

      {/* Optional pre-creation Creative Director */}
      <section className="rounded-xl border border-border bg-surface-secondary/20 p-4">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <h3 className="text-sm font-semibold text-foreground">
              {t("director.title")}
            </h3>
            <p className="mt-1 max-w-2xl text-xs leading-5 text-muted">
              {t("director.description")}
            </p>
          </div>
          <Switch
            aria-label={t("director.toggle")}
            isSelected={directorEnabled}
            isDisabled={directorLocked}
            onChange={handleDirectorToggle}
            className="shrink-0"
          >
            <Switch.Control>
              <Switch.Thumb />
            </Switch.Control>
          </Switch>
        </div>

        {directorEnabled && (
          <div className="mt-4 space-y-4 border-t border-border pt-4">
            <div className="grid gap-3 sm:grid-cols-2">
              <div>
                <label
                  htmlFor="creative-director-agent"
                  className="mb-1 block text-xs font-medium text-foreground"
                >
                  {t("director.agentLabel")}
                </label>
                <select
                  id="creative-director-agent"
                  className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary"
                  value={selectedDirectorId}
                  onChange={(event) =>
                    handleDirectorAgentChange(event.target.value)
                  }
                  disabled={directorLocked || isLoadingDirectorAgents}
                >
                  {selectedDirectorId &&
                    !directorAgents.some(
                      (agent) => agent.agent_id === selectedDirectorId,
                    ) && (
                      <option value={selectedDirectorId}>
                        {confirmedDirection
                          ? `${selectedDirectorId} (${t("director.cachedAgent")})`
                          : selectedDirectorId}
                      </option>
                    )}
                  {directorAgents.map((agent) => (
                    <option key={agent.agent_id} value={agent.agent_id}>
                      {agent.label}
                      {agent.provider_alias
                        ? ` · ${agent.provider_alias}`
                        : ""}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label
                  htmlFor="creative-director-instruction"
                  className="mb-1 block text-xs font-medium text-foreground"
                >
                  {t("director.instructionLabel")}
                </label>
                <input
                  id="creative-director-instruction"
                  className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary"
                  value={directorInstruction}
                  onChange={(event) =>
                    handleDirectorInstructionChange(event.target.value)
                  }
                  placeholder={t("director.instructionPlaceholder")}
                  maxLength={2000}
                  disabled={directorLocked}
                />
              </div>
            </div>

            <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
              <Button
                variant="secondary"
                isDisabled={
                  directorLocked ||
                  !idea.trim() ||
                  !selectedDirectorId ||
                  directorAgents.length === 0
                }
                onPress={startCreativeDirector}
              >
                {isDirecting
                  ? t("director.generating")
                  : directorPreview || confirmedDirection
                    ? t("director.regenerate")
                    : t("director.generate")}
              </Button>
              <p className="text-xs leading-5 text-muted">
                {t("director.costHint")}
              </p>
            </div>

            {(directorAgentError || directorError) && (
              <p
                role="alert"
                className="rounded-lg bg-red-50 px-3 py-2 text-xs text-red-700 dark:bg-red-950/30 dark:text-red-300"
              >
                {directorAgentError || directorError}
              </p>
            )}

            {directorPreview && (
              <div className="space-y-1">
                <p className="text-sm leading-6 text-foreground">
                  {directorPreview.result.framing}
                </p>
                <p className="text-xs text-muted">
                  {t("director.usage", {
                    attempts: directorPreview.attempts.length,
                    tokens: directorPreview.usage.total_tokens ?? 0,
                  })}
                </p>
              </div>
            )}

            {displayedDirections.length > 0 && (
              <div className="space-y-3">
                <p className="text-xs font-medium text-foreground">
                  {t("director.chooseHint")}
                </p>
                <div
                  className={
                    displayedDirections.length === 1
                      ? "grid max-w-3xl gap-3"
                      : "grid gap-3 lg:grid-cols-3"
                  }
                >
                  {displayedDirections.map((direction, index) => {
                    const selected =
                      confirmedDirection?.direction === direction ||
                      selectedDirectionIndex === index;
                    return (
                      <button
                        key={`${direction.title}-${index}`}
                        type="button"
                        aria-pressed={selected}
                        className={`rounded-xl border p-4 text-left transition-colors focus:outline-none focus:ring-2 focus:ring-primary ${
                          selected
                            ? "border-primary bg-primary/5"
                            : "border-border bg-background hover:border-primary/50"
                        }`}
                        onClick={() =>
                          selectCreativeDirection(direction, index)
                        }
                        disabled={!directorPreview || directorLocked}
                      >
                        <div className="flex items-start justify-between gap-3">
                          <h4 className="text-sm font-semibold text-foreground">
                            {direction.title}
                          </h4>
                          {selected && (
                            <span className="shrink-0 rounded-full bg-primary/10 px-2 py-0.5 text-[11px] font-medium text-primary">
                              {t("director.selected")}
                            </span>
                          )}
                        </div>
                        <p className="mt-2 text-xs leading-5 text-foreground">
                          {direction.pitch}
                        </p>
                        <dl className="mt-3 space-y-2 text-xs leading-5">
                          <div>
                            <dt className="font-medium text-foreground">
                              {t("director.storyEngine")}
                            </dt>
                            <dd
                              className={
                                selected ? "text-muted" : "line-clamp-5 text-muted"
                              }
                            >
                              {direction.story_engine}
                            </dd>
                          </div>
                          <div>
                            <dt className="font-medium text-foreground">
                              {t("director.coreConflict")}
                            </dt>
                            <dd
                              className={
                                selected ? "text-muted" : "line-clamp-4 text-muted"
                              }
                            >
                              {direction.core_conflict}
                            </dd>
                          </div>
                          {direction.risks.length > 0 && (
                            <div>
                              <dt className="font-medium text-foreground">
                                {t("director.risks")}
                              </dt>
                              <dd
                                className={
                                  selected ? "text-muted" : "line-clamp-3 text-muted"
                                }
                              >
                                {direction.risks.join(" · ")}
                              </dd>
                            </div>
                          )}
                        </dl>
                      </button>
                    );
                  })}
                </div>
              </div>
            )}

            {confirmedDirection && (
              <div>
                <label
                  htmlFor="creative-director-adjustments"
                  className="mb-1 block text-xs font-medium text-foreground"
                >
                  {t("director.adjustmentsLabel")}
                </label>
                <textarea
                  id="creative-director-adjustments"
                  className="min-h-20 w-full resize-y rounded-lg border border-border bg-background p-3 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary"
                  value={directorAdjustments}
                  onChange={(event) =>
                    handleDirectorAdjustmentsChange(event.target.value)
                  }
                  placeholder={t("director.adjustmentsPlaceholder")}
                  maxLength={2000}
                  disabled={directorLocked}
                />
                <p className="mt-1 text-xs leading-5 text-muted">
                  {t("director.constraintHint")}
                </p>
              </div>
            )}
          </div>
        )}
      </section>

      {/* Generation Parameters (collapsible) */}
      <div>
        <button
          type="button"
          className="flex items-center gap-2 text-sm font-medium text-muted hover:text-foreground transition-colors py-1"
          onClick={() => setShowGenParams(!showGenParams)}
          disabled={directorLocked}
        >
          <svg
            className={`w-4 h-4 transition-transform ${showGenParams ? "rotate-90" : ""}`}
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
          >
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
          </svg>
          {t("genParams.title")}
        </button>
        {showGenParams && (
          <div className="border border-border rounded-lg p-4 mt-1 space-y-3 bg-surface-secondary/30">
            <p className="text-xs text-muted">{t("genParams.hint")}</p>

            <OptionalSliderParam
              label={t("genParams.temperature")}
              value={temperature}
              onToggle={(on) => setTemperature(on ? 0.7 : null)}
              onValueChange={setTemperature}
              min={0} max={2} step={0.05}
            />
            <OptionalSliderParam
              label={t("genParams.topP")}
              value={topP}
              onToggle={(on) => setTopP(on ? 0.9 : null)}
              onValueChange={setTopP}
              min={0} max={1} step={0.05}
            />
            <OptionalNumberParam
              label={t("genParams.maxTokens")}
              value={maxTokens}
              onToggle={(on) => setMaxTokens(on ? 4096 : null)}
              onValueChange={setMaxTokens}
              min={256} max={1000000} step={256}
            />
            <SwitchParam
              label={t("genParams.allowFailureRetry")}
              description={t("genParams.allowFailureRetryHint")}
              value={allowFailureRetry}
              onChange={setAllowFailureRetry}
            />
            <OptionalSliderParam
              label={t("genParams.presencePenalty")}
              value={presencePenalty}
              onToggle={(on) => setPresencePenalty(on ? 0 : null)}
              onValueChange={setPresencePenalty}
              min={-2} max={2} step={0.1}
            />
            <OptionalSliderParam
              label={t("genParams.frequencyPenalty")}
              value={frequencyPenalty}
              onToggle={(on) => setFrequencyPenalty(on ? 0 : null)}
              onValueChange={setFrequencyPenalty}
              min={-2} max={2} step={0.1}
            />
            <OptionalTextParam
              label={t("genParams.systemPrompt")}
              value={systemPrompt}
              onToggle={(on) => setSystemPrompt(on ? "" : null)}
              onValueChange={setSystemPrompt}
              placeholder={t("genParams.systemPromptPlaceholder")}
            />
          </div>
        )}
      </div>

      {/* Step Progress */}
      {(isRunning || result || hasCachedSteps || steps.some((step) => step.status === "error")) && (
        <div className="space-y-3">
          {steps.map((step, idx) => (
            <div key={step.key} className="flex items-center gap-3">
              {/* Step Indicator */}
              <div
                className={`w-8 h-8 rounded-full flex items-center justify-center shrink-0 text-sm font-semibold ${
                  step.status === "done"
                    ? "bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400"
                    : step.status === "running"
                      ? "bg-primary/10 text-primary"
                      : step.status === "error"
                        ? "bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400"
                        : "bg-muted/30 text-muted"
                }`}
              >
                {step.status === "done" ? (
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                    <polyline points="20 6 9 17 4 12" />
                  </svg>
                ) : step.status === "running" ? (
                  <div className="animate-spin w-4 h-4 border-2 border-current border-t-transparent rounded-full" />
                ) : step.status === "error" ? (
                  "✕"
                ) : (
                  idx + 1
                )}
              </div>

              {/* Step Label */}
              <div className="flex-1">
                <p
                  className={`text-sm font-medium ${
                    step.status === "done"
                      ? "text-green-700 dark:text-green-400"
                      : step.status === "running"
                        ? "text-primary"
                        : step.status === "error"
                          ? "text-red-600 dark:text-red-400"
                          : "text-muted"
                  }`}
                >
                  {stepLabelMap[step.key]}
                </p>
                {step.cached && step.status === "done" && (
                  <p className="text-xs text-green-600 dark:text-green-400 mt-0.5">{t("stepCached")}</p>
                )}
                {step.error && (
                  <p className="text-xs text-red-500 mt-0.5">{step.error}</p>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Start Button */}
      <Button
        variant="primary"
        className="w-full"
        isDisabled={directorLocked || !idea.trim() || requiresDirection}
        onPress={startGeneration}
      >
        {requiresDirection ? t("director.confirmFirst") : buttonLabel}
      </Button>
    </div>
  );
}
