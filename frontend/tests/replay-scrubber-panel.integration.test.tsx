// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import type {
  ReplayRawEvent,
  ReplayRunSummary,
  ReplayTimeline,
  ReplayTimelineEvent,
} from "../src/api";
import { ReplayScrubberPanel } from "../src/replay-scrubber-panel";

const RUN_ID = "11111111-2222-3333-4444-555555555555";
const OLD_RUN = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";

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
    bookmarks: [
      { seq: 2, kind: "steer", ts: "2026-07-30T00:00:02Z", summary: "please do the thing", event_kind: "claude_user" },
      { seq: 3, kind: "verdict", ts: "2026-07-30T00:00:03Z", summary: "MERGE-READY", event_kind: "claude_assistant" },
      { seq: 4, kind: "error", ts: "2026-07-30T00:00:04Z", summary: "exit", event_kind: "provider_process_exit" },
    ],
  };
}

const rawFetches: number[] = [];
const rawAborts: number[] = [];

vi.mock("../src/api", () => ({
  getAgentReplayRuns: vi.fn(async () => ({
    ticket: "WIKI-174",
    runs: [runSummary(), runSummary({ run_id: OLD_RUN })],
  })),
  getReplayTimeline: vi.fn(async () => timeline()),
  getReplayRawEvent: vi.fn(
    async (_runId: string, seq: number, signal?: AbortSignal): Promise<ReplayRawEvent> => {
      rawFetches.push(seq);
      signal?.addEventListener("abort", () => rawAborts.push(seq), { once: true });
      return {
        run_id: RUN_ID,
        seq,
        raw: { seq, direction: "stdout", payload: { hint: `raw-${seq}` } },
      };
    }
  ),
}));

describe("replay scrubber panel", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    rawFetches.length = 0;
    rawAborts.length = 0;
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  async function flushAsync() {
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
  }

  test("advances one event per play step at real-time speed", async () => {
    render(<ReplayScrubberPanel ticket="WIKI-174" />);
    await flushAsync();
    expect(screen.getByText("1 / 4")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Play replay" }));
    await act(async () => {
      // Real gap 1 → 2 = 1 second; at speed 1x = 1000ms.
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(screen.getByText("2 / 4")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Pause replay" })).toBeTruthy();
  });

  test("scrubbing pauses playback and cancels an in-flight raw fetch", async () => {
    render(<ReplayScrubberPanel ticket="WIKI-174" />);
    await flushAsync();
    // Let the initial cursor's debounced raw fetch actually fire so we have
    // an in-flight request whose AbortController the next cursor change must
    // cancel.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(150);
    });
    expect(rawFetches).toEqual([1]);

    const slider = screen.getByRole("slider", { name: "Event cursor" });
    fireEvent.change(slider, { target: { value: "3" } });
    await flushAsync();
    // Play button was paused when scrubbed.
    expect(screen.getByRole("button", { name: "Play replay" })).toBeTruthy();
    expect(screen.getByText("4 / 4")).toBeTruthy();
    expect(rawAborts).toEqual([1]);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(150);
    });
    expect(rawFetches.at(-1)).toBe(4);
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
});
