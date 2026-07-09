import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { chromium } from "playwright";

import {
  codexAssistant,
  codexUser,
  makeFixtureRoot,
  startBackend,
  writeQueue,
  writeRegistry,
} from "./wiki32-harness.mjs";

const OUT_PATH = "/tmp/wiki-24-window-state-memory.json";

function buildLayout() {
  return {
    version: 2,
    activeWindowId: "window-0",
    windows: Array.from({ length: 6 }, (_, index) => ({
      id: `window-${index}`,
      focusedPaneId: `pane-${index + 1}`,
      layout: { kind: "pane", id: `pane-${index + 1}`, path: `agent://WIKI-3${2 + index}` },
    })),
  };
}

async function leaderChord(page, key) {
  await page.keyboard.press("Control+a");
  await page.keyboard.press(key);
}

async function openAgentPage(page, baseUrl, layout) {
  await page.addInitScript(({ storedLayout }) => {
    localStorage.setItem("wiki-window-layout-v2", JSON.stringify(storedLayout));
    localStorage.setItem("wiki-sidebar-visible", "false");
  }, { storedLayout: layout });
  await page.goto(`${baseUrl}/#/agent/WIKI-32`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".session-scroll");
}

async function main() {
  const fixtures = makeFixtureRoot("wiki24-window-state-memory-");
  process.env.WIKI_UI_STATE_PATH = join(fixtures.root, "ui-state.json");
  const layout = buildLayout();
  const tickets = [];

  for (let index = 0; index < 6; index += 1) {
    const ticket = `WIKI-3${2 + index}`;
    const transcript = join(fixtures.root, `${ticket}.jsonl`);
    const rows = [codexUser(`warmup ${ticket}`, "2026-07-09T00:00:00Z")];
    for (let message = 0; message < 48; message += 1) {
      rows.push(
        codexAssistant(
          `${ticket} msg ${message} ${"x".repeat(4000)}`,
          `2026-07-09T00:${String((message + 1) % 60).padStart(2, "0")}:00Z`,
        ),
      );
    }
    writeFileSync(transcript, rows.map((row) => JSON.stringify(row)).join("\n") + "\n");
    tickets.push([ticket, transcript]);
  }

  writeRegistry(fixtures.registryPath, tickets);
  writeQueue(fixtures.queuePath, "WIKI-32");

  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({
    headless: true,
    args: ["--enable-precise-memory-info", "--js-flags=--expose-gc"],
  });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const sessionResponses = [];
  page.on("response", (response) => {
    if (!response.url().includes("/api/agents/WIKI-3")) return;
    sessionResponses.push(response.url());
  });

  try {
    await openAgentPage(page, backend.baseUrl, layout);
    await page.waitForTimeout(1200);
    for (let index = 1; index < 6; index += 1) {
      await leaderChord(page, String(index));
      await page.waitForSelector(".session-scroll");
      await page.waitForTimeout(1200);
    }
    await leaderChord(page, "0");
    await page.waitForSelector(".session-scroll");
    await page.waitForTimeout(1200);
    await page.evaluate(async () => {
      for (let index = 0; index < 3; index += 1) {
        globalThis.gc?.();
        await new Promise((resolve) => setTimeout(resolve, 50));
      }
    });
    const cdp = await page.context().newCDPSession(page);
    const heap = await cdp.send("Runtime.getHeapUsage");
    const result = {
      fixtureRoot: fixtures.root,
      fixtures: {
        registry: fixtures.registryPath,
        queue: fixtures.queuePath,
        statusDir: fixtures.statusDir,
        transcripts: Object.fromEntries(tickets),
      },
      windowsVisited: 6,
      transcriptMessagesPerWindow: 49,
      sessionResponsesObserved: sessionResponses.length,
      usedJSHeapBytes: heap.usedSize,
      totalJSHeapBytes: heap.totalSize,
      usedJSHeapMB: Number((heap.usedSize / (1024 * 1024)).toFixed(2)),
    };
    writeFileSync(OUT_PATH, JSON.stringify(result, null, 2));
    console.log(JSON.stringify(result, null, 2));
  } finally {
    await browser.close();
    await backend.stop();
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
