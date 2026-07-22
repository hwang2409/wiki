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

  const spawnPayloads = [];
  const archivePayloads = [];
  const provisionPayloads = [];
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

    // Composer now derives orch from the caller's session id header (H1). It
    // fetches /api/agents to look up the current orch's provider_session_id
    // and passes it as X-Wiki-Session-Id on the provision request.
    await page.route("**/api/agents", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          workers: [],
          orchestrators: [
            {
              id: TICKET,
              window: null,
              window_alive: true,
              provider_session_id: "orch-session-fake",
              cwd: "/fake/repo",
              kind: "cc",
              model: "claude-opus-4-7",
              effort: null,
              spawned_at: null,
              transcript_exists: true,
            },
          ],
          archived: [],
        }),
      });
    });

    await page.route("**/api/composer/provision-worktree", async (route) => {
      const request = route.request();
      const body = request.postDataJSON();
      provisionPayloads.push({
        body,
        sessionHeader: request.headers()["x-wiki-session-id"] ?? null,
      });
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          workdir: `/fake/repo/.claude/worktrees/${body.ticket.toLowerCase()}`,
          provisioned: false,
        }),
      });
    });

    await page.route("**/api/agents/spawn", async (route) => {
      spawnPayloads.push(route.request().postDataJSON());
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

    await page.route("**/api/agents/WIKI-149/archive", async (route) => {
      archivePayloads.push({
        method: route.request().method(),
        body: route.request().postData(),
      });
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          run_id: "mock",
          agent_id: "WIKI-149",
          state: "archived",
        }),
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
    // combobox ARIA — expanded, controls, activedescendant
    const ariaExpanded = await composer.getAttribute("aria-expanded");
    if (ariaExpanded !== "true") {
      throw new Error(`aria-expanded should be "true" when menu open, got ${ariaExpanded}`);
    }
    const ariaControls = await composer.getAttribute("aria-controls");
    if (ariaControls !== "composer-slash-menu") {
      throw new Error(`aria-controls wrong: ${ariaControls}`);
    }
    const activeInitial = await composer.getAttribute("aria-activedescendant");
    if (activeInitial !== "composer-slash-menu-option-0") {
      throw new Error(`aria-activedescendant initial wrong: ${activeInitial}`);
    }
    await composer.press("ArrowDown");
    const activeAfter = await composer.getAttribute("aria-activedescendant");
    if (activeAfter !== "composer-slash-menu-option-1") {
      throw new Error(`aria-activedescendant after arrow wrong: ${activeAfter}`);
    }
    await composer.press("ArrowUp");
    await page.screenshot({ path: SCREENSHOTS.menu, fullPage: false });

    // fuzzy filter narrows: /spa → spawn is first
    await composer.pressSequentially("spa", { delay: 15 });
    const spawnItem = menu.locator(".composer-slash-item").first();
    const firstName = await spawnItem.locator(".composer-slash-name").textContent();
    if (firstName?.trim() !== "/spawn") {
      throw new Error(`filter failed — first item is "${firstName}" (expected "/spawn")`);
    }

    // Enter opens command form
    await composer.press("Enter");
    const form = page.locator(".composer-command-form");
    await form.waitFor();
    if (await page.locator(".session-input-wrap textarea").count()) {
      throw new Error("textarea should be replaced when command is active");
    }
    await page.screenshot({ path: SCREENSHOTS.form, fullPage: false });

    const submit = form.locator(".composer-command-submit");
    if (!(await submit.isDisabled())) {
      throw new Error("submit should be disabled with empty required args");
    }

    // First spawn: cdx worker — assert effort default high
    await form.getByLabel("ticket").fill("WIKI-149");
    await form.getByLabel("kind").selectOption("cdx");
    await form.getByLabel("model").fill("gpt-5.6-luna");
    await form.getByLabel("goal").fill("Verify cdx worker payload carries effort.");
    // leave effort blank — should default to high

    if (await submit.isDisabled()) {
      throw new Error("submit should be enabled once required args are filled");
    }
    await submit.click();
    await page.waitForFunction(() => document.querySelector(".composer-command-form") === null);
    if (provisionPayloads.length !== 1 || provisionPayloads[0].body.ticket !== "WIKI-149") {
      throw new Error(`provision not called: ${JSON.stringify(provisionPayloads)}`);
    }
    if (provisionPayloads[0].sessionHeader !== "orch-session-fake") {
      throw new Error(
        `X-Wiki-Session-Id header missing/wrong: ${JSON.stringify(provisionPayloads[0])}`
      );
    }
    if (spawnPayloads.length !== 1) {
      throw new Error(`spawn should fire once, got ${spawnPayloads.length}`);
    }
    const cdxPayload = spawnPayloads[0];
    if (cdxPayload.kind !== "cdx" || cdxPayload.effort !== "high") {
      throw new Error(`cdx effort default missing: ${JSON.stringify(cdxPayload)}`);
    }
    if (cdxPayload.workdir !== "/fake/repo/.claude/worktrees/wiki-149") {
      throw new Error(`workdir not from provision: ${JSON.stringify(cdxPayload)}`);
    }
    if (cdxPayload.orch !== TICKET) {
      throw new Error(`orch not threaded: ${JSON.stringify(cdxPayload)}`);
    }

    // Second spawn: cc worker — effort must be null
    await composer.focus();
    await composer.press("/");
    await composer.pressSequentially("spawn", { delay: 10 });
    await composer.press("Enter");
    const form2 = page.locator(".composer-command-form");
    await form2.waitFor();
    await form2.getByLabel("ticket").fill("WIKI-149");
    await form2.getByLabel("model").fill("claude-opus-4-7");
    await form2.getByLabel("goal").fill("cc worker.");
    await form2.locator(".composer-command-submit").click();
    await page.waitForFunction(() => document.querySelector(".composer-command-form") === null);
    if (spawnPayloads.length !== 2) {
      throw new Error(`expected second spawn, got ${spawnPayloads.length}`);
    }
    const ccPayload = spawnPayloads[1];
    if (ccPayload.kind !== "cc" || ccPayload.effort !== null) {
      throw new Error(`cc payload wrong: ${JSON.stringify(ccPayload)}`);
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

    // archive — outcome flows through to backend
    await composer.focus();
    await composer.press("/");
    await composer.pressSequentially("archive", { delay: 10 });
    await composer.press("Enter");
    const archiveForm = page.locator(".composer-command-form");
    await archiveForm.waitFor();
    await archiveForm.getByLabel("agent-id").fill("WIKI-149");
    await archiveForm.getByLabel("outcome").selectOption("closed");
    await archiveForm.locator(".composer-command-submit").click();
    await page.waitForFunction(() => document.querySelector(".composer-command-form") === null);
    if (archivePayloads.length !== 1) {
      throw new Error(`expected 1 archive call, got ${archivePayloads.length}`);
    }
    let parsedArchive = null;
    try {
      parsedArchive = archivePayloads[0].body ? JSON.parse(archivePayloads[0].body) : null;
    } catch {
      parsedArchive = null;
    }
    if (!parsedArchive || parsedArchive.outcome !== "closed") {
      throw new Error(`archive payload missing outcome: ${JSON.stringify(archivePayloads[0])}`);
    }

    // cancel preserves populated args in the raw fallback
    await composer.focus();
    await composer.press("/");
    await composer.pressSequentially("archive", { delay: 10 });
    await composer.press("Enter");
    const cancelForm = page.locator(".composer-command-form");
    await cancelForm.waitFor();
    await cancelForm.getByLabel("agent-id").fill("WIKI-149");
    await cancelForm.getByLabel("outcome").selectOption("merged");
    await page.locator(".composer-command-cancel").click();
    await page.waitForFunction(() => document.querySelector(".composer-command-form") === null);
    const restored = await composer.inputValue();
    if (!restored.startsWith("\\/archive")) {
      throw new Error(`cancel should preserve \\/archive prefix, got "${restored}"`);
    }
    if (!restored.includes("WIKI-149") || !restored.includes("merged")) {
      throw new Error(`cancel should preserve populated args, got "${restored}"`);
    }
    if (await page.locator(".composer-slash-menu").count()) {
      throw new Error("backslash escape should suppress slash menu");
    }

    // outside-click dismisses menu without discarding raw text
    await composer.focus();
    // clear whatever is in the composer, type fresh /steer prefix
    await composer.press("Meta+A");
    await composer.press("Backspace");
    await composer.press("/");
    await composer.pressSequentially("ste", { delay: 15 });
    await page.locator(".composer-slash-menu").waitFor();
    const rawBefore = await composer.inputValue();
    await page.locator(".session-scroll").click({ position: { x: 5, y: 5 } });
    await delay(200);
    if (await page.locator(".composer-slash-menu").count()) {
      throw new Error("outside pointer-down should close menu");
    }
    const rawAfter = await composer.inputValue();
    if (rawAfter !== rawBefore) {
      throw new Error(`outside click must preserve raw text, got "${rawAfter}" (was "${rawBefore}")`);
    }
  } finally {
    await page.close();
    await browser.close();
    await backend.stop();
  }
}

await main();
