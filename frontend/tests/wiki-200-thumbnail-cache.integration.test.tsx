// @vitest-environment jsdom
// WIKI-200 — thumbnail cache round-trip + mtime invalidation.
//
// jsdom doesn't ship IndexedDB, so the module falls back to its
// in-memory Map. That's still the meaningful path to test: the mtime
// invalidation logic and the batch API contract don't depend on which
// storage backs them, and in-browser we'd also exercise this fallback
// on any surface where IndexedDB is disabled (private windows, corp
// policies, storage denied).

import { afterEach, describe, expect, test } from "vitest";

import {
  __resetThumbnailCacheForTests,
  deleteThumbnail,
  readThumbnail,
  readThumbnailBatch,
  readThumbnailIfFresh,
  writeThumbnail,
  writeThumbnailBatch,
  type ThumbnailRecord,
} from "../src/thumbnail-cache";

const NOW = 1_700_000_000_000;

function record(path: string, mtimeMs: number, previewBase64: string | null = "data:preview"): ThumbnailRecord {
  return {
    path,
    mtimeMs,
    width: 640,
    height: 480,
    previewBase64,
    storedAt: NOW,
  };
}

afterEach(() => {
  __resetThumbnailCacheForTests();
});

describe("thumbnail cache", () => {
  test("writeThumbnail / readThumbnail round-trips a record", async () => {
    await writeThumbnail(record("hero.png", 42));
    const got = await readThumbnail("hero.png");
    expect(got?.mtimeMs).toBe(42);
    expect(got?.previewBase64).toBe("data:preview");
  });

  test("readThumbnail returns null for an unknown path", async () => {
    const got = await readThumbnail("nope.png");
    expect(got).toBeNull();
  });

  test("readThumbnailIfFresh returns record when mtime matches", async () => {
    await writeThumbnail(record("hero.png", 42));
    const got = await readThumbnailIfFresh("hero.png", 42);
    expect(got?.mtimeMs).toBe(42);
  });

  test("readThumbnailIfFresh evicts a stale record on mtime mismatch", async () => {
    await writeThumbnail(record("hero.png", 42));
    const stale = await readThumbnailIfFresh("hero.png", 99);
    expect(stale).toBeNull();
    // Eviction is fire-and-forget; poll once to let the delete settle.
    await new Promise((resolve) => setTimeout(resolve, 0));
    const evicted = await readThumbnail("hero.png");
    expect(evicted).toBeNull();
  });

  test("readThumbnailIfFresh tolerates missing expected mtime and returns cached record", async () => {
    // No expected mtime = caller doesn't know it yet (server hasn't
    // answered). Cache should still serve the cached preview so the
    // blur-up placeholder paints on the first frame.
    await writeThumbnail(record("hero.png", 42));
    const got = await readThumbnailIfFresh("hero.png", null);
    expect(got?.mtimeMs).toBe(42);
  });

  test("writeThumbnailBatch + readThumbnailBatch cover every requested key", async () => {
    await writeThumbnailBatch([
      record("a.png", 1),
      record("b.png", 2),
      record("c.png", 3, null),
    ]);
    const got = await readThumbnailBatch(["a.png", "b.png", "c.png", "missing.png"]);
    expect(got.get("a.png")?.mtimeMs).toBe(1);
    expect(got.get("b.png")?.mtimeMs).toBe(2);
    expect(got.get("c.png")?.previewBase64).toBeNull();
    expect(got.get("missing.png")).toBeNull();
  });

  test("readThumbnailBatch with empty input returns an empty map without hitting storage", async () => {
    const got = await readThumbnailBatch([]);
    expect(got.size).toBe(0);
  });

  test("deleteThumbnail removes a single record", async () => {
    await writeThumbnail(record("hero.png", 42));
    await deleteThumbnail("hero.png");
    const got = await readThumbnail("hero.png");
    expect(got).toBeNull();
  });

  test("__resetThumbnailCacheForTests clears the memory fallback between tests", async () => {
    await writeThumbnail(record("hero.png", 42));
    __resetThumbnailCacheForTests();
    const got = await readThumbnail("hero.png");
    expect(got).toBeNull();
  });
});
