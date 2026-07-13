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

const TICKET = "WIKI-97";
const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || "/tmp/wiki-97-playwright-evidence";

function logStep(message) {
  console.error(`[wiki-97-playwright] ${message}`);
}

async function writeTranscript(target) {
  const rows = [
    { type: "mode", mode: "normal", sessionId: "wiki-97-transcript-markdown" },
    codexAssistant(
      [
        "**Session & transcript**",
        "11. Time-travel forks — scrub session like video.",
        "12. Session diffing — compare two workers.",
        "",
        "**Table section**",
        "Label | Value",
        "--- | ---",
        "Alpha | Beta",
        "",
        "**Nested list section**",
        "1. Parent item",
        "  - Child one",
        "  - Child two",
        "2. Next item",
        "",
        "**Another section**",
        "1. Already working.",
        "2. Still a list.",
        "",
        "```text",
        "11. something inside a fence",
        "```",
        "",
        "Inline `11. x` should stay inline.",
      ].join("\n"),
      "2026-07-13T16:00:00Z"
    ),
  ];
  await fs.writeFile(target, rows.map((row) => JSON.stringify(row)).join("\n") + "\n");
}

async function main() {
  logStep("preparing isolated fixtures");
  await fs.mkdir(OUT_DIR, { recursive: true });
  const fixtures = makeFixtureRoot("wiki-97-transcript-markdown-");
  const transcript = path.join(fixtures.root, "wiki-97-transcript-markdown.jsonl");
  await writeTranscript(transcript);
  writeRegistry(fixtures.registryPath, [[TICKET, transcript]]);
  writeQueue(fixtures.queuePath, TICKET, []);

  logStep("starting isolated backend");
  const backend = await startBackend(fixtures);
  logStep(`backend ready at ${backend.baseUrl}`);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 1200 } });

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
              layout: { kind: "pane", id: "pane-1", path: "agent://WIKI-97" },
            },
          ],
        })
      );
    });
    await page.goto(`${backend.baseUrl}/#/agent/WIKI-97`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".session-scroll");

    const assistant = page.locator(".session-assistant").first();
    await assistant.waitFor({ state: "visible" });
    await assistant.locator("strong", { hasText: "Session & transcript" }).waitFor({ state: "visible" });
    const orderedLists = assistant.locator("ol");
    await orderedLists.first().waitFor({ state: "visible" });
    await orderedLists.nth(1).waitFor({ state: "visible" });
    await orderedLists.first().getByText("Time-travel forks").waitFor({ state: "visible" });
    await assistant.locator("table").waitFor({ state: "visible" });
    await assistant.locator("table").getByText("Label").waitFor({ state: "visible" });
    await assistant.locator("table").getByText("Alpha").waitFor({ state: "visible" });
    await assistant.locator("ol").nth(1).locator("ul").waitFor({ state: "visible" });
    await assistant.locator("ol").nth(1).locator("ul > li").first().getByText("Child one").waitFor({ state: "visible" });
    await assistant.getByText("Still a list.", { exact: true }).waitFor({ state: "visible" });
    await assistant.locator("pre code", { hasText: "11. something inside a fence" }).waitFor({ state: "visible" });
    await assistant.locator("code", { hasText: "11. x" }).waitFor({ state: "visible" });
    const firstListStart = await orderedLists.first().evaluate((element) => element.getAttribute("start"));
    if (firstListStart !== "11") {
      throw new Error(`Expected the repaired ordered list to preserve start=11, saw ${firstListStart}`);
    }
    const secondListStart = await orderedLists.nth(1).evaluate((element) => element.getAttribute("start"));
    if (secondListStart !== null) {
      throw new Error(`Expected the plain ordered list to omit a start attribute for item 1, saw ${secondListStart}`);
    }

    const screenshotPath = path.join(OUT_DIR, "wiki-97-transcript-markdown.png");
    logStep("capturing transcript screenshot");
    await page.locator(".session-scroll").screenshot({ path: screenshotPath });

    await fs.writeFile(
      path.join(OUT_DIR, "summary.json"),
      JSON.stringify(
        {
          screenshot: screenshotPath,
          listRoot: "ordered list starts at 11 after a bold lead-in",
          table: "table renders after prose without literal pipes",
          nestedList: "two-space child bullets nest under ordered items",
        },
        null,
        2
      )
    );
  } finally {
    await browser.close();
    await backend.stop();
  }
}

await main();
