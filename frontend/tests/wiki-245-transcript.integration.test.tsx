// WIKI-245 transcript readability contracts: compact evidence, semantic
// labels, one visual unit per tool event, and render-time harness cleanup.
import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { SessionEvent, SessionTool } from "../src/api";
import {
  outputLabelForTool,
  traceRows,
  toolStatus,
  toolSummaryParts,
} from "../src/transcript-event-utils";
import { BoundedPreview } from "../src/transcript-preview";
import {
  cleanHarnessOutput,
  HarnessOutput,
  parseHarnessOutput,
} from "../src/transcript-output";

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

function tool(overrides: Partial<SessionTool> = {}): SessionTool {
  return {
    name: "Edit",
    input: "/tmp/hot.md",
    output: "updated",
    ok: true,
    archetype: "edit",
    summary: "edit hot.md",
    ...overrides,
  };
}

function event(id: number, overrides: Partial<SessionEvent> = {}): SessionEvent {
  return {
    id,
    kind: "tool",
    ts: null,
    text: "",
    disposition: "rendered",
    tool: tool(),
    ...overrides,
  };
}

test("short output is compact while actions stay keyboard-reachable", () => {
  const { container, getByRole } = render(<BoundedPreview label="acknowledgement" text="updated" />);
  const preview = container.querySelector(".transcript-preview");
  expect(preview?.classList.contains("is-compact")).toBe(true);
  const copy = getByRole("button", { name: "copy output" });
  expect(copy).toBeTruthy();
  copy.focus();
  expect(document.activeElement).toBe(copy);
});

test("compact display copies the unmodified raw output", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  vi.stubGlobal("navigator", { clipboard: { writeText } });
  const { getByRole } = render(
    <BoundedPreview label="acknowledgement" text="updated" rawText="updated (harness detail)" />,
  );
  fireEvent.click(getByRole("button", { name: "copy output" }));
  expect(writeText).toHaveBeenCalledWith("updated (harness detail)");
});

test("harness wrappers become semantic segments and known boilerplate is removed", () => {
  const raw = "File updated successfully. (file state is current in your context — no need to Read it back)";
  expect(cleanHarnessOutput(raw)).toBe("File updated successfully.");
  expect(parseHarnessOutput("<tool_use_error>Read the file first</tool_use_error>")).toEqual([
    { kind: "error", text: "Read the file first" },
  ]);
  const { container } = render(
    <HarnessOutput text={"<tool_use_error>Read the file first</tool_use_error>\n<system-reminder>quiet note</system-reminder>"} />,
  );
  expect(container.querySelector(".session-output-segment.is-error")?.textContent).toBe("Read the file first");
  expect(container.querySelector(".session-output-segment.is-note")?.textContent).toBe("quiet note");
  expect(container.textContent).not.toContain("<tool_use_error>");
});

test("tool units expose verb, target, status, and semantic output labels", () => {
  expect(toolSummaryParts(tool())).toEqual({ verb: "edit", target: "hot.md" });
  expect(toolStatus(tool({ output: null, ok: null }))).toBe("working");
  expect(outputLabelForTool(tool())).toBe("acknowledgement");
  expect(outputLabelForTool(tool({ archetype: "read" }))).toBe("file contents");
  expect(outputLabelForTool(tool({ archetype: "git", name: "Diff" }))).toBe("diff");
  expect(outputLabelForTool(tool({ ok: false }))).toBe("error output");
});

test("parallel completion timeline still produces one row per tool event", () => {
  const first = event(1, { ts: "2026-08-06T12:00:00.000Z" });
  const second = event(2, {
    ts: "2026-08-06T12:00:01.000Z",
    tool: tool({ summary: "read agent-events.ts", archetype: "read" }),
  });
  const timeline = [
    { event: first, eventIndex: 0, kind: "event" as const },
    { event: second, eventIndex: 1, kind: "event" as const },
    { event: second, eventIndex: 1, kind: "result" as const },
    { event: first, eventIndex: 0, kind: "result" as const },
  ];
  const rows = traceRows(timeline);
  expect(rows).toHaveLength(2);
  expect(rows.every((row) => row.kind === "tool")).toBe(true);
  expect(rows.map((row) => row.event.id)).toEqual([1, 2]);
});
