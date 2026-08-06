// @vitest-environment jsdom
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { cleanup, render, within } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import { CommandPalette } from "../src/command-palette";
import { KanbanBoard } from "../src/kanban";
import { searchPalette } from "../src/api";
import { FleetSwitcher, QuickSwitcher } from "../src/switcher";

vi.mock("../src/api", () => ({
  searchPalette: vi.fn(),
}));

const CSS_SOURCE = readFileSync(
  resolve(dirname(fileURLToPath(import.meta.url)), "..", "src", "styles.css"),
  "utf-8",
);
const CHROME_CSS = CSS_SOURCE.slice(CSS_SOURCE.lastIndexOf("WIKI-246: OpenCode chrome language"));

// Comments would otherwise hide a rule from selector matching, and joined
// duplicates would let a reverted late override pass on the earlier rule.
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

// The declarations that actually win the cascade for equal-specificity
// duplicates: the last rule body in source order.
function finalCssDeclarations(selector: string): string {
  const bodies = cssRuleBodies(selector);
  return bodies[bodies.length - 1];
}

const noOp = () => {};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("WIKI-246 OpenCode chrome states", () => {
  test("real switcher dialogs mount title rows and clickable esc hints", () => {
    const switcher = render(
      <QuickSwitcher
        notes={[
          {
            id: "note-1",
            path: "notes/one.md",
            title: "one",
            excerpt: "",
            updated_at: "2026-08-06T00:00:00Z",
          },
        ]}
        files={[]}
        filesLoading={false}
        recent={[]}
        sessions={[]}
        onClose={noOp}
        onOpen={noOp}
        onOpenFile={noOp}
        onOpenRecent={noOp}
        onOpenSession={noOp}
        onOpenPage={noOp}
      />,
    );

    const switcherDialog = switcher.getByRole("dialog", { name: "Quick switcher" });
    expect(within(switcherDialog).getByText("Quick switcher")).toBeTruthy();
    expect(
      within(switcherDialog).getByRole("button", { name: "Close quick switcher" }),
    ).toBeTruthy();

    vi.mocked(searchPalette).mockResolvedValue({ results: [] });
    const commandPalette = render(<CommandPalette onClose={noOp} onOpen={noOp} />);
    const paletteDialog = commandPalette.getByRole("dialog", { name: "Command palette" });
    expect(within(paletteDialog).getByText("Command palette")).toBeTruthy();
    expect(
      within(paletteDialog).getByRole("button", { name: "Close command palette" }),
    ).toBeTruthy();
    expect(cssDeclarations(".dialog-title")).toContain("font-weight: var(--fw-semibold);");
    expect(cssDeclarations(".dialog-title-esc")).toContain("color: var(--text-muted);");
    expect(cssDeclarations(".dialog-title-esc")).toContain("cursor: pointer;");
    expect(within(switcherDialog).getByLabelText("Keyboard shortcuts").textContent).toContain(
      "up/downnavigateenteropenescclose",
    );
    expect(within(paletteDialog).getByLabelText("Keyboard shortcuts").textContent).toContain(
      "up/downnavigateenteropenescclose",
    );
    expect(cssDeclarations(".quick-switcher-footer")).toContain("gap: 8px;");
    expect(cssDeclarations(".quick-switcher-hint-key")).toContain("color: var(--text);");
    expect(cssDeclarations(".quick-switcher-hint-label")).toContain(
      "color: var(--text-muted);",
    );
  });

  test("real switcher selection and fleet rows use the chrome declarations", () => {
    const switcher = render(
      <QuickSwitcher
        notes={[]}
        files={[]}
        filesLoading={false}
        recent={[]}
        sessions={[
          {
            id: "session-1",
            model: "model",
            orchestratorId: null,
            provider: "provider",
            role: "worker",
            sessionKind: "worker",
          },
        ]}
        onClose={noOp}
        onOpen={noOp}
        onOpenFile={noOp}
        onOpenRecent={noOp}
        onOpenSession={noOp}
        onOpenPage={noOp}
      />,
    );

    const selected = switcher.container.querySelector(".quick-switcher-result.is-selected");
    expect(selected).toBeTruthy();
    expect(cssDeclarations(".quick-switcher-result")).toContain("border-radius: 0;");
    expect(cssDeclarations(".quick-switcher-result.is-selected")).toContain(
      "background-color: var(--accent-primary);",
    );

    const fleet = render(
      <FleetSwitcher
        title="Agents"
        items={[
          { key: "current", value: "current", label: "current", meta: "working", active: true },
          { key: "disabled", value: "disabled", label: "disabled", meta: "offline", disabled: true },
        ]}
        onClose={noOp}
        onPick={noOp}
      />,
    );

    expect(fleet.container.querySelector(".fleet-switcher-result.is-current")).toBeTruthy();
    expect(
      within(fleet.getByRole("dialog", { name: "Agents" })).getByRole("button", {
        name: "Close Agents",
      }),
    ).toBeTruthy();
    expect(cssDeclarations(".fleet-switcher-result.is-current:not(.is-selected)")).toContain(
      "background-color: transparent;",
    );
    expect(CSS_SOURCE).toContain(
      ".quick-switcher-result:hover:not(.is-selected):not(.is-disabled)",
    );
  });

  test("real kanban cards keep flat chrome", () => {
    const view = render(
      <KanbanBoard
        content={"Todo:\n- ship chrome"}
        notes={[]}
        onChange={noOp}
        onComplete={noOp}
        onOpenNote={noOp}
      />,
    );

    const card = view.getByText("ship chrome").closest(".kanban-card");
    expect(card).toBeTruthy();
    expect(cssDeclarations(".kanban-card")).toContain("border-radius: 0;");
    expect(cssDeclarations(".kanban-card")).toContain("border: 0;");
    expect(cssDeclarations(".kanban-card:hover")).toContain("box-shadow: none;");
  });

  test("chrome hover keeps elevation without foreground or border promotion", () => {
    for (const selector of [
      ".ribbon-action:hover",
      ".nav-action-button:hover",
      ".tree-item-self:hover",
      ".dialog-button:hover",
      ".tmux-status-item:hover",
      ".nav-agent:hover",
    ]) {
      const declarations = cssDeclarations(selector);
      expect(declarations).toContain("background");
      expect(declarations).not.toMatch(/(?:^|\n)\s*color\s*:/);
      expect(declarations).not.toMatch(/(?:^|\n)\s*border-color\s*:/);
    }
  });

  test("dialogs keep the WIKI-242 inset at a 320px viewport", () => {
    const chromeDialog = CHROME_CSS.indexOf(".dialog,\n.quick-switcher,\n.settings-modal {");
    const narrowOverride = CHROME_CSS.indexOf("@media (max-width: 640px)", chromeDialog);
    expect(chromeDialog).toBeGreaterThanOrEqual(0);
    expect(narrowOverride).toBeGreaterThan(chromeDialog);

    const narrowDialogCss = CHROME_CSS.slice(narrowOverride);
    expect(narrowDialogCss).toContain(".dialog,");
    expect(narrowDialogCss).toContain(".quick-switcher,");
    expect(narrowDialogCss).toContain(".settings-modal,");
    expect(narrowDialogCss).toContain(".agent-spawn-modal {");
    expect(narrowDialogCss).toContain("top: 12px;");
    expect(narrowDialogCss).toContain("max-height: calc(100vh - 24px);");
    expect(narrowDialogCss).toContain("overflow-y: auto;");

    // A later unscoped rule must not win the cascade at narrow widths: the
    // final .quick-switcher max-height in source order must be the narrow
    // media override, not the 60vh/overflow-hidden desktop rule.
    const desktopSwitcher = CHROME_CSS.lastIndexOf(".quick-switcher {\n  max-height: 60vh;");
    expect(desktopSwitcher).toBeGreaterThanOrEqual(0);
    const postDesktop = CHROME_CSS.slice(desktopSwitcher);
    const lateOverride = postDesktop.indexOf("@media (max-width: 640px)");
    expect(lateOverride).toBeGreaterThan(0);
    const lateCss = postDesktop.slice(lateOverride);
    expect(lateCss).toContain(".quick-switcher {");
    expect(lateCss).toContain("max-height: calc(100vh - 24px);");
    expect(lateCss).toContain("overflow-y: auto;");
  });

  test("muted chrome controls have visible hover elevation in the final cascade", () => {
    // element-on-element or transparent hover backgrounds are invisible; the
    // FINAL rule in source order must carry the modifier token so a reverted
    // late override cannot hide behind an earlier legacy rule.
    for (const selector of [".ribbon-action:hover", ".dialog-button:hover", ".tmux-status-item:hover"]) {
      const finalBody = finalCssDeclarations(selector);
      expect(finalBody).toContain("var(--background-modifier-hover)");
      expect(finalBody).not.toContain("var(--background-element)");
      expect(finalBody).not.toContain("transparent");
    }
  });

  test("hover rules never promote border color", () => {
    expect(cssDeclarations(".nav-inline-retry:hover")).not.toMatch(/border-color/);
    for (const match of CSS_SOURCE.matchAll(/^([^@{}]*:hover[^{]*)\{([^}]*)\}/gm)) {
      if (match[1].includes(".session-")) continue; // transcript scope (WIKI-245)
      expect(match[2]).not.toMatch(/(?:^|\n)\s*border-color\s*:\s*var\(--text-/);
    }
  });

  test("converted chrome has no transition sites", () => {
    expect(cssDeclarations(".tmux-status-item")).not.toContain("transition");
    expect(CSS_SOURCE).not.toContain(
      "transition-property: transform;\n  transition-duration: 140ms;",
    );
    expect(cssDeclarations(".session-composer-row")).not.toContain("transition");
    expect(CSS_SOURCE).not.toContain(".agent-spawn-static");
    expect(CHROME_CSS).not.toContain("animation: chrome-fade-in");
  });

  test("current navigation items expose one dot and no left rail", () => {
    expect(cssDeclarations(".tree-item-self.is-active")).toContain("border-left: 0;");
    expect(cssDeclarations(".nav-agent.is-active")).toContain("border-left: 0;");
    expect(CSS_SOURCE).toContain(".tree-item-self.is-active::before");
    expect(CSS_SOURCE).toContain(".nav-agent.is-active::before");
    expect(CSS_SOURCE).toContain('content: "●";');
  });

  test("pending-user styling stays outside transcript messages", () => {
    expect(CSS_SOURCE).toContain(".session-pending-user {");
    expect(CSS_SOURCE).not.toContain(".session-user {\n  border-top: 0;");
  });
});
