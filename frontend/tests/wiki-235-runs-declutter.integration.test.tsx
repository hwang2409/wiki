// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test } from "vitest";

import { AgentsSidebar } from "../src/agents";
import type { AgentWorker, Orchestrator } from "../src/api";

function worker(ticket: string, state: string, orch: string | null): AgentWorker {
  return {
    ticket,
    state,
    orch,
    registered: true,
    window_alive: true,
    window: null,
    run_id: `run-${ticket}`,
    runtime_state: state,
    kind: "cdx",
    role: "implement",
    model: "gpt-5.6-luna",
    session: null,
    spawned_at: "2026-08-02T00:00:00Z",
    worktree: "/tmp/wiki",
    log: null,
    history: [],
    pr: null,
    step: "working",
    blocker: null,
    status_age_seconds: state === "blocked" ? 30 : 60,
    latest_event_at: null,
    latest_event_seq: null,
    last_viewed_at: null,
    last_viewed_seq: null,
  };
}

function orchestrator(id: string): Orchestrator {
  return {
    id,
    window: null,
    window_alive: true,
    run_id: `run-${id}`,
    runtime_state: "working",
    cwd: "/tmp/wiki",
    kind: "cc",
    model: "claude-opus-4.7",
    effort: null,
    spawned_at: "2026-08-02T00:00:00Z",
    transcript_exists: true,
  };
}

const workers = [
  worker("WIKI-WORK", "working", "wiki"),
  worker("WIKI-READY", "merge-ready", "wiki"),
  worker("WIKI-BLOCK", "blocked", "wiki"),
  worker("FREE-1", "working", null),
];
const orchestrators = [orchestrator("wiki"), orchestrator("phoebe")];

beforeEach(() => localStorage.clear());
afterEach(() => cleanup());

describe("WIKI-235 runs sidebar", () => {
  test("shows orchestrators first and keeps empty orchestrators directly openable", () => {
    const opened: string[] = [];
    render(
      <AgentsSidebar
        activeTicket={null}
        data={{ workers, orchestrators, error: null }}
        refreshTick={0}
        onOpen={(ticket) => opened.push(ticket)}
      />,
    );

    const rows = screen.getAllByRole("button").map((row) => row.textContent ?? "");
    expect(rows[0]).toContain("phoebe");
    expect(rows[1]).toContain("wiki");
    expect(rows[2]).toContain("FREE-1");
    expect(screen.queryByText("WIKI-BLOCK")).toBeNull();
    expect(screen.queryByTestId("nav-agents-group-history")).toBeNull();
    expect(document.querySelector(".nav-agent-num")).toBeNull();

    const phoebe = screen.getByText("phoebe").closest("button");
    expect(phoebe).not.toBeNull();
    expect(phoebe?.classList.contains("is-empty")).toBe(true);
    expect(phoebe?.hasAttribute("aria-expanded")).toBe(false);
    expect(phoebe?.hasAttribute("aria-controls")).toBe(false);
    fireEvent.click(phoebe!);
    expect(opened).toEqual(["phoebe"]);
  });

  test("expands in attention order, opens workers, and collapses again", () => {
    const opened: string[] = [];
    render(
      <AgentsSidebar
        activeTicket={null}
        data={{ workers, orchestrators, error: null }}
        refreshTick={0}
        onOpen={(ticket) => opened.push(ticket)}
      />,
    );

    const wiki = screen.getByRole("button", { name: /wikiworking/i });
    expect(wiki.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(wiki);
    expect(wiki.getAttribute("aria-expanded")).toBe("true");

    const group = screen.getByTestId("nav-orch-workers-wiki");
    const tickets = within(group)
      .getAllByRole("button")
      .map((row) => row.querySelector(".nav-agent-ticket")?.textContent);
    expect(tickets).toEqual(["WIKI-BLOCK", "WIKI-READY", "WIKI-WORK"]);

    fireEvent.click(within(group).getByText("WIKI-READY"));
    expect(opened).toEqual(["WIKI-READY"]);
    fireEvent.click(wiki);
    expect(screen.queryByTestId("nav-orch-workers-wiki")).toBeNull();
  });

  test("persists expanded orchestrators across remounts", () => {
    const props = {
      activeTicket: null,
      data: { workers, orchestrators, error: null },
      refreshTick: 0,
      onOpen: () => {},
    };
    const first = render(<AgentsSidebar {...props} />);
    fireEvent.click(screen.getByRole("button", { name: /wikiworking/i }));
    first.unmount();

    render(<AgentsSidebar {...props} />);
    expect(screen.getByTestId("nav-orch-workers-wiki")).toBeTruthy();
  });
});
