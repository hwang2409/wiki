import fs from "node:fs/promises";
import { chromium } from "playwright";

import {
  makeFixtureRoot,
  startBackend,
  writeQueue,
  writeRegistry,
} from "../scripts/wiki32-harness.mjs";

const TICKET = "WIKI-373-ZERO-STATE";

async function main() {
  const fixtures = makeFixtureRoot("wiki-373-zero-state-");
  const transcript = fixtures.root + "/codex-init-only.jsonl";
  await fs.writeFile(
    transcript,
    JSON.stringify({
      type: "event_msg",
      timestamp: new Date().toISOString(),
      payload: { type: "thread_settings_applied" },
    }) + "\n",
  );
  writeRegistry(fixtures.registryPath, [[TICKET, transcript]]);
  writeQueue(fixtures.queuePath, TICKET, []);

  const sessionEvents = [{
    id: 0,
    kind: "claude_init",
    ts: new Date().toISOString(),
    text: "",
    disposition: "rendered",
    claude_init: { model: "claude-sonnet", cwd: "/tmp/wiki" },
  }];
  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });

  try {
    await page.route(`**/api/agents/${TICKET}/session?**`, async (route) => {
      const cursor = Number(new URL(route.request().url()).searchParams.get("cursor") ?? 0);
      const tailFrom = cursor >= 0 && cursor <= sessionEvents.length ? cursor : 0;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          version: 2,
          format: "claude",
          path: transcript,
          tokens: null,
          model: "claude-sonnet",
          kind: "worker",
          provider: "claude",
          tasks: [],
          pr: null,
          session_meta: {},
          dispositions: { rendered: sessionEvents.length, summarized: 0, ignored: 0, unknown: 0 },
          base: 0,
          cursor: sessionEvents.length,
          tail_from: tailFrom,
          events: sessionEvents.slice(tailFrom),
          patches: [],
          subagents: [],
          queue: [],
          working: true,
        }),
      });
    });
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
    await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
    await page.getByTestId("session-zero-events").waitFor();
    if (await page.locator(".session-virtual-row").count() !== 0) {
      throw new Error("init-only session rendered an eager transcript row");
    }
    if (await page.getByText(/^session started:/).count() !== 0) {
      throw new Error("session started was rendered in the transcript");
    }
  } finally {
    await page.close();
    await browser.close();
    await backend.stop();
  }
}

await main();
