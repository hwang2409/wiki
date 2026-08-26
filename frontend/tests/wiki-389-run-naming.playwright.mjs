import { mkdirSync } from "node:fs";
import path from "node:path";
import { chromium } from "playwright";

import {
  makeFixtureRoot,
  startBackend,
  writeQueue,
  writeRegistry,
} from "../scripts/wiki32-harness.mjs";

const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || "/tmp/wiki-389-run-naming";
mkdirSync(OUT_DIR, { recursive: true });

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function viewLabels(page) {
  return page.locator('[data-testid="sidebar-views"] .sidebar-views-row').evaluateAll((rows) =>
    rows.map((row) => row.getAttribute("aria-label")),
  );
}

async function searchQuickSwitcher(page, query) {
  await page.keyboard.press("Control+p");
  const dialog = page.getByRole("dialog", { name: "Quick switcher" });
  await dialog.waitFor();
  const input = dialog.getByPlaceholder("Find a note, file, or session...");
  await input.fill(query);
  const result = dialog.locator(".quick-switcher-result", { hasText: "Runs" });
  await result.waitFor();
  return { dialog, result };
}

const fixtures = makeFixtureRoot("wiki-389-run-naming-");
writeRegistry(fixtures.registryPath, []);
writeQueue(fixtures.queuePath, "WIKI-389", []);

let browser;
let backend;

try {
  backend = await startBackend(fixtures);
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

  await page.addInitScript(() => {
    localStorage.setItem("wiki-sidebar-visible", "true");
    localStorage.setItem("wiki-sidebar-tab", "files");
    localStorage.removeItem("wiki-window-layout-v2");
  });

  await page.goto(`${backend.baseUrl}/#/agent-list`, { waitUntil: "domcontentloaded" });
  await page.locator(".app-container").waitFor();
  await page.locator('[data-testid="sidebar-views-list"]').waitFor();

  const labels = await viewLabels(page);
  assert(labels.filter((label) => label === "Runs").length === 1, "Views should contain one Runs destination");
  assert(!labels.includes("Agent list"), "Views should not expose Agent list");
  assert(new URL(page.url()).hash === "#/agents", "Agent list deep link should redirect to Runs");
  await page.screenshot({ path: path.join(OUT_DIR, "before-sidebar-runs.png"), fullPage: true });

  await page.locator('[data-testid="sidebar-views"] [aria-label="Runs"]').click();
  await page.waitForFunction(() => window.location.hash === "#/agents");
  await page.locator(".agents-view").waitFor();

  await page.getByRole("button", { name: "Show sidebar runs", exact: true }).click();
  await page.locator('.sidebar-mode[data-mode="agents"]').waitFor();
  await page.screenshot({ path: path.join(OUT_DIR, "after-sidebar-runs.png"), fullPage: true });

  let quickSwitcher = await searchQuickSwitcher(page, "Agent list");
  assert(await quickSwitcher.result.count() === 1, "Agent list search should find Runs");
  await quickSwitcher.result.click();
  await page.waitForSelector('[role="dialog"][aria-label="Quick switcher"]', { state: "detached" });

  quickSwitcher = await searchQuickSwitcher(page, "Agents");
  assert(await quickSwitcher.result.count() === 1, "Agents search should find Runs");
  await quickSwitcher.result.click();

  console.log("WIKI-389 run naming: PASS");
  console.log(`WIKI-389 screenshots: ${OUT_DIR}`);
} finally {
  if (browser) await browser.close();
  if (backend) await backend.stop();
}
