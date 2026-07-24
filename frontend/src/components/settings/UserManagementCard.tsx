"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";

import { useAuth, type AuthUser } from "@/components/auth/AuthProvider";
import { apiGet, apiPost } from "@/lib/api";


export function UserManagementCard() {
  const t = useTranslations("settings.users");
  const { user: currentUser } = useAuth();
  const [users, setUsers] = useState<AuthUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [busyUserId, setBusyUserId] = useState<string | null>(null);
  const [resetUserId, setResetUserId] = useState<string | null>(null);
  const [resetPassword, setResetPassword] = useState("");
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [message, setMessage] = useState<{
    tone: "success" | "error";
    text: string;
  } | null>(null);

  const loadUsers = useCallback(async () => {
    setLoading(true);
    try {
      const result = await apiGet<{ data: AuthUser[] }>("/api/auth/users");
      setUsers(result.data);
    } catch (cause) {
      setMessage({
        tone: "error",
        text: cause instanceof Error ? cause.message : t("loadFailed"),
      });
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    void loadUsers();
  }, [loadUsers]);

  const createUser = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setBusyUserId("create");
    setMessage(null);
    try {
      await apiPost("/api/auth/users", {
        username,
        display_name: displayName,
        password,
      });
      setUsername("");
      setDisplayName("");
      setPassword("");
      setMessage({ tone: "success", text: t("createSuccess") });
      await loadUsers();
    } catch (cause) {
      setMessage({
        tone: "error",
        text: cause instanceof Error ? cause.message : t("createFailed"),
      });
    } finally {
      setBusyUserId(null);
    }
  };

  const changeStatus = async (target: AuthUser) => {
    const action = target.status === "active" ? "disable" : "enable";
    setBusyUserId(target.id);
    setMessage(null);
    try {
      await apiPost(`/api/auth/users/${target.id}/${action}`, {});
      setMessage({
        tone: "success",
        text: action === "disable" ? t("disableSuccess") : t("enableSuccess"),
      });
      await loadUsers();
    } catch (cause) {
      setMessage({
        tone: "error",
        text: cause instanceof Error ? cause.message : t("actionFailed"),
      });
    } finally {
      setBusyUserId(null);
    }
  };

  const submitReset = async (target: AuthUser) => {
    setBusyUserId(target.id);
    setMessage(null);
    try {
      await apiPost(`/api/auth/users/${target.id}/reset-password`, {
        password: resetPassword,
      });
      setResetPassword("");
      setResetUserId(null);
      setMessage({ tone: "success", text: t("resetSuccess") });
    } catch (cause) {
      setMessage({
        tone: "error",
        text: cause instanceof Error ? cause.message : t("resetFailed"),
      });
    } finally {
      setBusyUserId(null);
    }
  };

  return (
    <div className="space-y-7">
      <div>
        <h2 className="text-lg font-semibold text-foreground">{t("title")}</h2>
        <p className="mt-1 max-w-2xl text-sm leading-6 text-muted">
          {t("description")}
        </p>
      </div>

      {message && (
        <div
          role="status"
          className={`rounded-lg border px-3.5 py-3 text-sm ${
            message.tone === "success"
              ? "border-green-200 bg-green-50 text-green-800 dark:border-green-900 dark:bg-green-950/40 dark:text-green-300"
              : "border-red-200 bg-red-50 text-red-800 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300"
          }`}
        >
          {message.text}
        </div>
      )}

      <section className="border-b border-border pb-7">
        <h3 className="text-sm font-semibold text-foreground">{t("createTitle")}</h3>
        <p className="mt-1 text-sm leading-6 text-muted">{t("createDescription")}</p>
        <form
          onSubmit={createUser}
          className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-[1fr_1fr_1fr_auto]"
        >
          <input
            required
            minLength={3}
            maxLength={64}
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            placeholder={t("username")}
            aria-label={t("username")}
            className="h-10 rounded-lg border border-border bg-surface px-3 text-sm outline-none focus:border-accent focus:ring-2 focus:ring-accent/20"
          />
          <input
            required
            maxLength={64}
            value={displayName}
            onChange={(event) => setDisplayName(event.target.value)}
            placeholder={t("displayName")}
            aria-label={t("displayName")}
            className="h-10 rounded-lg border border-border bg-surface px-3 text-sm outline-none focus:border-accent focus:ring-2 focus:ring-accent/20"
          />
          <input
            required
            minLength={12}
            maxLength={256}
            type="password"
            autoComplete="new-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            placeholder={t("initialPassword")}
            aria-label={t("initialPassword")}
            className="h-10 rounded-lg border border-border bg-surface px-3 text-sm outline-none focus:border-accent focus:ring-2 focus:ring-accent/20"
          />
          <button
            type="submit"
            disabled={busyUserId === "create"}
            className="h-10 rounded-lg bg-accent px-4 text-sm font-semibold text-white transition hover:bg-accent-hover disabled:opacity-50"
          >
            {busyUserId === "create" ? t("creating") : t("create")}
          </button>
        </form>
      </section>

      <section>
        <div className="flex items-center justify-between gap-4">
          <div>
            <h3 className="text-sm font-semibold text-foreground">{t("listTitle")}</h3>
            <p className="mt-1 text-sm text-muted">{t("listCount", { count: users.length })}</p>
          </div>
          <button
            type="button"
            onClick={() => void loadUsers()}
            className="rounded-lg border border-border px-3 py-1.5 text-xs font-medium text-muted hover:bg-surface-secondary hover:text-foreground"
          >
            {t("refresh")}
          </button>
        </div>

        {loading ? (
          <p className="py-10 text-center text-sm text-muted">{t("loading")}</p>
        ) : (
          <div className="mt-4 divide-y divide-border border-y border-border">
            {users.map((item) => {
              const isSelf = item.id === currentUser?.id;
              const isResetting = resetUserId === item.id;
              return (
                <div key={item.id} className="py-4">
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <p className="truncate text-sm font-semibold text-foreground">
                          {item.display_name}
                        </p>
                        <span className="rounded-full bg-surface-secondary px-2 py-0.5 text-[11px] font-medium text-muted">
                          {item.role === "admin" ? t("admin") : t("user")}
                        </span>
                        <span
                          className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${
                            item.status === "active"
                              ? "bg-green-100 text-green-800 dark:bg-green-950 dark:text-green-300"
                              : "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300"
                          }`}
                        >
                          {item.status === "active" ? t("active") : t("disabled")}
                        </span>
                      </div>
                      <p className="mt-1 text-xs text-muted">@{item.username}</p>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      <button
                        type="button"
                        onClick={() => {
                          setResetUserId(isResetting ? null : item.id);
                          setResetPassword("");
                        }}
                        className="rounded-lg border border-border px-3 py-1.5 text-xs font-medium text-foreground hover:bg-surface-secondary"
                      >
                        {t("resetPassword")}
                      </button>
                      {!isSelf && item.role !== "admin" && (
                        <button
                          type="button"
                          disabled={busyUserId === item.id}
                          onClick={() => void changeStatus(item)}
                          className={`rounded-lg px-3 py-1.5 text-xs font-medium disabled:opacity-50 ${
                            item.status === "active"
                              ? "text-red-700 hover:bg-red-50 dark:text-red-300 dark:hover:bg-red-950/30"
                              : "text-accent hover:bg-accent/10"
                          }`}
                        >
                          {item.status === "active" ? t("disable") : t("enable")}
                        </button>
                      )}
                    </div>
                  </div>

                  {isResetting && (
                    <div className="mt-4 flex flex-col gap-2 rounded-lg bg-surface-secondary p-3 sm:flex-row">
                      <input
                        autoFocus
                        minLength={12}
                        maxLength={256}
                        type="password"
                        autoComplete="new-password"
                        value={resetPassword}
                        onChange={(event) => setResetPassword(event.target.value)}
                        placeholder={t("newPassword")}
                        className="h-9 flex-1 rounded-lg border border-border bg-surface px-3 text-sm outline-none focus:border-accent"
                      />
                      <button
                        type="button"
                        disabled={resetPassword.length < 12 || busyUserId === item.id}
                        onClick={() => void submitReset(item)}
                        className="h-9 rounded-lg bg-accent px-3 text-xs font-semibold text-white hover:bg-accent-hover disabled:opacity-50"
                      >
                        {t("confirmReset")}
                      </button>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </section>
    </div>
  );
}
