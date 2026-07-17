import fs from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";

import {
  codexAssistant,
  makeFixtureRoot,
  startBackend,
  writeQueue,
  writeRegistry,
} from "../scripts/wiki32-harness.mjs";

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function dispatchGlobalKey(page, target, { isComposing = false } = {}) {
  await target.evaluate((element, composing) => {
    const event = new KeyboardEvent("keydown", {
      bubbles: true,
      cancelable: true,
      isComposing: composing,
      key: "j",
    });
    Object.defineProperty(event, "target", { configurable: true, value: element });
    window.dispatchEvent(event);
  }, isComposing);
}

async function assertTextEntryDoesNotScroll(page, scroller, target, label, options) {
  await scroller.evaluate((element) => element.scrollTo({ top: 0, behavior: "auto" }));
  await page.locator(".pane-frame.is-focused").focus();
  const before = await scroller.evaluate((element) => element.scrollTop);
  await dispatchGlobalKey(page, target, options);
  await page.waitForTimeout(100);
  const after = await scroller.evaluate((element) => element.scrollTop);
  assert(after === before, `${label} allowed j to scroll from ${before} to ${after}`);
}

async function main() {
  const fixtures = makeFixtureRoot("wiki-133-jk-scroll-");
  const vault = path.join(fixtures.root, "vault");
  const transcript = path.join(fixtures.root, "WIKI-133.jsonl");
  await fs.mkdir(vault, { recursive: true });
  await fs.writeFile(
    path.join(vault, "scroll.md"),
    [
      "# Scroll fixture",
      "",
      ...Array.from({ length: 80 }, (_, index) => `## Section ${index + 1}\n\nScroll content ${index + 1}.`),
    ].join("\n"),
  );
  await fs.writeFile(
    transcript,
    Array.from({ length: 80 }, (_, index) =>
      JSON.stringify(
        codexAssistant(
          `Agent transcript line ${index + 1}`,
          `2026-07-17T12:00:${String(index % 60).padStart(2, "0")}Z`,
        ),
      ),
    ).join("\n") + "\n",
  );
  writeRegistry(fixtures.registryPath, [["WIKI-133", transcript]]);
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
              layout: {
                kind: "split",
                direction: "row",
                ratio: 0.7,
                first: { kind: "pane", id: "pane-1", path: "scroll.md" },
                second: { kind: "pane", id: "pane-2", path: "agent://WIKI-133" },
              },
            },
          ],
        }),
      );
    });
    await page.goto(`${backend.baseUrl}/#/note/scroll.md`, { waitUntil: "domcontentloaded" });
    const notePreview = page.locator(".pane-frame[data-pane-key='pane-1'] .markdown-preview-view");
    await notePreview.waitFor({ state: "visible" });
    const scroller = page.locator(".pane-frame.is-focused .view-content");
    await page.waitForFunction(() => {
      const element = document.querySelector(".pane-frame.is-focused .view-content");
      return element instanceof HTMLElement && element.scrollHeight > element.clientHeight;
    });
    await page.locator(".pane-frame.is-focused").focus();
    assert(
      await page.evaluate(() => document.activeElement?.classList.contains("pane-frame")),
      "note pane was not focused before keyboard scrolling",
    );

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
    await notePreview.evaluate((preview) => {
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

    await page.locator(".session-composer textarea").waitFor({ state: "visible" });
    await assertTextEntryDoesNotScroll(
      page,
      scroller,
      page.locator(".session-composer textarea"),
      "composer",
    );

    await page.getByLabel("Search", { exact: true }).click();
    const searchInput = page.getByPlaceholder("Search...");
    await searchInput.waitFor({ state: "visible" });
    await assertTextEntryDoesNotScroll(page, scroller, searchInput, "search");

    await page.locator(".pane-frame.is-focused").focus();
    await page.waitForFunction(() => document.activeElement?.classList.contains("pane-frame"));
    await page.keyboard.press("Meta+k");
    const switcherInput = page.getByPlaceholder("Find a note, file, or session...");
    await switcherInput.waitFor({ state: "visible" });
    await assertTextEntryDoesNotScroll(page, scroller, switcherInput, "command palette");
    await switcherInput.focus();
    await page.keyboard.press("Escape");

    await page.getByLabel("Settings", { exact: true }).click();
    const settings = page.locator(".settings-modal");
    await settings.waitFor({ state: "visible" });
    await assertTextEntryDoesNotScroll(page, scroller, settings.locator("select").first(), "modal");
    await settings.getByRole("button", { name: "Close settings" }).click();

    await page.goto(`${backend.baseUrl}/#/edit/scroll.md`, { waitUntil: "domcontentloaded" });
    const editor = page.locator(".cm-content[contenteditable='true']");
    await editor.waitFor({ state: "visible" });
    const editorView = page.locator(".pane-frame.is-focused .view-content");
    await editorView.evaluate((element) => {
      const preview = document.createElement("div");
      preview.className = "markdown-preview-view";
      preview.style.height = "2000px";
      element.append(preview);
    });
    await assertTextEntryDoesNotScroll(page, editorView, editor, "editor contenteditable");
    await assertTextEntryDoesNotScroll(page, editorView, editor, "IME", { isComposing: true });

    await page.goto(`${backend.baseUrl}/#/agent/WIKI-133`, { waitUntil: "domcontentloaded" });
    const sessionScroller = page.locator(".pane-frame.is-focused .session-scroll");
    await sessionScroller.waitFor({ state: "visible" });
    await page.locator(".pane-frame.is-focused").focus();
    await sessionScroller.evaluate((element) => element.scrollTo({ top: 0, behavior: "auto" }));
    await page.keyboard.press("j");
    await page.waitForTimeout(100);
    assert(
      (await sessionScroller.evaluate((element) => element.scrollTop)) === 0,
      "j scrolled a session view",
    );

    await page.getByLabel("Agents", { exact: true }).click();
    const agentsView = page.locator(".pane-frame.is-focused .view-content");
    await agentsView.waitFor({ state: "visible" });
    await page.locator(".pane-frame.is-focused").focus();
    const agentsBefore = await agentsView.evaluate((element) => element.scrollTop);
    await page.keyboard.press("j");
    await page.waitForTimeout(100);
    assert(
      (await agentsView.evaluate((element) => element.scrollTop)) === agentsBefore,
      "j scrolled the agents view",
    );
  } finally {
    await browser.close();
    await backend.stop();
  }
}

await main();
