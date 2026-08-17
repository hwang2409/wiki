import fs from "node:fs/promises";
import { mkdirSync, rmSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

import {
  makeFixtureRoot,
  startBackend,
  writeQueue,
  writeRegistry,
} from "../scripts/wiki32-harness.mjs";

const TICKET = "WIKI-315";
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const OUT_DIR = path.join(ROOT, ".playwright-mcp", "wiki-315");
const VIEWPORT = { width: 1440, height: 900 };
const LAYOUT = {
  version: 2,
  activeWindowId: "window-0",
  windows: [
    {
      id: "window-0",
      focusedPaneId: "pane-1",
      layout: { kind: "pane", id: "pane-1", path: `agent://${TICKET}` },
    },
  ],
};

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function writeTranscript(target) {
  const rows = [
    {
      type: "session_meta",
      timestamp: "2026-08-17T16:00:00Z",
      payload: {
        session_id: "wiki-315-thought-markers",
        id: "wiki-315-thought-markers",
        cwd: ROOT,
      },
    },
    {
      type: "response_item",
      timestamp: "2026-08-17T16:00:01Z",
      payload: {
        type: "reasoning",
        id: "rs_wiki315_markers",
        summary: [
          {
            text:
              "**diagnosing duplicate sed output and planning harness simplification****planning incremental dashboard…",
          },
        ],
        encrypted_content: "opaque-codex-reasoning",
      },
    },
    {
      type: "response_item",
      timestamp: "2026-08-17T16:00:02Z",
      payload: {
        type: "reasoning",
        id: "rs_wiki315_clean",
        summary: [{ text: "planning the next verification step" }],
        encrypted_content: "opaque-clean-reasoning",
      },
    },
  ];
  await fs.writeFile(target, rows.map((row) => JSON.stringify(row)).join("\n") + "\n");
}

const fixtures = makeFixtureRoot("wiki-315-thought-markers-");
const transcript = path.join(fixtures.root, "wiki-315-thought-markers.jsonl");
let backend;
let browser;

try {
  mkdirSync(OUT_DIR, { recursive: true });
  await writeTranscript(transcript);
  writeRegistry(fixtures.registryPath, [[TICKET, transcript]]);
  writeQueue(fixtures.queuePath, TICKET, []);

  backend = await startBackend(fixtures);
  browser = await chromium.launch({ headless: true });

  for (const theme of ["mono-light", "mono-dark"]) {
    const context = await browser.newContext({ viewport: VIEWPORT });
    const page = await context.newPage();
    try {
      await page.addInitScript(({ layout, selectedTheme }) => {
        localStorage.setItem("wiki-theme", selectedTheme);
        localStorage.setItem("wiki-sidebar-visible", "false");
        localStorage.setItem("wiki-window-layout-v2", JSON.stringify(layout));
      }, { layout: LAYOUT, selectedTheme: theme });
      await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
      await page.locator(".agent-session-surface").waitFor({ state: "visible" });
      await page.locator(".session-thinking-head").first().waitFor({ state: "visible" });

      const heads = page.locator(".session-thinking-head");
      assert(await heads.count() === 2, `${theme}: expected two thought rows`);
      const firstHead = heads.first();
      const firstTitle = firstHead.locator(".session-thinking-title");
      const firstTitleText = await firstTitle.innerText();
      assert(
        firstTitleText ===
          "diagnosing duplicate sed output and planning harness simplification · planning incremental dashboard…",
        `${theme}: unexpected normalized preview: ${JSON.stringify(firstTitleText)}`,
      );
      assert(!(await firstHead.innerText()).includes("**"), `${theme}: preview leaked bold markers`);
      const chip = firstHead.locator(".session-thinking-chip");
      assert(
        (await chip.count()) === 1 && (await chip.innerText()).toLowerCase() === "encrypted",
        `${theme}: encrypted chip missing (head=${JSON.stringify(await firstHead.innerText())}, chips=${await chip.count()})`,
      );
      assert(
        await heads.nth(1).locator(".session-thinking-title").innerText() === "planning the next verification step",
        `${theme}: clean thought line changed`,
      );

      await page.screenshot({
        path: path.join(OUT_DIR, `thought-markers-${theme}-collapsed.png`),
      });

      await firstHead.click();
      const body = page.locator(".session-thinking").first();
      await body.waitFor({ state: "visible" });
      assert(await body.locator("strong").count() === 2, `${theme}: markdown emphasis did not render`);
      const bodyText = await body.innerText();
      assert(!bodyText.includes("**"), `${theme}: expanded body leaked markdown markers`);
      assert(bodyText.includes("planning incremental dashboard…"), `${theme}: expanded summary is incomplete`);

      await page.screenshot({
        path: path.join(OUT_DIR, `thought-markers-${theme}-expanded.png`),
      });
      console.error(`[wiki-315-playwright] ${theme} thought rows verified`);
    } finally {
      await context.close();
    }
  }
} finally {
  await browser?.close();
  await backend?.stop();
  rmSync(fixtures.root, { recursive: true, force: true });
}
