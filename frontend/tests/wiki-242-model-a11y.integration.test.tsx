// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { getAgentModels, setAgentModel } from "../src/api";
import { SessionModelFooter } from "../src/session";
import type { AgentModelOption } from "../src/api";
import type { TranscriptSession } from "../src/transcript-store";

vi.mock("../src/api", async () => {
  const actual = await vi.importActual<typeof import("../src/api")>("../src/api");
  return {
    ...actual,
    getAgentModels: vi.fn(),
    setAgentModel: vi.fn(),
  };
});

const models: AgentModelOption[] = [
  {
    id: "gpt-5.4",
    label: "GPT 5.4",
    kind: "cdx",
    provider: "codex",
    supports_reasoning_effort: true,
    default_worker: true,
    default_orchestrator: false,
  },
  {
    id: "gpt-5.5",
    label: "GPT 5.5",
    kind: "cdx",
    provider: "codex",
    supports_reasoning_effort: true,
    default_worker: false,
    default_orchestrator: false,
  },
  {
    id: "gpt-5.6",
    label: "GPT 5.6",
    kind: "cdx",
    provider: "codex",
    supports_reasoning_effort: true,
    default_worker: false,
    default_orchestrator: false,
  },
];

const session: TranscriptSession = {
  format: "codex",
  path: "fixture.jsonl",
  tokens: null,
  model: "gpt-5.4",
  desiredModel: null,
  kind: "cdx",
  provider: "codex",
  tasks: [],
  pr: null,
  sessionMeta: {},
  dispositions: { rendered: 0, summarized: 0, ignored: 0, unknown: 0 },
  base: 0,
  cursor: 0,
  events: [],
  eventsChangedFrom: 0,
  hasOlder: false,
  composerMessages: [],
  subagents: [],
  queue: [],
  working: false,
};

function renderFooter() {
  return render(<SessionModelFooter session={session} ticket="WIKI-242" />);
}

async function openMenu() {
  if (!screen.queryByRole("menu", { name: "Available models" })) {
    fireEvent.click(screen.getByRole("button", { name: "Change model" }));
  }
  const menu = await screen.findByRole("menu", { name: "Available models" });
  await waitFor(() => expect(document.activeElement).toBe(screen.getAllByRole("menuitem")[0]));
  return menu;
}

async function openConfirmation() {
  await openMenu();
  const option = screen.getAllByRole("menuitem")[0];
  fireEvent.click(option);
  const dialog = await screen.findByRole("dialog");
  await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "Cancel" })));
  return { dialog, option };
}

beforeEach(() => {
  vi.mocked(getAgentModels).mockResolvedValue({ models });
  vi.mocked(setAgentModel).mockResolvedValue({ status: "queued", desired_model: "gpt-5.5" });
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
    callback(0);
    return 1;
  });
  vi.stubGlobal("cancelAnimationFrame", () => undefined);
  vi.spyOn(HTMLElement.prototype, "getClientRects").mockImplementation(function () {
    return (this.isConnected ? [{}] : []) as DOMRectList;
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("session model accessibility", () => {
  test("focuses the first menu entry and supports the complete menu keyboard pattern", async () => {
    renderFooter();
    await openMenu();
    const options = screen.getAllByRole("menuitem");
    expect(options.map((option) => option.getAttribute("tabindex"))).toEqual(["0", "-1"]);

    fireEvent.keyDown(options[0], { key: "ArrowDown" });
    expect(document.activeElement).toBe(options[1]);
    expect(options[1].getAttribute("tabindex")).toBe("0");
    fireEvent.keyDown(options[1], { key: "Home" });
    expect(document.activeElement).toBe(options[0]);
    fireEvent.keyDown(options[0], { key: "End" });
    expect(document.activeElement).toBe(options.at(-1));
    fireEvent.keyDown(options.at(-1)!, { key: "ArrowDown" });
    expect(document.activeElement).toBe(options[0]);
    fireEvent.keyDown(options[0], { key: "ArrowUp" });
    expect(document.activeElement).toBe(options.at(-1));
  });

  test("closes the menu on forward Tab, reverse Tab, and Escape", async () => {
    renderFooter();
    await openMenu();
    const trigger = screen.getByRole("button", { name: "Change model" });
    fireEvent.keyDown(screen.getAllByRole("menuitem")[0], { key: "Tab" });
    expect(screen.queryByRole("menu", { name: "Available models" })).toBeNull();

    fireEvent.click(trigger);
    await waitFor(() => expect(document.activeElement).toBe(screen.getAllByRole("menuitem")[0]));
    fireEvent.keyDown(screen.getAllByRole("menuitem")[0], { key: "Tab", shiftKey: true });
    expect(screen.queryByRole("menu", { name: "Available models" })).toBeNull();

    fireEvent.click(trigger);
    await waitFor(() => expect(document.activeElement).toBe(screen.getAllByRole("menuitem")[0]));
    fireEvent.keyDown(screen.getAllByRole("menuitem")[0], { key: "Escape" });
    expect(screen.queryByRole("menu", { name: "Available models" })).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  test("uses unique menu and dialog ids for split footers", async () => {
    render(
      <>
        <SessionModelFooter session={session} ticket="WIKI-242-a" />
        <SessionModelFooter session={session} ticket="WIKI-242-b" />
      </>,
    );
    const triggers = screen.getAllByRole("button", { name: "Change model" });
    fireEvent.click(triggers[0]);
    fireEvent.click(triggers[1]);
    await waitFor(() => expect(screen.getAllByRole("menu", { name: "Available models" })).toHaveLength(2));

    const menus = screen.getAllByRole("menu", { name: "Available models" });
    expect(new Set(menus.map((menu) => menu.id)).size).toBe(2);
    expect(triggers[0].getAttribute("aria-controls")).toBe(menus[0].id);
    expect(triggers[1].getAttribute("aria-controls")).toBe(menus[1].id);

    fireEvent.click(screen.getAllByRole("menuitem")[0]);
    const dialogs = await screen.findAllByRole("dialog");
    expect(dialogs).toHaveLength(1);
    const titleId = dialogs[0].getAttribute("aria-labelledby");
    expect(titleId).toBeTruthy();
    expect(document.getElementById(titleId!)).toBeTruthy();
  });

  test("wraps dialog Tab focus and closes on Escape", async () => {
    renderFooter();
    const { dialog } = await openConfirmation();
    const cancel = screen.getByRole("button", { name: "Cancel" });
    const switchButton = screen.getByRole("button", { name: "Switch model" });

    switchButton.focus();
    fireEvent.keyDown(document, { key: "Tab" });
    expect(document.activeElement).toBe(cancel);
    fireEvent.keyDown(document, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(switchButton);
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(dialog.isConnected).toBe(false);
  });

  test("restores focus to the trigger after cancel and after a successful switch", async () => {
    renderFooter();
    await openConfirmation();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    // Focus returns to the model trigger — the keyboard user's anchor point.
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Change model" }));

    // Re-open the menu explicitly since cancel closed it.
    fireEvent.click(screen.getByRole("button", { name: "Change model" }));
    const second = await openConfirmation();
    fireEvent.click(screen.getByRole("button", { name: "Switch model" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(second.option.isConnected).toBe(false);
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Change model" }));
  });
});
