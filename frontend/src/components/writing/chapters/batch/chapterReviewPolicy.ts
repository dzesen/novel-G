export type ReviewEnforcement = "advisory" | "strict";

export interface ChapterReviewSelection {
  schema_version: "chapter_review_selection.v1";
  mode: "key_chapters" | "selected_chapters" | "all_chapters" | "no_chapters";
  selected_chapter_ids: string[];
  review_after_prose_repair: true;
  enforcement?: ReviewEnforcement;
}

export interface ReviewChapter {
  chapter_id: string;
  volume_id: string;
  order_index: number;
}

export interface ChapterReviewAuthorization {
  schema_version: "chapter_review_authorization.v1";
  selection: ChapterReviewSelection;
  chapter_ids: string[];
  required_chapter_ids: string[];
  digest: string;
}

export function initialChapterReviewSelection(): ChapterReviewSelection {
  return {
    schema_version: "chapter_review_selection.v1",
    mode: "key_chapters",
    selected_chapter_ids: [],
    review_after_prose_repair: true,
    enforcement: "advisory",
  };
}

export function keyReviewChapterIds(chapters: ReviewChapter[]): Set<string> {
  const volumes = new Map<string, ReviewChapter[]>();
  for (const chapter of chapters) {
    const entries = volumes.get(chapter.volume_id) ?? [];
    entries.push(chapter);
    volumes.set(chapter.volume_id, entries);
  }
  return new Set([...volumes.values()].flatMap((entries) => {
    const ordered = [...entries].sort((a, b) => a.order_index - b.order_index);
    return [ordered[0].chapter_id, ordered[ordered.length - 1].chapter_id];
  }));
}

export function changeChapterReviewMode(
  selection: ChapterReviewSelection, mode: ChapterReviewSelection["mode"],
): ChapterReviewSelection {
  return {
    ...selection, mode,
    selected_chapter_ids: mode === "all_chapters" || mode === "no_chapters"
      ? [] : selection.selected_chapter_ids,
  };
}

export function toggleReviewChapter(
  selection: ChapterReviewSelection, chapterId: string, checked: boolean,
): ChapterReviewSelection {
  const ids = new Set(selection.selected_chapter_ids);
  if (checked) ids.add(chapterId);
  else ids.delete(chapterId);
  return { ...selection, selected_chapter_ids: [...ids].sort() };
}
