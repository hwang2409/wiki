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
const TICKET = "WIKI-402";
const SCREENSHOT_DIR = path.join(ROOT, "docs", "pr-screenshots");

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function main() {
  const fixtures = makeFixtureRoot("wiki-402-vim-cursor-");
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

  try {
    await page.addInitScript(({ ticket }) => {
      localStorage.setItem("wiki-sidebar-visible", "false");
      localStorage.setItem("wiki-window-layout-v2", JSON.stringify({
        version: 2,
        activeWindowId: "window-0",
        windows: [{
          id: "window-0",
          focusedPaneId: "pane-1",
          layout: { kind: "pane", id: "pane-1", path: `agent://${ticket}` },
        }],
      }));
    }, { ticket: TICKET });

    await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
    const composer = page.locator(".session-composer textarea").first();
    await composer.waitFor({ state: "visible", timeout: 30_000 });
    await composer.click();
    await composer.fill("vim cursor");
    await page.waitForFunction(() => {
      const el = document.querySelector(".session-composer textarea");
      return el instanceof HTMLTextAreaElement && document.activeElement === el;
    });

    const insertState = await composer.evaluate((el) => ({
      caretColor: getComputedStyle(el).caretColor,
      overlayCount: document.querySelectorAll(".session-empty-block-cursor").length,
      vimClass: el.className,
    }));
    assert(insertState.caretColor !== "transparent" && insertState.caretColor !== "rgba(0, 0, 0, 0)",
      `insert mode must show the native caret, got ${insertState.caretColor}`);
    assert(insertState.overlayCount === 0,
      `insert mode must not render an overlay block, found ${insertState.overlayCount}`);
    assert(insertState.vimClass === "is-vim-insert",
      `insert mode must carry its vim class, got ${insertState.vimClass}`);
    await fs.mkdir(SCREENSHOT_DIR, { recursive: true });
    await page.screenshot({ path: path.join(SCREENSHOT_DIR, "wiki-402-insert.png") });

    await composer.press("Escape");
    await page.waitForFunction(() => {
      const el = document.querySelector(".session-composer textarea");
      return el instanceof HTMLTextAreaElement
        && el.classList.contains("is-vim-normal")
        && el.selectionEnd - el.selectionStart === 1;
    });
    const normalState = await composer.evaluate((el) => ({
      caretColor: getComputedStyle(el).caretColor,
      overlayCount: document.querySelectorAll(".session-empty-block-cursor").length,
      selectionLength: el.selectionEnd - el.selectionStart,
    }));
    assert(normalState.caretColor === "transparent" || normalState.caretColor === "rgba(0, 0, 0, 0)",
      `normal mode must hide the native caret, got ${normalState.caretColor}`);
    assert(normalState.overlayCount === 0 && normalState.selectionLength === 1,
      `normal mode must use the one-character block, overlay=${normalState.overlayCount} selection=${normalState.selectionLength}`);
    await page.screenshot({ path: path.join(SCREENSHOT_DIR, "wiki-402-normal.png") });

    await composer.press("i");
    await page.waitForFunction(() => {
      const el = document.querySelector(".session-composer textarea");
      if (!(el instanceof HTMLTextAreaElement)) return false;
      const caretColor = getComputedStyle(el).caretColor;
      return el.classList.contains("is-vim-insert")
        && el.selectionEnd === el.selectionStart
        && caretColor !== "transparent"
        && caretColor !== "rgba(0, 0, 0, 0)"
        && document.querySelectorAll(".session-empty-block-cursor").length === 0;
    });

    console.log("WIKI-402 playwright: insert bar, normal block, and mode round-trip passed");
  } finally {
    await browser.close();
    await backend.stop();
    await fs.rm(fixtures.root, { recursive: true, force: true });
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
