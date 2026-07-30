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
