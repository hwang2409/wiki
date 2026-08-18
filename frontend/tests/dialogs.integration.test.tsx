// @vitest-environment jsdom
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import { Dialog } from "../src/App";
import { BbDialog, DialogRail } from "../src/dialogs";
import { THEMES } from "../src/themes";

const CSS_SOURCE = readFileSync(
  resolve(dirname(fileURLToPath(import.meta.url)), "..", "src", "styles.css"),
  "utf-8",
).replace(/\/\*[\s\S]*?\*\//g, "");

function cssDeclarations(selector: string): string {
  const bodies = [...CSS_SOURCE.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
    .filter((match) => match[1].split(",").some((entry) => entry.trim() === selector))
    .map((match) => match[2]);
  expect(bodies.length).toBeGreaterThan(0);
  return bodies.join("\n");
}

const deleteDialog = {
  title: "Delete note.md?",
  description: "This removes note.md from the vault.",
  confirmLabel: "Delete",
  danger: true,
  onConfirm: vi.fn(),
};

test("destructive dialogs open on Cancel, not the confirm action", () => {
  const onClose = vi.fn();
  render(<Dialog dialog={deleteDialog} onClose={onClose} />);

  const cancel = screen.getByRole("button", { name: "Cancel" });
  expect(document.activeElement).toBe(cancel);

  fireEvent.keyDown(document, { key: "Enter" });
  fireEvent.click(document.activeElement as HTMLElement);
  expect(deleteDialog.onConfirm).not.toHaveBeenCalled();
  expect(onClose).toHaveBeenCalledTimes(1);
});

test("transcript leaves use the selected monospace family", () => {
  expect(cssDeclarations(".codex-stream-command-input")).toContain(
    "font-family: var(--font-monospace);",
  );
});

test("theme choices use one quiet active treatment", () => {
  const choice = cssDeclarations(".theme-choice");
  const active = cssDeclarations(".theme-choice.is-active");
  expect(choice).toContain("border: 1px solid transparent;");
  expect(choice).toContain("background-color: transparent;");
  expect(active).not.toContain("box-shadow:");
  expect(THEMES.every((theme) => theme.preview.length === 4)).toBe(true);
});

describe("settings dialog rail", () => {
  test("uses a tablist with one tab stop and arrow navigation", () => {
    render(
      <BbDialog
        title="Settings"
        onClose={vi.fn()}
        rail={
          <DialogRail
            active="appearance"
            items={[
              { id: "appearance", label: "Appearance" },
              { id: "typography", label: "Typography" },
            ]}
            onSelect={vi.fn()}
          />
        }
      >
        Content
      </BbDialog>,
    );

    const rail = screen.getByRole("tablist");
    const tabs = screen.getAllByRole("tab");
    expect(rail).toBeTruthy();
    expect(tabs.map((tab) => tab.getAttribute("tabindex"))).toEqual(["0", "-1"]);
    expect(tabs[0].getAttribute("aria-selected")).toBe("true");
    expect(tabs[1].getAttribute("aria-selected")).toBe("false");

    tabs[0].focus();
    fireEvent.keyDown(tabs[0], { key: "ArrowDown" });
    expect(document.activeElement).toBe(tabs[1]);
    fireEvent.keyDown(tabs[1], { key: "End" });
    expect(document.activeElement).toBe(tabs[1]);
    fireEvent.keyDown(tabs[1], { key: "Home" });
    expect(document.activeElement).toBe(tabs[0]);
    fireEvent.keyDown(tabs[0], { key: "ArrowUp" });
    expect(document.activeElement).toBe(tabs[1]);
  });
});
