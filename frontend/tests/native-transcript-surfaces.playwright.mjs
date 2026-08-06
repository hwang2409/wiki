import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

import {
  codexAssistant,
  codexToolCall,
  codexToolOutput,
  makeFixtureRoot,
  openSessionPage,
  startBackend,
  writeQueue,
  writeRegistry,
} from "../scripts/wiki32-harness.mjs";

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

async function writeJsonl(target, rows) {
  await fs.writeFile(target, rows.map((row) => JSON.stringify(row)).join("\n") + "\n");
}

async function main() {
  logStep("preparing isolated fixtures");
  await fs.mkdir(OUT_DIR, { recursive: true });
  const fixtures = makeFixtureRoot("wiki-41-native-surfaces-");
  const claudeTranscript = await copyFixture("claude_native_surfaces.jsonl", fixtures.root);
  const codexTranscript = await copyFixture("codex_native_surfaces.jsonl", fixtures.root);
  const ghPreviewTranscript = path.join(fixtures.root, "codex_github_preview.jsonl");
  await writeJsonl(ghPreviewTranscript, [
    codexAssistant(
      "Please review https://github.com/hwang2409/wiki/pull/45 before merging.",
      "2026-07-10T18:10:00Z"
    ),
    codexToolCall("call-gh-preview", "Read", '{"path":"notes.md"}', "2026-07-10T18:10:01Z"),
    codexToolOutput(
      "call-gh-preview",
      [
        "Rendered commit preview target:",
        "https://github.com/hwang2409/wiki/commit/467c1a2fc52a9d8072282894b92a954ccb17727e",
        "Fallback should stay bare:",
        "https://github.com/hwang2409/wiki/issues/64",
      ].join("\n"),
      "2026-07-10T18:10:02Z"
    ),
  ]);
  writeRegistry(fixtures.registryPath, [
    ["WIKI-32", claudeTranscript],
    ["WIKI-33", codexTranscript],
    ["WIKI-64", ghPreviewTranscript],
  ]);
  writeQueue(fixtures.queuePath, "WIKI-32", []);

  logStep("starting isolated backend");
  const backend = await startBackend(fixtures);
  logStep(`backend ready at ${backend.baseUrl}`);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1600 } });

  try {
    await page.route("**/api/gh/preview**", async (route) => {
      const requested = new URL(route.request().url()).searchParams.get("url");
      if (requested === "https://github.com/hwang2409/wiki/pull/45") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          headers: { ETag: '"pr-preview"' },
          body: JSON.stringify({
            ok: true,
            kind: "pr",
            title: "WIKI-58 inline thinking rows and font picker",
            state: "MERGED",
            extra: {
              mergeStateStatus: "CLEAN",
              checks: { pass: 0, fail: 0, pending: 0 },
              changedFiles: 3,
              updatedAt: "2026-07-10T18:00:00Z",
            },
          }),
        });
        return;
      }
      if (requested === "https://github.com/hwang2409/wiki/commit/467c1a2fc52a9d8072282894b92a954ccb17727e") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          headers: { ETag: '"commit-preview"' },
          body: JSON.stringify({
            ok: true,
            kind: "commit",
            title: "Add GitHub URL previews",
            state: null,
            extra: {
              sha: "467c1a2fc52a9d8072282894b92a954ccb17727e",
              author: "hwang2409",
              date: "2026-07-10T17:45:00Z",
            },
          }),
        });
        return;
      }
      await route.fulfill({
        status: 502,
        contentType: "application/json",
        body: JSON.stringify({ ok: false, error: "fixture failure" }),
      });
    });

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
    // WIKI-235: provider dispositions and Run details are absent from the
    // default transcript surface. The action forms remain available below.
    if ((await page.locator(".session-dispositions").count()) !== 0) {
      throw new Error("WIKI-152: ambient .session-dispositions chip must not render");
    }
    if ((await page.locator('[data-testid="session-run-details"]').count()) !== 0) {
      throw new Error("WIKI-235: Run details must not render");
    }

    const scopeQuestion = page.locator(".session-question").filter({ hasText: "Which scope?" });
    await scopeQuestion.waitFor({ state: "visible" });
    await scopeQuestion.locator(".session-question-option.is-picked").getByText("Original brief only").waitFor();

    const branchQuestion = page.locator(".session-question").filter({ hasText: "Which branch?" });
    await branchQuestion.waitFor({ state: "visible" });
    await branchQuestion.getByText("Custom reply").waitFor();
    await branchQuestion.getByText("Go with the fresh branch").waitFor();

    // WIKI-244: activity groups and tool bodies are open by default.
    await page.locator(".session-activity > .session-activity-row").first().waitFor({ state: "visible" });
    await expectVisibleText(page, ".session-tool-summary", "monitor: while true; do sleep 45; done");
    await expectVisibleText(page, ".transcript-preview-body", "Monitor started (task task123");

    logStep("capturing Claude screenshot");
    await page.screenshot({ path: path.join(OUT_DIR, "claude-native-surfaces.png"), fullPage: true });

    logStep("opening Codex fixture session");
    await openTicket(page, backend.baseUrl, "WIKI-33");
    logStep("asserting Codex session surfaces");
    if ((await page.locator(".session-dispositions").count()) !== 0) {
      throw new Error("WIKI-152: ambient .session-dispositions chip must not render for Codex either");
    }
    if ((await page.locator('[data-testid="session-run-details"]').count()) !== 0) {
      throw new Error("WIKI-235: Codex Run details must not render");
    }
    // WIKI-244: activity groups are open by default.
    await page.locator(".session-activity > .session-activity-row").first().waitFor({ state: "visible" });
    await expectVisibleText(page, ".session-thinking-chip", "encrypted");
    await expectVisibleText(page, ".session-marker", "subagent started");
    logStep("capturing Codex screenshot");
    await page.screenshot({ path: path.join(OUT_DIR, "codex-native-surfaces.png"), fullPage: true });

    logStep("opening GitHub preview fixture session");
    await openTicket(page, backend.baseUrl, "WIKI-64");
    await expectVisibleText(page, ".gh-preview-title", "WIKI-58 inline thinking rows and font picker");
    await expectVisibleText(page, ".gh-preview-badge", "merged");
    await expectVisibleText(page, ".gh-preview-badge.is-muted", "3 files");
    // WIKI-244: activity groups and tool bodies are open by default — no
    // clicks needed; the preview output is always visible in the tool row.
    await page.locator(".session-activity .session-tool").first().waitFor({ state: "visible" });
    const previewTool = page.locator(".session-activity .session-tool").first();
    const previewToolEventId = await previewTool.getAttribute("data-tool-event-id");
    if (!previewToolEventId) throw new Error("preview tool missing data-tool-event-id");
    // The gh preview output lives either merged in the tool row or in the
    // standalone result row keyed by the same data-tool-event-id.
    const previewScope = page
      .locator(`.session-activity-row[data-tool-event-id='${previewToolEventId}']`)
      .filter({ has: page.locator(".gh-preview-title") })
      .first();
    await previewScope.locator(".gh-preview-title", { hasText: "Add GitHub URL previews" }).waitFor();
    // WIKI-153 (#122) wrapped tool output in `.session-tool-output-text` so
    // preview cards render inline with ANSI text; the bare fallback anchor is
    // now a descendant of `.session-tool-output-blocks`, not a direct child.
    await previewScope
      .locator(".session-tool-output-blocks a.external-link", {
        hasText: "https://github.com/hwang2409/wiki/issues/64",
      })
      .waitFor({ state: "visible" });
    await page.waitForTimeout(250);
    logStep("capturing GitHub preview screenshot");
    await page.locator(".session-scroll").screenshot({ path: path.join(OUT_DIR, "github-preview-cards.png") });

    logStep("writing Playwright summary");
    await fs.writeFile(
      path.join(OUT_DIR, "summary.json"),
      JSON.stringify(
        {
          claude: {
            screenshot: path.join(OUT_DIR, "claude-native-surfaces.png"),
          },
          codex: {
            screenshot: path.join(OUT_DIR, "codex-native-surfaces.png"),
          },
          github_preview: {
            screenshot: path.join(OUT_DIR, "github-preview-cards.png"),
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
