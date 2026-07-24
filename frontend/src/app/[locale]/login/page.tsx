"use client";

import { FormEvent, useEffect, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { useTranslations } from "next-intl";

import { useAuth } from "@/components/auth/AuthProvider";


function safeReturnPath(locale: string): string {
  if (typeof window === "undefined") return `/${locale}`;
  const candidate = new URLSearchParams(window.location.search).get("next");
  if (!candidate || !candidate.startsWith(`/${locale}`) || candidate.startsWith("//")) {
    return `/${locale}`;
  }
  return candidate;
}


export default function LoginPage() {
  const t = useTranslations("auth");
  const { phase, login, setup } = useAuth();
  const router = useRouter();
  const pathname = usePathname();
  const locale = pathname.startsWith("/en") ? "en" : "zh";
  const isSetup = phase === "setup";
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (phase === "authenticated") {
      router.replace(safeReturnPath(locale));
    }
  }, [locale, phase, router]);

  const switchLocale = (nextLocale: "zh" | "en") => {
    if (nextLocale === locale) return;
    router.replace(`/${nextLocale}/login`);
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSubmitting(true);
    setError("");
    try {
      if (isSetup) {
        await setup(username, displayName, password);
      } else {
        await login(username, password);
      }
      router.replace(safeReturnPath(locale));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : t("unknownError"));
    } finally {
      setSubmitting(false);
    }
  };

  if (phase === "loading" || phase === "authenticated") {
    return (
      <div className="grid min-h-screen place-items-center bg-background text-sm text-muted">
        {t("checking")}
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-background lg:grid lg:grid-cols-[minmax(22rem,0.9fr)_minmax(32rem,1.1fr)]">
      <section className="relative hidden overflow-hidden bg-warm-900 px-12 py-14 text-warm-100 lg:flex lg:flex-col lg:justify-between dark:bg-warm-100 dark:text-warm-900">
        <div
          aria-hidden="true"
          className="absolute -right-24 top-20 h-80 w-80 rounded-full border border-warm-600/35"
        />
        <div
          aria-hidden="true"
          className="absolute -right-2 top-52 h-48 w-48 rounded-full border border-warm-500/30"
        />
        <div className="relative">
          <p className="text-sm font-semibold text-warm-500 dark:text-warm-600">
            {t("localBadge")}
          </p>
          <h1 className="mt-7 max-w-lg text-4xl font-semibold leading-tight tracking-[-0.035em] xl:text-5xl">
            {t("statement")}
          </h1>
          <p className="mt-6 max-w-md text-base leading-7 text-warm-300 dark:text-warm-700">
            {t("statementDetail")}
          </p>
        </div>

        <div className="relative border-t border-warm-700/70 pt-6 text-sm leading-6 text-warm-400 dark:border-warm-300 dark:text-warm-600">
          {t("privacyNote")}
        </div>
      </section>

      <section className="flex min-h-screen flex-col px-5 py-6 sm:px-10 lg:px-16 lg:py-10">
        <div className="flex items-center justify-between">
          <span className="text-base font-bold tracking-tight text-foreground">
            {t("product")}
          </span>
          <div className="flex rounded-lg border border-border bg-surface-secondary p-0.5">
            {(["zh", "en"] as const).map((item) => (
              <button
                key={item}
                type="button"
                onClick={() => switchLocale(item)}
                className={`rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
                  locale === item ? "bg-accent text-white" : "text-muted hover:text-foreground"
                }`}
              >
                {item === "zh" ? "中文" : "EN"}
              </button>
            ))}
          </div>
        </div>

        <div className="mx-auto flex w-full max-w-md flex-1 flex-col justify-center py-12">
          <div className="mb-9">
            <p className="mb-3 text-sm font-semibold text-accent">
              {isSetup ? t("setupKicker") : t("loginKicker")}
            </p>
            <h2 className="text-3xl font-semibold tracking-[-0.03em] text-foreground sm:text-4xl">
              {isSetup ? t("setupTitle") : t("loginTitle")}
            </h2>
            <p className="mt-4 max-w-sm text-sm leading-6 text-muted">
              {isSetup ? t("setupDescription") : t("loginDescription")}
            </p>
          </div>

          <form onSubmit={submit} className="space-y-5">
            {isSetup && (
              <label className="block">
                <span className="mb-2 block text-sm font-medium text-foreground">
                  {t("displayName")}
                </span>
                <input
                  required
                  autoComplete="name"
                  value={displayName}
                  onChange={(event) => setDisplayName(event.target.value)}
                  className="h-11 w-full rounded-lg border border-border bg-surface px-3.5 text-sm text-foreground outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/20"
                  placeholder={t("displayNamePlaceholder")}
                />
              </label>
            )}

            <label className="block">
              <span className="mb-2 block text-sm font-medium text-foreground">
                {t("username")}
              </span>
              <input
                required
                minLength={3}
                maxLength={64}
                autoComplete="username"
                autoFocus={!isSetup}
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                className="h-11 w-full rounded-lg border border-border bg-surface px-3.5 text-sm text-foreground outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/20"
                placeholder={t("usernamePlaceholder")}
              />
            </label>

            <label className="block">
              <span className="mb-2 block text-sm font-medium text-foreground">
                {t("password")}
              </span>
              <input
                required
                minLength={isSetup ? 12 : 1}
                maxLength={256}
                type="password"
                autoComplete={isSetup ? "new-password" : "current-password"}
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                className="h-11 w-full rounded-lg border border-border bg-surface px-3.5 text-sm text-foreground outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/20"
                placeholder={t("passwordPlaceholder")}
              />
              {isSetup && (
                <span className="mt-2 block text-xs leading-5 text-muted">
                  {t("passwordHint")}
                </span>
              )}
            </label>

            {error && (
              <div
                role="alert"
                className="rounded-lg border border-red-300 bg-red-50 px-3.5 py-3 text-sm text-red-800 dark:border-red-900 dark:bg-red-950/40 dark:text-red-200"
              >
                {error}
              </div>
            )}

            <button
              type="submit"
              disabled={submitting}
              className="flex h-11 w-full items-center justify-center rounded-lg bg-accent px-4 text-sm font-semibold text-white shadow-[0_6px_18px_rgba(45,58,36,0.18)] transition hover:bg-accent-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {submitting
                ? t("submitting")
                : isSetup
                  ? t("setupAction")
                  : t("loginAction")}
            </button>
          </form>

          <p className="mt-7 text-center text-xs leading-5 text-muted">
            {isSetup ? t("setupFootnote") : t("loginFootnote")}
          </p>
        </div>
      </section>
    </div>
  );
}
