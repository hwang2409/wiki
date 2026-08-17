import fs from "node:fs/promises";
import { mkdirSync, rmSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

import {
  codexAssistant,
  makeFixtureRoot,
  startBackend,
  writeQueue,
  writeRegistry,
} from "../scripts/wiki32-harness.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || path.join(ROOT, ".playwright-mcp", "wiki-330");
const TICKET = "WIKI-330";
const VIEWPORT = { width: 1440, height: 900 };
const LAYOUT = {
  version: 2,
  activeWindowId: "window-0",
  windows: [{
    id: "window-0",
    focusedPaneId: "pane-1",
    layout: { kind: "pane", id: "pane-1", path: `agent://${TICKET}` },
  }],
};

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function timestamp(offsetSeconds) {
  return new Date(Date.parse("2026-08-17T16:00:00Z") + offsetSeconds * 1000).toISOString();
}

function reasoning(id) {
  return {
    type: "response_item",
    timestamp: timestamp(id),
    payload: {
      type: "reasoning",
      id: `rs_wiki330_${id}`,
      summary: [{
        text: `**thought ${id}**\n${Array.from({ length: 12 }, (_, line) => `body ${id} line ${line + 1}`).join("\n")}`,
      }],
      encrypted_content: `opaque-${id}`,
    },
  };
}

function toolCall() {
  return {
    type: "response_item",
    timestamp: timestamp(5),
    payload: {
      type: "function_call",
      call_id: "call_wiki330_read",
      name: "Read",
      arguments: JSON.stringify({ file_path: "frontend/src/session-layout.ts" }),
    },
  };
}

function toolOutput() {
  return {
    type: "response_item",
    timestamp: timestamp(6),
    payload: {
      type: "function_call_output",
      call_id: "call_wiki330_read",
      output: "session layout source",
    },
  };
}

const fixtures = makeFixtureRoot("wiki-330-thought-height-");
const transcript = path.join(fixtures.root, "wiki-330.jsonl");
let backend;
let browser;

try {
  mkdirSync(OUT_DIR, { recursive: true });
  const rows = [
    {
      type: "session_meta",
      timestamp: timestamp(0),
      payload: { session_id: "wiki-330-thought-height", id: "wiki-330-thought-height", cwd: ROOT },
    },
    ...[1, 2, 3, 4].map(reasoning),
    toolCall(),
    toolOutput(),
    codexAssistant("The next transcript row stays readable.", timestamp(7)),
  ];
  await fs.writeFile(transcript, rows.map((row) => JSON.stringify(row)).join("\n") + "\n");
  writeRegistry(fixtures.registryPath, [[TICKET, transcript]]);
  writeQueue(fixtures.queuePath, TICKET, []);

  backend = await startBackend(fixtures);
  browser = await chromium.launch({ headless: true });

  for (const theme of ["bb-dark", "macos-dark", "bb-light"]) {
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
      const group = page.locator(".session-thinking-group");
      await group.waitFor({ state: "visible" });
      assert((await group.locator(".session-thinking-group-head").innerText()).includes("3 thoughts"), `${theme}: group summary missing`);
      await page.screenshot({ path: path.join(OUT_DIR, `${theme}-collapsed.png`), fullPage: true });

      await group.locator(".session-thinking-group-head").click();
      await group.locator(".session-thinking-group-rows .session-thinking").first().waitFor({ state: "visible" });
      assert(await group.locator(".session-thinking-title").count() === 3, `${theme}: expanded thought count changed`);
      await page.waitForFunction(() => {
        const rows = [...document.querySelectorAll(".session-virtual-row")];
        const groupRow = rows.find((row) => row.querySelector(".session-thinking-group"));
        const groupIndex = groupRow ? rows.indexOf(groupRow) : -1;
        const nextRow = groupIndex >= 0 ? rows[groupIndex + 1] : null;
        if (!groupRow || !nextRow) return false;
        return groupRow.getBoundingClientRect().bottom <= nextRow.getBoundingClientRect().top + 1;
      });
      await page.screenshot({ path: path.join(OUT_DIR, `${theme}-expanded.png`), fullPage: true });
    } finally {
      await context.close();
    }
  }
} finally {
  await browser?.close();
  await backend?.stop();
  rmSync(fixtures.root, { recursive: true, force: true });
}
