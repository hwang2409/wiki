import assert from "node:assert/strict";
import test from "node:test";
import { findMatches, type PageTextIndex } from "../src/pdfjs-runtime.ts";

const INDEX: PageTextIndex[] = [
  { page: 1, text: "silky pdf artifact preview" },
  { page: 2, text: "second page silky mention" },
  { page: 3, text: "terrarium unique marker" },
];

test("findMatches skips empty needles", () => {
  assert.deepEqual(findMatches(INDEX, ""), []);
  assert.deepEqual(findMatches(INDEX, "   "), []);
});

test("findMatches locates occurrences case-insensitively across pages", () => {
  const matches = findMatches(INDEX, "silky");
  assert.equal(matches.length, 2);
  assert.deepEqual(
    matches.map((match) => match.page),
    [1, 2],
  );
});

test("findMatches finds the unique marker used by the fixture pdf test", () => {
  const matches = findMatches(INDEX, "terrarium");
  assert.deepEqual(matches, [{ page: 3, matchIndex: 0 }]);
});

test("findMatches counts multiple matches on the same page", () => {
  const matches = findMatches(
    [{ page: 5, text: "aa bb aa bb aa" }],
    "aa",
  );
  assert.equal(matches.length, 3);
  assert.deepEqual(
    matches.map((match) => match.matchIndex),
    [0, 1, 2],
  );
});
