import type { AppConfig, ProviderConfig, WorkflowStep } from "../../types/config";

export const JUDGE_WORKFLOW = "remediate_chapter_prose_by_agent";
export const JUDGE_STEP = "outline_adherence";

export function resolveJudgeSettings(config: AppConfig) {
  const workflow = config.llm.workflows[JUDGE_WORKFLOW];
  const step = workflow?.steps[JUDGE_STEP];
  const inheritedAlias = workflow?.default_provider || config.llm.default_provider || "";
  const providerAlias = step?.provider || inheritedAlias;
  const provider = config.llm.providers[providerAlias];
  const writerWorkflow = config.llm.workflows.write_chapter_by_ai;
  const writerAlias = writerWorkflow?.steps.chapter_content?.provider
    || writerWorkflow?.default_provider || config.llm.default_provider;
  const writerModel = config.llm.providers[writerAlias]?.default_model?.trim().toLowerCase();
  return {
    providerAlias, provider, inheritedAlias,
    overrideAlias: step?.provider || "",
    timeoutOverride: step?.timeout_seconds ?? null,
    effectiveTimeout: step?.timeout_seconds ?? provider?.timeout_seconds ?? null,
    sameAsDefaultWriter: Boolean(writerModel && provider?.default_model?.trim().toLowerCase() === writerModel),
  };
}

export function updateJudgeStep(config: AppConfig, changes: Partial<WorkflowStep>): AppConfig {
  const current = config.llm.workflows[JUDGE_WORKFLOW] || { default_provider: "", steps: {} };
  return {
    ...config,
    llm: {
      ...config.llm,
      workflows: {
        ...config.llm.workflows,
        [JUDGE_WORKFLOW]: {
          ...current,
          steps: {
            ...current.steps,
            [JUDGE_STEP]: { ...(current.steps[JUDGE_STEP] || { provider: "", timeout_seconds: null }), ...changes },
          },
        },
      },
    },
  };
}

export function supportsKimiThinkingSetting(provider: ProviderConfig | undefined): boolean {
  if (provider?.type !== "openai" || provider.default_model.trim().toLowerCase() !== "kimi-k2.6") return false;
  try {
    return ["api.moonshot.cn", "api.moonshot.ai"].includes(new URL(provider.base_url).hostname.toLowerCase());
  } catch {
    return false;
  }
}
