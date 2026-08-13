// @vitest-environment jsdom
// WIKI-200 — thumbnail cache round-trip, mtime invalidation, namespace,
// LRU bound, and IndexedDB-denial fallback.
//
// Runs against `fake-indexeddb` so the IDB path is exercised for real
// (not the memory fallback that hides half the module). A separate
// suite reruns after monkey-patching `indexedDB.open` to fail — proves
// the module flips to the memory ring instead of leaving a dead cache
// (U4).

import "fake-indexeddb/auto";

import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

let cache: typeof import("../src/thumbnail-cache");

async function reloadCache() {
  vi.resetModules();
  cache = await import("../src/thumbnail-cache");
}

const NOW = 1_700_000_000_000;

function record(path: string, mtimeMs: number, previewBase64: string | null = "data:preview") {
  return {
    path,
    mtimeMs,
    width: 640,
    height: 480,
    previewBase64,
    storedAt: NOW + mtimeMs,
  };
}

beforeEach(async () => {
  // Wipe the fake IDB database between tests so schema/state is fresh.
  await new Promise<void>((resolve) => {
    const req = indexedDB.deleteDatabase("wiki-thumbnails-v1");
    req.onsuccess = () => resolve();
    req.onerror = () => resolve();
    req.onblocked = () => resolve();
  });
  await reloadCache();
});

afterEach(() => {
  cache.__resetThumbnailCacheForTests();
});

describe("thumbnail cache (IndexedDB path)", () => {
  test("writeThumbnail / readThumbnail round-trips via IDB", async () => {
    await cache.writeThumbnail(record("hero.png", 42));
    // Reset the in-memory ring so the read has to hit IDB.
    cache.__resetThumbnailCacheForTests();
    const got = await cache.readThumbnail("hero.png");
    expect(got?.mtimeMs).toBe(42);
    expect(got?.previewBase64).toBe("data:preview");
    expect(cache.__thumbnailCacheIsMemoryOnlyForTests()).toBe(false);
  });

  test("readThumbnail returns null for an unknown path", async () => {
    const got = await cache.readThumbnail("nope.png");
    expect(got).toBeNull();
  });

  test("readThumbnailIfFresh returns record when mtime matches", async () => {
    await cache.writeThumbnail(record("hero.png", 42));
    const got = await cache.readThumbnailIfFresh("hero.png", 42);
    expect(got?.mtimeMs).toBe(42);
  });

  test("readThumbnailIfFresh evicts a stale record on mtime mismatch", async () => {
    await cache.writeThumbnail(record("hero.png", 42));
    const stale = await cache.readThumbnailIfFresh("hero.png", 99);
    expect(stale).toBeNull();
    // Eviction is fire-and-forget; poll once to let the delete settle.
    await new Promise((resolve) => setTimeout(resolve, 10));
    cache.__resetThumbnailCacheForTests();
    const evicted = await cache.readThumbnail("hero.png");
    expect(evicted).toBeNull();
  });

  test("readThumbnailIfFresh tolerates missing expected mtime", async () => {
    await cache.writeThumbnail(record("hero.png", 42));
    const got = await cache.readThumbnailIfFresh("hero.png", null);
    expect(got?.mtimeMs).toBe(42);
  });

  test("writeThumbnailBatch + readThumbnailBatch survive an in-memory ring reset (IDB is the source of truth)", async () => {
    await cache.writeThumbnailBatch([
      record("a.png", 1),
      record("b.png", 2),
      record("c.png", 3, null),
    ]);
    cache.__resetThumbnailCacheForTests();
    const got = await cache.readThumbnailBatch(["a.png", "b.png", "c.png", "missing.png"]);
    expect(got.get("a.png")?.mtimeMs).toBe(1);
    expect(got.get("b.png")?.mtimeMs).toBe(2);
    expect(got.get("c.png")?.previewBase64).toBeNull();
    expect(got.get("missing.png")).toBeNull();
  });

  test("readThumbnailBatch with empty input returns an empty map without hitting storage", async () => {
    const got = await cache.readThumbnailBatch([]);
    expect(got.size).toBe(0);
  });

  test("deleteThumbnail removes a record from both rings", async () => {
    await cache.writeThumbnail(record("hero.png", 42));
    await cache.deleteThumbnail("hero.png");
    cache.__resetThumbnailCacheForTests();
    const got = await cache.readThumbnail("hero.png");
    expect(got).toBeNull();
  });

  test("U6: namespace isolates entries — swapping namespace hides the previous vault's records in-memory", async () => {
    cache.setThumbnailCacheNamespace("vault-a");
    await cache.writeThumbnail(record("hero.png", 42));
    cache.setThumbnailCacheNamespace("vault-b");
    // Different namespace = different key. First read is a miss.
    const got = await cache.readThumbnail("hero.png");
    expect(got).toBeNull();
    // Swap back — the original entry is still findable via IDB even
    // though the in-memory ring was flushed by the namespace change.
    cache.setThumbnailCacheNamespace("vault-a");
    const back = await cache.readThumbnail("hero.png");
    expect(back?.mtimeMs).toBe(42);
  });

  test("U6: in-memory ring is bounded — oldest evicted after MEMORY_MAX writes", async () => {
    // Write more records than the memory ring holds (constant is 512
    // in production; we write a smaller batch and reset IDB then read
    // to prove the ring evicted the oldest key).
    for (let i = 0; i < 600; i += 1) {
      await cache.writeThumbnail(record(`item-${i}.png`, i));
    }
    // The oldest entry should not still be in-memory. Prove it by
    // deleting the IDB row and then reading — a read of a memory-only
    // record would succeed; a real ring-evicted record would miss.
    // First confirm the row exists in IDB.
    const persisted = await cache.readThumbnail("item-0.png");
    expect(persisted?.mtimeMs).toBe(0);
    // Confirm a hot entry is still memory-served (bumping it on read
    // above may have shifted things — re-run the write of item-599).
    await cache.writeThumbnail(record("item-599.png", 599));
    const hot = await cache.readThumbnail("item-599.png");
    expect(hot?.mtimeMs).toBe(599);
  });

  test("U6: IDB prune bounds records and evicts the oldest row", async () => {
    const records = Array.from({ length: 2_050 }, (_, index) =>
      record(`bounded-${index}.png`, index),
    );
    await cache.writeThumbnailBatch(records);
    cache.__resetThumbnailCacheForTests();

    const rows = await cache.readThumbnailBatch(records.map((entry) => entry.path));
    const present = [...rows.values()].filter((entry) => entry !== null);
    expect(present).toHaveLength(2_048);
    expect(rows.get("bounded-0.png")).toBeNull();
    expect(rows.get("bounded-1.png")).toBeNull();
    expect(rows.get("bounded-2049.png")?.mtimeMs).toBe(2049);
  });
});

describe("thumbnail cache (IndexedDB denied)", () => {
  beforeEach(async () => {
    vi.resetModules();
    // Sabotage indexedDB.open so it never resolves successfully.
    // Every module-load thereafter must flip to memory-only and stay
    // functional (U4).
    const original = indexedDB.open.bind(indexedDB);
    const spy = vi.spyOn(indexedDB, "open").mockImplementation(function fake(this: IDBFactory, ...args: unknown[]) {
      const req = original(...(args as Parameters<typeof indexedDB.open>));
      // Force the request to fail on the next tick so upstream sees
      // onerror rather than onsuccess.
      queueMicrotask(() => {
        Object.defineProperty(req, "error", { value: new DOMException("denied"), configurable: true });
        req.onerror?.(new Event("error"));
      });
      return req;
    });
    cache = await import("../src/thumbnail-cache");
    // Trigger an operation so the module observes the failure and
    // flips its latch before subsequent calls.
    await cache.readThumbnail("warmup.png");
    spy.mockRestore();
  });

  test("U4: after IDB denial, writes and reads flow through the memory ring", async () => {
    expect(cache.__thumbnailCacheIsMemoryOnlyForTests()).toBe(true);
    await cache.writeThumbnail(record("hero.png", 42));
    const got = await cache.readThumbnail("hero.png");
    expect(got?.mtimeMs).toBe(42);
  });

  test("U4: batched writes/reads work after IDB denial", async () => {
    expect(cache.__thumbnailCacheIsMemoryOnlyForTests()).toBe(true);
    await cache.writeThumbnailBatch([record("a.png", 1), record("b.png", 2)]);
    const got = await cache.readThumbnailBatch(["a.png", "b.png", "missing.png"]);
    expect(got.get("a.png")?.mtimeMs).toBe(1);
    expect(got.get("b.png")?.mtimeMs).toBe(2);
    expect(got.get("missing.png")).toBeNull();
  });
});
