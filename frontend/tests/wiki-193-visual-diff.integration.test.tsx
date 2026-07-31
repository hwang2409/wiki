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
