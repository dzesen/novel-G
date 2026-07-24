import type { ChapterDetail, ChapterDraft } from "@/types/novel";
import { buildUserStorageKey } from "@/lib/userStorage";

const CJK_OR_WORD = /[\u3400-\u4dbf\u4e00-\u9fff]|[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*/g;

interface LocalChapterDraft {
  draft: ChapterDraft;
  savedAt: string;
}

export function countChapterWords(content: string): number {
  return content.match(CJK_OR_WORD)?.length ?? 0;
}

export function chapterToDraft(chapter: ChapterDetail): ChapterDraft {
  return {
    title: chapter.title,
    summary: chapter.summary || "",
    content: chapter.content || "",
    status: chapter.status,
  };
}

function storageKey(chapterId: string): string | null {
  return buildUserStorageKey("chapter", chapterId);
}

export function saveLocalChapterDraft(chapterId: string, draft: ChapterDraft): void {
  if (typeof window === "undefined") return;
  const key = storageKey(chapterId);
  if (!key) return;
  const value: LocalChapterDraft = { draft, savedAt: new Date().toISOString() };
  window.localStorage.setItem(key, JSON.stringify(value));
}

export function loadNewerLocalChapterDraft(
  chapter: ChapterDetail,
): ChapterDraft | null {
  if (typeof window === "undefined") return null;
  try {
    const key = storageKey(chapter._id);
    if (!key) return null;
    const raw = window.localStorage.getItem(key);
    if (!raw) return null;
    const value = JSON.parse(raw) as LocalChapterDraft;
    if (!value?.draft || !value.savedAt) return null;
    if (new Date(value.savedAt).getTime() <= new Date(chapter.updated_at).getTime()) {
      window.localStorage.removeItem(key);
      return null;
    }
    return value.draft;
  } catch {
    const key = storageKey(chapter._id);
    if (key) window.localStorage.removeItem(key);
    return null;
  }
}

export function clearLocalChapterDraft(chapterId: string): void {
  if (typeof window !== "undefined") {
    const key = storageKey(chapterId);
    if (key) window.localStorage.removeItem(key);
  }
}

export function downloadTextFile(filename: string, content: string): void {
  const blob = new Blob([content], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename.replace(/[\\/:*?"<>|]+/g, "_");
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}
