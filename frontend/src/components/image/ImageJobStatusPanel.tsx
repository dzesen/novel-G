"use client";

import type { ReactNode } from "react";
import { Button } from "@heroui/react";
import type { ImageJob, ImageJobStatus } from "@/types/image";

export interface ImageJobStatusLabels {
  status: (status: ImageJobStatus) => string;
  queuePosition: (count: number) => string;
  estimated: (duration: string) => string;
  elapsed: (duration: string) => string;
  completedImages: (count: number) => string;
  pollRetrying: string;
  cleanupPending: string;
  olderCleanupTitle: string;
  olderCleanupDescription: string;
  retryCleanup: string;
  retryingCleanup: string;
  abandon: string;
  abandoning: string;
  abandonWarning: string;
  ignoredDimensions: string;
  ignoredSlots: (slots: string) => string;
  pollUnavailable: string;
  cancel: string;
  cancelling: string;
}

interface ImageJobStatusPanelProps<TJob extends ImageJob> {
  job: TJob | null;
  cleanupJob: TJob | null;
  pollError: string | null;
  cleanupPollError: string | null;
  cancelling: boolean;
  cleanupCancelling: boolean;
  formatDuration: (seconds: number) => string;
  labels: ImageJobStatusLabels;
  primaryAction: ReactNode;
  onCancel: () => void;
  onRetryCleanup: () => void;
}

export default function ImageJobStatusPanel<TJob extends ImageJob>({
  job,
  cleanupJob,
  pollError,
  cleanupPollError,
  cancelling,
  cleanupCancelling,
  formatDuration,
  labels,
  primaryAction,
  onCancel,
  onRetryCleanup,
}: ImageJobStatusPanelProps<TJob>) {
  const cancelAvailable = Boolean(
    job &&
      !job.terminal &&
      job.status !== "cancelling" &&
      job.status !== "storing_asset" &&
      job.status !== "finalizing" &&
      (
        !job.cleanup_pending ||
        job.abandonable ||
        (job.failure !== null && !job.failure.retryable)
      ),
  );
  const cleanupActionAvailable = Boolean(
    cleanupJob &&
      (
        cleanupJob.abandonable ||
        (
          cleanupJob.failure !== null &&
          !cleanupJob.failure.retryable
        )
      ),
  );
  const abandonLostJob = Boolean(job?.abandonable);

  return (
    <>
      {cleanupJob && (
        <div
          role="status"
          className="mt-5 min-w-0 rounded-xl border border-amber-300 bg-amber-50 px-4 py-3 text-sm dark:border-amber-800 dark:bg-amber-950/30"
        >
          <p className="font-medium text-foreground">
            {labels.olderCleanupTitle}
          </p>
          <p className="mt-1 text-muted">
            {labels.olderCleanupDescription}
          </p>
          <JobMetrics
            job={cleanupJob}
            formatDuration={formatDuration}
            labels={labels}
          />
          <p className="mt-2 text-amber-700 dark:text-amber-300">
            {labels.cleanupPending}
          </p>
          <JobFailure job={cleanupJob} />
          {cleanupPollError && (
            <p className="mt-2 text-amber-700 dark:text-amber-300">
              {labels.pollUnavailable}
            </p>
          )}
          {cleanupActionAvailable && (
            <div className="mt-3 flex min-w-0 flex-wrap gap-2">
              <Button
                variant="outline"
                isDisabled={cleanupCancelling}
                onPress={() => {
                  if (
                    cleanupJob.abandonable &&
                    !window.confirm(labels.abandonWarning)
                  ) {
                    return;
                  }
                  onRetryCleanup();
                }}
              >
                {cleanupCancelling
                  ? cleanupJob.abandonable
                    ? labels.abandoning
                    : labels.retryingCleanup
                  : cleanupJob.abandonable
                    ? labels.abandon
                    : labels.retryCleanup}
              </Button>
            </div>
          )}
        </div>
      )}

      {job && (
        <div
          role="status"
          className="mt-5 min-w-0 rounded-xl border border-border bg-surface-secondary px-4 py-3 text-sm"
        >
          <JobMetrics
            job={job}
            formatDuration={formatDuration}
            labels={labels}
          />
          {job.cleanup_pending && (
            <p className="mt-2 text-amber-700 dark:text-amber-300">
              {labels.cleanupPending}
            </p>
          )}
          {job.status === "failed" &&
            job.failure?.retryable &&
            !job.terminal && (
              <p className="mt-2 text-amber-700 dark:text-amber-300">
                {labels.pollRetrying}
              </p>
            )}
          <JobFailure job={job} hideCancelled />
          <IgnoredSlots job={job} labels={labels} />
          {pollError && (
            <p className="mt-2 text-amber-700 dark:text-amber-300">
              {labels.pollUnavailable}
            </p>
          )}
        </div>
      )}

      <div className="mt-5 flex min-w-0 flex-wrap gap-2">
        {primaryAction}
        {cancelAvailable && (
          <Button
            variant="outline"
            isDisabled={cancelling}
            onPress={() => {
              if (
                abandonLostJob &&
                !window.confirm(labels.abandonWarning)
              ) {
                return;
              }
              onCancel();
            }}
          >
            {cancelling
              ? abandonLostJob
                ? labels.abandoning
                : job?.cleanup_pending
                  ? labels.retryingCleanup
                  : labels.cancelling
              : abandonLostJob
                ? labels.abandon
                : job?.cleanup_pending
                  ? labels.retryCleanup
                  : labels.cancel}
          </Button>
        )}
      </div>
    </>
  );
}

function JobMetrics({
  job,
  formatDuration,
  labels,
}: {
  job: ImageJob;
  formatDuration: (seconds: number) => string;
  labels: ImageJobStatusLabels;
}) {
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-2">
      <span className="font-medium text-foreground">
        {labels.status(job.status)}
      </span>
      {job.queue_position !== null && (
        <span className="text-muted">
          {labels.queuePosition(job.queue_position)}
        </span>
      )}
      {job.estimated_seconds !== null && (
        <span className="text-muted">
          {labels.estimated(formatDuration(job.estimated_seconds))}
        </span>
      )}
      {job.elapsed_seconds > 0 && (
        <span className="text-muted">
          {labels.elapsed(formatDuration(job.elapsed_seconds))}
        </span>
      )}
      {job.completed_images > 0 && (
        <span className="text-muted">
          {labels.completedImages(job.completed_images)}
        </span>
      )}
    </div>
  );
}

function JobFailure({
  job,
  hideCancelled = false,
}: {
  job: ImageJob;
  hideCancelled?: boolean;
}) {
  if (!job.failure || (hideCancelled && job.status === "cancelled")) {
    return null;
  }
  return (
    <div
      className={`mt-2 ${
        !job.terminal && job.failure.retryable
          ? "text-amber-700 dark:text-amber-300"
          : "text-red-700 dark:text-red-300"
      }`}
    >
      <p>{job.failure.message}</p>
      <p>{job.failure.action}</p>
    </div>
  );
}

function IgnoredSlots({
  job,
  labels,
}: {
  job: ImageJob;
  labels: ImageJobStatusLabels;
}) {
  const ignored = job.ignored_slots;
  if (ignored.length === 0) return null;
  const hasDimensions =
    ignored.includes("width") && ignored.includes("height");
  const remaining = hasDimensions
    ? ignored.filter((slot) => slot !== "width" && slot !== "height")
    : ignored;
  return (
    <>
      {hasDimensions && (
        <p className="mt-2 break-words text-amber-700 dark:text-amber-300">
          {labels.ignoredDimensions}
        </p>
      )}
      {remaining.length > 0 && (
        <p className="mt-2 break-words text-amber-700 dark:text-amber-300">
          {labels.ignoredSlots(remaining.join(", "))}
        </p>
      )}
    </>
  );
}
