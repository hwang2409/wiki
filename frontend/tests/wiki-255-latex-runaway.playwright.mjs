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

const TICKET = "WIKI-255";
const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || "/tmp/wiki-255-playwright-evidence";
const RUNAWAY_TEXT =
  "- numbers $ (S4=34 > S1=25) earn more per item. if two chains cost the same to upgrade, feed the high-seller first $";

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function main() {
  await fs.mkdir(OUT_DIR, { recursive: true });
  const fixtures = makeFixtureRoot("wiki-255-latex-runaway-");
  const transcript = path.join(fixtures.root, "wiki-255-latex-runaway.jsonl");
  await fs.writeFile(
    transcript,
    [
      { type: "mode", mode: "normal", sessionId: "wiki-255-latex-runaway" },
      codexAssistant(RUNAWAY_TEXT, "2026-08-06T14:00:00Z"),
    ]
      .map((row) => JSON.stringify(row))
      .join("\n") + "\n",
  );
  writeRegistry(fixtures.registryPath, [[TICKET, transcript]]);
  writeQueue(fixtures.queuePath, TICKET, []);

  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });

  try {
    await page.addInitScript(() => {
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
              layout: { kind: "pane", id: "pane-1", path: "agent://WIKI-255" },
            },
          ],
        }),
      );
    });

    await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
    const assistant = page.locator(".session-assistant").first();
    await assistant.waitFor({ state: "visible" });
    await assistant.getByText("numbers", { exact: false }).waitFor({ state: "visible" });
    assert(await assistant.locator(".katex").count() === 0, "runaway prose rendered as KaTeX");
    assert((await assistant.innerText()).includes(RUNAWAY_TEXT.slice(2)), "runaway text was not rendered as prose");

    await page.locator(".session-scroll").screenshot({
      path: path.join(OUT_DIR, "wiki-255-latex-runaway.png"),
    });
  } finally {
    await browser.close();
    await backend.stop();
  }
}

await main();
