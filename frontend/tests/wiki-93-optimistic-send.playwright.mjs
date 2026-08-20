import fs from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";

import {
  codexUser,
  makeFixtureRoot,
  startBackend,
  writeQueue,
  writeRegistry,
} from "../scripts/wiki32-harness.mjs";

const TICKET = "WIKI-93";
const SCREENSHOTS = {
  sending: "/tmp/wiki-93-pending-sending.png",
  reconcileBefore: "/tmp/wiki-93-reconcile-before.png",
  reconcileAfter: "/tmp/wiki-93-reconcile-after.png",
  autoQueued: "/tmp/wiki-93-auto-queued-tooltip.png",
  failed: "/tmp/wiki-93-failed-send.png",
};

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function appendUserMessage(transcript, text) {
  const timestamp = new Date().toISOString();
  await fs.appendFile(
    transcript,
    `${JSON.stringify(codexUser(text, timestamp))}\n`,
  );
  return timestamp;
}

async function main() {
  const fixtures = makeFixtureRoot("wiki-93-optimistic-send-");
  const transcript = path.join(fixtures.root, "codex-empty.jsonl");
  await fs.writeFile(
    "/tmp/wiki-93-fixtures.json",
    JSON.stringify({ ...fixtures, transcript, screenshots: SCREENSHOTS }, null, 2),
  );
  await fs.writeFile(
    transcript,
    `${JSON.stringify({
      type: "event_msg",
      timestamp: new Date().toISOString(),
      payload: { type: "thread_settings_applied" },
    })}\n`,
  );
  // writeRegistry pins every isolated control target to the inert @9999 tmux id.
  writeRegistry(fixtures.registryPath, [[TICKET, transcript]]);
  writeQueue(fixtures.queuePath, TICKET, []);

  const attempts = new Map();
  const sessionEvents = [];
  let sessionRequests = 0;
  let sessionQueue = [];
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
          working: true,
        }),
      });
    });

    await page.route(`**/api/agents/${TICKET}/message`, async (route) => {
      const body = route.request().postDataJSON();
      const attempt = (attempts.get(body.text) ?? 0) + 1;
      attempts.set(body.text, attempt);

      if (body.text === "optimistic hello") {
        await delay(5_000);
        const ts = await appendUserMessage(transcript, body.text);
        sessionEvents.push({
          id: sessionEvents.length,
          kind: "user",
          ts,
          text: body.text,
          disposition: "rendered",
        });
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ status: "sent" }),
        });
        return;
      }

      if (body.text === "queue this now") {
        sessionQueue = [{ text: body.text, queued_at: new Date().toISOString() }];
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            status: "queued",
            position: 1,
            messages: sessionQueue,
          }),
        });
        return;
      }

      if (body.text === "retry me" && attempt === 1) {
        await route.fulfill({
          status: 400,
          contentType: "application/json",
          body: JSON.stringify({ detail: "fixture send failure" }),
        });
        return;
      }

      const ts = await appendUserMessage(transcript, body.text);
      sessionEvents.push({
        id: sessionEvents.length,
        kind: "user",
        ts,
        text: body.text,
        disposition: "rendered",
      });
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ status: "sent" }),
      });
    });

    await page.addInitScript(({ ticket }) => {
      localStorage.setItem("wiki-sidebar-visible", "false");
      localStorage.setItem(
        "wiki-window-layout-v2",
        JSON.stringify({
          version: 2,
          activeWindowId: "window-0",
          windows: [
            {
              id: "window-0",
              focusedPaneId: "pane-1",
              layout: { kind: "pane", id: "pane-1", path: `agent://${ticket}` },
            },
          ],
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

    await composer.fill("optimistic hello");
    await composer.press("Enter");
    const sending = page.locator(".session-pending-user", { hasText: "optimistic hello" });
    await sending.waitFor({ state: "visible" });
    await sending.getByText("sending…").waitFor({ state: "visible" });
    if (await composer.inputValue() !== "") throw new Error("composer did not clear optimistically");
    await page.screenshot({ path: SCREENSHOTS.sending, fullPage: true });
    await page.screenshot({ path: SCREENSHOTS.reconcileBefore, fullPage: true });

    const reconciled = page.locator(".session-user:not(.session-pending-user)", { hasText: "optimistic hello" });
    try {
      await reconciled.waitFor({ state: "visible", timeout: 10_000 });
    } catch (error) {
      throw new Error(`real user row never arrived after ${sessionRequests} session polls`, { cause: error });
    }
    await sending.waitFor({ state: "detached", timeout: 2_000 });
    if (await reconciled.count() !== 1) throw new Error("optimistic user row was duplicated after reconciliation");
    await page.screenshot({ path: SCREENSHOTS.reconcileAfter, fullPage: true });

    await composer.fill("queue this now");
    await composer.press("Enter");
    const queued = page.locator(".session-queued.is-auto", { hasText: "queue this now" });
    await queued.waitFor({ state: "visible" });
    await queued.hover();
    await queued.getByRole("tooltip").waitFor({ state: "visible" });
    await page.screenshot({ path: SCREENSHOTS.autoQueued, fullPage: true });
    await page.mouse.move(20, 20);

    await composer.fill("retry me");
    await composer.press("Enter");
    const failed = page.locator(".session-pending-user.is-failed", { hasText: "retry me" });
    await failed.waitFor({ state: "visible" });
    await failed.getByRole("button", { name: "Retry send" }).waitFor({ state: "visible" });
    await failed.getByRole("button", { name: "Edit message" }).waitFor({ state: "visible" });
    await page.screenshot({ path: SCREENSHOTS.failed, fullPage: true });

    await failed.getByRole("button", { name: "Retry send" }).click();
    await failed.waitFor({ state: "detached", timeout: 10_000 });
    await page.locator(".session-user:not(.session-pending-user)", { hasText: "retry me" }).waitFor({ state: "visible" });

  } finally {
    await page.close();
    await browser.close();
    await backend.stop();
  }
}

await main();
