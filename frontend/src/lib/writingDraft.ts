import type { WritingDraft } from "@/types/novel";
import { buildUserStorageKey } from "@/lib/userStorage";

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

function getCurrentDraftId(): string | null {
  try {
    const key = currentDraftKey();
    return key ? localStorage.getItem(key) : null;
  } catch {
    return null;
  }
}

function parseDraft(raw: string | null): WritingDraft | null {
  if (!raw) {
    return null;
  }

  try {
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === "object") {
      return parsed as WritingDraft;
    }
  } catch {
    return null;
  }

  return null;
}

function persistWritingDraft(draft: WritingDraft, draftId: string): void {
  const serialized = JSON.stringify(draft);

  try {
    const itemKey = draftKey(draftId);
    const currentKey = currentDraftKey();
    if (!itemKey || !currentKey) return;
    localStorage.setItem(itemKey, serialized);
    localStorage.setItem(currentKey, draftId);
  } catch {
    try {
      const sessionKey = fallbackDraftKey();
      if (sessionKey) sessionStorage.setItem(sessionKey, serialized);
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
  const candidateKeys: string[] = [];
  if (draftId) {
    const key = draftKey(draftId);
    if (key) candidateKeys.push(key);
  }

  try {
    const currentKey = currentDraftKey();
    const currentDraftId = currentKey ? localStorage.getItem(currentKey) : null;
    if (currentDraftId) {
      const key = draftKey(currentDraftId);
      if (key) candidateKeys.push(key);
    }

    for (const key of Array.from(new Set(candidateKeys))) {
      const draft = parseDraft(localStorage.getItem(key));
      if (draft) {
        return draft;
      }
    }
  } catch {
    // localStorage 不可用时继续尝试旧的 sessionStorage 草稿。
  }

  try {
    const sessionKey = fallbackDraftKey();
    return sessionKey ? parseDraft(sessionStorage.getItem(sessionKey)) : null;
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
  const currentDraftId = draftId || getCurrentDraftId() || undefined;
  const currentDraft = loadWritingDraft(currentDraftId);

  if (!currentDraft) {
    return null;
  }

  const nextDraft = updater(currentDraft);
  // 创建态所有临时 UI 状态都跟随草稿 ID 写回，刷新后才能恢复对话和版本栈。
  persistWritingDraft(nextDraft, currentDraftId || createDraftId());
  return nextDraft;
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
    if (draftId) {
      const key = draftKey(draftId);
      if (key) localStorage.removeItem(key);
    }

    const currentKey = currentDraftKey();
    const currentDraftId = currentKey ? localStorage.getItem(currentKey) : null;
    if (!draftId || currentDraftId === draftId) {
      if (currentKey) localStorage.removeItem(currentKey);
    }
  } catch {
    // 清理失败不应阻塞页面跳转或小说保存。
  }

  try {
    const sessionKey = fallbackDraftKey();
    if (sessionKey) sessionStorage.removeItem(sessionKey);
  } catch {
    // ignore
  }
}
