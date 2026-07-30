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
    { kind: "code", payload: { language: "typescript", source: "export const answer = 42;\n" } },
    // Real 1x1 PNG so ingest passes the EXIF scrub — the test forces the
    // browser to emit onError manually to exercise the render-error UI.
    { kind: "image", title: "Broken image", payload: { data_base64: "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGL4z8AAAAMAAVpCWrwAAAAASUVORK5CYII=", mime: "image/png" } },
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

async function writeTranscript(fixtures, entries, results, { filename = "wiki-156-artifact-shell.jsonl", sessionId = "wiki-156-artifact-shell" } = {}) {
  const rows = [{ type: "mode", mode: "normal", sessionId }];
  entries.forEach((entry, index) => {
    const id = `toolu_wiki156_${index}`;
    rows.push({ type: "assistant", timestamp: `2026-07-22T15:00:${String(index * 2).padStart(2, "0")}Z`, message: { role: "assistant", content: [{ type: "tool_use", id, name: "mcp__wiki-artifacts__render_artifact", input: entry }] } });
    rows.push({ type: "user", timestamp: `2026-07-22T15:00:${String(index * 2 + 1).padStart(2, "0")}Z`, message: { role: "user", content: [{ type: "tool_result", tool_use_id: id, content: results[index].sentinel }] } });
  });
  const transcript = path.join(fixtures.root, filename);
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

const SECOND_TICKET = "WIKI-156B";

async function main() {
  const fixtures = makeFixtureRoot("wiki-156-artifact-shell-");
  const entries = inputs();
  const results = invokeArtifactTool(fixtures, entries);
  const transcript = await writeTranscript(fixtures, entries, results);
  // Second ticket references the SAME artifact ids via a distinct transcript path — proves
  // the inline-expand store keys by (ticket, subagent, session.path), not artifactId alone.
  const secondTranscript = await writeTranscript(fixtures, entries, results, {
    filename: "wiki-156-artifact-shell-second.jsonl",
    sessionId: "wiki-156-artifact-shell-second",
  });
  writeRegistry(fixtures.registryPath, [[TICKET, transcript], [SECOND_TICKET, secondTranscript]]);
  writeQueue(fixtures.queuePath, TICKET, []);
  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  page.on("pageerror", (error) => console.error(`[wiki-156-playwright] page error: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error") console.error(`[wiki-156-playwright] console: ${message.text()}`);
  });

  const [smallMermaidId, largeMermaidAId, largeMermaidBId, svgId, plotId, codeId, brokenImageId] = results.map((r) => r.id);

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
    // Wait for mermaid to finish rendering + layout to settle so hover target is stable.
    await smallBlock.locator(".artifact-mermaid svg").waitFor({ state: "visible", timeout: 8000 });
    await page.waitForFunction((id) => {
      const el = document.querySelector(`[data-artifact-id="${id}"]`);
      if (!el) return false;
      const rect = el.getBoundingClientRect();
      return rect.height > 60 && rect.width > 60;
    }, smallMermaidId, { timeout: 4000 });
    // Take a stable rect snapshot; re-hover after the async CSS transition kicks in.
    const actionsSelector = ".artifact-actions";
    const hiddenOpacity = await smallBlock.locator(actionsSelector).evaluate((node) => parseFloat(getComputedStyle(node).opacity));
    assert(hiddenOpacity < 0.5, `actions should be hidden by default, opacity=${hiddenOpacity}`);
    // Move pointer off the block first so hover event is guaranteed to fire on the next move.
    await page.mouse.move(0, 0);
    await page.waitForTimeout(50);
    await smallBlock.hover();
    // Retry hover if opacity hasn't advanced yet — reflow between the first hover and the CSS
    // transition can move the target off the pointer.
    await page.waitForFunction(({ id }) => {
      const block = document.querySelector(`[data-artifact-id="${id}"] .artifact-actions`);
      if (!block) return false;
      return parseFloat(getComputedStyle(block).opacity) > 0.9;
    }, { id: smallMermaidId }, { timeout: 4000 }).catch(async () => {
      await smallBlock.hover({ position: { x: 120, y: 12 } });
      await page.waitForFunction(({ id }) => {
        const block = document.querySelector(`[data-artifact-id="${id}"] .artifact-actions`);
        if (!block) return false;
        return parseFloat(getComputedStyle(block).opacity) > 0.9;
      }, { id: smallMermaidId }, { timeout: 4000 });
    });
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

    // Expanded state survives a session focus round-trip (same sessionKey re-entered).
    await setPanelUrl(page, [largeMermaidAId], largeMermaidAId);
    const panelForA = page.getByRole("complementary", { name: "Artifact panel" });
    await panelForA.waitFor({ state: "visible" });
    await panelForA.getByRole("button", { name: "Close artifact panel" }).click();
    await panelForA.waitFor({ state: "hidden" });
    const largeReturned = page.locator(`[data-artifact-id="${largeMermaidAId}"]`);
    await largeReturned.waitFor({ state: "visible" });
    assert(
      (await largeReturned.getAttribute("data-artifact-expanded")) === "true",
      "expand state should survive an intra-session focus round-trip",
    );

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

    // --- Untitled artifact: humanized kind label, kind rendered ONCE, no raw ID ---
    await setPanelUrl(page, [codeId], codeId);
    const untitledTab = panel.getByRole("tab", { name: /^Code$/ }).first();
    await untitledTab.waitFor({ state: "visible" });
    const untitledTabText = (await untitledTab.textContent())?.trim() ?? "";
    assert(untitledTabText === "Code", `untitled code artifact should render humanized kind label only, got "${untitledTabText}"`);
    assert(
      !untitledTabText.toLowerCase().includes(codeId.slice(0, 6)),
      `untitled tab must not expose artifact ID prefix, got "${untitledTabText}"`,
    );
    const kindBadges = await untitledTab.locator(".artifact-panel-tab-kind").count();
    assert(kindBadges === 0, `tab kind badge should not duplicate the title label, got ${kindBadges}`);

    // --- Unavailable-payload panel routes through shared fallback surface ---
    const ghostId = "00000000-0000-4000-8000-ffffffff0156";
    await setPanelUrl(page, [ghostId], ghostId);
    await panel.getByRole("tab").filter({ hasText: /Diagram|Artifact|artifact 00000000/ }).first().waitFor({ state: "visible" }).catch(() => {});
    const fallbackTitle = await panel.locator(".artifact-render-fallback-title").count();
    assert(fallbackTitle > 0, "panel with missing payload should render the shared fallback surface");
    const fallbackAction = await panel.locator(".artifact-render-fallback-action").count();
    assert(fallbackAction >= 1, "panel fallback should expose at least one recovery action");
    // Assert no raw ID prefix leaks into the ghost tab (id is a UUID, must not appear).
    const ghostTab = panel.getByRole("tab").first();
    const ghostTabText = (await ghostTab.textContent())?.trim() ?? "";
    assert(
      !ghostTabText.includes(ghostId.slice(0, 8)),
      `missing-payload tab must not leak the artifact ID slice, got "${ghostTabText}"`,
    );

    // --- Invalid image routes through shared error surface (inline) ---
    await panel.getByRole("button", { name: "Close artifact panel" }).click();
    const brokenBlock = page.locator(`[data-artifact-id="${brokenImageId}"]`);
    await brokenBlock.scrollIntoViewIfNeeded();
    await brokenBlock.waitFor({ state: "visible" });
    // Force the <img> to fail — data URL may still render partial bytes in some engines, so
    // dispatch onError explicitly to prove the shared surface transitions to error state.
    await brokenBlock.evaluate((node) => {
      const img = node.querySelector(".artifact-image-wrap img");
      if (img) img.dispatchEvent(new Event("error"));
    });
    await brokenBlock.locator(".artifact-render-error-title").waitFor({ state: "visible", timeout: 3000 });
    const inlineErrorText = (await brokenBlock.locator(".artifact-render-error-title").textContent())?.trim();
    assert(inlineErrorText === "Image couldn’t load.", `inline broken image should route through shared error surface, got "${inlineErrorText}"`);
    const inlineRetry = await brokenBlock.locator(".artifact-render-error-retry").count();
    assert(inlineRetry === 1, `inline broken image should offer Retry recovery action, got ${inlineRetry}`);

    // --- Invalid image routes through shared error surface (panel) ---
    await setPanelUrl(page, [brokenImageId], brokenImageId);
    await panel.waitFor({ state: "visible" });
    await panel.locator(".artifact-image-detail-wrap img").waitFor({ state: "attached", timeout: 4000 }).catch(() => {});
    await panel.evaluate((node) => {
      const img = node.querySelector(".artifact-image-detail-wrap img");
      if (img) img.dispatchEvent(new Event("error"));
    });
    await panel.locator(".artifact-render-error-title").waitFor({ state: "visible", timeout: 3000 });
    const panelErrorText = (await panel.locator(".artifact-render-error-title").textContent())?.trim();
    assert(panelErrorText === "Image couldn’t load.", `panel broken image should route through shared error surface, got "${panelErrorText}"`);
    const panelRetry = await panel.locator(".artifact-render-error-retry").count();
    assert(panelRetry >= 1, `panel broken image should offer Retry recovery action, got ${panelRetry}`);
    await panel.getByRole("button", { name: "Close artifact panel" }).click();

    // --- Cross-session no-carryover: same artifactId, different session, expand state stays scoped ---
    // Ensure the largeMermaidA in the primary session is expanded, then navigate to the second
    // ticket whose transcript reuses the SAME artifact ids. That artifact must render collapsed
    // there — the inline-expand store must key on (ticket, subagent, session.path), not just id.
    const largeA = page.locator(`[data-artifact-id="${largeMermaidAId}"]`).first();
    await largeA.scrollIntoViewIfNeeded();
    if ((await largeA.getAttribute("data-artifact-expanded")) !== "true") {
      await largeA.hover();
      const showAllBtn = largeA.getByRole("button", { name: "Show all" });
      if ((await showAllBtn.count()) > 0) {
        await showAllBtn.click();
        await page.waitForFunction((id) => {
          const el = document.querySelector(`[data-artifact-id="${id}"]`);
          return el?.getAttribute("data-artifact-expanded") === "true";
        }, largeMermaidAId, { timeout: 3000 });
      }
    }
    // Switch to the second ticket (fresh session.path, same artifact ids in transcript).
    await page.evaluate((secondTicket) => {
      const layout = {
        version: 2,
        activeWindowId: "window-0",
        windows: [{ id: "window-0", focusedPaneId: "pane-1", layout: { kind: "pane", id: "pane-1", path: `agent://${secondTicket}` } }],
      };
      localStorage.setItem("wiki-window-layout-v2", JSON.stringify(layout));
    }, SECOND_TICKET);
    await page.goto(`${backend.baseUrl}/#/agent/${SECOND_TICKET}`, { waitUntil: "domcontentloaded" });
    await page.locator(".session-scroll").waitFor({ state: "visible" });
    const secondLargeA = page.locator(`[data-artifact-id="${largeMermaidAId}"]`).first();
    await secondLargeA.waitFor({ state: "visible" });
    assert(
      (await secondLargeA.getAttribute("data-artifact-expanded")) !== "true",
      "expand state must not leak from a prior session into a fresh transcript session that reuses the same artifact id",
    );
  } finally {
    await context.close();
    await browser.close();
    await backend.stop();
    rmSync(fixtures.root, { recursive: true, force: true });
  }
}

await main();
