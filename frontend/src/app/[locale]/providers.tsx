"use client";

import { I18nProvider } from "@heroui/react";
import { ThemeProvider as NextThemesProvider } from "next-themes";
import { ThemeCustomizationProvider } from "@/components/ThemeCustomizationProvider";
import { AuthProvider } from "@/components/auth/AuthProvider";

export function Providers({ children, locale }: { children: React.ReactNode; locale: string }) {
  return (
    <NextThemesProvider attribute="class" defaultTheme="light" enableSystem={false}>
      <ThemeCustomizationProvider>
        <I18nProvider locale={locale}>
          <AuthProvider>{children}</AuthProvider>
        </I18nProvider>
      </ThemeCustomizationProvider>
    </NextThemesProvider>
  );
}
