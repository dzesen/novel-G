import SettingsContent from "@/components/settings/SettingsContent";
import { prepareLocalePage, type LocalePageProps } from "@/i18n/pageLocale";

export const dynamic = "force-dynamic";

export default async function SettingsPage({ params }: LocalePageProps) {
  await prepareLocalePage(params);
  return <SettingsContent />;
}
