import fs from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";

import {
  codexAssistant,
  codexUser,
  makeFixtureRoot,
  startBackend,
  writeQueue,
  writeRegistry,
} from "../scripts/wiki32-harness.mjs";

const TICKET = "WIKI-237";
const SCREENSHOTS = {
  workingNormal: "/tmp/WIKI-237-working-normal.png",
  idleNormal: "/tmp/WIKI-237-idle-normal.png",
  workingNarrow: "/tmp/WIKI-237-working-narrow.png",
  idleNarrow: "/tmp/WIKI-237-idle-narrow.png",
};

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function logStep(message) {
  console.error(`[wiki-237-playwright] ${message}`);
}

async function main() {
  const fixtures = makeFixtureRoot("wiki-237-prompt-dock-");
  const transcript = path.join(fixtures.root, "prompt-dock.jsonl");
  await fs.writeFile(
    transcript,
    [
      codexUser("make the agent composer clear", "2026-08-02T12:00:00Z"),
      codexAssistant("I am checking the prompt dock.", "2026-08-02T12:00:01Z"),
    ].map((row) => JSON.stringify(row)).join("\n") + "\n",
  );
  writeRegistry(fixtures.registryPath, [[TICKET, transcript]]);
  writeQueue(fixtures.queuePath, TICKET, []);

  let working = true;
  const deliveries = [];
  let skillsRequests = 0;
  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });

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
          model: "gpt-5.6-sol",
          kind: "cdx",
          provider: "codex",
          tasks: [],
          pr: null,
          session_meta: {},
          dispositions: { rendered: 2, summarized: 0, ignored: 0, unknown: 0 },
          base: 0,
          cursor: 2,
          tail_from: 0,
          events: [
            {
              id: 0,
              kind: "user",
              ts: "2026-08-02T12:00:00Z",
              text: "make the agent composer clear",
              disposition: "rendered",
            },
            {
              id: 1,
              kind: "assistant",
              ts: "2026-08-02T12:00:01Z",
              text: "I am checking the prompt dock.",
              disposition: "rendered",
            },
          ],
          patches: [],
          subagents: [],
          queue: [],
          working,
        }),
      });
    });

    await page.route(`**/api/agents/${TICKET}/message`, async (route) => {
      deliveries.push(route.request().postDataJSON());
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ status: "sent" }),
      });
    });

    await page.route("**/api/skills", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          skills: [
            { name: "frontend-design", description: "Design focused interfaces" },
          ],
        }),
      });
      skillsRequests += 1;
    });

    await page.route("**/api/upload", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          path: "/tmp/wiki-uploads/prompt-dock.png",
          url: "/api/uploads/prompt-dock.png",
        }),
      });
    });

    await page.addInitScript(({ ticket }) => {
      if (localStorage.getItem("wiki-sidebar-visible") === null) {
        localStorage.setItem("wiki-sidebar-visible", "false");
      }
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

    const openSession = async () => {
      const sessionUrl = `${backend.baseUrl}/#/agent/${TICKET}`;
      const skillsRequestsBeforeNavigation = skillsRequests;
      if (page.url() === sessionUrl) {
        await page.reload({ waitUntil: "domcontentloaded" });
      } else {
        await page.goto(sessionUrl, { waitUntil: "domcontentloaded" });
      }
      await page.locator(".session-composer textarea").waitFor({ state: "visible" });
      const deadline = Date.now() + 5_000;
      while (skillsRequests <= skillsRequestsBeforeNavigation && Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 25));
      }
      assert(
        skillsRequests > skillsRequestsBeforeNavigation,
        "current composer document must load skills before interaction checks",
      );
    };

    const assertTarget = async (expectedPlaceholder = `Ask a question or give ${TICKET} a new direction…`) => {
      const label = page.locator(".session-composer-target");
      const labelText = (await label.innerText()).replace(/\s+/g, " ");
      assert(labelText === `ask or steer ${TICKET}`, "composer must name its target");
      const composer = page.locator(".session-composer textarea");
      await page.waitForFunction(
        ({ selector, placeholder }) =>
          document.querySelector(selector)?.getAttribute("placeholder") === placeholder,
        { selector: ".session-composer textarea", placeholder: expectedPlaceholder },
      );
      assert(
        (await composer.getAttribute("placeholder")) === expectedPlaceholder,
        "composer must use a task-specific placeholder",
      );
      const labelFor = await label.getAttribute("for");
      assert(labelFor === await composer.getAttribute("id"), "target label must name the prompt field");
    };

    const assertGuidance = async (expected) => {
      const footerText = (await page.locator(".session-footer-hints").innerText()).replace(/\s+/g, " ");
      assert(
        footerText === expected.replaceAll(" · ", " "),
        `visible guidance mismatch: ${footerText}`,
      );
      const composer = page.locator(".session-composer textarea");
      const helpId = await composer.getAttribute("aria-describedby");
      const helpText = (await page.locator(`[id=${JSON.stringify(helpId)}]`).textContent()).trim();
      assert(helpText === expected, `accessible guidance mismatch: ${helpText}`);
    };

    logStep("working state at normal width");
    await openSession();
    await assertTarget();
    await assertGuidance("enter send now · shift+enter queue until idle · esc vim");
    const send = page.locator(".session-send");
    assert((await send.innerText()).trim() === "send", "normal-width send action must have visible text");
    const sendSize = await send.evaluate((node) => {
      const rect = node.getBoundingClientRect();
      return { width: rect.width, height: rect.height };
    });
    assert(sendSize.width >= 40 && sendSize.height >= 40, "send action must meet the 40px target floor");
    await page.screenshot({ path: SCREENSHOTS.workingNormal, fullPage: true });

    const composer = page.locator(".session-composer textarea");
    await composer.fill("queue this after the current turn");
    await composer.press("Shift+Enter");
    await page.waitForFunction(() => document.querySelector(".session-composer textarea")?.value === "");
    assert(deliveries.at(-1)?.mode === "on-idle", "working Shift+Enter must queue until idle");

    logStep("idle state at normal width");
    working = false;
    await openSession();
    await assertTarget();
    await assertGuidance("enter send · shift+enter newline · esc vim");
    assert(await send.getAttribute("aria-label") === "Send", "idle send action must not say send now");
    await page.screenshot({ path: SCREENSHOTS.idleNormal, fullPage: true });

    const beforeIdleShift = deliveries.length;
    await composer.fill("first line");
    await composer.press("Shift+Enter");
    assert((await composer.inputValue()) === "first line\n", "idle Shift+Enter must insert a newline");
    assert(deliveries.length === beforeIdleShift, "idle Shift+Enter must not queue a message");

    logStep("retained composer functions");
    const durableHistoryRow = page.locator(".session-user:not(.session-pending-user)", {
      hasText: "make the agent composer clear",
    });
    await durableHistoryRow.waitFor({ state: "visible" });
    await composer.fill("");
    await composer.press("Escape");
    await page.locator(".session-vim-mode", { hasText: "-- NORMAL --" }).waitFor();
    await composer.press("k");
    await page.waitForFunction(
      ({ selector, value }) => document.querySelector(selector)?.value === value,
      { selector: ".session-composer textarea", value: "make the agent composer clear" },
    );
    assert(
      (await composer.inputValue()) === "make the agent composer clear",
      "Vim k must recall composer history",
    );
    await composer.press("G");
    await page.waitForFunction(
      ({ selector, value }) => {
        const node = document.querySelector(selector);
        return node?.selectionStart === value.length - 1 && node?.selectionEnd === value.length;
      },
      { selector: ".session-composer textarea", value: "make the agent composer clear" },
    );
    await composer.press("j");
    await page.waitForFunction(
      ({ selector, value }) => document.querySelector(selector)?.value === value,
      { selector: ".session-composer textarea", value: "" },
    );
    assert((await composer.inputValue()) === "", "Vim j must restore the draft");
    await composer.press("i");
    await page.locator(".session-vim-mode").waitFor({ state: "detached" });
    await page.evaluate(
      () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))),
    );
    await page.waitForFunction(() => {
      const node = document.querySelector(".session-composer textarea");
      return node?.selectionStart === 0 && node?.selectionEnd === 0;
    });

    await composer.fill("/");
    await page.waitForFunction(() => {
      const node = document.querySelector(".session-composer textarea");
      return node?.value === "/" && node?.selectionStart === 1 && node?.selectionEnd === 1;
    });
    await page.locator(".composer-slash-menu").waitFor();
    await composer.press("Escape");
    await page.locator(".composer-slash-menu").waitFor({ state: "detached" });

    await composer.fill("");
    await page.evaluate(
      () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))),
    );
    await composer.press("$");
    const skillMenu = page.locator(".session-skill-menu");
    await skillMenu.waitFor();
    await composer.pressSequentially("front");
    await skillMenu.getByText("$frontend-design", { exact: true }).waitFor();
    assert(
      (await skillMenu.innerText()).includes("frontend-design"),
      "skill picker must show matching skills",
    );
    await composer.press("Enter");
    assert(
      (await composer.inputValue()) === "$frontend-design ",
      "skill picker must insert the selected skill",
    );

    await composer.fill("review this");
    await composer.evaluate((node) => {
      const transfer = new DataTransfer();
      transfer.items.add(new File([new Uint8Array([137, 80, 78, 71])], "prompt-dock.png", {
        type: "image/png",
      }));
      const event = new Event("paste", { bubbles: true, cancelable: true });
      Object.defineProperty(event, "clipboardData", { value: transfer });
      node.dispatchEvent(event);
    });
    const attachment = page.locator(".session-attachment");
    await attachment.waitFor();
    assert(
      (await composer.inputValue()).includes("[image: /tmp/wiki-uploads/prompt-dock.png]"),
      "image paste must add its composer token",
    );
    await attachment.getByRole("button", { name: "Remove attachment" }).click();
    await attachment.waitFor({ state: "detached" });
    assert(!(await composer.inputValue()).includes("[image:"), "attachment removal must clear its token");

    logStep("working and idle states at narrow width");
    working = true;
    // Below 900px, the shell hides the sidebar through CSS. Keep the stored
    // sidebar state on so the mobile two-column grid owns the pane width.
    await page.evaluate(() => localStorage.setItem("wiki-sidebar-visible", "true"));
    await page.setViewportSize({ width: 360, height: 780 });
    await openSession();
    await assertTarget(`Steer ${TICKET}…`);
    await assertGuidance("enter send now · shift+enter queue until idle · esc vim");
    await page.screenshot({ path: SCREENSHOTS.workingNarrow, fullPage: true });

    working = false;
    await openSession();
    await assertTarget(`Ask ${TICKET}…`);
    await assertGuidance("enter send · shift+enter newline · esc vim");
    const bounds = await page.locator(".session-tab").evaluate((root) => {
      const composerRoot = root.querySelector(".session-composer");
      const footer = root.querySelector(".session-footer");
      return {
        composer: [composerRoot.scrollWidth, composerRoot.clientWidth],
        footer: [footer.scrollWidth, footer.clientWidth],
      };
    });
    assert(
      bounds.composer[0] <= bounds.composer[1],
      `narrow composer must not overflow: ${bounds.composer.join("/")}`,
    );
    assert(
      bounds.footer[0] <= bounds.footer[1],
      `narrow guidance must not overflow: ${bounds.footer.join("/")}`,
    );
    await page.screenshot({ path: SCREENSHOTS.idleNarrow, fullPage: true });

    logStep("PASS");
  } finally {
    await page.close();
    await browser.close();
    await backend.stop();
  }
}

await main();
