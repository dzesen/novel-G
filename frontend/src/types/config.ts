export type SecretPatchMode = "keep" | "replace" | "clear";

export interface ProviderConfig {
  type: "openai" | "gemini" | "claude";
  base_url: string;
  default_model: string;
  enabled: boolean;
  timeout_seconds: number;
  max_retries: number;
  max_concurrency: number;
  use_system_proxy: boolean;
  supports_streaming: boolean;
  supports_function_calling: boolean;
  supports_stream_usage: boolean;
  structured_output: "prompt_json" | "json_object" | "schema_enforced";
  has_api_key: boolean;
  /** 仅存在于浏览器草稿，永不放进 changes。 */
  api_key?: string;
  api_key_mode?: SecretPatchMode;
  temperature?: number | null;
  top_p?: number | null;
  max_tokens?: number | null;
  system_prompt?: string | null;
  presence_penalty?: number | null;
  frequency_penalty?: number | null;
}

export type ProviderTestCapability =
  | "connection" | "streaming" | "stream_usage" | "json_object" | "json_schema" | "function_calling";
export type ProviderTestStatus = "passed" | "failed" | "skipped";
export interface ProviderCapabilityResult {
  capability: ProviderTestCapability;
  label: string;
  status: ProviderTestStatus;
  duration_ms: number;
  message: string;
}
export interface ProviderCapabilityRecommendation {
  supports_streaming: boolean;
  supports_function_calling: boolean;
  supports_stream_usage: boolean;
  structured_output: "prompt_json" | "json_object" | "schema_enforced";
}
export interface ProviderTestResponse {
  alias: string;
  provider_type: string;
  model: string;
  summary: string;
  results: ProviderCapabilityResult[];
  recommendations: ProviderCapabilityRecommendation;
}

export interface WorkflowStep { provider: string; timeout_seconds?: number | null; }
export interface WorkflowConfig { default_provider: string; steps: Record<string, WorkflowStep>; }
export interface WorkflowStepDefinition { name: string; label_key: string; }
export interface WorkflowDefinition {
  name: string;
  label_key: string;
  steps: WorkflowStepDefinition[];
}

export interface FormatReviewPolicy {
  mode: "disabled" | "provider" | "auto";
  provider_alias: string | null;
}
export interface LLMConfig {
  default_provider: string;
  format_review: FormatReviewPolicy;
  providers: Record<string, ProviderConfig>;
  workflows: Record<string, WorkflowConfig>;
}

export type ImageConfigScalar = string | number | boolean;
export interface ImageFieldParameterMapping {
  kind: "field";
  field: string;
  minimum?: number | null;
  maximum?: number | null;
  allowed_values?: ImageConfigScalar[] | null;
  default?: ImageConfigScalar | null;
}
export interface ImageValueMapParameterMapping {
  kind: "value_map";
  values: Record<string, Record<string, ImageConfigScalar>>;
}
export type ImageParameterMapping =
  | ImageFieldParameterMapping
  | ImageValueMapParameterMapping;

export interface ComfyUIWorkflowInputBinding {
  node_id: string;
  input: string;
  required: boolean;
  upload: boolean;
}
export interface ComfyUIWorkflowOutputBinding {
  node_id: string;
  field: string;
}
export interface ComfyUIWorkflowConfig {
  template_path: string;
  template_revision: string;
  reference_mode: "none" | "img2img" | "controlnet" | "style_reference" | "edit_model";
  bindings: Record<string, ComfyUIWorkflowInputBinding>;
  outputs: ComfyUIWorkflowOutputBinding[];
  dependencies: {
    node_types: string[];
    checkpoints: string[];
    loras: string[];
  };
}
export interface ComfyUIImageProviderConfig {
  type: "comfyui";
  base_url: string;
  enabled: boolean;
  timeout_seconds: number;
  max_concurrency: number;
  workflow: ComfyUIWorkflowConfig;
}
export interface OpenAICompatibleImageProviderConfig {
  type: "openai_compatible";
  base_url: string;
  default_model: string;
  enabled: boolean;
  timeout_seconds: number;
  max_retries: number;
  max_concurrency: number;
  parameters: Record<string, ImageParameterMapping | null>;
  result: {
    items_path: string;
    url_field: string;
    base64_field: string;
    mime_type_field: string;
    revised_prompt_field: string;
  };
  has_api_key: boolean;
  /** 只存在于浏览器草稿，永不放进 changes。 */
  api_key?: string;
  api_key_mode?: SecretPatchMode;
}
export type ImageProviderConfig =
  | ComfyUIImageProviderConfig
  | OpenAICompatibleImageProviderConfig;
export interface ImageProvidersConfig {
  default_provider: string;
  providers: Record<string, ImageProviderConfig>;
  usages: {
    character_portrait: string;
    cover: string;
    scene_illustration: string;
  };
}

export interface AppConfig {
  config_version?: number;
  mongodb_url: string;
  mongo_database_name: string;
  mongo_timeout_ms: number;
  llm: LLMConfig;
  image_providers: ImageProvidersConfig;
  [key: string]: unknown;
}
export interface ConfigIssue { path: string; code: string; message: string; severity: string; }
export interface ConfigView {
  editable_data: AppConfig;
  resolutions: Record<string, unknown>;
  revision: string;
  issues: ConfigIssue[];
}

export interface RenameProviderCommand {
  kind: "rename";
  from_alias: string;
  to_alias: string;
}
export interface DeleteProviderCommand {
  kind: "delete";
  alias: string;
  replacement_default_alias?: string | null;
}
export type ProviderCommand = RenameProviderCommand | DeleteProviderCommand;
export interface SecretPatch { mode: SecretPatchMode; value?: string; }
export interface ConfigPatchPayload {
  changes: Record<string, unknown>;
  provider_commands: ProviderCommand[];
  provider_secrets: Record<string, SecretPatch>;
  image_provider_secrets: Record<string, SecretPatch>;
  expected_revision: string;
  confirmation_token?: string;
}
export interface ProviderReferenceChange { path: string; before: unknown; after: unknown; }
export interface ConfigChangePreview {
  reference_changes: ProviderReferenceChange[];
  issues: ConfigIssue[];
  confirmation_token?: string | null;
}

export function newProviderConfig(): ProviderConfig {
  return {
    type: "openai", base_url: "", default_model: "", enabled: false,
    timeout_seconds: 60, max_retries: 2, max_concurrency: 5,
    use_system_proxy: false, supports_streaming: true,
    supports_function_calling: false,
    supports_stream_usage: false, has_api_key: false, api_key: "", api_key_mode: "keep",
    structured_output: "prompt_json",
    temperature: null, top_p: null, max_tokens: null, system_prompt: null,
    presence_penalty: null, frequency_penalty: null,
  };
}

function normalizeStepTimeout(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : null;
}
export function newWorkflowStepConfig(step?: Partial<WorkflowStep>): WorkflowStep {
  return { provider: step?.provider || "", timeout_seconds: normalizeStepTimeout(step?.timeout_seconds) };
}
export function getWorkflowStepNames(workflow?: WorkflowConfig, definition?: WorkflowDefinition): string[] {
  const names = new Set<string>(definition?.steps.map((step) => step.name) || []);
  Object.keys(workflow?.steps || {}).forEach((name) => names.add(name));
  return [...names];
}
export function newWorkflowConfig(
  workflowName: string,
  defaultProvider = "",
  workflow?: Partial<WorkflowConfig>,
  definition?: WorkflowDefinition,
): WorkflowConfig {
  void workflowName;
  const stepNames = getWorkflowStepNames(workflow as WorkflowConfig | undefined, definition);
  return {
    default_provider: workflow?.default_provider ?? defaultProvider,
    steps: Object.fromEntries(stepNames.map((name) => [name, newWorkflowStepConfig(workflow?.steps?.[name])])),
  };
}

function mapWorkflowProviderAliases(
  workflows: Record<string, WorkflowConfig>,
  mapAlias: (alias: string) => string,
): Record<string, WorkflowConfig> {
  return Object.fromEntries(Object.entries(workflows).map(([name, workflow]) => [name, {
    ...workflow,
    default_provider: mapAlias(workflow.default_provider),
    steps: Object.fromEntries(Object.entries(workflow.steps).map(([stepName, step]) => [
      stepName, { ...step, provider: mapAlias(step.provider) },
    ])),
  }]));
}
export function renameProviderAlias(config: AppConfig, currentAlias: string, nextAlias: string): AppConfig {
  if (!(currentAlias in config.llm.providers) || currentAlias === nextAlias) return config;
  const providers = Object.fromEntries(Object.entries(config.llm.providers).map(([alias, provider]) => [
    alias === currentAlias ? nextAlias : alias, provider,
  ]));
  const mapAlias = (alias: string) => alias === currentAlias ? nextAlias : alias;
  const review = config.llm.format_review;
  return { ...config, llm: { ...config.llm, providers,
    default_provider: mapAlias(config.llm.default_provider),
    format_review: review.mode === "provider" && review.provider_alias === currentAlias
      ? { ...review, provider_alias: nextAlias } : review,
    workflows: mapWorkflowProviderAliases(config.llm.workflows, mapAlias),
  }};
}
export function removeProviderAlias(
  config: AppConfig,
  aliasToRemove: string,
  replacementDefaultAlias = "",
): AppConfig {
  if (!(aliasToRemove in config.llm.providers)) return config;
  const providers = { ...config.llm.providers };
  delete providers[aliasToRemove];
  const validReplacementDefaultAlias = isProviderSelectable(
    providers,
    replacementDefaultAlias,
  ) ? replacementDefaultAlias : "";
  const mapAlias = (alias: string) => alias === aliasToRemove ? "" : alias;
  const review = config.llm.format_review;
  return { ...config, llm: { ...config.llm, providers,
    default_provider: config.llm.default_provider === aliasToRemove
      ? validReplacementDefaultAlias
      : config.llm.default_provider,
    format_review: review.mode === "provider" && review.provider_alias === aliasToRemove
      ? { mode: "disabled", provider_alias: null } : review,
    workflows: mapWorkflowProviderAliases(config.llm.workflows, mapAlias),
  }};
}

interface ProviderSelectionOptions { requireJsonSchema?: boolean; }
export function isProviderSelectable(
  providers: Record<string, ProviderConfig>, alias: string, options: ProviderSelectionOptions = {},
): boolean {
  const provider = providers[alias];
  return Boolean(
    provider?.enabled
      && (!options.requireJsonSchema || provider.structured_output === "schema_enforced"),
  );
}
export function getProviderAliasesForSelection(
  providers: Record<string, ProviderConfig>, currentAlias = "", options: ProviderSelectionOptions = {},
): string[] {
  const aliases = Object.keys(providers).filter((alias) => isProviderSelectable(providers, alias, options));
  return currentAlias && currentAlias in providers && !aliases.includes(currentAlias)
    ? [currentAlias, ...aliases] : aliases;
}

export function getReplacementDefaultProviderAlias(
  providers: Record<string, ProviderConfig>,
  aliasToRemove: string,
): string {
  return getProviderAliasesForSelection(providers).find(
    (alias) => alias !== aliasToRemove,
  ) || "";
}

export function normalizeAppConfig(config: AppConfig, catalog: WorkflowDefinition[] = []): AppConfig {
  const providers: Record<string, ProviderConfig> = Object.fromEntries(
    Object.entries(config.llm?.providers || {}).map(([alias, provider]) => [
      alias,
      { ...newProviderConfig(), ...provider, api_key: "", api_key_mode: "keep" as const },
    ]),
  );
  const definitions = new Map(catalog.map((definition) => [definition.name, definition]));
  const workflowNames = new Set([
    ...Object.keys(config.llm?.workflows || {}),
    ...catalog.map((definition) => definition.name),
  ]);
  const workflows = Object.fromEntries([...workflowNames].map((name) => [
    name,
    newWorkflowConfig(name, "", config.llm?.workflows?.[name], definitions.get(name)),
  ]));
  const imageConfig = config.image_providers || {
    default_provider: "",
    providers: {},
    usages: { character_portrait: "", cover: "", scene_illustration: "" },
  };
  const imageProviders: Record<string, ImageProviderConfig> = Object.fromEntries(
    Object.entries(imageConfig.providers || {}).map(([alias, provider]) => {
      const editable = { ...provider } as Record<string, unknown>;
      delete editable.api_key;
      delete editable.api_key_mode;
      if (provider.type === "openai_compatible") {
        return [alias, {
          ...editable,
          has_api_key: Boolean(provider.has_api_key),
          api_key: "",
          api_key_mode: "keep" as const,
        } as OpenAICompatibleImageProviderConfig];
      }
      delete editable.has_api_key;
      return [alias, editable as unknown as ComfyUIImageProviderConfig];
    }),
  );
  return { ...config, llm: {
    ...config.llm,
    format_review: config.llm?.format_review || { mode: "disabled", provider_alias: null },
    providers,
    workflows,
  }, image_providers: {
    ...imageConfig,
    providers: imageProviders,
    usages: {
      character_portrait: imageConfig.usages?.character_portrait || "",
      cover: imageConfig.usages?.cover || "",
      scene_illustration: imageConfig.usages?.scene_illustration || "",
    },
  }};
}

export function applyProviderToAllWorkflows(config: AppConfig, providerAlias: string): AppConfig {
  const workflows = Object.fromEntries(Object.entries(config.llm.workflows || {}).map(([name, workflow]) => [
    name, { ...workflow, default_provider: providerAlias,
      steps: Object.fromEntries(Object.entries(workflow.steps).map(([stepName, step]) => [
        stepName, { ...step, provider: providerAlias },
      ])),
    },
  ]));
  return { ...config, llm: { ...config.llm, default_provider: providerAlias, workflows } };
}

export function buildConfigPatch(
  config: AppConfig,
  revision: string,
  providerCommands: ProviderCommand[] = [],
): ConfigPatchPayload {
  const providerSecrets: Record<string, SecretPatch> = {};
  const providers = Object.fromEntries(Object.entries(config.llm.providers).map(([alias, provider]) => {
    const mode = provider.api_key_mode || "keep";
    providerSecrets[alias] = mode === "replace"
      ? { mode, value: provider.api_key || "" }
      : { mode };
    const editable: Partial<ProviderConfig> = { ...provider };
    delete editable.api_key;
    delete editable.api_key_mode;
    delete editable.has_api_key;
    return [alias, editable];
  }));
  const imageProviderSecrets: Record<string, SecretPatch> = {};
  const imageProviders = Object.fromEntries(
    Object.entries(config.image_providers.providers).map(([alias, provider]) => {
      const editable = { ...provider } as Record<string, unknown>;
      delete editable.api_key;
      delete editable.api_key_mode;
      delete editable.has_api_key;
      if (provider.type === "openai_compatible") {
        const mode = provider.api_key_mode || "keep";
        imageProviderSecrets[alias] = mode === "replace"
          ? { mode, value: provider.api_key || "" }
          : { mode };
      }
      return [alias, editable];
    }),
  );
  return {
    changes: {
      ...config,
      llm: { ...config.llm, providers },
      image_providers: { ...config.image_providers, providers: imageProviders },
    },
    provider_commands: providerCommands,
    provider_secrets: providerSecrets,
    image_provider_secrets: imageProviderSecrets,
    expected_revision: revision,
  };
}
