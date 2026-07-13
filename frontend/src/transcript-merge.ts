import type { AgentSessionData, QueuedMessage, SessionEvent, SessionPatch } from "./api";
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
    subagents: result.subagents ?? [],
    queue: result.queue ?? [],
    working: result.working ?? false,
  };
}

export function mergeQueueSources(current: QueuedMessage[], next: QueuedMessage[]): QueuedMessage[] {
  if (current.length === 0 || next.length === 0) return next;
  const sources = new Map(
    current
      .filter((message) => message.source)
      .map((message) => [`${message.queued_at}\u0000${message.text}`, message.source] as const),
  );
  if (sources.size === 0) return next;
  return next.map((message) => ({
    ...message,
    source: message.source ?? sources.get(`${message.queued_at}\u0000${message.text}`),
  }));
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
    return current;
  }
  // The server may retain events older than the client's initial tail window.
  // That lower retention base is compatible with an unchanged loaded suffix.
  if (
    result.tail_from === clientEnd &&
    result.events.length === 0 &&
    result.patches.length === 0 &&
    result.base < current.base
  ) {
    return current;
  }

  let base = current.base;
  let events = current.events;
  let changedFrom = events.length;

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

  return {
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
    pr: result.pr ?? current.pr,
    sessionMeta: result.session_meta ?? current.sessionMeta,
    dispositions: result.dispositions ?? current.dispositions,
    providerInspector: result.provider_inspector ?? current.providerInspector,
    base,
    cursor: result.cursor,
    events,
    eventsChangedFrom: changedFrom,
    hasOlder: result.has_older ?? current.hasOlder,
    subagents: result.subagents ?? current.subagents,
    queue: result.queue ? mergeQueueSources(current.queue, result.queue) : current.queue,
    working: result.working ?? current.working,
  };
}
