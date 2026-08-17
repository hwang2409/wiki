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
const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR
  ? path.resolve(process.env.WIKI_PLAYWRIGHT_OUT_DIR)
  : path.join(ROOT, ".playwright-mcp", "wiki-331");
const VIEWPORT = { width: 1440, height: 900 };
const TICKET = "WIKI-331";

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

function timestamp(offsetSeconds) {
  return new Date(Date.parse("2026-08-17T16:00:00Z") + offsetSeconds * 1000).toISOString();
}

async function writeTranscript(target) {
  const envelope = [
    "<WIKI_RUNTIME_CARD>",
    "ticket=WIKI-331 role=implement",
    "</WIKI_RUNTIME_CARD>",
    "Human task: inspect the closing audit.",
  ].join("\n");
  const rows = [
    { type: "mode", mode: "normal", sessionId: TICKET },
    {
      type: "user",
      timestamp: timestamp(0),
      message: { role: "user", content: envelope },
    },
    {
      type: "assistant",
      timestamp: timestamp(1),
      message: { role: "assistant", content: "The audit is ready." },
    },
  ];
  await fs.writeFile(target, rows.map((row) => JSON.stringify(row)).join("\n") + "\n");
}

const fixtures = makeFixtureRoot("wiki-331-closing-fixes-");
const transcript = path.join(fixtures.root, "wiki-331.jsonl");
let backend;
let browser;

try {
  mkdirSync(OUT_DIR, { recursive: true });
  await writeTranscript(transcript);
  writeRegistry(fixtures.registryPath, [[TICKET, transcript]]);
  writeQueue(fixtures.queuePath, TICKET, []);

  backend = await startBackend(fixtures);
  browser = await chromium.launch({ headless: true });

  for (const theme of ["bb-dark", "macos-dark", "bb-light"]) {
    const context = await browser.newContext({ viewport: VIEWPORT });
    const page = await context.newPage();
    try {
      await page.addInitScript(({ selectedTheme, storedLayout }) => {
        localStorage.setItem("wiki-theme", selectedTheme);
        localStorage.setItem("wiki-font", "JetBrains Mono");
        localStorage.setItem("wiki-sidebar-visible", "true");
        localStorage.setItem("wiki-sidebar-tab", "files");
        localStorage.setItem("wiki-window-layout-v2", JSON.stringify(storedLayout));
      }, { selectedTheme: theme, storedLayout: layout() });
      await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
      await page.locator(".agent-session-surface").waitFor({ state: "visible" });

      const family = await page.evaluate(() => {
        const fixture = document.createElement("div");
        fixture.hidden = true;
        const raw = document.createElement("pre");
        raw.className = "session-envelope-raw";
        const command = document.createElement("code");
        command.className = "codex-stream-command-input";
        raw.textContent = "raw envelope";
        command.textContent = "stdin";
        fixture.append(raw, command);
        document.body.append(fixture);
        return {
          raw: getComputedStyle(raw).fontFamily,
          command: getComputedStyle(command).fontFamily,
        };
      });
      assert(family.raw === family.command, `${theme}: transcript leaves use different families`);
      assert(family.raw.includes("JetBrains Mono"), `${theme}: selected family is missing: ${family.raw}`);

      await page.getByRole("button", { name: "Settings" }).click();
      await page.locator(".theme-grid").waitFor({ state: "visible" });
      const themeCounts = await page.evaluate(() => ({
        choices: document.querySelectorAll(".theme-choice").length,
        swatches: [...document.querySelectorAll(".theme-choice")].map(
          (choice) => choice.querySelectorAll(".theme-choice-swatch").length,
        ),
      }));
      assert(themeCounts.choices === 17, `${theme}: expected all theme choices`);
      assert(themeCounts.swatches.every((count) => count === 4), `${theme}: a theme lost a swatch`);
      await page.screenshot({
        path: path.join(OUT_DIR, `${theme}-settings.png`),
        fullPage: false,
      });
      await page.getByRole("button", { name: "Close" }).click();
    } finally {
      await context.close();
    }
  }
} finally {
  await browser?.close();
  await backend?.stop();
  rmSync(fixtures.root, { recursive: true, force: true });
}
