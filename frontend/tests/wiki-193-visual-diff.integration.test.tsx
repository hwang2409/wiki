// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, test, vi } from "vitest";

import type { SessionEvent } from "../src/api";
import { ArtifactBlock } from "../src/artifact-block";
import { ArtifactPanel } from "../src/artifact-panel";
import { VisualDiffRenderer } from "../src/visual-diff-renderer";

// Every ImmediateImage src is tagged so the mock canvas can hand back the
// distinct pixel buffer that corresponds to that variant.
const IMAGE_PIXELS = new WeakMap<object, Uint8ClampedArray>();

function solidPixels(width: number, height: number, rgba: [number, number, number, number]): Uint8ClampedArray {
  const buffer = new Uint8ClampedArray(width * height * 4);
  for (let i = 0; i < buffer.length; i += 4) {
    buffer[i] = rgba[0];
    buffer[i + 1] = rgba[1];
    buffer[i + 2] = rgba[2];
    buffer[i + 3] = rgba[3];
  }
  return buffer;
}

const BEFORE_PIXELS = solidPixels(12, 8, [0, 0, 0, 255]);
// Every after-pixel changes clearly (bright red) so computePixelDiff paints
// the whole overlay — asserting that pixel data really flowed through the
// draw / getImageData / diff / putImageData pipeline in jsdom.
const AFTER_PIXELS = solidPixels(12, 8, [255, 0, 0, 255]);

class ImmediateImage {
  onload: (() => void) | null = null;
  onerror: ((error: unknown) => void) | null = null;
  crossOrigin: string | null = null;
  decoding: string | null = null;
  naturalWidth = 12;
  naturalHeight = 8;
  #src = "";
  get src(): string {
    return this.#src;
  }
  set src(value: string) {
    this.#src = value;
    if (value.includes("variant=before")) IMAGE_PIXELS.set(this, BEFORE_PIXELS);
    else if (value.includes("variant=after")) IMAGE_PIXELS.set(this, AFTER_PIXELS);
    queueMicrotask(() => this.onload?.());
  }
}

type PutRecord = { width: number; height: number; alphaCount: number };

const PUT_CALLS: PutRecord[] = [];

function installCanvasMock() {
  HTMLCanvasElement.prototype.getContext = function (kind: string) {
    if (kind !== "2d") return null;
    let currentImage: object | null = null;
    return {
      drawImage(image: object, _dx: number, _dy: number, _dw?: number, _dh?: number) {
        currentImage = image;
      },
      getImageData(_x: number, _y: number, width: number, height: number) {
        const src = currentImage && IMAGE_PIXELS.get(currentImage);
        const data = new Uint8ClampedArray(width * height * 4);
        if (src) {
          // ImmediateImage claims 12x8 for both variants, matching the
          // pre-baked buffers. Any getImageData at that size returns them
          // verbatim; anything else returns zeros (never exercised here).
          if (src.length === data.length) data.set(src);
        }
        return { data, width, height, colorSpace: "srgb" as const };
      },
      createImageData(width: number, height: number) {
        return { data: new Uint8ClampedArray(width * height * 4), width, height, colorSpace: "srgb" as const };
      },
      putImageData(imageData: { data: Uint8ClampedArray; width: number; height: number }) {
        let alphaCount = 0;
        for (let i = 3; i < imageData.data.length; i += 4) {
          if (imageData.data[i] > 0) alphaCount += 1;
        }
        PUT_CALLS.push({ width: imageData.width, height: imageData.height, alphaCount });
      },
      clearRect() {},
    } as unknown as CanvasRenderingContext2D;
  } as unknown as HTMLCanvasElement["getContext"];
}

beforeAll(() => {
  (globalThis as { Image?: unknown }).Image = ImmediateImage;
  installCanvasMock();
});

afterEach(() => {
  cleanup();
  PUT_CALLS.length = 0;
});

function visualDiffEvent(): SessionEvent {
  return {
    id: 1,
    kind: "artifact",
    ts: null,
    text: "",
    disposition: "rendered",
    artifact_id: "abc",
    title: "Login form",
    artifact: {
      kind: "visual-diff",
      before: { mime: "image/png", width: 12, height: 8 },
      after: { mime: "image/png", width: 12, height: 8 },
    },
  };
}

describe("visual-diff renderer", () => {
  test("renders both variants and updates after-image opacity when the slider moves", async () => {
    render(<VisualDiffRenderer artifact={visualDiffEvent().artifact!} event={visualDiffEvent()} ticket="WIKI-193" />);
    const beforeImage = await screen.findByAltText("Login form");
    const stage = beforeImage.parentElement as HTMLElement;
    const afterImage = stage.querySelector("img.is-after") as HTMLImageElement;
    expect(afterImage).toBeTruthy();
    expect(beforeImage.getAttribute("src")).toBe(
      "/api/agents/WIKI-193/artifact/abc?variant=before",
    );
    expect(afterImage.getAttribute("src")).toBe(
      "/api/agents/WIKI-193/artifact/abc?variant=after",
    );
    // Default opacity is 50%.
    expect(afterImage.style.opacity).toBe("0.5");

    const slider = screen.getByRole("slider") as HTMLInputElement;
    fireEvent.change(slider, { target: { value: "1" } });
    expect(afterImage.style.opacity).toBe("1");

    fireEvent.change(slider, { target: { value: "0" } });
    expect(afterImage.style.opacity).toBe("0");
  });

  test("pixel-diff toggle drives the draw / diff / overlay / percentage pipeline", async () => {
    render(<VisualDiffRenderer artifact={visualDiffEvent().artifact!} event={visualDiffEvent()} ticket="WIKI-193" />);
    await screen.findByAltText("Login form");
    const toggle = screen.getByRole("button", { name: /pixel diff/i });
    expect(toggle.getAttribute("aria-pressed")).toBe("false");
    expect(PUT_CALLS.length).toBe(0);

    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-pressed")).toBe("true");

    // The diff effect runs after the click; wait for the overlay put to land.
    await waitFor(() => expect(PUT_CALLS.length).toBeGreaterThan(0));

    // Every put pushed through the pipeline sits at the bounded diff size
    // (12x8 in this test), which matches the natural size — the buffer was
    // real ImageData shaped correctly for the source, not a shape-only stub.
    const put = PUT_CALLS[PUT_CALLS.length - 1];
    expect(put.width).toBe(12);
    expect(put.height).toBe(8);
    // The AFTER pixels are pure red on a black BEFORE — every pixel changes,
    // so the overlay is fully painted (alpha > 0 across the full buffer).
    expect(put.alphaCount).toBe(12 * 8);

    // Percentage readout renders inside the toggle only after the diff
    // completes without an overlay error.
    await waitFor(() => expect(toggle.textContent ?? "").toMatch(/100%/));

    // No overlay error surfaced — the pipeline succeeded end-to-end.
    expect(screen.queryByRole("status")).toBeNull();

    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-pressed")).toBe("false");
    // Percentage disappears once the toggle turns off.
    await waitFor(() => expect(toggle.textContent ?? "").not.toMatch(/%/));
  });

  test("survives an image load failure and exposes the retry control", async () => {
    // Force onerror across every ImmediateImage instance for this test only.
    // Removing the onload path proves the component swaps from the loading
    // hook set to the error hook set without a hook-count mismatch — useId
    // must be called before the error return, or React throws here.
    const originalDescriptor = Object.getOwnPropertyDescriptor(ImmediateImage.prototype, "src")!;
    Object.defineProperty(ImmediateImage.prototype, "src", {
      configurable: true,
      set(this: ImmediateImage & { onerror: ((e: unknown) => void) | null }, value: string) {
        (this as unknown as { _src: string })._src = value;
        queueMicrotask(() => this.onerror?.(new Error("boom")));
      },
      get(this: { _src?: string }) {
        return this._src ?? "";
      },
    });
    try {
      render(
        <VisualDiffRenderer
          artifact={visualDiffEvent().artifact!}
          event={visualDiffEvent()}
          ticket="WIKI-193"
        />,
      );
      // The error state exposes the fallback title + a retry control.
      const retry = await screen.findByRole("button", { name: /retry|try again/i });
      expect(retry).toBeTruthy();
      // Clicking retry re-enters the loading path — no React hook error is
      // thrown during the state transitions.
      fireEvent.click(retry);
      // Post-click, the error UI is still up (loads still fail) — no crash.
      await screen.findByRole("button", { name: /retry|try again/i });
    } finally {
      Object.defineProperty(ImmediateImage.prototype, "src", originalDescriptor);
    }
  });

  test("readOnly hides the slider and toggle", async () => {
    render(
      <VisualDiffRenderer
        artifact={visualDiffEvent().artifact!}
        event={visualDiffEvent()}
        readOnly
        ticket="WIKI-193"
      />,
    );
    await screen.findByAltText("Login form");
    expect(screen.queryByRole("slider")).toBeNull();
    expect(screen.queryByRole("button", { name: /pixel diff/i })).toBeNull();
  });
});

describe("visual-diff compact preview in ArtifactBlock", () => {
  test("oversized visual-diff renders no live controls and never bubbles to onOpen", async () => {
    const onOpen = vi.fn();
    const event: SessionEvent = {
      id: 1,
      kind: "artifact",
      ts: null,
      text: "",
      disposition: "rendered",
      artifact_id: "big",
      title: "Screenshot pair",
      artifact: {
        kind: "visual-diff",
        // Above the 400px threshold — compact preview kicks in.
        before: { mime: "image/png", width: 1280, height: 720 },
        after: { mime: "image/png", width: 1280, height: 720 },
      },
    };
    render(<ArtifactBlock event={event} onOpen={onOpen} ticket="WIKI-193" />);
    await screen.findByAltText("Screenshot pair");

    // No interactive slider or pixel-diff toggle in the compact preview.
    expect(screen.queryByRole("slider")).toBeNull();
    expect(screen.queryByRole("button", { name: /pixel diff/i })).toBeNull();

    // The whole compact body remains a single expand affordance. onOpen fires
    // only from an explicit body click, never mid-gesture on live controls.
    expect(onOpen).not.toHaveBeenCalled();
  });

  test("compact body click forwards to onOpen so the panel can pick it up", async () => {
    const onOpen = vi.fn();
    const event: SessionEvent = {
      id: 1,
      kind: "artifact",
      ts: null,
      text: "",
      disposition: "rendered",
      artifact_id: "big",
      title: "Screenshot pair",
      artifact: {
        kind: "visual-diff",
        before: { mime: "image/png", width: 1280, height: 720 },
        after: { mime: "image/png", width: 1280, height: 720 },
      },
    };
    render(<ArtifactBlock event={event} onOpen={onOpen} ticket="WIKI-193" />);
    await screen.findByAltText("Screenshot pair");
    const compactBody = document.querySelector('[data-artifact-compact] .artifact-body') as HTMLElement | null;
    expect(compactBody).toBeTruthy();
    fireEvent.click(compactBody!);
    expect(onOpen).toHaveBeenCalledTimes(1);
  });
});

describe("visual-diff in ArtifactPanel", () => {
  test("panel renders both variants and controls for a focused visual-diff tab", async () => {
    const event: SessionEvent = {
      id: 1,
      kind: "artifact",
      ts: null,
      text: "",
      disposition: "rendered",
      artifact_id: "big",
      title: "Screenshot pair",
      artifact: {
        kind: "visual-diff",
        before: { mime: "image/png", width: 1280, height: 720 },
        after: { mime: "image/png", width: 1280, height: 720 },
      },
    };
    render(
      <ArtifactPanel
        artifacts={new Map([[event.artifact_id!, event]])}
        onClosePanel={() => {}}
        onCloseTab={() => {}}
        onFocusTab={() => {}}
        onReopen={() => {}}
        onResizeStart={() => {}}
        onUpdateViewState={() => {}}
        state={{
          tabs: [event.artifact_id!],
          focusedTab: event.artifact_id!,
          recentlyClosed: [],
          viewState: {},
        }}
        ticket="WIKI-193"
        width={640}
      />,
    );
    const before = await screen.findByAltText("Screenshot pair");
    expect(before.getAttribute("src")).toBe(
      "/api/agents/WIKI-193/artifact/big?variant=before",
    );
    const after = before.parentElement?.querySelector("img.is-after") as HTMLImageElement | null;
    expect(after?.getAttribute("src")).toBe(
      "/api/agents/WIKI-193/artifact/big?variant=after",
    );
    // Live controls surface — regression against the "Artifact unavailable"
    // fallback the panel was showing before the visual-diff case was added.
    expect(screen.getByRole("slider")).toBeTruthy();
    expect(screen.getByRole("button", { name: /pixel diff/i })).toBeTruthy();
    expect(screen.queryByText(/artifact unavailable/i)).toBeNull();
  });
});

describe("visual-diff pixel-diff at oversized native resolution", () => {
  test("localized one-pixel regression survives the bounded overlay cap", async () => {
    // 2400 x 1200 = 2.88 MP — above the 2 MP overlay cap, so the naive
    // bilinear downsample would have averaged a single-pixel regression
    // away. This test replaces the canvas mock with a tile-aware one and
    // asserts (a) native totals are reported, (b) the localized change is
    // counted, and (c) the bounded overlay contains a highlight at the
    // corresponding projected pixel.

    const NATIVE_W = 2400;
    const NATIVE_H = 1200;

    // The one differing pixel — everywhere else, before == after (pure black).
    const CHANGE_X = 42;
    const CHANGE_Y = 17;

    class LargeImage {
      onload: (() => void) | null = null;
      onerror: ((error: unknown) => void) | null = null;
      crossOrigin: string | null = null;
      decoding: string | null = null;
      naturalWidth = NATIVE_W;
      naturalHeight = NATIVE_H;
      isAfter = false;
      #src = "";
      get src(): string {
        return this.#src;
      }
      set src(value: string) {
        this.#src = value;
        this.isAfter = value.includes("variant=after");
        queueMicrotask(() => this.onload?.());
      }
    }

    const originalImage = (globalThis as { Image?: unknown }).Image;
    (globalThis as { Image?: unknown }).Image = LargeImage;

    const originalGetContext = HTMLCanvasElement.prototype.getContext;
    let boundedPut: { width: number; height: number; data: Uint8ClampedArray } | null = null;

    // Tile-aware canvas mock: drawImage carries the (sx, sy, sw, sh) source
    // rect, and getImageData returns the pixels for that native tile. Every
    // pixel is black except (CHANGE_X, CHANGE_Y) on the "after" image, which
    // becomes solid red — a single-pixel regression on a 2.88 MP screenshot.
    HTMLCanvasElement.prototype.getContext = function (kind: string) {
      if (kind !== "2d") return null;
      let currentImage: LargeImage | null = null;
      let currentSource: { sx: number; sy: number; sw: number; sh: number } | null = null;
      let canvas = this as HTMLCanvasElement;
      return {
        get canvas() { return canvas; },
        set imageSmoothingEnabled(_v: boolean) {},
        drawImage(image: object, ...rest: number[]) {
          currentImage = image as LargeImage;
          if (rest.length >= 8) {
            currentSource = { sx: rest[0], sy: rest[1], sw: rest[2], sh: rest[3] };
          } else {
            currentSource = { sx: 0, sy: 0, sw: currentImage.naturalWidth, sh: currentImage.naturalHeight };
          }
        },
        getImageData(_x: number, _y: number, width: number, height: number) {
          const data = new Uint8ClampedArray(width * height * 4);
          // Solid black by default. Alpha=255 across.
          for (let i = 3; i < data.length; i += 4) data[i] = 255;
          if (currentImage?.isAfter && currentSource) {
            const relX = CHANGE_X - currentSource.sx;
            const relY = CHANGE_Y - currentSource.sy;
            if (relX >= 0 && relX < width && relY >= 0 && relY < height) {
              const idx = (relY * width + relX) * 4;
              data[idx] = 255;      // red
              data[idx + 1] = 0;
              data[idx + 2] = 0;
              data[idx + 3] = 255;
            }
          }
          return { data, width, height, colorSpace: "srgb" as const };
        },
        createImageData(width: number, height: number) {
          return { data: new Uint8ClampedArray(width * height * 4), width, height, colorSpace: "srgb" as const };
        },
        putImageData(imageData: { data: Uint8ClampedArray; width: number; height: number }) {
          // The bounded overlay canvas has 2-D size < native; record the one
          // whose dims match the overlay canvas (not the per-tile scratch).
          if (imageData.width === canvas.width && imageData.height === canvas.height && canvas.width < NATIVE_W) {
            boundedPut = { width: imageData.width, height: imageData.height, data: new Uint8ClampedArray(imageData.data) };
          }
        },
        clearRect() {},
      } as unknown as CanvasRenderingContext2D;
    } as unknown as HTMLCanvasElement["getContext"];

    try {
      const event: SessionEvent = {
        id: 1,
        kind: "artifact",
        ts: null,
        text: "",
        disposition: "rendered",
        artifact_id: "big",
        title: "Oversized pair",
        artifact: {
          kind: "visual-diff",
          before: { mime: "image/png", width: NATIVE_W, height: NATIVE_H },
          after: { mime: "image/png", width: NATIVE_W, height: NATIVE_H },
        },
      };
      render(<VisualDiffRenderer artifact={event.artifact!} event={event} ticket="WIKI-193" />);
      await screen.findByAltText("Oversized pair");
      const toggle = screen.getByRole("button", { name: /pixel diff/i });
      fireEvent.click(toggle);

      // The overlay is composited from many tiles; wait for putImageData to land.
      await waitFor(() => expect(boundedPut).not.toBeNull());

      // Percentage renders as <0.1% (one native pixel out of 2.88M).
      await waitFor(() => expect(toggle.textContent ?? "").toMatch(/<0\.1%|0\.0%/));

      // The bounded overlay must contain exactly one painted pixel at the
      // projected location. Any other count means either the localized
      // change was averaged away (previous bug) or spuriously duplicated.
      let painted = 0;
      for (let i = 3; i < boundedPut!.data.length; i += 4) {
        if (boundedPut!.data[i] > 0) painted += 1;
      }
      expect(painted).toBe(1);
    } finally {
      HTMLCanvasElement.prototype.getContext = originalGetContext;
      (globalThis as { Image?: unknown }).Image = originalImage;
    }
  });
});

describe("visual-diff pixel-diff cancellation and yield", () => {
  test("toggling off mid-run cancels the async tiled work before the final overlay lands", async () => {
    // 2400 x 1200 = 2.88 MP, 6 tiles at NATIVE_DIFF_TILE=1024. The async
    // work parks between tiles on a setTimeout(0); a toggle-off before that
    // macrotask fires must abort the remaining tiles and skip the final
    // putImageData onto the overlay canvas. Without cancellation, the
    // reviewer-observed ~0.9s of unbroken JS locks up the webview.

    const NATIVE_W = 2400;
    const NATIVE_H = 1200;

    class LargeImage {
      onload: (() => void) | null = null;
      onerror: ((error: unknown) => void) | null = null;
      crossOrigin: string | null = null;
      decoding: string | null = null;
      naturalWidth = NATIVE_W;
      naturalHeight = NATIVE_H;
      #src = "";
      get src(): string { return this.#src; }
      set src(value: string) {
        this.#src = value;
        queueMicrotask(() => this.onload?.());
      }
    }

    const originalImage = (globalThis as { Image?: unknown }).Image;
    (globalThis as { Image?: unknown }).Image = LargeImage;

    const originalGetContext = HTMLCanvasElement.prototype.getContext;
    let overlayPutCount = 0;
    HTMLCanvasElement.prototype.getContext = function (kind: string) {
      if (kind !== "2d") return null;
      const canvas = this as HTMLCanvasElement;
      return {
        get canvas() { return canvas; },
        set imageSmoothingEnabled(_v: boolean) {},
        drawImage() {},
        getImageData(_x: number, _y: number, width: number, height: number) {
          const data = new Uint8ClampedArray(width * height * 4);
          for (let i = 3; i < data.length; i += 4) data[i] = 255;
          return { data, width, height, colorSpace: "srgb" as const };
        },
        createImageData(width: number, height: number) {
          return { data: new Uint8ClampedArray(width * height * 4), width, height, colorSpace: "srgb" as const };
        },
        putImageData(imageData: { data: Uint8ClampedArray; width: number; height: number }) {
          // Only count the final projection onto the overlay canvas (bounded
          // dims that match the canvas element). Tile scratches don't put.
          if (imageData.width === canvas.width && imageData.height === canvas.height && canvas.width < NATIVE_W) {
            overlayPutCount += 1;
          }
        },
        clearRect() {},
      } as unknown as CanvasRenderingContext2D;
    } as unknown as HTMLCanvasElement["getContext"];

    try {
      const event: SessionEvent = {
        id: 1,
        kind: "artifact",
        ts: null,
        text: "",
        disposition: "rendered",
        artifact_id: "cancel",
        title: "Oversized pair",
        artifact: {
          kind: "visual-diff",
          before: { mime: "image/png", width: NATIVE_W, height: NATIVE_H },
          after: { mime: "image/png", width: NATIVE_W, height: NATIVE_H },
        },
      };
      render(<VisualDiffRenderer artifact={event.artifact!} event={event} ticket="WIKI-193" />);
      await screen.findByAltText("Oversized pair");
      const toggle = screen.getByRole("button", { name: /pixel diff/i });

      // Turn pixel-diff on. The effect kicks off the async tiled diff — one
      // tile runs synchronously, then the loop parks on setTimeout(0).
      fireEvent.click(toggle);
      // Turn pixel-diff off before any macrotask can fire. Cleanup aborts
      // the controller; the parked tile iterations must return null on wake.
      fireEvent.click(toggle);

      // Let all pending macrotasks flush. If cancellation is broken, this
      // is where the remaining 5 tiles would run and a final put would land.
      await new Promise((resolve) => setTimeout(resolve, 100));

      expect(overlayPutCount).toBe(0);
      // Percentage readout must not render — that would prove stale stats
      // reached state after the abort.
      expect(toggle.textContent ?? "").not.toMatch(/%/);
    } finally {
      HTMLCanvasElement.prototype.getContext = originalGetContext;
      (globalThis as { Image?: unknown }).Image = originalImage;
    }
  });
});

describe("visual-diff renderer id uniqueness", () => {
  test("mounting two renderers for the same artifact produces distinct slider ids and local htmlFor bindings", async () => {
    const event: SessionEvent = {
      id: 1,
      kind: "artifact",
      ts: null,
      text: "",
      disposition: "rendered",
      artifact_id: "shared",
      title: "Screenshot pair",
      artifact: {
        kind: "visual-diff",
        before: { mime: "image/png", width: 12, height: 8 },
        after: { mime: "image/png", width: 12, height: 8 },
      },
    };
    render(
      <>
        <VisualDiffRenderer artifact={event.artifact!} event={event} ticket="WIKI-193" />
        <VisualDiffRenderer artifact={event.artifact!} event={event} ticket="WIKI-193" />
      </>,
    );
    await screen.findAllByAltText("Screenshot pair");
    const sliders = screen.getAllByRole("slider") as HTMLInputElement[];
    expect(sliders).toHaveLength(2);
    // Ids differ across copies.
    expect(sliders[0].id).not.toBe(sliders[1].id);
    // Each label's htmlFor binds to its own local slider — a click on a
    // label focuses the matching slider, not the sibling copy.
    for (const slider of sliders) {
      const label = document.querySelector(`label[for="${slider.id}"]`) as HTMLLabelElement | null;
      expect(label).toBeTruthy();
      expect(label!.getAttribute("for")).toBe(slider.id);
    }
  });
});
