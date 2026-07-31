import { spawnSync } from "node:child_process";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

import { makeFixtureRoot, startBackend, writeQueue } from "../scripts/wiki32-harness.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
// The venv usually lives in the main checkout rather than each worktree.
// Prefer WIKI_PYTHON if set (mirrors wiki32-harness); otherwise try the
// worktree first, then walk up to the main repo checkout.
async function resolvePython() {
  const candidates = [
    process.env.WIKI_PYTHON,
    path.join(ROOT, ".venv", "bin", "python"),
    path.resolve(ROOT, "..", "..", "..", "..", ".venv", "bin", "python"),
    path.resolve(ROOT, "..", "..", "..", ".venv", "bin", "python"),
  ].filter(Boolean);
  for (const candidate of candidates) {
    try {
      await fs.access(candidate);
      return candidate;
    } catch { /* try next */ }
  }
  throw new Error(`No Python runtime found for fixture worker: ${candidates.join(", ")}`);
}
let PYTHON;
const RUN_ID = "00000000-0000-4000-8000-000000000194";
const TICKET = "WIKI-194";

function logStep(message) {
  console.error(`[wiki-194-playwright] ${message}`);
}

// A continuous quantitative x + y plot — plotInteractivity returns full mode
// so the inspector arms wiki_zoom_x, wiki_zoom_y, and wiki_brush.
const CONTINUOUS_SPEC = {
  $schema: "https://vega.github.io/schema/vega-lite/v5.json",
  width: 480,
  height: 280,
  data: {
    values: Array.from({ length: 60 }, (_, i) => ({
      x: i,
      y: Math.sin(i / 4) * 20 + 30,
    })),
  },
  mark: { type: "circle", size: 60 },
  encoding: {
    x: { field: "x", type: "quantitative" },
    y: { field: "y", type: "quantitative" },
  },
};

function artifactInputs() {
  return [
    {
      kind: "plot",
      title: "Continuous scatter",
      caption: "Continuous x/y — full-mode fixture for WIKI-194 interaction tests",
      payload: { spec_vega_lite: CONTINUOUS_SPEC },
    },
  ];
}

function invokeFixtureWorker(fixtures, inputs) {
  const requests = [
    {
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: {
        protocolVersion: "2025-06-18",
        capabilities: {},
        clientInfo: { name: "wiki-194-fixture", version: "1" },
      },
    },
    ...inputs.map((input, index) => ({
      jsonrpc: "2.0",
      id: index + 10,
      method: "tools/call",
      params: { name: "render_artifact", arguments: input },
    })),
  ];
  const result = spawnSync(PYTHON, ["-m", "backend.app.wiki_artifacts"], {
    cwd: ROOT,
    env: { ...process.env, WIKI_AGENT_RUNTIME_DIR: fixtures.runtimeDir, WIKI_RUN_ID: RUN_ID },
    input: requests.map((request) => JSON.stringify(request)).join("\n") + "\n",
    encoding: "utf8",
  });
  if (result.status !== 0) {
    throw new Error(`artifact fixture worker failed: ${result.stderr || result.stdout}`);
  }
  const responses = result.stdout.trim().split("\n").map((line) => JSON.parse(line));
  return responses.slice(1).map((response) => {
    if (response.result?.isError) throw new Error(response.result.content?.[0]?.text || "artifact rejected");
    if (!response.result?.content?.[0]?.text) {
      throw new Error(`fixture response missing content: ${JSON.stringify(response).slice(0, 400)}`);
    }
    const sentinel = response.result.content[0].text;
    const event = JSON.parse(sentinel.slice("<<wiki-artifact:v1>>".length, -"<<end>>".length));
    return { artifactId: event.id, sentinel };
  });
}

async function writeTranscript(fixtures, inputs, results) {
  const rows = [{ type: "mode", mode: "normal", sessionId: "wiki-194-plot" }];
  inputs.forEach((input, index) => {
    const id = `toolu_artifact_${index + 1}`;
    rows.push({
      type: "assistant",
      timestamp: `2026-07-31T14:00:${String(index * 2).padStart(2, "0")}Z`,
      message: {
        role: "assistant",
        content: [{ type: "tool_use", id, name: "mcp__wiki-artifacts__render_artifact", input }],
      },
    });
    rows.push({
      type: "user",
      timestamp: `2026-07-31T14:00:${String(index * 2 + 1).padStart(2, "0")}Z`,
      message: {
        role: "user",
        content: [{ type: "tool_result", tool_use_id: id, content: results[index].sentinel }],
      },
    });
  });
  const target = path.join(fixtures.root, "wiki-194-plot.jsonl");
  await fs.writeFile(target, rows.map((row) => JSON.stringify(row)).join("\n") + "\n");
  return target;
}

async function writeRegistry(fixtures, transcript) {
  const registry = {
    _orchestrators: {
      [TICKET]: {
        window: "@9999",
        spawned_at: "2026-07-31T14:00:00Z",
        kind: "cc",
        transcript,
      },
    },
  };
  await fs.writeFile(fixtures.registryPath, JSON.stringify(registry, null, 2));
}

function sessionLayout() {
  return {
    version: 2,
    activeWindowId: "window-0",
    windows: [
      {
        id: "window-0",
        focusedPaneId: "pane-1",
        layout: { kind: "pane", id: "pane-1", path: `agent://${TICKET}` },
      },
    ],
  };
}

async function waitForVegaView(page, selector) {
  await page.waitForFunction((sel) => {
    const el = document.querySelector(sel);
    return el && el.__wikiVegaView;
  }, selector, { timeout: 10_000 });
}

async function scaleDomain(page, selector, channel) {
  return page.evaluate(
    ({ sel, ch }) => {
      const view = document.querySelector(sel).__wikiVegaView;
      const domain = view.scale(ch).domain();
      return [Number(domain[0]), Number(domain[1])];
    },
    { sel: selector, ch: channel },
  );
}

function domainsChanged(before, after, tolerance = 0.001) {
  return Math.abs(before[0] - after[0]) > tolerance || Math.abs(before[1] - after[1]) > tolerance;
}

async function main() {
  PYTHON = await resolvePython();
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
    acceptDownloads: true,
  });
  const page = await context.newPage();
  page.on("pageerror", (error) => logStep(`browser page error: ${error.message}`));

  logStep("creating isolated fixtures for the continuous plot");
  const fixtures = makeFixtureRoot("wiki-194-plot-");
  const inputs = artifactInputs();
  const results = invokeFixtureWorker(fixtures, inputs);
  const transcript = await writeTranscript(fixtures, inputs, results);
  await writeRegistry(fixtures, transcript);
  writeQueue(fixtures.queuePath, TICKET, []);

  // Plot artifacts store their spec inline, so there may be no artifact
  // files to mirror. Only copy if the artifacts dir exists.
  const runArtifacts = path.join(fixtures.runtimeDir, "runs", RUN_ID, "artifacts");
  const archiveArtifacts = path.join(fixtures.root, "archive", TICKET, "20260731-140000", "artifacts");
  try {
    const files = await fs.readdir(runArtifacts);
    await fs.mkdir(archiveArtifacts, { recursive: true });
    for (const file of files) {
      await fs.copyFile(path.join(runArtifacts, file), path.join(archiveArtifacts, file));
    }
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }

  logStep("starting the isolated worktree backend");
  const backend = await startBackend(fixtures);

  try {
    await page.addInitScript(({ layout }) => {
      localStorage.setItem("wiki-window-layout-v2", JSON.stringify(layout));
      localStorage.setItem("wiki-sidebar-visible", "false");
      localStorage.setItem("wiki-theme", "mono-light");
    }, { layout: sessionLayout() });
    await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
    await page.locator(".session-scroll").waitFor({ state: "visible" });

    // ─── inline plot never consumes page wheel scroll ────────────────────
    logStep("inline plot renders and does NOT arm interaction (page wheel passthrough)");
    const inlinePlot = page.locator('[data-artifact-kind="plot"] .artifact-plot').first();
    await inlinePlot.waitFor({ state: "visible" });
    await waitForVegaView(page, '[data-artifact-kind="plot"] .artifact-plot');
    const inlineDomainBefore = await scaleDomain(page, '[data-artifact-kind="plot"] .artifact-plot', "x");
    // Wheel over the inline plot. Since armed=false, wiki_zoom_x isn't
    // injected, so wheel bubbles to the page scroll (not consumed by Vega).
    const inlineBox = await inlinePlot.boundingBox();
    await page.mouse.move(inlineBox.x + inlineBox.width / 2, inlineBox.y + inlineBox.height / 2);
    await page.mouse.wheel(0, 200);
    const inlineDomainAfter = await scaleDomain(page, '[data-artifact-kind="plot"] .artifact-plot', "x");
    if (domainsChanged(inlineDomainBefore, inlineDomainAfter)) {
      throw new Error(
        `inline plot consumed wheel (x domain moved from ${inlineDomainBefore} to ${inlineDomainAfter})`,
      );
    }

    // ─── open the inspector: full-mode interaction is armed ──────────────
    logStep("open plot in the artifact inspector (full mode: pan/wheel/brush live)");
    await inlinePlot.hover();
    await page.keyboard.press("ControlOrMeta+Enter");
    const inspectorPlot = page.locator(".artifact-inspector .artifact-plot");
    await inspectorPlot.waitFor({ state: "visible" });
    await waitForVegaView(page, ".artifact-inspector .artifact-plot");

    const inspectorSelector = ".artifact-inspector .artifact-plot";
    const xBefore = await scaleDomain(page, inspectorSelector, "x");
    const yBefore = await scaleDomain(page, inspectorSelector, "y");

    // ─── real wheel gesture zooms the x scale ────────────────────────────
    logStep("wheel over the inspector plot moves x scale (armed)");
    const plotBox = await inspectorPlot.boundingBox();
    const centerX = plotBox.x + plotBox.width / 2;
    const centerY = plotBox.y + plotBox.height / 2;
    await page.mouse.move(centerX, centerY);
    await page.mouse.wheel(0, -400);
    await page.waitForTimeout(120);
    const xAfterWheel = await scaleDomain(page, inspectorSelector, "x");
    if (!domainsChanged(xBefore, xAfterWheel)) {
      throw new Error(`wheel-zoom did not move x domain (still ${xAfterWheel})`);
    }

    // ─── real drag pans the scale ────────────────────────────────────────
    logStep("drag pans the x scale (armed)");
    const xBeforeDrag = await scaleDomain(page, inspectorSelector, "x");
    await page.mouse.move(centerX, centerY);
    await page.mouse.down();
    await page.mouse.move(centerX - 60, centerY, { steps: 8 });
    await page.mouse.up();
    await page.waitForTimeout(120);
    const xAfterDrag = await scaleDomain(page, inspectorSelector, "x");
    if (!domainsChanged(xBeforeDrag, xAfterDrag)) {
      throw new Error(`drag-pan did not move x domain (still ${xAfterDrag})`);
    }

    // ─── shift-drag brush: scale changes ONLY after pointerup ────────────
    logStep("shift-drag brush: scale does not change mid-gesture, only on pointerup");
    const xBeforeBrush = await scaleDomain(page, inspectorSelector, "x");
    const yBeforeBrush = await scaleDomain(page, inspectorSelector, "y");
    await page.keyboard.down("Shift");
    await page.mouse.move(centerX - 100, centerY - 40);
    await page.mouse.down();
    await page.mouse.move(centerX - 30, centerY, { steps: 4 });
    // Mid-drag: scale MUST NOT have changed yet — the buffer holds pending
    // until pointerup so a mid-gesture re-embed can't kill the brush.
    const xMidBrush = await scaleDomain(page, inspectorSelector, "x");
    const yMidBrush = await scaleDomain(page, inspectorSelector, "y");
    if (domainsChanged(xBeforeBrush, xMidBrush) || domainsChanged(yBeforeBrush, yMidBrush)) {
      await page.mouse.up();
      await page.keyboard.up("Shift");
      throw new Error(
        `brush changed the scale mid-gesture (x ${xBeforeBrush} → ${xMidBrush}, y ${yBeforeBrush} → ${yMidBrush})`,
      );
    }
    await page.mouse.move(centerX + 60, centerY + 40, { steps: 4 });
    await page.mouse.up();
    await page.keyboard.up("Shift");
    await page.waitForTimeout(200);
    const xAfterBrush = await scaleDomain(page, inspectorSelector, "x");
    const yAfterBrush = await scaleDomain(page, inspectorSelector, "y");
    if (!domainsChanged(xBeforeBrush, xAfterBrush) || !domainsChanged(yBeforeBrush, yAfterBrush)) {
      throw new Error(
        `brush pointerup did not commit domains (x ${xBeforeBrush} → ${xAfterBrush}, y ${yBeforeBrush} → ${yAfterBrush})`,
      );
    }

    // ─── Reset button restores the default view ──────────────────────────
    logStep("Reset zoom button restores the default view");
    await page.locator(".artifact-inspector [data-panel-reset-zoom]").click();
    await waitForVegaView(page, inspectorSelector);
    await page.waitForTimeout(120);
    const xAfterReset = await scaleDomain(page, inspectorSelector, "x");
    const yAfterReset = await scaleDomain(page, inspectorSelector, "y");
    if (Math.abs(xAfterReset[0] - xBefore[0]) > 1 || Math.abs(xAfterReset[1] - xBefore[1]) > 1) {
      throw new Error(`Reset zoom did not restore x (expected ~${xBefore}, got ${xAfterReset})`);
    }
    if (Math.abs(yAfterReset[0] - yBefore[0]) > 1 || Math.abs(yAfterReset[1] - yBefore[1]) > 1) {
      throw new Error(`Reset zoom did not restore y (expected ~${yBefore}, got ${yAfterReset})`);
    }

    // ─── Close inspector; open the artifact in the side panel ───────────
    logStep("close inspector and open the artifact in the side panel");
    await page.keyboard.press("Escape");
    await page.locator(".artifact-inspector").waitFor({ state: "detached" });
    // Panel is URL-driven — same pattern wiki-156 uses.
    const plotArtifactId = results[0].artifactId;
    await page.evaluate(({ ticket, artifactId }) => {
      const url = new URL(window.location.href);
      url.searchParams.set("panel", ticket);
      url.searchParams.set("artifact", artifactId);
      url.searchParams.set("tab", artifactId);
      url.searchParams.set("focus", artifactId);
      history.pushState(null, "", url);
      window.dispatchEvent(new PopStateEvent("popstate"));
    }, { ticket: TICKET, artifactId: plotArtifactId });
    const panelPlot = page.locator(".artifact-panel .artifact-plot");
    await panelPlot.waitFor({ state: "visible" });
    await waitForVegaView(page, ".artifact-panel .artifact-plot");
    const panelBox = await panelPlot.boundingBox();
    // Push the plot out of default state by wheeling once.
    const panelXBefore = await scaleDomain(page, ".artifact-panel .artifact-plot", "x");
    await page.mouse.move(panelBox.x + panelBox.width / 2, panelBox.y + panelBox.height / 2);
    await page.mouse.wheel(0, -400);
    await page.waitForTimeout(120);
    const panelXDirty = await scaleDomain(page, ".artifact-panel .artifact-plot", "x");
    if (!domainsChanged(panelXBefore, panelXDirty)) {
      throw new Error("panel plot wheel did not zoom");
    }
    // Cmd/Ctrl+0 must fire the plot Reset button (R9F3).
    logStep("Cmd/Ctrl+0 keyboard shortcut resets the plot from the panel");
    await page.locator(".artifact-panel").click();
    await page.keyboard.press("ControlOrMeta+0");
    await waitForVegaView(page, ".artifact-panel .artifact-plot");
    await page.waitForTimeout(120);
    const panelXAfterShortcut = await scaleDomain(page, ".artifact-panel .artifact-plot", "x");
    if (Math.abs(panelXAfterShortcut[0] - panelXBefore[0]) > 1
      || Math.abs(panelXAfterShortcut[1] - panelXBefore[1]) > 1) {
      throw new Error(
        `Cmd/Ctrl+0 did not restore x (expected ~${panelXBefore}, got ${panelXAfterShortcut})`,
      );
    }

    // ─── PNG download uses the artifact title for the filename ───────────
    logStep("PNG download names the file after the artifact title (via panel plot)");
    const downloadPromise = page.waitForEvent("download");
    await page.locator(".artifact-panel").getByRole("button", { name: /Save as PNG/i }).click();
    const download = await downloadPromise;
    const suggested = download.suggestedFilename();
    if (!suggested.endsWith(".png")) {
      throw new Error(`unexpected download extension: ${suggested}`);
    }
    if (!suggested.includes("Continuous")) {
      throw new Error(`download filename should reflect the artifact title, got ${suggested}`);
    }

    logStep(`isolated fixture root: ${fixtures.root}`);
  } finally {
    await context.close();
    await browser.close();
    await backend.stop();
  }

  logStep("interactive-plot browser verification complete");
}

await main();
