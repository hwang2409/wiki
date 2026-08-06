// WIKI-251 contracts:
//  1) file-edit tools default to side-by-side (deletions LEFT, insertions RIGHT).
//  2) transcript surfaces wrap — no horizontal-scroll min-widths, cells break
//     long tokens with overflow-wrap: anywhere.
//  3) chat pane default sits at ~45% of viewport, not a fixed 480px.
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test } from "vitest";

import { DiffPatchView } from "../src/diff-view";
import { ToolCallRow } from "../src/session";
import type { SessionEvent, SessionTool } from "../src/api";

const CSS_SOURCE = readFileSync(
  resolve(dirname(fileURLToPath(import.meta.url)), "..", "src", "styles.css"),
  "utf-8",
);
const CSS_WITHOUT_COMMENTS = CSS_SOURCE.replace(/\/\*[\s\S]*?\*\//g, "");

function cssRuleBodies(selector: string): string[] {
  const bodies = [...CSS_WITHOUT_COMMENTS.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
    .filter((match) =>
      match[1]
        .split(",")
        .some((entry) => entry.trim() === selector),
    )
    .map((match) => match[2]);
  expect(bodies.length).toBeGreaterThan(0);
  return bodies;
}

function cssDeclarations(selector: string): string {
  return cssRuleBodies(selector).join("\n");
}

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

// Realistic hot.md-style hunk (one long line, matches the wiki-251 screenshot
// — a single-line edit deep inside a paragraph, with context above/below).
const HOT_MD_PATCH = [
  "--- a/vault/hot.md",
  "+++ b/vault/hot.md",
  "@@ -8,7 +8,7 @@",
  " ## Active threads",
  " ",
  ' - **WIKI (orch `wiki-dev`)**: OpenCode UI arc marching along.',
  '-    - PR #185 landed transcript follow-ups (spacing model, live-state anchor, data-level caps) but has known regressions in hunk chrome and the "polished" toggle that need triage before we merge #186.',
  '+    - PR #186 landed the 1:1 fidelity + polish wave (fable-5, 10-item gap table), superseding the #185 regressions Henry flagged; transcript now honors fence tags, structures embedded heredocs, and infers file-slice output.',
  " ",
  " ## Watchouts",
  " ",
].join("\n");

function editEvent(): SessionEvent {
  return {
    id: 42,
    kind: "tool",
    ts: null,
    text: "",
    disposition: "rendered",
    tool: {
      name: "Edit",
      input: JSON.stringify({
        file_path: "vault/hot.md",
        old_string:
          'PR #185 landed transcript follow-ups (spacing model, live-state anchor, data-level caps) but has known regressions in hunk chrome and the "polished" toggle that need triage before we merge #186.',
        new_string:
          "PR #186 landed the 1:1 fidelity + polish wave (fable-5, 10-item gap table), superseding the #185 regressions Henry flagged; transcript now honors fence tags, structures embedded heredocs, and infers file-slice output.",
        replace_all: false,
      }),
      output: "File updated.",
      ok: true,
      archetype: "edit",
      summary: "edit hot.md",
    } satisfies SessionTool,
  };
}

describe("WIKI-251 split-diff default", () => {
  test("DiffPatchView with viewType=split emits paired rows (removes left, adds right)", () => {
    const { container } = render(<DiffPatchView source={HOT_MD_PATCH} viewType="split" />);
    // The container is marked so callers/tests can distinguish modes.
    expect(container.querySelector(".diff-view.is-split")).toBeTruthy();
    expect(container.querySelector(".diff-hunk-body.is-split")).toBeTruthy();
    // A remove sits on the left cell, an add on the right cell — exactly the
    // "before | after" ordering Henry called out.
    const removes = container.querySelectorAll(".diff-split-cell.is-side-left.is-remove");
    const adds = container.querySelectorAll(".diff-split-cell.is-side-right.is-add");
    expect(removes.length).toBeGreaterThan(0);
    expect(adds.length).toBeGreaterThan(0);
    // Every context row shows on BOTH sides (one row = two cells in the grid).
    const contextLeft = container.querySelectorAll(".diff-split-cell.is-side-left.is-context").length;
    const contextRight = container.querySelectorAll(".diff-split-cell.is-side-right.is-context").length;
    expect(contextLeft).toBe(contextRight);
    expect(contextLeft).toBeGreaterThan(0);
  });

  test("DiffPatchView defaults to unified when viewType is omitted (back-compat)", () => {
    const { container } = render(<DiffPatchView source={HOT_MD_PATCH} />);
    expect(container.querySelector(".diff-view.is-unified")).toBeTruthy();
    expect(container.querySelector(".diff-hunk-body.is-split")).toBeNull();
  });

  test("edit tool in the transcript routes through split view by default", () => {
    const { container } = render(<ToolCallRow event={editEvent()} ticket="WIKI-251" withResult />);
    expect(container.querySelector(".diff-view.is-split")).toBeTruthy();
    expect(container.querySelector(".diff-split-cell.is-side-left")).toBeTruthy();
    expect(container.querySelector(".diff-split-cell.is-side-right")).toBeTruthy();
  });
});

describe("WIKI-251 wrap sweep", () => {
  test("diff lines wrap on the inline axis (no more white-space: pre or min-width: max-content)", () => {
    const line = cssDeclarations(".diff-line");
    expect(line).toContain("white-space: pre-wrap;");
    expect(line).toContain("overflow-wrap: anywhere;");
    expect(line).not.toContain("white-space: pre;");

    const code = cssDeclarations(".diff-code");
    expect(code).toContain("white-space: pre-wrap;");
    expect(code).toContain("overflow-wrap: anywhere;");

    // The old horizontal-scroll scaffolding is gone.
    expect(CSS_WITHOUT_COMMENTS).not.toMatch(/\.diff-file-body\s*\{[^}]*overflow-x:\s*auto/);
    expect(CSS_WITHOUT_COMMENTS).not.toMatch(/\.diff-hunk-body\s*\{[^}]*min-width:\s*max-content/);
  });

  test("split cells sit in a two-column grid with minmax(0, 1fr) — long code wraps in place", () => {
    const grid = cssDeclarations(".diff-hunk-body.is-split");
    expect(grid).toContain("display: grid;");
    expect(grid).toContain("grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);");
    expect(grid).toContain("min-width: 0;");
  });

  test("markdown pre/code fences wrap by default (no per-block horizontal scroll)", () => {
    const preMd = cssDeclarations(".markdown-preview-view pre code");
    expect(preMd).toContain("white-space: pre-wrap;");
    expect(preMd).toContain("overflow-wrap: anywhere;");

    const shiki = cssDeclarations(".shiki-block pre code");
    expect(shiki).toContain("white-space: pre-wrap;");
    expect(shiki).toContain("overflow-wrap: anywhere;");

    // No overflow-x on the pre containers themselves either.
    expect(CSS_WITHOUT_COMMENTS).not.toMatch(/\.markdown-preview-view pre\s*\{[^}]*overflow-x:\s*auto/);
    expect(CSS_WITHOUT_COMMENTS).not.toMatch(/\.shiki-block pre\s*\{[^}]*overflow-x:\s*auto/);
  });

  test("markdown tables wrap inside their pane instead of scrolling", () => {
    const table = cssDeclarations(".markdown-preview-view table");
    expect(table).toContain("width: 100%;");
    expect(table).toContain("table-layout: fixed;");
    const bodies = cssRuleBodies(".markdown-preview-view td");
    expect(bodies.some((body) => body.includes("overflow-wrap: anywhere;"))).toBe(true);
    // The scroll container no longer forces overflow-x.
    expect(CSS_WITHOUT_COMMENTS).not.toMatch(/\.markdown-table-scroll\s*\{[^}]*overflow-x:\s*auto/);
  });

  test("tool preview body wraps long tokens and drops overflow-x", () => {
    const body = cssDeclarations(".transcript-preview-body");
    expect(body).toContain("overflow-wrap: anywhere;");
    expect(body).not.toContain("overflow-x: auto;");
  });

  test("codex-stream diff lines wrap like the transcript diff", () => {
    const line = cssDeclarations(".codex-stream-diff-line");
    expect(line).toContain("white-space: pre-wrap;");
    expect(line).toContain("overflow-wrap: anywhere;");
    expect(line).not.toContain("min-width: max-content;");
  });
});
