import assert from "node:assert/strict";
import test from "node:test";
import {
  FIND_MATCH_LIMIT,
  boundedTextStream,
  extractPageText,
  findMatches,
  type PageTextIndex,
  type StreamablePage,
} from "../src/pdfjs-runtime.ts";

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

test("findMatches does not report truncation when hit count equals the cap exactly", () => {
  // Exactly `limit` matches must return truncated=false so the UI badge
  // reads "100/100" instead of a misleading "100/100+".
  const spam = "a".repeat(100);
  const { matches, truncated } = findMatches([{ page: 1, text: spam }], "a", 100);
  assert.equal(matches.length, 100);
  assert.equal(truncated, false);
});

test("FIND_MATCH_LIMIT is a small enough ceiling to stay in memory", () => {
  // Guardrail so a future bump doesn't quietly land at, say, 10M matches.
  assert.ok(FIND_MATCH_LIMIT <= 10_000, `FIND_MATCH_LIMIT is ${FIND_MATCH_LIMIT}`);
});

test("extractPageText stops pulling and cancels once the char budget is hit", async () => {
  // Peak memory bound: even when the underlying page would emit far more
  // text, the extractor MUST cancel the reader so pdf.js doesn't buffer
  // additional chunks in the worker or main thread heap.
  let pulled = 0;
  let cancelled = false;
  const stream = new ReadableStream<{ items: Array<{ str: string }> }>({
    pull(controller) {
      pulled += 1;
      if (pulled > 40) {
        controller.close();
        return;
      }
      controller.enqueue({ items: [{ str: "x".repeat(1000) }] });
    },
    cancel() {
      cancelled = true;
    },
  });
  const page: StreamablePage = { streamTextContent: () => stream };
  const text = await extractPageText(page, { charLimit: 500 });
  assert.equal(text.length, 500, "text is capped to the char budget");
  assert.equal(cancelled, true, "reader.cancel() was invoked once the cap was hit");
  // Allow one pre-fetched pull past what we consumed (browser buffering);
  // if this jumps into the tens we've regressed to buffer-everything.
  assert.ok(pulled <= 3, `expected ≤3 pulls before cancel, got ${pulled}`);
});

test("boundedTextStream cancels the source once the character budget is exhausted", async () => {
  // Renderer-level bound: the raw stream would emit far more text than the
  // TextLayer should ever see. boundedTextStream must forward only what
  // fits in `charLimit` and cancel the upstream reader so pdf.js stops
  // decoding — this is the guarantee that the render path stays bounded
  // regardless of hostile-size page text.
  let pulled = 0;
  let cancelled = false;
  const source = new ReadableStream<{ items: Array<{ str: string }> }>({
    pull(controller) {
      pulled += 1;
      if (pulled > 100) {
        controller.close();
        return;
      }
      controller.enqueue({ items: [{ str: "y".repeat(1000) }] });
    },
    cancel() {
      cancelled = true;
    },
  });
  const bounded = boundedTextStream(source, 500);
  const forwarded: Array<{ items?: Array<{ str?: string }> }> = [];
  const reader = bounded.stream.getReader();
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      forwarded.push(value as { items?: Array<{ str?: string }> });
    }
  } finally {
    reader.releaseLock();
  }
  const totalChars = forwarded
    .flatMap((chunk) => chunk.items ?? [])
    .reduce((sum, item) => sum + (item?.str?.length ?? 0), 0);
  assert.equal(totalChars, 500, "forwarded exactly the char budget, no more");
  assert.equal(cancelled, true, "upstream source was cancelled once the budget was hit");
  assert.ok(pulled <= 3, `expected ≤3 upstream pulls before cancel, got ${pulled}`);
});

test("boundedTextStream cancel() propagates to the source (unmount path)", async () => {
  // Simulates the render-effect cleanup calling `.cancel()` on the returned
  // handle before render completes — the upstream reader must be released
  // so a mid-flight text-layer stream stops decoding on page-switch.
  let cancelled = false;
  const source = new ReadableStream({
    pull() {
      // Never resolve — we cancel before the consumer ever pulls.
    },
    cancel() {
      cancelled = true;
    },
  });
  const bounded = boundedTextStream(source, 500);
  await bounded.cancel();
  assert.equal(cancelled, true, "cancel() propagated to the upstream reader");
});

test("extractPageText concatenates all items when the page fits under the budget", async () => {
  const stream = new ReadableStream<{ items: Array<{ str: string }> }>({
    start(controller) {
      controller.enqueue({ items: [{ str: "hello" }, { str: "world" }] });
      controller.enqueue({ items: [{ str: "again" }] });
      controller.close();
    },
  });
  const page: StreamablePage = { streamTextContent: () => stream };
  const text = await extractPageText(page, { charLimit: 500 });
  assert.equal(text, "hello world again");
});
