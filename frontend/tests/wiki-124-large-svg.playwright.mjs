import { spawnSync } from "node:child_process";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

import { makeFixtureRoot, startBackend, writeQueue, writeRegistry } from "../scripts/wiki32-harness.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");
const RUN_ID = "00000000-0000-4000-8000-000000000124";
const TICKET = "WIKI-124";

function svgProbe(targetBytes, width, height) {
  const prefix = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}">`;
  const suffix = `<rect width="${width}" height="${height}" fill="#4f6756"/></svg>`;
  const padding = "x".repeat(Math.max(0, targetBytes - prefix.length - suffix.length - 13));
  return `${prefix}<title>${padding}</title>${suffix}`;
}

function invokeArtifactTool(fixtures, inputs) {
  const requests = [
    {
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "wiki-124-fixture", version: "1" } },
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
  const rows = [{ type: "mode", mode: "normal", sessionId: "wiki-124-large-svg" }];
  inputs.forEach((input, index) => {
    const id = `toolu_wiki124_${index}`;
    rows.push({
      type: "assistant",
      timestamp: `2026-07-15T15:00:${String(index * 2).padStart(2, "0")}Z`,
      message: { role: "assistant", content: [{ type: "tool_use", id, name: "mcp__wiki-artifacts__render_artifact", input }] },
    });
    rows.push({
      type: "user",
      timestamp: `2026-07-15T15:00:${String(index * 2 + 1).padStart(2, "0")}Z`,
      message: { role: "user", content: [{ type: "tool_result", tool_use_id: id, content: results[index].sentinel }] },
    });
  });
  const transcript = path.join(fixtures.root, "wiki-124-large-svg.jsonl");
  await fs.writeFile(transcript, `${rows.map((row) => JSON.stringify(row)).join("\n")}\n`);
  return transcript;
}

async function main() {
  const fixtures = makeFixtureRoot("wiki-124-large-svg-");
  const sizes = [800, 8_000, 16_000, 24_000, 32_000, 64_000];
  const sources = sizes.map((size) => svgProbe(size, size === 800 ? 320 : 800, size === 800 ? 240 : 600));
  const inputs = sources.map((source, index) => ({
    kind: "svg",
    title: `SVG size probe ${sizes[index]} bytes`,
    payload: { source },
  }));
  const actualSizes = sources.map((source) => Buffer.byteLength(source));
  if (actualSizes[0] >= 2_000 || actualSizes[3] < 20_000 || actualSizes[3] > 30_000) {
    throw new Error(`SVG probes missed target sizes: ${actualSizes.join(", ")}`);
  }
  const results = invokeArtifactTool(fixtures, inputs);
  const transcript = await writeTranscript(fixtures, inputs, results);
  writeRegistry(fixtures.registryPath, [[TICKET, transcript]]);
  writeQueue(fixtures.queuePath, TICKET, []);

  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  try {
    await page.addInitScript(() => {
      localStorage.setItem("wiki-window-layout-v2", JSON.stringify({
        version: 2,
        activeWindowId: "window-0",
        windows: [{ id: "window-0", focusedPaneId: "pane-1", layout: { kind: "pane", id: "pane-1", path: "agent://WIKI-124" } }],
      }));
      localStorage.setItem("wiki-sidebar-visible", "false");
    });
    await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
    await page.locator(".session-scroll").waitFor({ state: "visible" });
    for (const [index, result] of results.entries()) {
      const artifact = page.locator(`[data-artifact-id="${result.id}"]`);
      await artifact.locator(".artifact-svg > svg").waitFor({ state: "visible" });
      const metrics = await artifact.locator(".artifact-svg > svg").evaluate((element) => ({
        height: element.getBoundingClientRect().height,
        width: element.getBoundingClientRect().width,
      }));
      if (metrics.width <= 0 || metrics.height <= 0) {
        throw new Error(`SVG size probe ${sizes[index]} rendered with empty bounds: ${JSON.stringify(metrics)}`);
      }
      if (index === 0 && await artifact.getAttribute("data-artifact-compact")) {
        throw new Error("800-byte SVG was unexpectedly compacted");
      }
      if (index > 0 && (await artifact.getAttribute("data-artifact-compact")) !== "true") {
        throw new Error(`${actualSizes[index]}-byte SVG did not enter the inspectable compact preview`);
      }
    }
  } finally {
    await browser.close();
    await backend.stop();
  }
  console.error(`[wiki-124-playwright] SVG size probes rendered: ${actualSizes.join(", ")} bytes`);
}

await main();
