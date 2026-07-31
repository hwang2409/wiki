// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, test, vi } from "vitest";

import type { SessionEvent } from "../src/api";
import { ArtifactBlock } from "../src/artifact-block";
import { VisualDiffRenderer } from "../src/visual-diff-renderer";

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
    queueMicrotask(() => this.onload?.());
  }
}

beforeAll(() => {
  (globalThis as { Image?: unknown }).Image = ImmediateImage;
});

afterEach(() => cleanup());

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

  test("pixel-diff toggle turns the overlay canvas on and off", async () => {
    render(<VisualDiffRenderer artifact={visualDiffEvent().artifact!} event={visualDiffEvent()} ticket="WIKI-193" />);
    await screen.findByAltText("Login form");
    const toggle = screen.getByRole("button", { name: /pixel diff/i });
    expect(toggle.getAttribute("aria-pressed")).toBe("false");

    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-pressed")).toBe("true");

    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-pressed")).toBe("false");
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
});
