// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import type {
  ReplayBookmark,
  ReplayRawEvent,
  ReplayRunSummary,
  ReplayTimeline,
  ReplayTimelineEvent,
} from "../src/api";
import { REPLAY_TUNABLES, ReplayScrubberPanel } from "../src/replay-scrubber-panel";

/**
 * Round-4 test rewrite. The panel is now demand-driven: it holds a bounded
 * WINDOW of events and paginates via opaque cursor as playback advances.
 * These tests intercept ``global.fetch`` directly so signal forwarding
 * through ``request()`` is exercised end-to-end.
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

const calls: FetchCall[] = [];
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
  const secondsInMinute = String((seq - 1) % 60).padStart(2, "0");
  return {
    seq,
    raw_seq: seq,
    ts: `2026-07-30T00:00:${secondsInMinute}Z`,
    kind: "claude_stream_event",
    disposition: "rendered",
    lifecycle_state: null,
    summary: `event ${seq}`,
    bookmark: null,
    ...overrides,
  };
}

const BASE_BOOKMARKS: ReplayBookmark[] = [
  { seq: 2, kind: "steer", ts: "2026-07-30T00:00:01Z", summary: "please do the thing", event_kind: "claude_user" },
  { seq: 3, kind: "verdict", ts: "2026-07-30T00:00:02Z", summary: "MERGE-READY", event_kind: "claude_assistant" },
  { seq: 4, kind: "error", ts: "2026-07-30T00:00:03Z", summary: "exit code 137", event_kind: "provider_process_exit" },
];

function singlePageTimeline(): ReplayTimeline {
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

function respondDefault(url: string): Response {
  if (url.startsWith("/api/agents/") && url.includes("/replay/runs")) {
    return jsonResponse({
      ticket: "WIKI-174",
      runs: [runSummary(), runSummary({ run_id: OLD_RUN })],
      runs_truncated: false,
    });
  }
  if (url.startsWith("/api/agent-runs/") && url.includes("/replay/timeline")) {
    return jsonResponse(singlePageTimeline());
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

const ORIGINAL_TUNABLES = { ...REPLAY_TUNABLES };

describe("replay scrubber panel", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    calls.length = 0;
    now = 0;
    // Reset to production defaults; tests can override per-case.
    Object.assign(REPLAY_TUNABLES, ORIGINAL_TUNABLES);
    vi.spyOn(global, "fetch").mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input.toString();
      const call = record(url, init);
      return call.aborted ? new Response("aborted", { status: 499 }) : respondDefault(url);
    });
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.restoreAllMocks();
    Object.assign(REPLAY_TUNABLES, ORIGINAL_TUNABLES);
  });

  async function flushAsync(times = 3) {
    for (let i = 0; i < times; i++) {
      await act(async () => {
        await Promise.resolve();
      });
    }
  }

  test("timeline + runs fetch through the real request() layer", async () => {
    render(<ReplayScrubberPanel ticket="WIKI-174" />);
    await flushAsync();
    const paths = calls.map((c) => c.url);
    expect(paths.some((p) => p.endsWith("/replay/runs"))).toBe(true);
    expect(paths.some((p) => p.includes("/replay/timeline"))).toBe(true);
    expect(screen.getByText("1 / 4")).toBeTruthy();
  });

  test("advances one event per play tick at real-time speed", async () => {
    render(<ReplayScrubberPanel ticket="WIKI-174" />);
    await flushAsync();
    fireEvent.click(screen.getByRole("button", { name: "Play replay" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(screen.getByText("2 / 4")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Pause replay" })).toBeTruthy();
  });

  test("rapid scrub cancels in-flight raw event fetch via signal", async () => {
    render(<ReplayScrubberPanel ticket="WIKI-174" />);
    await flushAsync();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(150);
    });
    const initialRawFetch = calls.find((c) => c.url.includes("/replay/events/1"));
    expect(initialRawFetch).toBeTruthy();
    expect(initialRawFetch!.signal).not.toBeNull();

    const slider = screen.getByRole("slider", { name: "Event cursor" });
    fireEvent.change(slider, { target: { value: "3" } });
    await flushAsync();
    expect(initialRawFetch!.aborted).toBe(true);
    expect(screen.getByText("4 / 4")).toBeTruthy();
  });

  test("ignores a delayed raw payload after the cursor moves on", async () => {
    let resolveFirst: ((response: Response) => void) | null = null;
    const timeline = singlePageTimeline();
    timeline.events = [
      event(1, { kind: "claude_assistant", summary: "timeline one" }),
      event(2, { kind: "claude_assistant", summary: "timeline two" }),
      event(3, { kind: "claude_assistant", summary: "timeline three" }),
      event(4, { kind: "claude_assistant", summary: "timeline four" }),
    ];
    (global.fetch as ReturnType<typeof vi.fn>).mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input.toString();
      record(url, init);
      if (url.includes("/replay/timeline")) return jsonResponse(timeline);
      if (url.includes("/replay/events/1")) {
        return new Promise<Response>((resolve) => {
          resolveFirst = resolve;
        });
      }
      if (url.includes("/replay/events/2")) {
        return jsonResponse({
          run_id: RUN_ID,
          seq: 2,
          raw: { payload: { message: { role: "assistant", content: [{ type: "text", text: "new payload" }] } } },
        });
      }
      return respondDefault(url);
    });

    render(<ReplayScrubberPanel ticket="WIKI-174" />);
    await flushAsync();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(120);
    });
    expect(resolveFirst).toBeTruthy();

    fireEvent.change(screen.getByRole("slider", { name: "Event cursor" }), { target: { value: "1" } });
    await flushAsync();
    expect(screen.getByText("timeline two")).toBeTruthy();
    expect(screen.queryByText("old payload")).toBeNull();
    expect(screen.getByText("loading event…")).toBeTruthy();

    await act(async () => {
      resolveFirst!(jsonResponse({
        run_id: RUN_ID,
        seq: 1,
        raw: { payload: { message: { role: "assistant", content: [{ type: "text", text: "old payload" }] } } },
      }));
      await Promise.resolve();
    });
    expect(screen.queryByText("old payload")).toBeNull();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(120);
    });
    await flushAsync();
    expect(screen.getByText("new payload")).toBeTruthy();
    expect(screen.queryByText("old payload")).toBeNull();
  });

  test("max-speed stepping shows summaries and settles on the final payload", async () => {
    const timeline = singlePageTimeline();
    timeline.events = timeline.events.map((current) => ({
      ...current,
      kind: "claude_assistant",
      summary: `timeline ${current.seq}`,
    }));
    (global.fetch as ReturnType<typeof vi.fn>).mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input.toString();
      record(url, init);
      if (url.includes("/replay/timeline")) return jsonResponse(timeline);
      if (url.includes("/replay/events/")) {
        const seq = Number(url.split("/replay/events/")[1]?.split("?")[0]);
        return jsonResponse({
          run_id: RUN_ID,
          seq,
          raw: { payload: { message: { role: "assistant", content: [{ type: "text", text: `payload ${seq}` }] } } },
        });
      }
      return respondDefault(url);
    });

    render(<ReplayScrubberPanel ticket="WIKI-174" />);
    await flushAsync();
    fireEvent.change(screen.getByLabelText("Playback speed"), { target: { value: "max" } });
    fireEvent.click(screen.getByRole("button", { name: "Play replay" }));
    for (let step = 0; step < 3; step += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(40);
      });
      await flushAsync();
    }

    expect(screen.getByText("4 / 4")).toBeTruthy();
    expect(screen.queryByText("payload 1")).toBeNull();
    expect(screen.queryByText("payload 2")).toBeNull();
    expect(screen.queryByText("payload 3")).toBeNull();
    expect(screen.getByText("loading event…")).toBeTruthy();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(120);
    });
    await flushAsync();
    expect(screen.getByText("payload 4")).toBeTruthy();
    expect(screen.queryByText("payload 1")).toBeNull();
    expect(screen.queryByText("payload 2")).toBeNull();
    expect(screen.queryByText("payload 3")).toBeNull();
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

  // -------------------------------------------------------------------------
  // Round-4 review item 2: bounded window + demand-driven fetch + eviction
  // -------------------------------------------------------------------------

  test("initial load fetches ONE page, not the whole timeline", async () => {
    // Larger page size than PREFETCH_MARGIN so initial load does not
    // immediately fire a prefetch (the trailing edge is far ahead of
    // absoluteIndex=0).
    REPLAY_TUNABLES.pageSize = 1000;
    REPLAY_TUNABLES.prefetchMargin = 128;
    const totalPages = 5;
    const pageSize = REPLAY_TUNABLES.pageSize;
    const pages: ReplayTimeline[] = Array.from({ length: totalPages }, (_, i) => {
      const isLast = i === totalPages - 1;
      const cursorForNext = isLast ? null : `cursor-${i + 1}`;
      return {
        run: runSummary({ total_events: totalPages * pageSize }),
        events: Array.from({ length: pageSize }, (_, j) => event(i * pageSize + j + 1)),
        next_cursor: cursorForNext,
        has_more: !isLast,
        bookmarks: i === 0 ? BASE_BOOKMARKS : [],
        bookmarks_truncated: false,
        warnings: [],
      };
    });
    (global.fetch as ReturnType<typeof vi.fn>).mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input.toString();
      record(url, init);
      if (url.includes("/replay/timeline")) {
        const match = url.match(/cursor=cursor-(\d+)/);
        const pageIndex = match ? Number(match[1]) : 0;
        return jsonResponse(pages[pageIndex] ?? pages[pages.length - 1]);
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

    const timelineFetches = calls.filter((c) => c.url.includes("/replay/timeline"));
    expect(timelineFetches).toHaveLength(1);
    // Slider max is based on run.total_events, not fetched-so-far.
    const slider = screen.getByRole("slider", { name: "Event cursor" }) as HTMLInputElement;
    expect(slider.max).toBe(String(totalPages * pageSize - 1));
  });

  test("scrubbing near end of window triggers a next-page prefetch", async () => {
    const totalPages = 3;
    const pageSize = 200;
    const pages: ReplayTimeline[] = Array.from({ length: totalPages }, (_, i) => {
      const isLast = i === totalPages - 1;
      return {
        run: runSummary({ total_events: totalPages * pageSize }),
        events: Array.from({ length: pageSize }, (_, j) => event(i * pageSize + j + 1)),
        next_cursor: isLast ? null : `cursor-${i + 1}`,
        has_more: !isLast,
        bookmarks: i === 0 ? BASE_BOOKMARKS : [],
        bookmarks_truncated: false,
        warnings: [],
      };
    });
    (global.fetch as ReturnType<typeof vi.fn>).mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input.toString();
      record(url, init);
      if (url.includes("/replay/timeline")) {
        const match = url.match(/cursor=cursor-(\d+)/);
        const pageIndex = match ? Number(match[1]) : 0;
        return jsonResponse(pages[pageIndex] ?? pages[pages.length - 1]);
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
    expect(calls.filter((c) => c.url.includes("/replay/timeline"))).toHaveLength(1);

    // Scrub to near end of first page → prefetch page 2.
    const slider = screen.getByRole("slider", { name: "Event cursor" });
    fireEvent.change(slider, { target: { value: "150" } });
    await flushAsync();
    const afterScrub = calls.filter((c) => c.url.includes("/replay/timeline"));
    expect(afterScrub.length).toBeGreaterThanOrEqual(2);
    // Second fetch must forward the opaque cursor from page 1.
    expect(afterScrub[1].url).toContain("cursor=cursor-1");
  });

  function makePages(pageSize: number, totalPages: number): ReplayTimeline[] {
    return Array.from({ length: totalPages }, (_, i) => {
      const isLast = i === totalPages - 1;
      return {
        run: runSummary({ total_events: totalPages * pageSize }),
        events: Array.from({ length: pageSize }, (_, j) => event(i * pageSize + j + 1)),
        next_cursor: isLast ? null : `cursor-${i + 1}`,
        has_more: !isLast,
        bookmarks: i === 0 ? BASE_BOOKMARKS : [],
        bookmarks_truncated: false,
        warnings: [],
      };
    });
  }

  function mockPagedFetch(pages: ReplayTimeline[]) {
    (global.fetch as ReturnType<typeof vi.fn>).mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input.toString();
      record(url, init);
      if (url.includes("/replay/timeline")) {
        const match = url.match(/cursor=cursor-(\d+)/);
        const pageIndex = match ? Number(match[1]) : 0;
        return jsonResponse(pages[pageIndex] ?? pages[pages.length - 1]);
      }
      if (url.endsWith("/replay/runs")) {
        return jsonResponse({ ticket: "WIKI-174", runs: [runSummary()], runs_truncated: false });
      }
      if (url.includes("/replay/events/")) {
        return jsonResponse({ run_id: RUN_ID, seq: 1, raw: {} });
      }
      return new Response("not found", { status: 404 });
    });
  }

  test("windowed eviction surfaces a sliding-window status hint", async () => {
    vi.useRealTimers();
    REPLAY_TUNABLES.windowSize = 4;
    REPLAY_TUNABLES.prefetchMargin = 1;
    REPLAY_TUNABLES.pageSize = 2;
    const pages = makePages(2, 6);
    mockPagedFetch(pages);

    render(<ReplayScrubberPanel ticket="WIKI-174" />);
    await waitFor(() => {
      expect(screen.getByRole("slider", { name: "Event cursor" })).toBeTruthy();
    });

    const slider = screen.getByRole("slider", { name: "Event cursor" });
    // Walk forward, waiting between each scrub for the prefetch chain to
    // settle. We check the fetch count as the settle signal because the
    // frame-info text depends on which events are currently loaded.
    for (let target = 1; target <= 10; target += 1) {
      const before = calls.filter((c) => c.url.includes("/replay/timeline")).length;
      fireEvent.change(slider, { target: { value: String(target) } });
      // Give the prefetch cycle a moment; the effect fires on absoluteIndex
      // changes and again on timeline updates.
      await new Promise((r) => setTimeout(r, 15));
      await waitFor(
        () => {
          const now = calls.filter((c) => c.url.includes("/replay/timeline")).length;
          // Either a new fetch happened OR the panel decided no fetch is
          // needed at this cursor position — either way, the state is
          // settled for this scrub step.
          expect(now >= before).toBe(true);
        },
        { timeout: 250 },
      );
    }
    await waitFor(() => {
      expect(screen.getByText(/sliding window/)).toBeTruthy();
    });
    const timelineFetches = calls.filter((c) => c.url.includes("/replay/timeline"));
    expect(timelineFetches.length).toBeGreaterThan(3);
  });

  test("backward scrub past the evicted edge triggers a rewind fetch", async () => {
    vi.useRealTimers();
    REPLAY_TUNABLES.windowSize = 4;
    REPLAY_TUNABLES.prefetchMargin = 1;
    REPLAY_TUNABLES.pageSize = 2;
    const pages = makePages(2, 6);
    mockPagedFetch(pages);

    render(<ReplayScrubberPanel ticket="WIKI-174" />);
    for (let i = 0; i < 8; i++) {
      await act(async () => {
        await new Promise((r) => setTimeout(r, 0));
      });
    }

    const slider = screen.getByRole("slider", { name: "Event cursor" });
    for (let target = 1; target <= 10; target += 1) {
      fireEvent.change(slider, { target: { value: String(target) } });
      for (let i = 0; i < 6; i++) {
        await act(async () => {
          await new Promise((r) => setTimeout(r, 0));
        });
      }
    }
    expect(screen.getByText(/sliding window/)).toBeTruthy();
    const preRewind = calls.filter((c) => c.url.includes("/replay/timeline")).length;

    // Scrub back to seq 1 which has evicted — panel must reset & re-page.
    fireEvent.change(slider, { target: { value: "0" } });
    for (let i = 0; i < 8; i++) {
      await act(async () => {
        await new Promise((r) => setTimeout(r, 0));
      });
    }
    const rewindFetches = calls
      .filter((c) => c.url.includes("/replay/timeline"))
      .slice(preRewind);
    expect(rewindFetches.length).toBeGreaterThanOrEqual(1);
    // The rewind starts from the beginning — first fetch has NO cursor.
    expect(rewindFetches[0].url).not.toContain("cursor=");
  });

  // -------------------------------------------------------------------------
  // Round-5 review items 3+4: stuck loading + empty-page warning render
  // -------------------------------------------------------------------------

  test("empty final page's warnings render visibly", async () => {
    // Round-5 review item 4: the round-3/4 test only checked that
    // the state carried the warning; this asserts the DOM actually
    // renders it so a regression in ``ReplayLoadStatus`` breaks the
    // test.
    const firstPage: ReplayTimeline = {
      run: runSummary({ total_events: 2 }),
      events: [event(1), event(2)],
      next_cursor: "Y3Vyc29yLTE=",
      has_more: true,
      bookmarks: [],
      bookmarks_truncated: false,
      warnings: ["scan budget reached — continuing via cursor"],
    };
    const emptyFinal: ReplayTimeline = {
      run: runSummary({ total_events: 2 }),
      events: [],
      next_cursor: null,
      has_more: false,
      bookmarks: [],
      bookmarks_truncated: false,
      warnings: ["dropped 1 oversized event line(s)"],
    };
    (global.fetch as ReturnType<typeof vi.fn>).mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input.toString();
      record(url, init);
      if (url.includes("/replay/timeline")) {
        return jsonResponse(url.includes("cursor=") ? emptyFinal : firstPage);
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
    await flushAsync(5);
    // Both warnings must be visible in the DOM — the round-3/4 test
    // stopped at state-level assertions.
    expect(screen.getByText(/scan budget reached/)).toBeTruthy();
    expect(screen.getByText(/dropped 1 oversized event line/)).toBeTruthy();
  });

  test("stale prefetch clears its own loading indicator via request identity", async () => {
    // Round-5 review item 3: a superseded prefetch must not leave
    // ``pageLoading`` stuck true. We resolve two overlapping fetches
    // and confirm the "loading next page…" indicator disappears once
    // both finish.
    vi.useRealTimers();
    REPLAY_TUNABLES.pageSize = 2;
    REPLAY_TUNABLES.prefetchMargin = 1;
    REPLAY_TUNABLES.windowSize = 100;
    const pages = makePages(2, 3);
    mockPagedFetch(pages);
    render(<ReplayScrubberPanel ticket="WIKI-174" />);
    await waitFor(() =>
      expect(screen.getByRole("slider", { name: "Event cursor" })).toBeTruthy(),
    );
    // Scrub past the trailing edge to trigger a prefetch, then scrub back
    // immediately to abort it. If ``finally`` guards on identity, the
    // aborted request's stale ``pageLoading=true`` still clears.
    const slider = screen.getByRole("slider", { name: "Event cursor" });
    fireEvent.change(slider, { target: { value: "1" } });
    fireEvent.change(slider, { target: { value: "0" } });
    await new Promise((r) => setTimeout(r, 50));
    await waitFor(() => {
      // The "loading next page…" phrase must not persist once the
      // prefetch chain settles.
      expect(screen.queryByText(/loading next page/)).toBeNull();
    });
  });
});
