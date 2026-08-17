// WIKI-333: live type-tier sweep. #268's wiki-143 probe matrix guards the
// classes the WIKI-329 audit flagged; this suite guards the open set — every
// visible text leaf on the swept surfaces must compute on the 15/13/10 tiers
// with weight <= 600, so NEW off-tier text fails here even if its class was
// never audited.
import { mkdirSync, rmSync } from "node:fs";
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

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR
  ? path.resolve(process.env.WIKI_PLAYWRIGHT_OUT_DIR)
  : path.join(ROOT, ".playwright-mcp", "wiki-332");
const VIEWPORT = { width: 1440, height: 900 };
const TICKET = "WIKI-332";
const TIERS = ["15px", "13px", "10px"];

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function layout() {
  return {
    version: 2,
    activeWindowId: "window-0",
    windows: [{
      id: "window-0",
      focusedPaneId: "pane-1",
      layout: { kind: "pane", id: "pane-1", path: `agent://${TICKET}` },
    }],
  };
}

async function writeTranscript(target) {
  const rows = [
    { type: "mode", mode: "normal", sessionId: TICKET },
    {
      type: "user",
      timestamp: "2026-08-17T16:00:00.000Z",
      message: { role: "user", content: "Sweep the type tiers." },
    },
    {
      type: "assistant",
      timestamp: "2026-08-17T16:00:01.000Z",
      message: { role: "assistant", content: "Every straggler lands on a tier." },
    },
  ];
  await fs.writeFile(target, rows.map((row) => JSON.stringify(row)).join("\n") + "\n");
}

const fixtures = makeFixtureRoot("wiki-332-tier-sweep-");
const transcript = path.join(fixtures.root, "wiki-332.jsonl");
let backend;
let browser;

try {
  mkdirSync(OUT_DIR, { recursive: true });
  await writeTranscript(transcript);
  writeRegistry(fixtures.registryPath, [[TICKET, transcript]]);
  writeQueue(fixtures.queuePath, TICKET, []);

  backend = await startBackend(fixtures);
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: VIEWPORT });
  const page = await context.newPage();

  await page.addInitScript((storedLayout) => {
    localStorage.setItem("wiki-theme", "bb-dark");
    localStorage.setItem("wiki-sidebar-visible", "true");
    localStorage.setItem("wiki-window-layout-v2", JSON.stringify(storedLayout));
  }, layout());
  await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
  await page.locator(".agent-session-surface").waitFor({ state: "visible" });

  const sweep = async (scope) =>
    page.evaluate(({ selector, sizes }) => {
      const root = document.querySelector(selector);
      if (!root) return { missing: true, offenders: [] };
      const offenders = [];
      for (const element of root.querySelectorAll("*")) {
        const hasText = [...element.childNodes].some(
          (node) => node.nodeType === Node.TEXT_NODE && node.textContent.trim().length > 0,
        );
        if (!hasText) continue;
        const rect = element.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) continue;
        const style = getComputedStyle(element);
        if (style.visibility === "hidden" || style.display === "none") continue;
        if (!sizes.includes(style.fontSize)) {
          offenders.push(`${element.className || element.tagName}: ${style.fontSize}`);
        }
        if (Number(style.fontWeight) > 600) {
          offenders.push(`${element.className || element.tagName}: weight ${style.fontWeight}`);
        }
      }
      return { missing: false, offenders: offenders.slice(0, 20) };
    }, { selector: scope, sizes: TIERS });

  for (const scope of [".agent-session-surface", ".status-bar"]) {
    const { missing, offenders } = await sweep(scope);
    assert(!missing, `sweep scope not found: ${scope}`);
    assert(
      offenders.length === 0,
      `${scope} has off-tier text: ${JSON.stringify(offenders)}`,
    );
  }

  await page.getByRole("button", { name: "Settings" }).click();
  await page.locator(".bb-dialog").waitFor({ state: "visible" });
  const dialogSweep = await sweep(".bb-dialog");
  assert(
    dialogSweep.offenders.length === 0,
    `settings dialog has off-tier text: ${JSON.stringify(dialogSweep.offenders)}`,
  );
  await page.screenshot({ path: path.join(OUT_DIR, "bb-dark-settings-tiers.png") });

  console.log("wiki-332 tier sweep passed");
  await context.close();
} finally {
  await browser?.close();
  await backend?.stop();
  rmSync(fixtures.root, { recursive: true, force: true });
}
