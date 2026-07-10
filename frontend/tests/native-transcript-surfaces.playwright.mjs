import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

import { makeFixtureRoot, openSessionPage, startBackend, writeQueue, writeRegistry } from "../scripts/wiki32-harness.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const FIXTURES_DIR = path.resolve(ROOT, "backend", "tests", "fixtures");
const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || "/tmp/wiki-41-playwright-evidence";

function logStep(message) {
  console.error(`[wiki-41-playwright] ${message}`);
}

async function copyFixture(name, targetDir) {
  const source = path.join(FIXTURES_DIR, name);
  const target = path.join(targetDir, name);
  await fs.copyFile(source, target);
  return target;
}

async function expectVisibleText(page, selector, text) {
  const locator = page.locator(selector, { hasText: text }).first();
  await locator.waitFor({ state: "visible" });
  return locator;
}

async function openTicket(page, baseUrl, ticket) {
  await page.goto(`${baseUrl}/#/agent/${ticket}`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".session-scroll");
}

async function main() {
  logStep("preparing isolated fixtures");
  await fs.mkdir(OUT_DIR, { recursive: true });
  const fixtures = makeFixtureRoot("wiki-41-native-surfaces-");
  const claudeTranscript = await copyFixture("claude_native_surfaces.jsonl", fixtures.root);
  const codexTranscript = await copyFixture("codex_native_surfaces.jsonl", fixtures.root);
  writeRegistry(fixtures.registryPath, [
    ["WIKI-32", claudeTranscript],
    ["WIKI-33", codexTranscript],
  ]);
  writeQueue(fixtures.queuePath, "WIKI-32", []);

  logStep("starting isolated backend");
  const backend = await startBackend(fixtures);
  logStep(`backend ready at ${backend.baseUrl}`);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1600 } });

  try {
    logStep("opening Claude fixture session");
    await openSessionPage(page, backend.baseUrl, {
      version: 2,
      activeWindowId: "window-0",
      windows: [
        {
          id: "window-0",
          focusedPaneId: "pane-1",
          layout: { kind: "pane", id: "pane-1", path: "agent://WIKI-32" },
        },
      ],
    });
    logStep("asserting Claude session surfaces");

    await expectVisibleText(page, ".session-state-meta", "Fixture transcript");
    await expectVisibleText(page, ".session-state-meta.is-faint", "wiki worker");
    await expectVisibleText(page, ".session-dispositions-value", "Unknown 0");

    const scopeQuestion = page.locator(".session-question").filter({ hasText: "Which scope?" });
    await scopeQuestion.waitFor({ state: "visible" });
    await scopeQuestion.locator(".session-question-option.is-picked").getByText("Original brief only").waitFor();

    const branchQuestion = page.locator(".session-question").filter({ hasText: "Which branch?" });
    await branchQuestion.waitFor({ state: "visible" });
    await branchQuestion.getByText("Custom reply").waitFor();
    await branchQuestion.getByText("Go with the fresh branch").waitFor();

    const activityToggle = page.locator(".session-activity-head").first();
    await activityToggle.waitFor({ state: "visible" });
    await activityToggle.click();
    await expectVisibleText(page, ".session-tool-summary", "monitor: while true; do sleep 45; done");
    const monitorTool = page
      .locator(".session-tool")
      .filter({ hasText: "monitor: while true; do sleep 45; done" })
      .first();
    await monitorTool.locator(".session-tool-head").click();
    await expectVisibleText(page, ".session-tool-output", "Monitor started (task task123");

    logStep("capturing Claude screenshot");
    await page.screenshot({ path: path.join(OUT_DIR, "claude-native-surfaces.png"), fullPage: true });

    logStep("opening Codex fixture session");
    await openTicket(page, backend.baseUrl, "WIKI-33");
    logStep("asserting Codex session surfaces");
    await expectVisibleText(page, ".session-dispositions-value", "Unknown 1");
    const codexActivityToggle = page.locator(".session-activity-head").first();
    await codexActivityToggle.waitFor({ state: "visible" });
    await codexActivityToggle.click();
    await expectVisibleText(page, ".session-thinking-chip", "encrypted");
    await expectVisibleText(page, ".session-marker", "subagent started");
    logStep("capturing Codex screenshot");
    await page.screenshot({ path: path.join(OUT_DIR, "codex-native-surfaces.png"), fullPage: true });

    logStep("writing Playwright summary");
    await fs.writeFile(
      path.join(OUT_DIR, "summary.json"),
      JSON.stringify(
        {
          claude: {
            dispositions: "Unknown 0",
            screenshot: path.join(OUT_DIR, "claude-native-surfaces.png"),
          },
          codex: {
            dispositions: "Unknown 1",
            screenshot: path.join(OUT_DIR, "codex-native-surfaces.png"),
          },
        },
        null,
        2
      )
    );
    logStep("frontend verification complete");
  } finally {
    await page.close();
    await browser.close();
    await backend.stop();
  }
}

await main();
