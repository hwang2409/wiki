// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { AgentsSidebar } from "../src/agents";
import type { AgentWorker, ArchivedWorker, Orchestrator } from "../src/api";

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
const archivedWorker: ArchivedWorker = {
  ticket: "WIKI-ARCHIVED",
  archived_at: "2026-08-01T00:00:00Z",
  kind: "cdx",
  role: "implement",
  model: "gpt-5.6-luna",
  outcome: "completed",
  state: "completed",
  pr: null,
  step: "done",
};

beforeEach(() => localStorage.clear());
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

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

    const tickets = Array.from(document.querySelectorAll(".nav-agent-ticket")).map(
      (ticket) => ticket.textContent,
    );
    expect(tickets).toEqual(["phoebe", "wiki", "FREE-1"]);
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

  test("opens a non-empty orchestrator separately from its worker disclosure", () => {
    const opened: string[] = [];
    render(
      <AgentsSidebar
        activeTicket={null}
        data={{ workers, orchestrators, error: null }}
        refreshTick={0}
        onOpen={(ticket) => opened.push(ticket)}
      />,
    );

    const wiki = screen.getByText("wiki").closest("button");
    expect(wiki).not.toBeNull();
    const disclosure = screen.getByRole("button", { name: "Expand wiki workers" });
    expect(disclosure.getAttribute("aria-expanded")).toBe("false");

    fireEvent.click(wiki!);
    expect(opened).toEqual(["wiki"]);
    expect(screen.queryByTestId("nav-orch-workers-wiki")).toBeNull();

    fireEvent.click(disclosure);
    expect(disclosure.getAttribute("aria-expanded")).toBe("true");

    const group = screen.getByTestId("nav-orch-workers-wiki");
    const tickets = within(group)
      .getAllByRole("button")
      .map((row) => row.querySelector(".nav-agent-ticket")?.textContent);
    expect(tickets).toEqual(["WIKI-BLOCK", "WIKI-READY", "WIKI-WORK"]);

    fireEvent.click(within(group).getByText("WIKI-READY"));
    expect(opened).toEqual(["wiki", "WIKI-READY"]);
    fireEvent.click(disclosure);
    expect(screen.queryByTestId("nav-orch-workers-wiki")).toBeNull();
  });

  test("keeps owned-worker unread attention visible while its group is collapsed", () => {
    const unreadWorkers = workers.map((item) =>
      item.ticket === "WIKI-WORK"
        ? { ...item, latest_event_seq: 2, last_viewed_seq: 1 }
        : item,
    );
    render(
      <AgentsSidebar
        activeTicket={null}
        data={{ workers: unreadWorkers, orchestrators, error: null }}
        refreshTick={0}
        onOpen={() => {}}
      />,
    );

    const wiki = screen.getByText("wiki").closest("button");
    expect(wiki).not.toBeNull();
    expect(wiki?.classList.contains("has-unread")).toBe(true);
    expect(within(wiki!).getByTestId("nav-orch-unread")).toBeTruthy();
    expect(within(wiki!).getByText("workers have unread updates")).toBeTruthy();
    expect(screen.queryByText("WIKI-WORK")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Expand wiki workers" }));
    const workerRow = screen.getByText("WIKI-WORK").closest("button");
    expect(workerRow).not.toBeNull();
    expect(within(workerRow!).getByTestId("nav-agent-unread")).toBeTruthy();
    expect(within(wiki!).getByTestId("nav-orch-unread")).toBeTruthy();
  });

  test("auto-expands a directly opened worker once and preserves a manual collapse", async () => {
    const view = render(
      <AgentsSidebar
        activeTicket="WIKI-READY"
        data={{ workers, orchestrators, error: null }}
        refreshTick={0}
        onOpen={() => {}}
      />,
    );

    const group = await screen.findByTestId("nav-orch-workers-wiki");
    const activeWorker = within(group).getByText("WIKI-READY").closest("button");
    expect(activeWorker?.classList.contains("is-active")).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Collapse wiki workers" }));
    expect(screen.queryByTestId("nav-orch-workers-wiki")).toBeNull();

    view.rerender(
      <AgentsSidebar
        activeTicket="WIKI-READY"
        data={{
          workers: workers.map((worker) => ({ ...worker })),
          orchestrators: orchestrators.map((orchestrator) => ({ ...orchestrator })),
          error: null,
        }}
        refreshTick={1}
        onOpen={() => {}}
      />,
    );

    expect(screen.queryByTestId("nav-orch-workers-wiki")).toBeNull();
    expect(screen.getByRole("button", { name: "Expand wiki workers" })).toBeTruthy();
  });

  test("prioritizes owned-worker persistence failure and clears it after recovery", async () => {
    vi.useFakeTimers();
    let shouldFail = true;
    const fetchMock = vi.fn(async () => {
      if (shouldFail) {
        return new Response(JSON.stringify({ detail: "failed" }), {
          status: 500,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response(
        JSON.stringify({
          run_id: "run-WIKI-WORK",
          last_viewed_at: "2026-08-02T01:00:00Z",
          last_viewed_seq: 3,
          latest_event_at: "2026-08-02T01:00:00Z",
          latest_event_seq: 3,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    const mixedWorkers = workers.map((item) => {
      if (item.ticket === "WIKI-WORK") {
        return { ...item, latest_event_seq: 2, last_viewed_seq: 1 };
      }
      if (item.ticket === "WIKI-READY") {
        return { ...item, latest_event_seq: 4, last_viewed_seq: 3 };
      }
      return item;
    });
    const view = render(
      <AgentsSidebar
        activeTicket="WIKI-WORK"
        data={{ workers: mixedWorkers, orchestrators, error: null }}
        refreshTick={0}
        onOpen={() => {}}
      />,
    );

    await act(async () => {
      await vi.runAllTimersAsync();
    });
    expect(fetchMock).toHaveBeenCalledTimes(3);

    fireEvent.click(screen.getByRole("button", { name: "Collapse wiki workers" }));
    const wiki = screen.getByText("wiki").closest("button");
    expect(wiki).not.toBeNull();
    expect(wiki?.classList.contains("has-unread")).toBe(true);
    expect(wiki?.classList.contains("has-viewed-failure")).toBe(true);
    expect(within(wiki!).getByTestId("nav-orch-unread")).toBeTruthy();
    expect(within(wiki!).getByText("workers have unread updates")).toBeTruthy();
    expect(within(wiki!).getByTestId("nav-orch-viewed-failed")).toBeTruthy();
    expect(within(wiki!).getByText("worker read state failed to save")).toBeTruthy();
    expect(wiki?.querySelector(".nav-orch-attention.is-failed")).not.toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Expand wiki workers" }));
    const failedWorker = screen.getByText("WIKI-WORK").closest("button");
    expect(failedWorker).not.toBeNull();
    expect(within(failedWorker!).getByTestId("nav-agent-viewed-failed")).toBeTruthy();

    shouldFail = false;
    const recoveredWorkers = mixedWorkers.map((item) => {
      if (item.ticket === "WIKI-WORK") return { ...item, latest_event_seq: 3 };
      if (item.ticket === "WIKI-READY") return { ...item, last_viewed_seq: 4 };
      return item;
    });
    view.rerender(
      <AgentsSidebar
        activeTicket="WIKI-WORK"
        data={{ workers: recoveredWorkers, orchestrators, error: null }}
        refreshTick={1}
        onOpen={() => {}}
      />,
    );
    await act(async () => {
      await vi.runAllTimersAsync();
    });

    expect(fetchMock).toHaveBeenCalledTimes(4);
    expect(wiki?.classList.contains("has-unread")).toBe(false);
    expect(wiki?.classList.contains("has-viewed-failure")).toBe(false);
    expect(within(wiki!).queryByTestId("nav-orch-unread")).toBeNull();
    expect(within(wiki!).queryByTestId("nav-orch-viewed-failed")).toBeNull();
  });

  test("clears a failed viewed marker when a server refresh confirms the same sequence", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ detail: "response lost" }), {
        status: 500,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const failedWorkers = workers.map((item) =>
      item.ticket === "WIKI-WORK"
        ? { ...item, latest_event_seq: 2, last_viewed_seq: 1 }
        : item,
    );
    const view = render(
      <AgentsSidebar
        activeTicket="WIKI-WORK"
        data={{ workers: failedWorkers, orchestrators, error: null }}
        refreshTick={0}
        onOpen={() => {}}
      />,
    );

    await act(async () => {
      await vi.runAllTimersAsync();
    });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    const failedWorker = screen.getByText("WIKI-WORK").closest("button");
    expect(within(failedWorker!).getByTestId("nav-agent-viewed-failed")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Collapse wiki workers" }));
    const wiki = screen.getByText("wiki").closest("button");
    expect(within(wiki!).getByTestId("nav-orch-viewed-failed")).toBeTruthy();

    await act(async () => {
      view.rerender(
        <AgentsSidebar
          activeTicket="WIKI-WORK"
          data={{
            workers: failedWorkers.map((item) =>
              item.ticket === "WIKI-WORK" ? { ...item, last_viewed_seq: 2 } : { ...item },
            ),
            orchestrators: orchestrators.map((item) => ({ ...item })),
            error: null,
          }}
          refreshTick={1}
          onOpen={() => {}}
        />,
      );
    });

    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(wiki?.classList.contains("has-viewed-failure")).toBe(false);
    expect(within(wiki!).queryByTestId("nav-orch-viewed-failed")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Expand wiki workers" }));
    const confirmedWorker = screen.getByText("WIKI-WORK").closest("button");
    expect(within(confirmedWorker!).queryByTestId("nav-agent-viewed-failed")).toBeNull();
  });

  test("persists expanded orchestrators across remounts", () => {
    const props = {
      activeTicket: null,
      data: { workers, orchestrators, error: null },
      refreshTick: 0,
      onOpen: () => {},
    };
    const first = render(<AgentsSidebar {...props} />);
    fireEvent.click(screen.getByRole("button", { name: "Expand wiki workers" }));
    first.unmount();

    render(<AgentsSidebar {...props} />);
    expect(screen.getByTestId("nav-orch-workers-wiki")).toBeTruthy();
  });

  test("uses active-run copy when only archived runs exist", () => {
    render(
      <AgentsSidebar
        activeTicket={null}
        data={{ workers: [], orchestrators: [], archived: [archivedWorker], error: null }}
        refreshTick={0}
        onOpen={() => {}}
      />,
    );

    expect(screen.getByTestId("nav-agents-empty").textContent).toContain("No active runs");
    expect(screen.queryByText("No runs yet")).toBeNull();
  });
});
