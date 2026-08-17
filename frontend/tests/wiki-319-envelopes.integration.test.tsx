// @vitest-environment jsdom
import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { SessionEvent } from "../src/api";
import {
  ThoughtGroupRow,
  UserText,
  groupSettledThoughtRows,
} from "../src/session";
import type { EventRow } from "../src/session-layout";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  delete (HTMLElement.prototype as { scrollHeight?: number }).scrollHeight;
  delete (HTMLElement.prototype as { clientHeight?: number }).clientHeight;
});

beforeEach(() => {
  class ImmediateResizeObserver {
    observe() {}
    disconnect() {}
  }
  globalThis.ResizeObserver = ImmediateResizeObserver;
  Object.defineProperty(HTMLElement.prototype, "scrollHeight", {
    configurable: true,
    get() {
      return this.classList.contains("session-user-content") ? 320 : 0;
    },
  });
  Object.defineProperty(HTMLElement.prototype, "clientHeight", {
    configurable: true,
    get() {
      return this.classList.contains("session-user-content") ? 100 : 0;
    },
  });
});

function thought(id: number, text: string): SessionEvent {
  return {
    id,
    kind: "thinking",
    ts: `2026-08-17T12:00:0${id}.000Z`,
    text,
    disposition: "rendered",
    encrypted: true,
  };
}

function row(event: SessionEvent): EventRow {
  return { event, key: event.id };
}

test("clamps from rendered overflow and toggles the full message", () => {
  const { container, getByRole } = render(<UserText text={"line\n".repeat(40)} />);
  const content = container.querySelector(".session-user-content");
  expect(content?.getAttribute("data-rendered-overflow")).toBe("true");
  expect(content?.classList.contains("is-clamped")).toBe(true);
  const toggle = getByRole("button", { name: "Show more" });
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
  fireEvent.click(toggle);
  expect(getByRole("button", { name: "Show less" })).toBeTruthy();
  expect(content?.classList.contains("is-clamped")).toBe(false);
});

test("renders known envelopes as disclosures and leaves unknown tags unchanged", () => {
  const runtime = "<WIKI_RUNTIME_CARD>run=abc</WIKI_RUNTIME_CARD>";
  const plugins = "<recommended_plugins>one</recommended_plugins>";
  const unknown = "<custom_machine_tag>keep me</custom_machine_tag>";
  const { container } = render(
    <UserText text={`${plugins}\n${runtime}\nHuman task\n${unknown}`} />,
  );
  const summaries = Array.from(container.querySelectorAll("summary"), (summary) => summary.textContent);
  expect(summaries).toEqual(["recommended plugins", "runtime card"]);
  expect(container.querySelector(".session-envelope-raw")?.textContent).toBe(plugins);
  expect(container.textContent).toContain(unknown);
  fireEvent.click(container.querySelector("summary") as HTMLElement);
  expect(container.textContent).toContain(plugins);
});

test("groups settled thoughts while preserving order and per-row detail", () => {
  const rawRows = [1, 2, 3, 4].map((id) => row(thought(id, `title ${id}\nbody ${id}`)));
  const grouped = groupSettledThoughtRows(rawRows);
  expect(grouped).toHaveLength(2);
  expect(grouped[0]?.thoughts?.map((entry) => entry.event.text)).toEqual([
    "title 1\nbody 1",
    "title 2\nbody 2",
    "title 3\nbody 3",
  ]);
  expect(grouped[1]?.event.text).toBe("title 4\nbody 4");
  expect(grouped[1]?.live).toBe(true);

  const durations = new Map([[1, 100], [2, 200], [3, 300]]);
  const { container, getByRole } = render(
    <ThoughtGroupRow durations={durations} events={grouped[0]?.thoughts ?? []} />,
  );
  expect(getByRole("button").textContent).toContain("3 thoughts");
  expect(getByRole("button").textContent).toContain("600ms");
  fireEvent.click(getByRole("button"));
  expect(container.querySelectorAll(".session-thinking-title").length).toBe(3);
  expect(container.textContent?.indexOf("title 1")).toBeLessThan(container.textContent?.indexOf("title 2") ?? 0);
  expect(container.textContent?.indexOf("body 2")).toBeLessThan(container.textContent?.indexOf("body 3") ?? 0);
  expect(container.querySelectorAll(".session-thinking-chip")).toHaveLength(4);
});
