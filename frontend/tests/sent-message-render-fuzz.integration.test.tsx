// @vitest-environment jsdom
// Seeded fuzz harness for the sent-message render contract. Simulates a
// backend session (event log + composer_messages + queue) and drives
// SessionTab through random interleavings of send, queue, ack, echo,
// response, and state-change polls, including suffix re-sends. Sent messages
// must stay visible at every stable point and after the final drain.
import { act, cleanup, render } from "@testing-library/react";
import { beforeAll, beforeEach, expect, test, vi } from "vitest";

import type { AgentSessionData, ComposerMessage, QueuedMessage, SessionEvent } from "../src/api";

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

type RowCallRecord = {
  offset: number;
  eventTexts: string[];
  rowTexts: string[];
  changedFrom: number;
  cacheReset: boolean;
};

const rowCallLog: RowCallRecord[] = [];

vi.mock("../src/session-layout", async () => {
  const actual = await vi.importActual<typeof import("../src/session-layout")>("../src/session-layout");
  return {
    ...actual,
    eventRowsIncremental: (
      events: SessionEvent[],
      offset: number,
      previous: Parameters<typeof actual.eventRowsIncremental>[2],
    ) => {
      const result = actual.eventRowsIncremental(events, offset, previous);
      rowCallLog.push({
        offset,
        eventTexts: events.map((event) => `${event.id}:${event.kind}:${event.text.slice(0, 30)}`),
        rowTexts: result.rows.map((row) => `${row.key}=${row.event.id}:${row.event.kind}:${row.event.text.slice(0, 30)}`),
        changedFrom: result.changedFrom,
        cacheReset: previous === null,
      });
      if (rowCallLog.length > 12) rowCallLog.shift();
      return result;
    },
  };
});

import { SessionTab } from "../src/session";
import {
  addPendingUserMessage,
  replaceTranscriptQueue,
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

const TS0 = Date.parse("2026-08-27T12:00:00Z");

function mulberry32(seed: number) {
  let a = seed >>> 0;
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

type Rng = () => number;

function pick<T>(rng: Rng, items: T[]): T {
  return items[Math.floor(rng() * items.length)];
}

type SentMessage = {
  pendingId: string;
  text: string;
  sentAt: string;
  seq: number;
  queued: boolean;
  acked: boolean;
  echoed: boolean;
};

class FakeServer {
  events: SessionEvent[] = [];
  composerMessages: ComposerMessage[] = [];
  queue: QueuedMessage[] = [];
  working = false;
  cursor = 1;
  clock = 0;

  ts(): string {
    this.clock += 1;
    return new Date(TS0 + this.clock * 1000).toISOString();
  }

  append(event: Omit<SessionEvent, "id">): void {
    this.events.push({ ...event, id: this.events.length } as SessionEvent);
  }

  pollResult(tailFrom: number): AgentSessionData {
    this.cursor += 1;
    return {
      version: 2,
      format: "claude",
      path: "/tmp/fuzz.jsonl",
      tokens: null,
      base: 0,
      cursor: this.cursor,
      tail_from: tailFrom,
      events: this.events.slice(tailFrom),
      patches: [],
      has_older: false,
      subagents: [],
      queue: this.queue.map((message) => ({ ...message })),
      working: this.working,
      composer_messages: this.composerMessages.map((message) => ({ ...message })),
    } as AgentSessionData;
  }
}

async function runScenario(seed: number): Promise<void> {
  const rng = mulberry32(seed);
  const ticket = `WIKI-FUZZ-${seed}`;
  const server = new FakeServer();
  const log: string[] = [];
  let delivered = 0;
  let nextSend = 1;
  const sends: SentMessage[] = [];

  const priorCount = 1 + Math.floor(rng() * 4);
  for (let index = 0; index < priorCount; index += 1) {
    const kind = pick(rng, ["assistant", "thinking", "user"] as const);
    server.append({
      kind,
      ts: rng() < 0.85 ? server.ts() : null,
      text: `${kind} prior ${index}`,
      disposition: "rendered",
    } as Omit<SessionEvent, "id">);
  }

  getAgentSession.mockResolvedValueOnce(server.pollResult(0));
  delivered = server.events.length;
  const view = render(<SessionTab showComposer ticket={ticket} />);
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });

  const poll = async (tailFrom?: number) => {
    const from = tailFrom ?? delivered;
    const result = server.pollResult(from);
    delivered = server.events.length;
    getAgentSession.mockResolvedValueOnce(result);
    await act(async () => {
      await retryTranscript({ mode: "live", ticket });
    });
  };

  const actions: Array<{ name: string; enabled: () => boolean; run: () => Promise<void> }> = [
    {
      name: "send-direct",
      enabled: () => sends.length < 3,
      run: async () => {
        const seq = nextSend++;
        const message: SentMessage = {
          pendingId: `${seed.toString(16).padStart(8, "0")}-0000-4000-8000-${seq.toString().padStart(12, "0")}`,
          text: `message number ${seq} from henry`,
          sentAt: server.ts(),
          seq,
          queued: false,
          acked: false,
          echoed: false,
        };
        sends.push(message);
        await act(async () => {
          addPendingUserMessage(ticket, {
            id: message.pendingId,
            requestId: message.pendingId,
            text: message.text,
            mode: "now",
          });
        });
        // Backend accepted immediately (idle case).
        server.composerMessages.push({
          pending_id: message.pendingId,
          text: message.text,
          sent_at: message.sentAt,
          echoed_at: null,
          seq: message.seq,
        });
        message.acked = true;
        await act(async () => {
          updatePendingUserMessage(ticket, message.pendingId, { status: "sent" });
        });
        if (rng() < 0.5) await poll();
      },
    },
    {
      name: "send-queued",
      enabled: () => sends.length < 3,
      run: async () => {
        const seq = nextSend++;
        const message: SentMessage = {
          pendingId: `${seed.toString(16).padStart(8, "0")}-0000-4000-8000-${seq.toString().padStart(12, "0")}`,
          text: `message number ${seq} from henry`,
          sentAt: server.ts(),
          seq,
          queued: true,
          acked: false,
          echoed: false,
        };
        sends.push(message);
        await act(async () => {
          addPendingUserMessage(ticket, {
            id: message.pendingId,
            requestId: message.pendingId,
            text: message.text,
            mode: "now",
          });
        });
        // Backend queued the send behind the running turn.
        server.queue.push({
          text: message.text,
          queued_at: message.sentAt,
          pending_id: message.pendingId,
          source: "auto",
        } as QueuedMessage);
        await act(async () => {
          replaceTranscriptQueue(ticket, server.queue.map((entry) => ({ ...entry })), {
            pendingId: message.pendingId,
            source: "auto",
            text: message.text,
            position: server.queue.length,
          });
        });
      },
    },
    {
      name: "deliver-queued",
      enabled: () => server.queue.length > 0,
      run: async () => {
        const entry = server.queue.shift()!;
        const message = sends.find((candidate) => candidate.pendingId === entry.pending_id)!;
        message.sentAt = server.ts();
        server.composerMessages.push({
          pending_id: message.pendingId,
          text: message.text,
          sent_at: message.sentAt,
          echoed_at: null,
          seq: message.seq,
        });
        message.acked = true;
        if (rng() < 0.8) {
          const withPendingId = rng() < 0.7;
          server.append({
            kind: "user",
            ts: rng() < 0.85 ? server.ts() : null,
            text: message.text,
            disposition: "rendered",
            ...(withPendingId ? { pending_id: message.pendingId } : {}),
          } as Omit<SessionEvent, "id">);
          const composerEntry = server.composerMessages.find(
            (candidate) => candidate.pending_id === message.pendingId,
          );
          if (composerEntry) composerEntry.echoed_at = server.ts();
          message.echoed = true;
        }
      },
    },
    {
      name: "echo",
      enabled: () => sends.some((message) => message.acked && !message.echoed),
      run: async () => {
        const message = sends.find((candidate) => candidate.acked && !candidate.echoed)!;
        const withPendingId = rng() < 0.7;
        server.append({
          kind: "user",
          ts: rng() < 0.85 ? server.ts() : null,
          text: message.text,
          disposition: "rendered",
          ...(withPendingId ? { pending_id: message.pendingId } : {}),
        } as Omit<SessionEvent, "id">);
        const composerEntry = server.composerMessages.find(
          (candidate) => candidate.pending_id === message.pendingId,
        );
        if (composerEntry) composerEntry.echoed_at = server.ts();
        message.echoed = true;
      },
    },
    {
      name: "respond",
      enabled: () => sends.some((message) => message.acked),
      run: async () => {
        server.working = true;
        server.append({
          kind: "thinking",
          ts: server.ts(),
          text: "reasoning about testing",
          disposition: "rendered",
        } as Omit<SessionEvent, "id">);
        server.append({
          kind: "assistant",
          ts: server.ts(),
          text: "two layers, same as last time",
          disposition: "rendered",
        } as Omit<SessionEvent, "id">);
      },
    },
    {
      name: "stray-event",
      enabled: () => true,
      run: async () => {
        server.append({
          kind: pick(rng, ["assistant", "thinking"] as const),
          ts: rng() < 0.85 ? server.ts() : null,
          text: "stray output",
          disposition: "rendered",
        } as Omit<SessionEvent, "id">);
      },
    },
    {
      name: "flip-working",
      enabled: () => true,
      run: async () => {
        server.working = !server.working;
      },
    },
    {
      name: "poll",
      enabled: () => true,
      run: () => poll(),
    },
    {
      name: "poll-resend-suffix",
      enabled: () => delivered > 0,
      run: async () => {
        const back = 1 + Math.floor(rng() * Math.min(4, delivered));
        await poll(delivered - back);
      },
    },
  ];

  const steps = 16 + Math.floor(rng() * 12);
  for (let step = 0; step < steps; step += 1) {
    const enabled = actions.filter((action) => action.enabled());
    const action = pick(rng, enabled);
    log.push(action.name);
    await action.run();
    // Only assert at stable points: a plain poll with the client fully caught
    // up. Mid-delivery windows (chip replaced before the echo poll) are
    // transient by design and self-heal on the next poll.
    if (action.name !== "poll" || delivered !== server.events.length) continue;
    const text = view.container.textContent ?? "";
    for (const message of sends) {
      if (!message.echoed && !message.acked) continue;
      if (server.queue.some((entry) => entry.pending_id === message.pendingId)) continue;
      if (!text.includes(message.text)) {
        const dump = rowCallLog
          .map((call, index) =>
            `#${index} changedFrom=${call.changedFrom} reset=${call.cacheReset}\n  events: ${call.eventTexts.join(" | ")}\n  rows:   ${call.rowTexts.join(" | ")}`)
          .join("\n");
        throw new Error(
          `seed ${seed}: "${message.text}" missing after step ${step} (${action.name}); trace: ${log.join(" -> ")}\nrow calls:\n${dump}`,
        );
      }
    }
  }

  // Drain: deliver everything, then settle with two polls.
  while (server.queue.length > 0) {
    const deliver = actions.find((action) => action.name === "deliver-queued")!;
    await deliver.run();
  }
  await poll();
  await poll();
  const text = view.container.textContent ?? "";
  const missing = sends.filter((message) => !text.includes(message.text));
  if (missing.length > 0) {
    // Remount probe: same store data, fresh component caches. If the message
    // appears on remount, the drop lives in component-level incremental
    // caches, not in the store.
    cleanup();
    const remount = render(<SessionTab showComposer ticket={ticket} />);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    const remountText = remount.container.textContent ?? "";
    const remountShows = missing.map(
      (message) => `${message.text}: remount=${remountText.includes(message.text) ? "SHOWS" : "still missing"}`,
    );
    throw new Error(
      `seed ${seed} final missing [${remountShows.join("; ")}]; trace: ${log.join(" -> ")}`,
    );
  }
  cleanup();
}

test("fuzz: sent messages never disappear across poll interleavings", async () => {
  const failures: string[] = [];
  for (let seed = 1; seed <= 200; seed += 1) {
    try {
      await runScenario(seed);
    } catch (error) {
      failures.push(error instanceof Error ? error.message : String(error));
      cleanup();
      if (failures.length >= 5) break;
    }
  }
  expect(failures).toEqual([]);
}, 240_000);
