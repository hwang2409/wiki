import { expect, test } from "vitest";

import type { SessionEvent } from "../src/api";
import type { EventRow } from "../src/session-layout";
import { buildVirtualLayout, VIRTUAL_ROW_GAP } from "../src/session-layout";

function thought(id: number, text: string): SessionEvent {
  return {
    id,
    kind: "thinking",
    ts: `2026-08-17T12:00:0${id}.000Z`,
    text,
    disposition: "rendered",
    encrypted: true,
  };
}

function tool(id: number): SessionEvent {
  return {
    id,
    kind: "tool",
    ts: null,
    text: "",
    disposition: "rendered",
    tool: {
      name: "Read",
      input: "src/api.ts",
      output: "content",
      ok: true,
      archetype: "read",
      summary: "read api.ts",
    },
  };
}

function row(event: SessionEvent): EventRow {
  return { event, key: event.id };
}

test("accepts a multi-event thought-group measurement and shifts later rows", () => {
  const thoughts = [1, 2, 3].map((id) => row(thought(id, `thought ${id}`)));
  const group: EventRow = { ...thoughts[0], thoughts };
  const next = row(tool(4));
  const heights = new Map([
    [group.key, { refs: thoughts.map(({ event }) => event), height: 477 }],
    [next.key, { refs: [next.event], height: 80 }],
  ]);

  const layout = buildVirtualLayout([group, next], heights);

  expect(layout.sizes[0]).toBe(477 + VIRTUAL_ROW_GAP);
  expect(layout.tops[1]).toBe(477 + VIRTUAL_ROW_GAP);
  expect(layout.totalHeight).toBe(477 + VIRTUAL_ROW_GAP + 80);
});

test("collapse and expand round trips use each measured group height", () => {
  const thoughts = [1, 2, 3].map((id) => row(thought(id, `thought ${id}`)));
  const group: EventRow = { ...thoughts[0], thoughts };
  const next = row(tool(4));
  const nextHeight = { refs: [next.event], height: 80 };
  const refs = thoughts.map(({ event }) => event);
  const collapsed = new Map([
    [group.key, { refs, height: 40 }],
    [next.key, nextHeight],
  ]);
  const expanded = new Map([
    [group.key, { refs, height: 477 }],
    [next.key, nextHeight],
  ]);

  const collapsedLayout = buildVirtualLayout([group, next], collapsed);
  const expandedLayout = buildVirtualLayout([group, next], expanded);
  const collapsedAgainLayout = buildVirtualLayout([group, next], collapsed);

  expect(collapsedAgainLayout.totalHeight).toBe(collapsedLayout.totalHeight);
  expect(collapsedAgainLayout.tops).toEqual(collapsedLayout.tops);
  expect(expandedLayout.totalHeight).toBeGreaterThan(collapsedLayout.totalHeight);
  expect(expandedLayout.tops[1]).toBe(477 + VIRTUAL_ROW_GAP);
});
