"use client";

import { useEffect, useRef } from "react";
import { useTranslations } from "next-intl";
import { useRouter, usePathname } from "next/navigation";
import { Button } from "@/components/ui/Button";
import type { NovelDetail } from "@/types/novel";
import BookCover from "./BookCover";

interface NovelDetailPanelProps {
  novel: NovelDetail | null;
  onDelete: (id: string) => void;
  onBack: () => void;
}
const PREVIEW_LONG_FIELDS: { key: keyof NovelDetail; labelKey: string }[] = [
  { key: "introduction", labelKey: "introduction" }, { key: "summary", labelKey: "summary" },
  { key: "tone", labelKey: "tone" }, { key: "target_audience", labelKey: "targetAudience" },
];
const STYLE_FIELDS: { key: keyof NovelDetail; labelKey: string }[] = [
  { key: "writing_style", labelKey: "writingStyle" }, { key: "narrative_pov", labelKey: "narrativePov" },
  { key: "era_background", labelKey: "eraBackground" },
];

export default function NovelDetailPanel({ novel, onDelete, onBack }: NovelDetailPanelProps) {
  const t = useTranslations("novel");
  const tb = useTranslations("bookshelf");
  const router = useRouter();
  const pathname = usePathname();
  const locale = pathname.startsWith("/en") ? "en" : "zh";
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { heading.current?.focus(); }, [novel?._id]);
  if (!novel) return null;
  const statuses: Record<string, string> = { draft: "statusDraft", ongoing: "statusOngoing", completed: "statusCompleted", paused: "statusPaused" };
  const handleDelete = () => { if (confirm(tb("deleteConfirm", { title: novel.title }))) onDelete(novel._id); };
  return (
    <article className="novel-overview">
      <button type="button" onClick={onBack} className="library-back">
        <svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d="M20 12H4m6-6-6 6 6 6" /></svg>{tb("backToShelf")}
      </button>
      <header className="novel-overview-header">
        <BookCover novel={novel} className="detail-cover" />
        <div className="novel-overview-title">
          <div className="novel-overview-tags"><span>{t(statuses[novel.status] ?? "statusDraft")}</span>{novel.genre !== "unclassified" && <span>{novel.genre}</span>}</div>
          <h2 ref={heading} tabIndex={-1}>{novel.title}</h2>
          {novel.subtitle && <p>{novel.subtitle}</p>}
          {novel.tags?.length > 0 && <div className="novel-overview-tags">{novel.tags.map((tag) => <span key={tag}>{tag}</span>)}</div>}
          <Button variant="primary" onClick={() => router.push(`/${locale}/writing/${encodeURIComponent(novel._id)}`)}>{tb("goWriting")}<svg aria-hidden="true" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d="M4 12h16m-6-6 6 6-6 6" /></svg></Button>
        </div>
      </header>
      <div className="novel-overview-body">
        <div className="novel-overview-prose">
          {PREVIEW_LONG_FIELDS.map(({ key, labelKey }) => novel[key] ? <section key={key}><h3>{t(labelKey)}</h3><p>{String(novel[key])}</p></section> : null)}
        </div>
        <aside className="novel-overview-facts">
          <dl>
            <div><dt>{t("chapterCount")}</dt><dd>{novel.stats?.chapter_count ?? 0}</dd></div>
            <div><dt>{t("totalWords")}</dt><dd>{(novel.stats?.total_word_count ?? 0).toLocaleString(locale)}</dd></div>
            <div><dt>{t("createdAt")}</dt><dd>{new Date(novel.created_at).toLocaleDateString(locale)}</dd></div>
          </dl>
          {STYLE_FIELDS.map(({ key, labelKey }) => novel[key] ? <section key={key}><h3>{t(labelKey)}</h3><p>{String(novel[key])}</p></section> : null)}
          <Button variant="quiet" onClick={handleDelete}>{t("delete")}</Button>
        </aside>
      </div>
    </article>
  );
}
