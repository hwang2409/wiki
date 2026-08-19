import fs from "node:fs/promises";

import { chromium } from "playwright";

import {
  makeFixtureRoot,
  startBackend,
  writeQueue,
  writeRegistry,
} from "../scripts/wiki32-harness.mjs";

const TICKET = "WIKI-373";

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function main() {
  const fixtures = makeFixtureRoot("wiki-373-instant-send-");
  const transcript = fixtures.root + "/codex-empty.jsonl";
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
  let sendCount = 0;
  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });

  try {
    await page.route("**/api/agents/" + TICKET + "/session?**", async (route) => {
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

    await page.route("**/api/agents/" + TICKET + "/message", async (route) => {
      sendCount += 1;
      const body = route.request().postDataJSON();
      if (sendCount === 1) {
        await delay(2_000);
        sessionEvents.push({
          id: sessionEvents.length,
          kind: "user",
          ts: new Date().toISOString(),
          text: body.text,
          pending_id: body.pending_id,
          disposition: "rendered",
        });
        await delay(3_000);
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ status: "sent", pending_id: body.pending_id }),
        });
        return;
      }
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: "fixture send failure" }),
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
            layout: { kind: "pane", id: "pane-1", path: "agent://" + ticket },
          }],
        }),
      );
    }, { ticket: TICKET });
    await page.goto(backend.baseUrl + "/#/agent/" + TICKET, { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".session-composer textarea");
    if (await page.getByText(/^session started:/).count() !== 0) {
      throw new Error("session started was rendered in the transcript");
    }

    const composer = page.locator(".session-composer textarea");
    await composer.fill("instant hello");
    await composer.press("Enter");
    const pending = page.locator(".session-scroll .session-pending-user", { hasText: "instant hello" });
    await pending.waitFor({ state: "visible" });
    const pendingRow = pending.locator("..");
    const pendingKey = await pendingRow.getAttribute("data-row-key");

    const authoritative = page.locator(".session-scroll .session-user:not(.session-pending-user)", { hasText: "instant hello" });
    await authoritative.waitFor({ state: "visible", timeout: 8_000 });
    await pending.waitFor({ state: "detached" });
    if (await authoritative.count() !== 1) throw new Error("authoritative message was duplicated");
    if (await authoritative.locator("..").getAttribute("data-row-key") !== pendingKey) {
      throw new Error("authoritative message changed its transcript row position");
    }

    await composer.fill("failed hello");
    await composer.press("Enter");
    const failed = page.locator(".session-scroll .session-pending-user", { hasText: "failed hello" });
    await failed.getByText("send failed: fixture send failure").waitFor({ state: "visible" });
  } finally {
    await page.close();
    await browser.close();
    await backend.stop();
  }
}

await main();
