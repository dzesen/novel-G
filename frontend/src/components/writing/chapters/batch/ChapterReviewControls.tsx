"use client";

import { useTranslations } from "next-intl";
import {
  keyReviewChapterIds,
  toggleReviewChapter,
  type ChapterReviewSelection,
  type ReviewChapter,
} from "./chapterReviewPolicy";

interface Props {
  value: ChapterReviewSelection;
  onChange: (value: ChapterReviewSelection) => void;
  chapters?: ReviewChapter[];
  disabled?: boolean;
}

export default function ChapterReviewControls({
  value, onChange, chapters = [], disabled = false,
}: Props) {
  const t = useTranslations("writing.batch");
  const keyIds = keyReviewChapterIds(chapters);
  const volumeIds = [...new Set(chapters.map((chapter) => chapter.volume_id))];
  return (
    <fieldset data-testid="chapter-review-controls" disabled={disabled} className="grid min-w-0 gap-2 border-t border-border pt-3">
      <legend className="text-sm font-medium text-foreground">{t("chapterReviewTitle")}</legend>
      <label htmlFor="batch-chapter-review-mode" className="text-xs text-warm-700 dark:text-muted">
        {t("chapterReviewScope")}
      </label>
      <select
        id="batch-chapter-review-mode"
        value={value.mode}
        onChange={(event) => onChange({
          ...value,
          mode: event.target.value as ChapterReviewSelection["mode"],
          selected_chapter_ids: event.target.value === "all_chapters" ? [] : value.selected_chapter_ids,
        })}
        className="min-h-11 w-full min-w-0 rounded-md border border-border bg-surface px-3 py-2 text-base text-foreground focus-visible:outline-2 focus-visible:outline-accent sm:text-sm"
        aria-describedby="batch-chapter-review-hint"
      >
        <option value="key_chapters">{t("chapterReviewKey")}</option>
        <option value="selected_chapters">{t("chapterReviewSelected")}</option>
        <option value="all_chapters">{t("chapterReviewAll")}</option>
      </select>
      <p id="batch-chapter-review-hint" className="text-xs leading-5 text-warm-700 dark:text-muted">
        {t("chapterReviewHint")}
      </p>
      {value.mode !== "all_chapters" && (
        chapters.length === 0 ? (
          <p className="text-xs leading-5 text-warm-700 dark:text-muted">{t("chapterReviewLoadChapters")}</p>
        ) : (
          <details>
            <summary className="min-h-11 cursor-pointer py-3 text-xs font-medium text-accent focus-visible:outline-2 focus-visible:outline-accent">
              {t("chapterReviewChoose", { count: value.selected_chapter_ids.length })}
            </summary>
            <div className="grid max-h-60 gap-1 overflow-y-auto overscroll-contain">
              {chapters.map((chapter) => {
                const isKey = value.mode === "key_chapters" && keyIds.has(chapter.chapter_id);
                return (
                  <label key={chapter.chapter_id} className="flex min-h-11 min-w-0 cursor-pointer items-center gap-2 py-2 text-sm text-foreground">
                    <input
                      type="checkbox"
                      className="size-4 shrink-0 accent-accent"
                      checked={isKey || value.selected_chapter_ids.includes(chapter.chapter_id)}
                      disabled={disabled || isKey}
                      onChange={(event) => onChange(toggleReviewChapter(value, chapter.chapter_id, event.target.checked))}
                    />
                    <span className="min-w-0 break-words">
                      {volumeIds.length > 1
                        ? t("chapterReviewChapterWithVolume", { volume: volumeIds.indexOf(chapter.volume_id) + 1, chapter: chapter.order_index })
                        : t("chapterReviewChapter", { chapter: chapter.order_index })}
                      {isKey && <span className="ml-2 text-xs text-warm-700 dark:text-muted">{t("chapterReviewKeyBadge")}</span>}
                    </span>
                  </label>
                );
              })}
            </div>
          </details>
        )
      )}
    </fieldset>
  );
}
