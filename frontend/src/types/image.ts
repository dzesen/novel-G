export type CharacterPortraitJobStatus =
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

export interface CharacterPortraitAsset {
  asset_id: string;
  content_hash: string;
  mime: string;
  state: "available" | "missing";
  content_url: string | null;
  width: number;
  height: number;
}

export interface CharacterPortraitProvider {
  alias: string;
  model: string;
  workflow_revision: string;
  reference_mode: string;
  available: boolean;
  queue_position: number | null;
  estimated_seconds: number | null;
  warnings: string[];
}

export interface CharacterPortraitJob {
  job_id: string;
  status: CharacterPortraitJobStatus;
  terminal: boolean;
  cleanup_pending: boolean;
  abandonable: boolean;
  queue_position: number | null;
  estimated_seconds: number | null;
  elapsed_seconds: number;
  completed_images: number;
  submit_count: number;
  ignored_slots: string[];
  failure: ImageJobFailure | null;
  asset: CharacterPortraitAsset | null;
  anchor: AppearanceAnchor | null;
  provider?: CharacterPortraitProvider | null;
  warnings?: string[];
}

export interface CharacterPortraitState {
  anchor: AppearanceAnchor | null;
  asset: CharacterPortraitAsset | null;
  active_job: CharacterPortraitJob | null;
  cleanup_job: CharacterPortraitJob | null;
  provider: CharacterPortraitProvider;
  warnings?: string[];
}
