import assert from "node:assert/strict";
import test from "node:test";

import {
  buildVirtualLayout,
  buildVirtualLayoutIncremental,
  eventRows,
  eventRowsIncremental,
  VIRTUAL_ROW_GAP,
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

function toolEvent(id, archetype, name = archetype) {
  return {
    ...event(id, "tool"),
    tool: { name, input: "", output: null, ok: true, archetype, summary: name },
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
});

test("mergeSession retains a tool result completion timestamp from a patch", () => {
  const pendingTool = event(0, "tool");
  const current = buildSession(sessionData([pendingTool]));
  const result = {
    ...sessionData([]),
    cursor: current.cursor + 1,
    tail_from: current.base + current.events.length,
    base: current.base,
    patches: [{
      id: 0,
      output: "tests passed",
      ok: true,
      completed_at: "2026-08-02T12:00:04Z",
    }],
  };

  const merged = mergeSession(current, result);
  assert.equal(merged.events[0].tool.completed_at, "2026-08-02T12:00:04Z");
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

test("mergeQueueSources preserves an unmatched legacy snapshot by reference", () => {
  const current = [{
    text: "old queued message",
    queued_at: "2026-07-13T12:00:00Z",
    pending_id: "73f65e1c-ad09-4ce5-9ca8-31bb44db431b",
  }];
  const next = [{
    text: "new queued message",
    queued_at: "2026-07-13T12:01:00Z",
  }];

  const merged = mergeQueueSources(current, next);
  assert.strictEqual(merged, next);
  assert.strictEqual(merged[0], next[0]);
  assert.deepEqual(Object.keys(merged[0]), ["text", "queued_at"]);
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
});

test("mergeSession keeps the current tail when an empty poll moves base backward", () => {
  const current = buildSession({
    ...sessionData(Array.from({ length: 500 }, (_, index) => event(49_500 + index))),
    base: 49_500,
    tail_from: 49_500,
    has_older: true,
  });
  const result = {
    ...sessionData([]),
    base: 48_000,
    cursor: current.cursor,
    tail_from: 48_000,
    patches: [],
  };

  const merged = mergeSession(current, result);
  assert.strictEqual(merged, current);
  assert.equal(merged.hasOlder, true);
  assert.equal(merged.base, 49_500);
  assert.equal(merged.events.length, 500);
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

test("eventRowsIncremental keeps one virtual row per event", () => {
  let events = [event(0), event(1, "tool"), event(2, "thinking"), event(3)];
  let result = eventRowsIncremental(events, 0, null);
  assert.deepEqual(result.rows, eventRows(events, 0));
  assert.equal(result.rows.length, events.length);
  assert.deepEqual(result.rows.map((row) => row.key), [0, 1, 2, 3]);

  events = [...events, event(4, "tool"), event(5, "thinking"), event(6)];
  result = eventRowsIncremental(events, 0, result.cache);
  assert.deepEqual(result.rows, eventRows(events, 0));
  assert.equal(result.rows.length, events.length);

  events = events.map((entry, index) => index === 1
    ? { ...entry, tool: { ...entry.tool, output: "patched", ok: true } }
    : entry);
  result = eventRowsIncremental(events, 0, result.cache);
  assert.deepEqual(result.rows, eventRows(events, 0));

  events = events.slice(2);
  result = eventRowsIncremental(events, 2, result.cache);
  assert.deepEqual(result.rows, eventRows(events, 2));
  assert.equal(result.changedFrom, 0);
});

// WIKI-259: an event that renders no visible content must not reserve row
// height + gap in the virtual layout — that painted as a random blank band.
test("events that render nothing produce no virtual rows", () => {
  const events = [
    toolEvent(0, "read", "Read"),
    event(1, "thinking", ""),
    toolEvent(2, "read", "Read"),
    { ...event(3, "tool"), tool: undefined },
    event(4, "assistant", "   "),
    event(5, "assistant", "prose"),
  ];
  const rows = eventRows(events, 0);
  assert.deepEqual(rows.map((row) => row.key), [0, 2, 5], "hidden events are skipped, keys stay event-indexed");

  const heights = new Map(rows.map((row) => [row.key, { refs: [row.event], height: 20 }]));
  const layout = buildVirtualLayout(rows, heights);
  assert.equal(layout.tops[1] - layout.tops[0], 20, "hidden thought adds no height or gap between inline tools");
  assert.equal(layout.totalHeight, 20 + (20 + VIRTUAL_ROW_GAP) + 20);
});

test("claude init stays indexed while its transcript row is hidden", () => {
  const events = [event(0, "claude_init", ""), event(1, "assistant", "reply")];
  const result = eventRowsIncremental(events, 0, null);

  assert.deepEqual(result.rows.map((row) => row.key), [1]);
  assert.equal(result.rows[0].event.id, 1);
});

test("a hidden thinking event becomes a row when its text streams in", () => {
  let events = [toolEvent(0, "read", "Read"), event(1, "thinking", ""), toolEvent(2, "read", "Read")];
  let result = eventRowsIncremental(events, 0, null);
  assert.deepEqual(result.rows.map((row) => row.key), [0, 2]);

  events = events.map((entry, index) => (index === 1 ? { ...entry, text: "now visible" } : entry));
  result = eventRowsIncremental(events, 0, result.cache);
  assert.deepEqual(result.rows, eventRows(events, 0));
  assert.deepEqual(result.rows.map((row) => row.key), [0, 1, 2]);
});

test("incremental rows and layout stay correct around hidden events", () => {
  let events = [event(0), event(1, "thinking", ""), event(2, "tool")];
  let grouped = eventRowsIncremental(events, 0, null);
  assert.deepEqual(grouped.rows, eventRows(events, 0));
  const heights = new Map();
  let incremental = buildVirtualLayoutIncremental(grouped.rows, heights, 0, null, grouped.changedFrom);
  assert.deepEqual(incremental.layout, buildVirtualLayout(grouped.rows, heights));

  events = [...events, event(3, "thinking", ""), event(4)];
  grouped = eventRowsIncremental(events, 0, grouped.cache);
  assert.deepEqual(grouped.rows, eventRows(events, 0));
  incremental = buildVirtualLayoutIncremental(grouped.rows, heights, 0, incremental.cache, grouped.changedFrom);
  assert.deepEqual(incremental.layout, buildVirtualLayout(grouped.rows, heights));
});

test("virtual row spacing follows rendered presentation", () => {
  const rows = eventRows([
    toolEvent(0, "read", "Read"),
    toolEvent(1, "read", "Read"),
    toolEvent(2, "bash", "Bash"),
    event(3, "thinking", "thought"),
    event(4, "assistant", "prose"),
  ], 0);
  const heights = new Map(rows.map((row) => [row.key, { refs: [row.event], height: 20 }]));
  const layout = buildVirtualLayout(rows, heights);
  const topDeltas = layout.tops.slice(1).map((top, index) => top - layout.tops[index]);

  assert.equal(topDeltas[0], 20, "inline to inline rows have no gap");
  assert.equal(topDeltas[1], 20 + VIRTUAL_ROW_GAP, "inline to block rows have one gap");
  assert.equal(topDeltas[2], 20 + VIRTUAL_ROW_GAP, "block to thought rows have one gap");
  assert.equal(topDeltas[3], 20 + VIRTUAL_ROW_GAP, "thought to prose rows have one gap");
});

test("buildVirtualLayoutIncremental matches full layout across row and height deltas", () => {
  let events = [event(0), event(1, "tool"), event(2), event(3)];
  let grouped = eventRowsIncremental(events, 0, null);
  const heights = new Map();
  let incremental = buildVirtualLayoutIncremental(grouped.rows, heights, 0, null, grouped.changedFrom);
  assert.deepEqual(incremental.layout, buildVirtualLayout(grouped.rows, heights));

  events = [...events, event(4), event(5, "tool")];
  grouped = eventRowsIncremental(events, 0, grouped.cache);
  incremental = buildVirtualLayoutIncremental(
    grouped.rows,
    heights,
    0,
    incremental.cache,
    grouped.changedFrom,
  );
  assert.deepEqual(incremental.layout, buildVirtualLayout(grouped.rows, heights));

  const measuredRow = grouped.rows[1];
  heights.set(measuredRow.key, { refs: [measuredRow.event], height: 321 });
  incremental = buildVirtualLayoutIncremental(grouped.rows, heights, 1, incremental.cache, 1);
  assert.deepEqual(incremental.layout, buildVirtualLayout(grouped.rows, heights));

  events = events.slice(0, -1);
  grouped = eventRowsIncremental(events, 0, grouped.cache);
  incremental = buildVirtualLayoutIncremental(grouped.rows, heights, 1, incremental.cache, grouped.changedFrom);
  assert.deepEqual(incremental.layout, buildVirtualLayout(grouped.rows, heights));

  events = events.slice(2);
  grouped = eventRowsIncremental(events, 2, grouped.cache);
  incremental = buildVirtualLayoutIncremental(grouped.rows, heights, 1, incremental.cache, grouped.changedFrom);
  assert.deepEqual(incremental.layout, buildVirtualLayout(grouped.rows, heights));
});
