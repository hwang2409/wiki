import fs from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";

import { makeFixtureRoot, startBackend, writeQueue, writeRegistry } from "../scripts/wiki32-harness.mjs";

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function main() {
  const fixtures = makeFixtureRoot("wiki-133-jk-scroll-");
  const vault = path.join(fixtures.root, "vault");
  await fs.mkdir(vault, { recursive: true });
  await fs.writeFile(
    path.join(vault, "scroll.md"),
    [
      "# Scroll fixture",
      "",
      ...Array.from({ length: 80 }, (_, index) => `## Section ${index + 1}\n\nScroll content ${index + 1}.`),
    ].join("\n"),
  );
  writeRegistry(fixtures.registryPath, []);
  writeQueue(fixtures.queuePath, "WIKI-133", []);

  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1000, height: 600 } });

  try {
    await page.addInitScript(() => {
      localStorage.setItem(
        "wiki-window-layout-v2",
        JSON.stringify({
          version: 2,
          activeWindowId: "window-0",
          windows: [
            {
              id: "window-0",
              focusedPaneId: "pane-1",
              layout: { kind: "pane", id: "pane-1", path: "scroll.md" },
            },
          ],
        }),
      );
    });
    await page.goto(`${backend.baseUrl}/#/note/scroll.md`, { waitUntil: "domcontentloaded" });
    await page.locator(".markdown-preview-view").waitFor({ state: "visible" });
    const scroller = page.locator(".pane-frame.is-focused .view-content");
    await page.waitForFunction(() => {
      const element = document.querySelector(".pane-frame.is-focused .view-content");
      return element instanceof HTMLElement && element.scrollHeight > element.clientHeight;
    });

    await scroller.evaluate((element) => element.scrollTo({ top: 0, behavior: "auto" }));
    await page.keyboard.press("j");
    await page.waitForFunction(() => {
      const element = document.querySelector(".pane-frame.is-focused .view-content");
      return element instanceof HTMLElement && element.scrollTop > 0;
    });
    const afterJ = await scroller.evaluate((element) => element.scrollTop);
    assert(afterJ > 0, `j did not scroll note preview: ${afterJ}`);

    await page.keyboard.press("k");
    await page.waitForFunction((before) => {
      const element = document.querySelector(".pane-frame.is-focused .view-content");
      return element instanceof HTMLElement && element.scrollTop < before;
    }, afterJ);
    const afterK = await scroller.evaluate((element) => element.scrollTop);
    assert(afterK < afterJ, `k did not scroll note preview back: ${afterK} >= ${afterJ}`);

    await page.waitForTimeout(500);
    await scroller.evaluate((element) => element.scrollTo({ top: 0, behavior: "auto" }));
    await page.locator(".markdown-preview-view").evaluate((preview) => {
      const input = document.createElement("input");
      input.dataset.wiki133Input = "true";
      input.type = "text";
      preview.prepend(input);
      input.focus();
    });
    const focusState = await page.evaluate(() => ({
      active: document.activeElement?.tagName,
      activeType: document.activeElement instanceof HTMLInputElement ? document.activeElement.type : null,
      focusedPane: document.querySelector(".pane-frame.is-focused") !== null,
    }));
    assert(focusState.active === "INPUT", `expected focused input, saw ${JSON.stringify(focusState)}`);
    const beforeInput = await scroller.evaluate((element) => element.scrollTop);
    await page.keyboard.type("j");
    await page.waitForTimeout(100);
    const inputScrollTop = await scroller.evaluate((element) => element.scrollTop);
    const inputValue = await page.locator('input[data-wiki133-input="true"]').inputValue();
    assert(inputValue === "j", `j did not remain text in the focused input: ${inputValue}`);
    assert(
      inputScrollTop === beforeInput,
      `j stole focus from a focused input and scrolled from ${beforeInput} to ${inputScrollTop}`,
    );
  } finally {
    await browser.close();
    await backend.stop();
  }
}

await main();
