import { useEffect, useId, useState } from "react";
import {
  getAgentOlderSession,
  getAgentSession,
  getSubagentSession,
  type AgentSessionData,
  type ComposerMessage,
  type ProviderEventInspector,
  type SessionDispositionCounts,
  type SessionMeta,
  type QueuedMessage,
  type SessionEvent,
  type SessionPr,
  type SessionTask,
  type SubagentInfo,
} from "./api";
import { mergeQueueSources, mergeSession, prependOlderEvents } from "./transcript-merge";

const POLL_MS = 2500;

export type TranscriptTarget = {
  ticket: string;
  subagent?: string;
  // When set, selects a specific archived session for this ticket. Without
  // it the backend returns the newest archive — wrong for older history
  // rows. See getAgentSession + agents.tsx renderHistoryRow.
  archivedAt?: string;
};

export type TranscriptSession = {
  format: AgentSessionData["format"];
  path: string;
  tokens: number | null;
  model: string | null;
  desiredModel: string | null;
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
  eventsChangedFrom: number;
  hasOlder: boolean;
  composerMessages: ComposerMessage[];
  subagents: SubagentInfo[];
  queue: QueuedMessage[];
  working: boolean;
};

export type TranscriptSnapshot = {
  session: TranscriptSession | null;
  pendingUserMessages: PendingUserMessage[];
  error: string | null;
  loading: boolean;
};

export type ArtifactViewState = {
  expanded?: boolean;
  filter?: string;
  find?: string;
  findOpen?: boolean;
  foldedBlocks?: number[];
  panX?: number;
  panY?: number;
  showLineNumbers?: boolean;
  sortColumn?: string | null;
  sortDirection?: "asc" | "desc" | null;
  zoom?: number;
};

export type PanelState = {
  focusedTab: string | null;
  open: boolean;
  recentlyClosed: string[];
  tabs: string[];
  viewState: Record<string, ArtifactViewState>;
};

export type PendingUserMessage = {
  id: string;
  text: string;
  status: "sending" | "sent" | "failed";
  mode: "now" | "on-idle";
  firstSeenTs: number;
  eventIdFloor: number;
  error?: string;
};

export type QueueSourceAnnotation = {
  pendingId: string;
  source: "auto" | "explicit";
  text: string;
  position?: number;
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
const panelStates = new Map<string, PanelState>();
let pollHandle: number | null = null;

export function emptyPanelState(): PanelState {
  return {
    focusedTab: null,
    open: false,
    recentlyClosed: [],
    tabs: [],
    viewState: {},
  };
}

export function readPanelState(sessionKey: string): PanelState {
  return panelStates.get(sessionKey) ?? emptyPanelState();
}

export function writePanelState(sessionKey: string, state: PanelState) {
  panelStates.set(sessionKey, state);
}

type InlineArtifactState = { expanded?: boolean };
const inlineArtifactStates = new Map<string, InlineArtifactState>();
const inlineArtifactListeners = new Map<string, Set<Listener>>();
const EMPTY_INLINE_ARTIFACT_STATE: InlineArtifactState = Object.freeze({});

function inlineArtifactKey(sessionKey: string, artifactId: string): string {
  return `${sessionKey}::${artifactId}`;
}

export function readInlineArtifactState(sessionKey: string, artifactId: string): InlineArtifactState {
  return inlineArtifactStates.get(inlineArtifactKey(sessionKey, artifactId)) ?? EMPTY_INLINE_ARTIFACT_STATE;
}

export function writeInlineArtifactState(sessionKey: string, artifactId: string, state: InlineArtifactState) {
  const key = inlineArtifactKey(sessionKey, artifactId);
  inlineArtifactStates.set(key, state);
  inlineArtifactListeners.get(key)?.forEach((listener) => listener());
}

export function subscribeInlineArtifactState(sessionKey: string, artifactId: string, listener: Listener): () => void {
  const key = inlineArtifactKey(sessionKey, artifactId);
  const listeners = inlineArtifactListeners.get(key) ?? new Set<Listener>();
  listeners.add(listener);
  inlineArtifactListeners.set(key, listeners);
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) inlineArtifactListeners.delete(key);
  };
}

export function clearInlineArtifactStates(sessionKey: string) {
  const prefix = `${sessionKey}::`;
  for (const key of Array.from(inlineArtifactStates.keys())) {
    if (key.startsWith(prefix)) inlineArtifactStates.delete(key);
  }
  for (const key of Array.from(inlineArtifactListeners.keys())) {
    if (key.startsWith(prefix)) {
      inlineArtifactListeners.get(key)?.forEach((listener) => listener());
    }
  }
}

function targetKey(target: TranscriptTarget): string {
  return `${target.ticket}::${target.subagent ?? ""}::${target.archivedAt ?? ""}`;
}

function createEntry(target: TranscriptTarget): Entry {
  return {
    key: targetKey(target),
    target,
    snapshot: { session: null, pendingUserMessages: [], error: null, loading: true },
    listeners: new Set(),
    pollers: new Set(),
    inFlight: null,
    dirty: false,
    lastLoadedAt: 0,
  };
}

function normalizePendingText(text: string): string {
  return text
    .replace(/\[image:\s*\/tmp\/wiki-uploads\/([^\]\s]+)\s*\]/g, "[image:$1]")
    .replace(/\u27e6img:\/api\/uploads\/([^\u27e7]+)\u27e7/g, "[image:$1]")
    .trim();
}

export function composerTextMatches(eventText: string, composerText: string): boolean {
  const normalizedEvent = normalizePendingText(eventText);
  const normalizedComposer = normalizePendingText(composerText);
  if (normalizedEvent === normalizedComposer) return true;
  const suffix = normalizedEvent.slice(normalizedComposer.length).trimStart();
  const prefix = normalizedEvent.slice(0, -normalizedComposer.length).trimEnd();
  return (
    normalizedEvent.startsWith(normalizedComposer) && suffix.startsWith("<")
  ) || (
    normalizedEvent.endsWith(normalizedComposer) && prefix.endsWith(">")
  );
}

function pendingTextMatches(eventText: string, message: PendingUserMessage): boolean {
  return composerTextMatches(eventText, message.text);
}

function reconcilePendingUserMessages(
  pending: PendingUserMessage[],
  events: SessionEvent[],
  composerMessages: ComposerMessage[],
): PendingUserMessage[] {
  if (pending.length === 0) return pending;
  const acknowledged = new Set(composerMessages.map((message) => message.pending_id));
  const remaining = pending.filter((message) => !acknowledged.has(message.id));
  for (const event of events) {
    if (event.kind !== "user") continue;
    const eventTs = event.ts ? Date.parse(event.ts) : Number.NaN;
    // Without a provider timestamp, text alone cannot prove this event came
    // after an older optimistic row. Durable pending_id acknowledgements above
    // remain the canonical path for timestamp-less provider events.
    if (!Number.isFinite(eventTs)) continue;
    const match = remaining.findIndex((message) => {
      if (event.id <= message.eventIdFloor || !pendingTextMatches(event.text, message)) return false;
      return eventTs >= message.firstSeenTs - 2_000;
    });
    if (match >= 0) remaining.splice(match, 1);
  }
  // Text matching is only a fallback for providers without durable ids. One
  // event consumes one optimistic row, so identical sends remain FIFO-safe.
  return remaining.length === pending.length ? pending : remaining;
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

async function loadTarget(target: TranscriptTarget, cursor: number, path?: string): Promise<AgentSessionData> {
  return target.subagent
    ? getSubagentSession(target.ticket, target.subagent, cursor, path)
    : getAgentSession(target.ticket, cursor, path, target.archivedAt);
}

export async function loadOlderEvents(ticket: string, before: number, count = 500): Promise<void> {
  const entry = getEntry({ ticket });
  const result = await getAgentOlderSession(ticket, before, count);
  const current = entry.snapshot.session;
  if (!current || current.path !== result.path || current.base !== before) return;
  const merged = prependOlderEvents(current, result, before);
  if (!merged) {
    const reset = await getAgentSession(ticket, 0);
    const latest = entry.snapshot.session;
    if (!latest || latest.path !== current.path || latest.base !== before) return;
    entry.snapshot = {
      ...entry.snapshot,
      session: mergeSession(null, reset),
      error: null,
      loading: false,
    };
    emit(entry);
    return;
  }
  entry.snapshot = {
    ...entry.snapshot,
    session: merged,
  };
  emit(entry);
}

function fetchEntry(entry: Entry): Promise<void> {
  if (entry.inFlight) return entry.inFlight;
  entry.dirty = false;
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
        pendingUserMessages: reconcilePendingUserMessages(
          entry.snapshot.pendingUserMessages,
          result.events,
          result.composer_messages ?? [],
        ),
        error: null,
        loading: false,
      };
      entry.lastLoadedAt = Date.now();
    } catch (error) {
      if (!entry.snapshot.session) {
        entry.snapshot = {
          session: null,
          pendingUserMessages: entry.snapshot.pendingUserMessages,
          error: error instanceof Error ? error.message : "No session transcript found.",
          loading: false,
        };
      } else {
        entry.snapshot = { ...entry.snapshot, loading: false };
      }
    } finally {
      entry.inFlight = null;
      emit(entry);
      if (entry.dirty && entry.listeners.size > 0) void fetchEntry(entry);
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

export function refreshTranscript(ticket: string) {
  entries.forEach((entry) => {
    if (entry.target.ticket !== ticket || entry.target.subagent) return;
    entry.dirty = true;
    if (entry.listeners.size > 0) void fetchEntry(entry);
  });
}

export function replaceTranscriptQueue(
  ticket: string,
  messages: QueuedMessage[],
  annotation?: QueueSourceAnnotation,
) {
  entries.forEach((entry) => {
    if (entry.target.ticket !== ticket || entry.target.subagent || !entry.snapshot.session) return;
    const annotated = messages.map((message) => ({ ...message }));
    if (annotation) {
      const requestedIndex = annotation.position === undefined ? -1 : annotation.position - 1;
      let fallbackIndex = -1;
      for (let index = annotated.length - 1; index >= 0; index -= 1) {
        if (annotated[index].text === annotation.text) {
          fallbackIndex = index;
          break;
        }
      }
      const exactIndex = annotated.findIndex(
        (message) => message.pending_id === annotation.pendingId,
      );
      const legacyIndex = annotated.every((message) => !message.pending_id)
        ? (
          requestedIndex >= 0 && requestedIndex < annotated.length
            ? requestedIndex
            : fallbackIndex
        )
        : -1;
      const index = exactIndex >= 0
        ? exactIndex
        : legacyIndex;
      if (index >= 0) {
        annotated[index].source = annotation.source;
        annotated[index].pending_id ??= annotation.pendingId;
      }
    }
    const queuedPendingIds = new Set(
      annotated.flatMap((message) => message.pending_id ? [message.pending_id] : []),
    );
    entry.snapshot = {
      ...entry.snapshot,
      pendingUserMessages: entry.snapshot.pendingUserMessages.filter(
        (message) => !queuedPendingIds.has(message.id),
      ),
      session: {
        ...entry.snapshot.session,
        queue: mergeQueueSources(entry.snapshot.session.queue, annotated),
      },
    };
    emit(entry);
  });
}

export function addPendingUserMessage(
  ticket: string,
  message: Pick<PendingUserMessage, "id" | "text" | "mode">,
) {
  const entry = getEntry({ ticket });
  const events = entry.snapshot.session?.events ?? [];
  const eventIdFloor = events.length > 0 ? events[events.length - 1].id : -1;
  const pending: PendingUserMessage = {
    ...message,
    status: "sending",
    firstSeenTs: Date.now(),
    eventIdFloor,
  };
  entry.snapshot = {
    ...entry.snapshot,
    pendingUserMessages: [...entry.snapshot.pendingUserMessages, pending],
  };
  emit(entry);
}

export function updatePendingUserMessage(
  ticket: string,
  id: string,
  update: Partial<Pick<PendingUserMessage, "status" | "error">>,
) {
  const entry = getEntry({ ticket });
  let changed = false;
  const pendingUserMessages = entry.snapshot.pendingUserMessages.map((message) => {
    if (message.id !== id) return message;
    changed = true;
    return { ...message, ...update };
  });
  if (!changed) return;
  entry.snapshot = { ...entry.snapshot, pendingUserMessages };
  emit(entry);
}

export function retryPendingUserMessage(ticket: string, id: string) {
  const entry = getEntry({ ticket });
  const events = entry.snapshot.session?.events ?? [];
  const eventIdFloor = events.length > 0 ? events[events.length - 1].id : -1;
  let changed = false;
  const pendingUserMessages = entry.snapshot.pendingUserMessages.map((message) => {
    if (message.id !== id) return message;
    changed = true;
    return {
      ...message,
      status: "sending" as const,
      error: undefined,
      firstSeenTs: Date.now(),
      eventIdFloor,
    };
  });
  if (!changed) return;
  entry.snapshot = { ...entry.snapshot, pendingUserMessages };
  emit(entry);
}

export function removePendingUserMessage(ticket: string, id: string) {
  const entry = getEntry({ ticket });
  const pendingUserMessages = entry.snapshot.pendingUserMessages.filter((message) => message.id !== id);
  if (pendingUserMessages.length === entry.snapshot.pendingUserMessages.length) return;
  entry.snapshot = { ...entry.snapshot, pendingUserMessages };
  emit(entry);
}

export function replaceTranscriptDesiredModel(ticket: string, desiredModel: string | null) {
  entries.forEach((entry) => {
    if (entry.target.ticket !== ticket || entry.target.subagent || !entry.snapshot.session) return;
    entry.snapshot = {
      ...entry.snapshot,
      session: {
        ...entry.snapshot.session,
        desiredModel,
      },
    };
    emit(entry);
  });
}
