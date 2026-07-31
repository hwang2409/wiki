// @vitest-environment jsdom
// WIKI-222: stream output flows at full height below the shared threshold;
// only past it does a renderer clamp (with an expand affordance or, for
// virtualized tables, an inner scroll viewport).
import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { SessionArtifact, SessionEvent } from "../src/api";
import { ArtifactRenderer } from "../src/artifact-renderers";
import {
  STREAM_CLAMP_LINES,
  STREAM_CLAMP_PX,
  STREAM_CLAMP_SLACK_PX,
  StreamClamp,
} from "../src/stream-clamp";
import { BoundedPreview } from "../src/transcript-preview";

class NoopObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

let scrollHeightSpy: { restore: () => void } | null = null;

function mockScrollHeight(value: number) {
  const original = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollHeight");
  Object.defineProperty(HTMLElement.prototype, "scrollHeight", {
    configurable: true,
    get: () => value,
  });
  scrollHeightSpy = {
    restore: () => {
      if (original) Object.defineProperty(HTMLElement.prototype, "scrollHeight", original);
      else delete (HTMLElement.prototype as { scrollHeight?: unknown }).scrollHeight;
    },
  };
}

beforeEach(() => {
  (globalThis as { ResizeObserver?: unknown }).ResizeObserver ??= NoopObserver;
});

afterEach(() => {
  scrollHeightSpy?.restore();
  scrollHeightSpy = null;
  cleanup();
  vi.restoreAllMocks();
});

test("StreamClamp leaves under-threshold content unclamped with no toggle", () => {
  mockScrollHeight(STREAM_CLAMP_PX - 40);
  const { container } = render(
    <StreamClamp>
      <pre>short content</pre>
    </StreamClamp>
  );
  const clamp = container.querySelector(".stream-clamp");
  expect(clamp?.classList.contains("is-clamped")).toBe(false);
  const body = container.querySelector<HTMLElement>(".stream-clamp-body");
  expect(body?.style.maxHeight).toBe("");
  expect(container.querySelector(".stream-clamp-toggle")).toBeNull();
});

test("StreamClamp clamps over-threshold content and expands on toggle", () => {
  mockScrollHeight(STREAM_CLAMP_PX + STREAM_CLAMP_SLACK_PX + 400);
  const { container } = render(
    <StreamClamp>
      <pre>tall content</pre>
    </StreamClamp>
  );
  const clamp = container.querySelector(".stream-clamp");
  expect(clamp?.classList.contains("is-clamped")).toBe(true);
  const body = container.querySelector<HTMLElement>(".stream-clamp-body");
  expect(body?.style.maxHeight).toBe(`${STREAM_CLAMP_PX}px`);

  const toggle = container.querySelector<HTMLButtonElement>(".stream-clamp-toggle");
  expect(toggle?.textContent).toBe("show all");
  fireEvent.click(toggle!);
  expect(container.querySelector(".stream-clamp")?.classList.contains("is-clamped")).toBe(false);
  expect(container.querySelector<HTMLElement>(".stream-clamp-body")?.style.maxHeight).toBe("");
  expect(container.querySelector(".stream-clamp-toggle")?.textContent).toBe("collapse");
});

test("BoundedPreview shows threshold-sized text in full with no expand chip", () => {
  const text = Array.from({ length: STREAM_CLAMP_LINES }, (_, i) => `line ${i + 1}`).join("\n");
  const { container, queryByText } = render(<BoundedPreview label="output" text={text} />);
  const bodyText = container.querySelector(".transcript-preview-body")?.textContent ?? "";
  expect(bodyText).toContain(`line ${STREAM_CLAMP_LINES}`);
  expect(queryByText("expand")).toBeNull();
  expect(container.querySelector(".transcript-preview-more")).toBeNull();
});

test("BoundedPreview clips past the threshold and expands to full flow", () => {
  const total = STREAM_CLAMP_LINES + 25;
  const text = Array.from({ length: total }, (_, i) => `line ${i + 1}`).join("\n");
  const { container, getByText } = render(<BoundedPreview label="output" text={text} />);
  let bodyText = container.querySelector(".transcript-preview-body")?.textContent ?? "";
  expect(bodyText).toContain(`line ${STREAM_CLAMP_LINES}`);
  expect(bodyText).not.toContain(`line ${total}`);
  expect(container.querySelector(".transcript-preview-more")?.textContent).toContain("+25 more lines");

  fireEvent.click(getByText("expand"));
  bodyText = container.querySelector(".transcript-preview-body")?.textContent ?? "";
  expect(bodyText).toContain(`line ${total}`);
});

function tableArtifact(rowCount: number): SessionArtifact {
  return {
    kind: "table",
    columns: [
      { key: "name", label: "Name", type: "string" },
      { key: "count", label: "Count", type: "number" },
    ],
    rows: Array.from({ length: rowCount }, (_, i) => [`row ${i + 1}`, i + 1]),
  };
}

const tableEvent = { id: 1, kind: "artifact" } as unknown as SessionEvent;

test("small artifact tables render every row with no scroll viewport", () => {
  const { container } = render(
    <ArtifactRenderer artifact={tableArtifact(12)} event={tableEvent} ticket="WIKI-222" />
  );
  const scroll = container.querySelector<HTMLElement>(".artifact-table-scroll");
  expect(scroll?.style.maxHeight).toBe("");
  expect(container.querySelectorAll("tbody tr:not(.artifact-table-spacer)").length).toBe(12);
});

test("tall artifact tables keep the clamped virtualized viewport", () => {
  const rowCount = 200;
  const { container } = render(
    <ArtifactRenderer artifact={tableArtifact(rowCount)} event={tableEvent} ticket="WIKI-222" />
  );
  const scroll = container.querySelector<HTMLElement>(".artifact-table-scroll");
  expect(scroll?.style.maxHeight).toBe(`${STREAM_CLAMP_PX}px`);
  const rendered = container.querySelectorAll("tbody tr:not(.artifact-table-spacer)").length;
  expect(rendered).toBeGreaterThan(0);
  expect(rendered).toBeLessThan(rowCount);
});
