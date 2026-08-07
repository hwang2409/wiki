// @vitest-environment jsdom
// WIKI-270: bash tool outputs that happen to be JSON (e.g. `gh api graphql`)
// render as pretty-printed, syntax-highlighted JSON in the block view instead
// of a one-line escape wall. Non-JSON / truncated / ANSI-decorated payloads
// keep the existing raw renderer, and oversized payloads skip parsing.
import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import type { SessionEvent, SessionTool } from "../src/api";
import { ToolCallRow } from "../src/session";
import { prettyPrintedJson } from "../src/transcript-event-utils";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function bashTool(overrides: Partial<SessionTool> = {}): SessionTool {
  return {
    name: "Bash",
    input: "gh api graphql -f query='{}'",
    output: "",
    ok: true,
    archetype: "bash",
    summary: "$ gh api graphql",
    ...overrides,
  };
}

function event(id: number, tool: SessionTool): SessionEvent {
  return { id, kind: "tool", ts: null, text: "", disposition: "rendered", tool };
}

function outputText(container: HTMLElement): string {
  return container.querySelector(".session-tool-output-text")?.textContent ?? "";
}

test("bash JSON output pretty-prints and decodes unicode escapes in strings", () => {
  // Real `gh api graphql` output: single-line JSON with `<` unicode
  // escapes because GitHub returns HTML fragments. Pretty-printing must land
  // 2-space indent and JSON.parse must decode `<` to a literal `<`.
  const rawJson = '{"data":{"repository":{"name":"wiki"}},"body":"\\u003cdiv\\u003ehi\\u003c/div\\u003e"}';
  const { container } = render(
    <ToolCallRow event={event(1, bashTool({ output: rawJson }))} ticket="WIKI-270" withResult />,
  );
  const body = outputText(container);
  expect(body).toContain('{\n  "data"');
  expect(body).toContain('    "repository"');
  expect(body).toContain('"<div>hi</div>"');
  expect(body).not.toContain("\\u003c");
  // Pretty JSON body is Shiki-highlighted with a `json` language tag.
  const highlighted = container
    .querySelector(".session-tool-output-text .syntax-inline")
    ?.getAttribute("data-lang");
  expect(highlighted).toBe("json");
});

test("bash non-JSON output falls back to the raw renderer", () => {
  const notJson = "total 12\ndrwxr-xr-x  4 user  staff  128 Aug  7 12:00 .\n";
  const { container } = render(
    <ToolCallRow event={event(2, bashTool({ input: "ls -la", output: notJson }))} ticket="WIKI-270" withResult />,
  );
  const body = outputText(container);
  expect(body).toContain("total 12");
  // No syntax highlighting inside the OUTPUT block — the command in the
  // header is highlighted separately and does not count.
  expect(container.querySelector(".session-tool-output-text .syntax-inline")).toBeNull();
});

test("bash JSON prefix with a truncation marker stays raw (parse fails)", () => {
  // Backend appends `… [N chars truncated]` to over-long tool outputs. The
  // remaining prefix is cut mid-value and will not parse — render raw rather
  // than partially guessing.
  const truncated = '{"data":{"repository":{"name":"wiki"';
  const marker = "\n… [1727 chars truncated]";
  const { container } = render(
    <ToolCallRow event={event(3, bashTool({ output: truncated + marker }))} ticket="WIKI-270" withResult />,
  );
  const body = outputText(container);
  expect(body).toContain('{"data"');
  expect(body).toContain("chars truncated");
  expect(container.querySelector(".session-tool-output-text .syntax-inline")).toBeNull();
});

test("ANSI-decorated bash output skips JSON parsing", () => {
  // Some bash tools (npm, git) mix ANSI color codes into their output. Those
  // must never be handed to JSON.parse — the ANSI branch keeps the color
  // information the reader would otherwise lose.
  const esc = String.fromCharCode(0x1b);
  const ansi = `${esc}[32m{"ok":true}${esc}[0m`;
  const { container } = render(
    <ToolCallRow event={event(4, bashTool({ input: "npm --json", output: ansi }))} ticket="WIKI-270" withResult />,
  );
  const body = outputText(container);
  // Not re-serialized into a pretty JSON block — the raw braces stay together.
  expect(body).not.toContain('{\n  "ok"');
  expect(container.querySelector(".session-tool-output-text .syntax-inline")).toBeNull();
});

test("prettyPrintedJson skips payloads above the 1 MiB cap", () => {
  // detectStructuredContent caps at 1 MiB so JSON.parse cannot block the
  // render frame on multi-megabyte payloads. The cap lives in
  // transcript-event-utils; the render layer just consumes its verdict.
  const big = '{"tail":"' + "a".repeat(1_100_000) + '"}';
  expect(prettyPrintedJson(big)).toBeNull();
  // A payload well under the cap still pretty-prints.
  const small = '{"ok":true,"n":1}';
  expect(prettyPrintedJson(small)).toBe('{\n  "ok": true,\n  "n": 1\n}');
});
