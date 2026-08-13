// @vitest-environment jsdom
// WIKI-200 — gallery virtualization + thumbnail-cache integration.
//
// jsdom has no layout engine, so IntersectionObserver is stubbed with a
// driver we can pulse from the test to simulate scroll enters AND exits.
// The gallery must:
//   - keep every tile a real <button> for a11y (U3),
//   - resolve replacement nodes across equal-count rerenders (U2),
//   - bound live-<img> count as tiles leave the visible band (U1),
//   - hydrate the blur-up preview from IDB before the network answers.

import { cleanup, render, waitFor, act } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import type { ArtifactFileEntry } from "../src/api";
import { ImageGallery, GALLERY_VIRTUALIZE_THRESHOLD } from "../src/artifact-detail/gallery";
import { __resetThumbnailCacheForTests, writeThumbnail } from "../src/thumbnail-cache";

type Callback = IntersectionObserverCallback;

interface Driver {
  observers: Set<ProgrammableObserver>;
  emit(target: Element, isIntersecting: boolean): void;
  observedElements(): Set<Element>;
}

class ProgrammableObserver implements IntersectionObserver {
  root = null;
  rootMargin = "";
  thresholds = [];
  private targets = new Set<Element>();
  constructor(private callback: Callback) {
    driver.observers.add(this);
  }
  observe(target: Element) {
    this.targets.add(target);
  }
  unobserve(target: Element) {
    this.targets.delete(target);
  }
  disconnect() {
    this.targets.clear();
    driver.observers.delete(this);
  }
  takeRecords() {
    return [];
  }
  fire(target: Element, isIntersecting: boolean) {
    if (!this.targets.has(target)) return;
    const entry = {
      target,
      isIntersecting,
      intersectionRatio: isIntersecting ? 1 : 0,
      time: 0,
      rootBounds: null,
      boundingClientRect: target.getBoundingClientRect(),
      intersectionRect: target.getBoundingClientRect(),
    } as IntersectionObserverEntry;
    this.callback([entry], this);
  }
  currentTargets(): ReadonlySet<Element> {
    return this.targets;
  }
}

const driver: Driver = {
  observers: new Set<ProgrammableObserver>(),
  emit(target, isIntersecting) {
    for (const observer of driver.observers) observer.fire(target, isIntersecting);
  },
  observedElements() {
    const merged = new Set<Element>();
    for (const observer of driver.observers) {
      for (const target of observer.currentTargets()) merged.add(target);
    }
    return merged;
  },
};

function files(count: number, prefix = "demo"): ArtifactFileEntry[] {
  return Array.from({ length: count }, (_, index) => ({
    path: `${prefix}/${index}.png`,
    label: `${prefix} ${index}`,
    status: null,
  }));
}

beforeEach(() => {
  driver.observers.clear();
  __resetThumbnailCacheForTests();
  vi.stubGlobal("IntersectionObserver", ProgrammableObserver);
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

  test(`virtualizes above ${GALLERY_VIRTUALIZE_THRESHOLD} tiles — placeholders are still buttons; observer watches every tile`, () => {
    const total = 60;
    const list = files(total);
    const { container } = render(<ImageGallery files={list} />);
    const grid = container.querySelector<HTMLDivElement>(".artifact-gallery");
    expect(grid?.dataset.artifactGalleryVirtualized).toBe("true");

    // U3: every tile is a <button> — off-screen tiles remain tabbable
    // even though their <img> is not mounted.
    const buttons = container.querySelectorAll<HTMLButtonElement>("button.artifact-gallery-tile");
    expect(buttons.length).toBe(total);

    const liveImgs = container.querySelectorAll<HTMLImageElement>(".artifact-gallery-image");
    expect(liveImgs.length).toBeGreaterThan(0);
    expect(liveImgs.length).toBeLessThan(total);

    const placeholderButtons = container.querySelectorAll<HTMLButtonElement>("button.artifact-gallery-tile.is-virtualized");
    expect(placeholderButtons.length).toBe(total - liveImgs.length);

    // U1/U2: EVERY tile (including live seed tiles) is observed so
    // exits can free the <img> when they scroll away.
    expect(driver.observedElements().size).toBe(total);
  });

  test("U1: tiles that scroll out of the observer band unmount their <img>", async () => {
    const total = 60;
    const list = files(total);
    const { container } = render(<ImageGallery files={list} />);
    const initialLive = container.querySelectorAll(".artifact-gallery-image").length;
    expect(initialLive).toBeGreaterThan(0);

    // First: enter a batch of tiles below the seed band.
    const cells = Array.from(container.querySelectorAll<HTMLLIElement>(".artifact-gallery-cell"));
    const seed = cells.slice(0, initialLive);
    const bottom = cells.slice(initialLive, initialLive + 20);
    act(() => {
      for (const cell of bottom) driver.emit(cell, true);
    });
    await waitFor(() => {
      const live = container.querySelectorAll(".artifact-gallery-image").length;
      expect(live).toBe(initialLive + bottom.length);
    });

    // Then: fire exits on the seed band as if the user scrolled past it.
    act(() => {
      for (const cell of seed) driver.emit(cell, false);
    });
    await waitFor(() => {
      const live = container.querySelectorAll(".artifact-gallery-image").length;
      expect(live).toBe(bottom.length);
    });

    // Mounted count stays bounded by the visible band — never grows to
    // total after full scroll traversal.
    expect(container.querySelectorAll(".artifact-gallery-image").length).toBeLessThan(total);
  });

  test("U2: equal-count rerenders reuse the observer and still resolve new cells", async () => {
    const total = 60;
    const first = files(total, "first");
    const { container, rerender } = render(<ImageGallery files={first} />);
    expect(driver.observedElements().size).toBe(total);

    // Switch to a fresh set of the SAME length. The observer instance
    // is reused (totalTiles unchanged), but ref callbacks fire for each
    // replaced <li> so indexByNode must repopulate.
    const second = files(total, "second");
    rerender(<ImageGallery files={second} />);
    expect(driver.observedElements().size).toBe(total);

    const cells = Array.from(container.querySelectorAll<HTMLLIElement>(".artifact-gallery-cell"));
    // Fire an intersection on a cell that started off-screen. Before
    // the fix, the observer's index map was stale and this was a no-op.
    const target = cells[total - 1];
    const beforeLive = container.querySelectorAll(".artifact-gallery-image").length;
    act(() => driver.emit(target, true));
    await waitFor(() => {
      expect(container.querySelectorAll(".artifact-gallery-image").length).toBe(beforeLive + 1);
    });
  });

  test("cached thumbnail hydrates the tile with a blur-up preview before the network answers", async () => {
    await writeThumbnail({
      path: "demo/0.png",
      mtimeMs: 42,
      width: 800,
      height: 600,
      previewBase64: "data:image/jpeg;base64,PREVIEW",
      storedAt: 1_700_000_000_000,
    });
    // Never resolve the batch fetch — proves the preview came from the
    // cache, not the network.
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
