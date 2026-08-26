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
    total_events: 4,
    initial_prompt_excerpt: "Review replay readability",
  };
}

const events = [
  {
    seq: 1,
    raw_seq: 1,
    ts: "2026-08-25T12:00:01Z",
    kind: "item_completed",
    disposition: "future-disposition",
    lifecycle_state: "future-lifecycle",
    summary: "codex item/completed: userMessage — **Replay summary**",
    bookmark: null,
  },
  {
    seq: 2,
    raw_seq: 2,
    ts: "2026-08-25T12:00:02Z",
    kind: "item_completed",
    disposition: "rendered",
    lifecycle_state: null,
    summary: "codex item/completed: commandExecution",
    bookmark: null,
  },
  {
    seq: 3,
    raw_seq: 3,
    ts: "2026-08-25T12:00:03Z",
    kind: "item_completed",
    disposition: "rendered",
    lifecycle_state: "idle",
    summary: "codex item/completed: agentMessage — replay is readable",
    bookmark: "verdict",
  },
  {
    seq: 4,
    raw_seq: 4,
    ts: "2026-08-25T12:00:04Z",
    kind: "unknown_provider_kind",
    disposition: "unknown-disposition",
    lifecycle_state: null,
    summary: "mystery provider event",
    bookmark: null,
  },
];

const rawEvents = {
  1: {
    seq: 1,
    kind: "item_completed",
    payload: {
      params: {
        item: {
          id: "user-1",
          type: "userMessage",
          content: [{ type: "text", text: "# Replay summary\n\nThe **human view** is primary." }],
        },
      },
    },
  },
  2: {
    seq: 2,
    kind: "item_completed",
    payload: {
      params: {
        item: {
          id: "command-1",
          type: "commandExecution",
          command: "cat frontend/src/replay-scrubber-panel.tsx",
          exitCode: 0,
        },
      },
    },
  },
  3: {
    seq: 3,
    kind: "item_completed",
    payload: {
      params: {
        item: {
          id: "agent-1",
          type: "agentMessage",
          text: "Replay is now readable.\n\nThe raw event remains available in details.",
        },
      },
    },
  },
  4: {
    seq: 4,
    kind: "unknown_provider_kind",
    payload: {
      params: { item: { type: "futureProviderItem" } },
    },
  },
};

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

    await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
    const surface = page.locator(".agent-session-surface").first();
    await surface.waitFor();
    await surface.getByRole("button", { name: "Replay" }).click();
    const replay = page.locator(".session-side-panel-replay .replay-panel");
    await replay.waitFor();
    await replay.locator(".replay-event-message-markdown").getByText("Replay summary", { exact: true }).waitFor();

    const raw = replay.locator(".replay-event-raw");
    assert(!(await raw.isVisible()), "raw JSON must stay hidden while details are closed");
    assert((await replay.locator(".replay-event-message-markdown strong").count()) === 1, "message markdown must render in the primary summary");
    assert((await replay.locator(".replay-event-status").innerText()).includes("future-disposition"), "unknown disposition must fall back to its raw value");
    assert((await replay.locator(".replay-event-status").innerText()).includes("future-lifecycle"), "unknown lifecycle must fall back to its raw value");
    await page.screenshot({ path: path.join(OUT_DIR, "wiki-387-before.png"), fullPage: false });

    await replay.getByText("Event details", { exact: true }).click();
    await raw.waitFor({ state: "visible" });
    assert((await raw.innerText()).includes("human view"), "details must contain the full raw JSON");
    assert((await replay.getByRole("button", { name: "copy JSON" }).count()) === 1, "details must expose a copy control");
    await page.screenshot({ path: path.join(OUT_DIR, "wiki-387-after.png"), fullPage: false });

    await replay.getByRole("button", { name: "Next event" }).click();
    await replay.locator(".session-tool-target").getByText("frontend/src/replay-scrubber-panel.tsx", { exact: false }).waitFor();
    assert((await replay.locator(".session-tool").count()) === 1, "tool events must use the transcript tool row");
    await replay.getByRole("button", { name: "Next event" }).click();
    await replay.getByRole("button", { name: "Next event" }).click();
    const kindLabel = replay.locator(".replay-frame-kind").first();
    await kindLabel.waitFor();
    assert((await kindLabel.innerText()) === "unknown_provider_kind", "unknown event kind must remain visible");
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
