// @vitest-environment jsdom
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, test } from "vitest";

const CSS_SOURCE = readFileSync(
  resolve(dirname(fileURLToPath(import.meta.url)), "..", "src", "styles.css"),
  "utf-8",
);
const CHROME_CSS = CSS_SOURCE.slice(CSS_SOURCE.lastIndexOf("WIKI-246: OpenCode chrome language"));

function cssRule(selector: string): string {
  const start = CSS_SOURCE.lastIndexOf(`${selector} {`);
  expect(start).toBeGreaterThanOrEqual(0);
  const end = CSS_SOURCE.indexOf("}", start);
  expect(end).toBeGreaterThan(start);
  return CSS_SOURCE.slice(start, end);
}

afterEach(() => cleanup());

describe("WIKI-246 OpenCode chrome states", () => {
  test("selected controls use fill selection without a border", () => {
    const { container } = render(
      <div>
        <button className="command-palette-mode-button is-selected">semantic</button>
        <button className="quick-switcher-result is-selected">selected result</button>
        <article className="agent-card is-selected">selected agent</article>
      </div>,
    );

    expect(container.querySelectorAll(".is-selected")).toHaveLength(3);
    expect(cssRule(".command-palette-mode-button")).toContain("border: 0;");
    expect(CSS_SOURCE).toContain(".command-palette-mode-button.is-selected");
    expect(CSS_SOURCE).toContain("background-color: var(--accent-primary);");
    expect(cssRule(".agent-card.is-selected")).toContain("border-left: 0;");
    expect(cssRule(".agent-card.is-selected")).toContain(
      "background-color: var(--accent-primary);",
    );
  });

  test("disabled switcher entries do not receive hover elevation", () => {
    const { container } = render(
      <button className="quick-switcher-result fleet-switcher-result is-disabled">
        unavailable
      </button>,
    );

    const entry = container.querySelector(".fleet-switcher-result");
    expect(entry?.classList.contains("is-disabled")).toBe(true);
    expect(CSS_SOURCE).toContain(
      ".quick-switcher-result:hover:not(.is-selected):not(.is-disabled)",
    );
  });

  test("current navigation items expose one dot and no left rail", () => {
    const { container } = render(
      <div>
        <button className="tree-item-self is-active">current note</button>
        <button className="nav-agent is-active">current agent</button>
      </div>,
    );

    expect(container.querySelectorAll(".is-active")).toHaveLength(2);
    expect(cssRule(".tree-item-self.is-active")).toContain("border-left: 0;");
    expect(cssRule(".nav-agent.is-active")).toContain("border-left: 0;");
    expect(CSS_SOURCE).toContain(".tree-item-self.is-active::before");
    expect(CSS_SOURCE).toContain(".nav-agent.is-active::before");
    expect(CSS_SOURCE).toContain('content: "●";');
  });

  test("dialogs and composer expose the intended chrome structure", () => {
    const { container } = render(
      <div className="modal-backdrop">
        <section aria-modal="true" className="dialog" role="dialog">
          <div className="dialog-title">commands</div>
          <div className="dialog-actions">
            <button className="dialog-button dialog-confirm">confirm</button>
          </div>
        </section>
        <section className="session-composer">
          <div className="session-composer-row">
            <label className="session-composer-target" htmlFor="prompt">ask</label>
            <div className="session-input-wrap"><textarea id="prompt" /></div>
            <button className="session-send">send</button>
          </div>
        </section>
      </div>,
    );

    expect(container.querySelector('[role="dialog"]')?.classList.contains("dialog")).toBe(true);
    expect(CSS_SOURCE).toContain('.dialog-title::after');
    expect(CSS_SOURCE).toContain('content: "esc";');
    expect(CHROME_CSS).toContain(".dialog,");
    expect(CHROME_CSS).toContain("  border: 0;");
    expect(container.querySelector(".session-composer-row")?.classList.contains("session-composer-row")).toBe(true);
    expect(container.querySelector(".session-composer-target")?.getAttribute("for")).toBe("prompt");
    expect(container.querySelector(".session-input-wrap")).toBeTruthy();
    expect(container.querySelector(".session-send")).toBeTruthy();
  });

  test("pending-user styling stays outside transcript messages", () => {
    expect(CSS_SOURCE).toContain(".session-pending-user {");
    expect(CSS_SOURCE).not.toContain(".session-user {\n  border-top: 0;");
  });
});
