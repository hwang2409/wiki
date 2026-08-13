// WIKI-200 — IndexedDB thumbnail cache.
//
// Persists `AssetMeta` (width/height + `preview_base64`) across sessions so
// blur-up placeholders paint on the first frame after a cold app launch
// instead of waiting on a network round trip. Invalidated by the file's
// integer-ms mtime — a stale entry survives until it is next requested, at
// which point a mismatched mtime replaces it.
//
// Fallback: if IndexedDB open OR any transaction fails, the module flips
// to an in-memory Map for the rest of the session. Reads and writes stay
// on the memory path — a dead IDB never leaves the cache silently unable
// to answer.
//
// Namespace: entries are keyed by `<namespace>::<path>`. The namespace
// defaults to `default` and can be overridden via `setThumbnailCacheNamespace`
// at app boot so multiple vaults sharing an origin don't collide. Changing
// the namespace flushes the memory ring but leaves the IDB store intact
// (stale entries under the old namespace age out via LRU).
//
// LRU: both the memory ring and the IDB store are bounded — memory to
// MEMORY_MAX entries, IDB to IDB_MAX entries. The IDB prune runs
// opportunistically after write, walking oldest-first via a cursor on
// `storedAt`.

const DB_NAME = "wiki-thumbnails-v1";
const STORE = "thumbnails";
const CURRENT_VERSION = 2;
const STORED_AT_INDEX = "storedAt";

const MEMORY_MAX = 512;
const IDB_MAX = 2048;
const IDB_PRUNE_INTERVAL_WRITES = 64;

let namespace = "default";
let writeCounterSincePrune = 0;

let dbPromise: Promise<IDBDatabase | null> | null = null;
// Once true, EVERY read/write flows through memoryFallback for the rest of
// the session. Flipped by any IDB open, transaction, or store failure.
let useMemoryOnly = false;

export interface ThumbnailRecord {
  path: string;
  mtimeMs: number;
  width: number;
  height: number;
  previewBase64: string | null;
  storedAt: number;
}

// Memory fallback doubles as an LRU ring: on read/write we move the key
// to the tail of a Map (JS Map iteration order = insertion order); on
// overflow we drop the head.
const memoryFallback = new Map<string, ThumbnailRecord>();

export function setThumbnailCacheNamespace(next: string): void {
  const normalized = next.trim() || "default";
  if (normalized === namespace) return;
  namespace = normalized;
  memoryFallback.clear();
}

function keyFor(path: string): string {
  return `${namespace}::${path}`;
}

function bumpMemory(key: string, record: ThumbnailRecord): void {
  memoryFallback.delete(key);
  memoryFallback.set(key, record);
  while (memoryFallback.size > MEMORY_MAX) {
    const oldest = memoryFallback.keys().next();
    if (oldest.done) break;
    memoryFallback.delete(oldest.value);
  }
}

function readMemory(key: string): ThumbnailRecord | null {
  const record = memoryFallback.get(key);
  if (!record) return null;
  bumpMemory(key, record);
  return record;
}

function hasIndexedDb(): boolean {
  return typeof indexedDB !== "undefined";
}

function fallToMemory(): void {
  if (useMemoryOnly) return;
  useMemoryOnly = true;
  dbPromise = null;
}

function openDb(): Promise<IDBDatabase | null> {
  if (useMemoryOnly) return Promise.resolve(null);
  if (!hasIndexedDb()) {
    fallToMemory();
    return Promise.resolve(null);
  }
  if (dbPromise) return dbPromise;
  dbPromise = new Promise<IDBDatabase | null>((resolve) => {
    let request: IDBOpenDBRequest;
    try {
      request = indexedDB.open(DB_NAME, CURRENT_VERSION);
    } catch {
      fallToMemory();
      resolve(null);
      return;
    }
    request.onupgradeneeded = () => {
      const db = request.result;
      const store = db.objectStoreNames.contains(STORE)
        ? request.transaction!.objectStore(STORE)
        : db.createObjectStore(STORE, { keyPath: "path" });
      if (!store.indexNames.contains(STORED_AT_INDEX)) {
        store.createIndex(STORED_AT_INDEX, "storedAt");
      }
    };
    request.onsuccess = () => {
      const db = request.result;
      db.onclose = () => {
        if (dbPromise) dbPromise = null;
      };
      db.onversionchange = () => {
        db.close();
        if (dbPromise) dbPromise = null;
      };
      resolve(db);
    };
    request.onerror = () => {
      fallToMemory();
      resolve(null);
    };
    request.onblocked = () => {
      fallToMemory();
      resolve(null);
    };
  });
  return dbPromise;
}

async function withStore<T>(
  mode: IDBTransactionMode,
  fn: (store: IDBObjectStore) => IDBRequest<T>,
): Promise<T | null> {
  const db = await openDb();
  if (!db) return null;
  return new Promise<T | null>((resolve) => {
    let tx: IDBTransaction;
    try {
      tx = db.transaction(STORE, mode);
    } catch {
      fallToMemory();
      resolve(null);
      return;
    }
    tx.onabort = () => {
      fallToMemory();
      resolve(null);
    };
    tx.onerror = () => {
      fallToMemory();
      resolve(null);
    };
    const request = fn(tx.objectStore(STORE));
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => {
      fallToMemory();
      resolve(null);
    };
  });
}

async function pruneIfDue(insertedRecords: number): Promise<void> {
  writeCounterSincePrune += insertedRecords;
  if (writeCounterSincePrune < IDB_PRUNE_INTERVAL_WRITES) return;
  writeCounterSincePrune = 0;
  if (useMemoryOnly) return;
  const db = await openDb();
  if (!db) return;
  await new Promise<void>((resolve) => {
    let tx: IDBTransaction;
    try {
      tx = db.transaction(STORE, "readwrite");
    } catch {
      fallToMemory();
      resolve();
      return;
    }
    const store = tx.objectStore(STORE);
    tx.oncomplete = () => resolve();
    tx.onabort = () => resolve();
    tx.onerror = () => resolve();
    const countRequest = store.count();
    countRequest.onsuccess = () => {
      const total = countRequest.result;
      if (total <= IDB_MAX) return;
      const toEvict = total - IDB_MAX;
      if (!store.indexNames.contains(STORED_AT_INDEX)) return;
      const index = store.index(STORED_AT_INDEX);
      const cursorRequest = index.openCursor();
      let evicted = 0;
      cursorRequest.onsuccess = () => {
        const cursor = cursorRequest.result;
        if (!cursor || evicted >= toEvict) return;
        cursor.delete();
        evicted += 1;
        cursor.continue();
      };
    };
  });
}

export async function readThumbnail(path: string): Promise<ThumbnailRecord | null> {
  const key = keyFor(path);
  if (useMemoryOnly || !hasIndexedDb()) return readMemory(key);
  const record = await withStore<ThumbnailRecord | undefined>("readonly", (store) => store.get(key));
  if (useMemoryOnly) return readMemory(key);
  if (record) bumpMemory(key, record);
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
  const keyed = paths.map((path) => ({ path, key: keyFor(path) }));
  if (useMemoryOnly || !hasIndexedDb()) {
    for (const { path, key } of keyed) out.set(path, readMemory(key));
    return out;
  }
  const db = await openDb();
  if (!db) {
    for (const { path, key } of keyed) out.set(path, readMemory(key));
    return out;
  }
  await new Promise<void>((resolve) => {
    let tx: IDBTransaction;
    try {
      tx = db.transaction(STORE, "readonly");
    } catch {
      fallToMemory();
      for (const { path, key } of keyed) out.set(path, readMemory(key));
      resolve();
      return;
    }
    const store = tx.objectStore(STORE);
    tx.oncomplete = () => resolve();
    tx.onabort = () => {
      fallToMemory();
      resolve();
    };
    tx.onerror = () => {
      fallToMemory();
      resolve();
    };
    for (const { path, key } of keyed) {
      const request = store.get(key);
      request.onsuccess = () => {
        const value = (request.result as ThumbnailRecord | undefined) ?? null;
        if (value) bumpMemory(key, value);
        out.set(path, value);
      };
      request.onerror = () => out.set(path, null);
    }
  });
  if (useMemoryOnly) {
    for (const { path, key } of keyed) {
      if (out.get(path) === null) out.set(path, readMemory(key));
    }
  }
  return out;
}

export async function writeThumbnail(record: ThumbnailRecord): Promise<void> {
  const key = keyFor(record.path);
  bumpMemory(key, record);
  if (useMemoryOnly || !hasIndexedDb()) return;
  await withStore("readwrite", (store) => store.put({ ...record, path: key }));
  await pruneIfDue(1);
}

export async function writeThumbnailBatch(records: ThumbnailRecord[]): Promise<void> {
  if (records.length === 0) return;
  for (const record of records) bumpMemory(keyFor(record.path), record);
  if (useMemoryOnly || !hasIndexedDb()) return;
  const db = await openDb();
  if (!db) return;
  await new Promise<void>((resolve) => {
    let tx: IDBTransaction;
    try {
      tx = db.transaction(STORE, "readwrite");
    } catch {
      fallToMemory();
      resolve();
      return;
    }
    const store = tx.objectStore(STORE);
    tx.oncomplete = () => resolve();
    tx.onabort = () => {
      fallToMemory();
      resolve();
    };
    tx.onerror = () => {
      fallToMemory();
      resolve();
    };
    for (const record of records) store.put({ ...record, path: keyFor(record.path) });
  });
  await pruneIfDue(records.length);
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
  const key = keyFor(path);
  memoryFallback.delete(key);
  if (useMemoryOnly || !hasIndexedDb()) return;
  await withStore("readwrite", (store) => store.delete(key));
}

export async function clearThumbnails(): Promise<void> {
  memoryFallback.clear();
  if (useMemoryOnly || !hasIndexedDb()) return;
  await withStore("readwrite", (store) => store.clear());
}

// Test hook — force a reopen and reset the memory-only latch. Never call
// from production code.
export function __resetThumbnailCacheForTests(): void {
  dbPromise = null;
  useMemoryOnly = false;
  writeCounterSincePrune = 0;
  namespace = "default";
  memoryFallback.clear();
}

// Test hook — expose whether the memory-only latch has flipped.
export function __thumbnailCacheIsMemoryOnlyForTests(): boolean {
  return useMemoryOnly;
}
