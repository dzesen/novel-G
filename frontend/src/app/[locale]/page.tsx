import BookshelfContent from "@/components/bookshelf/BookshelfContent";
import { prepareLocalePage, type LocalePageProps } from "@/i18n/pageLocale";

export const dynamic = "force-dynamic";

export default async function HomePage({ params }: LocalePageProps) {
  await prepareLocalePage(params);
  return <BookshelfContent />;
}
