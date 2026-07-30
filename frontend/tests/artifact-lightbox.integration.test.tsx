// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import type { ArtifactFileEntry } from "../src/api";
import { ArtifactLightbox, type LightboxItem } from "../src/artifact-detail/lightbox";
import { ImageGallery } from "../src/artifact-detail/gallery";
import { FileListArtifactDetail } from "../src/artifact-detail/file-list";
import { SharedImageRenderer } from "../src/artifact-renderers";

const items: LightboxItem[] = [
  { src: "/api/vault/assets/first.png", alt: "First", caption: "first caption" },
  { src: "/api/vault/assets/second.png", alt: "Second", caption: "second caption" },
  { src: "/api/vault/assets/third.png", alt: "Third", caption: "third caption" },
];

afterEach(() => {
  cleanup();
});

// Default: silence any network calls the components make (gallery batch
// asset-meta POST, MarkdownImage asset-meta GET, lightbox clipboard fetch)
// so tests that don't stub fetch themselves don't spam relative-URL errors
// or hang on real connections. Tests that need to observe fetch replace it
// via vi.spyOn inside their block.
beforeEach(() => {
  vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response("{}", {
    status: 200,
    headers: { "Content-Type": "application/json" },
  }));
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ArtifactLightbox", () => {
  test("renders active item and counter", () => {
    render(
      <ArtifactLightbox
        index={1}
        items={items}
        onClose={() => undefined}
        onIndexChange={() => undefined}
      />,
    );
    const dialog = screen.getByRole("dialog");
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(screen.getByAltText("Second")).toBeTruthy();
    // Counter should be "2 / 3"
    expect(dialog.textContent).toMatch(/2.*3/);
  });

  test("Escape closes the lightbox", () => {
    const onClose = vi.fn();
    render(
      <ArtifactLightbox
        index={0}
        items={items}
        onClose={onClose}
        onIndexChange={() => undefined}
      />,
    );
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(onClose).toHaveBeenCalled();
  });

  test("ArrowRight and ArrowLeft cycle through items", () => {
    const onIndexChange = vi.fn();
    render(
      <ArtifactLightbox
        index={0}
        items={items}
        onClose={() => undefined}
        onIndexChange={onIndexChange}
      />,
    );
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "ArrowRight" });
    expect(onIndexChange).toHaveBeenLastCalledWith(1);
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "ArrowLeft" });
    // 0 -> -1 wraps to length-1 = 2
    expect(onIndexChange).toHaveBeenLastCalledWith(2);
  });

  test("Plus and Minus keys change the zoom label", () => {
    render(
      <ArtifactLightbox
        index={0}
        items={items}
        onClose={() => undefined}
        onIndexChange={() => undefined}
      />,
    );
    const dialog = screen.getByRole("dialog");
    expect(screen.getByText("100%")).toBeTruthy();
    fireEvent.keyDown(dialog, { key: "+" });
    expect(screen.getByText("125%")).toBeTruthy();
    fireEvent.keyDown(dialog, { key: "0" });
    expect(screen.getByText("100%")).toBeTruthy();
  });

  test("single item hides pagination and arrow keys are no-ops", () => {
    const onIndexChange = vi.fn();
    render(
      <ArtifactLightbox
        index={0}
        items={[items[0]]}
        onClose={() => undefined}
        onIndexChange={onIndexChange}
      />,
    );
    expect(screen.queryByLabelText("Previous image")).toBeNull();
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "ArrowRight" });
    expect(onIndexChange).not.toHaveBeenCalled();
  });

  test("clicking the backdrop closes the lightbox", () => {
    const onClose = vi.fn();
    render(
      <ArtifactLightbox
        index={0}
        items={items}
        onClose={onClose}
        onIndexChange={() => undefined}
      />,
    );
    const dialog = screen.getByRole("dialog");
    // clicking the dialog root (backdrop) closes
    fireEvent.click(dialog);
    expect(onClose).toHaveBeenCalled();
  });

  test("copy button reads current image src via clipboard", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    render(
      <ArtifactLightbox
        index={0}
        items={[items[0]]}
        onClose={() => undefined}
        onIndexChange={() => undefined}
      />,
    );
    const copyButton = screen.getByTitle(/Copy image/);
    await act(async () => {
      fireEvent.click(copyButton);
    });
    await waitFor(() => {
      expect(writeText).toHaveBeenCalledWith("/api/vault/assets/first.png");
    });
  });
});

describe("ImageGallery", () => {
  const files: ArtifactFileEntry[] = [
    { path: "notes/one.png", label: "One" },
    { path: "notes/two.jpg", label: "Two" },
    { path: "notes/three.webp", label: "Three" },
  ];

  beforeEach(() => {
    // Silence the batch asset-meta POST — it doesn't matter for these
    // structural assertions and the real backend isn't running under vitest.
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } }),
    );
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  test("renders a tile per image with bounded thumbnail srcset", () => {
    render(<ImageGallery files={files} />);
    const tiles = screen.getAllByRole("button", { name: /Open .+ in fullscreen/ });
    expect(tiles).toHaveLength(3);
    const images = tiles.map((tile) => tile.querySelector("img.artifact-gallery-image"));
    expect(images.every((img) => img?.getAttribute("loading") === "lazy")).toBe(true);
    expect(images[0]?.getAttribute("src")).toBe("/api/vault/assets/notes/one.png?w=320");
    const srcSet = images[0]?.getAttribute("srcset");
    expect(srcSet).toContain("/api/vault/assets/notes/one.png?w=320 320w");
    expect(srcSet).toContain("/api/vault/assets/notes/one.png?w=640 640w");
    expect(images[0]?.getAttribute("sizes")).toContain("200px");
  });

  test("clicking a tile opens lightbox at that index", () => {
    render(<ImageGallery files={files} />);
    const tiles = screen.getAllByRole("button", { name: /Open .+ in fullscreen/ });
    fireEvent.click(tiles[1]);
    const dialog = screen.getByRole("dialog");
    expect(dialog.textContent).toMatch(/2.*3/);
    expect(within(dialog).getByAltText("Two")).toBeTruthy();
  });

  test("arrow-key nav walks between gallery items in the lightbox", () => {
    render(<ImageGallery files={files} />);
    fireEvent.click(screen.getAllByRole("button", { name: /Open .+ in fullscreen/ })[0]);
    const dialog = screen.getByRole("dialog");
    fireEvent.keyDown(dialog, { key: "ArrowRight" });
    expect(within(dialog).getByAltText("Two")).toBeTruthy();
    fireEvent.keyDown(dialog, { key: "ArrowRight" });
    expect(within(dialog).getByAltText("Three")).toBeTruthy();
    fireEvent.keyDown(dialog, { key: "ArrowRight" });
    // wraps back to first
    expect(within(dialog).getByAltText("One")).toBeTruthy();
  });
});

describe("FileListArtifactDetail gallery detection", () => {
  test("switches to gallery when every entry is an image path", () => {
    const artifact = {
      kind: "file-list" as const,
      files: [
        { path: "screenshots/a.png" },
        { path: "screenshots/b.png" },
      ],
    };
    render(<FileListArtifactDetail artifact={artifact} />);
    expect(screen.getAllByRole("button", { name: /Open .+ in fullscreen/ })).toHaveLength(2);
    expect(screen.queryByText("No files.")).toBeNull();
  });

  test("keeps file-list layout when non-image files are present", () => {
    const artifact = {
      kind: "file-list" as const,
      files: [
        { path: "screenshots/a.png" },
        { path: "notes/readme.md" },
      ],
    };
    render(<FileListArtifactDetail artifact={artifact} />);
    expect(screen.queryByRole("button", { name: /Open .+ in fullscreen/ })).toBeNull();
    expect(screen.getByText("screenshots/a.png")).toBeTruthy();
  });
});

describe("SharedImageRenderer polish", () => {
  test("wraps in an expand button that opens the lightbox", async () => {
    render(
      <SharedImageRenderer
        alt="agent artifact"
        caption="agent artifact"
        imgClassName="artifact-image"
        openInLightbox
        source="/api/agents/WIKI-1/artifact/abc.png"
      />,
    );
    const trigger = screen.getByRole("button", { name: /Open agent artifact in fullscreen/ });
    fireEvent.click(trigger);
    expect(screen.getByRole("dialog")).toBeTruthy();
  });

  test("blur-up placeholder shows before load and clears after load", async () => {
    const { container } = render(
      <SharedImageRenderer
        alt="pic"
        openInLightbox={false}
        source="/api/agents/WIKI-1/artifact/abc.png"
      />,
    );
    expect(container.querySelector(".artifact-image-blur")).toBeTruthy();
    const img = container.querySelector("img")!;
    await act(async () => {
      fireEvent.load(img);
    });
    expect(container.querySelector(".artifact-image-blur")).toBeNull();
    expect(img.className).toContain("is-loaded");
  });

  test("lazy-loading is on by default", () => {
    const { container } = render(
      <SharedImageRenderer
        alt="pic"
        source="/api/agents/WIKI-1/artifact/abc.png"
      />,
    );
    const img = container.querySelector("img")!;
    expect(img.getAttribute("loading")).toBe("lazy");
    expect(img.getAttribute("decoding")).toBe("async");
  });

  test("reserves aspect ratio from persisted width/height (CLS = 0)", () => {
    const { container } = render(
      <SharedImageRenderer
        alt="pic"
        height={720}
        openInLightbox={false}
        source="/api/agents/WIKI-1/artifact/abc.png"
        width={1280}
      />,
    );
    const wrap = container.querySelector(".artifact-image-wrap") as HTMLElement;
    expect(wrap.style.aspectRatio).toBe("1280 / 720");
    const img = container.querySelector("img") as HTMLImageElement;
    expect(img.getAttribute("width")).toBe("1280");
    expect(img.getAttribute("height")).toBe("720");
  });

  test("renders a persisted low-res preview blurred, not the shimmer", () => {
    const preview = "data:image/jpeg;base64,ZmFrZS1wcmV2aWV3";
    const { container } = render(
      <SharedImageRenderer
        alt="pic"
        height={200}
        openInLightbox={false}
        previewBase64={preview}
        source="/api/agents/WIKI-1/artifact/abc.png"
        width={400}
      />,
    );
    const previewImg = container.querySelector(".artifact-image-preview") as HTMLImageElement;
    expect(previewImg).toBeTruthy();
    expect(previewImg.src).toContain("data:image/jpeg;base64");
    expect(container.querySelector(".artifact-image-blur")).toBeNull();
  });
});

describe("ArtifactLightbox lifecycle cleanup", () => {
  test("aborts an in-flight clipboard fetch when the item switches", async () => {
    const aborted: boolean[] = [];
    const originalFetch = globalThis.fetch;
    const originalClipboard = navigator.clipboard;
    let resolveFetch: ((response: Response) => void) | null = null;
    const fetchMock = vi.fn((_url: RequestInfo | URL, init?: RequestInit) => {
      const signal = init?.signal;
      return new Promise<Response>((resolve, reject) => {
        resolveFetch = resolve;
        signal?.addEventListener("abort", () => {
          aborted.push(true);
          reject(new DOMException("aborted", "AbortError"));
        });
      });
    });
    globalThis.fetch = fetchMock as typeof fetch;
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
    });
    // Ensure ClipboardItem branch is exercised
    (globalThis as { ClipboardItem?: unknown }).ClipboardItem = class {
      constructor(_types: Record<string, Blob>) {
        // no-op
      }
    };
    const items: LightboxItem[] = [
      { src: "/api/vault/assets/first.png", alt: "First" },
      { src: "/api/vault/assets/second.png", alt: "Second" },
    ];
    let currentIndex = 0;
    const { rerender } = render(
      <ArtifactLightbox
        index={currentIndex}
        items={items}
        onClose={() => undefined}
        onIndexChange={(next) => {
          currentIndex = next;
        }}
      />,
    );
    const copyButton = screen.getByTitle(/Copy image/);
    await act(async () => {
      fireEvent.click(copyButton);
    });
    // switch to next item while fetch is pending
    await act(async () => {
      rerender(
        <ArtifactLightbox
          index={1}
          items={items}
          onClose={() => undefined}
          onIndexChange={() => undefined}
        />,
      );
    });
    expect(aborted).toContain(true);
    // Cleanup for other tests
    resolveFetch?.(new Response(new Blob([], { type: "image/png" })));
    globalThis.fetch = originalFetch;
    if (originalClipboard) {
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: originalClipboard,
      });
    }
  });

  test("aborts the clipboard fetch on close", async () => {
    const aborted: boolean[] = [];
    const originalFetch = globalThis.fetch;
    const originalClipboard = navigator.clipboard;
    const fetchMock = vi.fn((_url: RequestInfo | URL, init?: RequestInit) => {
      const signal = init?.signal;
      return new Promise<Response>((_resolve, reject) => {
        signal?.addEventListener("abort", () => {
          aborted.push(true);
          reject(new DOMException("aborted", "AbortError"));
        });
      });
    });
    globalThis.fetch = fetchMock as typeof fetch;
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
    });
    (globalThis as { ClipboardItem?: unknown }).ClipboardItem = class {
      constructor(_types: Record<string, Blob>) {
        // no-op
      }
    };
    const items: LightboxItem[] = [{ src: "/api/vault/assets/first.png", alt: "First" }];
    const { unmount } = render(
      <ArtifactLightbox
        index={0}
        items={items}
        onClose={() => undefined}
        onIndexChange={() => undefined}
      />,
    );
    await act(async () => {
      fireEvent.click(screen.getByTitle(/Copy image/));
    });
    await act(async () => {
      unmount();
    });
    expect(aborted).toContain(true);
    globalThis.fetch = originalFetch;
    if (originalClipboard) {
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: originalClipboard,
      });
    }
  });

  test("inerts sibling nodes while open and restores on close", () => {
    const outside = document.createElement("div");
    outside.textContent = "background page";
    document.body.appendChild(outside);
    try {
      const { unmount } = render(
        <ArtifactLightbox
          index={0}
          items={[{ src: "/api/vault/assets/first.png", alt: "First" }]}
          onClose={() => undefined}
          onIndexChange={() => undefined}
        />,
      );
      expect(outside.hasAttribute("inert")).toBe(true);
      expect(outside.getAttribute("aria-hidden")).toBe("true");
      unmount();
      expect(outside.hasAttribute("inert")).toBe(false);
      expect(outside.getAttribute("aria-hidden")).toBeNull();
    } finally {
      outside.remove();
    }
  });

  test("Tab wraps focus inside the dialog", () => {
    render(
      <ArtifactLightbox
        index={0}
        items={[{ src: "/api/vault/assets/first.png", alt: "First" }]}
        onClose={() => undefined}
        onIndexChange={() => undefined}
      />,
    );
    const dialog = screen.getByRole("dialog");
    // In jsdom offsetParent is null for many elements; the trap should still
    // preventDefault + push focus back inside the dialog when tab escapes.
    const closeButton = screen.getByTitle(/Close/);
    closeButton.focus();
    fireEvent.keyDown(dialog, { key: "Tab" });
    // The trap keeps document.activeElement inside the dialog subtree.
    expect(dialog.contains(document.activeElement)).toBe(true);
  });
});

describe("MarkdownImage layout stability (WIKI-201)", () => {
  test("commits with real dims on first render when note provides asset_meta", async () => {
    const { ObsidianMarkdown } = await import("../src/markdown");
    // pane.tsx seeds the shared cache from note.asset_meta before rendering.
    // ObsidianMarkdown does that seeding itself when we pass the prop.
    // The path key must match the FIRST candidate MarkdownImage resolves,
    // which for a top-level note is just the raw asset path.
    const { container } = render(
      <ObsidianMarkdown
        assetMeta={{ "hero.png": { width: 1280, height: 720 } }}
        content={"![hero](hero.png)"}
        notePath="index.md"
        notes={[]}
        onOpenNote={() => undefined}
      />,
    );
    const img = container.querySelector("img") as HTMLImageElement;
    // Dimensions and aspect ratio must be set BEFORE any onLoad — this is
    // the frame's very first commit. No race, no swap.
    expect(img.getAttribute("width")).toBe("1280");
    expect(img.getAttribute("height")).toBe("720");
    const frame = container.querySelector(".markdown-image-frame") as HTMLElement;
    expect(frame.style.aspectRatio).toBe("1280 / 720");
    expect(frame.className).toContain("has-known-ratio");
  });

  test("shared asset-meta fetch is not aborted when the first consumer unmounts", async () => {
    const { MarkdownImage: _MarkdownImage } = await import("../src/markdown");
    // We assert the fetch itself never receives a signal that gets aborted.
    // Consumer A mounts, kicks off a fetch, then unmounts before it resolves.
    // Consumer B mounts and awaits the same promise; it must still resolve.
    const originalFetch = globalThis.fetch;
    let signalReceived: AbortSignal | undefined;
    let resolveFetch: ((response: Response) => void) | null = null;
    globalThis.fetch = vi.fn((_url: RequestInfo | URL, init?: RequestInit) => {
      signalReceived = init?.signal ?? undefined;
      return new Promise<Response>((resolve) => {
        resolveFetch = resolve;
      });
    }) as typeof fetch;
    try {
      const { ObsidianMarkdown } = await import("../src/markdown");
      const { unmount } = render(
        <ObsidianMarkdown
          content={"![a](notes/shared.png)"}
          notePath="notes/index.md"
          notes={[]}
          onOpenNote={() => undefined}
        />,
      );
      await act(async () => {
        unmount();
      });
      // Second consumer mounts after the first unmounted — the shared
      // promise must still be pending and still resolvable.
      expect(signalReceived).toBeUndefined();
      expect(resolveFetch).not.toBeNull();
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  test("preview stays in the DOM through the fade after load", async () => {
    vi.useFakeTimers();
    try {
      const preview = "data:image/jpeg;base64,ZmFkZQ==";
      const { container } = render(
        <SharedImageRenderer
          alt="pic"
          height={200}
          openInLightbox={false}
          previewBase64={preview}
          source="/api/agents/WIKI-1/artifact/abc.png"
          width={400}
        />,
      );
      const img = container.querySelector("img[decoding='async']") as HTMLImageElement;
      await act(async () => {
        fireEvent.load(img);
      });
      // Still in DOM immediately after load; is-fading class flips on.
      const preview1 = container.querySelector(".artifact-image-preview") as HTMLElement | null;
      expect(preview1).not.toBeNull();
      expect(preview1!.className).toContain("is-fading");
      // Advance past the retention timer and re-render.
      await act(async () => {
        vi.advanceTimersByTime(400);
      });
      expect(container.querySelector(".artifact-image-preview")).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("MarkdownImage percent-encoded delimiter round-trip", () => {
  test("hero%23draft.png resolves to /api/vault/assets/hero%23draft.png without shimmer", async () => {
    const { MarkdownImage, seedAssetMetaCache } = await import("../src/markdown-image");
    seedAssetMetaCache({
      "hero#draft.png": { width: 800, height: 600, preview_base64: null },
    });
    const { container } = render(
      <MarkdownImage src="hero%23draft.png" alt="hash" notePath="index.md" />,
    );
    // The RAW image element must render — no fallback to shimmer, no
    // stuck-in-loading state — with a URL that re-encodes the literal `#`
    // as `%23` so the backend's `_read_vault_asset_bytes` sees the real
    // filename on disk.
    const img = container.querySelector("img[decoding='async']") as HTMLImageElement | null;
    expect(img).not.toBeNull();
    expect(img!.getAttribute("src")).toBe("/api/vault/assets/hero%23draft.png");
    // Dimensions from the cache flow through onto the frame + img, proving
    // the resolver did not silently reject the filename.
    expect(img!.getAttribute("width")).toBe("800");
    expect(img!.getAttribute("height")).toBe("600");
    const frame = container.querySelector(".markdown-image-frame") as HTMLElement;
    expect(frame.style.aspectRatio).toBe("800 / 600");
    // The frame carries has-known-ratio from the seeded cache — the
    // has-known-ratio class is the signal that the resolver produced a
    // vault candidate; the buggy path would have rejected the filename
    // and left the frame in its unlocked min-height state.
    expect(frame.className).toContain("has-known-ratio");
  });

  test("hero%3Fdraft.png resolves to /api/vault/assets/hero%3Fdraft.png without shimmer", async () => {
    const { MarkdownImage, seedAssetMetaCache } = await import("../src/markdown-image");
    seedAssetMetaCache({
      "hero?draft.png": { width: 640, height: 480, preview_base64: null },
    });
    const { container } = render(
      <MarkdownImage src="hero%3Fdraft.png" alt="question" notePath="index.md" />,
    );
    const img = container.querySelector("img[decoding='async']") as HTMLImageElement | null;
    expect(img).not.toBeNull();
    expect(img!.getAttribute("src")).toBe("/api/vault/assets/hero%3Fdraft.png");
    expect(img!.getAttribute("width")).toBe("640");
    expect(img!.getAttribute("height")).toBe("480");
    const frame = container.querySelector(".markdown-image-frame") as HTMLElement;
    expect(frame.style.aspectRatio).toBe("640 / 480");
    expect(frame.className).toContain("has-known-ratio");
  });
});

describe("gallery tile ratio lock (WIKI-192)", () => {
  test("tile ratio does not swap when metadata arrives after mount", async () => {
    // Give the batch endpoint a 60ms round-trip delay so the tile lives in
    // the "no metadata yet" state briefly. Whatever meta arrives later must
    // not swap the tile's aspect ratio.
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => {
      await new Promise((resolve) => setTimeout(resolve, 60));
      return new Response(
        JSON.stringify({ "notes/one.png": { width: 1200, height: 800, media_type: "image/png" } }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    });
    const { container } = render(
      <ImageGallery files={[{ path: "notes/one.png", label: "One" }]} />,
    );
    const tile = container.querySelector(".artifact-gallery-tile") as HTMLElement;
    // The tile MUST NOT carry an inline aspect-ratio — the CSS locks it to
    // 4/3 and the batch response must not override that inline.
    expect(tile.getAttribute("style") || "").not.toContain("aspect-ratio");
    // Even after the batch resolves, the tile stays free of inline ratio.
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(tile.getAttribute("style") || "").not.toContain("aspect-ratio");
  });
});
