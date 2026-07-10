import { useEffect, useId, useState } from "react";
import {
  getAgentSession,
  getSubagentSession,
  type AgentSessionData,
  type ProviderEventInspector,
  type SessionDispositionCounts,
  type SessionMeta,
  type QueuedMessage,
  type SessionEvent,
  type SessionPatch,
  type SessionPr,
  type SessionTask,
  type SubagentInfo,
} from "./api";

const POLL_MS = 2500;

export type TranscriptTarget = {
  ticket: string;
  subagent?: string;
};

export type TranscriptSession = {
  format: AgentSessionData["format"];
  path: string;
  tokens: number | null;
  model: string | null;
  kind: string | null;
  provider: string | null;
  tasks: SessionTask[];
  pr: SessionPr | null;
  sessionMeta: SessionMeta;
  dispositions: SessionDispositionCounts;
  providerInspector: ProviderEventInspector | null;
  base: number;
  cursor: number;
  events: SessionEvent[];
  subagents: SubagentInfo[];
  queue: QueuedMessage[];
  working: boolean;
};

export type TranscriptSnapshot = {
  session: TranscriptSession | null;
  error: string | null;
  loading: boolean;
};

type Listener = () => void;

type Entry = {
  key: string;
  target: TranscriptTarget;
  snapshot: TranscriptSnapshot;
  listeners: Set<Listener>;
  pollers: Set<string>;
  inFlight: Promise<void> | null;
  dirty: boolean;
  lastLoadedAt: number;
};

const entries = new Map<string, Entry>();
let pollHandle: number | null = null;

function targetKey(target: TranscriptTarget): string {
  return `${target.ticket}::${target.subagent ?? ""}`;
}

function createEntry(target: TranscriptTarget): Entry {
  return {
    key: targetKey(target),
    target,
    snapshot: { session: null, error: null, loading: true },
    listeners: new Set(),
    pollers: new Set(),
    inFlight: null,
    dirty: false,
    lastLoadedAt: 0,
  };
}

function getEntry(target: TranscriptTarget): Entry {
  const key = targetKey(target);
  const existing = entries.get(key);
  if (existing) return existing;
  const created = createEntry(target);
  entries.set(key, created);
  return created;
}

function emit(entry: Entry) {
  entry.listeners.forEach((listener) => listener());
}

function setPollerState() {
  const needsPoller = [...entries.values()].some((entry) => entry.pollers.size > 0);
  if (needsPoller && pollHandle === null) {
    pollHandle = window.setInterval(() => {
      entries.forEach((entry) => {
        if (entry.pollers.size > 0) void fetchEntry(entry);
      });
    }, POLL_MS);
    return;
  }
  if (!needsPoller && pollHandle !== null) {
    window.clearInterval(pollHandle);
    pollHandle = null;
  }
}

function buildSession(result: AgentSessionData): TranscriptSession {
  return {
    format: result.format,
    path: result.path,
    tokens: result.tokens,
    model: result.model ?? null,
    kind: result.kind ?? null,
    provider: result.provider ?? null,
    tasks: result.tasks ?? [],
    pr: result.pr ?? null,
    sessionMeta: result.session_meta ?? {},
    dispositions: result.dispositions ?? { rendered: 0, summarized: 0, ignored: 0, unknown: 0 },
    providerInspector: result.provider_inspector ?? null,
    base: result.base,
    cursor: result.cursor,
    events: result.events,
    subagents: result.subagents ?? [],
    queue: result.queue ?? [],
    working: result.working ?? false,
  };
}

function applyPatches(events: SessionEvent[], patches: SessionPatch[]): SessionEvent[] {
  if (patches.length === 0) return events;
  const indexById = new Map<number, number>();
  events.forEach((event, index) => indexById.set(event.id, index));
  let next = events;
  let changed = false;
  for (const patch of patches) {
    const index = indexById.get(patch.id);
    if (index === undefined) continue;
    const event = next[index];
    if (!event?.tool) continue;
    if (event.tool.output === patch.output && event.tool.ok === patch.ok) continue;
    if (!changed) {
      next = next.slice();
      changed = true;
    }
    next[index] = {
      ...event,
      tool: {
        ...event.tool,
        output: patch.output,
        ok: patch.ok,
      },
    };
  }
  return next;
}

function mergeSession(current: TranscriptSession | null, result: AgentSessionData): TranscriptSession {
  if (
    !current ||
    current.path !== result.path ||
    result.cursor < current.cursor
  ) {
    return buildSession(result);
  }

  let base = current.base;
  let events = current.events;

  if (result.base > base) {
    const trim = result.base - base;
    if (trim >= events.length) {
      return buildSession(result);
    }
    events = events.slice(trim);
    base = result.base;
  }

  const clientEnd = base + events.length;
  if (result.tail_from < base || result.tail_from > clientEnd) {
    return buildSession(result);
  }

  events = events.slice(0, result.tail_from - base).concat(result.events);
  events = applyPatches(events, result.patches);

  return {
    ...current,
    format: result.format,
    path: result.path,
    tokens: result.tokens,
    model: result.model ?? current.model,
    kind: result.kind ?? current.kind,
    provider: result.provider ?? current.provider,
    tasks: result.tasks ?? current.tasks,
    pr: result.pr ?? current.pr,
    sessionMeta: result.session_meta ?? current.sessionMeta,
    dispositions: result.dispositions ?? current.dispositions,
    providerInspector: result.provider_inspector ?? current.providerInspector,
    base,
    cursor: result.cursor,
    events,
    subagents: result.subagents ?? current.subagents,
    queue: result.queue ?? current.queue,
    working: result.working ?? current.working,
  };
}

async function loadTarget(target: TranscriptTarget, cursor: number, path?: string): Promise<AgentSessionData> {
  return target.subagent
    ? getSubagentSession(target.ticket, target.subagent, cursor, path)
    : getAgentSession(target.ticket, cursor, path);
}

function fetchEntry(entry: Entry): Promise<void> {
  if (entry.inFlight) return entry.inFlight;
  const current = entry.snapshot;
  if (!current.session && !current.loading) {
    entry.snapshot = { ...current, loading: true, error: null };
    emit(entry);
  }
  entry.inFlight = (async () => {
    try {
      const result = await loadTarget(
        entry.target,
        entry.snapshot.session?.cursor ?? 0,
        entry.snapshot.session?.path
      );
      entry.snapshot = {
        session: mergeSession(entry.snapshot.session, result),
        error: null,
        loading: false,
      };
      entry.dirty = false;
      entry.lastLoadedAt = Date.now();
    } catch (error) {
      if (!entry.snapshot.session) {
        entry.snapshot = {
          session: null,
          error: error instanceof Error ? error.message : "No session transcript found.",
          loading: false,
        };
      } else {
        entry.snapshot = { ...entry.snapshot, loading: false };
      }
    } finally {
      entry.inFlight = null;
      emit(entry);
    }
  })();
  return entry.inFlight;
}

function shouldRefreshImmediately(entry: Entry): boolean {
  return (
    entry.dirty ||
    !entry.snapshot.session ||
    (Date.now() - entry.lastLoadedAt) >= POLL_MS
  );
}

function subscribeEntry(target: TranscriptTarget, listener: Listener) {
  const entry = getEntry(target);
  entry.listeners.add(listener);
  if (!entry.snapshot.session && !entry.inFlight) {
    void fetchEntry(entry);
  }
  return () => {
    entry.listeners.delete(listener);
  };
}

function getSnapshot(target: TranscriptTarget): TranscriptSnapshot {
  return getEntry(target).snapshot;
}

function setPolling(target: TranscriptTarget, subscriberId: string, polling: boolean) {
  const entry = getEntry(target);
  if (polling) entry.pollers.add(subscriberId);
  else entry.pollers.delete(subscriberId);
  setPollerState();
  if (polling && shouldRefreshImmediately(entry)) {
    void fetchEntry(entry);
  }
}

export function useTranscriptSession(target: TranscriptTarget, polling: boolean): TranscriptSnapshot {
  const subscriberId = useId();
  const [snapshot, setSnapshot] = useState(() => getSnapshot(target));

  useEffect(() => {
    setSnapshot(getSnapshot(target));
    return subscribeEntry(target, () => setSnapshot(getSnapshot(target)));
  }, [target]);

  useEffect(() => {
    setPolling(target, subscriberId, polling);
    return () => setPolling(target, subscriberId, false);
  }, [polling, subscriberId, target]);

  return snapshot;
}

export function invalidateTranscript(ticket: string, surface: string | null = null) {
  entries.forEach((entry) => {
    if (entry.target.ticket !== ticket) return;
    if (surface === "queue" && entry.target.subagent) return;
    entry.dirty = true;
    if (entry.pollers.size > 0) void fetchEntry(entry);
  });
}

export function replaceTranscriptQueue(ticket: string, messages: QueuedMessage[]) {
  entries.forEach((entry) => {
    if (entry.target.ticket !== ticket || entry.target.subagent || !entry.snapshot.session) return;
    entry.snapshot = {
      ...entry.snapshot,
      session: {
        ...entry.snapshot.session,
        queue: messages,
      },
    };
    emit(entry);
  });
}
