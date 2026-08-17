// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import { BbDialog, DialogRail } from "../src/dialogs";

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
