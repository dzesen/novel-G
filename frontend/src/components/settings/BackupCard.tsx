"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { apiDownload, apiGet, apiPost, apiPostForm } from "@/lib/api";

interface BackupStatus {
  enabled: boolean;
  interval_hours: number;
  retention_count: number;
  directory: string;
  latest_backup: {
    name: string;
    created_at: string;
    size_bytes: number;
  } | null;
}

interface MissingImageAsset {
  asset_id: string;
  novel_id: string;
  subject_kind: string;
  subject_id: string;
  content_hash: string;
  relative_path: string | null;
  reason: "file_missing" | "invalid_reference_path";
}

interface OrphanImageFile {
  novel_id: string | null;
  content_hash: string | null;
  relative_path: string;
  byte_size: number;
}

interface UnmanagedImageFile {
  relative_path: string;
  byte_size: number;
}

interface ImageAssetReconciliationReport {
  checked_at: string;
  missing_asset_count: number;
  orphan_file_count: number;
  unmanaged_file_count: number;
  missing_assets: MissingImageAsset[];
  orphan_files: OrphanImageFile[];
  unmanaged_files: UnmanagedImageFile[];
  missing_assets_truncated: boolean;
  orphan_files_truncated: boolean;
  unmanaged_files_truncated: boolean;
}

interface RestoreResponse {
  message: string;
  stats: Record<string, unknown>;
  asset_reconciliation_status: "completed" | "unavailable";
  asset_reconciliation: ImageAssetReconciliationReport | null;
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function BackupCard() {
  const t = useTranslations("settings.backup");
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [status, setStatus] = useState<BackupStatus | null>(null);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [exporting, setExporting] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const [reconciling, setReconciling] = useState(false);
  const [reconciliationReport, setReconciliationReport] =
    useState<ImageAssetReconciliationReport | null>(null);
  const [reconciliationError, setReconciliationError] = useState<string | null>(
    null,
  );
  const [message, setMessage] = useState<{ tone: "success" | "error"; text: string } | null>(null);

  const loadStatus = async () => {
    try {
      setStatus(await apiGet<BackupStatus>("/api/backup/status"));
    } catch (error) {
      setMessage({ tone: "error", text: error instanceof Error ? error.message : t("statusFailed") });
    }
  };

  useEffect(() => {
    void loadStatus();
    // This status is intentionally loaded once when the backup surface opens.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const exportBackup = async () => {
    setExporting(true);
    setMessage(null);
    try {
      await apiDownload("/api/backup/export", "novel-generator-backup.json");
      setMessage({ tone: "success", text: t("exportSuccess") });
      await loadStatus();
    } catch (error) {
      setMessage({ tone: "error", text: error instanceof Error ? error.message : t("exportFailed") });
    } finally {
      setExporting(false);
    }
  };

  const restoreBackup = async () => {
    if (!selectedFile || restoring) return;
    setRestoring(true);
    setMessage(null);
    try {
      const form = new FormData();
      form.append("file", selectedFile);
      const result = await apiPostForm<RestoreResponse>(
        "/api/backup/restore",
        form,
      );
      setMessage({ tone: "success", text: t("restoreSuccess") });
      if (
        result.asset_reconciliation_status === "completed" &&
        result.asset_reconciliation
      ) {
        setReconciliationReport(result.asset_reconciliation);
        setReconciliationError(null);
      } else {
        setReconciliationReport(null);
        setReconciliationError(
          t("reconciliation.unavailableAfterRestore"),
        );
      }
      setSelectedFile(null);
      if (fileInputRef.current) fileInputRef.current.value = "";
      await loadStatus();
    } catch (error) {
      setMessage({ tone: "error", text: error instanceof Error ? error.message : t("restoreFailed") });
    } finally {
      setRestoring(false);
    }
  };

  const reconcileAssets = async () => {
    if (reconciling) return;
    setReconciling(true);
    setReconciliationError(null);
    try {
      const report = await apiPost<ImageAssetReconciliationReport>(
        "/api/backup/image-assets/reconcile",
        {},
      );
      setReconciliationReport(report);
    } catch {
      setReconciliationError(t("reconciliation.failed"));
    } finally {
      setReconciling(false);
    }
  };

  const reconciliationHasDefects =
    reconciliationReport !== null &&
    (reconciliationReport.missing_asset_count > 0 ||
      reconciliationReport.orphan_file_count > 0);
  const reconciliationHasOtherFiles =
    reconciliationReport !== null &&
    reconciliationReport.unmanaged_file_count > 0;
  const reconciliationHasFindings =
    reconciliationHasDefects || reconciliationHasOtherFiles;

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-lg font-semibold text-foreground">{t("title")}</h2>
        <p className="mt-1 max-w-2xl text-sm leading-6 text-muted">{t("description")}</p>
      </div>

      {message && (
        <div
          role="status"
          className={`rounded-lg border px-3 py-2.5 text-sm ${
            message.tone === "success"
              ? "border-green-200 bg-green-50 text-green-800 dark:border-green-900 dark:bg-green-950/40 dark:text-green-300"
              : "border-red-200 bg-red-50 text-red-800 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300"
          }`}
        >
          {message.text}
        </div>
      )}

      <section className="border-b border-border pb-6">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <h3 className="text-sm font-semibold text-foreground">{t("automaticTitle")}</h3>
            <p className="mt-1 max-w-xl text-sm leading-6 text-muted">
              {status
                ? t("automaticDescription", {
                    hours: status.interval_hours,
                    count: status.retention_count,
                  })
                : t("statusLoading")}
            </p>
          </div>
          <span className={`rounded-full px-2.5 py-1 text-xs font-medium ${status?.enabled ? "bg-green-100 text-green-800 dark:bg-green-950 dark:text-green-300" : "bg-surface-secondary text-muted"}`}>
            {status?.enabled ? t("enabled") : t("disabled")}
          </span>
        </div>
        {status?.latest_backup ? (
          <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-3">
            <div>
              <dt className="text-xs text-muted">{t("latest")}</dt>
              <dd className="mt-1 truncate text-foreground" title={status.latest_backup.name}>{status.latest_backup.name}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted">{t("createdAt")}</dt>
              <dd className="mt-1 text-foreground">{new Date(status.latest_backup.created_at).toLocaleString()}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted">{t("size")}</dt>
              <dd className="mt-1 text-foreground">{formatSize(status.latest_backup.size_bytes)}</dd>
            </div>
          </dl>
        ) : status ? (
          <p className="mt-4 text-xs text-muted">{t("noBackupYet")}</p>
        ) : null}
        {status && <p className="mt-3 break-all text-[11px] text-muted">{t("directory", { path: status.directory })}</p>}
      </section>

      <section className="border-b border-border pb-6">
        <h3 className="text-sm font-semibold text-foreground">{t("manualTitle")}</h3>
        <p className="mt-1 max-w-xl text-sm leading-6 text-muted">{t("manualDescription")}</p>
        <button
          type="button"
          onClick={() => void exportBackup()}
          disabled={exporting}
          className="mt-4 rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-accent-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
        >
          {exporting ? t("exporting") : t("exportButton")}
        </button>
      </section>

      <section className="border-b border-border pb-6">
        <h3 className="text-sm font-semibold text-foreground">{t("restoreTitle")}</h3>
        <p className="mt-1 max-w-xl text-sm leading-6 text-muted">{t("restoreDescription")}</p>
        <input
          ref={fileInputRef}
          type="file"
          accept="application/json,.json"
          className="mt-4 block w-full max-w-xl text-sm text-muted file:mr-3 file:rounded-lg file:border file:border-border file:bg-surface file:px-3 file:py-2 file:text-sm file:font-medium file:text-foreground hover:file:bg-surface-secondary"
          onChange={(event) => {
            const file = event.target.files?.[0] ?? null;
            if (file && file.size > 100 * 1024 * 1024) {
              setSelectedFile(null);
              setMessage({ tone: "error", text: t("fileTooLarge") });
              return;
            }
            setSelectedFile(file);
            setMessage(null);
          }}
        />
        {selectedFile && (
          <div className="mt-4 rounded-lg border border-red-200 bg-red-50 p-4 dark:border-red-900 dark:bg-red-950/35">
            <p className="text-sm font-medium text-red-800 dark:text-red-300">{t("restoreWarningTitle")}</p>
            <p className="mt-1 text-sm leading-6 text-red-700 dark:text-red-300">{t("restoreWarning", { name: selectedFile.name })}</p>
            <div className="mt-3 flex flex-col-reverse items-stretch gap-2 sm:flex-row sm:items-center">
              <button type="button" onClick={() => setSelectedFile(null)} className="rounded-lg px-3 py-1.5 text-sm text-muted hover:bg-surface">
                {t("cancel")}
              </button>
              <button type="button" onClick={() => void restoreBackup()} disabled={restoring} className="rounded-lg bg-red-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-700 disabled:opacity-50">
                {restoring ? t("restoring") : t("restoreButton")}
              </button>
            </div>
          </div>
        )}
      </section>

      <section aria-labelledby="asset-reconciliation-title">
        <div className="flex flex-col items-stretch gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <h3
              id="asset-reconciliation-title"
              className="text-sm font-semibold text-foreground"
            >
              {t("reconciliation.title")}
            </h3>
            <p className="mt-1 max-w-xl text-sm leading-6 text-muted">
              {t("reconciliation.description")}
            </p>
          </div>
          <button
            type="button"
            onClick={() => void reconcileAssets()}
            disabled={reconciling}
            className="w-full shrink-0 rounded-lg border border-border bg-surface px-3 py-2 text-sm font-medium text-foreground transition-colors hover:bg-surface-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50 sm:w-auto"
          >
            {reconciling
              ? t("reconciliation.running")
              : t("reconciliation.run")}
          </button>
        </div>

        {reconciliationError && (
          <div
            role="alert"
            className="mt-4 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2.5 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950/35 dark:text-amber-200"
          >
            {reconciliationError}
          </div>
        )}

        {!reconciliationReport ? (
          <p className="mt-4 text-sm text-muted">
            {t("reconciliation.notRun")}
          </p>
        ) : (
          <div className="mt-4 space-y-4">
            <div
              className={`rounded-lg border px-3 py-3 ${
                reconciliationHasDefects
                  ? "border-amber-200 bg-amber-50/70 dark:border-amber-900 dark:bg-amber-950/25"
                  : reconciliationHasOtherFiles
                    ? "border-blue-200 bg-blue-50/70 dark:border-blue-900 dark:bg-blue-950/25"
                  : "border-green-200 bg-green-50/70 dark:border-green-900 dark:bg-green-950/25"
              }`}
            >
              <p className="text-sm font-medium text-foreground">
                {reconciliationHasFindings
                  ? t("reconciliation.summary", {
                      missing:
                        reconciliationReport.missing_asset_count,
                      orphan: reconciliationReport.orphan_file_count,
                      unmanaged:
                        reconciliationReport.unmanaged_file_count,
                    })
                  : t("reconciliation.cleanTitle")}
              </p>
              <p className="mt-1 text-xs leading-5 text-muted">
                {reconciliationHasFindings
                  ? t("reconciliation.reportOnly")
                  : t("reconciliation.cleanDescription")}
              </p>
              <p className="mt-1 text-xs text-muted">
                {t("reconciliation.checkedAt", {
                  time: new Date(
                    reconciliationReport.checked_at,
                  ).toLocaleString(),
                })}
              </p>
            </div>

            {reconciliationReport.missing_asset_count > 0 && (
              <div>
                <h4 className="text-sm font-semibold text-foreground">
                  {t("reconciliation.missingTitle")}
                </h4>
                <p className="mt-1 text-xs leading-5 text-muted">
                  {t("reconciliation.missingDescription")}
                </p>
                <ul className="mt-2 space-y-2">
                  {reconciliationReport.missing_assets.map((asset) => (
                    <li
                      key={asset.asset_id}
                      className="min-w-0 rounded-lg border border-border bg-surface p-3"
                    >
                      <div className="flex min-w-0 flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                        <span className="w-fit rounded-full bg-red-100 px-2 py-0.5 text-xs font-medium text-red-800 dark:bg-red-950 dark:text-red-300">
                          {asset.reason === "file_missing"
                            ? t("reconciliation.missingBadge")
                            : t("reconciliation.invalidPathBadge")}
                        </span>
                        <span className="min-w-0 truncate text-xs text-muted">
                          {t("reconciliation.assetId")}: {asset.asset_id}
                        </span>
                      </div>
                      <dl className="mt-3 grid min-w-0 gap-2 text-xs sm:grid-cols-2">
                        <div className="min-w-0">
                          <dt className="text-muted">
                            {t("reconciliation.novelId")}
                          </dt>
                          <dd className="mt-0.5 truncate text-foreground">
                            {asset.novel_id}
                          </dd>
                        </div>
                        <div className="min-w-0">
                          <dt className="text-muted">
                            {t("reconciliation.subject")}
                          </dt>
                          <dd className="mt-0.5 truncate text-foreground">
                            {asset.subject_kind} · {asset.subject_id}
                          </dd>
                        </div>
                        <div className="min-w-0 sm:col-span-2">
                          <dt className="text-muted">
                            {t("reconciliation.path")}
                          </dt>
                          <dd
                            data-testid="reconciliation-path"
                            className="mt-0.5 block max-w-full truncate font-mono text-foreground"
                            title={
                              asset.relative_path ??
                              t("reconciliation.pathUnavailable")
                            }
                          >
                            {asset.relative_path ??
                              t("reconciliation.pathUnavailable")}
                          </dd>
                        </div>
                      </dl>
                    </li>
                  ))}
                </ul>
                {reconciliationReport.missing_assets_truncated && (
                  <p className="mt-2 text-xs text-muted">
                    {t("reconciliation.truncated", {
                      count: reconciliationReport.missing_assets.length,
                    })}
                  </p>
                )}
              </div>
            )}

            {reconciliationReport.orphan_file_count > 0 && (
              <div>
                <h4 className="text-sm font-semibold text-foreground">
                  {t("reconciliation.orphanTitle")}
                </h4>
                <p className="mt-1 text-xs leading-5 text-muted">
                  {t("reconciliation.orphanDescription")}
                </p>
                <ul className="mt-2 space-y-2">
                  {reconciliationReport.orphan_files.map((file) => (
                    <li
                      key={file.relative_path}
                      className="min-w-0 rounded-lg border border-border bg-surface p-3"
                    >
                      <div className="flex min-w-0 flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                        <span className="w-fit rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-900 dark:bg-amber-950 dark:text-amber-200">
                          {t("reconciliation.orphanBadge")}
                        </span>
                        <span className="text-xs text-muted">
                          {formatSize(file.byte_size)}
                        </span>
                      </div>
                      <dl className="mt-3 grid min-w-0 gap-2 text-xs sm:grid-cols-2">
                        <div className="min-w-0">
                          <dt className="text-muted">
                            {t("reconciliation.novelId")}
                          </dt>
                          <dd className="mt-0.5 truncate text-foreground">
                            {file.novel_id ??
                              t("reconciliation.unknownNovel")}
                          </dd>
                        </div>
                        <div className="min-w-0">
                          <dt className="text-muted">
                            {t("reconciliation.hash")}
                          </dt>
                          <dd className="mt-0.5 truncate font-mono text-foreground">
                            {file.content_hash ??
                              t("reconciliation.unknownHash")}
                          </dd>
                        </div>
                        <div className="min-w-0 sm:col-span-2">
                          <dt className="text-muted">
                            {t("reconciliation.path")}
                          </dt>
                          <dd
                            data-testid="reconciliation-path"
                            className="mt-0.5 block max-w-full truncate font-mono text-foreground"
                            title={file.relative_path}
                          >
                            {file.relative_path}
                          </dd>
                        </div>
                      </dl>
                    </li>
                  ))}
                </ul>
                {reconciliationReport.orphan_files_truncated && (
                  <p className="mt-2 text-xs text-muted">
                    {t("reconciliation.truncated", {
                      count: reconciliationReport.orphan_files.length,
                    })}
                  </p>
                )}
              </div>
            )}

            {reconciliationReport.unmanaged_file_count > 0 && (
              <div>
                <h4 className="text-sm font-semibold text-foreground">
                  {t("reconciliation.unmanagedTitle")}
                </h4>
                <p className="mt-1 text-xs leading-5 text-muted">
                  {t("reconciliation.unmanagedDescription")}
                </p>
                <ul className="mt-2 space-y-2">
                  {reconciliationReport.unmanaged_files.map((file) => (
                    <li
                      key={file.relative_path}
                      className="min-w-0 rounded-lg border border-border bg-surface p-3"
                    >
                      <div className="flex min-w-0 flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                        <span className="w-fit rounded-full bg-blue-100 px-2 py-0.5 text-xs font-medium text-blue-900 dark:bg-blue-950 dark:text-blue-200">
                          {t("reconciliation.unmanagedBadge")}
                        </span>
                        <span className="text-xs text-muted">
                          {formatSize(file.byte_size)}
                        </span>
                      </div>
                      <dl className="mt-3 min-w-0 text-xs">
                        <div className="min-w-0">
                          <dt className="text-muted">
                            {t("reconciliation.path")}
                          </dt>
                          <dd
                            data-testid="reconciliation-unmanaged-path"
                            className="mt-0.5 block max-w-full truncate font-mono text-foreground"
                            title={file.relative_path}
                          >
                            {file.relative_path}
                          </dd>
                        </div>
                      </dl>
                    </li>
                  ))}
                </ul>
                {reconciliationReport.unmanaged_files_truncated && (
                  <p className="mt-2 text-xs text-muted">
                    {t("reconciliation.truncated", {
                      count: reconciliationReport.unmanaged_files.length,
                    })}
                  </p>
                )}
              </div>
            )}
          </div>
        )}
      </section>
    </div>
  );
}
