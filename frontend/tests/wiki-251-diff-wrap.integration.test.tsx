// WIKI-251 contracts:
//  1) file-edit tools default to side-by-side (deletions LEFT, insertions RIGHT).
//  2) transcript surfaces wrap — no horizontal-scroll min-widths, cells break
//     long tokens with overflow-wrap: anywhere.
//  3) chat pane default sits at ~45-50% of viewport, not a fixed 480px.
//  4) split-diff rows pair the old cell (left) with the new cell (right) in
//     the DOM — a regression that unpaired them (removes in one column,
//     adds in another) must fail here.
//  5) the raw/polished toggle for edits: polished branch = SplitDiffView,
//     raw branch = <pre> with the actual patch source (not the tool ack).
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test } from "vitest";

import { SplitDiffView } from "../src/split-diff";
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

const LONG_OLD =
  'PR #185 landed transcript follow-ups (spacing model, live-state anchor, data-level caps) but has known regressions in hunk chrome and the "polished" toggle that need triage before we merge #186.';
const LONG_NEW =
  "PR #186 landed the 1:1 fidelity + polish wave (fable-5, 10-item gap table), superseding the #185 regressions Henry flagged; transcript now honors fence tags, structures embedded heredocs, and infers file-slice output.";

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
        old_string: LONG_OLD,
        new_string: LONG_NEW,
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
  test("SplitDiffView (react-diff-view) renders a two-column split for a modified line", () => {
    const { container } = render(
      <SplitDiffView
        className="test-diff"
        emptyClassName="test-diff-empty"
        emptyMessage="No diff to display."
        patch={HOT_MD_PATCH}
      />,
    );
    // Every rendered file wears the shared container class.
    const diffs = container.querySelectorAll(".test-diff");
    expect(diffs.length).toBe(1);

    // react-diff-view emits a <table> per file with a split-view <tbody>.
    // The class marker on inserted/deleted cells is diff-code-insert /
    // diff-code-delete; the paired gutters are diff-gutter-delete /
    // diff-gutter-insert. Presence of BOTH proves side-by-side rendering.
    const deletes = container.querySelectorAll(".diff-code-delete");
    const inserts = container.querySelectorAll(".diff-code-insert");
    expect(deletes.length).toBeGreaterThan(0);
    expect(inserts.length).toBeGreaterThan(0);
  });

  test("split-diff pairs old/new cells within the same DOM row", () => {
    const { container } = render(
      <SplitDiffView
        className="test-diff"
        emptyClassName="test-diff-empty"
        emptyMessage="No diff to display."
        patch={HOT_MD_PATCH}
      />,
    );

    // Every row that mentions a delete cell must ALSO contain an insert or an
    // empty-side cell — the two are DOM siblings inside the same <tr>. A
    // regression that emitted deletes in one column and inserts in another
    // would violate this pairing.
    const rows = container.querySelectorAll("tr.diff-line");
    expect(rows.length).toBeGreaterThan(0);

    let sawPairedChange = false;
    for (const row of Array.from(rows)) {
      const deleteCell = row.querySelector(".diff-code-delete");
      const insertCell = row.querySelector(".diff-code-insert");
      if (deleteCell && insertCell) {
        // Both sides present in one row — the change row Henry called out.
        // Their DOM order must be delete-first (left column) then insert
        // (right column).
        const cells = Array.from(row.querySelectorAll("td.diff-code"));
        const deleteIndex = cells.indexOf(deleteCell);
        const insertIndex = cells.indexOf(insertCell);
        expect(deleteIndex).toBeGreaterThanOrEqual(0);
        expect(insertIndex).toBeGreaterThanOrEqual(0);
        expect(deleteIndex).toBeLessThan(insertIndex);
        sawPairedChange = true;
      }
    }
    expect(sawPairedChange).toBe(true);
  });

  test("edit tool in the transcript routes through split view by default", () => {
    const { container } = render(<ToolCallRow event={editEvent()} ticket="WIKI-251" withResult />);
    // The tool renders via SplitDiffView (session-tool-split-diff wraps it).
    expect(container.querySelector(".session-tool-split-diff")).toBeTruthy();
    // Split rendering emitted BOTH delete and insert cells (side-by-side).
    expect(container.querySelectorAll(".diff-code-delete").length).toBeGreaterThan(0);
    expect(container.querySelectorAll(".diff-code-insert").length).toBeGreaterThan(0);
  });
});

describe("WIKI-251 raw/polished toggle for edits", () => {
  test("polished branch (default) renders SplitDiffView; raw branch renders <pre> with the patch source", () => {
    const { container } = render(<ToolCallRow event={editEvent()} ticket="WIKI-251" withResult />);

    // Polished branch — split view is visible; the raw pane is NOT.
    expect(container.querySelector(".session-tool-split-diff")).toBeTruthy();
    expect(container.querySelector(".session-tool-raw")).toBeNull();

    // Toggle the raw disclosure.
    const rawButton = container.querySelector<HTMLButtonElement>(".session-tool-raw-toggle");
    expect(rawButton).toBeTruthy();
    act(() => {
      fireEvent.click(rawButton!);
    });

    // Raw branch — the split view is hidden, and a <pre> with the actual
    // patch source (not the "File updated." ack) is rendered.
    expect(container.querySelector(".session-tool-split-diff")).toBeNull();
    const rawBlock = container.querySelector(".session-tool-raw");
    expect(rawBlock).toBeTruthy();
    const pre = rawBlock!.querySelector("pre");
    expect(pre).toBeTruthy();
    const rawText = pre!.textContent ?? "";
    // The patch's headers and the edited strings must appear verbatim.
    expect(rawText).toContain("--- a/vault/hot.md");
    expect(rawText).toContain("+++ b/vault/hot.md");
    expect(rawText).toContain(LONG_NEW.slice(0, 40));
    // The tool ack "File updated." must NOT be what raw shows.
    expect(rawText).not.toBe("File updated.");
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

  test("react-diff-view cells wrap in place — long code stays in-container", () => {
    const code = cssDeclarations(".wiki-diff .diff-code");
    expect(code).toContain("white-space: pre-wrap;");
    expect(code).toContain("overflow-wrap: anywhere;");
    const table = cssDeclarations(".wiki-diff");
    expect(table).toContain("table-layout: fixed;");
    expect(table).toContain("width: 100%;");
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

  test("artifact table cells wrap in place at container width", () => {
    const shell = cssDeclarations(".artifact-table-scroll");
    // The scroll shell drops horizontal scroll — only vertical remains for
    // very tall tables with sticky headers.
    expect(shell).not.toContain("overflow: auto;");
    expect(shell).not.toContain("overflow-x: auto;");

    const table = cssDeclarations(".artifact-table");
    expect(table).toContain("table-layout: fixed;");

    // The shared td/th selector emits ONE rule body under the multi-selector
    // form (".artifact-table th, .artifact-table td"). Locate it by scanning
    // for the joined selector — cssRuleBodies matches an exact single
    // selector, which won't hit the comma-joined form.
    const source = readFileSync(
      resolve(dirname(fileURLToPath(import.meta.url)), "..", "src", "styles.css"),
      "utf-8",
    );
    const cellRule = source
      .split(/\/\*[\s\S]*?\*\//g).join("")
      .match(/\.artifact-table\s+th\s*,\s*\.artifact-table\s+td\s*\{([^{}]+)\}/);
    expect(cellRule).toBeTruthy();
    expect(cellRule![1]).toContain("overflow-wrap: anywhere;");
    expect(cellRule![1]).not.toMatch(/white-space:\s*nowrap;/);
  });

  test("artifact JSON wraps rather than scrolling horizontally", () => {
    const body = cssDeclarations(".artifact-json");
    expect(body).toContain("white-space: pre-wrap;");
    expect(body).toContain("overflow-wrap: anywhere;");
    expect(body).not.toContain("overflow: auto;");
  });

  test("no .is-nowrap escape hatch remains in the CSS (WIKI-251 HIGH#3)", () => {
    // The class name and every consumer are removed — nothing in the app can
    // fall back to horizontal-scroll on tool output.
    expect(CSS_WITHOUT_COMMENTS).not.toMatch(/\.is-nowrap\b/);
    // And the JSX components no longer emit the class.
    const preview = readFileSync(
      resolve(dirname(fileURLToPath(import.meta.url)), "..", "src", "transcript-preview.tsx"),
      "utf-8",
    );
    expect(preview).not.toMatch(/is-nowrap/);
  });
});
