import assert from "node:assert/strict";
import test from "node:test";

import {
  buildVirtualLayout,
  buildVirtualLayoutIncremental,
  groupEvents,
  groupEventsIncremental,
} from "../src/session-layout.ts";
import { buildSession, mergeSession } from "../src/transcript-merge.ts";

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
