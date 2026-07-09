import assert from "node:assert/strict";
import test from "node:test";
import { createStateKeyWriteBarrier, deletePaneStateEntries } from "../src/pane-state-cache.ts";

test("deletePaneStateEntries removes only the targeted pane prefix", () => {
  const cache = new Map<string, number>([
    ["pane-1:ticket-a", 1],
    ["pane-1:ticket-b", 2],
    ["pane-2:ticket-c", 3],
  ]);

  const deleted = deletePaneStateEntries(cache, "pane-1");

  assert.deepEqual(deleted.sort(), ["pane-1:ticket-a", "pane-1:ticket-b"]);
  assert.deepEqual([...cache.entries()], [["pane-2:ticket-c", 3]]);
});

test("blocked keys skip the close-time reinsert and allow later saves after consume", () => {
  const cache = new Map<string, { scrollTop: number }>([["pane-1:ticket-a", { scrollTop: 12 }]]);
  const barrier = createStateKeyWriteBarrier();
  const deleted = deletePaneStateEntries(cache, "pane-1");
  const key = "pane-1:ticket-a";

  barrier.block(deleted);
  if (barrier.allows(key)) cache.set(key, { scrollTop: 40 });

  assert.equal(cache.has(key), false);
  assert.equal(barrier.consume(key), true);
  assert.equal(barrier.allows(key), true);

  if (barrier.allows(key)) cache.set(key, { scrollTop: 64 });
  assert.deepEqual(cache.get(key), { scrollTop: 64 });
});
