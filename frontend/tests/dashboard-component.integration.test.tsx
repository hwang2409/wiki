// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import type { DashboardTicketsPayload } from "../src/dashboard";
import { DashboardView } from "../src/dashboard";

const EMPTY_PAYLOAD: DashboardTicketsPayload = { tickets: [], repo_allowlist: [] };

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

afterEach(() => {
  cleanup();
  vi.useRealTimers();
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

test("renders the backend-filtered implementation payload without changing its count", async () => {
  const fetchFn = vi.fn(async (): Promise<DashboardTicketsPayload> => ({
    tickets: [
      ticket({ ticket: "WIKI-IMPLEMENT", role: "implement" }),
      ticket({ ticket: "WIKI-LEGACY", role: null, live: false }),
    ],
    repo_allowlist: [],
  }));

  render(<DashboardView fetchTickets={fetchFn} pollMs={60_000} />);

  expect(await screen.findByText("WIKI-IMPLEMENT")).toBeTruthy();
  expect(screen.getByText("WIKI-LEGACY")).toBeTruthy();
  expect(screen.getByText("2 tickets")).toBeTruthy();
  expect(screen.queryByText("WIKI-REVIEW1")).toBeNull();
  expect(screen.queryByText("WIKI-SIM1")).toBeNull();
  expect(screen.queryByText("WIKI-ORCH")).toBeNull();
});
