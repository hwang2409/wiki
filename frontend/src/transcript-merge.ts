import type {
  AgentOlderSessionData,
  AgentSessionData,
  QueuedMessage,
  SessionEvent,
  SessionPatch,
} from "./api";
import type { TranscriptSession } from "./transcript-store";

export function buildSession(result: AgentSessionData): TranscriptSession {
  return {
    format: result.format,
    path: result.path,
    tokens: result.tokens,
    model: result.model ?? null,
    desiredModel: result.desired_model ?? null,
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
    eventsChangedFrom: 0,
    hasOlder: result.has_older ?? false,
    composerMessages: result.composer_messages ?? [],
    subagents: result.subagents ?? [],
    queue: result.queue ?? [],
    working: result.working ?? false,
  };
}

function queueKey(message: QueuedMessage): string {
  return message.pending_id ?? legacyQueueKey(message);
}

function legacyQueueKey(message: QueuedMessage): string {
  return `${message.queued_at}\u0000${message.text}`;
}

export function mergeQueueSources(current: QueuedMessage[], next: QueuedMessage[]): QueuedMessage[] {
  if (current.length === 0 || next.length === 0) return next;
  const previous = new Map<string, QueuedMessage>();
  current.forEach((message) => {
    previous.set(queueKey(message), message);
    previous.set(legacyQueueKey(message), message);
  });
  let changed = false;
  const merged = next.map((message) => {
    const match = previous.get(queueKey(message)) ?? previous.get(legacyQueueKey(message));
    const pendingId = message.pending_id ?? match?.pending_id;
    const source = message.source ?? match?.source;
    if (pendingId === message.pending_id && source === message.source) return message;
    changed = true;
    return {
      ...message,
      ...(pendingId ? { pending_id: pendingId } : {}),
      ...(source ? { source } : {}),
    };
  });
  return changed ? merged : next;
}

function sameJsonValue(left: unknown, right: unknown): boolean {
  if (Object.is(left, right)) return true;
  if (Array.isArray(left) || Array.isArray(right)) {
    return Array.isArray(left)
      && Array.isArray(right)
      && left.length === right.length
      && left.every((value, index) => sameJsonValue(value, right[index]));
  }
  if (!left || !right || typeof left !== "object" || typeof right !== "object") return false;
  const leftRecord = left as Record<string, unknown>;
  const rightRecord = right as Record<string, unknown>;
  const leftKeys = Object.keys(leftRecord);
  const rightKeys = Object.keys(rightRecord);
  return leftKeys.length === rightKeys.length
    && leftKeys.every((key) => Object.prototype.hasOwnProperty.call(rightRecord, key)
      && sameJsonValue(leftRecord[key], rightRecord[key]));
}

function mergeSessionState(
  current: TranscriptSession,
  result: AgentSessionData,
  base: number,
  events: SessionEvent[],
  eventsChangedFrom: number,
): TranscriptSession {
  const next: TranscriptSession = {
    ...current,
    format: result.format,
    path: result.path,
    tokens: result.tokens,
    model: result.model ?? current.model,
    desiredModel: Object.prototype.hasOwnProperty.call(result, "desired_model")
      ? result.desired_model ?? null
      : current.desiredModel,
    kind: result.kind ?? current.kind,
    provider: result.provider ?? current.provider,
    tasks: result.tasks ?? current.tasks,
    pr: Object.prototype.hasOwnProperty.call(result, "pr")
      ? result.pr ?? null
      : current.pr,
    sessionMeta: result.session_meta ?? current.sessionMeta,
    dispositions: result.dispositions ?? current.dispositions,
    providerInspector: result.provider_inspector ?? current.providerInspector,
    base,
    cursor: result.cursor,
    events,
    eventsChangedFrom,
    hasOlder: result.has_older ?? current.hasOlder,
    composerMessages: result.composer_messages ?? current.composerMessages,
    subagents: result.subagents ?? current.subagents,
    queue: result.queue ? mergeQueueSources(current.queue, result.queue) : current.queue,
    working: result.working ?? current.working,
  };
  const unchanged = next.format === current.format
    && next.path === current.path
    && next.tokens === current.tokens
    && next.model === current.model
    && next.desiredModel === current.desiredModel
    && next.kind === current.kind
    && next.provider === current.provider
    && next.base === current.base
    && next.cursor === current.cursor
    && next.events === current.events
    && next.hasOlder === current.hasOlder
    && next.working === current.working
    && sameJsonValue(next.tasks, current.tasks)
    && sameJsonValue(next.pr, current.pr)
    && sameJsonValue(next.sessionMeta, current.sessionMeta)
    && sameJsonValue(next.dispositions, current.dispositions)
    && sameJsonValue(next.providerInspector, current.providerInspector)
    && sameJsonValue(next.composerMessages, current.composerMessages)
    && sameJsonValue(next.subagents, current.subagents)
    && sameJsonValue(next.queue, current.queue);
  return unchanged ? current : next;
}

export function prependOlderEvents(
  current: TranscriptSession,
  result: AgentOlderSessionData,
  before: number,
): TranscriptSession | null {
  if (
    current.path !== result.path
    || current.base !== before
    || result.base + result.events.length !== before
  ) {
    return null;
  }
  return {
    ...current,
    base: result.base,
    events: [...result.events, ...current.events],
    eventsChangedFrom: 0,
    hasOlder: result.has_older,
  };
}

function applyPatches(
  events: SessionEvent[],
  patches: SessionPatch[],
): { events: SessionEvent[]; changedFrom: number } {
  if (patches.length === 0) return { events, changedFrom: events.length };
  const indexById = new Map<number, number>();
  events.forEach((event, index) => indexById.set(event.id, index));
  let next = events;
  let changedFrom = events.length;
  for (const patch of patches) {
    const index = indexById.get(patch.id);
    if (index === undefined) continue;
    const event = next[index];
    if (!event?.tool) continue;
    if (event.tool.output === patch.output && event.tool.ok === patch.ok) continue;
    if (next === events) next = events.slice();
    changedFrom = Math.min(changedFrom, index);
    next[index] = {
      ...event,
      tool: {
        ...event.tool,
        output: patch.output,
        ok: patch.ok,
      },
    };
  }
  return { events: next, changedFrom };
}

export function mergeSession(
  current: TranscriptSession | null,
  result: AgentSessionData,
): TranscriptSession {
  if (!current || current.path !== result.path || result.cursor < current.cursor) {
    return buildSession(result);
  }

  const clientEnd = current.base + current.events.length;
  if (
    result.tail_from === clientEnd &&
    result.events.length === 0 &&
    result.patches.length === 0 &&
    result.base === current.base &&
    result.path === current.path
  ) {
    return mergeSessionState(current, result, current.base, current.events, current.events.length);
  }
  if (
    result.events.length === 0 &&
    result.patches.length === 0 &&
    result.base < current.base
  ) {
    // The server may retain events older than the client's initial tail window,
    // and any empty poll that moves the base backward is compatible with the
    // already-loaded suffix because the cursor guard above rejects regressions.
    return mergeSessionState(
      current,
      { ...result, has_older: current.hasOlder || Boolean(result.has_older) },
      current.base,
      current.events,
      current.events.length,
    );
  }
  if (
    result.events.length === 0 &&
    result.patches.length === 0 &&
    result.base < current.base
  ) {
    return mergeSessionState(
      current,
      { ...result, has_older: current.hasOlder || Boolean(result.has_older) },
      current.base,
      current.events,
      current.events.length,
    );
  }

  let base = current.base;
  let events = current.events;
  let changedFrom = events.length;

  // A full reset after change-log overflow can start inside a client prefix
  // loaded through older-page pagination. Preserve that still-contiguous
  // prefix and replace only the suffix covered by the reset.
  if (
    result.tail_from === result.base
    && result.base > base
    && result.base <= clientEnd
  ) {
    const prefixLength = result.base - base;
    events = events.slice(0, prefixLength).concat(result.events);
    changedFrom = prefixLength;
    const patched = applyPatches(events, result.patches);
    return mergeSessionState(
      current,
      result,
      base,
      patched.events,
      Math.min(changedFrom, patched.changedFrom),
    );
  }

  if (result.base > base) {
    const trim = result.base - base;
    if (trim >= events.length) return buildSession(result);
    events = events.slice(trim);
    base = result.base;
    changedFrom = 0;
  }

  const currentEnd = base + events.length;
  if (result.tail_from < base || result.tail_from > currentEnd) {
    return buildSession(result);
  }

  const tailIndex = result.tail_from - base;
  if (tailIndex !== events.length || result.events.length > 0) {
    events = events.slice(0, tailIndex).concat(result.events);
    changedFrom = Math.min(changedFrom, tailIndex);
  }
  const patched = applyPatches(events, result.patches);
  events = patched.events;
  changedFrom = Math.min(changedFrom, patched.changedFrom);

  return mergeSessionState(current, result, base, events, changedFrom);
}
