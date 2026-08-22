"use client";

import { useEffect } from "react";
import { usePathname, useRouter } from "next/navigation";

import Navbar from "@/components/layout/Navbar";
import { useAuth } from "@/components/auth/AuthProvider";


function LoadingScreen() {
  return (
    <div className="grid min-h-screen place-items-center bg-background px-6">
      <div className="flex items-center gap-3 text-sm text-muted" role="status">
        <span className="h-2.5 w-2.5 animate-pulse rounded-full bg-accent" />
        正在确认本机会话…
      </div>
    </div>
  );
}


export function AppShell({
  children,
  modal,
}: {
  children: React.ReactNode;
  modal: React.ReactNode;
}) {
  const { phase } = useAuth();
  const pathname = usePathname();
  const router = useRouter();
  const locale = pathname.startsWith("/en") ? "en" : "zh";
  const loginPath = `/${locale}/login`;
  const isLogin = pathname === loginPath;
  const isSavedWritingWorkspace =
    /^\/(?:zh|en)\/writing\/(?!new(?:\/|$))[^/]+(?:\/|$)/.test(pathname);

  useEffect(() => {
    if ((phase === "setup" || phase === "unauthenticated") && !isLogin) {
      const safeNext = pathname.startsWith(`/${locale}`) ? pathname : `/${locale}`;
      router.replace(`${loginPath}?next=${encodeURIComponent(safeNext)}`);
    }
  }, [
    isLogin,
    locale,
    loginPath,
    pathname,
    phase,
    router,
  ]);

  if (phase === "loading") return <LoadingScreen />;
  if ((phase === "setup" || phase === "unauthenticated") && !isLogin) {
    return <LoadingScreen />;
  }
  if (isLogin) {
    return <main className="min-h-screen">{children}</main>;
  }
  if (isSavedWritingWorkspace) {
    return (
      <>
        {children}
        {modal}
      </>
    );
  }

  return (
    <>
      <Navbar />
      <main className="flex-1">{children}</main>
      {modal}
    </>
  );
}
