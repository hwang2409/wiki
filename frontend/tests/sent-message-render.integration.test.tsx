// @vitest-environment jsdom
// Regression contract: a sent composer message must stay rendered through the
// ack poll, the transcript echo + agent response poll, and the idle flip.
// Before the display-space identity diff fix, the incremental row cache could
// drop the message row until a pane remount rebuilt it.
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, expect, test, vi } from "vitest";

import type { AgentSessionData, SessionEvent } from "../src/api";

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

import { SessionTab } from "../src/session";
import {
  addPendingUserMessage,
  retryTranscript,
  updatePendingUserMessage,
} from "../src/transcript-store";

class NoopObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

beforeAll(() => {
  (globalThis as { IntersectionObserver?: unknown }).IntersectionObserver ??= NoopObserver;
  (globalThis as { ResizeObserver?: unknown }).ResizeObserver ??= NoopObserver;
});

beforeEach(() => {
  getAgentSession.mockReset();
});

afterEach(() => {
  cleanup();
});

const MESSAGE = "Hi, how do I test, thanks";
const PENDING_ID = "11111111-2222-4333-8444-555555555555";
const TS0 = Date.parse("2026-08-27T12:00:00Z");

function ts(offsetSeconds: number): string {
  return new Date(TS0 + offsetSeconds * 1000).toISOString();
}

function assistantEvent(id: number, offsetSeconds: number, text: string): SessionEvent {
  return { id, kind: "assistant", ts: ts(offsetSeconds), text, disposition: "rendered" } as SessionEvent;
}

function payload(overrides: Partial<AgentSessionData>): AgentSessionData {
  return {
    version: 2,
    format: "claude",
    path: "/tmp/orch.jsonl",
    tokens: null,
    base: 0,
    cursor: 1,
    tail_from: 0,
    events: [],
    patches: [],
    has_older: false,
    subagents: [],
    queue: [],
    working: false,
    composer_messages: [],
    ...overrides,
  } as AgentSessionData;
}

async function poll(ticket: string, result: AgentSessionData) {
  getAgentSession.mockResolvedValueOnce(result);
  await act(async () => {
    await retryTranscript({ mode: "live", ticket });
  });
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

test("sent message stays rendered through ack + agent-response polls", async () => {
  const ticket = "WIKI-REPRO-1";
  const initialEvents = [
    assistantEvent(0, -600, "earlier answer"),
    assistantEvent(1, -300, "another earlier answer"),
  ];
  getAgentSession.mockResolvedValueOnce(payload({ cursor: 1, events: initialEvents, tail_from: 0 }));

  const view = render(<SessionTab showComposer={false} ticket={ticket} />);
  await flush();
  expect(view.container.textContent).toContain("earlier answer");

  // Henry hits send.
  await act(async () => {
    addPendingUserMessage(ticket, {
      id: PENDING_ID,
      requestId: "req-1",
      text: MESSAGE,
      mode: "now",
    });
  });
  expect(view.container.textContent).toContain(MESSAGE);

  await act(async () => {
    updatePendingUserMessage(ticket, PENDING_ID, { status: "sent" });
  });
  expect(view.container.textContent).toContain(MESSAGE);

  // Poll A: backend ack via composer_messages, no transcript events yet.
  await poll(ticket, payload({
    cursor: 2,
    tail_from: 2,
    events: [],
    composer_messages: [
      { pending_id: PENDING_ID, text: MESSAGE, sent_at: ts(0), echoed_at: null, seq: 1 },
    ],
    working: true,
  }));
  expect(view.container.textContent).toContain(MESSAGE);

  // Poll B: transcript echo + agent response arrive.
  await poll(ticket, payload({
    cursor: 3,
    tail_from: 2,
    events: [
      { id: 2, kind: "user", ts: ts(1), text: MESSAGE, disposition: "rendered", pending_id: PENDING_ID } as SessionEvent,
      { id: 3, kind: "thinking", ts: ts(3), text: "reasoning about testing", disposition: "rendered" } as SessionEvent,
      assistantEvent(4, 5, "two layers, same as last time"),
    ],
    composer_messages: [
      { pending_id: PENDING_ID, text: MESSAGE, sent_at: ts(0), echoed_at: ts(2), seq: 1 },
    ],
    working: true,
  }));
  expect(view.container.textContent).toContain("two layers, same as last time");
  expect(view.container.textContent).toContain(MESSAGE);

  // Poll C: idle flip.
  await poll(ticket, payload({
    cursor: 4,
    tail_from: 5,
    events: [],
    composer_messages: [
      { pending_id: PENDING_ID, text: MESSAGE, sent_at: ts(0), echoed_at: ts(2), seq: 1 },
    ],
    working: false,
  }));
  expect(view.container.textContent).toContain(MESSAGE);
});
