// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { DashboardTicketsPayload } from "../src/dashboard";
import { DashboardView } from "../src/dashboard";

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

function stubCosts() {
  return vi.fn(async () => EMPTY_COSTS);
}

function ticketRow(overrides: Partial<DashboardTicketsPayload["tickets"][number]> = {}) {
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

test("renders a skeleton table while the initial fetch is still pending", async () => {
  const gate: { resolve: (v: DashboardTicketsPayload) => void } = { resolve: () => {} };
  const fetchFn = vi.fn(
    () =>
      new Promise<DashboardTicketsPayload>((resolve) => {
        gate.resolve = resolve;
      })
  );

  render(<DashboardView fetchTickets={fetchFn} fetchCosts={stubCosts()} pollMs={60_000} />);

  const skeleton = await screen.findByTestId("dashboard-skeleton");
  expect(skeleton).toBeTruthy();
  expect(skeleton.querySelectorAll(".dashboard-skeleton-cell").length).toBeGreaterThan(0);

  await act(async () => {
    gate.resolve({ tickets: [ticketRow()], repo_allowlist: [] });
    await Promise.resolve();
    await Promise.resolve();
  });

  expect(screen.queryByTestId("dashboard-skeleton")).toBeNull();
  expect(screen.getByText("WIKI-1")).toBeTruthy();
});

test("initial load error shows retry affordance and no skeleton", async () => {
  const fetchFn = vi.fn(async () => {
    throw new Error("network down");
  });

  render(<DashboardView fetchTickets={fetchFn} fetchCosts={stubCosts()} pollMs={60_000} />);

  const errorNode = await screen.findByRole("alert");
  expect(errorNode.textContent).toContain("network down");
  const retryBtn = screen.getByRole("button", { name: /Try again/i });
  expect(retryBtn).toBeTruthy();
  expect(screen.queryByTestId("dashboard-skeleton")).toBeNull();
});

test("refresh failure after success surfaces stale banner but keeps last-good tickets", async () => {
  vi.useFakeTimers();
  let call = 0;
  const fetchFn = vi.fn(async (): Promise<DashboardTicketsPayload> => {
    call++;
    if (call === 1) {
      return { tickets: [ticketRow({ ticket: "WIKI-42" })], repo_allowlist: [] };
    }
    throw new Error("refresh boom");
  });

  render(<DashboardView fetchTickets={fetchFn} fetchCosts={stubCosts()} pollMs={5_000} />);

  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(screen.getByText("WIKI-42")).toBeTruthy();

  // Second poll fails.
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5_100);
    await Promise.resolve();
    await Promise.resolve();
  });

  // Ticket still visible — never blanked.
  expect(screen.getByText("WIKI-42")).toBeTruthy();
  const stale = screen.getByTestId("dashboard-stale-banner");
  expect(stale.textContent).toContain("stale");
  expect(stale.textContent).toContain("refresh boom");
  expect(screen.getByRole("button", { name: /Retry/i })).toBeTruthy();
});

test("retry button on stale banner triggers immediate fetch and clears banner on success", async () => {
  vi.useFakeTimers();
  let call = 0;
  const fetchFn = vi.fn(async (): Promise<DashboardTicketsPayload> => {
    call++;
    if (call === 1) return { tickets: [ticketRow({ ticket: "WIKI-42" })], repo_allowlist: [] };
    if (call === 2) throw new Error("refresh boom");
    return { tickets: [ticketRow({ ticket: "WIKI-42", description: "recovered" })], repo_allowlist: [] };
  });

  render(<DashboardView fetchTickets={fetchFn} fetchCosts={stubCosts()} pollMs={5_000} />);
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5_100);
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(screen.getByTestId("dashboard-stale-banner")).toBeTruthy();

  const retry = screen.getByRole("button", { name: /Retry/i });
  await act(async () => {
    fireEvent.click(retry);
    await Promise.resolve();
    await Promise.resolve();
  });

  expect(screen.queryByTestId("dashboard-stale-banner")).toBeNull();
  expect(screen.getByText("recovered")).toBeTruthy();
  expect(call).toBe(3);
});

test("ticket and cost failures render one stale label", async () => {
  vi.useFakeTimers();
  let ticketCalls = 0;
  let costCalls = 0;
  const fetchTickets = vi.fn(async (): Promise<DashboardTicketsPayload> => {
    ticketCalls++;
    if (ticketCalls > 1) throw new Error("ticket refresh failed");
    return { tickets: [ticketRow({ ticket: "WIKI-42" })], repo_allowlist: [] };
  });
  const fetchCosts = vi.fn(async () => {
    costCalls++;
    if (costCalls > 1) throw new Error("cost refresh failed");
    return EMPTY_COSTS;
  });

  render(<DashboardView fetchTickets={fetchTickets} fetchCosts={fetchCosts} pollMs={5_000} />);
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await vi.advanceTimersByTimeAsync(5_100);
    await Promise.resolve();
    await Promise.resolve();
  });

  expect(screen.getAllByText("stale")).toHaveLength(1);
  expect(screen.getByText(/Cost data: cost refresh failed/)).toBeTruthy();
});

test("zero-tickets state (loaded, empty) reads differently than a failed load", async () => {
  const fetchFn = vi.fn(async (): Promise<DashboardTicketsPayload> => ({
    tickets: [],
    repo_allowlist: [],
  }));
  render(<DashboardView fetchTickets={fetchFn} fetchCosts={stubCosts()} pollMs={60_000} />);
  const zero = await screen.findByTestId("dashboard-zero-tickets");
  expect(zero.textContent).toContain("No tickets with workers or PRs yet");
  expect(screen.queryByTestId("dashboard-skeleton")).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
});

test("filters-empty state offers a clear-filters affordance", async () => {
  const fetchFn = vi.fn(async (): Promise<DashboardTicketsPayload> => ({
    tickets: [ticketRow({ ticket: "WIKI-42", status: "working" })],
    repo_allowlist: [],
  }));
  render(<DashboardView fetchTickets={fetchFn} fetchCosts={stubCosts()} pollMs={60_000} />);
  await screen.findByText("WIKI-42");

  const stateTrigger = screen.getByRole("button", { name: /^State$/ });
  fireEvent.click(stateTrigger);
  const opt = await screen.findByRole("checkbox", { name: "working" });
  await act(async () => {
    fireEvent.click(opt);
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("checkbox", { name: "working" }));
  });
  const fromInput = screen.getByLabelText("Filter tickets from date") as HTMLInputElement;
  await act(async () => {
    fireEvent.change(fromInput, { target: { value: "2099-01-01" } });
  });
  const empty = screen.getByTestId("dashboard-filters-empty");
  expect(empty.textContent).toContain("No tickets match");
  const clear = screen.getByRole("button", { name: /Clear filters/i });
  await act(async () => {
    fireEvent.click(clear);
  });
  expect(screen.queryByTestId("dashboard-filters-empty")).toBeNull();
  expect(screen.getByText("WIKI-42")).toBeTruthy();
});
