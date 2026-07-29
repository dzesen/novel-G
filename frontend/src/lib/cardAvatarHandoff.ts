import { getCurrentStorageUser } from "@/lib/userStorage";

const DATABASE_NAME = "novel-g-card-avatar-handoff";
const DATABASE_VERSION = 2;
const STORE_NAME = "sources";
const USER_CREATED_AT_INDEX = "userCreatedAt";
const RETENTION_MILLISECONDS = 7 * 24 * 60 * 60 * 1000;
export const MAX_CARD_AVATAR_SOURCE_BYTES = 10 * 1024 * 1024;

interface StoredAvatarSource {
  handoffKey: string;
  userId: string;
  proposalId: string;
  blob: Blob;
  contentType: "application/json" | "image/png";
  createdAt: number;
}

export interface CardAvatarHandoff {
  blob: Blob;
  contentType: "application/json" | "image/png";
}

function requestResult<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () =>
      reject(request.error ?? new Error("IndexedDB request failed"));
  });
}

function transactionDone(transaction: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onerror = () =>
      reject(transaction.error ?? new Error("IndexedDB transaction failed"));
    transaction.onabort = () =>
      reject(transaction.error ?? new Error("IndexedDB transaction aborted"));
  });
}

function openDatabase(): Promise<IDBDatabase> {
  if (typeof indexedDB === "undefined") {
    return Promise.reject(new Error("IndexedDB is unavailable"));
  }
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DATABASE_NAME, DATABASE_VERSION);
    request.onupgradeneeded = () => {
      const database = request.result;
      if (database.objectStoreNames.contains(STORE_NAME)) {
        database.deleteObjectStore(STORE_NAME);
      }
      const store = database.createObjectStore(STORE_NAME, {
        keyPath: "handoffKey",
      });
      store.createIndex(
        USER_CREATED_AT_INDEX,
        ["userId", "createdAt"],
      );
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () =>
      reject(request.error ?? new Error("IndexedDB open failed"));
  });
}

function currentUserKey(proposalId: string): {
  handoffKey: string;
  userId: string;
} {
  const userId = getCurrentStorageUser();
  if (!userId) {
    throw new Error("Authenticated browser storage is unavailable");
  }
  return {
    handoffKey: `${userId}:${proposalId}`,
    userId,
  };
}

async function deleteExpired(
  database: IDBDatabase,
  userId: string,
): Promise<void> {
  const transaction = database.transaction(STORE_NAME, "readwrite");
  const done = transactionDone(transaction);
  const index = transaction
    .objectStore(STORE_NAME)
    .index(USER_CREATED_AT_INDEX);
  const request = index.openKeyCursor(
    IDBKeyRange.bound(
      [userId, 0],
      [userId, Date.now() - RETENTION_MILLISECONDS],
    ),
  );
  await new Promise<void>((resolve, reject) => {
    request.onsuccess = () => {
      const cursor = request.result;
      if (!cursor) {
        resolve();
        return;
      }
      transaction.objectStore(STORE_NAME).delete(cursor.primaryKey);
      cursor.continue();
    };
    request.onerror = () =>
      reject(request.error ?? new Error("IndexedDB cleanup failed"));
  });
  await done;
}

export async function stageCardAvatarHandoff(
  proposalId: string,
  file: File,
  contentType: "application/json" | "image/png",
): Promise<void> {
  if (file.size > MAX_CARD_AVATAR_SOURCE_BYTES) {
    throw new Error("Card avatar source exceeds the browser handoff limit");
  }
  const identity = currentUserKey(proposalId);
  const database = await openDatabase();
  try {
    await deleteExpired(database, identity.userId);
    const transaction = database.transaction(STORE_NAME, "readwrite");
    const done = transactionDone(transaction);
    transaction.objectStore(STORE_NAME).put({
      ...identity,
      proposalId,
      blob: file.slice(0, file.size, contentType),
      contentType,
      createdAt: Date.now(),
    } satisfies StoredAvatarSource);
    await done;
  } finally {
    database.close();
  }
}

export async function loadCardAvatarHandoff(
  proposalId: string,
): Promise<CardAvatarHandoff | null> {
  const identity = currentUserKey(proposalId);
  const database = await openDatabase();
  try {
    await deleteExpired(database, identity.userId);
    const transaction = database.transaction(STORE_NAME, "readonly");
    const done = transactionDone(transaction);
    const stored = await requestResult<StoredAvatarSource | undefined>(
      transaction.objectStore(STORE_NAME).get(identity.handoffKey),
    );
    await done;
    return stored
      ? { blob: stored.blob, contentType: stored.contentType }
      : null;
  } finally {
    database.close();
  }
}

export async function deleteCardAvatarHandoffs(
  proposalIds: readonly string[],
): Promise<void> {
  if (proposalIds.length === 0 || typeof indexedDB === "undefined") return;
  const userId = getCurrentStorageUser();
  if (!userId) return;
  const database = await openDatabase();
  try {
    const transaction = database.transaction(STORE_NAME, "readwrite");
    const done = transactionDone(transaction);
    const store = transaction.objectStore(STORE_NAME);
    for (const proposalId of proposalIds) {
      store.delete(`${userId}:${proposalId}`);
    }
    await done;
  } finally {
    database.close();
  }
}
