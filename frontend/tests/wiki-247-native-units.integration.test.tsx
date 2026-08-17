// WIKI-247 contracts: no aggregated turn header — every tool call and
// thinking trace is its own visual unit; the composer has no 4-sided
// outline; block interiors are two-tone mono with reference-grade diffs.
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test } from "vitest";

import type { SessionEvent, SessionTool } from "../src/api";
import { ActivityEventRow, ToolCallRow } from "../src/session";
import { buildVirtualLayout, eventRows, VIRTUAL_ROW_GAP } from "../src/session-layout";
import normalizedEditFixture from "./fixtures/wiki-245-claude-edit-normalized.json";

const CSS_SOURCE = readFileSync(
  resolve(dirname(fileURLToPath(import.meta.url)), "..", "src", "styles.css"),
  "utf-8",
);
const CSS_WITHOUT_COMMENTS = CSS_SOURCE.replace(/\/\*[\s\S]*?\*\//g, "");

function cssRuleBodies(selector: string): string[] {
  const bodies = [...CSS_WITHOUT_COMMENTS.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
    .filter((match) => match[1].split(",").some((entry) => entry.trim() === selector))
    .map((match) => match[2]);
  expect(bodies.length).toBeGreaterThan(0);
  return bodies;
}

function cssDeclarations(selector: string): string {
  return cssRuleBodies(selector).join("\n");
}

function finalCssDeclarations(selector: string): string {
  const bodies = cssRuleBodies(selector);
  return bodies[bodies.length - 1];
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

function tool(overrides: Partial<SessionTool> = {}): SessionTool {
  return {
    name: "Read",
    input: "src/api.ts",
    output: "one\ntwo",
    ok: true,
    archetype: "read",
    summary: "read api.ts",
    ...overrides,
  };
}

function toolEvent(id: number, overrides: Partial<SessionTool> = {}): SessionEvent {
  return {
    id,
    kind: "tool",
    ts: null,
    text: "",
    disposition: "rendered",
    tool: tool(overrides),
  };
}

function thinkingEvent(id: number, text = "weighing the options here"): SessionEvent {
  return {
    id,
    kind: "thinking",
    ts: null,
    text,
    disposition: "rendered",
  };
}

describe("per-event transcript units", () => {
  test("no aggregated group header renders", () => {
    const { container } = render(
      <div>
        <ActivityEventRow event={toolEvent(1)} rowKey={1} ticket="WIKI-247" />
        <ActivityEventRow
          event={toolEvent(2, { summary: "grep pattern", archetype: "search" })}
          rowKey={2}
          ticket="WIKI-247"
        />
        <ActivityEventRow event={thinkingEvent(3)} rowKey={3} ticket="WIKI-247" />
      </div>,
    );
    expect(container.querySelector(".session-activity-head")).toBeNull();
    expect(container.querySelector(".session-activity-body")).toBeNull();
    expect(container.querySelector(".session-collapsible")).toBeNull();
    expect(container.textContent).not.toMatch(/\d+ tool calls?/);
    expect(container.textContent).not.toMatch(/\d+ thinking/);
    expect(container.textContent).not.toContain("DONE");
  });

  test("each tool call and thinking trace is a direct child unit", () => {
    const { container } = render(
      <div>
        {[
          toolEvent(1),
          toolEvent(2, { summary: "grep pattern", archetype: "search" }),
          thinkingEvent(3),
          toolEvent(4, { name: "Bash", archetype: "bash", summary: "bash echo", input: "echo hi", output: "hi" }),
        ].map((event) => <ActivityEventRow event={event} key={event.id} rowKey={event.id} ticket="WIKI-247" />)}
      </div>,
    );
    const units = [...container.querySelectorAll(".session-activity")];
    expect(units).toHaveLength(4);
    expect(units.every((unit) => unit.children.length === 1)).toBe(true);
    expect(units.filter((unit) => unit.querySelector(".is-reasoning"))).toHaveLength(1);
    expect(units.filter((unit) => unit.querySelector(".is-tool"))).toHaveLength(3);
  });

  test("the activity container carries no box chrome and layout owns row rhythm", () => {
    const activity = cssDeclarations(".session-activity");
    expect(activity).not.toMatch(/(?:^|\n)\s*border\s*:/);
    expect(activity).not.toMatch(/(?:^|\n)\s*background/);
    expect(CSS_WITHOUT_COMMENTS).not.toContain(".session-activity > .session-activity-row.is-block");
    const rows = eventRows([
      toolEvent(1, { output: "one" }),
      toolEvent(2, { output: "two" }),
      toolEvent(3, { name: "Bash", archetype: "bash", output: "three" }),
      thinkingEvent(4),
      { id: 5, kind: "assistant", ts: null, text: "prose", disposition: "rendered" },
    ], 0);
    const heights = new Map(rows.map((row) => [row.key, { refs: [row.event], height: 20 }]));
    const layout = buildVirtualLayout(rows, heights);
    expect(layout.tops.slice(1).map((top, index) => top - layout.tops[index])).toEqual([
      20,
      20 + VIRTUAL_ROW_GAP,
      20 + VIRTUAL_ROW_GAP,
      20 + VIRTUAL_ROW_GAP,
    ]);
  });

  test("trace rows have no tree connectors", () => {
    expect(CSS_SOURCE).not.toContain(".session-trace-connector");
    const { container } = render(
      <div>
        <ActivityEventRow event={toolEvent(1)} rowKey={1} ticket="WIKI-247" />
        <ActivityEventRow event={toolEvent(2)} rowKey={2} ticket="WIKI-247" />
      </div>,
    );
    expect(container.textContent).not.toContain("├");
    expect(container.textContent).not.toContain("└");
  });
});

describe("quiet composer", () => {
  test("the composer row is a rounded 4-sided card (WIKI-293 bb reskin)", () => {
    // WIKI-247 originally locked the composer to a left-bar strip. The
    // WIKI-293 bb reskin replaces that with a rounded card: a full 1px
    // hairline on all four edges, matching border-radius, and a card
    // shadow. The left-bar accent is gone — focus lifts the whole ring.
    const row = cssDeclarations(".session-composer-row");
    expect(row).toMatch(/border-top:\s*1px/);
    expect(row).toMatch(/border-right:\s*1px/);
    expect(row).toMatch(/border-bottom:\s*1px/);
    expect(row).toMatch(/border-left:\s*1px/);
    expect(row).toMatch(/border-radius:\s*var\(--composer-radius\)/);
    expect(row).not.toMatch(/(?:^|\n)\s*outline\s*:/);
  });

  test("textarea focus never draws the outline box; focus lifts the card", () => {
    const focusRules = cssDeclarations(".session-composer textarea:focus");
    expect(focusRules).toContain("outline: none;");
    expect(focusRules).toContain("outline: auto;");
    // WIKI-293: the visible focus state lives on the card — every edge
    // shifts to the active border color, plus a soft ring via box-shadow.
    const focusWithin = cssDeclarations(".session-composer-row:focus-within");
    expect(focusWithin).toMatch(/border-top-color:\s*var\(--border-active\)/);
    expect(focusWithin).toMatch(/border-left-color:\s*var\(--border-active\)/);
    expect(focusWithin).toMatch(/box-shadow:/);
    expect(focusWithin).toMatch(/background-color:/);
  });
});

describe("native block interiors", () => {
  test("bash blocks read two-tone: command in text color, output muted", () => {
    const { container } = render(
      <ToolCallRow
        event={toolEvent(7, {
          name: "Bash",
          archetype: "bash",
          summary: "bash printf hi",
          input: "printf hi &&\nls",
          output: "hi",
        })}
        ticket="WIKI-247"
        withResult
      />,
    );
    const command = container.querySelector(".session-tool-block-title.is-command");
    expect(command?.textContent).toBe("$ printf hi &&\nls");
    expect(container.querySelector(".shiki-block")).toBeNull();
    expect(cssDeclarations(".session-tool-block-title.is-command")).toContain(
      "color: var(--text-normal);",
    );
  });

  test("edit diffs render line-number gutters with red/green line backgrounds", () => {
    // WIKI-251: SplitDiffView (react-diff-view) is the transcript edit
    // renderer. The library's insert/delete gutters are the landmarks; the
    // wiki-diff CSS variables still wire the red/green tokens through.
    const { container } = render(
      <ToolCallRow
        event={normalizedEditFixture as unknown as SessionEvent}
        ticket="WIKI-247"
        withResult
      />,
    );
    expect(container.querySelector(".session-tool-split-diff")).toBeTruthy();
    expect(container.querySelector(".diff-gutter-insert")).toBeTruthy();
    expect(container.querySelector(".diff-gutter-delete")).toBeTruthy();
    // The shared wiki-diff tokens still translate the library's gutter/code
    // classes to the wiki palette (styles.css:11585 — one rule body shared
    // between .split-diff-file .wiki-diff and .wiki-diff).
    expect(cssDeclarations(".wiki-diff"))
      .toContain("--diff-gutter-insert-background-color");
    expect(cssDeclarations(".wiki-diff"))
      .toContain("--diff-gutter-delete-background-color");
  });

  // WIKI-251: the "transcript diffs drop the box, duplicate file header,
  // and hunk chrome" test was removed. That inline-diff chrome policy
  // applied to the old DiffPatchView (unified) in the transcript — the
  // transcript now uses SplitDiffView (react-diff-view), which has its own
  // DOM/CSS surface (see .wiki-diff tokens in styles.css:11569). The
  // unified renderer still lives in artifact-detail, where the box/header
  // are appropriate.

  test("block interiors pin the monospace stack on the leaves", () => {
    expect(cssDeclarations(".session-tool-body")).toContain("font-family: var(--font-monospace);");
    expect(cssDeclarations(".diff-view")).toContain("font-family: var(--font-monospace);");
    expect(
      cssDeclarations(".transcript-preview-body.is-custom .session-tool-output-text"),
    ).toContain("font-family: var(--font-monospace);");
    expect(CSS_WITHOUT_COMMENTS).not.toContain(".session-tool-output-text {\n  white-space: pre-wrap;\n  overflow-wrap: anywhere;\n  font-family: inherit;");
  });
});

describe("sidebar orchestrator rows", () => {
  test("agent rows carry no active dot; current state is primary text", () => {
    expect(CSS_SOURCE).not.toContain(".nav-agent.is-active::before");
    expect(CSS_SOURCE).not.toMatch(/\.nav-agent[^,{]*::(?:before|after)/);
    const active = finalCssDeclarations(".nav-agent.is-active");
    expect(active).toContain("border-left: 0;");
    const currentTreatment = cssDeclarations(".nav-agent.is-active");
    expect(currentTreatment).toContain("background-color: transparent;");
    expect(currentTreatment).toContain("color: var(--primary, var(--accent-primary));");
  });

  test("worker rows align to the parent text column without a guide rail", () => {
    const workers = finalCssDeclarations(".nav-orch-workers");
    expect(workers).toContain("margin-left: 40px;");
    expect(workers).toContain("border-left: 0;");
    const rail = cssDeclarations(".nav-orch-attention");
    expect(rail).toContain("left: 0;");
    expect(cssDeclarations(".nav-agent.is-owned")).toContain("min-height: 32px;");
  });
});
