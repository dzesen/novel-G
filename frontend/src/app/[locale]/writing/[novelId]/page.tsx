import { prepareLocalePage } from "@/i18n/pageLocale";
import WritingContent from "@/components/writing/WritingContent";

export const dynamic = "force-dynamic";

export default async function WritingEditPage({
  params,
}: {
  params: Promise<{ locale: string; novelId: string }>;
}) {
  const { novelId } = await prepareLocalePage(params);
  return <WritingContent mode="edit" novelId={novelId} />;
}
