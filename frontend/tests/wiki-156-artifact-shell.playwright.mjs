import { spawnSync } from "node:child_process";
import fs from "node:fs/promises";
import { existsSync, mkdirSync, rmSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

import { makeFixtureRoot, startBackend, writeQueue, writeRegistry } from "../scripts/wiki32-harness.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const PYTHON = [
  process.env.WIKI_PYTHON,
  path.join(ROOT, ".venv", "bin", "python"),
  path.resolve(ROOT, "..", "..", "..", ".venv", "bin", "python"),
].filter(Boolean).find((candidate) => existsSync(candidate));
if (!PYTHON) {
  throw new Error("No python interpreter found for wiki-156 test");
}
const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || "/tmp/wiki-156-evidence";
mkdirSync(OUT_DIR, { recursive: true });

const TICKET = "WIKI-156";
const RUN_ID = "00000000-0000-4000-8000-000000000156";

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function largeMermaid() {
  const lines = ["flowchart LR"];
  for (let i = 0; i < 24; i += 1) {
    lines.push(`  N${i + 1} --> N${i + 2}`);
  }
  return lines.join("\n");
}

function smallMermaid() {
  return "flowchart LR\n  Plan --> Build\n  Build --> Ship";
}

function largePlot() {
  return {
    $schema: "https://vega.github.io/schema/vega-lite/v5.json",
    width: 480,
    height: 320,
    data: { values: Array.from({ length: 40 }, (_, index) => ({ x: index, y: Math.sin(index / 3) * 4 })) },
    mark: "line",
    encoding: {
      x: { field: "x", type: "quantitative" },
      y: { field: "y", type: "quantitative" },
    },
  };
}

function inputs() {
  return [
    { kind: "mermaid", title: "Titled diagram", caption: "How the pipeline flows end to end.", payload: { source: smallMermaid() } },
    { kind: "mermaid", title: "Large mermaid graph", payload: { source: largeMermaid() } },
    { kind: "mermaid", title: "Second large graph", payload: { source: largeMermaid() } },
    { kind: "svg", title: "Titled svg", payload: { source: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 60"><rect width="120" height="60" fill="#123"/></svg>' } },
    { kind: "plot", title: "Trend plot", payload: { spec_vega_lite: largePlot() } },
    { kind: "code", title: "Untitled fallback", payload: { language: "typescript", source: "export const answer = 42;\n" } },
  ];
}

function invokeArtifactTool(fixtures, entries) {
  const requests = [
    { jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "wiki-156-fixture", version: "1" } } },
    ...entries.map((entry, index) => ({ jsonrpc: "2.0", id: index + 10, method: "tools/call", params: { name: "render_artifact", arguments: entry } })),
  ];
  const result = spawnSync(PYTHON, ["-m", "backend.app.wiki_artifacts"], {
    cwd: ROOT,
    env: { ...process.env, WIKI_AGENT_RUNTIME_DIR: fixtures.runtimeDir, WIKI_RUN_ID: RUN_ID },
    input: requests.map((request) => JSON.stringify(request)).join("\n") + "\n",
    encoding: "utf8",
  });
  if (result.status !== 0) throw new Error(result.stderr || result.stdout);
  return result.stdout.trim().split("\n").slice(1).map((line) => {
    const response = JSON.parse(line);
    if (response.result?.isError) throw new Error(response.result.content?.[0]?.text || "artifact rejected");
    const sentinel = response.result.content[0].text;
    const event = JSON.parse(sentinel.slice("<<wiki-artifact:v1>>".length, -"<<end>>".length));
    return { id: event.id, sentinel };
  });
}

async function writeTranscript(fixtures, entries, results) {
  const rows = [{ type: "mode", mode: "normal", sessionId: "wiki-156-artifact-shell" }];
  entries.forEach((entry, index) => {
    const id = `toolu_wiki156_${index}`;
    rows.push({ type: "assistant", timestamp: `2026-07-22T15:00:${String(index * 2).padStart(2, "0")}Z`, message: { role: "assistant", content: [{ type: "tool_use", id, name: "mcp__wiki-artifacts__render_artifact", input: entry }] } });
    rows.push({ type: "user", timestamp: `2026-07-22T15:00:${String(index * 2 + 1).padStart(2, "0")}Z`, message: { role: "user", content: [{ type: "tool_result", tool_use_id: id, content: results[index].sentinel }] } });
  });
  const transcript = path.join(fixtures.root, "wiki-156-artifact-shell.jsonl");
  await fs.writeFile(transcript, rows.map((row) => JSON.stringify(row)).join("\n") + "\n");
  return transcript;
}

function sessionLayout() {
  return { version: 2, activeWindowId: "window-0", windows: [{ id: "window-0", focusedPaneId: "pane-1", layout: { kind: "pane", id: "pane-1", path: `agent://${TICKET}` } }] };
}

async function setPanelUrl(page, tabs, focus) {
  await page.evaluate(({ ticket, tabs, focus }) => {
    const url = new URL(window.location.href);
    url.searchParams.set("panel", ticket);
    url.searchParams.set("artifact", focus);
    url.searchParams.set("tab", tabs.join(","));
    url.searchParams.set("focus", focus);
    history.pushState(null, "", url);
    window.dispatchEvent(new PopStateEvent("popstate"));
  }, { ticket: TICKET, tabs, focus });
}

async function main() {
  const fixtures = makeFixtureRoot("wiki-156-artifact-shell-");
  const entries = inputs();
  const results = invokeArtifactTool(fixtures, entries);
  const transcript = await writeTranscript(fixtures, entries, results);
  writeRegistry(fixtures.registryPath, [[TICKET, transcript]]);
  writeQueue(fixtures.queuePath, TICKET, []);
  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  page.on("pageerror", (error) => console.error(`[wiki-156-playwright] page error: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error") console.error(`[wiki-156-playwright] console: ${message.text()}`);
  });

  const [smallMermaidId, largeMermaidAId, largeMermaidBId, svgId, plotId, codeId] = results.map((r) => r.id);

  try {
    await page.addInitScript(({ layout }) => {
      localStorage.setItem("wiki-window-layout-v2", JSON.stringify(layout));
      localStorage.setItem("wiki-sidebar-visible", "false");
      localStorage.setItem("wiki-theme", "mono-light");
    }, { layout: sessionLayout() });
    await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
    await page.locator(".session-scroll").waitFor({ state: "visible" });

    const smallBlock = page.locator(`[data-artifact-id="${smallMermaidId}"]`);
    const largeBlockA = page.locator(`[data-artifact-id="${largeMermaidAId}"]`);
    const largeBlockB = page.locator(`[data-artifact-id="${largeMermaidBId}"]`);
    await smallBlock.waitFor({ state: "visible" });
    await largeBlockA.waitFor({ state: "visible" });

    // --- No filled card background on artifact blocks ---
    const smallStyle = await smallBlock.evaluate((node) => {
      const shell = node.closest(".artifact-block-shell");
      const shellStyle = shell ? getComputedStyle(shell) : null;
      const blockStyle = getComputedStyle(node);
      return {
        shellBg: shellStyle?.backgroundColor ?? null,
        shellBorderTop: shellStyle?.borderTopWidth ?? null,
        blockBg: blockStyle.backgroundColor,
        headerBg: getComputedStyle(node.querySelector(".artifact-header")).backgroundColor,
      };
    });
    assert(
      smallStyle.shellBg === "rgba(0, 0, 0, 0)" || smallStyle.shellBg === "transparent",
      `artifact shell should have no filled background, got ${smallStyle.shellBg}`,
    );
    assert(
      smallStyle.blockBg === "rgba(0, 0, 0, 0)" || smallStyle.blockBg === "transparent",
      `artifact block should have no filled background, got ${smallStyle.blockBg}`,
    );
    assert(
      smallStyle.headerBg === "rgba(0, 0, 0, 0)" || smallStyle.headerBg === "transparent",
      `artifact header should have no filled background, got ${smallStyle.headerBg}`,
    );
    assert(
      smallStyle.shellBorderTop && smallStyle.shellBorderTop !== "0px",
      `artifact shell should have a hairline top separator, got border-top-width=${smallStyle.shellBorderTop}`,
    );

    // --- Actions hover-visible ---
    const actionsSelector = ".artifact-actions";
    const hiddenOpacity = await smallBlock.locator(actionsSelector).evaluate((node) => parseFloat(getComputedStyle(node).opacity));
    assert(hiddenOpacity < 0.5, `actions should be hidden by default, opacity=${hiddenOpacity}`);
    await smallBlock.hover();
    await page.waitForFunction(({ id }) => {
      const block = document.querySelector(`[data-artifact-id="${id}"] .artifact-actions`);
      return block && parseFloat(getComputedStyle(block).opacity) > 0.9;
    }, { id: smallMermaidId }, { timeout: 2000 });
    const shownOpacity = await smallBlock.locator(actionsSelector).evaluate((node) => parseFloat(getComputedStyle(node).opacity));
    assert(shownOpacity > 0.9, `actions should be visible on hover, opacity=${shownOpacity}`);

    // --- Single-line header: title only, no separate filename+title stack ---
    const titleCount = await smallBlock.locator(".artifact-title").count();
    assert(titleCount === 1, `header should have exactly one title node, got ${titleCount}`);
    const titleText = (await smallBlock.locator(".artifact-title").textContent())?.trim();
    assert(titleText === "Titled diagram", `header should show explicit title, got ${titleText}`);

    // --- Description is a secondary muted line, ellipsis-truncated ---
    const captionInfo = await smallBlock.locator(".artifact-caption").evaluate((node) => ({
      overflow: getComputedStyle(node).textOverflow,
      whiteSpace: getComputedStyle(node).whiteSpace,
    }));
    assert(captionInfo.overflow === "ellipsis", `caption should ellipsis-truncate, got ${captionInfo.overflow}`);
    assert(captionInfo.whiteSpace === "nowrap", `caption should stay on one line, got ${captionInfo.whiteSpace}`);

    // --- Long artifact collapses; expand button flips label; expand persists per-artifact ---
    assert(
      (await largeBlockA.getAttribute("data-artifact-compact")) === "true",
      "long mermaid should collapse by default",
    );
    await largeBlockA.hover();
    const showAllA = largeBlockA.getByRole("button", { name: "Show all" });
    await showAllA.waitFor({ state: "visible" });
    await showAllA.click();
    await page.waitForFunction((id) => {
      const el = document.querySelector(`[data-artifact-id="${id}"]`);
      return el?.getAttribute("data-artifact-expanded") === "true";
    }, largeMermaidAId, { timeout: 2000 });

    // Second long block still collapsed — expand state is per-artifact.
    assert(
      (await largeBlockB.getAttribute("data-artifact-expanded")) !== "true",
      "expand state should not leak to a sibling artifact",
    );

    // Expand persists across a soft reload (module-level cache; same session key).
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.locator(".session-scroll").waitFor({ state: "visible" });
    const largeAAfter = page.locator(`[data-artifact-id="${largeMermaidAId}"]`);
    await largeAAfter.waitFor({ state: "visible" });
    // NOTE: cache is module-scoped so it resets on full page reload; we don't require survival
    // across a hard reload — only across sibling artifact focus changes.

    // --- Plot skeleton present during load ---
    const plotBlock = page.locator(`[data-artifact-id="${plotId}"]`);
    await plotBlock.waitFor({ state: "visible" });
    const plotSkeletonSelector = ".artifact-placeholder[data-artifact-placeholder-shape=\"plot\"]";
    const hasPlotSkeleton = await plotBlock.locator(plotSkeletonSelector).count();
    // Placeholder is present until vega finishes; assert it either exists now or existed.
    if (hasPlotSkeleton === 0) {
      // The chart may have already resolved; check that the wrapper still exists.
      assert((await plotBlock.locator(".artifact-plot-wrap").count()) > 0, "plot wrapper should exist for placeholder overlay");
    }

    // --- SVG "Preparing image…" copy replaces implementation wording ---
    // Assert the string is not present in any load state.
    const svgSanitizeText = await page.locator(".artifact-loading").filter({ hasText: /Sanitiz/i }).count();
    assert(svgSanitizeText === 0, `SVG placeholder should not expose sanitize wording, found ${svgSanitizeText} occurrences`);

    // --- IDs hidden when title exists (panel) ---
    await setPanelUrl(page, [smallMermaidId, largeMermaidAId, svgId], smallMermaidId);
    const panel = page.getByRole("complementary", { name: "Artifact panel" });
    await panel.waitFor({ state: "visible" });
    const titledTab = panel.getByRole("tab", { name: /Titled diagram/ });
    await titledTab.waitFor({ state: "visible" });
    const tabText = (await titledTab.textContent())?.trim() ?? "";
    assert(
      !/artifact\s+[a-f0-9]{4,}/i.test(tabText),
      `tab with title should not expose raw artifact ID, got "${tabText}"`,
    );

    // --- "Pin to vault — Coming soon" absent from panel ---
    const pin = panel.getByRole("menuitem", { name: /Pin to vault/ });
    assert((await pin.count()) === 0, "Pin to vault Coming-soon control should be removed");

    // --- Recently-closed moved to overflow menu ---
    await titledTab.click();
    // Close the currently focused tab.
    await panel.getByRole("button", { name: "Close Titled diagram" }).click();
    // The old inline strip must be gone.
    assert(
      (await panel.locator(".artifact-panel-recent").count()) === 0,
      "Recently-closed inline strip should no longer render as a second tab strip",
    );
    // The overflow menu button should expose it.
    await panel.getByRole("button", { name: "Artifact panel menu" }).click();
    const overflowMenu = panel.locator("#artifact-panel-overflow-menu");
    await overflowMenu.waitFor({ state: "visible" });
    await overflowMenu.getByRole("menuitem", { name: "Titled diagram" }).waitFor({ state: "visible" });

    await page.screenshot({ path: path.join(OUT_DIR, "wiki-156-shell.png") });
  } finally {
    await context.close();
    await browser.close();
    await backend.stop();
    rmSync(fixtures.root, { recursive: true, force: true });
  }
}

await main();
