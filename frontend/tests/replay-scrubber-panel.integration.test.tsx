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
 * WIKI-174 round-2 review item 6: the round-1 tests mocked the whole api
 * module, so deleting ``signal`` forwarding in ``getReplayRawEvent`` still
 * passed. Round 2 tests intercept ``global.fetch`` directly, so the panel's
 * abort behaviour is verified through the real ``request()`` → ``fetch``
 * signal-forwarding path.
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
    next_after_seq: null,
    bookmarks: BASE_BOOKMARKS,
    warnings: [],
  };
}

function respond(url: string): Response {
  if (url.startsWith("/api/agents/") && url.includes("/replay/runs")) {
    return jsonResponse({
      ticket: "WIKI-174",
      runs: [runSummary(), runSummary({ run_id: OLD_RUN })],
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
      // Return synchronously (Promise.resolve) so scheduled tests can await
      // and advance timers in a predictable order.
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
      // Real gap 1 → 2 = 1 second; at speed 1x = 1000ms.
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(screen.getByText("2 / 4")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Pause replay" })).toBeTruthy();
  });

  test("rapid scrub cancels the in-flight raw event fetch via signal", async () => {
    render(<ReplayScrubberPanel ticket="WIKI-174" />);
    await flushAsync();
    // Let the initial cursor's debounced raw fetch actually fire.
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
    // The scrub tore down the prior effect, which must have aborted the
    // in-flight raw fetch's signal. If ``getReplayRawEvent`` stopped
    // forwarding the signal this expectation would fail.
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

  test("paginates until server drains the cursor with no silent cap", async () => {
    // Return two pages: first has next_after_seq=2, second is empty.
    const firstPage: ReplayTimeline = {
      run: runSummary({ total_events: 6 }),
      events: [event(1), event(2, { kind: "claude_user", bookmark: "steer" })],
      next_after_seq: 2,
      bookmarks: BASE_BOOKMARKS,
      warnings: [],
    };
    const secondPage: ReplayTimeline = {
      run: runSummary({ total_events: 6 }),
      events: [event(3), event(4), event(5), event(6)],
      next_after_seq: null,
      bookmarks: [],
      warnings: ["dropped 2 malformed line(s)"],
    };
    (global.fetch as ReturnType<typeof vi.spyOn>).mockRestore();
    vi.spyOn(global, "fetch").mockImplementation(async (input) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.includes("/replay/timeline")) {
        return jsonResponse(url.includes("after_seq=2") ? secondPage : firstPage);
      }
      if (url.endsWith("/replay/runs")) {
        return jsonResponse({ ticket: "WIKI-174", runs: [runSummary()] });
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
    // Server-provided warning is surfaced in the UI.
    expect(screen.getByText(/dropped 2 malformed line/)).toBeTruthy();
    // The slider can reach the last of the 6 events (no 5000-style silent cap).
    const slider = screen.getByRole("slider", { name: "Event cursor" }) as HTMLInputElement;
    expect(slider.max).toBe("5");
  });
});
