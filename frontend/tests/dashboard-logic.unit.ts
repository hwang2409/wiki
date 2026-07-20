import assert from "node:assert/strict";
import test from "node:test";

import type { DashboardTicket } from "../src/api.ts";
import {
  collectProjects,
  collectStates,
  compareTickets,
  emptyFilters,
  filterTickets,
  filtersActive,
  parseStoredFilters,
  startDashboardPolling,
  ticketMatchesFilters,
  ticketProject,
} from "../src/dashboard-logic.ts";

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

test("ticketProject splits on the last hyphen", () => {
  assert.equal(ticketProject("WIKI-134"), "WIKI");
  assert.equal(ticketProject("PHO-14053"), "PHO");
  assert.equal(ticketProject("MITMWEB-B2"), "MITMWEB");
  assert.equal(ticketProject("ORG-TEAM-42"), "ORG-TEAM");
  assert.equal(ticketProject("NOHYPHEN"), "NOHYPHEN");
});

test("collectProjects/collectStates returns sorted unique values", () => {
  const rows = [
    ticket({ ticket: "WIKI-1", status: "working" }),
    ticket({ ticket: "PHO-2", status: "merge-ready" }),
    ticket({ ticket: "WIKI-3", status: "working" }),
    ticket({ ticket: "MITMWEB-B4", status: "blocked" }),
  ];
  assert.deepEqual(collectProjects(rows), ["MITMWEB", "PHO", "WIKI"]);
  assert.deepEqual(collectStates(rows), ["blocked", "merge-ready", "working"]);
});

test("emptyFilters is inactive; setting any dimension makes it active", () => {
  const f = emptyFilters();
  assert.equal(filtersActive(f), false);
  assert.equal(filtersActive({ ...f, projects: ["WIKI"] }), true);
  assert.equal(filtersActive({ ...f, states: ["working"] }), true);
  assert.equal(filtersActive({ ...f, dateFrom: "2026-07-01" }), true);
  assert.equal(filtersActive({ ...f, dateTo: "2026-07-31" }), true);
});

test("project filter keeps only tickets whose prefix is in the selection (OR within)", () => {
  const rows = [
    ticket({ ticket: "WIKI-1" }),
    ticket({ ticket: "PHO-2" }),
    ticket({ ticket: "MITMWEB-B3" }),
  ];
  const out = filterTickets(rows, { ...emptyFilters(), projects: ["WIKI", "MITMWEB"] });
  assert.deepEqual(
    out.map((r) => r.ticket),
    ["WIKI-1", "MITMWEB-B3"]
  );
});

test("state filter keeps only tickets whose status is in the selection", () => {
  const rows = [
    ticket({ ticket: "A-1", status: "working" }),
    ticket({ ticket: "A-2", status: "merge-ready" }),
    ticket({ ticket: "A-3", status: "blocked" }),
  ];
  const out = filterTickets(rows, { ...emptyFilters(), states: ["working", "blocked"] });
  assert.deepEqual(
    out.map((r) => r.ticket),
    ["A-1", "A-3"]
  );
});

test("date bounds are inclusive on both ends and each bound is optional", () => {
  const rows = [
    ticket({ ticket: "A-1", date: "2026-07-01T00:00:00Z" }),
    ticket({ ticket: "A-2", date: "2026-07-15T23:59:59Z" }),
    ticket({ ticket: "A-3", date: "2026-07-31T12:00:00Z" }),
    ticket({ ticket: "A-4", date: null }),
  ];
  const both = filterTickets(rows, {
    ...emptyFilters(),
    dateFrom: "2026-07-01",
    dateTo: "2026-07-31",
  });
  assert.deepEqual(
    both.map((r) => r.ticket),
    ["A-1", "A-2", "A-3"],
    "inclusive on both bounds; null-date rows excluded when any bound set"
  );
  const openLower = filterTickets(rows, { ...emptyFilters(), dateTo: "2026-07-15" });
  assert.deepEqual(
    openLower.map((r) => r.ticket),
    ["A-1", "A-2"]
  );
  const openUpper = filterTickets(rows, { ...emptyFilters(), dateFrom: "2026-07-15" });
  assert.deepEqual(
    openUpper.map((r) => r.ticket),
    ["A-2", "A-3"]
  );
});

test("filters compose with AND across dimensions", () => {
  const rows = [
    ticket({ ticket: "WIKI-1", status: "working", date: "2026-07-05T00:00:00Z" }),
    ticket({ ticket: "WIKI-2", status: "blocked", date: "2026-07-05T00:00:00Z" }),
    ticket({ ticket: "PHO-3", status: "working", date: "2026-07-05T00:00:00Z" }),
    ticket({ ticket: "WIKI-4", status: "working", date: "2026-08-05T00:00:00Z" }),
  ];
  const out = filterTickets(rows, {
    projects: ["WIKI"],
    states: ["working"],
    dateFrom: "2026-07-01",
    dateTo: "2026-07-31",
  });
  assert.deepEqual(
    out.map((r) => r.ticket),
    ["WIKI-1"]
  );
});

test("filterTickets is a no-op when no filters are active", () => {
  const rows = [ticket({ ticket: "A-1" }), ticket({ ticket: "B-2" })];
  const out = filterTickets(rows, emptyFilters());
  assert.equal(out, rows, "returns the same array reference when inactive");
});

test("ticketMatchesFilters handles null date row correctly", () => {
  const row = ticket({ ticket: "A-1", date: null });
  assert.equal(ticketMatchesFilters(row, emptyFilters()), true);
  assert.equal(
    ticketMatchesFilters(row, { ...emptyFilters(), dateFrom: "2026-01-01" }),
    false,
    "null date excluded when any date bound is set"
  );
});

test("parseStoredFilters tolerates missing/malformed input", () => {
  assert.deepEqual(parseStoredFilters(null), emptyFilters());
  assert.deepEqual(parseStoredFilters(""), emptyFilters());
  assert.deepEqual(parseStoredFilters("not-json"), emptyFilters());
  assert.deepEqual(parseStoredFilters("[]"), emptyFilters());
  assert.deepEqual(
    parseStoredFilters(
      JSON.stringify({ projects: ["WIKI", 3, null], states: ["working"], dateFrom: "2026-07-01" })
    ),
    { projects: ["WIKI"], states: ["working"], dateFrom: "2026-07-01", dateTo: null }
  );
});

test("parseStoredFilters roundtrips a serialized active filter", () => {
  const original = {
    projects: ["WIKI", "PHO"],
    states: ["working", "merge-ready"],
    dateFrom: "2026-07-01",
    dateTo: "2026-07-31",
  };
  assert.deepEqual(parseStoredFilters(JSON.stringify(original)), original);
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
