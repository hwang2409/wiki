// WIKI-304: capture the bb-grammar transcript rows (compact tool activity,
// event-code-block output, marker info-rows) at 1440x900 across four themes
// on a real archived-style session with many tool calls.
import fs from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { chromium } from "playwright";

const FRONTEND_ROOT = process.env.WIKI_FRONTEND_ROOT || process.cwd();
const harness = await import(
  pathToFileURL(path.resolve(FRONTEND_ROOT, "scripts", "wiki32-harness.mjs")).href
);
const { makeFixtureRoot, startBackend, writeQueue, writeRegistry } = harness;

const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR
  || path.resolve(FRONTEND_ROOT, "..", ".playwright-mcp", "wiki-304");
const TICKET = "WIKI-304";

function ts(second) {
  return `2026-08-17T12:00:${String(second).padStart(2, "0")}.000Z`;
}

function assistant(second, content) {
  return {
    type: "assistant",
    timestamp: ts(second),
    message: { role: "assistant", content },
  };
}

function toolResult(second, toolId, content, isError = false) {
  return {
    type: "user",
    timestamp: ts(second),
    message: {
      role: "user",
      content: [{ type: "tool_result", tool_use_id: toolId, content, is_error: isError }],
    },
  };
}

const LONG_OUTPUT = [
  "app/Actions/Fortify/CreateNewUser.php",
  ...Array.from({ length: 24 }, (_, i) =>
    `app/Providers/generated/Provider${String(i + 1).padStart(2, "0")}.php: registered`,
  ),
].join("\n");

const TRANSCRIPT = [
  { type: "mode", mode: "normal", sessionId: `fixture-${TICKET}` },
  { type: "custom-title", customTitle: `${TICKET} bb transcript rows`, sessionId: `fixture-${TICKET}` },
  {
    type: "user",
    timestamp: ts(0),
    message: {
      role: "user",
      content: "adopt bb activity-row grammar for tool calls and bash output",
    },
  },
  assistant(1, [
    { type: "text", text: "I'll audit the transcript surfaces and apply the bb row grammar to tool calls, bash output, and marker rows." },
    { type: "tool_use", id: "toolu_read", name: "Read", input: { file_path: "frontend/src/session.tsx" } },
  ]),
  toolResult(2, "toolu_read", [
    "export function ToolCallRow({ event, nested = false, onInspect, ticket, withResult }) {",
    "  const tool = event.tool!;",
    "  const status = toolStatus(tool);",
    "  return (",
    "    <div className={`session-tool session-activity-row is-tool is-${status}`}>",
    "      <div className=\"session-tool-head\">",
    "        …",
    "      </div>",
    "    </div>",
    "  );",
    "}",
  ].join("\n")),
  assistant(3, [
    { type: "tool_use", id: "toolu_grep", name: "Grep", input: { pattern: "session-tool-body", path: "frontend/src/styles.css" } },
  ]),
  toolResult(4, "toolu_grep", "frontend/src/styles.css:7871:.session-tool-body {"),
  assistant(5, [
    { type: "tool_use", id: "toolu_bash", name: "Bash", input: { command: "cd frontend && npm run test:component", description: "Component tests" } },
  ]),
  toolResult(6, "toolu_bash", LONG_OUTPUT),
  assistant(7, [
    { type: "tool_use", id: "toolu_fail", name: "Bash", input: { command: "npm run test:missing-target", description: "Missing target" } },
  ]),
  toolResult(8, "toolu_fail", "npm ERR! Missing script: \"test:missing-target\"", true),
  { type: "marker", timestamp: ts(9), text: "model changed to claude-opus-4-7", marker: "model_changed" },
  { type: "marker", timestamp: ts(10), text: "permissions elevated for Bash tool", marker: "permissions" },
  assistant(11, [
    { type: "text", text: "Component tests pass. The row grammar is aligned; expansion follows the event-code-block shape." },
  ]),
];

async function writeJsonl(target, rows) {
  await fs.writeFile(target, rows.map((row) => JSON.stringify(row)).join("\n") + "\n");
}

async function preparePage(context, theme) {
  const page = await context.newPage();
  await page.addInitScript(({ layout, theme }) => {
    localStorage.setItem("wiki-window-layout-v2", JSON.stringify(layout));
    localStorage.setItem("wiki-sidebar-visible", "false");
    localStorage.setItem("wiki-theme", theme);
  }, {
    theme,
    layout: {
      version: 2,
      activeWindowId: "window-0",
      windows: [
        { id: "window-0", focusedPaneId: "pane-1", layout: { kind: "pane", id: "pane-1", path: `agent://${TICKET}` } },
      ],
    },
  });
  return page;
}

async function settle(page) {
  await page.waitForSelector(".session-scroll", { state: "attached", timeout: 30_000 });
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(400);
}

async function openLegacyDisclosures(page) {
  for (let round = 0; round < 8; round += 1) {
    let clicked = 0;
    for (const selector of [
      ".session-activity-head[aria-expanded='false']",
      ".session-tool-head[aria-expanded='false']",
    ]) {
      for (const head of await page.locator(selector).all()) {
        await head.scrollIntoViewIfNeeded().catch(() => {});
        await head.click({ force: true }).catch(() => {});
        clicked += 1;
      }
    }
    if (clicked === 0) break;
    await page.waitForTimeout(120);
  }
}

const THEMES = ["mono-light", "mono-dark", "opencode", "tokyo-night"];

async function main() {
  await fs.mkdir(OUT_DIR, { recursive: true });
  const fixtures = makeFixtureRoot("wiki-304-");
  const transcript = path.join(fixtures.root, "wiki-304-claude.jsonl");
  await writeJsonl(transcript, TRANSCRIPT);
  writeRegistry(fixtures.registryPath, [[TICKET, transcript]]);
  writeQueue(fixtures.queuePath, TICKET, []);

  const backend = await startBackend(fixtures);
  try {
    const browser = await chromium.launch({ headless: true });
    try {
      for (const theme of THEMES) {
        const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
        const page = await preparePage(context, theme);
        await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
        await settle(page);
        await openLegacyDisclosures(page);
        await page.waitForTimeout(200);
        // Scroll to top so head of transcript is visible.
        await page.evaluate(() => {
          const scroller = document.querySelector(".session-scroll");
          if (scroller) scroller.scrollTop = 0;
        });
        await page.waitForTimeout(200);
        await page.screenshot({ path: path.join(OUT_DIR, `01-${theme}-head.png`) });
        // Scroll to tool blocks (bash + failure + markers) — mid-transcript.
        await page.evaluate(() => {
          const scroller = document.querySelector(".session-scroll");
          if (scroller) scroller.scrollTop = scroller.scrollHeight;
        });
        await page.waitForTimeout(200);
        await page.screenshot({ path: path.join(OUT_DIR, `02-${theme}-tail.png`) });
        await context.close();
      }
      const files = await fs.readdir(OUT_DIR);
      console.log(`[wiki-304] screenshots written to ${OUT_DIR}:`);
      for (const f of files.sort()) console.log(`  ${f}`);
    } finally {
      await browser.close();
    }
  } finally {
    if (typeof backend.stop === "function") await backend.stop();
    else if (typeof backend.kill === "function") backend.kill();
    await fs.rm(fixtures.root, { recursive: true, force: true });
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
