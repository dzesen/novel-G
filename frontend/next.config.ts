import type { NextConfig } from "next";
import createNextIntlPlugin from "next-intl/plugin";

const withNextIntl = createNextIntlPlugin("./src/i18n/request.ts");

const nextConfig: NextConfig = {
  ...(process.env.NOVEL_G_DESKTOP_BUILD === "1" ? { output: "standalone" as const } : {}),
  reactCompiler: true,
  poweredByHeader: false,
};

export default withNextIntl(nextConfig);
