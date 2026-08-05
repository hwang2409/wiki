// WIKI-241: capture short, long, wrapped, expanded, and narrow states of the
// tool output preview so the PR body can link to visual evidence. We open
// each activity + tool by clicking, then screenshot the whole session view;
// the transcript-preview head + chips are the region of interest.
import fs from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";

import {
  makeFixtureRoot,
  startBackend,
  writeQueue,
  writeRegistry,
} from "./wiki32-harness.mjs";

const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || "/tmp/wiki-241-evidence";
const TICKET = "WIKI-241";
const NOW = "2026-08-03T12:00:00Z";

function makeAssistantWithTool(index, toolId, name, input, text) {
  return {
    type: "assistant",
    timestamp: NOW,
    message: {
      role: "assistant",
      content: [
        { type: "text", text },
        { type: "tool_use", id: toolId, name, input },
      ],
    },
  };
}

function makeToolResult(toolId, content) {
  return {
    type: "user",
    timestamp: NOW,
    message: {
      role: "user",
      content: [{ type: "tool_result", tool_use_id: toolId, content }],
    },
  };
}

const READ_OUTPUT_SHORT = [
  '"""Session store — persistence layer for agent runtime."""',
  "",
  "from pathlib import Path",
  "",
  "def load(root: Path) -> dict:",
  "    return {\"root\": str(root)}",
].join("\n");

const READ_OUTPUT_LONG = [
  '"""Session store — persistence layer for agent runtime."""',
  "",
  "from __future__ import annotations",
  "",
  "import asyncio",
  "import json",
  "import os",
  "from dataclasses import dataclass",
  "from pathlib import Path",
  "from typing import Any, Iterable, Mapping",
  "",
  ...Array.from({ length: 55 }, (_, i) => `def helper_${String(i + 1).padStart(2, "0")}(value: Mapping[str, Any]) -> None:`),
  "",
  ...Array.from({ length: 20 }, (_, i) => `    return value.get("key_${i + 1}")`),
].join("\n");

const BASH_INPUT = "grep -RIn --exclude-dir=node_modules 'BoundedPreview' frontend/src";
const BASH_OUTPUT_WRAPPED = [
  "frontend/src/session.tsx:99:import { BoundedPreview } from \"./transcript-preview\";",
  ...Array.from({ length: 40 }, (_, i) =>
    `frontend/src/session.tsx:${1100 + i}: <BoundedPreview label="tool output" text={very_very_very_long_argument_${i + 1}_that_exceeds_the_wrap_threshold_and_forces_the_body_to_use_horizontal_scroll_or_wrap} tone="normal" />`,
  ),
].join("\n");

const TRANSCRIPT = [
  { type: "mode", mode: "normal", sessionId: `fixture-${TICKET}` },
  { type: "permission-mode", permissionMode: "bypassPermissions", sessionId: `fixture-${TICKET}` },
  { type: "custom-title", customTitle: `${TICKET} tool output controls`, sessionId: `fixture-${TICKET}` },
  makeAssistantWithTool(0, "toolu_short", "Read", { path: "store.py" }, "reading a short file"),
  makeToolResult("toolu_short", READ_OUTPUT_SHORT),
  makeAssistantWithTool(1, "toolu_long", "Read", { path: "store.py" }, "reading a long file"),
  makeToolResult("toolu_long", READ_OUTPUT_LONG),
  makeAssistantWithTool(2, "toolu_bash", "Bash", { command: BASH_INPUT, description: "grep" }, "running grep"),
  makeToolResult("toolu_bash", BASH_OUTPUT_WRAPPED),
];

async function writeJsonl(target, rows) {
  await fs.writeFile(target, rows.map((row) => JSON.stringify(row)).join("\n") + "\n");
}

async function openEverything(page) {
  await page.waitForSelector(".session-scroll", { state: "attached", timeout: 30_000 });
  // Repeatedly open every closed activity + tool head until nothing changes.
  // Under virtualization some rows only render after their parent opens.
  for (let round = 0; round < 8; round += 1) {
    let clicked = 0;
    const heads = await page.locator(".session-activity-head[aria-expanded='false']").all();
    for (const head of heads) {
      await head.scrollIntoViewIfNeeded().catch(() => {});
      await head.click({ force: true }).catch(() => {});
      clicked += 1;
    }
    const toolHeads = await page.locator(".session-tool-head[aria-expanded='false']").all();
    for (const head of toolHeads) {
      await head.scrollIntoViewIfNeeded().catch(() => {});
      await head.click({ force: true }).catch(() => {});
      clicked += 1;
    }
    if (clicked === 0) break;
    await page.waitForTimeout(150);
  }
}

async function preparePage(context, layoutId) {
  const page = await context.newPage();
  await page.addInitScript(({ layout }) => {
    localStorage.setItem("wiki-window-layout-v2", JSON.stringify(layout));
    localStorage.setItem("wiki-sidebar-visible", "false");
  }, {
    layout: {
      version: 2,
      activeWindowId: "window-0",
      windows: [
        { id: "window-0", focusedPaneId: layoutId, layout: { kind: "pane", id: layoutId, path: `agent://${TICKET}` } },
      ],
    },
  });
  return page;
}

async function main() {
  await fs.mkdir(OUT_DIR, { recursive: true });
  const fixtures = makeFixtureRoot("wiki-241-");
  const transcript = path.join(fixtures.root, "wiki-241-claude.jsonl");
  await writeJsonl(transcript, TRANSCRIPT);
  writeRegistry(fixtures.registryPath, [[TICKET, transcript]]);
  writeQueue(fixtures.queuePath, TICKET, []);

  const backend = await startBackend(fixtures);
  try {
    const browser = await chromium.launch({ headless: true });
    try {
      // --- normal width ---
      const wide = await browser.newContext({ viewport: { width: 1280, height: 1200 } });
      const page = await preparePage(wide, "pane-1");
      await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
      await openEverything(page);

      // Full-page overview: shows every state in one image.
      await page.screenshot({ path: path.join(OUT_DIR, "01-overview-collapsed-tools.png"), fullPage: true });

      // Individual states — find each by summary text.
      const shortRow = page.locator(".session-activity-row.is-result").filter({ has: page.locator(".session-tool-result-summary", { hasText: "short" }).or(page.locator(".transcript-preview-body", { hasText: "load(root" })) }).first();
      const longRow = page.locator(".session-activity-row.is-result").filter({ has: page.locator(".transcript-preview-body", { hasText: "helper_01" }) }).first();
      const bashRow = page.locator(".session-activity-row.is-result").filter({ has: page.locator(".transcript-preview-body", { hasText: "grep" }) }).first();

      await shortRow.scrollIntoViewIfNeeded().catch(() => {});
      await shortRow.screenshot({ path: path.join(OUT_DIR, "02-short-output.png") }).catch(() => {});

      await longRow.scrollIntoViewIfNeeded().catch(() => {});
      await longRow.screenshot({ path: path.join(OUT_DIR, "03-long-collapsed.png") }).catch(() => {});

      const showAllChip = longRow.locator(".transcript-chip", { hasText: "show all" }).first();
      if (await showAllChip.count()) {
        await showAllChip.click({ force: true });
        await longRow.locator(".transcript-chip", { hasText: "show less" }).waitFor({ state: "attached", timeout: 5000 }).catch(() => {});
        await longRow.screenshot({ path: path.join(OUT_DIR, "04-long-expanded-sticky-head.png") }).catch(() => {});
      }

      await bashRow.scrollIntoViewIfNeeded().catch(() => {});
      const keepChip = bashRow.locator(".transcript-chip", { hasText: "keep lines" }).first();
      if (await keepChip.count()) {
        await keepChip.click({ force: true });
        await bashRow.locator(".transcript-chip", { hasText: "wrap lines" }).waitFor({ state: "attached", timeout: 5000 }).catch(() => {});
        await bashRow.screenshot({ path: path.join(OUT_DIR, "05-bash-keep-lines.png") }).catch(() => {});
        await bashRow.locator(".transcript-chip", { hasText: "wrap lines" }).first().click({ force: true });
        await bashRow.locator(".transcript-chip", { hasText: "keep lines" }).waitFor({ state: "attached", timeout: 5000 }).catch(() => {});
      }
      await bashRow.screenshot({ path: path.join(OUT_DIR, "06-bash-wrap-lines.png") }).catch(() => {});

      // Final full-page snapshot of expanded state.
      await page.screenshot({ path: path.join(OUT_DIR, "07-overview-after-interaction.png"), fullPage: true });

      // --- narrow layout ---
      const narrow = await browser.newContext({ viewport: { width: 780, height: 1200 } });
      const nPage = await preparePage(narrow, "pane-1");
      await nPage.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
      await openEverything(nPage);
      await nPage.screenshot({ path: path.join(OUT_DIR, "08-narrow-overview.png"), fullPage: true });

      const nLongRow = nPage.locator(".session-activity-row.is-result").filter({ has: nPage.locator(".transcript-preview-body", { hasText: "helper_01" }) }).first();
      await nLongRow.scrollIntoViewIfNeeded().catch(() => {});
      await nLongRow.screenshot({ path: path.join(OUT_DIR, "09-narrow-long-collapsed.png") }).catch(() => {});
      const nShowAll = nLongRow.locator(".transcript-chip", { hasText: "show all" }).first();
      if (await nShowAll.count()) {
        await nShowAll.click({ force: true });
        await nLongRow.locator(".transcript-chip", { hasText: "show less" }).waitFor({ state: "attached", timeout: 5000 }).catch(() => {});
        await nLongRow.screenshot({ path: path.join(OUT_DIR, "10-narrow-long-expanded.png") }).catch(() => {});
      }

      const files = await fs.readdir(OUT_DIR);
      console.log(`[wiki-241] screenshots written to ${OUT_DIR}:`);
      for (const f of files) console.log(`  ${f}`);
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
