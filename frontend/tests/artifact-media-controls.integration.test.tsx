// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { useRef } from "react";
import { MediaControls } from "../src/artifact-media-controls";

function MediaControlsHarness() {
  const mediaRef = useRef<HTMLVideoElement | null>(null);
  return (
    <MediaControls
      className=""
      controlsClassName=""
      initialDuration={1}
      mediaKey="test-media"
      mediaLabel="Test video"
      mediaRef={mediaRef}
      onSpeedChange={vi.fn()}
      showFullscreen
      speed={1}
    >
      <video ref={mediaRef} />
    </MediaControls>
  );
}

beforeEach(() => {
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
    window.setTimeout(() => callback(0), 0);
    return 1;
  });
  vi.stubGlobal("cancelAnimationFrame", () => undefined);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("media controls modal fallback", () => {
  test("closes from a nested control and restores focus to the expand button", async () => {
    render(<MediaControlsHarness />);
    const expandButton = screen.getByRole("button", { name: "Enter fullscreen" });

    expandButton.focus();
    fireEvent.click(expandButton);
    const volume = screen.getByRole("slider", { name: "Volume" });
    volume.focus();
    fireEvent.keyDown(volume, { key: "Escape" });

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
      expect(document.activeElement?.getAttribute("aria-label")).toBe("Enter fullscreen");
    });
  });

  test("cleans scroll lock and inert state when unmounted while open", () => {
    const outside = document.createElement("div");
    document.body.appendChild(outside);
    try {
      const { unmount } = render(<MediaControlsHarness />);
      fireEvent.click(screen.getByRole("button", { name: "Enter fullscreen" }));
      expect(document.body.style.overflow).toBe("hidden");
      expect(outside.hasAttribute("inert")).toBe(true);

      unmount();

      expect(document.body.style.overflow).toBe("");
      expect(outside.hasAttribute("inert")).toBe(false);
      expect(outside.hasAttribute("aria-hidden")).toBe(false);
    } finally {
      outside.remove();
    }
  });
});
