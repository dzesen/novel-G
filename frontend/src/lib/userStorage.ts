export type UserStorageScope = "ai-create" | "draft" | "chapter" | "migration";

let currentUserId: string | null = null;

export function setCurrentStorageUser(userId: string | null): void {
  currentUserId = userId;
}

export function getCurrentStorageUser(): string | null {
  return currentUserId;
}

export function buildUserStorageKey(
  scope: UserStorageScope,
  resourceId?: string,
): string | null {
  if (!currentUserId) return null;
  const suffix = resourceId ? `:${resourceId}` : "";
  return `novel-g:${currentUserId}:${scope}${suffix}`;
}

/**
 * 将升级前的单用户草稿一次性转交给初始管理员。
 * 只应在初始管理员会话建立后调用；普通用户绝不能扫描这些旧键。
 */
export function migrateLegacyStorageForInitialAdmin(): void {
  if (typeof window === "undefined" || !currentUserId) return;
  const marker = buildUserStorageKey("migration", "legacy-v1");
  if (!marker || window.localStorage.getItem(marker)) return;

  const copyLocal = (legacyKey: string, nextKey: string | null) => {
    if (!nextKey) return;
    const value = window.localStorage.getItem(legacyKey);
    if (value && !window.localStorage.getItem(nextKey)) {
      window.localStorage.setItem(nextKey, value);
    }
    if (value) window.localStorage.removeItem(legacyKey);
  };

  copyLocal("ai_create_novel_cache", buildUserStorageKey("ai-create"));

  const legacyCurrentDraft = window.localStorage.getItem("writing_draft_current");
  if (legacyCurrentDraft) {
    copyLocal(
      `writing_draft:${legacyCurrentDraft}`,
      buildUserStorageKey("draft", legacyCurrentDraft),
    );
    copyLocal("writing_draft_current", buildUserStorageKey("draft", "current"));
  }

  for (const key of Object.keys(window.localStorage)) {
    const chapterPrefix = "novel-generator:chapter-draft:";
    if (key.startsWith(chapterPrefix)) {
      copyLocal(
        key,
        buildUserStorageKey("chapter", key.slice(chapterPrefix.length)),
      );
    }
  }

  window.localStorage.setItem(marker, new Date().toISOString());
}
