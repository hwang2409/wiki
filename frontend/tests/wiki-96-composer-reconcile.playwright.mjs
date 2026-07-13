import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

import {
  makeFixtureRoot,
  startBackend,
  writeQueue,
  writeRegistry,
} from "../scripts/wiki32-harness.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const TICKET = "WIKI-96";
const SCREENSHOTS = {
  reconciled: path.join(ROOT, "docs/pr-screenshots/wiki-96-mid-turn-reconciled.png"),
  queuedOnce: path.join(ROOT, "docs/pr-screenshots/wiki-96-queued-once.png"),
};

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function main() {
  const fixtures = makeFixtureRoot("wiki-96-composer-reconcile-");
  const transcript = path.join(fixtures.root, "codex-empty.jsonl");
  await fs.writeFile(
    transcript,
    `${JSON.stringify({
      type: "event_msg",
      timestamp: new Date().toISOString(),
      payload: { type: "thread_settings_applied" },
    })}\n`,
  );
  writeRegistry(fixtures.registryPath, [[TICKET, transcript]]);
  writeQueue(fixtures.queuePath, TICKET, []);

  const sessionEvents = [];
  const composerMessages = [];
  let sessionQueue = [];
  let composerSeq = 0;
  let duplicateSends = 0;
  let sessionRequests = 0;
  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });

  try {
    await page.route(`**/api/agents/${TICKET}/session?**`, async (route) => {
      sessionRequests += 1;
      const cursor = Number(new URL(route.request().url()).searchParams.get("cursor") ?? 0);
      const tailFrom = cursor >= 0 && cursor <= sessionEvents.length ? cursor : 0;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          version: 2,
          format: "codex",
          path: transcript,
          tokens: null,
          model: "gpt-5.4",
          kind: "cdx",
          provider: "codex",
          tasks: [],
          pr: null,
          session_meta: {},
          dispositions: { rendered: sessionEvents.length, summarized: 0, ignored: 1, unknown: 0 },
          base: 0,
          cursor: sessionEvents.length,
          tail_from: tailFrom,
          events: sessionEvents.slice(tailFrom),
          patches: [],
          subagents: [],
          queue: sessionQueue,
          composer_messages: composerMessages,
          working: true,
        }),
      });
    });

    await page.route(`**/api/agents/${TICKET}/message`, async (route) => {
      const body = route.request().postDataJSON();
      if (typeof body.pending_id !== "string") {
        throw new Error("composer did not send pending_id");
      }

      if (body.text === "mid-turn durable id") {
        await delay(600);
        composerMessages.push({
          pending_id: body.pending_id,
          text: body.text,
          sent_at: new Date(Date.now() - 45_000).toISOString(),
          echoed_at: new Date().toISOString(),
          seq: ++composerSeq,
        });
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ status: "sent", pending_id: body.pending_id, messages: [] }),
        });
        return;
      }

      if (body.text === "queued exactly once") {
        sessionQueue = [{
          text: body.text,
          queued_at: new Date().toISOString(),
          pending_id: body.pending_id,
        }];
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            status: "sent",
            pending_id: body.pending_id,
            position: 1,
            messages: sessionQueue,
          }),
        });
        return;
      }

      if (body.text === "durable hook dedupe") {
        await delay(600);
        const echoedAt = new Date().toISOString();
        composerMessages.push({
          pending_id: body.pending_id,
          text: body.text,
          sent_at: new Date(Date.now() - 1_000).toISOString(),
          echoed_at: echoedAt,
          seq: ++composerSeq,
        });
        sessionEvents.push({
          id: sessionEvents.length,
          kind: "user",
          ts: echoedAt,
          text: `${body.text}\n<system-reminder>hook context</system-reminder>`,
          disposition: "rendered",
        });
      }

      if (body.text === "delayed fallback") {
        await delay(600);
        sessionEvents.push({
          id: sessionEvents.length,
          kind: "user",
          ts: new Date(Date.now() + 45_000).toISOString(),
          text: body.text,
          disposition: "rendered",
        });
      } else if (body.text === "hook fallback") {
        await delay(600);
        sessionEvents.push({
          id: sessionEvents.length,
          kind: "user",
          ts: new Date().toISOString(),
          text: `${body.text}\n<system-reminder>hook context</system-reminder>`,
          disposition: "rendered",
        });
      } else if (body.text === "identical text") {
        duplicateSends += 1;
        if (duplicateSends === 2) {
          await delay(600);
          sessionEvents.push({
            id: sessionEvents.length,
            kind: "user",
            ts: new Date().toISOString(),
            text: body.text,
            disposition: "rendered",
          });
        }
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ status: "sent", pending_id: body.pending_id, messages: sessionQueue }),
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
    await page.waitForSelector(".session-scroll");
    const composer = page.locator(".session-composer textarea");
    await composer.waitFor({ state: "visible" });
    await page.bringToFront();
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    const visibility = await page.locator(".session-tab").evaluate((node) => ({
      document: document.visibilityState,
      rect: node.getBoundingClientRect().toJSON(),
    }));
    if (visibility.document !== "visible" || visibility.rect.width <= 0 || visibility.rect.height <= 0) {
      throw new Error(`session fixture is not visible: ${JSON.stringify(visibility)}`);
    }
    await delay(200);

    await composer.fill("mid-turn durable id");
    await composer.press("Enter");
    const pendingMidTurn = page.locator(".session-pending-user", { hasText: "mid-turn durable id" });
    await pendingMidTurn.waitFor();
    const reconciled = page.locator(".session-user:not(.session-pending-user)", { hasText: "mid-turn durable id" });
    try {
      await reconciled.waitFor({ timeout: 8_000 });
    } catch (error) {
      throw new Error(
        `durable echo missing after ${sessionRequests} session requests; `
        + `composer messages=${JSON.stringify(composerMessages)}; `
        + `pending=${await pendingMidTurn.count()}`,
        { cause: error },
      );
    }
    await pendingMidTurn.waitFor({ state: "detached" });
    if (await reconciled.count() !== 1) throw new Error("durable provider echo rendered more than once");
    await page.locator(".session-tab").screenshot({ path: SCREENSHOTS.reconciled });

    await composer.fill("queued exactly once");
    await composer.press("Enter");
    const queued = page.locator(".session-queued", { hasText: "queued exactly once" });
    await queued.waitFor();
    if (await queued.count() !== 1) throw new Error("queued message rendered more than once");
    if (await page.locator(".session-pending-user", { hasText: "queued exactly once" }).count()) {
      throw new Error("queued message retained its optimistic pending row");
    }
    await page.locator(".session-tab").screenshot({ path: SCREENSHOTS.queuedOnce });

    sessionQueue = [];
    await delay(2_700);

    await composer.fill("durable hook dedupe");
    await composer.press("Enter");
    const durableHookPending = page.locator(".session-pending-user", { hasText: "durable hook dedupe" });
    await durableHookPending.waitFor();
    await durableHookPending.waitFor({ state: "detached", timeout: 8_000 });
    if (await page.locator(".session-user:not(.session-pending-user)", { hasText: "durable hook dedupe" }).count() !== 1) {
      throw new Error("hook-wrapped native echo and durable acknowledgement rendered twice");
    }

    await composer.fill("delayed fallback");
    await composer.press("Enter");
    const delayedPending = page.locator(".session-pending-user", { hasText: "delayed fallback" });
    await delayedPending.waitFor();
    await delayedPending.waitFor({ state: "detached", timeout: 8_000 });
    if (await page.locator(".session-user:not(.session-pending-user)", { hasText: "delayed fallback" }).count() !== 1) {
      throw new Error("delayed fallback did not reconcile to exactly one transcript row");
    }

    await composer.fill("hook fallback");
    await composer.press("Enter");
    const hookPending = page.locator(".session-pending-user", { hasText: "hook fallback" });
    await hookPending.waitFor();
    await hookPending.waitFor({ state: "detached", timeout: 8_000 });

    await composer.fill("identical text");
    await composer.press("Enter");
    await page.locator(".session-pending-user", { hasText: "identical text" })
      .getByText("sent · waiting for transcript")
      .waitFor();
    await composer.fill("identical text");
    await composer.press("Enter");
    const duplicatePending = page.locator(".session-pending-user", { hasText: "identical text" });
    await duplicatePending.first().waitFor();
    await page.locator(".session-user:not(.session-pending-user)", { hasText: "identical text" }).waitFor({ timeout: 8_000 });
    if (await duplicatePending.count() !== 1) {
      throw new Error("one transcript echo did not consume exactly one identical pending row");
    }
  } finally {
    await page.close();
    await browser.close();
    await backend.stop();
  }
}

await main();
