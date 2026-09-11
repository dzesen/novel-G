import WritingContent from "@/components/writing/WritingContent";
import { prepareLocalePage, type LocalePageProps } from "@/i18n/pageLocale";

export const dynamic = "force-dynamic";

export default async function WritingNewPage({ params }: LocalePageProps) {
  await prepareLocalePage(params);
  return <WritingContent mode="create" />;
}
