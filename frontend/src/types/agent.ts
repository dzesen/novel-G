export type AgentCapabilityId =
  | "chapter_outline"
  | "chapter_prose"
  | "chapter_state"
  | "scene_rewrite"
  | "creative_inspiration"
  | "continuity_review";

export type AgentScope = "novel" | "volume" | "chapter";

export interface AgentCapability {
  capability: AgentCapabilityId;
  version: number;
  label: string;
  description: string;
  customizable: boolean;
  preview_only: boolean;
  scope_options: string[];
  input_contract: string;
  output_contract: string;
  context_policy: string;
  side_effect_policy: "preview_only" | "accept_required" | "system_write";
  handler_id: string;
}

export interface AgentProfile {
  agent_id: string;
  label: string;
  description: string;
  instruction: string;
  capabilities: AgentCapabilityId[];
  origin: "builtin" | "custom";
  owner_id: string | null;
  provider_alias: string | null;
  generation_params: {
    temperature?: number;
    top_p?: number;
    max_tokens?: number;
  };
  enabled: boolean;
  version: number;
  visibility: "private" | "shared";
  editable: boolean;
}

export interface AgentProviderOption {
  alias: string;
  type: string;
  model: string;
}

export interface CreativeIdea {
  title: string;
  concept: string;
  fit_reason: string;
  affected_elements: string[];
  risks: string[];
  suggested_changes: string[];
}

export interface CreativeInspirationResult {
  framing: string;
  ideas: CreativeIdea[];
}

export type ContinuitySeverity = "high" | "medium" | "low";

export interface ContinuityIssue {
  severity: ContinuitySeverity;
  category:
    | "character_state"
    | "timeline"
    | "location"
    | "world_rule"
    | "plot_thread"
    | "volume_outline"
    | "faction"
    | "other";
  location: string;
  evidence: string[];
  problem: string;
  suggestion: string;
  confidence: number;
}

export interface ContinuityReviewResult {
  summary: string;
  coverage: string;
  issues: ContinuityIssue[];
}

export interface AgentToolMetadata {
  agent_id: string;
  agent_version: number;
  provider_alias: string;
  usage: {
    input_tokens?: number;
    output_tokens?: number;
    total_tokens?: number;
  };
  context_report: {
    coverage: string;
    truncated_sections: string[];
  };
  write_policy: "preview_only";
}
