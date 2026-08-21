export interface GenerationPresetPromptPreview {
  identifier: string;
  name: string;
  source_role: string;
  role_authority: "untrusted";
  content: string;
  marker: boolean;
  system_prompt: boolean;
  unrecognized_fields: string[];
}

export interface GenerationPresetOrderItemPreview {
  order_index: number;
  identifier: string;
  enabled: boolean;
  resolved: boolean;
  selected_by_default: boolean;
  prompt: GenerationPresetPromptPreview | null;
}

export interface GenerationPresetOrderProfilePreview {
  profile_index: number;
  external_character_id: string | number | null;
  items: GenerationPresetOrderItemPreview[];
}

export interface GenerationPresetUnsupportedParameter {
  field: string;
  value: unknown;
  reason:
    | "unsupported_by_agent_runtime"
    | "context_limit_is_not_an_output_default"
    | "invalid_type"
    | "out_of_range"
    | string;
  applied: false;
}

export interface GenerationPresetPreview {
  format: "sillytavern_generation_preset";
  source_name: string;
  source_hash: string;
  prompt_count: number;
  order_profile_count: number;
  default_profile_index: number | null;
  active_prompt_count: number;
  active_prompt_chars: number;
  order_profiles: GenerationPresetOrderProfilePreview[];
  unassigned_prompts: GenerationPresetPromptPreview[];
  mapped_generation_params: {
    temperature?: number;
    top_p?: number;
    max_tokens?: number;
    presence_penalty?: number;
    frequency_penalty?: number;
  };
  unsupported_generation_params: GenerationPresetUnsupportedParameter[];
  isolated_extensions: Array<{
    path: string;
    item_count: number;
    enabled: false;
  }>;
  notices: Array<{
    code: string;
    path: string;
    message: string;
  }>;
  unrecognized_top_level_fields: string[];
  instruction_prefix: string;
  suggested_instruction: string;
  suggested_instruction_chars: number;
  max_instruction_chars: number;
  suggested_instruction_over_limit: boolean;
  application_policy: {
    target: "custom_agent";
    preview_only_capabilities_only: true;
    external_roles_demoted: true;
    extension_code_executed: false;
  };
}
