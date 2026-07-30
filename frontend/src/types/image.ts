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
