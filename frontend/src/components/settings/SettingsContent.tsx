"use client";

import { useEffect } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@heroui/react";
import { useConfig } from "@/hooks/useConfig";
import { useAuth } from "@/components/auth/AuthProvider";
import { DatabaseCard } from "@/components/settings/DatabaseCard";
import { ProviderCard } from "@/components/settings/ProviderCard";
import { JudgeProviderSettings } from "@/components/settings/JudgeProviderSettings";
import { ImageProviderCard } from "@/components/settings/ImageProviderCard";
import { WorkflowCard } from "@/components/settings/WorkflowCard";
import { ThemeCard } from "@/components/settings/ThemeCard";
import { BackupCard } from "@/components/settings/BackupCard";
import { UserManagementCard } from "@/components/settings/UserManagementCard";
import GenerationRoleSettings from "@/components/settings/GenerationRoleSettings";
import { validateConfig } from "@/lib/validation";
import type { AppConfig } from "@/types/config";
import { useRouter, usePathname, useSearchParams } from "next/navigation";

type SettingsSection =
  | "theme"
  | "users"
  | "database"
  | "backup"
  | "provider"
  | "workflow"
  | "generation-roles";
type ProviderSettingsTab = "llm" | "image" | "judge";

const NAV_ITEMS: { key: SettingsSection; icon: React.ReactNode }[] = [
  {
    key: "theme",
    icon: (
      <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="13.5" cy="6.5" r="2.5" />
        <circle cx="17.5" cy="10.5" r="2.5" />
        <circle cx="8.5" cy="7.5" r="2.5" />
        <circle cx="6.5" cy="12.5" r="2.5" />
        <path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.926 0 1.648-.746 1.648-1.688 0-.437-.18-.835-.437-1.125-.29-.289-.438-.652-.438-1.125a1.64 1.64 0 0 1 1.668-1.668h1.996c3.051 0 5.555-2.503 5.555-5.554C21.965 6.012 17.461 2 12 2z" />
      </svg>
    ),
  },
  {
    key: "users",
    icon: (
      <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" />
        <circle cx="9" cy="7" r="4" />
        <path d="M19 8v6" />
        <path d="M22 11h-6" />
      </svg>
    ),
  },
  {
    key: "database",
    icon: (
      <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <ellipse cx="12" cy="5" rx="9" ry="3" />
        <path d="M3 5V19A9 3 0 0 0 21 19V5" />
        <path d="M3 12A9 3 0 0 0 21 12" />
      </svg>
    ),
  },
  {
    key: "backup",
    icon: (
      <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 2v6l2-2" /><path d="m12 8-2-2" /><path d="M5 9a7 7 0 1 0 2-5" /><path d="M5 2v5h5" />
      </svg>
    ),
  },
  {
    key: "provider",
    icon: (
      <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 2a4 4 0 0 0-4 4c0 2 1.5 3.5 3 4.5V13H9v2h2v2H9v2h6v-2h-2v-2h2v-2h-2v-2.5c1.5-1 3-2.5 3-4.5a4 4 0 0 0-4-4z" />
      </svg>
    ),
  },
  {
    key: "generation-roles",
    icon: (
      <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="9" cy="8" r="4" />
        <path d="M3 21v-2a6 6 0 0 1 12 0v2" />
        <path d="m18 3 .7 1.8L21 5.5l-2.3.7L18 8l-.7-1.8L15 5.5l2.3-.7z" />
      </svg>
    ),
  },
  {
    key: "workflow",
    icon: (
      <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3 3h6v6H3z" />
        <path d="M15 3h6v6h-6z" />
        <path d="M9 15h6v6H9z" />
        <path d="M6 9v3h6m6-3v3h-6m0 0v3" />
      </svg>
    ),
  },
];

interface SettingsContentProps {
  presentation?: "page" | "modal";
}

export default function SettingsContent({
  presentation = "page",
}: SettingsContentProps) {
  const t = useTranslations("settings");
  const { user } = useAuth();
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const currentLocale = pathname.startsWith("/en") ? "en" : "zh";
  const isModal = presentation === "modal";
  const isAdmin = user?.role === "admin";
  const availableNavItems = isAdmin
    ? NAV_ITEMS
    : NAV_ITEMS.filter((item) => item.key === "generation-roles");
  const requestedSection = searchParams.get("section");
  const activeSection = availableNavItems.some(
    (item) => item.key === requestedSection,
  )
    ? requestedSection as SettingsSection
    : isAdmin
      ? "theme"
      : "generation-roles";
  const requestedProviderTab = searchParams.get("providerTab");
  const providerSettingsTab: ProviderSettingsTab = requestedProviderTab === "image" || requestedProviderTab === "judge"
    ? requestedProviderTab : "llm";
  const requestedProviderAlias = searchParams.get("provider") || "";
  const showConfigActions = ["database", "provider", "workflow"].includes(activeSection);
  const {
    config,
    loading,
    saving,
    error,
    success,
    fetchConfig,
    saveConfig,
    queueProviderRename,
    queueProviderDelete,
    imageProvidersDirty,
    imagePipelineStatuses,
    workflowCatalog,
    setConfig,
    clearMessages,
  } = useConfig(isAdmin);

  useEffect(() => {
    if (!user || isAdmin || requestedSection === "generation-roles") return;
    const next = new URLSearchParams(searchParams.toString());
    next.set("section", "generation-roles");
    window.history.replaceState(null, "", `${pathname}?${next.toString()}`);
  }, [isAdmin, pathname, requestedSection, searchParams, user]);

  const selectSection = (section: SettingsSection) => {
    const next = new URLSearchParams(searchParams.toString());
    next.set("section", section);
    window.history.replaceState(null, "", `${pathname}?${next.toString()}`);
  };

  const selectProviderTab = (tab: ProviderSettingsTab, alias?: string) => {
    const next = new URLSearchParams(searchParams.toString());
    next.set("section", "provider");
    next.set("providerTab", tab);
    if (alias !== undefined) next.set("provider", alias);
    window.history.replaceState(null, "", `${pathname}?${next.toString()}`);
  };

  useEffect(() => {
    if (!isModal) return;

    const originalOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;

      if (window.history.length > 1) {
        router.back();
        return;
      }

      router.replace(`/${currentLocale}`);
    };

    window.addEventListener("keydown", handleKeyDown);

    return () => {
      document.body.style.overflow = originalOverflow;
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [currentLocale, isModal, router]);

  const handleSave = async () => {
    if (!config) return;
    clearMessages();

    const validationError = validateConfig(config, t);
    if (validationError) {
      alert(validationError);
      return;
    }

    const hasMongo =
      config.mongodb_url !== undefined || config.mongo_database_name !== undefined;
    const msg = hasMongo ? t("saveSuccessMongo") : t("saveSuccess");
    await saveConfig(config, msg, {
      confirmProviderDeletion: (preview) => {
        const paths = preview.reference_changes.map((change) => change.path).join("\n");
        return window.confirm(t("providerDeleteConfirm", {
          paths: paths || t("providerDeleteNoReferences"),
        }));
      },
      missingConfirmationTokenMessage: t("providerDeleteMissingToken"),
    });
  };

  const handleReload = async () => {
    clearMessages();
    const result = await fetchConfig();
    if (result) {
      alert(t("reloadSuccess"));
    }
  };

  const updateConfig = (partial: Partial<AppConfig>) => {
    if (!config) return;
    setConfig({ ...config, ...partial });
  };

  const closeSettings = () => {
    if (window.history.length > 1) {
      router.back();
      return;
    }

    router.replace(`/${currentLocale}`);
  };

  const header = (
    <div className={isModal ? "flex flex-wrap items-center justify-between gap-3 border-b border-border px-3 py-3 sm:px-4" : "mb-8 flex flex-wrap items-start justify-between gap-4"}>
      <div className="flex min-w-0 items-center gap-3">
        <button
          onClick={closeSettings}
          className="flex h-9 w-9 items-center justify-center rounded-lg text-muted transition-colors hover:bg-surface-secondary hover:text-foreground"
          title={t("back")}
          aria-label={t("back")}
        >
          <svg
            xmlns="http://www.w3.org/2000/svg"
            width="20"
            height="20"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="M19 12H5" />
            <path d="m12 19-7-7 7-7" />
          </svg>
        </button>
        <div className="min-w-0">
          <h1 id="settings-title" className="text-2xl font-bold text-foreground">{t("title")}</h1>
          {!isModal && <p className="mt-1 text-sm text-muted">{t("description")}</p>}
        </div>
      </div>
      {showConfigActions && <div className="flex w-full flex-wrap gap-2 sm:w-auto sm:shrink-0">
        <Button
          variant="outline"
          onPress={handleReload}
          isDisabled={saving}
          className="flex-1 border-border text-foreground hover:bg-surface-secondary sm:flex-none"
        >
          {t("reload")}
        </Button>
        <Button
          onPress={handleSave}
          isDisabled={saving || !config}
          className="flex-1 bg-accent text-white hover:bg-accent-hover sm:flex-none"
        >
          {saving ? t("saving") : t("save")}
        </Button>
      </div>}
    </div>
  );

  const renderSectionContent = () => {
    if (activeSection === "users") return <UserManagementCard />;
    if (activeSection === "backup") return <BackupCard />;
    if (activeSection === "generation-roles") {
      return <GenerationRoleSettings />;
    }
    if (!config) return null;
    switch (activeSection) {
      case "theme":
        return <ThemeCard />;
      case "database":
        return <DatabaseCard config={config} onChange={updateConfig} />;
      case "provider":
        return (
          <div className="space-y-4">
            <div
              role="tablist"
              aria-label={t("provider.title")}
              className="grid max-w-full grid-cols-3 gap-1 rounded-lg border border-border bg-surface-secondary/40 p-1"
            >
              {(["llm", "image", "judge"] as const).map((tab) => (
                <button
                  key={tab}
                  type="button"
                  role="tab"
                  aria-selected={providerSettingsTab === tab}
                  onClick={() => selectProviderTab(tab)}
                  className={`min-h-11 min-w-0 break-words rounded-md px-2 py-2 text-sm font-medium transition-colors sm:px-4 ${
                    providerSettingsTab === tab
                      ? "bg-surface text-accent shadow-sm"
                      : "text-muted hover:text-foreground"
                  }`}
                >
                  {tab === "llm" ? t("provider.title") : tab === "image" ? t("imageProvider.title") : t("judge.title")}
                </button>
              ))}
            </div>
            {providerSettingsTab === "judge" ? (
              <JudgeProviderSettings config={config} onChange={setConfig} onEditProvider={(alias) => selectProviderTab("llm", alias)} />
            ) : providerSettingsTab === "llm" ? (
              <ProviderCard
                key={requestedProviderAlias}
                initialAlias={requestedProviderAlias}
                config={config}
                onChange={setConfig}
                onProviderRename={(from, to) => queueProviderRename({
                  kind: "rename",
                  target: "llm",
                  from_alias: from,
                  to_alias: to,
                })}
                onProviderDelete={(alias, replacementDefaultAlias) => queueProviderDelete({
                  kind: "delete",
                  target: "llm",
                  alias,
                  replacement_default_alias: replacementDefaultAlias || null,
                })}
              />
            ) : (
              <ImageProviderCard
                config={config}
                onChange={setConfig}
                hasUnsavedChanges={imageProvidersDirty}
                pipelineStatuses={imagePipelineStatuses}
                onProviderRename={(from, to) => queueProviderRename({
                  kind: "rename",
                  target: "image",
                  from_alias: from,
                  to_alias: to,
                })}
                onProviderDelete={(alias, replacementDefaultAlias) => queueProviderDelete({
                  kind: "delete",
                  target: "image",
                  alias,
                  replacement_default_alias: replacementDefaultAlias || null,
                })}
              />
            )}
          </div>
        );
      case "workflow":
        return (
          <div className="grid gap-4">
            <Button className="justify-self-start" variant="secondary" onPress={() => selectProviderTab("judge")}>{t("judge.openSettings")}</Button>
            <WorkflowCard config={config} catalog={workflowCatalog} onChange={setConfig} />
          </div>
        );
    }
  };

  const sidebar = (
    <nav className={isModal
      ? "flex w-full shrink-0 gap-1 overflow-x-auto border-b border-border bg-surface-secondary/40 px-2 py-2 md:w-52 md:flex-col md:overflow-y-auto md:border-b-0 md:border-r md:py-3"
      : "flex w-full shrink-0 gap-1 overflow-x-auto rounded-lg border border-border bg-surface p-2 md:w-56 md:flex-col md:p-3"
    }>
      {availableNavItems.map((item) => (
        <button
          key={item.key}
          onClick={() => selectSection(item.key)}
          className={`flex shrink-0 items-center gap-2.5 rounded-md px-3 py-2 text-left text-sm transition-colors md:shrink ${
            activeSection === item.key
              ? "bg-accent/10 font-medium text-accent"
              : "text-muted hover:bg-surface-secondary hover:text-foreground"
          }`}
        >
          <span className="shrink-0">{item.icon}</span>
          <span className="truncate">
            {item.key === "generation-roles"
              ? t("generationRoles.title")
              : t(`${item.key}.title`)}
          </span>
        </button>
      ))}
    </nav>
  );

  const messages = (
    <>
      {error && (
        <div className="mb-4 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700 dark:border-red-800 dark:bg-red-950/30 dark:text-red-300">
          {error}
        </div>
      )}
      {success && (
        <div className="mb-4 rounded-lg border border-green-200 bg-green-50 p-3 text-sm text-green-700 dark:border-green-800 dark:bg-green-950/30 dark:text-green-300">
          {success}
        </div>
      )}
    </>
  );

  const configIndependent = [
    "theme",
    "users",
    "backup",
    "generation-roles",
  ].includes(activeSection);
  const body = loading && !configIndependent ? (
    <div className="flex flex-1 items-center justify-center">
      <div className="text-muted">Loading...</div>
    </div>
  ) : !config && !configIndependent ? (
    <div className="flex flex-1 items-center justify-center">
      <div className="text-muted">{error || "Failed to load"}</div>
    </div>
  ) : (
    <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-hidden md:flex-row md:gap-0">
      {sidebar}
      <div className={isModal ? "min-w-0 flex-1 overflow-y-auto px-3 py-3 sm:px-4" : "min-w-0 flex-1 overflow-y-auto md:pl-6"}>
        {messages}
        {renderSectionContent()}
      </div>
    </div>
  );

  if (isModal) {
    return (
      <div
        className="fixed inset-0 z-[60] bg-black/40 px-2 py-2 backdrop-blur-sm sm:px-3 sm:py-4"
        onMouseDown={closeSettings}
      >
        <div className="mx-auto flex h-full max-w-7xl items-center justify-center">
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="settings-title"
            className="flex h-[94vh] w-full flex-col overflow-hidden rounded-lg border border-border bg-background shadow-2xl sm:h-[88vh]"
            onMouseDown={(event) => event.stopPropagation()}
          >
            {header}
            {body}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-7xl px-4 py-8">
      {header}
      {body}
    </div>
  );
}
