// @vitest-environment jsdom
// WIKI-253: long tool outputs default-collapse behind a one-line peek row;
// diffs, error-tone outputs, and live-streaming outputs stay expanded until
// the stream completes. The peek label surfaces top-level JSON keys, array
// counts, bash read paths (middle-truncated so basenames survive), or the
// first non-empty line — the reader can tell what they are hiding before
// they click. The collapse gate itself is pixel-measured, not line-counted,
// so wrap-heavy blocks and structurally short blocks are distinguished.
import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import type { SessionEvent, SessionTool } from "../src/api";
import { ToolCallRow, SessionUiStateContext, type SessionUiState } from "../src/session";
import {
  COLLAPSE_HEIGHT_PX,
  TOOL_OUTPUT_COLLAPSE_LINES,
  middleTruncatePath,
  toolOutputPeek,
} from "../src/transcript-event-utils";

// The vitest setup file installs a ResizeObserver no-op and an offsetHeight
// stub that returns 2× COLLAPSE_HEIGHT_PX for `.session-tool-block-preview-
// measure` — enough to trigger the gate on every long tool output. Real
// browsers use the native APIs; per-test overrides go via data-mock-height
// on the specific node.

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function withUiState(node: React.ReactNode, uiState: SessionUiState = { booleans: new Map(), overrides: new Map() }) {
  return (
    <SessionUiStateContext.Provider value={uiState}>{node}</SessionUiStateContext.Provider>
  );
}

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

// --- middleTruncatePath ---

test("middleTruncatePath preserves the basename on long paths", () => {
  const path = "/Users/henry/me/fun/wiki/backend/src/agents/orchestrator/very-deep/README.md";
  const truncated = middleTruncatePath(path, 40);
  expect(truncated.length).toBeLessThanOrEqual(41); // budget + '…'
  expect(truncated.endsWith("README.md")).toBe(true);
  expect(truncated).toMatch(/^\/Users/);
});

test("middleTruncatePath leaves short paths untouched", () => {
  const short = "src/api.ts";
  expect(middleTruncatePath(short, 40)).toBe(short);
});

test("toolOutputPeek middle-truncates long bash paths and keeps the basename visible", () => {
  const longPath = "/Users/henry/me/fun/wiki/backend/src/agents/orchestrator/deeply/nested/very-long-name/CONFIG.yaml";
  const peek = toolOutputPeek(
    tool({ name: "Bash", input: `cat ${longPath}`, archetype: "bash" }),
    "contents",
  );
  expect(peek.preview).toContain("CONFIG.yaml");
  expect(peek.preview).toContain("…");
});

// --- ToolCallRow: default-collapse peek row (height-driven) ---

test("bash tool output taller than COLLAPSE_HEIGHT_PX renders a peek row", () => {
  const output = longOutput(TOOL_OUTPUT_COLLAPSE_LINES + 5);
  const evt = event(1, {
    tool: tool({ name: "Bash", input: "ls -la /repo", output }),
  });
  const { container } = render(withUiState(<ToolCallRow event={evt} ticket="WIKI-253" withResult />));
  const peek = container.querySelector<HTMLButtonElement>(".session-tool-output-peek");
  expect(peek).toBeTruthy();
  expect(peek?.getAttribute("aria-expanded")).toBe("false");
  const meta = container.querySelector(".session-tool-output-peek-meta");
  expect(meta?.textContent).toMatch(new RegExp(`${TOOL_OUTPUT_COLLAPSE_LINES + 5} lines`));
  // The body block preview is absent while collapsed.
  expect(container.querySelector(".transcript-preview")).toBeNull();
});

test("generic read_agent JSON payload collapses under the shared render-layer gate", () => {
  // read_agent has no GitHub URL, isn't bash, isn't rich-write — under the
  // pre-fix presentation branch this would render inline and never see the
  // collapse gate. The render-layer promotion routes tall inline outputs
  // through the block presentation so the same peek row appears.
  const payload = JSON.stringify(
    { runs: Array.from({ length: 30 }, (_, i) => ({ id: i, status: "done", label: `run-${i}` })) },
    null,
    2,
  );
  const evt = event(11, {
    tool: tool({
      name: "read_agent",
      input: JSON.stringify({ agent_id: "abc" }),
      output: payload,
      archetype: "read",
      summary: "read_agent abc",
    }),
  });
  const { container } = render(withUiState(<ToolCallRow event={evt} ticket="WIKI-253" withResult />));
  expect(container.querySelector(".session-tool-output-peek")).toBeTruthy();
});

test("list_agents tall output collapses through the same gate", () => {
  const payload = JSON.stringify(
    Array.from({ length: 40 }, (_, i) => ({ ticket: `PHO-${i}`, state: "working" })),
    null,
    2,
  );
  const evt = event(12, {
    tool: tool({
      name: "list_agents",
      input: "{}",
      output: payload,
      archetype: "search",
      summary: "list_agents",
    }),
  });
  const { container } = render(withUiState(<ToolCallRow event={evt} ticket="WIKI-253" withResult />));
  expect(container.querySelector(".session-tool-output-peek")).toBeTruthy();
});

test("clicking the peek row expands the body and adds a collapse affordance", () => {
  const output = longOutput(TOOL_OUTPUT_COLLAPSE_LINES + 5);
  const evt = event(2, {
    tool: tool({ name: "Bash", input: "ls", output }),
  });
  const { container } = render(withUiState(<ToolCallRow event={evt} ticket="WIKI-253" withResult />));
  const peek = container.querySelector<HTMLButtonElement>(".session-tool-output-peek");
  expect(peek).toBeTruthy();
  act(() => {
    fireEvent.click(peek!);
  });
  expect(container.querySelector(".session-tool-output-peek")).toBeNull();
  expect(container.querySelector(".transcript-preview")).toBeTruthy();
  const collapse = container.querySelector<HTMLButtonElement>(".session-tool-output-collapse");
  expect(collapse).toBeTruthy();
  act(() => {
    fireEvent.click(collapse!);
  });
  expect(container.querySelector(".session-tool-output-peek")).toBeTruthy();
});

test("threshold is pixel-measured only — line-count and char-count don't participate independently", () => {
  // Pathological shapes: a block with many-but-narrow lines vs one with
  // few-but-wide lines. The mock offsetHeight in the setup approximates
  // `max(lines*20, chars/3)`. The 6-narrow-line block measures ~120px (stays
  // inline), the 3-wide-line block measures ~500px (collapses). Neither
  // line-count nor char-count alone is the gate — only the composite pixel
  // value drives the decision, matching what a real browser does under wrap.
  const narrow = Array.from({ length: 6 }, (_, i) => `x ${i}`).join("\n"); // 6 lines, ~30 chars total
  const narrowEvt = event(30, { tool: tool({ name: "Bash", input: "ls", output: narrow }) });
  const narrowRun = render(withUiState(<ToolCallRow event={narrowEvt} ticket="WIKI-253" withResult />));
  expect(narrowRun.container.querySelector(".session-tool-output-peek")).toBeNull();

  const wideLine = "x".repeat(600);
  const wide = [wideLine, wideLine, wideLine].join("\n"); // 3 lines, ~1800 chars → ~600px
  const wideEvt = event(31, { tool: tool({ name: "Bash", input: "ls", output: wide }) });
  const wideRun = render(withUiState(<ToolCallRow event={wideEvt} ticket="WIKI-253" withResult />));
  expect(wideRun.container.querySelector(".session-tool-output-peek")).toBeTruthy();
  narrowRun.unmount();
  wideRun.unmount();
});

test("short tool outputs (measured under COLLAPSE_HEIGHT_PX) render inline without collapse chrome", () => {
  // The setup's offsetHeight stub approximates ~20px per line, so 3 short
  // lines measure ~60px — well below the 240px threshold — and stay expanded.
  const evt = event(3, {
    tool: tool({ name: "Bash", input: "ls", output: "one\ntwo\nthree" }),
  });
  const { container } = render(withUiState(<ToolCallRow event={evt} ticket="WIKI-253" withResult />));
  expect(container.querySelector(".session-tool-output-peek")).toBeNull();
  expect(container.querySelector(".session-tool-output-collapse")).toBeNull();
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
  const { container } = render(withUiState(<ToolCallRow event={evt} ticket="WIKI-253" withResult />));
  expect(container.querySelector(".session-tool-output-peek")).toBeNull();
  expect(container.querySelector(".transcript-preview")).toBeTruthy();
});

test("structured error with ok:true stays expanded — tone drives the exclusion, not the boolean", () => {
  // Some tools mark structured failures inside the payload while keeping the
  // top-level ok flag true (e.g. a wrapper capturing an exit code inside a
  // harness `<tool_use_error>` segment). The error tone still applies;
  // the block must stay expanded even though ok=true would previously have
  // gated the exclusion.
  const errorPayload = `<tool_use_error>connection refused</tool_use_error>\n${longOutput(TOOL_OUTPUT_COLLAPSE_LINES + 5, "trace")}`;
  const evt = event(15, {
    tool: tool({
      name: "run_wrapper",
      input: "{}",
      output: errorPayload,
      ok: true,
      archetype: "bash",
      summary: "run_wrapper",
    }),
  });
  const { container } = render(withUiState(<ToolCallRow event={evt} ticket="WIKI-253" withResult />));
  expect(container.querySelector(".session-tool-output-peek")).toBeNull();
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
  const { container } = render(withUiState(<ToolCallRow event={evt} ticket="WIKI-253" withResult />));
  expect(container.querySelector(".session-tool-output-peek")).toBeNull();
  expect(container.querySelector(".transcript-preview")).toBeTruthy();
});

test("live stream transitions to collapsed on completion (unless user expanded first)", () => {
  const partial = longOutput(TOOL_OUTPUT_COLLAPSE_LINES + 5, "log");
  const initial = event(20, {
    tool: tool({
      name: "Bash",
      input: "tail -f logs",
      output: partial,
      ok: null,
    }),
  });
  const { container, rerender } = render(withUiState(<ToolCallRow event={initial} ticket="WIKI-253" withResult />));
  // Partial stream: full body renders, no peek.
  expect(container.querySelector(".session-tool-output-peek")).toBeNull();
  expect(container.querySelector(".transcript-preview")).toBeTruthy();
  // Stream completes.
  const finished = event(20, {
    tool: tool({
      name: "Bash",
      input: "tail -f logs",
      output: partial,
      ok: true,
    }),
  });
  rerender(withUiState(<ToolCallRow event={finished} ticket="WIKI-253" withResult />));
  // After completion, the tall block collapses to the peek row.
  expect(container.querySelector(".session-tool-output-peek")).toBeTruthy();
  expect(container.querySelector(".transcript-preview")).toBeNull();
});

test("live stream: user expansion mid-stream survives the completion transition", () => {
  // No peek during running, so simulate user expansion by pre-seeding the
  // override before completion — an explicit UI-state override matches what
  // clicking the peek row would do (once one is present).
  const uiState: SessionUiState = { booleans: new Map(), overrides: new Map() };
  uiState.overrides.set("tool-output:21", true);
  const partial = longOutput(TOOL_OUTPUT_COLLAPSE_LINES + 5, "log");
  const running = event(21, {
    tool: tool({
      name: "Bash",
      input: "tail -f logs",
      output: partial,
      ok: null,
    }),
  });
  const { container, rerender } = render(withUiState(<ToolCallRow event={running} ticket="WIKI-253" withResult />, uiState));
  const finished = event(21, {
    tool: tool({
      name: "Bash",
      input: "tail -f logs",
      output: partial,
      ok: true,
    }),
  });
  rerender(withUiState(<ToolCallRow event={finished} ticket="WIKI-253" withResult />, uiState));
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
  const { container } = render(withUiState(<ToolCallRow event={evt} ticket="WIKI-253" withResult />));
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
  const { container } = render(withUiState(<ToolCallRow event={evt} ticket="WIKI-253" withResult />));
  const toggle = container.querySelector<HTMLButtonElement>(".session-tool-error-toggle");
  expect(toggle).toBeTruthy();
  expect(toggle?.textContent).toBe("hide error");
  expect(toggle?.getAttribute("aria-expanded")).toBe("true");
});

// --- Cross-session isolation ---

test("expansion state is scoped per SessionUiState — numeric ids do not leak across sessions", () => {
  // Two sessions, same numeric event id, independent stores. Expanding in
  // session A must not touch session B.
  const stateA: SessionUiState = { booleans: new Map(), overrides: new Map() };
  const stateB: SessionUiState = { booleans: new Map(), overrides: new Map() };
  const evt = event(100, {
    tool: tool({
      name: "Bash",
      input: "ls",
      output: longOutput(TOOL_OUTPUT_COLLAPSE_LINES + 3),
    }),
  });
  const a = render(withUiState(<ToolCallRow event={evt} ticket="WIKI-253A" withResult />, stateA));
  const b = render(withUiState(<ToolCallRow event={evt} ticket="WIKI-253B" withResult />, stateB));
  const peekA = a.container.querySelector<HTMLButtonElement>(".session-tool-output-peek");
  const peekB = b.container.querySelector<HTMLButtonElement>(".session-tool-output-peek");
  expect(peekA).toBeTruthy();
  expect(peekB).toBeTruthy();
  act(() => {
    fireEvent.click(peekA!);
  });
  // Session A now expanded, session B still collapsed.
  expect(a.container.querySelector(".session-tool-output-peek")).toBeNull();
  expect(b.container.querySelector(".session-tool-output-peek")).toBeTruthy();
  // The override lives in stateA only.
  expect(stateA.overrides.get("tool-output:100")).toBe(true);
  expect(stateB.overrides.has("tool-output:100")).toBe(false);
  a.unmount();
  b.unmount();
});
