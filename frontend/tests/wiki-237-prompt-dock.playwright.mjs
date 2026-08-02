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
  workingSplit: "/tmp/WIKI-237-working-split.png",
  idleSplit: "/tmp/WIKI-237-idle-split.png",
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
  let sessionRequests = 0;
  let skillsRequests = 0;
  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });

  try {
    await page.route(`**/api/agents/${TICKET}/session?**`, async (route) => {
      sessionRequests += 1;
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
        localStorage.setItem("wiki-sidebar-visible", "true");
      }
      if (localStorage.getItem("wiki-window-layout-v2") === null) {
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
      }
    }, { ticket: TICKET });

    const mountSession = async () => {
      const sessionUrl = `${backend.baseUrl}/#/agent/${TICKET}`;
      const skillsRequestsBeforeNavigation = skillsRequests;
      await page.goto(sessionUrl, { waitUntil: "domcontentloaded" });
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
      const visibleExpected = expected.replaceAll(" · ", " ");
      await page.waitForFunction(
        ({ selector, value }) => {
          const node = document.querySelector(selector);
          return node instanceof HTMLElement && node.innerText.replace(/\s+/g, " ").trim() === value;
        },
        { selector: ".session-footer-hints", value: visibleExpected },
      );
      const footerText = (await page.locator(".session-footer-hints").innerText()).replace(/\s+/g, " ");
      assert(
        footerText === visibleExpected,
        `visible guidance mismatch: ${footerText}`,
      );
      const composer = page.locator(".session-composer textarea");
      const helpId = await composer.getAttribute("aria-describedby");
      const helpText = (await page.locator(`[id=${JSON.stringify(helpId)}]`).textContent()).trim();
      assert(helpText === expected, `accessible guidance mismatch: ${helpText}`);
    };

    const waitForDelivery = async (count) => {
      const deadline = Date.now() + 5_000;
      while (deliveries.length < count && Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 25));
      }
      assert(deliveries.length >= count, `expected ${count} deliveries, got ${deliveries.length}`);
    };

    const waitForWorkingState = async (nextWorking) => {
      const requestsBeforeChange = sessionRequests;
      working = nextWorking;
      await page.evaluate(() => window.dispatchEvent(new Event("resize")));
      await new Promise((resolve) => setTimeout(resolve, 2_600));
      const pollDeadline = Date.now() + 5_000;
      while (sessionRequests <= requestsBeforeChange && Date.now() < pollDeadline) {
        await new Promise((resolve) => setTimeout(resolve, 25));
      }
      assert(
        sessionRequests > requestsBeforeChange,
        "session polling must request the new working state",
      );
      const expectedAction = nextWorking ? "Send now" : "Send";
      await page.waitForFunction(
        ({ selector, value }) => document.querySelector(selector)?.getAttribute("aria-label") === value,
        { selector: ".session-send", value: expectedAction },
      );
    };

    const assertCompactSend = async (context) => {
      const composerRoot = page.locator(".session-composer");
      await page.waitForFunction(() =>
        document.querySelector(".session-composer")?.getAttribute("data-compact") === "true",
      );
      const compactState = await composerRoot.evaluate((root) => {
        const sendButton = root.querySelector(".session-send");
        const sendLabel = root.querySelector(".session-send-label");
        const buttonRect = sendButton.getBoundingClientRect();
        return {
          composerWidth: root.getBoundingClientRect().width,
          buttonWidth: buttonRect.width,
          buttonHeight: buttonRect.height,
          labelDisplay: getComputedStyle(sendLabel).display,
        };
      });
      assert(
        compactState.composerWidth <= 480,
        `${context} composer must be at most 480px: ${compactState.composerWidth}`,
      );
      assert(
        compactState.buttonWidth === 40 && compactState.buttonHeight === 40,
        `${context} send action must be 40px: ${compactState.buttonWidth}x${compactState.buttonHeight}`,
      );
      assert(
        compactState.labelDisplay === "none",
        `${context} send text must be hidden: ${compactState.labelDisplay}`,
      );
    };

    const assertExpandedSend = async (context) => {
      await page.waitForFunction(() =>
        document.querySelector(".session-composer")?.getAttribute("data-compact") !== "true",
      );
      const expandedState = await page.locator(".session-composer").evaluate((root) => {
        const sendButton = root.querySelector(".session-send");
        const sendLabel = root.querySelector(".session-send-label");
        const buttonRect = sendButton.getBoundingClientRect();
        return {
          composerWidth: root.getBoundingClientRect().width,
          buttonWidth: buttonRect.width,
          buttonHeight: buttonRect.height,
          labelDisplay: getComputedStyle(sendLabel).display,
        };
      });
      assert(
        expandedState.composerWidth > 480,
        `${context} composer must exceed 480px: ${expandedState.composerWidth}`,
      );
      assert(
        expandedState.buttonWidth >= 82 && expandedState.buttonHeight === 40,
        `${context} send action must show its 82x40 layout: ${expandedState.buttonWidth}x${expandedState.buttonHeight}`,
      );
      assert(
        expandedState.labelDisplay !== "none",
        `${context} send text must be visible: ${expandedState.labelDisplay}`,
      );
    };

    const resizeSplit = async (ratio) => {
      const split = page.locator(".pane-split.row");
      const divider = split.locator(".pane-divider.row");
      const bounds = await split.boundingBox();
      assert(bounds, "split pane must have measurable bounds");
      await divider.hover();
      await page.mouse.down();
      await page.mouse.move(
        bounds.x + bounds.width * ratio,
        bounds.y + bounds.height / 2,
        { steps: 8 },
      );
      await page.mouse.up();
    };

    const assertNoHorizontalOverflow = async (context) => {
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
        `${context} composer must not overflow: ${bounds.composer.join("/")}`,
      );
      assert(
        bounds.footer[0] <= bounds.footer[1],
        `${context} guidance must not overflow: ${bounds.footer.join("/")}`,
      );
    };

    logStep("working state at normal width");
    await mountSession();
    let mountedComposer = await page.locator(".session-composer").elementHandle();
    assert(mountedComposer, "composer must mount once before live update checks");
    const assertComposerStayedMounted = async (context) => {
      assert(
        await page.evaluate(
          (node) => node.isConnected && document.querySelector(".session-composer") === node,
          mountedComposer,
        ),
        `${context} must keep the original composer mounted`,
      );
    };
    await assertTarget();
    await assertGuidance("enter send now · shift+enter queue until idle · esc vim");
    await assertExpandedSend("normal working");
    const send = page.locator(".session-send");
    assert((await send.innerText()).trim() === "send", "normal-width send action must have visible text");
    const sendSize = await send.evaluate((node) => {
      const rect = node.getBoundingClientRect();
      return { width: rect.width, height: rect.height };
    });
    assert(sendSize.width >= 40 && sendSize.height >= 40, "send action must meet the 40px target floor");
    await page.screenshot({ path: SCREENSHOTS.workingNormal, fullPage: true });

    const composer = page.locator(".session-composer textarea");
    logStep("live working-to-idle polling at normal width");
    await waitForWorkingState(false);
    await assertTarget();
    await assertGuidance("enter send · shift+enter newline · esc vim");
    await assertExpandedSend("normal idle after polling");
    await assertComposerStayedMounted("working-to-idle polling");
    assert(await send.getAttribute("aria-label") === "Send", "idle send action must not say send now");
    await page.screenshot({ path: SCREENSHOTS.idleNormal, fullPage: true });

    let expectedDeliveries = deliveries.length + 1;
    const requestsBeforeIdleEnter = sessionRequests;
    working = true;
    await composer.fill("send now while idle");
    await composer.press("Enter");
    await waitForDelivery(expectedDeliveries);
    assert(deliveries.at(-1)?.mode === "now", "idle Enter must use mode now");
    await page.waitForFunction(() =>
      document.querySelector(".session-send")?.getAttribute("aria-label") === "Send now",
    );
    assert(
      sessionRequests > requestsBeforeIdleEnter,
      "idle Enter must refresh the mounted composer into working state",
    );
    await assertGuidance("enter send now · shift+enter queue until idle · esc vim");

    expectedDeliveries += 1;
    await composer.fill("send now with enter");
    await composer.press("Enter");
    await waitForDelivery(expectedDeliveries);
    assert(deliveries.at(-1)?.mode === "now", "working Enter must use mode now");

    expectedDeliveries += 1;
    await composer.fill("send now with the visible action");
    await send.click();
    await waitForDelivery(expectedDeliveries);
    assert(deliveries.at(-1)?.mode === "now", "working Send button must use mode now");

    expectedDeliveries += 1;
    await composer.fill("queue this after the current turn");
    await composer.press("Shift+Enter");
    await waitForDelivery(expectedDeliveries);
    await page.waitForFunction(() => document.querySelector(".session-composer textarea")?.value === "");
    assert(deliveries.at(-1)?.mode === "on-idle", "working Shift+Enter must queue until idle");

    logStep("retained composer functions");
    const durableHistoryRow = page.locator(".session-user:not(.session-pending-user)", {
      hasText: "make the agent composer clear",
    });
    await durableHistoryRow.waitFor({ state: "visible" });
    await page.waitForFunction(() =>
      document.querySelector(".session-composer")?.getAttribute("data-history-count") === "1",
    );
    await composer.fill("");
    await page.evaluate(
      () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))),
    );
    await page.waitForFunction(() => {
      const node = document.querySelector(".session-composer textarea");
      return node?.value === "" && node?.selectionStart === 0 && node?.selectionEnd === 0;
    });
    await composer.press("Escape");
    await page.locator(".session-vim-mode", { hasText: "-- NORMAL --" }).waitFor();
    await page.evaluate(
      () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))),
    );
    await page.waitForFunction(() => {
      const node = document.querySelector(".session-composer textarea");
      return node?.selectionStart === 0 && node?.selectionEnd === 0;
    });
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
    await composer.fill("");

    logStep("create a real split without reloading");
    await composer.focus();
    await page.keyboard.press("Control+a");
    await page.keyboard.press("p");
    await page.locator(".pane-split.row").waitFor({ state: "visible" });
    mountedComposer = await page.locator(".session-composer").elementHandle();
    assert(mountedComposer, "split composer must mount before live divider checks");

    logStep("live resize into a narrow desktop split pane");
    await resizeSplit(0.35);
    await assertTarget(`Steer ${TICKET}…`);
    await assertGuidance("enter send now · shift+enter queue until idle · esc vim");
    await assertCompactSend("narrow split working");
    await assertNoHorizontalOverflow("narrow split working");
    await assertComposerStayedMounted("wide-to-narrow split resize");
    await page.screenshot({ path: SCREENSHOTS.workingSplit, fullPage: true });

    logStep("live resize back to an expanded working pane");
    await resizeSplit(0.65);
    await assertTarget();
    await assertGuidance("enter send now · shift+enter queue until idle · esc vim");
    await assertExpandedSend("expanded split working");
    await assertNoHorizontalOverflow("expanded split working");
    await assertComposerStayedMounted("narrow-to-wide split resize");

    logStep("live working state at narrow viewport width");
    await page.setViewportSize({ width: 360, height: 1200 });
    await assertTarget(`Steer ${TICKET}…`);
    await assertGuidance("enter send now · shift+enter queue until idle · esc vim");
    await assertCompactSend("narrow viewport working");
    await assertNoHorizontalOverflow("narrow viewport working");
    await assertComposerStayedMounted("narrow viewport resize");
    await page.screenshot({ path: SCREENSHOTS.workingNarrow, fullPage: true });

    logStep("return to idle through mounted-session refresh");
    await page.setViewportSize({ width: 1280, height: 900 });
    await assertTarget();
    await assertExpandedSend("restored normal working");
    working = false;
    expectedDeliveries += 1;
    await composer.fill("finish the working turn now");
    await composer.press("Enter");
    await waitForDelivery(expectedDeliveries);
    assert(deliveries.at(-1)?.mode === "now", "working Enter before idle widths must use mode now");
    await page.waitForFunction(() =>
      document.querySelector(".session-send")?.getAttribute("aria-label") === "Send",
    );
    await assertTarget();
    await assertGuidance("enter send · shift+enter newline · esc vim");
    await assertExpandedSend("normal idle");
    await assertComposerStayedMounted("working-to-idle polling");
    assert(await send.getAttribute("aria-label") === "Send", "idle send action must not say send now");
    await page.screenshot({ path: SCREENSHOTS.idleNormal, fullPage: true });

    expectedDeliveries += 1;
    await composer.fill("send now while idle");
    await composer.press("Enter");
    await waitForDelivery(expectedDeliveries);
    assert(deliveries.at(-1)?.mode === "now", "idle Enter must use mode now");

    const beforeIdleShift = deliveries.length;
    await composer.fill("first line");
    await composer.press("Shift+Enter");
    assert((await composer.inputValue()) === "first line\n", "idle Shift+Enter must insert a newline");
    assert(deliveries.length === beforeIdleShift, "idle Shift+Enter must not queue a message");
    await composer.fill("");

    logStep("live idle state in a narrow desktop split pane");
    await resizeSplit(0.35);
    await assertTarget(`Ask ${TICKET}…`);
    await assertGuidance("enter send · shift+enter newline · esc vim");
    await assertCompactSend("narrow split idle");
    await assertNoHorizontalOverflow("narrow split idle");
    await assertComposerStayedMounted("narrow split idle resize");
    await page.screenshot({ path: SCREENSHOTS.idleSplit, fullPage: true });

    logStep("live idle state at narrow viewport width");
    await resizeSplit(0.65);
    await assertTarget();
    await assertExpandedSend("restored expanded idle");
    await page.setViewportSize({ width: 360, height: 1200 });
    await assertTarget(`Ask ${TICKET}…`);
    await assertGuidance("enter send · shift+enter newline · esc vim");
    await assertCompactSend("narrow viewport idle");
    await assertNoHorizontalOverflow("narrow viewport idle");
    await assertComposerStayedMounted("narrow viewport polling");
    await page.screenshot({ path: SCREENSHOTS.idleNarrow, fullPage: true });

    logStep("PASS");
  } finally {
    await page.close();
    await browser.close();
    await backend.stop();
  }
}

await main();
