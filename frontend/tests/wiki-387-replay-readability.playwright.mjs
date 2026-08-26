import fs from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";

import {
  codexAssistant,
  makeFixtureRoot,
  startBackend,
  writeQueue,
  writeRegistry,
} from "../scripts/wiki32-harness.mjs";

const TICKET = "WIKI-387";
const RUN_ID = "fixture-wiki-387-run";
const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || path.resolve("../docs/pr-screenshots/wiki-387");

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function waitForProviderBlocks(page, rootSelector, eventSeq, expected) {
  await page.waitForFunction(
    ({ rootSelector: selector, eventSeq: seq, expectedBlocks }) => {
      const root = document.querySelector(selector);
      if (!root) return false;
      const blocks = Array.from(root.querySelectorAll(`[data-provider-event-seq='${seq}']`)).map((node) => {
        const type = node.getAttribute("data-provider-block-type");
        const ok = node.getAttribute("data-tool-ok");
        return ok === null ? type : `${type}:${ok}`;
      });
      return JSON.stringify(blocks) === JSON.stringify(expectedBlocks);
    },
    { rootSelector, eventSeq, expectedBlocks: expected },
  );
}

function runSummary() {
  return {
    run_id: RUN_ID,
    agent_id: TICKET,
    orch_id: "wiki",
    role: "implement",
    provider: "codex",
    model: "gpt-5.6",
    outcome: "working",
    state: "working",
    created_at: "2026-08-25T12:00:00Z",
    updated_at: "2026-08-25T12:01:00Z",
    total_events: 8,
    initial_prompt_excerpt: "Review replay readability",
  };
}

const providerFixtures = [
  {
    kind: "item_completed",
    summary: "completed mcp tool",
    payload: { method: "item/completed", params: { item: {
      type: "mcpToolCall", server: "filesystem", tool: "read_file", status: "completed",
      result: { isError: false },
    } } },
  },
  {
    kind: "item_completed",
    summary: "completed web search",
    payload: { method: "item/completed", params: { item: {
      type: "webSearch", query: "wiki", status: "completed",
    } } },
  },
  {
    kind: "item_completed",
    summary: "completed dynamic tool",
    payload: { method: "item/completed", params: { item: {
      type: "dynamicToolCall", tool: "lookup", status: "completed",
    } } },
  },
  {
    kind: "item_completed",
    summary: "completed collaboration tool",
    payload: { method: "item/completed", params: { item: {
      type: "collabAgentToolCall", action: "review", status: "completed",
    } } },
  },
  {
    kind: "claude_assistant",
    summary: "mixed Claude text and tool",
    payload: { message: { role: "assistant", content: [
      { type: "text", text: "I will inspect the file." },
      { type: "tool_use", name: "Read", input: { file_path: "README.md" } },
      { type: "text", text: "Then I will report back." },
    ] } },
  },
  {
    kind: "claude_user",
    summary: "Claude result with nested error",
    payload: { message: { role: "user", content: [{
      type: "tool_result", content: [{ type: "text", text: "permission denied" }],
      result: { isError: true },
    }] } },
  },
  {
    kind: "future_kind",
    disposition: "future-disposition",
    lifecycle_state: "future-lifecycle",
    summary: "mystery provider event",
    payload: { params: { item: { type: "futureProviderItem" } } },
  },
  {
    kind: "item_completed",
    summary: "thinking",
    payload: { method: "item/completed", params: { item: {
      type: "reasoning", summary: [{ text: "check the evidence" }],
    } } },
  },
].map((fixture, index) => ({
  seq: index + 1,
  raw_seq: index + 1,
  ts: `2026-08-25T12:00:0${index + 1}Z`,
  disposition: fixture.disposition ?? "rendered",
  lifecycle_state: null,
  bookmark: null,
  ...fixture,
}));

const events = providerFixtures;
const providerEvents = providerFixtures.map((event) => ({
  seq: event.seq,
  raw_seq: event.raw_seq,
  normalized_at: event.ts,
  disposition: event.disposition === "unknown" ? "unknown" : "rendered",
  kind: event.kind,
  lifecycle_state: event.lifecycle_state,
  payload: event.payload,
}));
const rawEvents = Object.fromEntries(providerFixtures.map((event) => [event.seq, {
  seq: event.seq,
  kind: event.kind,
  payload: event.payload,
}]));

async function main() {
  await fs.mkdir(OUT_DIR, { recursive: true });
  const fixtures = makeFixtureRoot("wiki-387-replay-readability-");
  const transcript = path.join(fixtures.root, "transcript.jsonl");
  await fs.writeFile(transcript, `${JSON.stringify(codexAssistant("Replay readability fixture."))}\n`);
  writeRegistry(fixtures.registryPath, [[TICKET, transcript]]);
  writeQueue(fixtures.queuePath, TICKET, []);

  let backend;
  let browser;
  try {
    backend = await startBackend(fixtures);
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: 1440, height: 940 } });
    page.setDefaultTimeout(8_000);
    await page.addInitScript(({ ticket }) => {
      localStorage.setItem("wiki-sidebar-visible", "false");
      localStorage.setItem(
        "wiki-window-layout-v2",
        JSON.stringify({
          version: 2,
          activeWindowId: "window-0",
          windows: [{
            id: "window-0",
            focusedPaneId: "pane-1",
            layout: { kind: "pane", id: "pane-1", path: `agent://${ticket}` },
          }],
        }),
      );
    }, { ticket: TICKET });

    await page.route(`**/api/agents/${TICKET}/replay/runs`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ ticket: TICKET, runs: [runSummary()], runs_truncated: false }),
      });
    });
    await page.route(`**/api/agent-runs/${RUN_ID}/replay/timeline?*`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          run: runSummary(),
          events,
          next_cursor: null,
          has_more: false,
          bookmarks: [],
          bookmarks_truncated: false,
          warnings: [],
        }),
      });
    });
    await page.route(`**/api/agent-runs/${RUN_ID}/replay/events/*`, async (route) => {
      const seq = Number(route.request().url().split("/").at(-1));
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ run_id: RUN_ID, seq, raw: rawEvents[seq] }),
      });
    });
    await page.route(`**/api/agents/${TICKET}/session?*`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          version: 2,
          format: "provider-events",
          path: `provider://${RUN_ID}`,
          tokens: null,
          tasks: [],
          pr: null,
          session_meta: {},
          dispositions: { rendered: 7, summarized: 0, ignored: 0, unknown: 1 },
          base: 0,
          cursor: 0,
          tail_from: 0,
          events: [],
          patches: [],
          has_older: false,
          working: false,
          model: "gpt-5.6",
          desired_model: null,
          kind: "cdx",
          provider: "codex",
          provider_inspector: {
            run_id: RUN_ID,
            provider: "codex",
            state: "idle",
            raw_count: providerEvents.length,
            normalized_count: providerEvents.length,
            dispositions: { rendered: 7, summarized: 0, ignored: 0, unknown: 1 },
            pending_requests: [],
            events: providerEvents,
          },
          queue: [],
          subagents: [],
        }),
      });
    });

    await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
    const surface = page.locator(".agent-session-surface").first();
    await surface.waitFor();
    const transcript = page.locator("[data-testid='session-provider-event-rows']");
    await transcript.waitFor();
    const expected = [
      ["tool:true"],
      ["tool:true"],
      ["tool:true"],
      ["tool:true"],
      ["message", "tool:null", "message"],
      ["tool:false"],
      ["marker"],
      ["thinking"],
    ];
    let unknownStatus = "";
    for (let index = 1; index <= expected.length; index += 1) {
      await waitForProviderBlocks(page, "[data-testid='session-provider-event-rows']", index, expected[index - 1]);
    }
    await surface.getByRole("button", { name: "Replay" }).click();
    const replay = page.locator(".session-side-panel-replay .replay-panel");
    await replay.waitFor();

    for (let index = 1; index <= expected.length; index += 1) {
      await waitForProviderBlocks(page, ".session-side-panel-replay .replay-panel", index, expected[index - 1]);
      if (index === 7) unknownStatus = await replay.locator(".replay-event-status").innerText();
      if (index < expected.length) {
        await replay.getByRole("button", { name: "Next event" }).click();
      }
    }

    const raw = replay.locator(".replay-event-raw");
    assert(!(await raw.isVisible()), "raw JSON must stay hidden while details are closed");
    assert(unknownStatus.includes("future-disposition"), "unknown disposition must fall back to its raw value");
    assert(unknownStatus.includes("future-lifecycle"), "unknown lifecycle must fall back to its raw value");
    await page.screenshot({ path: path.join(OUT_DIR, "wiki-387-before.png"), fullPage: false });

    await replay.getByText("Event details", { exact: true }).click();
    await raw.waitFor({ state: "visible" });
    assert((await raw.innerText()).includes("check the evidence"), "details must contain the full raw JSON");
    assert((await replay.getByRole("button", { name: "copy JSON" }).count()) === 1, "details must expose a copy control");
    await page.screenshot({ path: path.join(OUT_DIR, "wiki-387-after.png"), fullPage: false });

    const kindLabel = replay.locator(".replay-frame-kind").first();
    await kindLabel.waitFor();
    assert((await kindLabel.innerText()) === "item completed", "provider event kind must remain visible");
    console.log(`WIKI-387 replay readability: PASS (screenshots in ${OUT_DIR})`);
  } finally {
    if (browser) await browser.close();
    if (backend) await backend.stop();
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
