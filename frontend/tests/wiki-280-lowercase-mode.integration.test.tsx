// @vitest-environment jsdom
// WIKI-280: lowercase mode applies to EVERYTHING — including code, pre,
// shiki blocks, editor, diffs, and the terminal pane. The prior WIKI-277
// exclusions kept those surfaces at their original casing; the only
// remaining escape hatch is `data-preserve-case` for isolated subtrees
// that must not be flattened (e.g. proper nouns in copy-critical UI).
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, test } from "vitest";

import type { SessionEvent } from "../src/api";
import { ThinkingRow } from "../src/session";
import {
  LOWERCASE_ROOT_CLASS,
  LOWERCASE_STORAGE_KEY,
  applyLowercase,
  applyStoredLowercase,
  getStoredLowercase,
} from "../src/lowercase-mode";

const CSS_SOURCE = readFileSync(
  resolve(dirname(fileURLToPath(import.meta.url)), "..", "src", "styles.css"),
  "utf-8",
);
const CSS_WITHOUT_COMMENTS = CSS_SOURCE.replace(/\/\*[\s\S]*?\*\//g, "");

function selectorHasRule(selector: string): boolean {
  return [...CSS_WITHOUT_COMMENTS.matchAll(/([^{}]+)\{([^{}]*)\}/g)].some((match) =>
    match[1].split(",").some((entry) => entry.trim() === selector),
  );
}

function ruleBodyFor(selector: string): string {
  const bodies = [...CSS_WITHOUT_COMMENTS.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
    .filter((match) => match[1].split(",").some((entry) => entry.trim() === selector))
    .map((match) => match[2]);
  expect(bodies.length).toBeGreaterThan(0);
  return bodies.join("\n");
}

afterEach(() => {
  cleanup();
  localStorage.removeItem(LOWERCASE_STORAGE_KEY);
  document.documentElement.classList.remove(LOWERCASE_ROOT_CLASS);
});

describe("root toggle behavior", () => {
  test("default is off — no class, no stored value", () => {
    expect(getStoredLowercase()).toBe(false);
    expect(applyStoredLowercase()).toBe(false);
    expect(document.documentElement.classList.contains(LOWERCASE_ROOT_CLASS)).toBe(false);
  });

  test("enabling adds the root class and persists to localStorage", () => {
    applyLowercase(true);
    expect(document.documentElement.classList.contains(LOWERCASE_ROOT_CLASS)).toBe(true);
    expect(localStorage.getItem(LOWERCASE_STORAGE_KEY)).toBe("true");
    expect(getStoredLowercase()).toBe(true);
  });

  test("disabling removes the root class and clears storage", () => {
    applyLowercase(true);
    applyLowercase(false);
    expect(document.documentElement.classList.contains(LOWERCASE_ROOT_CLASS)).toBe(false);
    expect(localStorage.getItem(LOWERCASE_STORAGE_KEY)).toBeNull();
    expect(getStoredLowercase()).toBe(false);
  });

  test("setting survives a simulated reload via applyStoredLowercase", () => {
    applyLowercase(true);
    document.documentElement.classList.remove(LOWERCASE_ROOT_CLASS);
    expect(applyStoredLowercase()).toBe(true);
    expect(document.documentElement.classList.contains(LOWERCASE_ROOT_CLASS)).toBe(true);
  });
});

describe("everything-lowercase CSS semantics", () => {
  test("the root rule declares text-transform: lowercase", () => {
    expect(ruleBodyFor(".lowercase-mode")).toMatch(/text-transform:\s*lowercase/);
  });

  // WIKI-277 excluded code-adjacent surfaces so they kept original casing.
  // WIKI-280 removes those exclusions — the ONLY remaining opt-out is
  // `[data-preserve-case]`. If a new exclusion sneaks in for any of these
  // selectors, this test fires so we notice and reconsider.
  test.each([
    ".lowercase-mode code",
    ".lowercase-mode pre",
    ".lowercase-mode pre *",
    ".lowercase-mode .shiki-block",
    ".lowercase-mode .shiki-block *",
    ".lowercase-mode .cm-editor",
    ".lowercase-mode .cm-editor *",
    ".lowercase-mode .diff-view",
    ".lowercase-mode .diff-view *",
    ".lowercase-mode .wiki-diff",
    ".lowercase-mode .wiki-diff *",
    ".lowercase-mode .terminal-pane",
    ".lowercase-mode .terminal-pane *",
  ])("no exclusion rule for %s", (selector) => {
    expect(selectorHasRule(selector)).toBe(false);
  });

  test("data-preserve-case is the single escape hatch — text-transform: none", () => {
    expect(ruleBodyFor(".lowercase-mode [data-preserve-case]")).toMatch(
      /text-transform:\s*none/,
    );
    expect(ruleBodyFor(".lowercase-mode [data-preserve-case] *")).toMatch(
      /text-transform:\s*none/,
    );
  });
});

describe("thinking body regression (WIKI-280)", () => {
  // The expanded thinking body renders through ShikiCode, which wraps its
  // output in `.shiki-block`. Under WIKI-277 that class was excluded from
  // the lowercase transform, so the body kept its original casing when
  // the row was opened. The fix drops the exclusion; the DOM path that
  // used to be a text-transform:none island is now under the plain
  // `.lowercase-mode` rule and lowercases like everything else.
  test("expanded thinking body renders markdown without a code wrapper", () => {
    const event: SessionEvent = {
      id: 1,
      kind: "thinking",
      ts: "2026-08-13T00:00:00.000Z",
      text: "**Deciding**\nConsidering the TWO branches of the parser.",
      disposition: "rendered",
    };
    const { container } = render(<ThinkingRow event={event} />);
    fireEvent.click(container.querySelector(".session-thinking-head") as HTMLButtonElement);
    const body = container.querySelector(".session-thinking");
    expect(body).not.toBeNull();
    expect(body?.querySelector("strong")?.textContent).toBe("Deciding");
    // Sanity: the summary text is present (before or after highlight resolution).
    expect(body?.textContent ?? "").toContain(
      "Considering",
    );
    // The CSS rule that used to force text-transform:none on this subtree
    // must be gone; only the root `.lowercase-mode` rule and the
    // `[data-preserve-case]` escape hatch survive.
    expect(selectorHasRule(".lowercase-mode .shiki-block")).toBe(false);
    expect(selectorHasRule(".lowercase-mode .shiki-block *")).toBe(false);
  });
});
