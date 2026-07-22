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
const TICKET = "WIKI-148";
const SCREENSHOTS = {
  menu: path.join(ROOT, "docs/pr-screenshots/wiki-148-slash-menu.png"),
  form: path.join(ROOT, "docs/pr-screenshots/wiki-148-command-form.png"),
};

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function ensureScreenshotDir() {
  await fs.mkdir(path.dirname(SCREENSHOTS.menu), { recursive: true });
}

async function main() {
  await ensureScreenshotDir();
  const fixtures = makeFixtureRoot("wiki-148-slash-menu-");
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

  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });

  let spawnPayload = null;
  let messagePayload = null;
  let gatePayload = null;
  let messageCalls = 0;

  try {
    await page.route(`**/api/agents/${TICKET}/session?**`, async (route) => {
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
          dispositions: { rendered: 0, summarized: 0, ignored: 1, unknown: 0 },
          base: 0,
          cursor: 0,
          tail_from: 0,
          events: [],
          patches: [],
          subagents: [],
          queue: [],
          composer_messages: [],
          working: true,
        }),
      });
    });

    await page.route("**/api/agents/spawn", async (route) => {
      spawnPayload = route.request().postDataJSON();
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          window: "@spawn-mock",
          run_id: "mock-run",
          log: null,
          prompt_path: null,
        }),
      });
    });

    await page.route("**/api/agents/WIKI-149/message", async (route) => {
      messageCalls += 1;
      messagePayload = route.request().postDataJSON();
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ status: "sent", messages: [] }),
      });
    });

    await page.route("**/api/composer/gate", async (route) => {
      gatePayload = route.request().postDataJSON();
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          verdict: "pass",
          summary: "ready",
          raw: { ready: true, reasons: [] },
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
    await page.waitForSelector(".session-scroll");
    const composer = page.locator(".session-input-wrap textarea");
    await composer.waitFor({ state: "visible" });
    await composer.focus();
    await delay(100);

    // trigger menu
    await composer.press("/");
    const menu = page.locator(".composer-slash-menu");
    await menu.waitFor();
    const items = menu.locator(".composer-slash-item");
    if (await items.count() !== 5) {
      throw new Error(`expected 5 commands, got ${await items.count()}`);
    }
    await page.screenshot({ path: SCREENSHOTS.menu, fullPage: false });

    // filter narrows: /spa → spawn is first
    await composer.pressSequentially("spa", { delay: 15 });
    const spawnItem = menu.locator(".composer-slash-item").first();
    const firstName = await spawnItem.locator(".composer-slash-name").textContent();
    if (firstName?.trim() !== "/spawn") {
      throw new Error(`filter failed — first item is "${firstName}" (expected "/spawn")`);
    }

    // Enter inserts chip / opens command form
    await composer.press("Enter");
    const form = page.locator(".composer-command-form");
    await form.waitFor();
    if (await page.locator(".session-input-wrap textarea").count()) {
      throw new Error("textarea should be replaced when command is active");
    }
    await page.screenshot({ path: SCREENSHOTS.form, fullPage: false });

    // submit disabled until required args filled
    const submit = form.locator(".composer-command-submit");
    if (!(await submit.isDisabled())) {
      throw new Error("submit should be disabled with empty required args");
    }

    // fill args
    await form.getByLabel("ticket").fill("WIKI-149");
    // kind + role already default to cc / implement
    await form.getByLabel("model").fill("claude-opus-4-7");
    await form.getByLabel("goal").fill("Verify slash menu wiring works end-to-end.");

    if (await submit.isDisabled()) {
      throw new Error("submit should be enabled once required args are filled");
    }
    await submit.click();
    await page.waitForFunction(() => document.querySelector(".composer-command-form") === null);
    if (!spawnPayload) throw new Error("spawn call did not fire");
    if (spawnPayload.ticket !== "WIKI-149" || spawnPayload.kind !== "cc" || spawnPayload.role !== "implement") {
      throw new Error(`spawn payload wrong: ${JSON.stringify(spawnPayload)}`);
    }
    if (spawnPayload.orch !== TICKET) {
      throw new Error(`orch not threaded: ${JSON.stringify(spawnPayload)}`);
    }
    if (!spawnPayload.prompt?.includes("Verify slash menu")) {
      throw new Error(`prompt not sent: ${JSON.stringify(spawnPayload)}`);
    }

    // steer command dispatches sendAgentMessage
    await composer.focus();
    await composer.press("/");
    await composer.pressSequentially("steer", { delay: 10 });
    await page.locator(".composer-slash-menu").waitFor();
    await composer.press("Enter");
    const steerForm = page.locator(".composer-command-form");
    await steerForm.waitFor();
    await steerForm.getByLabel("agent-id").fill("WIKI-149");
    await steerForm.getByLabel("message").fill("hurry up");
    await steerForm.locator(".composer-command-submit").click();
    await page.waitForFunction(() => document.querySelector(".composer-command-form") === null);
    if (messageCalls !== 1) throw new Error(`expected 1 steer send, got ${messageCalls}`);
    if (messagePayload?.text !== "hurry up" || messagePayload?.mode !== "now") {
      throw new Error(`steer payload wrong: ${JSON.stringify(messagePayload)}`);
    }

    // gate command dispatches gate endpoint
    await composer.focus();
    await composer.press("/");
    await composer.pressSequentially("gate", { delay: 10 });
    await page.locator(".composer-slash-menu").waitFor();
    await composer.press("Enter");
    const gateForm = page.locator(".composer-command-form");
    await gateForm.waitFor();
    await gateForm.getByLabel("pr").fill("https://github.com/hwang2409/wiki/pull/999");
    await gateForm.locator(".composer-command-submit").click();
    await page.waitForFunction(() => document.querySelector(".composer-command-form") === null);
    if (!gatePayload || gatePayload.pr !== "https://github.com/hwang2409/wiki/pull/999") {
      throw new Error(`gate payload wrong: ${JSON.stringify(gatePayload)}`);
    }

    // escape cancels form and preserves \/name for raw send
    await composer.focus();
    await composer.press("/");
    await composer.pressSequentially("archive", { delay: 10 });
    await composer.press("Enter");
    await page.locator(".composer-command-form").waitFor();
    await page.locator(".composer-command-cancel").click();
    await page.waitForFunction(() => document.querySelector(".composer-command-form") === null);
    const restored = await composer.inputValue();
    if (!restored.startsWith("\\/archive")) {
      throw new Error(`esc cancel should preserve \\/archive prefix, got "${restored}"`);
    }
    // menu should NOT appear again for \/archive
    if (await page.locator(".composer-slash-menu").count()) {
      throw new Error("backslash escape should suppress slash menu");
    }
  } finally {
    await page.close();
    await browser.close();
    await backend.stop();
  }
}

await main();
