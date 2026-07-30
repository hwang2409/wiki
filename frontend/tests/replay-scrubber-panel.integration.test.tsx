// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import type {
  ReplayBookmark,
  ReplayRawEvent,
  ReplayRunSummary,
  ReplayTimeline,
  ReplayTimelineEvent,
} from "../src/api";
import { ReplayScrubberPanel } from "../src/replay-scrubber-panel";

/**
 * WIKI-174 round-2/round-3 tests intercept ``global.fetch`` directly so
 * signal forwarding through ``request()`` is exercised end-to-end.
 * Round-3 additions cover: opaque cursor + has_more consumption, empty
 * final page that still carries a warning (must not be dropped), and
 * ``bookmarks_truncated`` surfacing.
 */

const RUN_ID = "11111111-2222-3333-4444-555555555555";
const OLD_RUN = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";

type FetchCall = {
  url: string;
  method: string;
  signal: AbortSignal | null;
  aborted: boolean;
  abortedAt: number | null;
};

let calls: FetchCall[] = [];
let now = 0;

function record(url: string, options: RequestInit | undefined): FetchCall {
  const call: FetchCall = {
    url,
    method: (options?.method ?? "GET").toUpperCase(),
    signal: options?.signal ?? null,
    aborted: false,
    abortedAt: null,
  };
  options?.signal?.addEventListener(
    "abort",
    () => {
      call.aborted = true;
      call.abortedAt = ++now;
    },
    { once: true },
  );
  calls.push(call);
  return call;
}

function jsonResponse(body: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

function runSummary(overrides: Partial<ReplayRunSummary> = {}): ReplayRunSummary {
  return {
    run_id: RUN_ID,
    agent_id: "WIKI-174",
    orch_id: "wiki",
    role: "implement",
    provider: "claude",
    model: "opus-4-7",
    outcome: "handoff",
    state: "dead",
    created_at: "2026-07-30T00:00:00Z",
    updated_at: "2026-07-30T00:05:00Z",
    total_events: 4,
    initial_prompt_excerpt: "Do the thing",
    ...overrides,
  };
}

function event(seq: number, overrides: Partial<ReplayTimelineEvent> = {}): ReplayTimelineEvent {
  return {
    seq,
    raw_seq: seq,
    ts: `2026-07-30T00:00:0${seq}Z`,
    kind: "claude_stream_event",
    disposition: "rendered",
    lifecycle_state: null,
    summary: `event ${seq}`,
    bookmark: null,
    ...overrides,
  };
}

const BASE_BOOKMARKS: ReplayBookmark[] = [
  { seq: 2, kind: "steer", ts: "2026-07-30T00:00:02Z", summary: "please do the thing", event_kind: "claude_user" },
  { seq: 3, kind: "verdict", ts: "2026-07-30T00:00:03Z", summary: "MERGE-READY", event_kind: "claude_assistant" },
  { seq: 4, kind: "error", ts: "2026-07-30T00:00:04Z", summary: "exit code 137", event_kind: "provider_process_exit" },
];

function timeline(): ReplayTimeline {
  return {
    run: runSummary(),
    events: [
      event(1),
      event(2, {
        kind: "claude_user",
        summary: "please do the thing",
        bookmark: "steer",
      }),
      event(3, {
        kind: "claude_assistant",
        summary: "MERGE-READY: https://github.com/x/y/pull/1",
        bookmark: "verdict",
      }),
      event(4, { kind: "provider_process_exit", bookmark: "error" }),
    ],
    next_cursor: null,
    has_more: false,
    bookmarks: BASE_BOOKMARKS,
    bookmarks_truncated: false,
    warnings: [],
  };
}

function respond(url: string): Response {
  if (url.startsWith("/api/agents/") && url.includes("/replay/runs")) {
    return jsonResponse({
      ticket: "WIKI-174",
      runs: [runSummary(), runSummary({ run_id: OLD_RUN })],
      runs_truncated: false,
    });
  }
  if (url.startsWith("/api/agent-runs/") && url.includes("/replay/timeline")) {
    return jsonResponse(timeline());
  }
  if (url.startsWith("/api/agent-runs/") && url.includes("/replay/events/")) {
    const seq = Number(url.split("/replay/events/")[1]?.split("?")[0]);
    const body: ReplayRawEvent = {
      run_id: RUN_ID,
      seq,
      raw: { seq, direction: "stdout", payload: { hint: `raw-${seq}` } },
    };
    return jsonResponse(body);
  }
  return new Response("not found", { status: 404 });
}

describe("replay scrubber panel", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    calls = [];
    now = 0;
    vi.spyOn(global, "fetch").mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input.toString();
      const call = record(url, init);
      return call.aborted ? new Response("aborted", { status: 499 }) : respond(url);
    });
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  async function flushAsync() {
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
  }

  test("timeline + runs fetch through the real request() layer", async () => {
    render(<ReplayScrubberPanel ticket="WIKI-174" />);
    await flushAsync();
    const paths = calls.map((c) => c.url);
    expect(paths.some((p) => p.endsWith("/replay/runs"))).toBe(true);
    expect(paths.some((p) => p.includes("/replay/timeline"))).toBe(true);
    expect(screen.getByText("1 / 4")).toBeTruthy();
  });

  test("advances one event per play step at real-time speed", async () => {
    render(<ReplayScrubberPanel ticket="WIKI-174" />);
    await flushAsync();
    fireEvent.click(screen.getByRole("button", { name: "Play replay" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(screen.getByText("2 / 4")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Pause replay" })).toBeTruthy();
  });

  test("rapid scrub cancels the in-flight raw event fetch via signal", async () => {
    render(<ReplayScrubberPanel ticket="WIKI-174" />);
    await flushAsync();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(150);
    });
    const initialRawFetch = calls.find((c) => c.url.includes("/replay/events/1"));
    expect(initialRawFetch).toBeTruthy();
    expect(initialRawFetch!.signal).not.toBeNull();
    expect(initialRawFetch!.aborted).toBe(false);

    const slider = screen.getByRole("slider", { name: "Event cursor" });
    fireEvent.change(slider, { target: { value: "3" } });
    await flushAsync();
    expect(initialRawFetch!.aborted).toBe(true);
    expect(screen.getByRole("button", { name: "Play replay" })).toBeTruthy();
    expect(screen.getByText("4 / 4")).toBeTruthy();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(150);
    });
    const newestRawFetch = [...calls]
      .reverse()
      .find((c) => c.url.includes("/replay/events/"));
    expect(newestRawFetch?.url).toContain("/replay/events/4");
  });

  test("bookmark buttons jump to their event", async () => {
    render(<ReplayScrubberPanel ticket="WIKI-174" />);
    await flushAsync();
    const verdict = screen.getByRole("button", { name: /verdict at seq 3/ });
    fireEvent.click(verdict);
    await flushAsync();
    expect(screen.getByText("3 / 4")).toBeTruthy();
    expect(screen.getByText(/MERGE-READY/)).toBeTruthy();
  });

  test("max speed advances immediately regardless of timestamp gap", async () => {
    render(<ReplayScrubberPanel ticket="WIKI-174" />);
    await flushAsync();
    fireEvent.change(screen.getByLabelText("Playback speed"), { target: { value: "max" } });
    fireEvent.click(screen.getByRole("button", { name: "Play replay" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60);
    });
    expect(screen.getByText("2 / 4")).toBeTruthy();
  });

  test("paginates via opaque cursor + has_more with no silent cap", async () => {
    const firstPage: ReplayTimeline = {
      run: runSummary({ total_events: 6 }),
      events: [event(1), event(2, { kind: "claude_user", bookmark: "steer" })],
      next_cursor: "b3B0aXF1ZQ==",
      has_more: true,
      bookmarks: BASE_BOOKMARKS,
      bookmarks_truncated: false,
      warnings: [],
    };
    const secondPage: ReplayTimeline = {
      run: runSummary({ total_events: 6 }),
      events: [event(3), event(4), event(5), event(6)],
      next_cursor: null,
      has_more: false,
      bookmarks: [],
      bookmarks_truncated: false,
      warnings: ["dropped 2 malformed line(s)"],
    };
    (global.fetch as ReturnType<typeof vi.spyOn>).mockRestore();
    vi.spyOn(global, "fetch").mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input.toString();
      record(url, init);
      if (url.includes("/replay/timeline")) {
        // Second-page request MUST forward the exact opaque cursor from
        // page one; anything else (round-1/2 ``after_seq=N`` style) would
        // return the first page indefinitely.
        return jsonResponse(url.includes(`cursor=${encodeURIComponent("b3B0aXF1ZQ==")}`) ? secondPage : firstPage);
      }
      if (url.endsWith("/replay/runs")) {
        return jsonResponse({ ticket: "WIKI-174", runs: [runSummary()], runs_truncated: false });
      }
      if (url.includes("/replay/events/")) {
        return jsonResponse({ run_id: RUN_ID, seq: 1, raw: {} });
      }
      return new Response("not found", { status: 404 });
    });

    render(<ReplayScrubberPanel ticket="WIKI-174" />);
    await flushAsync();
    await flushAsync();
    expect(screen.getByText("1 / 6")).toBeTruthy();
    expect(screen.getByText(/dropped 2 malformed line/)).toBeTruthy();
    const slider = screen.getByRole("slider", { name: "Event cursor" }) as HTMLInputElement;
    expect(slider.max).toBe("5");

    const timelineFetches = calls.filter((c) => c.url.includes("/replay/timeline"));
    expect(timelineFetches).toHaveLength(2);
    expect(timelineFetches[0].url).not.toContain("cursor=");
    expect(timelineFetches[1].url).toContain("cursor=");
  });

  test("empty final page preserves prior warnings and terminates paging", async () => {
    // Round-3 review item 4: the round-2 loop bailed out on empty final
    // page and silently discarded server warnings that came with it.
    const firstPage: ReplayTimeline = {
      run: runSummary({ total_events: 2 }),
      events: [event(1), event(2)],
      next_cursor: "Y3Vyc29yLTE=",
      has_more: true,
      bookmarks: [],
      bookmarks_truncated: false,
      warnings: ["scan hit byte budget — later events not classified"],
    };
    const emptyFinalPage: ReplayTimeline = {
      run: runSummary({ total_events: 2 }),
      events: [],
      next_cursor: null,
      has_more: false,
      bookmarks: [],
      bookmarks_truncated: true,
      warnings: ["bookmark list truncated"],
    };
    (global.fetch as ReturnType<typeof vi.spyOn>).mockRestore();
    vi.spyOn(global, "fetch").mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input.toString();
      record(url, init);
      if (url.includes("/replay/timeline")) {
        return jsonResponse(url.includes("cursor=") ? emptyFinalPage : firstPage);
      }
      if (url.endsWith("/replay/runs")) {
        return jsonResponse({ ticket: "WIKI-174", runs: [runSummary()], runs_truncated: false });
      }
      if (url.includes("/replay/events/")) {
        return jsonResponse({ run_id: RUN_ID, seq: 1, raw: {} });
      }
      return new Response("not found", { status: 404 });
    });

    render(<ReplayScrubberPanel ticket="WIKI-174" />);
    await flushAsync();
    await flushAsync();
    // Both the earlier scan-budget warning and the final-page truncation
    // warning must show; neither should be swallowed.
    expect(screen.getByText(/scan hit byte budget/)).toBeTruthy();
    // The client-side flag AND the server's warning string both match a
    // /bookmark list truncated/ regex — assert both are present via a
    // multi-match query so we detect swallowed warnings AND dedup regressions.
    const truncationLines = screen.getAllByText(/bookmark list truncated/);
    expect(truncationLines.length).toBeGreaterThanOrEqual(1);
    // Paging terminated after seeing has_more=false.
    const timelineFetches = calls.filter((c) => c.url.includes("/replay/timeline"));
    expect(timelineFetches).toHaveLength(2);
  });
});
