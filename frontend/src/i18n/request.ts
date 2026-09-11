import { hasLocale } from "next-intl";
import { getRequestConfig } from "next-intl/server";
import { routing, type AppLocale } from "./routing";

const messageLoaders = {
  zh: () => import("./messages/zh.json"),
  en: () => import("./messages/en.json"),
} satisfies Record<AppLocale, () => Promise<{ default: unknown }>>;

export default getRequestConfig(async ({ requestLocale }) => {
  const requested = await requestLocale;
  const locale = hasLocale(routing.locales, requested) ? requested : routing.defaultLocale;
  return { locale, messages: (await messageLoaders[locale]()).default };
});
