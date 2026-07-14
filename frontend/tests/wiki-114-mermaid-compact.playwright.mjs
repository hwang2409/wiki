import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

import { makeFixtureRoot, startBackend, writeQueue, writeRegistry } from "../scripts/wiki32-harness.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const PYTHON = [
  process.env.WIKI_PYTHON,
  path.join(ROOT, ".venv", "bin", "python"),
  path.resolve(ROOT, "..", "..", "..", ".venv", "bin", "python"),
].find((candidate) => candidate && existsSync(candidate));
const RUN_ID = "00000000-0000-4000-8000-000000000114";
const TICKET = "WIKI-114";
const THEME = process.env.WIKI_114_THEME ?? "mono-light";
const SCREENSHOT_SUFFIX = process.env.WIKI_114_SCREENSHOT_SUFFIX ?? "";
const CAPTURE_LEGACY_PREVIEW = process.env.WIKI_114_CAPTURE_LEGACY_PREVIEW === "1";
const SCREENSHOTS = {
  compact: `/tmp/wiki-114-large-mermaid-compact${SCREENSHOT_SUFFIX}.png`,
  wide: `/tmp/wiki-114-wide-mermaid-compact${SCREENSHOT_SUFFIX}.png`,
  detail: `/tmp/wiki-114-large-mermaid-detail${SCREENSHOT_SUFFIX}.png`,
  legacy: "/tmp/wiki-114-large-mermaid-before.png",
};

function logStep(message) {
  console.error(`[wiki-114-playwright] ${message}`);
}

function largeMermaidSource() {
  const node = (index) => `N${index}["Checkpoint ${index}: owner handoff"]`;
  return [
    "flowchart TD",
    ...Array.from({ length: 33 }, (_, index) => `  ${node(index + 1)} --> ${node(index + 2)}`),
  ].join("\n");
}

function wideMermaidSource() {
  const node = (index) => `W${index}["Wide checkpoint ${index}: owner handoff"]`;
  return [
    "flowchart LR",
    ...Array.from({ length: 24 }, (_, index) => `  ${node(index + 1)} --> ${node(index + 2)}`),
  ].join("\n");
}

function artifactInputs() {
  return [
    {
      kind: "mermaid",
      title: "Large delivery dependency map",
      payload: { source: largeMermaidSource() },
    },
    {
      kind: "mermaid",
      title: "Wide delivery dependency map",
      payload: { source: wideMermaidSource() },
    },
    {
      kind: "mermaid",
      title: "Small release path",
      payload: { source: "flowchart LR\n  Plan --> Build\n  Build --> Ship" },
    },
    {
      kind: "svg",
      title: "Large SVG dependency map",
      payload: {
        source: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 900"><rect width="640" height="900" fill="#fff"/><text x="28" y="64" font-size="24">SVG diagram stays at native scale</text></svg>',
      },
    },
  ];
}

function invokeArtifactTool(fixtures, inputs) {
  if (!PYTHON) throw new Error("No Python runtime found for the isolated artifact fixture");
  const requests = [
    { jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "wiki-114-fixture", version: "1" } } },
    ...inputs.map((input, index) => ({ jsonrpc: "2.0", id: index + 10, method: "tools/call", params: { name: "render_artifact", arguments: input } })),
  ];
  const result = spawnSync(PYTHON, ["-m", "backend.app.wiki_artifacts"], {
    cwd: ROOT,
    env: { ...process.env, WIKI_AGENT_RUNTIME_DIR: fixtures.runtimeDir, WIKI_RUN_ID: RUN_ID },
    input: `${requests.map((request) => JSON.stringify(request)).join("\n")}\n`,
    encoding: "utf8",
  });
  if (result.status !== 0) throw new Error(result.stderr || result.stdout);
  return result.stdout.trim().split("\n").slice(1).map((line) => {
    const response = JSON.parse(line);
    if (response.result?.isError) throw new Error(response.result.content?.[0]?.text || "artifact rejected");
    const sentinel = response.result.content[0].text;
    return { id: JSON.parse(sentinel.slice("<<wiki-artifact:v1>>".length, -"<<end>>".length)).id, sentinel };
  });
}

async function writeTranscript(fixtures, inputs, results) {
  const rows = [{ type: "mode", mode: "normal", sessionId: "wiki-114-mermaid-compact" }];
  inputs.forEach((input, index) => {
    const id = `toolu_wiki114_${index}`;
    rows.push({ type: "assistant", timestamp: `2026-07-14T15:00:0${index * 2}Z`, message: { role: "assistant", content: [{ type: "tool_use", id, name: "mcp__wiki-artifacts__render_artifact", input }] } });
    rows.push({ type: "user", timestamp: `2026-07-14T15:00:0${index * 2 + 1}Z`, message: { role: "user", content: [{ type: "tool_result", tool_use_id: id, content: results[index].sentinel }] } });
  });
  const transcript = path.join(fixtures.root, "wiki-114-mermaid-compact.jsonl");
  await fs.writeFile(transcript, `${rows.map((row) => JSON.stringify(row)).join("\n")}\n`);
  return transcript;
}

function sessionLayout() {
  return { version: 2, activeWindowId: "window-0", windows: [{ id: "window-0", focusedPaneId: "pane-1", layout: { kind: "pane", id: "pane-1", path: `agent://${TICKET}` } }] };
}

async function main() {
  logStep("creating isolated MCP, transcript, registry, runtime, queue, state, session, vault, and account-home fixtures");
  const fixtures = makeFixtureRoot("wiki-114-mermaid-compact-");
  const uiStatePath = path.join(fixtures.root, "ui-state.json");
  process.env.WIKI_UI_STATE_PATH = uiStatePath;
  const inputs = artifactInputs();
  const results = invokeArtifactTool(fixtures, inputs);
  const transcript = await writeTranscript(fixtures, inputs, results);
  writeRegistry(fixtures.registryPath, [[TICKET, transcript]]);
  writeQueue(fixtures.queuePath, TICKET, []);

  logStep("starting isolated worktree backend");
  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  page.on("pageerror", (error) => console.error(`[wiki-114-playwright] page error: ${error.message}`));
  page.on("console", (message) => { if (message.type() === "error") console.error(`[wiki-114-playwright] console: ${message.text()}`); });

  try {
    await page.addInitScript(({ layout, theme }) => {
      localStorage.setItem("wiki-window-layout-v2", JSON.stringify(layout));
      localStorage.setItem("wiki-sidebar-visible", "false");
      localStorage.setItem("wiki-theme", theme);
    }, { layout: sessionLayout(), theme: THEME });
    await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
    await page.locator(".session-scroll").waitFor({ state: "visible" });

    const large = page.locator(`[data-artifact-id="${results[0].id}"]`);
    const wide = page.locator(`[data-artifact-id="${results[1].id}"]`);
    const small = page.locator(`[data-artifact-id="${results[2].id}"]`);
    const largeSvg = page.locator(`[data-artifact-id="${results[3].id}"]`);
    const preview = large.locator(".artifact-compact-diagram");
    await preview.waitFor({ state: "visible" });
    await large.locator(".artifact-mermaid svg").waitFor({ state: "visible" });
    await wide.locator(".artifact-mermaid svg").waitFor({ state: "visible" });
    await small.locator(".artifact-mermaid svg").waitFor({ state: "visible" });
    await largeSvg.locator(".artifact-svg > svg").waitFor({ state: "visible" });

    if (CAPTURE_LEGACY_PREVIEW) {
      const legacySvg = large.locator(".artifact-mermaid svg");
      const legacyState = await legacySvg.evaluate((element) => ({
        height: element.getAttribute("height"),
        style: element.getAttribute("style"),
        width: element.getAttribute("width"),
      }));
      await legacySvg.evaluate((element) => {
        element.removeAttribute("style");
        element.setAttribute("width", "100%");
        element.removeAttribute("height");
      });
      const legacyStyles = await page.addStyleTag({ content: `
        .artifact-compact-diagram { height: auto !important; overflow: visible !important; border: 0 !important; background: transparent !important; }
        .artifact-compact-diagram .artifact-mermaid { display: grid !important; width: 100% !important; max-width: 100% !important; min-height: 120px !important; place-items: center !important; overflow: auto !important; }
        .artifact-compact-diagram .artifact-mermaid svg { max-width: 400px !important; max-height: 400px !important; }
        .artifact-compact-diagram::after, .artifact-compact-diagram::before, .artifact-compact-diagram-hint { display: none !important; }
      ` });
      await large.screenshot({ path: SCREENSHOTS.legacy });
      await legacyStyles.evaluate((element) => element.remove());
      await legacySvg.evaluate((element, state) => {
        if (state.width === null) element.removeAttribute("width");
        else element.setAttribute("width", state.width);
        if (state.height === null) element.removeAttribute("height");
        else element.setAttribute("height", state.height);
        if (state.style === null) element.removeAttribute("style");
        else element.setAttribute("style", state.style);
      }, legacyState);
    }

    if ((await large.getAttribute("data-artifact-compact")) !== "true") throw new Error("30+ node Mermaid diagram did not compact");
    const previewHeight = await preview.evaluate((element) => element.getBoundingClientRect().height);
    if (Math.abs(previewHeight - 400) > 1) throw new Error(`Compact Mermaid viewport height changed: ${previewHeight}`);
    const renderedHeight = await large.locator(".artifact-mermaid svg").evaluate((element) => element.getBoundingClientRect().height);
    if (renderedHeight <= 400) throw new Error(`Large Mermaid SVG was scaled down instead of cropped: ${renderedHeight}`);
    await large.getByText("Diagram continues · Click to inspect").waitFor({ state: "visible" });
    await large.screenshot({ path: SCREENSHOTS.compact });

    if ((await wide.getAttribute("data-artifact-compact")) !== "true") throw new Error("Wide Mermaid diagram did not compact");
    const wideMetrics = await wide.locator(".artifact-mermaid svg").evaluate((element) => {
      const viewBox = element.viewBox.baseVal;
      const label = element.querySelector(".nodeLabel, .nodeLabel *, text, foreignObject *");
      return {
        effectiveFontSize: label ? parseFloat(getComputedStyle(label).fontSize) * element.getBoundingClientRect().width / viewBox.width : 0,
        renderedWidth: element.getBoundingClientRect().width,
        viewBoxWidth: viewBox.width,
      };
    });
    if (Math.abs(wideMetrics.renderedWidth - wideMetrics.viewBoxWidth) > 1) throw new Error(`Wide Mermaid SVG did not retain its viewBox width: ${JSON.stringify(wideMetrics)}`);
    if (wideMetrics.effectiveFontSize < 9) throw new Error(`Wide Mermaid label fell below the legibility floor: ${JSON.stringify(wideMetrics)}`);
    logStep(`wide Mermaid metrics: ${JSON.stringify(wideMetrics)}`);
    await wide.getByText("Diagram continues · Click to inspect").waitFor({ state: "visible" });
    await wide.screenshot({ path: SCREENSHOTS.wide });

    if ((await largeSvg.getAttribute("data-artifact-compact")) !== "true") throw new Error("Large SVG diagram did not compact");
    const svgMetrics = await largeSvg.locator(".artifact-svg > svg").evaluate((element) => ({
      renderedHeight: element.getBoundingClientRect().height,
      renderedWidth: element.getBoundingClientRect().width,
      viewBoxHeight: element.viewBox.baseVal.height,
      viewBoxWidth: element.viewBox.baseVal.width,
    }));
    if (Math.abs(svgMetrics.renderedWidth - svgMetrics.viewBoxWidth) > 1 || Math.abs(svgMetrics.renderedHeight - svgMetrics.viewBoxHeight) > 1) {
      throw new Error(`ViewBox-only SVG did not retain its intrinsic dimensions: ${JSON.stringify(svgMetrics)}`);
    }
    logStep(`viewBox-only SVG metrics: ${JSON.stringify(svgMetrics)}`);
    await largeSvg.getByText("Diagram continues · Click to inspect").waitFor({ state: "visible" });

    if (await small.getAttribute("data-artifact-compact")) throw new Error("Small Mermaid diagram was unexpectedly compacted");
    if ((await small.locator(".artifact-compact-diagram").count()) !== 0) throw new Error("Small Mermaid diagram gained crop chrome");
    if ((await small.getByText("Diagram continues · Click to inspect").count()) !== 0) throw new Error("Small Mermaid diagram gained an inspection affordance");

    await large.locator(".artifact-body").click();
    const panel = page.getByRole("complementary", { name: "Artifact panel" });
    await panel.waitFor({ state: "visible" });
    await panel.getByLabel("Mermaid pan and zoom canvas").waitFor({ state: "visible" });
    await panel.locator(".artifact-mermaid svg").waitFor({ state: "visible" });
    await panel.screenshot({ path: SCREENSHOTS.detail });

    await fs.writeFile("/tmp/wiki-114-fixtures.json", JSON.stringify({
      root: fixtures.root,
      registry: fixtures.registryPath,
      transcript,
      queue: fixtures.queuePath,
      runtime: fixtures.runtimeDir,
      status: fixtures.statusDir,
      archive: path.join(fixtures.root, "archive"),
      knowledge_db: path.join(fixtures.root, "knowledge.db"),
      ui_state: uiStatePath,
      codex_sessions: fixtures.sessionsDir,
      tmp: path.join(fixtures.root, "tmp"),
      vault: path.join(fixtures.root, "vault"),
      claude_projects: path.join(fixtures.root, "claude-projects"),
      account_home: path.join(fixtures.root, "account-home"),
      supervisor_socket: fixtures.supervisorSocketPath,
      screenshots: SCREENSHOTS,
    }, null, 2));
  } finally {
    await context.close();
    await browser.close();
    await backend.stop();
  }

  logStep("compact Mermaid preview verification complete");
}

await main();
