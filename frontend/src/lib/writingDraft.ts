import type { WritingDraft } from "@/types/novel";
import { buildUserStorageKey } from "./userStorage.ts";
import { isWritingDraft } from "./novelCreationDraft.ts";

export interface StoredWritingDraft {
  draftId: string;
  draft: WritingDraft;
}

function draftKey(draftId: string): string | null {
  return buildUserStorageKey("draft", draftId);
}

function currentDraftKey(): string | null {
  return buildUserStorageKey("draft", "current");
}

function fallbackDraftKey(): string | null {
  return buildUserStorageKey("draft", "session");
}

function createDraftId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }

  return `${Date.now()}_${Math.random().toString(36).slice(2)}`;
}

function getCurrentDraftIds(): string[] {
  const result: string[] = [];
  try {
    const key = currentDraftKey();
    const localValue = key ? localStorage.getItem(key) : null;
    if (localValue) result.push(localValue);
  } catch {
    // localStorage 不可用时继续读取 sessionStorage。
  }

  try {
    const key = currentDraftKey();
    const sessionValue = key ? sessionStorage.getItem(key) : null;
    if (sessionValue && !result.includes(sessionValue)) {
      result.push(sessionValue);
    }
  } catch {
    // ignore
  }
  return result;
}

function parseDraft(raw: string | null): WritingDraft | null {
  if (!raw) {
    return null;
  }

  try {
    const parsed = JSON.parse(raw);
    return isWritingDraft(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function loadExactWritingDraft(draftId: string): WritingDraft | null {
  const key = draftKey(draftId);
  if (!key) return null;

  try {
    const draft = parseDraft(localStorage.getItem(key));
    if (draft) return draft;
  } catch {
    // localStorage 不可用时继续读取 sessionStorage。
  }

  try {
    return parseDraft(sessionStorage.getItem(key));
  } catch {
    return null;
  }
}

function persistWritingDraft(draft: WritingDraft, draftId: string): void {
  const serialized = JSON.stringify(draft);
  const itemKey = draftKey(draftId);
  const currentKey = currentDraftKey();
  if (!itemKey || !currentKey) return;

  try {
    localStorage.setItem(itemKey, serialized);
    localStorage.setItem(currentKey, draftId);
    return;
  } catch {
    try {
      sessionStorage.setItem(itemKey, serialized);
      sessionStorage.setItem(currentKey, draftId);
    } catch {
      // ignore
    }
  }
}

/**
 * 保存一份新的写作创建草稿。
 *
 * Args:
 *   draft: 需要写入浏览器存储的创建草稿。
 *
 * Returns:
 *   新草稿 ID。
 */
export function saveWritingDraft(draft: WritingDraft): string {
  const draftId = createDraftId();
  persistWritingDraft(draft, draftId);

  return draftId;
}

/**
 * 加载指定或当前写作创建草稿。
 *
 * Args:
 *   draftId: URL 中携带的草稿 ID；为空时尝试加载当前草稿。
 *
 * Returns:
 *   找到的创建草稿；不存在或解析失败时返回 null。
 */
export function loadWritingDraft(draftId?: string): WritingDraft | null {
  if (draftId) {
    return loadExactWritingDraft(draftId);
  }

  return loadCurrentWritingDraft()?.draft ?? null;
}

/** 返回可恢复的当前草稿及其稳定 ID。 */
export function loadCurrentWritingDraft(): StoredWritingDraft | null {
  for (const currentDraftId of getCurrentDraftIds()) {
    const draft = loadExactWritingDraft(currentDraftId);
    if (draft) return { draftId: currentDraftId, draft };
  }

  try {
    const sessionKey = fallbackDraftKey();
    const legacyDraft = sessionKey
      ? parseDraft(sessionStorage.getItem(sessionKey))
      : null;
    if (!legacyDraft) return null;

    const draftId = createDraftId();
    persistWritingDraft(legacyDraft, draftId);
    if (sessionKey) sessionStorage.removeItem(sessionKey);
    return { draftId, draft: legacyDraft };
  } catch {
    return null;
  }
}

/**
 * 原地更新指定或当前创建草稿。
 *
 * Args:
 *   draftId: 需要更新的草稿 ID；为空时更新当前草稿。
 *   updater: 接收旧草稿并返回新草稿的更新函数。
 *
 * Returns:
 *   更新后的草稿；草稿不存在时返回 null。
 */
export function updateWritingDraft(
  draftId: string | undefined,
  updater: (draft: WritingDraft) => WritingDraft,
): WritingDraft | null {
  const storedDraft = draftId
    ? { draftId, draft: loadExactWritingDraft(draftId) }
    : loadCurrentWritingDraft();

  if (!storedDraft?.draft) {
    return null;
  }

  const nextDraft = updater(storedDraft.draft);
  // 创建态所有临时 UI 状态都跟随草稿 ID 写回，刷新后才能恢复对话和版本栈。
  persistWritingDraft(nextDraft, storedDraft.draftId);
  return nextDraft;
}

function clearStorageDraft(storage: Storage, draftId?: string): void {
  const currentKey = currentDraftKey();
  const currentDraftId = currentKey ? storage.getItem(currentKey) : null;
  const targetDraftId = draftId || currentDraftId;
  if (targetDraftId) {
    const key = draftKey(targetDraftId);
    if (key) storage.removeItem(key);
  }
  if (!draftId || currentDraftId === draftId) {
    if (currentKey) storage.removeItem(currentKey);
  }
}

/**
 * 清理指定或当前写作创建草稿。
 *
 * Args:
 *   draftId: 需要清理的草稿 ID；为空时清理当前草稿。
 *
 * Returns:
 *   无。
 */
export function clearWritingDraft(draftId?: string): void {
  try {
    clearStorageDraft(localStorage, draftId);
  } catch {
    // 清理失败不应阻塞页面跳转或小说保存。
  }

  try {
    clearStorageDraft(sessionStorage, draftId);
    const sessionKey = fallbackDraftKey();
    if (sessionKey) sessionStorage.removeItem(sessionKey);
  } catch {
    // ignore
  }
}
