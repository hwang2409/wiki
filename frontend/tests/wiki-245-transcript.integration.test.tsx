// WIKI-245 transcript readability contracts: compact evidence, semantic
// labels, one visual unit per tool event, and render-time harness cleanup.
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
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
import { clipSegmentsInline,
  cleanHarnessOutput,
  editDiffFromInput,
  HarnessOutput,
  parseHarnessOutput,
} from "../src/transcript-output";
import { ToolCallRow } from "../src/session";
import normalizedEditFixture from "./fixtures/wiki-245-claude-edit-normalized.json";

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
  expect(outputLabelForTool(tool())).toBe("diff");
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

function renderTool(toolOverrides: Partial<SessionTool> = {}) {
  return render(
    <ToolCallRow
      event={event(7, { tool: tool(toolOverrides) })}
      ticket="WIKI-245"
      withResult
    />,
  );
}

test("real inline rows keep raw output keyboard reachable", () => {
  const { container, getByRole } = renderTool({
    name: "Status",
    archetype: "status",
    summary: "status",
    output: "<tool_use_error>failed status</tool_use_error>\n<system-reminder>quiet note</system-reminder>",
  });
  const row = container.querySelector(".session-tool");
  expect(row?.classList.contains("is-inline")).toBe(true);
  expect(row?.querySelector(".session-output-segment.is-error")?.textContent).toBe("failed status");
  expect(row?.querySelector(".session-output-segment.is-note")?.textContent).toBe("quiet note");
  const raw = getByRole("button", { name: "show raw output" });
  raw.focus();
  expect(document.activeElement).toBe(raw);
  expect(raw.getAttribute("aria-expanded")).toBe("false");
  fireEvent.click(raw);
  expect(raw.getAttribute("aria-expanded")).toBe("true");
  expect(container.querySelector(".session-tool-raw")?.textContent).toContain("<tool_use_error>");
});

test("tier is chosen by tool kind, not multiline output shape", () => {
  const read = renderTool({
    name: "Read",
    archetype: "read",
    summary: "read hot.md",
    output: "one\ntwo\nthree\nfour",
  });
  expect(read.container.querySelector(".session-tool")?.classList.contains("is-inline")).toBe(true);
  expect(read.container.querySelector(".session-tool-inline-result")?.textContent).toBe("(4 lines)");
  expect(read.container.querySelector(".session-tool-body")).toBeNull();
  cleanup();

  const bash = renderTool({
    name: "Bash",
    archetype: "bash",
    summary: "bash echo ok",
    input: "echo ok",
    output: "ok",
  });
  expect(bash.container.querySelector(".session-tool")?.classList.contains("is-block")).toBe(true);
  expect(bash.container.querySelector(".session-tool-block-title")?.textContent).toContain("$ echo ok");
  expect(bash.container.querySelector(".transcript-preview-summary")).toBeNull();
  expect(bash.container.querySelector(".transcript-chip")).toBeNull();
  cleanup();

  const write = renderTool({
    name: "Write",
    archetype: "edit",
    summary: "write hot.md",
    output: "one\ntwo\nthree\nfour",
  });
  expect(write.container.querySelector(".session-tool")?.classList.contains("is-block")).toBe(true);
  // WIKI-249: clipped blocks end in OpenCode's muted "Click to expand" line
  // (session/index.tsx:2083-2085), not a line-count hint.
  const expand = write.getByRole("button", { name: /Click to expand/ });
  expect(expand).toBeTruthy();
  expect(expand.getAttribute("aria-expanded")).toBe("false");
  expect(write.container.querySelector(".session-tool-output-text")?.textContent).toContain("three");
  expect(write.container.querySelector(".session-tool-output-text")?.textContent).not.toContain("four");
});

test("apply_patch input renders its patch body as a diff", () => {
  const { container } = renderTool({
    name: "apply_patch",
    archetype: "edit",
    summary: "apply patch hot.md",
    input: "*** Begin Patch\n*** Update File: hot.md\n@@\n-old\n+new\n*** End Patch",
    output: "Done",
  });
  expect(container.querySelector(".session-tool-diff-body")).toBeTruthy();
  expect(container.querySelector(".diff-file-path")?.textContent).toBe("hot.md");
  expect(container.querySelector(".diff-line.is-remove")?.textContent).toContain("old");
  expect(container.querySelector(".diff-line.is-add")?.textContent).toContain("new");
});

test("edit rows without diff material stay inline", () => {
  const { container } = renderTool({
    name: "Edit",
    archetype: "edit",
    summary: "edit hot.md",
    input: "/tmp/hot.md",
    output: "updated",
  });
  expect(container.querySelector(".session-tool.is-inline")).toBeTruthy();
  expect(container.querySelector(".session-tool-diff-body")).toBeNull();
});

test("a normalized raw Claude edit fixture renders through the real tool row", () => {
  const { container } = render(
    <ToolCallRow
      event={normalizedEditFixture as unknown as SessionEvent}
      ticket="WIKI-245"
      withResult
    />,
  );
  expect(container.querySelector(".diff-line.is-remove")?.textContent).toContain("function classNamesFor");
  expect(container.querySelector(".diff-line.is-add")?.textContent).toContain("export function classNamesFor");
});

test("replacement diffs keep full common ranges but cap visible context", () => {
  const common = Array.from({ length: 10 }, (_, index) => `common-${index}`);
  const diff = editDiffFromInput("hot.md", {
    file_path: "hot.md",
    old_string: [...common, "old", ...common].join("\n"),
    new_string: [...common, "new", ...common].join("\n"),
  });
  expect(diff).toContain("-old");
  expect(diff).toContain("+new");
  expect(diff).not.toContain("-common-");
});

test("long replacement diffs stay within the renderer cap", () => {
  const oldText = Array.from({ length: 500 }, (_, index) => `old-${index}`).join("\n");
  const newText = Array.from({ length: 500 }, (_, index) => `new-${index}`).join("\n");
  const diff = editDiffFromInput("hot.md", { file_path: "hot.md", old_string: oldText, new_string: newText });
  expect(diff).not.toBeNull();
  expect(diff!.length).toBeLessThanOrEqual(60_000);
  expect(diff).toContain("diff truncated");
});

test("malformed and multi-file patches are handled safely", () => {
  expect(editDiffFromInput(
    "apply_patch",
    "*** Begin Patch\n*** Update File: hot.md\n@@\n context only\n*** End Patch",
  )).toBeNull();
  const patch = [
    "*** Begin Patch",
    "*** Update File: one.md",
    "@@",
    "-one",
    "+ONE",
    "*** Update File: two.md",
    "@@",
    "-two",
    "+TWO",
    "*** End Patch",
  ].join("\n");
  const diff = editDiffFromInput("apply_patch", patch);
  expect(diff).toBeNull();
  const manyLines = [
    "*** Begin Patch",
    "*** Update File: many.md",
    "@@",
    ...Array.from({ length: 5_000 }, (_, index) => `+line-${index}`),
    "*** End Patch",
  ].join("\n");
  expect(editDiffFromInput("apply_patch", manyLines)).toBeNull();
});

test("truncated edit payloads use a degraded view instead of a false diff", () => {
  const { container } = renderTool({
    input: "hot.md",
    edit: {
      file_path: "hot.md",
      old_string: "old content",
      new_string: "new content",
      old_string_truncated: true,
    },
    output: "updated",
  });
  expect(container.querySelector(".session-tool-degraded-diff")?.textContent)
    .toContain("edit too large to diff");
  expect(container.querySelector(".diff-line")).toBeNull();
});

test("failed edits show semantic errors before the intended diff", () => {
  const { container } = renderTool({
    input: "hot.md",
    edit: { file_path: "hot.md", old_string: "old", new_string: "new" },
    output: "<tool_use_error>old text was not found</tool_use_error>",
    ok: false,
  });
  // WIKI-253: failure details render expanded from the start — hiding a
  // broken run behind a toggle was the exact scannability regression the
  // ticket flipped.
  expect(container.querySelector(".session-tool-body")).toBeTruthy();
  expect(container.querySelector(".session-output-segment.is-error")?.textContent)
    .toContain("old text was not found");
  expect(container.querySelector(".diff-view")).toBeNull();
});

test("failed edits keep plain failure output primary", () => {
  const { container } = renderTool({
    input: "hot.md",
    edit: { file_path: "hot.md", old_string: "old", new_string: "new" },
    output: "old text was not found",
    ok: false,
  });
  expect(container.querySelector(".session-tool-failure-output")?.textContent)
    .toContain("old text was not found");
  expect(container.querySelector(".diff-view")).toBeNull();
});

test("raw toggle lives in the head and opens a body below it", () => {
  const { container, getByRole } = renderTool({
    name: "Bash",
    archetype: "bash",
    input: "echo ok",
    output: "ok",
  });
  const toggle = getByRole("button", { name: "show raw output" });
  expect(toggle.closest(".session-tool-head")).toBeTruthy();
  expect(container.querySelector(".session-tool-raw")).toBeNull();
  fireEvent.click(toggle);
  const body = container.querySelector(".session-trace-indent .session-tool-raw");
  expect(body).toBeTruthy();
  expect(toggle.getAttribute("aria-expanded")).toBe("true");
});

test("plain failed output renders as error segments", () => {
  const { container } = renderTool({
    input: "hot.md",
    edit: { file_path: "hot.md", old_string: "old", new_string: "new" },
    output: "old text was not found",
    ok: false,
  });
  // WIKI-253: no toggle click — failures render expanded from the start.
  expect(container.querySelector(".session-output-segment.is-error, .harness-output .is-error")?.textContent)
    .toContain("old text was not found");
});

test("generic inline tools render the capped inline output", () => {
  const longLine = "x".repeat(400);
  const { container } = renderTool({
    name: "SomeGenericTool",
    archetype: "generic",
    input: "run",
    output: longLine,
  });
  const inline = container.querySelector(".session-tool-inline-result");
  expect(inline).toBeTruthy();
  expect((inline?.textContent ?? "").length).toBeLessThanOrEqual(120);
  expect(inline?.textContent ?? "").toMatch(/\.\.\.$/);
});

test("failed block output stays visible by default; the toggle can hide it", () => {
  // WIKI-253 flipped this: failures render expanded from the start so a
  // broken run cannot hide behind a chip while the reader scans downstream
  // rows. The hide-error control still exists for readers who want to
  // collapse a known failure.
  const { container, getByRole } = renderTool({
    name: "Bash",
    archetype: "bash",
    input: "false",
    output: "boom",
    ok: false,
  });
  expect(container.querySelector(".session-tool-body")).toBeTruthy();
  fireEvent.click(getByRole("button", { name: "hide error" }));
  expect(container.querySelector(".session-tool-body")).toBeNull();
  fireEvent.click(getByRole("button", { name: "show error" }));
  expect(container.querySelector(".session-tool-body")).toBeTruthy();
});

test("apply_patch with a garbage preamble falls back instead of diffing", () => {
  const patch = [
    "this is not patch grammar",
    "*** Begin Patch",
    "*** Update File: a.txt",
    "@@",
    "-old",
    "+new",
    "*** End Patch",
  ].join("\n");
  const { container } = renderTool({
    name: "apply_patch",
    archetype: "edit",
    input: patch,
    edit: { patch },
    output: "Done",
  });
  expect(container.querySelector(".diff-view, .diff-line")).toBeNull();
});

test("clipSegmentsInline holds the budget at exact-fill and tiny boundaries", () => {
  const seg = (text: string) => ({ kind: "text" as const, text });
  const rendered = (out: Array<{ text: string }>) =>
    out.reduce((n, s, i) => n + s.text.length + (i > 0 ? 1 : 0), 0);

  // 118 + 1 + 1 with separators exceeds 120 -> clipped output stays <= 120.
  const exactFill = clipSegmentsInline([seg("x".repeat(118)), seg("y"), seg("z")], 120);
  expect(rendered(exactFill)).toBeLessThanOrEqual(120);
  expect(exactFill[exactFill.length - 1].text).toMatch(/\.\.\.$/);

  // totals equal to the budget pass through unchanged, no ellipsis.
  const fits = clipSegmentsInline([seg("a".repeat(59)), seg("b".repeat(60))], 120);
  expect(rendered(fits)).toBe(120);
  expect(fits[fits.length - 1].text).not.toMatch(/\.\.\.$/);

  // a first segment that cannot fit a tiny budget still yields an ellipsis.
  const tiny = clipSegmentsInline([seg("overflowing")], 3);
  expect(tiny).toHaveLength(1);
  expect(tiny[0].text).toBe("...");
  expect(rendered(tiny)).toBeLessThanOrEqual(3);

  // clipped segments keep their kind.
  const kinds = clipSegmentsInline([{ kind: "error" as const, text: "e".repeat(200) }], 120);
  expect(kinds[0].kind).toBe("error");
  expect(rendered(kinds)).toBeLessThanOrEqual(120);
});

test("small transcript controls keep the 24px target baseline", () => {
  const css = readFileSync(
    resolve(dirname(fileURLToPath(import.meta.url)), "..", "src", "styles.css"),
    "utf-8",
  );
  const block = css.slice(css.indexOf("small transcript controls keep a 24px minimum target"));
  for (const selector of [
    ".session-tool-error-toggle",
    ".session-tool-raw-toggle",
    ".transcript-preview-more",
    ".session-thinking-head",
  ]) {
    expect(block).toContain(selector);
  }
  expect(block).toContain("min-height: 24px;");
  expect(css).toContain('.session-tool-head:hover .session-tool-raw-toggle');
  expect(css).toContain('.session-tool-raw-toggle[aria-expanded="true"]');
});
