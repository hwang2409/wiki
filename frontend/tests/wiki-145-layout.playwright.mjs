import fs from "node:fs/promises";
import { mkdirSync, rmSync } from "node:fs";
import path from "node:path";
import { chromium } from "playwright";

import {
  codexAssistant,
  makeFixtureRoot,
  startBackend,
  writeQueue,
  writeRegistry,
} from "../scripts/wiki32-harness.mjs";

const TICKET = "WIKI-145";
const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || "/tmp/wiki-145-layout-evidence";
mkdirSync(OUT_DIR, { recursive: true });

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

let browser;
let backend;
const fixtures = makeFixtureRoot("wiki-145-layout-");

try {
  const transcript = path.join(fixtures.root, "wiki-145-layout.jsonl");
  await fs.writeFile(
    transcript,
    [
      { type: "mode", mode: "normal", sessionId: "wiki-145-layout" },
      codexAssistant("Layout smoke fixture.", "2026-07-22T15:00:00Z"),
    ]
      .map((row) => JSON.stringify(row))
      .join("\n") + "\n",
  );
  writeRegistry(fixtures.registryPath, [[TICKET, transcript]]);
  writeQueue(fixtures.queuePath, TICKET, []);

  backend = await startBackend(fixtures);
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

  // sidebar VISIBLE for this test so we can measure the rail.
  await page.addInitScript(() => {
    localStorage.setItem("wiki-sidebar-visible", "true");
  });
  await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".app-container");
  await page.waitForSelector(".workspace-sidebar");
  await page.waitForSelector('[data-testid="sidebar-views"]');
  await page.waitForSelector(".workspace-panes, .workspace-leaf");

  // WIKI-296 collapsed the shell to a two-column grid; the ribbon column is
  // gone and destinations now live inside the sidebar's Views section.
  const columns = await page.evaluate(() => {
    const app = document.querySelector(".app-container");
    if (!(app instanceof HTMLElement)) return null;
    const cs = getComputedStyle(app);
    return cs.gridTemplateColumns;
  });
  assert(columns, "app-container missing gridTemplateColumns");
  const parts = columns.split(/\s+/);
  assert(parts.length === 2, `expected 2 grid columns, got ${parts.length}: ${columns}`);
  const sidebarPx = parseFloat(parts[0]);
  assert(
    sidebarPx >= 180 && sidebarPx <= 240,
    `workspace-sidebar expected 180-240px (calm rail), got ${sidebarPx}`,
  );

  // Sidebar right edge is a hairline (1px), not a filled band; background must be transparent.
  const sidebarBorder = await page.evaluate(() => {
    const el = document.querySelector(".workspace-sidebar");
    const body = document.body;
    if (!(el instanceof HTMLElement)) return null;
    const cs = getComputedStyle(el);
    const bodyBg = getComputedStyle(body).backgroundColor;
    return {
      borderRight: cs.borderRightWidth,
      background: cs.backgroundColor,
      bodyBg,
    };
  });
  assert(sidebarBorder, "workspace-sidebar missing");
  assert(
    sidebarBorder.borderRight === "1px",
    `workspace-sidebar right border expected 1px hairline, got ${sidebarBorder.borderRight}`,
  );
  // Transparent OR matches the surrounding body background — either signals "no filled band".
  const sidebarTransparent =
    sidebarBorder.background === "rgba(0, 0, 0, 0)" ||
    sidebarBorder.background === "transparent" ||
    sidebarBorder.background === sidebarBorder.bodyBg;
  assert(
    sidebarTransparent,
    `workspace-sidebar background expected transparent or body bg (${sidebarBorder.bodyBg}), got ${sidebarBorder.background}`,
  );

  // WIKI-151 removed the phantom `.workspace-tab-header` bar entirely (the
  // active-window title lives in the bottom tmux rail and the view-header
  // carries breadcrumbs). Assert it's absent so a regression that reintroduces
  // the bordered nonfunctional tab lands on this test.
  const stillPresent = await page.evaluate(
    () => document.querySelectorAll(".workspace-tab-header").length,
  );
  assert(
    stillPresent === 0,
    `WIKI-151 removed .workspace-tab-header, but ${stillPresent} still render`,
  );

  await page.screenshot({ path: path.join(OUT_DIR, "wiki-145-layout.png") });
} finally {
  if (browser) await browser.close();
  if (backend) await backend.stop();
  rmSync(fixtures.root, { recursive: true, force: true });
}
