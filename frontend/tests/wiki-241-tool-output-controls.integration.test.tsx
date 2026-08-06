// @vitest-environment jsdom
// WIKI-241: expanded tool detail uses action labels ("show all" / "show
// less", "copy output"), keeps the preview head visible while long bodies
// scroll, and anchors the transcript scroll so expand and collapse do not
// jump the reader elsewhere. WIKI-251 removed the "wrap lines"/"keep lines"
// chip: every tool-output body wraps unconditionally — no more no-wrap
// escape hatch, and no more toggle whose "off" state would force horizontal
// scroll on long lines.
import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { STREAM_CLAMP_LINES } from "../src/stream-clamp";
import { BoundedPreview } from "../src/transcript-preview";

class NoopObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

beforeEach(() => {
  (globalThis as { ResizeObserver?: unknown }).ResizeObserver ??= NoopObserver;
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function longText(lines: number, width = 20): string {
  return Array.from({ length: lines }, (_, i) => `line-${String(i + 1).padStart(3, "0")} ${"x".repeat(width)}`).join("\n");
}

test("head shows purpose label and summary counts", () => {
  const { container, getByText } = render(
    <BoundedPreview label="file contents" text={longText(4)} />
  );
  expect(container.querySelector(".transcript-preview-label")?.textContent).toBe("file contents");
  expect(getByText(/lines · /)).toBeTruthy();
});

test("tool output body wraps unconditionally — WIKI-251 removed the wrap toggle", () => {
  const { container, queryByText } = render(
    <BoundedPreview label="tool output" text={longText(35, 200)} />
  );
  // The old "keep lines" / "wrap lines" chip is gone. Long output body
  // always wears .is-wrap so long tokens fall to overflow-wrap: anywhere
  // instead of dispatching a horizontal scrollbar.
  expect(queryByText("keep lines")).toBeNull();
  expect(queryByText("wrap lines")).toBeNull();
  const body = container.querySelector(".transcript-preview-body");
  expect(body?.classList.contains("is-wrap")).toBe(true);
  expect(body?.classList.contains("is-nowrap")).toBe(false);
});

test("expand toggle uses show all / show less and drives is-expanded state", () => {
  const total = STREAM_CLAMP_LINES + 12;
  const { container, getByText } = render(
    <BoundedPreview label="file contents" text={longText(total)} />
  );
  expect(container.querySelector(".transcript-preview")?.classList.contains("is-expanded")).toBe(false);
  fireEvent.click(getByText("show all"));
  expect(container.querySelector(".transcript-preview")?.classList.contains("is-expanded")).toBe(true);
  fireEvent.click(getByText("show less"));
  expect(container.querySelector(".transcript-preview")?.classList.contains("is-expanded")).toBe(false);
});

test("copy chip reads 'copy output' and flips to 'copied' on success", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  vi.stubGlobal("navigator", { clipboard: { writeText } });
  const { getByText, findByText } = render(
    <BoundedPreview label="tool output" text="hello world" />
  );
  const copy = getByText("copy output");
  fireEvent.click(copy);
  expect(writeText).toHaveBeenCalledWith("hello world");
  await findByText("copied");
});

test("expand anchors the enclosing .session-scroll so the head stays at the same viewport y", () => {
  // Simulate the transcript layout: an outer .session-scroll with fixed
  // padding so the preview head sits at a known offset. Expanding must not
  // teleport the scroll — the head's offset from the container top stays.
  const container = document.createElement("div");
  container.className = "session-scroll";
  container.style.height = "400px";
  container.style.overflow = "auto";
  document.body.appendChild(container);
  const spacer = document.createElement("div");
  spacer.style.height = "600px";
  container.appendChild(spacer);
  const mount = document.createElement("div");
  container.appendChild(mount);

  render(<BoundedPreview label="file contents" text={longText(STREAM_CLAMP_LINES + 20)} />, { container: mount });

  // Force the reader to a mid-scroll position, then rig getBoundingClientRect
  // so the head's viewport y is known and stable.
  container.scrollTop = 200;
  const head = mount.querySelector<HTMLElement>(".transcript-preview-head");
  const preview = mount.querySelector<HTMLElement>(".transcript-preview");
  expect(head).toBeTruthy();
  expect(preview).toBeTruthy();

  const beforeContainerRect = { top: 0 } as DOMRect;
  const beforeHeadRect = { top: 60 } as DOMRect;
  const afterHeadRect = { top: 60 } as DOMRect;

  vi.spyOn(container, "getBoundingClientRect").mockReturnValue(beforeContainerRect);
  const headSpy = vi.spyOn(head!, "getBoundingClientRect");
  headSpy.mockReturnValueOnce(beforeHeadRect);
  headSpy.mockReturnValue(afterHeadRect);

  const rafSpy = vi.spyOn(window, "requestAnimationFrame").mockImplementation((cb) => {
    cb(0);
    return 0;
  });

  const showAll = mount.querySelector<HTMLButtonElement>(".transcript-chip.is-active, .transcript-chip");
  // Find the actual expand chip by text.
  const chips = Array.from(mount.querySelectorAll<HTMLButtonElement>(".transcript-chip"));
  const expandChip = chips.find((c) => c.textContent === "show all");
  expect(expandChip).toBeTruthy();
  expect(showAll).toBeTruthy();
  fireEvent.click(expandChip!);

  // Head y did not move (mocked equal), so no scroll correction needed. If
  // the head DID move, the component would have adjusted container.scrollTop
  // by the delta — that path is exercised by the next test.
  expect(container.scrollTop).toBe(200);

  rafSpy.mockRestore();
  document.body.removeChild(container);
});

test("expand corrects container.scrollTop when the head shifts", () => {
  const container = document.createElement("div");
  container.className = "session-scroll";
  container.style.height = "400px";
  container.style.overflow = "auto";
  document.body.appendChild(container);
  const spacer = document.createElement("div");
  spacer.style.height = "800px";
  container.appendChild(spacer);
  const mount = document.createElement("div");
  container.appendChild(mount);

  render(<BoundedPreview label="file contents" text={longText(STREAM_CLAMP_LINES + 30)} />, { container: mount });
  container.scrollTop = 300;

  const head = mount.querySelector<HTMLElement>(".transcript-preview-head");
  vi.spyOn(container, "getBoundingClientRect").mockReturnValue({ top: 0 } as DOMRect);
  const headSpy = vi.spyOn(head!, "getBoundingClientRect");
  // Before click: head sits 80px into the viewport.
  headSpy.mockReturnValueOnce({ top: 80 } as DOMRect);
  // After expand: head would drift 40px down (say, sticky header nudged the
  // layout). Return a stable "after" for subsequent frames.
  headSpy.mockReturnValue({ top: 120 } as DOMRect);
  const rafSpy = vi.spyOn(window, "requestAnimationFrame").mockImplementation((cb) => {
    cb(0);
    return 0;
  });

  const expandChip = Array.from(mount.querySelectorAll<HTMLButtonElement>(".transcript-chip"))
    .find((c) => c.textContent === "show all");
  fireEvent.click(expandChip!);

  // Delta = 120 - 80 = 40; container.scrollTop advances by 40 so the head
  // returns to its original viewport y.
  expect(container.scrollTop).toBe(340);

  rafSpy.mockRestore();
  document.body.removeChild(container);
});

test("narrow layout keeps every remaining action chip present and reachable", () => {
  const total = STREAM_CLAMP_LINES + 5;
  const { container } = render(
    <div style={{ width: "260px" }}>
      <BoundedPreview label="tool output" text={longText(total, 200)} />
    </div>
  );
  const chips = Array.from(container.querySelectorAll<HTMLButtonElement>(".transcript-chip"));
  const texts = chips.map((c) => c.textContent);
  // WIKI-251: the wrap chip is gone. Show-all and copy-output remain and
  // must both be present + accessible at narrow widths.
  expect(texts).not.toContain("keep lines");
  expect(texts).not.toContain("wrap lines");
  expect(texts).toContain("show all");
  expect(texts).toContain("copy output");
  for (const chip of chips) {
    expect(chip.getAttribute("aria-label")).toBeTruthy();
  }
});
