// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { DashboardTicketsPayload } from "../src/dashboard";
import { DashboardView } from "../src/dashboard";

const EMPTY_PAYLOAD: DashboardTicketsPayload = { tickets: [], repo_allowlist: [] };
const EMPTY_COSTS = {
  updated_at: null,
  totals: {
    label: "all",
    input: 0,
    cache_read: 0,
    cache_write: 0,
    cached: 0,
    output: 0,
    reasoning: 0,
    total_tokens: 0,
    cost_usd: 0,
    unpriced_tokens: 0,
    pricing: "priced" as const,
    models: [],
  },
  top: { worker: [], ticket: [], orchestrator: [], day: [] },
  prompt_size_distribution: [],
  velocity: { tokens_per_minute: 0, window_seconds: 60, tokens: 0 },
  runs_scanned: 0,
  refreshing: false,
};

function ticket(overrides: Partial<DashboardTicketsPayload["tickets"][number]> = {}) {
  return {
    ticket: "WIKI-1",
    description: "implementation",
    pr: null,
    repo: null,
    enriched: false,
    status: "implementing",
    detail: null,
    date: "2026-07-20T12:00:00+00:00",
    live: true,
    role: "implement",
    kind: "cc",
    ...overrides,
  };
}

beforeEach(() => {
  window.localStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  window.localStorage.clear();
});

test("polling does not overlap: next fetch waits for prior completion", async () => {
  vi.useFakeTimers();
  let inflight = 0;
  let peak = 0;
  // Fetch takes LONGER than the poll interval so a naive setInterval loop
  // would stack multiple inflight requests. The completion-scheduled poll
  // must keep peak at 1.
  const fetchFn = vi.fn(async (_signal: AbortSignal): Promise<DashboardTicketsPayload> => {
    inflight++;
    peak = Math.max(peak, inflight);
    await new Promise((resolve) => setTimeout(resolve, 20_000));
    inflight--;
    return EMPTY_PAYLOAD;
  });
  render(<DashboardView fetchTickets={fetchFn} pollMs={5_000} />);
  await vi.advanceTimersByTimeAsync(60_000);
  expect(peak).toBe(1);
  expect(fetchFn.mock.calls.length).toBeGreaterThan(0);
});

test("successful cost polling makes one request per interval tick", async () => {
  vi.useFakeTimers();
  const fetchCosts = vi.fn(async () => EMPTY_COSTS);
  render(
    <DashboardView
      fetchTickets={async () => EMPTY_PAYLOAD}
      fetchCosts={fetchCosts}
      pollMs={5_000}
    />
  );

  await act(async () => {
    await Promise.resolve();
  });
  expect(fetchCosts).toHaveBeenCalledTimes(1);

  await act(async () => {
    await vi.advanceTimersByTimeAsync(5_000);
  });
  expect(fetchCosts).toHaveBeenCalledTimes(2);

  await act(async () => {
    await vi.advanceTimersByTimeAsync(5_000);
  });
  expect(fetchCosts).toHaveBeenCalledTimes(3);
});

test("unmount aborts inflight fetch", async () => {
  vi.useFakeTimers();
  const abortSpy = vi.fn();
  const fetchFn = vi.fn(async (signal: AbortSignal): Promise<DashboardTicketsPayload> => {
    signal.addEventListener("abort", abortSpy);
    await new Promise((resolve) => setTimeout(resolve, 5_000));
    return EMPTY_PAYLOAD;
  });
  const { unmount } = render(<DashboardView fetchTickets={fetchFn} pollMs={15_000} />);
  // Let the initial fetch start and register the abort listener.
  await Promise.resolve();
  await Promise.resolve();
  unmount();
  expect(abortSpy).toHaveBeenCalled();
});

test("renders endpoint rows and their count", async () => {
  const fetchFn = vi.fn(async (): Promise<DashboardTicketsPayload> => ({
    tickets: [
      ticket({ ticket: "WIKI-135", role: "implement", live: true }),
      ticket({ ticket: "WIKI-LEGACY", role: null, live: false }),
    ],
    repo_allowlist: [],
  }));

  render(<DashboardView fetchTickets={fetchFn} pollMs={60_000} />);

  expect(await screen.findByText("WIKI-135")).toBeTruthy();
  expect(screen.getByText("WIKI-LEGACY")).toBeTruthy();
  expect(screen.getByText("2 tickets")).toBeTruthy();
  expect(screen.getAllByRole("row")).toHaveLength(3);
});

test("filter bar filters rows, updates count with X / Y, and persists to localStorage", async () => {
  const fetchFn = vi.fn(async (): Promise<DashboardTicketsPayload> => ({
    tickets: [
      ticket({ ticket: "WIKI-135", status: "working", date: "2026-07-20T12:00:00Z" }),
      ticket({ ticket: "WIKI-140", status: "merge-ready", date: "2026-07-20T12:00:00Z" }),
      ticket({ ticket: "PHO-14053", status: "working", date: "2026-07-20T12:00:00Z" }),
    ],
    repo_allowlist: [],
  }));

  render(<DashboardView fetchTickets={fetchFn} pollMs={60_000} />);

  await screen.findByText("WIKI-135");
  expect(screen.getByText("3 tickets")).toBeTruthy();

  // Open the Project dropdown and pick WIKI
  const projectTrigger = screen.getByRole("button", { name: /^Project$/ });
  fireEvent.click(projectTrigger);
  const wikiOption = await screen.findByRole("checkbox", { name: "WIKI" });
  await act(async () => {
    fireEvent.click(wikiOption);
  });

  // Count now reflects filtered vs total
  expect(screen.getByText("2 / 3 tickets")).toBeTruthy();
  expect(screen.queryByText("PHO-14053")).toBeNull();
  expect(screen.getByText("WIKI-135")).toBeTruthy();
  expect(screen.getByText("WIKI-140")).toBeTruthy();

  // localStorage was updated
  const stored = window.localStorage.getItem("wiki-dashboard-filters");
  expect(stored).toBeTruthy();
  expect(JSON.parse(stored!)).toEqual({
    projects: ["WIKI"],
    states: [],
    dateFrom: null,
    dateTo: null,
  });

  // Clear all restores full list and removes stored key
  const clearButton = screen.getByRole("button", { name: "Clear" });
  await act(async () => {
    fireEvent.click(clearButton);
  });
  expect(screen.getByText("3 tickets")).toBeTruthy();
  expect(screen.getByText("PHO-14053")).toBeTruthy();
  expect(window.localStorage.getItem("wiki-dashboard-filters")).toBeNull();
});

test("persisted filters hydrate on mount", async () => {
  window.localStorage.setItem(
    "wiki-dashboard-filters",
    JSON.stringify({ projects: ["PHO"], states: [], dateFrom: null, dateTo: null })
  );
  const fetchFn = vi.fn(async (): Promise<DashboardTicketsPayload> => ({
    tickets: [
      ticket({ ticket: "WIKI-135", status: "working" }),
      ticket({ ticket: "PHO-14053", status: "working" }),
    ],
    repo_allowlist: [],
  }));

  render(<DashboardView fetchTickets={fetchFn} pollMs={60_000} />);

  await screen.findByText("PHO-14053");
  expect(screen.queryByText("WIKI-135")).toBeNull();
  expect(screen.getByText("1 / 2 tickets")).toBeTruthy();
});

test("shows empty-filtered message when filters exclude all tickets", async () => {
  const fetchFn = vi.fn(async (): Promise<DashboardTicketsPayload> => ({
    tickets: [ticket({ ticket: "WIKI-135", status: "working" })],
    repo_allowlist: [],
  }));

  render(<DashboardView fetchTickets={fetchFn} pollMs={60_000} />);
  await screen.findByText("WIKI-135");

  const stateTrigger = screen.getByRole("button", { name: /^State$/ });
  fireEvent.click(stateTrigger);
  const blockedOption = await screen.findByRole("checkbox", { name: "working" });
  await act(async () => {
    fireEvent.click(blockedOption);
  });
  // Toggle it off then set a state that no ticket has via date filter
  await act(async () => {
    fireEvent.click(screen.getByRole("checkbox", { name: "working" }));
  });
  const fromInput = screen.getByLabelText("Filter tickets from date") as HTMLInputElement;
  await act(async () => {
    fireEvent.change(fromInput, { target: { value: "2099-01-01" } });
  });
  expect(screen.getByText("No tickets match the current filters.")).toBeTruthy();
  expect(screen.getByText("0 / 1 tickets")).toBeTruthy();
});
