"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useTheme } from "next-themes";
import { useAuth } from "@/components/auth/AuthProvider";
import { IconButton } from "@/components/ui/IconButton";
import { useDismissableLayer } from "@/components/ui/useDismissableLayer";

function MenuIcon() {
  return (
    <svg aria-hidden="true" width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <circle cx="5" cy="12" r="1" />
      <circle cx="12" cy="12" r="1" />
      <circle cx="19" cy="12" r="1" />
    </svg>
  );
}

function ThemeIcon({ dark }: { dark: boolean }) {
  return dark ? (
    <svg aria-hidden="true" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
    </svg>
  ) : (
    <svg aria-hidden="true" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z" />
    </svg>
  );
}

function SettingsIcon() {
  return (
    <svg aria-hidden="true" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Z" />
      <path d="M19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06-2.83 2.83-.06-.06a1.7 1.7 0 0 0-1.88-.34 1.7 1.7 0 0 0-1.03 1.56V21h-4v-.09A1.7 1.7 0 0 0 8.97 19.35a1.7 1.7 0 0 0-1.88.34l-.06.06-2.83-2.83.06-.06A1.7 1.7 0 0 0 4.6 15a1.7 1.7 0 0 0-1.56-1.03H3v-4h.09A1.7 1.7 0 0 0 4.65 8.94a1.7 1.7 0 0 0-.34-1.88L4.25 7l2.83-2.83.06.06a1.7 1.7 0 0 0 1.88.34A1.7 1.7 0 0 0 10.05 3H10V3h4v.09a1.7 1.7 0 0 0 1.03 1.56 1.7 1.7 0 0 0 1.88-.34l.06-.06L19.8 7l-.06.06a1.7 1.7 0 0 0-.34 1.88 1.7 1.7 0 0 0 1.56 1.03H21v4h-.09A1.7 1.7 0 0 0 19.4 15Z" />
    </svg>
  );
}

export default function WritingUtilities() {
  const t = useTranslations("writing.navigation");
  const tNav = useTranslations("nav");
  const pathname = usePathname();
  const router = useRouter();
  const { theme, setTheme } = useTheme();
  const { user, logout } = useAuth();
  const [open, setOpen] = useState(false);
  const [mounted, setMounted] = useState(false);
  const [loggingOut, setLoggingOut] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const locale = pathname.startsWith("/en") ? "en" : "zh";
  const dark = mounted && theme === "dark";
  const closeMenu = useCallback(() => setOpen(false), []);

  useEffect(() => setMounted(true), []);
  useDismissableLayer(open, rootRef, triggerRef, closeMenu);

  const switchLocale = (nextLocale: "zh" | "en") => {
    if (nextLocale === locale) return;
    const rest = pathname.replace(/^\/(zh|en)/, "") || "/";
    router.push(`/${nextLocale}${rest}${window.location.search}`);
    closeMenu();
  };

  const openSettings = () => {
    const section = user?.role === "admin" ? "" : "?section=generation-roles";
    router.push(`/${locale}/settings${section}`, { scroll: false });
    closeMenu();
  };

  const handleLogout = async () => {
    setLoggingOut(true);
    try {
      await logout();
      router.replace(`/${locale}/login`);
    } finally {
      setLoggingOut(false);
    }
  };

  return (
    <div ref={rootRef} className="relative shrink-0">
      <IconButton
        ref={triggerRef}
        label={t("workspaceMenu")}
        selected={open}
        aria-expanded={open}
        aria-controls="writing-utility-menu"
        onClick={() => setOpen((value) => !value)}
      >
        <MenuIcon />
      </IconButton>
      {open && (
        <div
          id="writing-utility-menu"
          className="absolute right-0 top-[calc(100%+0.5rem)] z-[60] w-64 overflow-hidden rounded-xl border border-border bg-surface shadow-dialog"
        >
          <div className="border-b border-border px-4 py-3">
            <p className="truncate text-sm font-semibold text-foreground">
              {user?.display_name || t("userFallback")}
            </p>
            {user?.username && (
              <p className="mt-0.5 truncate text-xs text-muted">@{user.username}</p>
            )}
          </div>

          <div className="grid gap-1 p-2">
            <div className="flex items-center justify-between gap-3 rounded-lg px-2 py-1.5">
              <span className="text-xs font-medium text-muted">{tNav("language")}</span>
              <div className="flex rounded-md border border-border bg-surface-secondary p-0.5" aria-label={tNav("language")}>
                {(["zh", "en"] as const).map((item) => (
                  <button
                    key={item}
                    type="button"
                    onClick={() => switchLocale(item)}
                    aria-pressed={locale === item}
                    className={[
                      "min-h-8 rounded px-2.5 text-xs font-semibold focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus",
                      locale === item ? "bg-accent text-on-accent" : "text-muted hover:text-foreground",
                    ].join(" ")}
                  >
                    {t(item === "zh" ? "languageZh" : "languageEn")}
                  </button>
                ))}
              </div>
            </div>
            <button
              type="button"
              disabled={!mounted}
              onClick={() => setTheme(dark ? "light" : "dark")}
              className="flex min-h-10 items-center gap-3 rounded-lg px-3 text-left text-sm text-foreground hover:bg-surface-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus disabled:opacity-50"
            >
              <ThemeIcon dark={dark} />
              {dark ? t("themeLightAction") : t("themeDarkAction")}
            </button>
            <button
              type="button"
              onClick={openSettings}
              className="flex min-h-10 items-center gap-3 rounded-lg px-3 text-left text-sm text-foreground hover:bg-surface-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
            >
              <SettingsIcon />
              {tNav("settings")}
            </button>
          </div>

          <div className="border-t border-border p-2">
            <button
              type="button"
              disabled={loggingOut}
              onClick={() => void handleLogout()}
              className="min-h-10 w-full rounded-lg px-3 text-left text-sm font-medium text-muted hover:bg-surface-secondary hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus disabled:opacity-50"
            >
              {loggingOut ? tNav("loggingOut") : tNav("logout")}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
