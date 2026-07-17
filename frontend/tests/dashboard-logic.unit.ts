import assert from "node:assert/strict";
import test from "node:test";

import type { DashboardTicket } from "../src/api.ts";
import { compareTickets, startDashboardPolling } from "../src/dashboard-logic.ts";

function ticket(overrides: Partial<DashboardTicket> = {}): DashboardTicket {
  return {
    ticket: "T-1",
    description: "d",
    pr: null,
    repo: null,
    enriched: false,
    status: "unknown",
    detail: null,
    date: null,
    live: false,
    role: null,
    kind: null,
    ...overrides,
  };
}

test("compareTickets returns 0 for equal primary values so sort stays stable", () => {
  const rows = [
    ticket({ ticket: "B", date: "2026-01-01T00:00:00Z" }),
    ticket({ ticket: "A", date: "2026-01-01T00:00:00Z" }),
    ticket({ ticket: "C", date: "2026-01-01T00:00:00Z" }),
  ];
  const ascending = [...rows].sort((a, b) => compareTickets(a, b, "date", true));
  const descending = [...rows].sort((a, b) => compareTickets(a, b, "date", false));
  assert.deepEqual(
    ascending.map((r) => r.ticket),
    ["B", "A", "C"],
    "tied rows keep input order in ascending sort"
  );
  assert.deepEqual(
    descending.map((r) => r.ticket),
    ["B", "A", "C"],
    "tied rows keep input order in descending sort"
  );
});

test("compareTickets flips sign for descending on distinct values", () => {
  const a = ticket({ ticket: "A", date: "2026-01-01T00:00:00Z" });
  const b = ticket({ ticket: "B", date: "2026-02-01T00:00:00Z" });
  assert.ok(compareTickets(a, b, "date", true) < 0);
  assert.ok(compareTickets(a, b, "date", false) > 0);
});

class FakeTimer {
  private nextId = 1;
  private pending = new Map<number, () => void>();
  schedule = (cb: () => void, _ms: number): number => {
    const id = this.nextId++;
    this.pending.set(id, cb);
    return id;
  };
  cancel = (id: number): void => {
    this.pending.delete(id);
  };
  fire(): void {
    const entries = Array.from(this.pending.entries());
    this.pending.clear();
    for (const [, cb] of entries) cb();
  }
  size(): number {
    return this.pending.size;
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

test("polling never overlaps requests — next fetch only fires after current settles", async () => {
  const timer = new FakeTimer();
  const gates = [deferred<string>(), deferred<string>(), deferred<string>()];
  const started: AbortSignal[] = [];
  let callIdx = 0;
  const handle = startDashboardPolling<string>({
    fetch: (signal) => {
      started.push(signal);
      return gates[callIdx++].promise;
    },
    onData: () => {},
    onError: () => {},
    intervalMs: 1000,
    setTimeoutFn: timer.schedule,
    clearTimeoutFn: timer.cancel,
  });
  // First load fired immediately; no timer yet, no second request.
  await Promise.resolve();
  assert.equal(started.length, 1);
  assert.equal(timer.size(), 0);
  // Firing the timer before the fetch settles must NOT start a second fetch.
  timer.fire();
  await Promise.resolve();
  assert.equal(started.length, 1, "no overlap while first fetch is pending");
  // Settle the first fetch → next poll is scheduled.
  gates[0].resolve("a");
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(timer.size(), 1, "next poll scheduled after settle");
  // Fire the scheduled timer → second fetch begins.
  timer.fire();
  await Promise.resolve();
  assert.equal(started.length, 2);
  gates[1].resolve("b");
  await Promise.resolve();
  handle.stop();
});

test("stop() aborts the inflight signal on unmount", async () => {
  const timer = new FakeTimer();
  const gate = deferred<string>();
  let signal: AbortSignal | null = null;
  const handle = startDashboardPolling<string>({
    fetch: (s) => {
      signal = s;
      return gate.promise;
    },
    onData: () => {
      throw new Error("onData should not fire after stop()");
    },
    onError: () => {
      throw new Error("onError should not fire after stop()");
    },
    intervalMs: 1000,
    setTimeoutFn: timer.schedule,
    clearTimeoutFn: timer.cancel,
  });
  await Promise.resolve();
  assert.ok(signal);
  assert.equal(signal!.aborted, false);
  handle.stop();
  assert.equal(signal!.aborted, true, "inflight signal aborted on stop");
  // Settle the promise after stop → no state callbacks fire (throws would fail).
  gate.resolve("late");
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(timer.size(), 0, "no new poll scheduled after stop");
});
