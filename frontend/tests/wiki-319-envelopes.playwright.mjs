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

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const OUT_DIR = path.join(ROOT, ".playwright-mcp", "wiki-319");
const VIEWPORT = { width: 1440, height: 900 };
const LAYOUT = (ticket) => ({
  version: 2,
  activeWindowId: "window-0",
  windows: [{
    id: "window-0",
    focusedPaneId: "pane-1",
    layout: { kind: "pane", id: "pane-1", path: `agent://${ticket}` },
  }],
});

function timestamp(offsetSeconds) {
  return new Date(Date.parse("2026-08-17T16:00:00Z") + offsetSeconds * 1000).toISOString();
}

function sessionMeta(id) {
  return {
    type: "session_meta",
    timestamp: timestamp(0),
    payload: { session_id: id, id, cwd: ROOT },
  };
}

function userMessage(message, offsetSeconds = 1) {
  return {
    type: "event_msg",
    timestamp: timestamp(offsetSeconds),
    payload: { type: "user_message", message },
  };
}

function assistantMessage(message, offsetSeconds) {
  return {
    type: "event_msg",
    timestamp: timestamp(offsetSeconds),
    payload: { type: "agent_message", message },
  };
}

function reasoning(id) {
  return {
    type: "response_item",
    timestamp: timestamp(id + 1),
    payload: {
      type: "reasoning",
      id: `rs_wiki316_${id}`,
      summary: [{ text: `**thought ${id}**\nbody for thought ${id}` }],
      encrypted_content: `opaque-${id}`,
    },
  };
}

async function writeTranscript(target, rows) {
  await fs.writeFile(target, rows.map((row) => JSON.stringify(row)).join("\n") + "\n");
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

const fixtures = makeFixtureRoot("wiki-319-envelopes-");
const archived = path.join(fixtures.root, "wiki-316-archived.jsonl");
const current = path.join(fixtures.root, "wiki-319-current.jsonl");
let backend;
let browser;

try {
  mkdirSync(OUT_DIR, { recursive: true });
  await writeTranscript(archived, [
    sessionMeta("wiki-316-archived"),
    userMessage("Inspect the archived thought run.", 1),
    ...Array.from({ length: 81 }, (_, index) => reasoning(index + 1)),
  ]);
  const longMessage = [
    "<recommended_plugins>\n- GitHub\n- Linear\n</recommended_plugins>",
    "<WIKI_RUNTIME_CARD>\nrun_id=wiki-319-current\nrole=implement\n</WIKI_RUNTIME_CARD>",
    "Human task: preserve every detail while compressing the session envelope.",
    ...Array.from({ length: 36 }, (_, index) => `long task line ${index + 1}`),
  ].join("\n");
  await writeTranscript(current, [
    sessionMeta("wiki-319-current"),
    userMessage(longMessage, 1),
    assistantMessage(Array.from({ length: 30 }, (_, index) => `current output line ${index + 1}`).join("\n"), 42),
  ]);
  writeRegistry(fixtures.registryPath, [["WIKI-316", archived], ["WIKI-319", current]]);
  writeQueue(fixtures.queuePath, "WIKI-316", []);
  writeQueue(fixtures.queuePath, "WIKI-319", []);

  backend = await startBackend(fixtures);
  browser = await chromium.launch({ headless: true });

  for (const theme of ["mono-dark", "mono-light"]) {
    for (const [ticket, prefix] of [["WIKI-316", "archived"], ["WIKI-319", "current"]]) {
      const context = await browser.newContext({ viewport: VIEWPORT });
      const page = await context.newPage();
      try {
        await page.addInitScript(({ layout, selectedTheme }) => {
          localStorage.setItem("wiki-theme", selectedTheme);
          localStorage.setItem("wiki-sidebar-visible", "false");
          localStorage.setItem("wiki-window-layout-v2", JSON.stringify(layout));
        }, { layout: LAYOUT(ticket), selectedTheme: theme });
        await page.goto(`${backend.baseUrl}/#/agent/${ticket}`, { waitUntil: "domcontentloaded" });
        await page.locator(".agent-session-surface").waitFor({ state: "visible" });
        await page.locator(".session-scroll-inner").waitFor({ state: "visible" });
        if (ticket === "WIKI-316") {
          const group = page.locator(".session-thinking-group");
          await group.waitFor({ state: "visible" });
          assert((await group.locator(".session-thinking-group-head").innerText()).includes("80 thoughts"), `${theme}: archived group count missing`);
          await page.screenshot({ path: path.join(OUT_DIR, `${prefix}-${theme}-collapsed.png`), fullPage: true });
          await group.locator(".session-thinking-group-head").dispatchEvent("click");
          await page.locator(".session-thinking-group-rows .session-thinking").first().waitFor({ state: "attached" });
          assert(await group.locator(".session-thinking-title").count() === 80, `${theme}: expanded archived row count changed`);
          await page.screenshot({ path: path.join(OUT_DIR, `${prefix}-${theme}-expanded.png`), fullPage: true });
        } else {
          await page.getByRole("button", { name: "Show more" }).waitFor({ state: "visible" });
          assert(await page.locator(".session-envelope").count() === 2, `${theme}: known envelope count changed`);
          await page.screenshot({ path: path.join(OUT_DIR, `${prefix}-${theme}.png`), fullPage: true });
        }
      } finally {
        await context.close();
      }
    }
  }
} finally {
  await browser?.close();
  await backend?.stop();
  rmSync(fixtures.root, { recursive: true, force: true });
}
