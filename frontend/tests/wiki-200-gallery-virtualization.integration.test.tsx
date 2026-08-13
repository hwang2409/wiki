// @vitest-environment jsdom
// WIKI-200 — gallery virtualization + thumbnail-cache integration.
//
// The gallery must window its DOM once tile count > 20, so a
// 200-file dump doesn't mount 200 <img> tags on the initial frame.
// jsdom has no real layout, so we stub IntersectionObserver to expose
// the observed set (all off-screen tiles start unobserved-but-mounted
// as placeholders; the seed band of visible tiles is what we count).

import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import type { ArtifactFileEntry } from "../src/api";
import { ImageGallery, GALLERY_VIRTUALIZE_THRESHOLD } from "../src/artifact-detail/gallery";
import { __resetThumbnailCacheForTests, writeThumbnail } from "../src/thumbnail-cache";

const OBSERVED = new Set<Element>();

class StubIntersectionObserver implements IntersectionObserver {
  root = null;
  rootMargin = "";
  thresholds = [];
  constructor(_callback: IntersectionObserverCallback, _init?: IntersectionObserverInit) {}
  observe(target: Element) {
    OBSERVED.add(target);
  }
  unobserve(target: Element) {
    OBSERVED.delete(target);
  }
  disconnect() {
    OBSERVED.clear();
  }
  takeRecords() {
    return [];
  }
}

function files(count: number): ArtifactFileEntry[] {
  return Array.from({ length: count }, (_, index) => ({
    path: `demo/${index}.png`,
    label: `demo ${index}`,
    status: null,
  }));
}

beforeEach(() => {
  OBSERVED.clear();
  __resetThumbnailCacheForTests();
  vi.stubGlobal("IntersectionObserver", StubIntersectionObserver);
  vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response("{}", {
    status: 200,
    headers: { "Content-Type": "application/json" },
  }));
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("ImageGallery virtualization", () => {
  test("does not virtualize small galleries — every tile is a live button", () => {
    const list = files(10);
    const { container } = render(<ImageGallery files={list} />);
    const tiles = container.querySelectorAll<HTMLButtonElement>(".artifact-gallery-tile:not(.is-virtualized)");
    expect(tiles.length).toBe(10);
    const grid = container.querySelector<HTMLDivElement>(".artifact-gallery");
    expect(grid?.dataset.artifactGalleryVirtualized).toBeUndefined();
  });

  test(`virtualizes above ${GALLERY_VIRTUALIZE_THRESHOLD} tiles — off-screen tiles are placeholders, live tiles get real images`, () => {
    const total = 60;
    const list = files(total);
    const { container } = render(<ImageGallery files={list} />);
    const grid = container.querySelector<HTMLDivElement>(".artifact-gallery");
    expect(grid?.dataset.artifactGalleryVirtualized).toBe("true");

    const cells = container.querySelectorAll<HTMLLIElement>(".artifact-gallery-cell");
    expect(cells.length).toBe(total);

    const liveTiles = container.querySelectorAll<HTMLButtonElement>(".artifact-gallery-tile:not(.is-virtualized)");
    const placeholderTiles = container.querySelectorAll<HTMLDivElement>(".artifact-gallery-tile.is-virtualized");

    // Seed band mounts real buttons; the rest render as sized
    // placeholders. Numbers are asymmetric on purpose — asserting an
    // exact count here would couple this test to the seed constant,
    // whereas the guarantee we care about is "way fewer live tiles
    // than total files".
    expect(liveTiles.length).toBeGreaterThan(0);
    expect(liveTiles.length).toBeLessThan(total);
    expect(placeholderTiles.length).toBeGreaterThan(0);
    expect(liveTiles.length + placeholderTiles.length).toBe(total);
    expect(OBSERVED.size).toBe(placeholderTiles.length);
  });

  test("cached thumbnail hydrates the tile with a blur-up preview before the network answers", async () => {
    // Warm the IDB fallback with a cached preview. When the gallery
    // mounts, the tile must render <img class="artifact-gallery-preview">
    // sourced from that cached base64 without waiting for the batch
    // POST to /api/vault/asset-meta to resolve.
    await writeThumbnail({
      path: "demo/0.png",
      mtimeMs: 42,
      width: 800,
      height: 600,
      previewBase64: "data:image/jpeg;base64,PREVIEW",
      storedAt: 1_700_000_000_000,
    });
    // Never resolve the batch fetch — that proves the preview came from
    // the cache, not the network.
    vi.spyOn(globalThis, "fetch").mockImplementation(
      () => new Promise(() => undefined),
    );
    const { container } = render(<ImageGallery files={files(1)} />);
    await waitFor(() => {
      const preview = container.querySelector<HTMLImageElement>(".artifact-gallery-preview");
      expect(preview?.src).toContain("PREVIEW");
    });
  });
});
