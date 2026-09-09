"use client";

import { useMemo, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { Button } from "@/components/ui/Button";
import type { NovelSummary } from "@/types/novel";
import BookCover from "./BookCover";

interface NovelListProps {
  novels: NovelSummary[];
  loading: boolean;
  onSelect: (id: string) => void;
  onNewNovel: () => void;
  onOpenTrash: () => void;
}

function ArrowIcon() {
  return <svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d="M4 12h15m-6-6 6 6-6 6" /></svg>;
}

function NovelStatus({ status }: { status: string }) {
  const t = useTranslations("novel");
  const keys: Record<string, string> = { draft: "statusDraft", ongoing: "statusOngoing", completed: "statusCompleted", paused: "statusPaused" };
  return <span className="novel-status" data-status={status}><span aria-hidden="true" />{t(keys[status] ?? "statusDraft")}</span>;
}

export default function NovelList({ novels, loading, onSelect, onNewNovel, onOpenTrash }: NovelListProps) {
  const t = useTranslations("bookshelf");
  const locale = useLocale();
  const router = useRouter();
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("all");
  const [sort, setSort] = useState("updated");
  const ordered = useMemo(() => [...novels].sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at)), [novels]);
  const recent = ordered.find((novel) => novel.status === "ongoing") ?? ordered.find((novel) => novel.status !== "completed") ?? ordered[0];
  const visible = useMemo(() => ordered.filter((novel) => (
    (filter === "all" || novel.status === filter) &&
    [novel.title, novel.subtitle, novel.genre, ...(novel.tags ?? [])].filter(Boolean).join(" ").toLocaleLowerCase().includes(search.trim().toLocaleLowerCase())
  )).sort((a, b) => sort === "title" ? a.title.localeCompare(b.title, locale) : 0), [ordered, search, filter, sort, locale]);
  const openWriting = (novel: NovelSummary) => router.push(`/${locale}/writing/${encodeURIComponent(novel._id)}`);

  return (
    <div className="studio-library" data-testid="studio-library">
      <header className="library-heading">
        <div><h1>{t("title")}</h1><p>{t("studioDescription")}</p></div>
        <Button variant="primary" onClick={onNewNovel}>
          <svg aria-hidden="true" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"><path d="M12 5v14M5 12h14" /></svg>
          {t("newNovel")}
        </Button>
      </header>
      {!loading && recent && !search && filter === "all" && (
        <section className="library-resume" aria-label={t("resumeWriting")}>
          <BookCover novel={recent} className="resume-cover" />
          <div className="resume-copy">
            <h2>{t("resumeWriting")}</h2>
            <p className="resume-title">{recent.title}</p>
            {recent.subtitle && <p className="resume-description">{recent.subtitle}</p>}
            <div className="resume-meta">
              <NovelStatus status={recent.status} />
              <span>{t("chapterCount", { count: recent.stats?.chapter_count ?? 0 })}</span>
              <span>{t("wordCount", { count: (recent.stats?.total_word_count ?? 0).toLocaleString(locale) })}</span>
            </div>
          </div>
          <Button variant="primary" className="resume-action" onClick={() => openWriting(recent)}>{t("goWriting")}<ArrowIcon /></Button>
        </section>
      )}
      <section aria-label={t("allWorks")}>
        <div className="library-toolbar">
          <div className="library-filters" role="group" aria-label={t("filterLabel")}>
            {["all", "ongoing", "draft", "completed", "paused"].map((status) => (
              <button key={status} type="button" aria-pressed={filter === status} onClick={() => setFilter(status)}>
                {t(`filters.${status}`)}{status === "all" && <span className="library-count">{novels.length}</span>}
              </button>
            ))}
          </div>
          <div className="library-search-tools">
            <label className="library-search">
              <svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round"><circle cx="10.5" cy="10.5" r="6.5" /><path d="m16 16 4 4" /></svg>
              <input type="search" aria-label={t("searchPlaceholder")} placeholder={t("searchPlaceholder")} value={search} onChange={(event) => setSearch(event.target.value)} />
            </label>
            <select className="library-sort" aria-label={t("sortLabel")} value={sort} onChange={(event) => setSort(event.target.value)}>
              <option value="updated">{t("sortUpdated")}</option><option value="title">{t("sortTitle")}</option>
            </select>
          </div>
        </div>
        {loading ? (
          <div className="library-grid" role="status" aria-label={t("loading")}>
            {[0, 1, 2, 3].map((item) => <div key={item} className="library-skeleton animate-pulse"><div /><span /><span /></div>)}
          </div>
        ) : novels.length === 0 ? (
          <div className="library-empty">
            <svg aria-hidden="true" width="42" height="42" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.1" strokeLinecap="round" strokeLinejoin="round"><path d="M12 6c-3-2-6-2-9-1v14c3-1 6-1 9 1 3-2 6-2 9-1V5c-3-1-6-1-9 1Zm0 0v14" /></svg>
            <h2>{t("emptyTitle")}</h2><p>{t("emptyGuide")}</p><Button variant="primary" onClick={onNewNovel}>{t("newNovel")}<ArrowIcon /></Button>
          </div>
        ) : visible.length === 0 ? (
          <div className="library-empty" role="status"><h2>{t("noResults")}</h2><p>{t("noResultsHint")}</p><Button onClick={() => { setSearch(""); setFilter("all"); }}>{t("clearFilters")}</Button></div>
        ) : (
          <div className="library-grid">
            {visible.map((novel) => (
              <article className="library-book" key={novel._id}>
                <button type="button" className="library-book-open" aria-label={novel.title} onClick={() => onSelect(novel._id)}>
                  <div className="library-book-display"><BookCover novel={novel} /></div>
                  <h2>{novel.title}</h2>
                  <p className="library-book-subtitle">{novel.subtitle || (novel.genre !== "unclassified" ? novel.genre : t("manuscript"))}</p>
                </button>
                <div className="library-book-meta"><NovelStatus status={novel.status} /><span>{t("wordCount", { count: (novel.stats?.total_word_count ?? 0).toLocaleString(locale) })}</span></div>
                <div className="library-book-footer"><span>{t("updatedOn", { date: new Date(novel.updated_at).toLocaleDateString(locale, { month: "short", day: "numeric" }) })}</span><Link href={`/${locale}/writing/${encodeURIComponent(novel._id)}`} prefetch={false} aria-label={t("openWork", { title: novel.title })}><ArrowIcon /></Link></div>
              </article>
            ))}
          </div>
        )}
      </section>
      <footer className="library-footer"><p>{t("libraryFootnote")}</p><button type="button" onClick={onOpenTrash}>
        <svg aria-hidden="true" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round"><path d="M4 6h16M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7M14 10v7" /></svg>{t("trashBin")}
      </button></footer>
    </div>
  );
}
