"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { apiDownload, apiGet, apiPostForm } from "@/lib/api";

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
      await apiPostForm("/api/backup/restore", form);
      setMessage({ tone: "success", text: t("restoreSuccess") });
      setSelectedFile(null);
      if (fileInputRef.current) fileInputRef.current.value = "";
      await loadStatus();
    } catch (error) {
      setMessage({ tone: "error", text: error instanceof Error ? error.message : t("restoreFailed") });
    } finally {
      setRestoring(false);
    }
  };

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

      <section>
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
            <div className="mt-3 flex gap-2">
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
    </div>
  );
}
