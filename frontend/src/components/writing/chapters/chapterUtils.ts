import type { ChapterDetail, ChapterDraft } from "@/types/novel";
import { buildUserStorageKey } from "@/lib/userStorage";

export { countChapterWords } from "./chapterWordCount";

interface LocalChapterDraft {
  draft: ChapterDraft;
  savedAt: string;
  acknowledgedDraft?: ChapterDraft;
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
  let acknowledgedDraft: ChapterDraft | undefined;
  try {
    const raw = window.localStorage.getItem(key);
    if (raw) acknowledgedDraft = (JSON.parse(raw) as LocalChapterDraft).acknowledgedDraft;
  } catch {
    // A malformed older backup must not prevent saving the current edit.
  }
  const value: LocalChapterDraft = { draft, savedAt: new Date().toISOString(), acknowledgedDraft };
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
    const serverDraft = chapterToDraft(chapter);
    const matchesAcknowledged = value.acknowledgedDraft && sameChapterDraft(value.acknowledgedDraft, serverDraft);
    if (sameChapterDraft(value.draft, serverDraft) || (
      !matchesAcknowledged && new Date(value.savedAt).getTime() <= new Date(chapter.updated_at).getTime()
    )) {
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

function sameChapterDraft(left: ChapterDraft, right: ChapterDraft): boolean {
  // Chapter updates normalize title whitespace on the server.
  return left.title.trim() === right.title.trim() && left.summary === right.summary
    && left.content === right.content && left.status === right.status;
}

/** A response confirms only its snapshot; later local edits still need recovery. */
export function acknowledgeLocalChapterDraft(chapterId: string, saved: ChapterDraft): void {
  if (typeof window === "undefined") return;
  const key = storageKey(chapterId);
  if (!key) return;
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return;
    const value = JSON.parse(raw) as LocalChapterDraft;
    if (!value?.draft) return;
    if (sameChapterDraft(value.draft, saved)) {
      window.localStorage.removeItem(key);
    } else {
      // The server timestamp can exceed the newer edit's local timestamp.
      window.localStorage.setItem(key, JSON.stringify({ ...value, acknowledgedDraft: saved }));
    }
  } catch {
    // Recovery storage failure cannot turn an acknowledged server save into a failure.
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
