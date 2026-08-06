// @vitest-environment jsdom
// WIKI-253: long tool outputs default-collapse behind a one-line peek row;
// diffs, failures, and live-streaming outputs stay expanded regardless of
// length. The peek label surfaces top-level JSON keys, array counts, bash
// read paths, or the first non-empty line — the reader can tell what they
// are hiding before they click.
import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { SessionEvent, SessionTool } from "../src/api";
import { ToolCallRow } from "../src/session";
import {
  TOOL_OUTPUT_COLLAPSE_LINES,
  toolOutputPeek,
} from "../src/transcript-event-utils";

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
    name: "Bash",
    input: "ls -la",
    output: "example",
    ok: true,
    archetype: "bash",
    summary: "$ ls -la",
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

function longOutput(lines: number, prefix = "line"): string {
  return Array.from({ length: lines }, (_, index) => `${prefix} ${index + 1}`).join("\n");
}

// --- toolOutputPeek ---

test("toolOutputPeek surfaces top-level JSON keys before size", () => {
  const payload = JSON.stringify({ name: "wiki", path: "/tmp", entries: [1, 2, 3], count: 4 }, null, 2);
  const peek = toolOutputPeek(tool({ name: "Read", input: "config.json" }), payload);
  expect(peek.preview).toBe("{name, path, entries, count}");
  expect(peek.size).toMatch(/B$|KB$/);
  expect(peek.lines).toBeGreaterThan(0);
});

test("toolOutputPeek collapses a large object to keys with a +N tail", () => {
  const record: Record<string, number> = {};
  for (let i = 0; i < 12; i += 1) record[`key_${i}`] = i;
  const peek = toolOutputPeek(tool({ name: "Read" }), JSON.stringify(record, null, 2));
  expect(peek.preview).toBe("{key_0, key_1, key_2, key_3, +8}");
});

test("toolOutputPeek surfaces array cardinality", () => {
  const peek = toolOutputPeek(tool({ name: "list_agents" }), JSON.stringify([1, 2, 3, 4, 5]));
  expect(peek.preview).toBe("[5 items]");
});

test("toolOutputPeek surfaces the target path for a bash read", () => {
  const peek = toolOutputPeek(
    tool({ name: "Bash", input: "sed -n '20,80p' backend/src/agents/orch.py", archetype: "bash" }),
    "def orchestrate():\n    return None\n",
  );
  expect(peek.preview).toBe("backend/src/agents/orch.py");
});

test("toolOutputPeek falls back to first non-empty line for plain text", () => {
  const peek = toolOutputPeek(tool({ name: "Bash", input: "ls" }), "\n\nfirst real line\nsecond\n");
  expect(peek.preview).toBe("first real line");
});

test("toolOutputPeek reports (empty) when there is no output", () => {
  const peek = toolOutputPeek(tool({ name: "Bash" }), "");
  expect(peek.preview).toBe("(empty)");
  expect(peek.lines).toBe(0);
});

// --- ToolCallRow: default-collapse peek row ---

test("bash tool output longer than the collapse threshold renders a peek row", () => {
  const output = longOutput(TOOL_OUTPUT_COLLAPSE_LINES + 5);
  const evt = event(1, {
    tool: tool({ name: "Bash", input: "ls -la /repo", output }),
  });
  const { container } = render(<ToolCallRow event={evt} ticket="WIKI-253" withResult />);
  const peek = container.querySelector<HTMLButtonElement>(".session-tool-output-peek");
  expect(peek).toBeTruthy();
  expect(peek?.getAttribute("aria-expanded")).toBe("false");
  // The peek carries the size tail so the reader can tell what they are hiding.
  const meta = container.querySelector(".session-tool-output-peek-meta");
  expect(meta?.textContent).toMatch(new RegExp(`${TOOL_OUTPUT_COLLAPSE_LINES + 5} lines`));
  // The body block preview is absent while collapsed.
  expect(container.querySelector(".transcript-preview")).toBeNull();
});

test("clicking the peek row expands the body and adds a collapse affordance", () => {
  const output = longOutput(TOOL_OUTPUT_COLLAPSE_LINES + 5);
  const evt = event(2, {
    tool: tool({ name: "Bash", input: "ls", output }),
  });
  const { container } = render(<ToolCallRow event={evt} ticket="WIKI-253" withResult />);
  const peek = container.querySelector<HTMLButtonElement>(".session-tool-output-peek");
  expect(peek).toBeTruthy();
  fireEvent.click(peek!);
  // Peek row is replaced by the actual body preview.
  expect(container.querySelector(".session-tool-output-peek")).toBeNull();
  expect(container.querySelector(".transcript-preview")).toBeTruthy();
  // A collapse affordance appears so the reader can hide the body again.
  const collapse = container.querySelector<HTMLButtonElement>(".session-tool-output-collapse");
  expect(collapse).toBeTruthy();
  fireEvent.click(collapse!);
  expect(container.querySelector(".session-tool-output-peek")).toBeTruthy();
});

test("short tool outputs render inline without collapse chrome", () => {
  const evt = event(3, {
    tool: tool({ name: "Bash", input: "ls", output: "one\ntwo\nthree" }),
  });
  const { container } = render(<ToolCallRow event={evt} ticket="WIKI-253" withResult />);
  expect(container.querySelector(".session-tool-output-peek")).toBeNull();
  expect(container.querySelector(".session-tool-output-collapse")).toBeNull();
  // The body still renders (short outputs behave as they always did).
  expect(container.querySelector(".transcript-preview")).toBeTruthy();
});

test("failed tools stay expanded — no peek row, no default hiding", () => {
  const output = longOutput(TOOL_OUTPUT_COLLAPSE_LINES + 8, "err");
  const evt = event(4, {
    tool: tool({
      name: "Bash",
      input: "make test",
      output,
      ok: false,
    }),
  });
  const { container } = render(<ToolCallRow event={evt} ticket="WIKI-253" withResult />);
  // Peek row must be absent for failures — the reader needs to see what broke.
  expect(container.querySelector(".session-tool-output-peek")).toBeNull();
  // The failed body renders directly.
  expect(container.querySelector(".transcript-preview")).toBeTruthy();
});

test("running tools (tool.ok === null) render live without collapse", () => {
  const output = longOutput(TOOL_OUTPUT_COLLAPSE_LINES + 2, "live");
  const evt = event(5, {
    tool: tool({
      name: "Bash",
      input: "tail -f logs",
      output,
      ok: null,
    }),
  });
  const { container } = render(<ToolCallRow event={evt} ticket="WIKI-253" withResult />);
  expect(container.querySelector(".session-tool-output-peek")).toBeNull();
  expect(container.querySelector(".transcript-preview")).toBeTruthy();
});

test("edit diffs stay expanded even for long diffs", () => {
  const oldText = longOutput(30, "keep");
  const newText = `${longOutput(15, "keep")}\n${longOutput(20, "added")}`;
  const evt = event(6, {
    tool: tool({
      name: "Edit",
      archetype: "edit",
      input: JSON.stringify({
        file_path: "src/example.ts",
        old_string: oldText,
        new_string: newText,
      }),
      summary: "edit src/example.ts",
      output: "OK",
    }),
  });
  const { container } = render(<ToolCallRow event={evt} ticket="WIKI-253" withResult />);
  // Diffs render through the diff body; no peek row.
  expect(container.querySelector(".session-tool-output-peek")).toBeNull();
  expect(container.querySelector(".session-tool-diff-body")).toBeTruthy();
});

test("failure toggle defaults to expanded — 'hide error' is the initial label", () => {
  const evt = event(7, {
    tool: tool({
      name: "Bash",
      input: "ls",
      output: "boom",
      ok: false,
    }),
  });
  const { container } = render(<ToolCallRow event={evt} ticket="WIKI-253" withResult />);
  const toggle = container.querySelector<HTMLButtonElement>(".session-tool-error-toggle");
  expect(toggle).toBeTruthy();
  expect(toggle?.textContent).toBe("hide error");
  expect(toggle?.getAttribute("aria-expanded")).toBe("true");
});
