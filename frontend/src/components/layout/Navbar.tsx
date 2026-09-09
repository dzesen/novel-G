"use client";

import { useTranslations } from "next-intl";
import { usePathname, useRouter } from "next/navigation";
import WritingUtilities from "@/components/writing/WritingUtilities";

export default function Navbar() {
  const t = useTranslations("nav");
  const router = useRouter();
  const pathname = usePathname();
  const locale = pathname.startsWith("/en") ? "en" : "zh";
  return (
    <header className="studio-navbar">
      <div className="studio-navbar-inner">
        <button type="button" className="studio-brand" onClick={() => router.push(`/${locale}`)} aria-label={t("home")}>
          <svg aria-hidden="true" width="26" height="26" viewBox="0 0 28 28" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M5 23V6h5l8 14V5h5M5 23h5V9M18 20v3h5" /></svg>
          <span>{t("brand")}</span>
        </button>
        <span className="studio-navbar-label">{t("studio")}</span>
        <div className="studio-navbar-tools"><span>{t("workspace")}</span><WritingUtilities /></div>
      </div>
    </header>
  );
}
