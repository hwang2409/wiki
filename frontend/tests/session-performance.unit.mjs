import assert from "node:assert/strict";
import test from "node:test";

import {
  buildVirtualLayout,
  buildVirtualLayoutIncremental,
  groupEvents,
  groupEventsIncremental,
} from "../src/session-layout.ts";
import {
  buildSession,
  mergeQueueSources,
  mergeSession,
  prependOlderEvents,
} from "../src/transcript-merge.ts";

function event(id, kind = "assistant", text = `event-${id}`) {
  return {
    id,
    kind,
    ts: null,
    text,
    disposition: "rendered",
    ...(kind === "tool"
      ? { tool: { name: "fixture", input: "", output: null, ok: null, archetype: "other", summary: "" } }
      : {}),
  };
}

function sessionData(events) {
  return {
    version: 2,
    format: "codex",
    path: "/tmp/wiki-94-session.jsonl",
    tokens: 1,
    base: 0,
    cursor: events.length,
    tail_from: 0,
    events,
    patches: [],
    has_older: false,
  };
}

test("mergeSession returns the current session by reference for a no-op poll", () => {
  const current = buildSession(sessionData([event(0), event(1)]));
  const result = {
    ...sessionData([]),
    cursor: current.cursor,
    tail_from: current.base + current.events.length,
    base: current.base,
    tasks: [],
    pr: null,
    session_meta: {},
    dispositions: { rendered: 0, summarized: 0, ignored: 0, unknown: 0 },
    subagents: [],
    queue: [],
    working: false,
  };

  assert.strictEqual(mergeSession(current, result), current);
});

test("mergeSession appends events whose tail starts at the current end", () => {
  const current = buildSession(sessionData([event(0), event(1)]));
  const appended = event(2, "user", "new message");
  const result = {
    ...sessionData([appended]),
    cursor: current.cursor + 1,
    tail_from: current.base + current.events.length,
    base: current.base,
  };

  const merged = mergeSession(current, result);
  assert.notStrictEqual(merged, current);
  assert.deepEqual(merged.events, [...current.events, appended]);
  assert.equal(merged.eventsChangedFrom, current.events.length);
});

test("mergeSession applies metadata-only changes without replacing events", () => {
  const current = buildSession({
    ...sessionData([event(0), event(1)]),
    desired_model: "gpt-old",
    tasks: [{ id: "task-1", subject: "old", status: "pending" }],
    pr: { number: 75, url: "https://example.test/pr/75" },
    subagents: [{ id: "sub-1", active: true, head: "old" }],
    working: true,
    queue: [{ text: "first", queued_at: "2026-07-13T12:00:00Z" }],
  });
  const result = {
    ...sessionData([]),
    cursor: current.cursor,
    tail_from: current.base + current.events.length,
    base: current.base,
    desired_model: "gpt-new",
    tasks: [{ id: "task-1", subject: "new", status: "completed" }],
    pr: null,
    subagents: [{ id: "sub-1", active: false, head: "old" }],
    working: false,
    queue: [{ text: "cross-client", queued_at: "2026-07-13T12:01:00Z" }],
  };

  const merged = mergeSession(current, result);
  assert.notStrictEqual(merged, current);
  assert.strictEqual(merged.events, current.events);
  assert.equal(merged.desiredModel, "gpt-new");
  assert.equal(merged.tasks[0].status, "completed");
  assert.equal(merged.pr, null);
  assert.equal(merged.subagents[0].active, false);
  assert.equal(merged.working, false);
  assert.deepEqual(merged.queue, result.queue);
});

test("mergeSession applies composer acknowledgements without replacing events", () => {
  const current = buildSession(sessionData([event(0), event(1)]));
  const composerMessage = {
    pending_id: "73f65e1c-ad09-4ce5-9ca8-31bb44db431b",
    text: "mid-turn message",
    sent_at: "2026-07-13T12:00:00Z",
    echoed_at: "2026-07-13T12:01:00Z",
    seq: 42,
  };
  const result = {
    ...sessionData([]),
    cursor: current.cursor,
    tail_from: current.base + current.events.length,
    base: current.base,
    composer_messages: [composerMessage],
  };

  const merged = mergeSession(current, result);
  assert.notStrictEqual(merged, current);
  assert.strictEqual(merged.events, current.events);
  assert.deepEqual(merged.composerMessages, [composerMessage]);
});

test("mergeQueueSources preserves a pending id across queue snapshots", () => {
  const current = [{
    text: "queued once",
    queued_at: "2026-07-13T12:00:00Z",
    pending_id: "73f65e1c-ad09-4ce5-9ca8-31bb44db431b",
    source: "explicit",
  }];
  const next = [{
    text: "queued once",
    queued_at: "2026-07-13T12:00:00Z",
  }];

  assert.deepEqual(mergeQueueSources(current, next), current);
});

test("mergeSession preserves loaded older prefix across a full reset", () => {
  const current = buildSession(sessionData(Array.from({ length: 6 }, (_, index) => event(index))));
  const resetEvents = [event(3, "assistant", "reset-3"), event(4), event(5), event(6)];
  const result = {
    ...sessionData(resetEvents),
    base: 3,
    cursor: 5_000,
    tail_from: 3,
    has_older: false,
  };

  const merged = mergeSession(current, result);
  assert.equal(merged.base, 0);
  assert.deepEqual(merged.events.slice(0, 3), current.events.slice(0, 3));
  assert.deepEqual(merged.events.slice(3), resetEvents);
  assert.equal(merged.eventsChangedFrom, 3);
});

test("prependOlderEvents rejects a non-contiguous page", () => {
  const current = buildSession({
    ...sessionData([event(3), event(4)]),
    base: 3,
    tail_from: 3,
  });
  const result = {
    version: 2,
    format: "codex",
    path: current.path,
    base: 4,
    events: [],
    has_older: false,
  };

  assert.equal(prependOlderEvents(current, result, 3), null);
});

test("groupEventsIncremental matches full grouping for append, patch, and base drift", () => {
  let events = [event(0), event(1, "tool"), event(2, "thinking"), event(3)];
  let result = groupEventsIncremental(events, 0, null);
  assert.deepEqual(result.groups, groupEvents(events, 0));

  events = [...events, event(4, "tool"), event(5, "thinking"), event(6)];
  result = groupEventsIncremental(events, 0, result.cache);
  assert.deepEqual(result.groups, groupEvents(events, 0));

  events = events.map((entry, index) => index === 1
    ? { ...entry, tool: { ...entry.tool, output: "patched", ok: true } }
    : entry);
  result = groupEventsIncremental(events, 0, result.cache);
  assert.deepEqual(result.groups, groupEvents(events, 0));

  events = events.slice(2);
  result = groupEventsIncremental(events, 2, result.cache);
  assert.deepEqual(result.groups, groupEvents(events, 2));
  assert.equal(result.changedFrom, 0);
});

test("buildVirtualLayoutIncremental matches full layout across group and height deltas", () => {
  let events = [event(0), event(1, "tool"), event(2), event(3)];
  let grouped = groupEventsIncremental(events, 0, null);
  const heights = new Map();
  let incremental = buildVirtualLayoutIncremental(grouped.groups, heights, 0, null, grouped.changedFrom);
  assert.deepEqual(incremental.layout, buildVirtualLayout(grouped.groups, heights));

  events = [...events, event(4), event(5, "tool")];
  grouped = groupEventsIncremental(events, 0, grouped.cache);
  incremental = buildVirtualLayoutIncremental(
    grouped.groups,
    heights,
    0,
    incremental.cache,
    grouped.changedFrom,
  );
  assert.deepEqual(incremental.layout, buildVirtualLayout(grouped.groups, heights));

  const measuredGroup = grouped.groups[1];
  const refs = measuredGroup.kind === "activity" ? measuredGroup.events : [measuredGroup.event];
  heights.set(measuredGroup.key, { refs, height: 321 });
  incremental = buildVirtualLayoutIncremental(grouped.groups, heights, 1, incremental.cache, 1);
  assert.deepEqual(incremental.layout, buildVirtualLayout(grouped.groups, heights));

  events = events.slice(0, -1);
  grouped = groupEventsIncremental(events, 0, grouped.cache);
  incremental = buildVirtualLayoutIncremental(grouped.groups, heights, 1, incremental.cache, grouped.changedFrom);
  assert.deepEqual(incremental.layout, buildVirtualLayout(grouped.groups, heights));

  events = events.slice(2);
  grouped = groupEventsIncremental(events, 2, grouped.cache);
  incremental = buildVirtualLayoutIncremental(grouped.groups, heights, 1, incremental.cache, grouped.changedFrom);
  assert.deepEqual(incremental.layout, buildVirtualLayout(grouped.groups, heights));
});
