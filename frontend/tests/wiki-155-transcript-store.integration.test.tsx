// @vitest-environment jsdom
import { useMemo } from "react";
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { AgentSessionData } from "../src/api";

const { getAgentSession } = vi.hoisted(() => ({
  getAgentSession: vi.fn<(...args: unknown[]) => Promise<AgentSessionData>>(),
}));

vi.mock("../src/api", async () => {
  const actual = await vi.importActual<typeof import("../src/api")>("../src/api");
  return {
    ...actual,
    getAgentSession,
  };
});

import {
  retryTranscript,
  useTranscriptSession,
  type TranscriptSnapshot,
} from "../src/transcript-store";

function baseSession(overrides: Partial<AgentSessionData> = {}): AgentSessionData {
  return {
    version: 2,
    format: "claude",
    path: "/tmp/session.jsonl",
    tokens: null,
    model: null,
    desired_model: null,
    kind: null,
    provider: null,
    tasks: [],
    pr: null,
    base: 0,
    cursor: 1,
    tail_from: 0,
    events: [
      {
        id: 1,
        ts: "2026-01-01T00:00:00Z",
        kind: "assistant" as const,
        text: "hello",
      },
    ] as unknown as AgentSessionData["events"],
    patches: [],
    has_older: false,
    subagents: [],
    queue: [],
    working: false,
    ...overrides,
  };
}

let latest: TranscriptSnapshot | null = null;

function Probe({ ticket }: { ticket: string }) {
  // Stable target identity across renders — otherwise useTranscriptSession's
  // effect resubscribes on every emit, re-triggering fetchEntry in a loop.
  const target = useMemo(() => ({ ticket }), [ticket]);
  // Disable polling — each test controls fetches explicitly via mock ordering
  // + retryTranscript(). Polling would consume unrelated mock returns.
  const snapshot = useTranscriptSession(target, false);
  latest = snapshot;
  return null;
}

beforeEach(() => {
  latest = null;
  getAgentSession.mockReset();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

test("initial-load failure sets `error`; last-good session absent so `refreshError` stays null", async () => {
  getAgentSession.mockRejectedValueOnce(new Error("cold boom"));

  render(<Probe ticket="WIKI-INITIAL-FAIL" />);
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });

  expect(latest?.error).toBe("cold boom");
  expect(latest?.refreshError).toBeNull();
  expect(latest?.session).toBeNull();
  expect(latest?.loading).toBe(false);
});

test("refresh failure after a successful load populates `refreshError` but keeps the session", async () => {
  getAgentSession.mockResolvedValueOnce(baseSession());
  getAgentSession.mockRejectedValueOnce(new Error("network hiccup"));

  render(<Probe ticket="WIKI-STALE" />);
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(latest?.session).not.toBeNull();
  expect(latest?.error).toBeNull();

  await act(async () => {
    await retryTranscript({ ticket: "WIKI-STALE" });
  });

  // Session survived — never blanked.
  expect(latest?.session).not.toBeNull();
  expect(latest?.session?.events.length).toBeGreaterThan(0);
  expect(latest?.refreshError).toBe("network hiccup");
  // Initial-load `error` stays null: the session existed, so this was a
  // refresh, not a cold-start failure.
  expect(latest?.error).toBeNull();
});

test("successful retry after failure clears both `error` and `refreshError`", async () => {
  getAgentSession.mockResolvedValueOnce(baseSession());
  getAgentSession.mockRejectedValueOnce(new Error("hiccup"));
  getAgentSession.mockResolvedValueOnce(baseSession({ cursor: 2 }));

  render(<Probe ticket="WIKI-RECOVER" />);
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
  await act(async () => {
    await retryTranscript({ ticket: "WIKI-RECOVER" });
  });
  expect(latest?.refreshError).toBe("hiccup");

  await act(async () => {
    await retryTranscript({ ticket: "WIKI-RECOVER" });
  });
  expect(latest?.refreshError).toBeNull();
  expect(latest?.error).toBeNull();
  expect(latest?.session?.cursor).toBe(2);
});

test("retryTranscript on a cold-error snapshot resets and re-fetches, resolving to success", async () => {
  getAgentSession.mockRejectedValueOnce(new Error("cold"));
  getAgentSession.mockResolvedValueOnce(baseSession());

  render(<Probe ticket="WIKI-COLD-RECOVER" />);
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(latest?.error).toBe("cold");

  await act(async () => {
    await retryTranscript({ ticket: "WIKI-COLD-RECOVER" });
  });
  expect(latest?.error).toBeNull();
  expect(latest?.refreshError).toBeNull();
  expect(latest?.session).not.toBeNull();
});
