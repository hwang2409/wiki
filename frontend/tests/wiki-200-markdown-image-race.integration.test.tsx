// @vitest-environment jsdom
// WIKI-200 U5 — stale IDB hydration must never clobber fresher network meta.
//
// Scenario: MarkdownImage mounts, fires both `readThumbnail(path)` (IDB)
// and `fetch(/api/vault/asset-meta/...)`. Network answers FIRST with a
// fresh preview (mtimeMs=100). Then the slow IDB read resolves with a
// stale record (mtimeMs=50). The rendered preview must remain the
// network's, not the stale IDB one.

import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { __resetAssetMetaForTests, MarkdownImage } from "../src/markdown-image";
import * as cacheModule from "../src/thumbnail-cache";

let resolveIdb: (record: cacheModule.ThumbnailRecord | null) => void;

beforeEach(() => {
  __resetAssetMetaForTests();
  cacheModule.__resetThumbnailCacheForTests();
  vi.spyOn(cacheModule, "readThumbnail").mockImplementation(
    () => new Promise((resolve) => {
      resolveIdb = resolve;
    }),
  );
  vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(
    JSON.stringify({
      width: 800,
      height: 600,
      preview_base64: "data:image/jpeg;base64,NETWORK-FRESH",
      mtime_ms: 100,
    }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  ));
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("MarkdownImage race — U5", () => {
  test("late stale IDB hydration does NOT overwrite the network preview", async () => {
    const { container } = render(<MarkdownImage src="hero.png" alt="hero" />);

    // Wait for the network fetch to resolve and paint the fresh preview.
    await waitFor(() => {
      const preview = container.querySelector<HTMLImageElement>(".markdown-image-preview");
      expect(preview?.src).toContain("NETWORK-FRESH");
    });

    // Now the slow IDB read finally returns a STALE record (older mtime).
    resolveIdb({
      path: "hero.png",
      mtimeMs: 50,
      width: 800,
      height: 600,
      previewBase64: "data:image/jpeg;base64,IDB-STALE",
      storedAt: 1_000_000,
    });

    // Give React a tick to flush any (incorrect) state update.
    await new Promise((r) => setTimeout(r, 50));

    // The preview must still be the network's.
    const preview = container.querySelector<HTMLImageElement>(".markdown-image-preview");
    expect(preview?.src).toContain("NETWORK-FRESH");
    expect(preview?.src).not.toContain("IDB-STALE");
  });

  test("transient HTTP failure does not poison the next mount", async () => {
    cleanup();
    cacheModule.__resetThumbnailCacheForTests();
    vi.mocked(cacheModule.readThumbnail).mockResolvedValue(null);
    const responses = [
      new Response("temporary", { status: 503 }),
      new Response("temporary", { status: 503 }),
      new Response("temporary", { status: 503 }),
      new Response(JSON.stringify({ width: 800, height: 600, preview_base64: "data:image/jpeg;base64,RECOVERED" }), { status: 200 }),
    ];
    vi.mocked(globalThis.fetch).mockImplementation(async () => responses.shift()!);

    const first = render(<MarkdownImage src="retry.png" alt="retry" />);
    await waitFor(() => expect(vi.mocked(globalThis.fetch)).toHaveBeenCalledTimes(3));
    first.unmount();

    const second = render(<MarkdownImage src="retry.png" alt="retry" />);
    await waitFor(() => {
      const preview = second.container.querySelector<HTMLImageElement>(".markdown-image-preview");
      expect(preview?.src).toContain("RECOVERED");
    });
    expect(vi.mocked(globalThis.fetch)).toHaveBeenCalledTimes(4);
  });

  test("network rejection removes stale hydrated metadata", async () => {
    cleanup();
    cacheModule.__resetThumbnailCacheForTests();
    vi.mocked(cacheModule.readThumbnail).mockResolvedValue({
      path: "stale.png",
      mtimeMs: 50,
      width: 800,
      height: 600,
      previewBase64: "data:image/jpeg;base64,IDB-STALE",
      storedAt: 1_000_000,
    });
    vi.mocked(globalThis.fetch).mockRejectedValue(new Error("offline"));

    const view = render(<MarkdownImage src="stale.png" alt="stale" />);
    await waitFor(() => expect(vi.mocked(globalThis.fetch)).toHaveBeenCalledTimes(3));
    await waitFor(() => {
      expect(view.container.querySelector(".markdown-image-preview")).toBeNull();
    });
    expect(cacheModule.readThumbnail).toHaveBeenCalledWith("stale.png");
  });
});
