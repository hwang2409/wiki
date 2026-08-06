// WIKI-261: syntax-highlight numbered read outputs. Verifies the render
// layer strips the cat -n / arrow gutter, tokenizes the clean code, and
// paints the gutter as a muted non-selectable column beside it. Mixed /
// unparseable payloads fall back to plain rendering (no highlight
// regression).
import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, test } from "vitest";

import type { SessionEvent, SessionTool } from "../src/api";
import { ToolCallRow } from "../src/session";

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
});

function tool(overrides: Partial<SessionTool> = {}): SessionTool {
  return {
    name: "Read",
    input: "{\"file_path\": \"frontend/src/api.ts\"}",
    output: "",
    ok: true,
    archetype: "read",
    summary: "read api.ts",
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

// Force the block-shape render path (past the 12-line promotion threshold
// so ToolOutputBody carries the numbered-read branch).
function longNumberedPayload(): string {
  const lines: string[] = [];
  for (let i = 0; i < 14; i += 1) {
    const num = 740 + i;
    lines.push(`${num}\texport const x${i} = ${i};`);
  }
  return lines.join("\n");
}

test("numbered read outputs render a muted, non-selectable gutter column", () => {
  const readTool = tool({ output: longNumberedPayload() });
  const { container } = render(
    <ToolCallRow event={event(1, { tool: readTool })} ticket="WIKI-261" withResult />,
  );
  const block = container.querySelector(".numbered-read");
  expect(block).toBeTruthy();
  expect(block?.getAttribute("data-lang")).toBe("typescript");
  const gutterCells = block!.querySelectorAll(".numbered-read-gutter");
  expect(gutterCells.length).toBe(14);
  expect(gutterCells[0].textContent).toBe("740");
  expect(gutterCells[13].textContent).toBe("753");
  // Gutter must be aria-hidden so screen readers announce only code.
  expect(gutterCells[0].getAttribute("aria-hidden")).toBe("true");
  // Code cells carry only the stripped code, no line numbers.
  const codeCells = block!.querySelectorAll(".numbered-read-code");
  expect(codeCells[0].textContent).toBe("export const x0 = 0;");
  expect(codeCells[0].textContent).not.toContain("740");
});

test("gutter column width scales with the widest line number", () => {
  // Force block-shape by exceeding the 12-line promotion threshold, so the
  // numbered-read view renders in the default (non-polished) path.
  const lines: string[] = [];
  for (let i = 0; i < 14; i += 1) {
    const num = 1 + i * 100;
    lines.push(`${num}\texport const x${i} = ${i};`);
  }
  const readTool = tool({ output: lines.join("\n") });
  const { container } = render(
    <ToolCallRow event={event(2, { tool: readTool })} ticket="WIKI-261" withResult />,
  );
  const block = container.querySelector(".numbered-read");
  expect(block).toBeTruthy();
  // Widest number 1301 → 4ch.
  expect((block as HTMLElement).style.getPropertyValue("--numbered-read-gutter-width")).toBe("4ch");
});

test("mixed / unparseable payloads fall back to the plain segment renderer", () => {
  // A leading harness message before the numbered block is a mixed payload:
  // the parser returns null and the block renders through the existing
  // segment path (no numbered-read column, no misaligned gutter).
  const mixed = "[note] partial dump follows\n740\texport const a = 1;\n741\texport const b = 2;";
  const long = mixed + "\n" + Array.from({ length: 12 }, (_, i) => `  extra line ${i}`).join("\n");
  const readTool = tool({ output: long });
  const { container } = render(
    <ToolCallRow event={event(3, { tool: readTool })} ticket="WIKI-261" withResult />,
  );
  expect(container.querySelector(".numbered-read")).toBeNull();
  // The plain segment renderer emits the leading text somewhere in the row.
  expect(container.textContent ?? "").toContain("[note] partial dump follows");
});

test("outputs without a filetype hint stay in the plain path", () => {
  // No file extension → no lang hint → NumberedReadHighlight not offered
  // (highlighting the code column would be a no-op).
  const noExt = tool({
    input: "{\"file_path\": \"frontend/src/NOTES\"}",
    summary: "read NOTES",
    output: longNumberedPayload(),
  });
  const { container } = render(
    <ToolCallRow event={event(4, { tool: noExt })} ticket="WIKI-261" withResult />,
  );
  expect(container.querySelector(".numbered-read")).toBeNull();
});

test("blank lines inside a numbered payload preserve empty rows with no fake number", () => {
  // Force the block path by padding out to the promotion threshold. The
  // second row is blank; the gutter cell for it must be empty (num=0).
  const rows = ["740\texport {};", "", "742\texport const y = 2;"];
  for (let i = 0; i < 12; i += 1) {
    rows.push(`${743 + i}\texport const y${i} = ${i};`);
  }
  const readTool = tool({ output: rows.join("\n") });
  const { container } = render(
    <ToolCallRow event={event(5, { tool: readTool })} ticket="WIKI-261" withResult />,
  );
  const gutterCells = container.querySelectorAll(".numbered-read-gutter");
  expect(gutterCells.length).toBeGreaterThanOrEqual(3);
  expect(gutterCells[0].textContent).toBe("740");
  expect(gutterCells[1].textContent).toBe("");
  expect(gutterCells[2].textContent).toBe("742");
});

test("polished toggle over a numbered payload renders the gutter-and-code view", () => {
  // For short read outputs (below the block-promotion threshold), the
  // polished disclosure IS the path that renders the numbered-read view.
  const readTool = tool({ output: "1\tconst a = 1;\n2\tconst b = 2;" });
  const { container, getByRole } = render(
    <ToolCallRow event={event(6, { tool: readTool })} ticket="WIKI-261" withResult />,
  );
  fireEvent.click(getByRole("button", { name: "show polished output" }));
  const block = container.querySelector(".numbered-read");
  expect(block).toBeTruthy();
  expect(block?.getAttribute("data-lang")).toBe("typescript");
});
