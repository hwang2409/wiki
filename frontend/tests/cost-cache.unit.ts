import assert from "node:assert/strict";
import test from "node:test";

import { getTicketCosts } from "../src/api.ts";

test("ticket cost requests share an in-flight fetch", async () => {
  const originalFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = (async (input) => {
    calls += 1;
    if (calls === 1) assert.match(String(input), /\/api\/costs\?ticket=WIKI-178/);
    return {
      ok: true,
      json: async () => ({
        updated_at: null,
        totals: {
          label: "all",
          input: 1,
          cache_read: 0,
          cache_write: 0,
          cached: 0,
          output: 1,
          reasoning: 0,
          total_tokens: 2,
          cost_usd: 0.001,
          unpriced_tokens: 0,
          pricing: "priced",
          models: ["gpt-5.6-sol"],
        },
        top: { worker: [], ticket: [], orchestrator: [], day: [] },
        prompt_size_distribution: [],
        velocity: { tokens_per_minute: 1, window_seconds: 60, tokens: 2 },
        runs_scanned: 1,
        refreshing: false,
      }),
    } as Response;
  }) as typeof fetch;
  try {
    const [first, second] = await Promise.all([
      getTicketCosts("WIKI-178"),
      getTicketCosts("WIKI-178"),
    ]);
    assert.equal(calls, 1);
    assert.equal(first, second);
    assert.equal(await getTicketCosts("WIKI-178"), first);
    assert.equal(calls, 1);

    for (let index = 0; index < 128; index += 1) {
      await getTicketCosts(`WIKI-${index}`);
    }
    await getTicketCosts("WIKI-178");
    assert.equal(calls, 130);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
