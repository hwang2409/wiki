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

function cssDeclarations(selector: string): string {
  const declarations = [...CSS_SOURCE.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
    .filter((match) => match[1].split(",").some((entry) => entry.trim() === selector))
    .map((match) => match[2]);
  expect(declarations.length).toBeGreaterThan(0);
  return declarations.join("\n");
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
