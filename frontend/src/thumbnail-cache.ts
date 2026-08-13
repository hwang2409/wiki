// WIKI-200 — IndexedDB thumbnail cache.
//
// Persists `AssetMeta` (width/height + `preview_base64`) across sessions so
// blur-up placeholders paint on the first frame after a cold app launch
// instead of waiting on a network round trip. Invalidated by the file's
// integer-ms mtime — a stale entry survives until it is next requested, at
// which point a mismatched mtime replaces it.
//
// SSR / non-browser callers get an in-memory fallback so the module stays
// importable inside unit tests.

const DB_NAME = "wiki-thumbnails-v1";
const STORE = "thumbnails";
const CURRENT_VERSION = 1;

// One shared connection per page; opened lazily. Split from the store name
// so the module can be reset from tests via `__resetThumbnailCacheForTests`.
let dbPromise: Promise<IDBDatabase | null> | null = null;

export interface ThumbnailRecord {
  path: string;
  mtimeMs: number;
  width: number;
  height: number;
  previewBase64: string | null;
  storedAt: number;
}

const memoryFallback = new Map<string, ThumbnailRecord>();

function hasIndexedDb(): boolean {
  return typeof indexedDB !== "undefined";
}

function openDb(): Promise<IDBDatabase | null> {
  if (!hasIndexedDb()) return Promise.resolve(null);
  if (dbPromise) return dbPromise;
  dbPromise = new Promise<IDBDatabase | null>((resolve) => {
    let request: IDBOpenDBRequest;
    try {
      request = indexedDB.open(DB_NAME, CURRENT_VERSION);
    } catch {
      resolve(null);
      return;
    }
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(STORE)) {
        db.createObjectStore(STORE, { keyPath: "path" });
      }
    };
    request.onsuccess = () => {
      const db = request.result;
      // Reset the shared promise when the connection drops so a subsequent
      // put() reopens cleanly rather than getting stuck against a closed
      // handle (Safari drops idle IDB connections aggressively).
      db.onclose = () => {
        if (dbPromise) dbPromise = null;
      };
      db.onversionchange = () => {
        db.close();
        if (dbPromise) dbPromise = null;
      };
      resolve(db);
    };
    request.onerror = () => resolve(null);
    request.onblocked = () => resolve(null);
  });
  return dbPromise;
}

function withStore<T>(
  mode: IDBTransactionMode,
  fn: (store: IDBObjectStore) => IDBRequest<T>,
): Promise<T | null> {
  return openDb().then((db) => {
    if (!db) return null;
    return new Promise<T | null>((resolve) => {
      let tx: IDBTransaction;
      try {
        tx = db.transaction(STORE, mode);
      } catch {
        resolve(null);
        return;
      }
      const request = fn(tx.objectStore(STORE));
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => resolve(null);
      tx.onabort = () => resolve(null);
    });
  });
}

export async function readThumbnail(path: string): Promise<ThumbnailRecord | null> {
  if (!hasIndexedDb()) return memoryFallback.get(path) ?? null;
  const record = await withStore<ThumbnailRecord | undefined>("readonly", (store) => store.get(path));
  return record ?? null;
}

// Batched read — one transaction for the whole set. Missing keys resolve
// to `null` in the returned map so callers can distinguish "not cached"
// from "cached without preview".
export async function readThumbnailBatch(
  paths: string[],
): Promise<Map<string, ThumbnailRecord | null>> {
  const out = new Map<string, ThumbnailRecord | null>();
  if (paths.length === 0) return out;
  if (!hasIndexedDb()) {
    for (const path of paths) out.set(path, memoryFallback.get(path) ?? null);
    return out;
  }
  const db = await openDb();
  if (!db) {
    for (const path of paths) out.set(path, null);
    return out;
  }
  await new Promise<void>((resolve) => {
    let tx: IDBTransaction;
    try {
      tx = db.transaction(STORE, "readonly");
    } catch {
      for (const path of paths) out.set(path, null);
      resolve();
      return;
    }
    const store = tx.objectStore(STORE);
    tx.oncomplete = () => resolve();
    tx.onabort = () => resolve();
    tx.onerror = () => resolve();
    for (const path of paths) {
      const request = store.get(path);
      request.onsuccess = () => out.set(path, (request.result as ThumbnailRecord | undefined) ?? null);
      request.onerror = () => out.set(path, null);
    }
  });
  return out;
}

export async function writeThumbnail(record: ThumbnailRecord): Promise<void> {
  if (!hasIndexedDb()) {
    memoryFallback.set(record.path, record);
    return;
  }
  await withStore("readwrite", (store) => store.put(record));
}

export async function writeThumbnailBatch(records: ThumbnailRecord[]): Promise<void> {
  if (records.length === 0) return;
  if (!hasIndexedDb()) {
    for (const record of records) memoryFallback.set(record.path, record);
    return;
  }
  const db = await openDb();
  if (!db) return;
  await new Promise<void>((resolve) => {
    let tx: IDBTransaction;
    try {
      tx = db.transaction(STORE, "readwrite");
    } catch {
      resolve();
      return;
    }
    const store = tx.objectStore(STORE);
    tx.oncomplete = () => resolve();
    tx.onabort = () => resolve();
    tx.onerror = () => resolve();
    for (const record of records) store.put(record);
  });
}

// Returns the cached record ONLY if its mtime matches the caller's
// expected value. A stale entry is discarded (evicted asynchronously) so
// the next read is a clean miss.
export async function readThumbnailIfFresh(
  path: string,
  expectedMtimeMs: number | null | undefined,
): Promise<ThumbnailRecord | null> {
  const record = await readThumbnail(path);
  if (!record) return null;
  if (expectedMtimeMs !== null && expectedMtimeMs !== undefined && record.mtimeMs !== expectedMtimeMs) {
    void deleteThumbnail(path);
    return null;
  }
  return record;
}

export async function deleteThumbnail(path: string): Promise<void> {
  if (!hasIndexedDb()) {
    memoryFallback.delete(path);
    return;
  }
  await withStore("readwrite", (store) => store.delete(path));
}

export async function clearThumbnails(): Promise<void> {
  if (!hasIndexedDb()) {
    memoryFallback.clear();
    return;
  }
  await withStore("readwrite", (store) => store.clear());
}

// Test hook — force a reopen. Never call from production code.
export function __resetThumbnailCacheForTests(): void {
  dbPromise = null;
  memoryFallback.clear();
}
