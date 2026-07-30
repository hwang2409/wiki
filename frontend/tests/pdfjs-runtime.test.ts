import assert from "node:assert/strict";
import test from "node:test";
import { FIND_MATCH_LIMIT, findMatches, type PageTextIndex } from "../src/pdfjs-runtime.ts";

const INDEX: PageTextIndex[] = [
  { page: 1, text: "silky pdf artifact preview" },
  { page: 2, text: "second page silky mention" },
  { page: 3, text: "terrarium unique marker" },
];

test("findMatches skips empty needles", () => {
  assert.deepEqual(findMatches(INDEX, ""), { matches: [], truncated: false });
  assert.deepEqual(findMatches(INDEX, "   "), { matches: [], truncated: false });
});

test("findMatches locates occurrences case-insensitively across pages", () => {
  const { matches, truncated } = findMatches(INDEX, "silky");
  assert.equal(truncated, false);
  assert.equal(matches.length, 2);
  assert.deepEqual(
    matches.map((match) => match.page),
    [1, 2],
  );
});

test("findMatches finds the unique marker used by the fixture pdf test", () => {
  const { matches, truncated } = findMatches(INDEX, "terrarium");
  assert.equal(truncated, false);
  assert.deepEqual(matches, [{ page: 3, matchIndex: 0 }]);
});

test("findMatches counts multiple matches on the same page", () => {
  const { matches, truncated } = findMatches(
    [{ page: 5, text: "aa bb aa bb aa" }],
    "aa",
  );
  assert.equal(truncated, false);
  assert.equal(matches.length, 3);
  assert.deepEqual(
    matches.map((match) => match.matchIndex),
    [0, 1, 2],
  );
});

test("findMatches truncates and reports when a query exceeds the match limit", () => {
  // A single page whose text contains many single-char hits blows past a
  // small limit; the helper must return exactly `limit` matches and set
  // truncated=true so the UI can surface it instead of allocating forever.
  const spam = "a".repeat(5000);
  const { matches, truncated } = findMatches([{ page: 1, text: spam }], "a", 100);
  assert.equal(truncated, true);
  assert.equal(matches.length, 100);
});

test("FIND_MATCH_LIMIT is a small enough ceiling to stay in memory", () => {
  // Guardrail so a future bump doesn't quietly land at, say, 10M matches.
  assert.ok(FIND_MATCH_LIMIT <= 10_000, `FIND_MATCH_LIMIT is ${FIND_MATCH_LIMIT}`);
});
