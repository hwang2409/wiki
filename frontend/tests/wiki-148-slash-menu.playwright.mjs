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

    // Round 7, Path B: the composer authenticates by attaching an
    // X-Wiki-App-Secret header sourced from the Tauri invoke bridge
    // (`get_wiki_app_secret`). Round 7 removed the previous
    // `window.__WIKI_APP_SECRET__` production fallback (any injected
    // script could poison it), so Playwright now stubs the Tauri IPC
    // bridge itself — `window.__TAURI_INTERNALS__.invoke` returns the
    // fixture secret for the `get_wiki_app_secret` command. The prior
    // per-orch composer-token endpoint is gone — the frontend must send
    // NO X-Wiki-Composer-Token header and MUST send X-Wiki-App-Secret
    // with the fixture value.
    //
    // Real end-to-end verification that the runtime-registered ACL
    // capability actually authorizes the invoke (i.e., that the native
    // shell path works with no injected shim) lives in the Rust
    // integration test at
    // `src-tauri/tests/wiki_app_secret_capability.rs`. Playwright cannot
    // exercise the real Tauri IPC transport from a plain Chromium.

    await page.route("**/api/composer/provision-worktree", async (route) => {
      const request = route.request();
      const body = request.postDataJSON();
      provisionPayloads.push({
        body,
        appSecretHeader: request.headers()["x-wiki-app-secret"] ?? null,
        // Round 6: neither the legacy session-id header nor the
        // round-5 composer-token header must appear on this request.
        legacySessionHeader: request.headers()["x-wiki-session-id"] ?? null,
        legacyTokenHeader: request.headers()["x-wiki-composer-token"] ?? null,
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

    await page.addInitScript(() => {
      // Round 7: composer requests carry X-Wiki-App-Secret sourced from
      // the Tauri invoke bridge in production. Playwright has no Tauri,
      // so stub `window.__TAURI_INTERNALS__` — the presence sentinel the
      // frontend checks for + an `invoke` handler that answers only
      // `get_wiki_app_secret` and rejects anything else. This exercises
      // the same `import("@tauri-apps/api/core")`.invoke() code path the
      // production build takes; the ACL-enforcement half of the security
      // model is covered by the Rust test at
      // `src-tauri/tests/wiki_app_secret_capability.rs`.
      window.__TAURI_INTERNALS__ = {
        transformCallback: (cb) => {
          const id = Math.floor(Math.random() * 1_000_000);
          window[`_${id}`] = cb;
          return id;
        },
        unregisterCallback: (id) => {
          delete window[`_${id}`];
        },
        invoke: async (cmd) => {
          if (cmd !== "get_wiki_app_secret") {
            throw new Error(`playwright IPC stub: unexpected command "${cmd}"`);
          }
          return "playwright-wiki-app-secret-fixture";
        },
      };
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
    if (provisionPayloads[0].appSecretHeader !== "playwright-wiki-app-secret-fixture") {
      throw new Error(
        `X-Wiki-App-Secret header missing/wrong: ${JSON.stringify(provisionPayloads[0])}`
      );
    }
    if (provisionPayloads[0].legacySessionHeader !== null) {
      throw new Error(
        `Legacy X-Wiki-Session-Id header must NOT be sent (H1 spoof surface): ${JSON.stringify(provisionPayloads[0])}`
      );
    }
    if (provisionPayloads[0].legacyTokenHeader !== null) {
      throw new Error(
        `Legacy X-Wiki-Composer-Token header must NOT be sent (round-5 token endpoint gone): ${JSON.stringify(provisionPayloads[0])}`
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

    // steer command dispatches sendAgentMessage — Meta+Enter from the
    // textarea must dispatch EXACTLY ONCE. Round-7 REVIEW [HIGH]
    // (composer-slash-menu.tsx:94): the prior handler had two
    // sequential `if` branches, so Meta+Enter fired both onSubmits
    // before React re-rendered with `commandBusy=true` and the second
    // call sneaked through. This assertion is the review's own repro.
    await composer.focus();
    await composer.press("/");
    await composer.pressSequentially("steer", { delay: 10 });
    await page.locator(".composer-slash-menu").waitFor();
    await composer.press("Enter");
    const steerForm = page.locator(".composer-command-form");
    await steerForm.waitFor();
    await steerForm.getByLabel("agent-id").fill("WIKI-149");
    const messageBox = steerForm.getByLabel("message");
    await messageBox.fill("hurry up");
    await messageBox.focus();
    await messageBox.press("Meta+Enter");
    await page.waitForFunction(() => document.querySelector(".composer-command-form") === null);
    if (messageCalls !== 1) throw new Error(`expected 1 steer send, got ${messageCalls}`);
    if (messagePayload?.text !== "hurry up" || messagePayload?.mode !== "now") {
      throw new Error(`steer payload wrong: ${JSON.stringify(messagePayload)}`);
    }

    // gate command dispatches gate endpoint; success summary must be
    // visible after the form clears. Round-7 REVIEW [MEDIUM]
    // (session.tsx:2768): silent clears violate the success-summary
    // contract.
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
    const gateNotice = page.locator(".composer-command-notice");
    await gateNotice.waitFor({ state: "visible", timeout: 2000 });
    const gateSummary = await gateNotice
      .locator(".composer-command-notice-summary")
      .textContent();
    if (!gateSummary || !gateSummary.toLowerCase().includes("gate")) {
      throw new Error(`gate success notice missing/wrong: "${gateSummary}"`);
    }
    // Notice should be dismissable so it doesn't linger before the next
    // command opens.
    await gateNotice.locator(".composer-command-notice-dismiss").click();
    if (await page.locator(".composer-command-notice").count()) {
      throw new Error("dismiss button should hide notice");
    }

    // archive — outcome flows through to backend AND surfaces as a
    // visible success notice (round-8 REVIEW [MEDIUM]:396 — destructive
    // /archive must have the same success-summary coverage /gate does,
    // else a per-command notice-skip could regress silently).
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
    const archiveNotice = page.locator(".composer-command-notice");
    await archiveNotice.waitFor({ state: "visible", timeout: 2000 });
    const archiveSummary = await archiveNotice
      .locator(".composer-command-notice-summary")
      .textContent();
    if (!archiveSummary || !archiveSummary.includes("archived WIKI-149 (closed)")) {
      throw new Error(
        `archive success notice missing/wrong summary: "${archiveSummary}"`
      );
    }
    const archiveDetail = await archiveNotice
      .locator(".composer-command-notice-detail")
      .textContent();
    if (!archiveDetail || !archiveDetail.toLowerCase().includes("archived")) {
      throw new Error(
        `archive success notice missing/wrong detail: "${archiveDetail}"`
      );
    }
    await archiveNotice.locator(".composer-command-notice-dismiss").click();
    if (await page.locator(".composer-command-notice").count()) {
      throw new Error("archive notice dismiss should hide notice");
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

    // Multiline-draft preservation. Round-7 REVIEW [MEDIUM]
    // (session.tsx:2645): opening `/spawn` from `existing draft\n/sp`
    // used to clear the whole composer; the surrounding draft must
    // survive both cancel and successful dispatch.
    await composer.focus();
    await composer.press("Meta+A");
    await composer.press("Backspace");
    // Prime the composer with a multi-line draft and place the caret
    // immediately after `/sp` so the slash-menu trigger fires (plain
    // Enter in the composer sends the message, so we can't type the
    // newline with a key press).
    await composer.fill("existing draft\n/sp");
    // Nudge input events so the menu recomputes with an alive trigger.
    await composer.press("End");
    await page.locator(".composer-slash-menu").waitFor();
    await composer.press("Enter");
    const draftForm = page.locator(".composer-command-form");
    await draftForm.waitFor();
    // cancel should stitch surrounding draft back in with the serialized command
    await page.locator(".composer-command-cancel").click();
    await page.waitForFunction(() => document.querySelector(".composer-command-form") === null);
    const restoredWithDraft = await composer.inputValue();
    if (!restoredWithDraft.startsWith("existing draft\n")) {
      throw new Error(
        `cancel dropped surrounding draft, got "${restoredWithDraft}"`
      );
    }
    if (!restoredWithDraft.includes("/spawn") && !restoredWithDraft.includes("\\/spawn")) {
      throw new Error(
        `cancel should restore command text after the draft, got "${restoredWithDraft}"`
      );
    }

    // Successful dispatch must also preserve the surrounding draft
    // (drop only the `/foo ...` command portion). Reset first.
    await composer.focus();
    await composer.press("Meta+A");
    await composer.press("Backspace");
    await composer.fill("keep me around\n");
    await composer.press("/");
    await composer.pressSequentially("gate", { delay: 15 });
    await page.locator(".composer-slash-menu").waitFor();
    await composer.press("Enter");
    const gateAgain = page.locator(".composer-command-form");
    await gateAgain.waitFor();
    await gateAgain.getByLabel("pr").fill("https://github.com/hwang2409/wiki/pull/1000");
    await gateAgain.locator(".composer-command-submit").click();
    await page.waitForFunction(() => document.querySelector(".composer-command-form") === null);
    const afterSuccess = await composer.inputValue();
    if (!afterSuccess.startsWith("keep me around")) {
      throw new Error(
        `successful dispatch discarded surrounding draft, got "${afterSuccess}"`
      );
    }
    // clean up notice for later assertions
    const lingering = page.locator(".composer-command-notice-dismiss");
    if (await lingering.count()) await lingering.click();

    // Busy-cancel guard. Round-7 REVIEW [MEDIUM]
    // (composer-slash-menu.tsx:88): cancel must NOT fire while a
    // destructive command is in flight; the button is disabled and
    // Escape becomes a no-op until the request resolves.
    let releaseSteer = () => {};
    const steerGate = new Promise((resolve) => {
      releaseSteer = resolve;
    });
    let steerBusyCalls = 0;
    await page.route("**/api/agents/WIKI-150/message", async (route) => {
      steerBusyCalls += 1;
      await steerGate;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ status: "sent", messages: [] }),
      });
    });
    await composer.focus();
    await composer.press("Meta+A");
    await composer.press("Backspace");
    await composer.press("/");
    await composer.pressSequentially("steer", { delay: 10 });
    await page.locator(".composer-slash-menu").waitFor();
    await composer.press("Enter");
    const busyForm = page.locator(".composer-command-form");
    await busyForm.waitFor();
    await busyForm.getByLabel("agent-id").fill("WIKI-150");
    await busyForm.getByLabel("message").fill("in flight");
    await busyForm.locator(".composer-command-submit").click();
    const cancelBtn = page.locator(".composer-command-cancel");
    await cancelBtn.waitFor();
    // busy → cancel button disabled. waitForFunction signature is
    // (pageFunction, arg, options); the options object MUST be the
    // third argument or Playwright treats it as `arg` and falls back
    // to the 30-second default — mutation failures would masquerade
    // as flaky slowness (round-8 REVIEW [MEDIUM]:562).
    await page.waitForFunction(
      () => {
        const btn = document.querySelector(".composer-command-cancel");
        return !!btn && btn.hasAttribute("disabled");
      },
      undefined,
      { timeout: 2000 }
    );
    // clicking a disabled button is a no-op
    await cancelBtn.click({ force: true }).catch(() => {});
    // Escape must ALSO be a no-op while busy. Round-8 REVIEW [MEDIUM]:
    // pressing Escape on the <form> itself does nothing (forms are not
    // focus-navigable, so the key event never bubbles); focus a live
    // enabled descendant — the message textarea stays enabled while
    // the request is in flight — so the busy guard in `handleKeyDown`
    // is actually exercised. Prior version passed even when both
    // production guards were removed.
    const busyMessage = busyForm.getByLabel("message");
    await busyMessage.focus();
    await busyMessage.press("Escape");
    await delay(100);
    if (!(await page.locator(".composer-command-form").count())) {
      throw new Error("Escape must not close the form while a command is in flight");
    }
    // resolve the request; form should close and exactly one dispatch happened
    releaseSteer();
    await page.waitForFunction(() => document.querySelector(".composer-command-form") === null);
    if (steerBusyCalls !== 1) {
      throw new Error(`busy-cancel: expected 1 message call, got ${steerBusyCalls}`);
    }
  } finally {
    await page.close();
    await browser.close();
    await backend.stop();
  }
}

await main();
