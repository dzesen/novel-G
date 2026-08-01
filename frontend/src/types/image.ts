export type ImageJobStatus =
  | "pending"
  | "submitting"
  | "queued"
  | "running"
  | "cancelling"
  | "late_cleanup_pending"
  | "storing_asset"
  | "finalizing"
  | "succeeded"
  | "failed"
  | "rejected"
  | "cancelled"
  | "cancel_failed";

export type CharacterPortraitJobStatus = ImageJobStatus;

export interface ImageJobFailure {
  code: string;
  message: string;
  action: string;
  retryable: boolean;
}

export interface AppearanceAnchor {
  descriptor: string;
  // uint64 seeds are projected as decimal strings so browsers never round
  // values above Number.MAX_SAFE_INTEGER.
  seed: string;
  reference_asset: string;
  established_at: string;
  provider: string;
  model: string;
  workflow_revision: string;
  reference_mode: string;
  runtime_fingerprint: Record<string, unknown>;
}

export interface ImageAsset {
  asset_id: string;
  content_hash: string;
  mime: string;
  state: "available" | "missing";
  content_url: string | null;
  width: number;
  height: number;
}

export type CharacterPortraitAsset = ImageAsset;

export interface ImageProviderState {
  alias: string;
  model: string;
  workflow_revision: string;
  reference_mode: string;
  available: boolean;
  queue_position: number | null;
  estimated_seconds: number | null;
  warnings: string[];
}

export type CharacterPortraitProvider = ImageProviderState;

export interface ImageJob {
  job_id: string;
  status: ImageJobStatus;
  terminal: boolean;
  selected_as_current?: boolean | null;
  cleanup_pending: boolean;
  abandonable: boolean;
  queue_position: number | null;
  estimated_seconds: number | null;
  elapsed_seconds: number;
  completed_images: number;
  submit_count: number;
  ignored_slots: string[];
  failure: ImageJobFailure | null;
  asset: ImageAsset | null;
  provider?: ImageProviderState | null;
  warnings?: string[];
}

export interface CharacterPortraitJob extends ImageJob {
  anchor: AppearanceAnchor | null;
}

export interface AppearanceAnchorDependency {
  job_id: string;
  chapter_id: string;
  chapter_title: string;
  chapter_order: number | null;
  status: string;
}

export interface CharacterPortraitState {
  anchor: AppearanceAnchor | null;
  asset: CharacterPortraitAsset | null;
  active_job: CharacterPortraitJob | null;
  cleanup_job: CharacterPortraitJob | null;
  provider: CharacterPortraitProvider;
  warnings?: string[];
  anchor_dependencies: AppearanceAnchorDependency[];
  anchor_dependency_total: number;
}

export interface CharacterVisualReference {
  asset_id: string;
  view: string | null;
  framing: string | null;
  expression: string | null;
  costume: string | null;
  note: string | null;
}

export interface ExternalLoraAdapter {
  kind: "lora";
  lora_name: string;
  trigger_word: string | null;
  strength: number;
  base_model_family: string;
  version_note: string | null;
}

export interface CharacterVisualProfile {
  exists: boolean;
  profile_id: string | null;
  owner_id: string;
  novel_id: string;
  character_card_id: string;
  references: CharacterVisualReference[];
  external_adapter: ExternalLoraAdapter | null;
  appearance_anchor: AppearanceAnchor | null;
  revision: number;
  created_at: string | null;
  updated_at: string | null;
}

export type NovelCoverJob = ImageJob;

export interface NovelCoverState {
  current_asset: ImageAsset | null;
  assets: ImageAsset[];
  active_job: NovelCoverJob | null;
  cleanup_job: NovelCoverJob | null;
  provider: ImageProviderState;
  warnings?: string[];
}

export interface SceneIllustrationCharacter {
  card_id: string;
  name: string;
  descriptor: string | null;
  anchored: boolean;
}

export type SceneIllustrationJob = ImageJob;

export interface SceneIllustrationState {
  characters: SceneIllustrationCharacter[];
  assets: ImageAsset[];
  active_job: SceneIllustrationJob | null;
  cleanup_job: SceneIllustrationJob | null;
  provider: ImageProviderState;
  warnings?: string[];
}

export type IllustrationStageName = "compose" | "identity_edit" | "refine";
export type IllustrationStageStatus =
  | "locked"
  | "ready"
  | "running"
  | "awaiting_selection"
  | "selected"
  | "failed"
  | "cancelled"
  | "skipped";

export interface IllustrationFieldDiff {
  before: string | null;
  after: string | null;
}

export interface IllustrationBriefDiff {
  current_outline_revision: string;
  outline_revision_changed: boolean;
  scene_missing: boolean;
  summary: IllustrationFieldDiff;
  purpose: IllustrationFieldDiff;
  characters_added: string[];
  characters_removed: string[];
}

export interface IllustrationBrief {
  exists: boolean;
  brief_id: string;
  owner_id: string;
  novel_id: string;
  chapter_id: string;
  title: string;
  scene_snapshot: {
    source_scene_index: number;
    summary: string;
    purpose: string;
    source_outline_revision: string;
    source_scene_fingerprint: string;
    captured_at: string;
  };
  scene_character_card_ids: string[];
  default_reference_character_card_id: string | null;
  default_pipeline_alias: string | null;
  current_asset_id: string | null;
  status: "active" | "archived";
  sort_order: number;
  revision: number;
  stale: boolean;
  diff: IllustrationBriefDiff | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface IllustrationRunStage {
  status: IllustrationStageStatus;
  selected_asset_id: string | null;
  latest_job_id: string | null;
}

export interface IllustrationRun {
  run_id: string;
  owner_id: string;
  novel_id: string;
  chapter_id: string;
  illustration_brief_id: string;
  pipeline_snapshot: {
    alias: string;
    revision: string;
    kind: "quick" | "consistency";
    effective_stage_providers: Record<string, string>;
  };
  reference_snapshot: {
    character_card_id: string;
    asset_id: string;
    asset_sha256: string;
    descriptor: string;
    appearance_anchor_sha256: string | null;
  };
  external_adapter_snapshot: ExternalLoraAdapter | null;
  parent_run_id: string | null;
  branch_from_asset_id: string | null;
  stages: Record<IllustrationStageName, IllustrationRunStage>;
  status: "active" | "finalized" | "cancelled" | "failed" | "branched";
  final_asset_id: string | null;
  revision: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface IllustrationCandidate {
  asset_id: string;
  illustration_brief_id: string;
  illustration_run_id: string;
  pipeline_stage: IllustrationStageName | "external_import";
  target_stage: IllustrationStageName;
  content_hash: string;
  mime: string;
  width: number;
  height: number;
  byte_size: number;
  source: "generated" | "imported";
  candidate_state: "available" | "selected" | "discarded" | "finalized";
  selected: boolean;
  content_url: string;
  created_at: string | null;
}

export interface IllustrationStageJob {
  run: IllustrationRun;
  job: ImageJob;
}

export interface IllustrationReadiness {
  run_id: string;
  run_revision: number;
  pipeline_alias: string;
  pipeline_revision: string;
  pipeline_kind: "consistency";
  status: "passed" | "blocked";
  readiness_digest: string;
  required_stages: IllustrationStageName[];
  optional_refine: boolean;
  max_provider_calls: number;
  reference_verified: boolean;
  stages: Array<{
    stage: IllustrationStageName;
    provider_alias: string;
    status: "passed" | "blocked";
    summary: string;
    queue_running: number;
    queue_pending: number;
    max_concurrency: number;
    median_completed_seconds: number | null;
    fallback_timeout_seconds: number;
  }>;
  issues: Array<{
    code: string;
    message: string;
    stage: IllustrationStageName | null;
  }>;
  quality: {
    status: "accepted" | "experimental" | "drifted";
    fingerprint: string;
    reason: string;
  };
}
