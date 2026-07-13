import { spawnSync } from "node:child_process";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { crc32, deflateSync } from "node:zlib";
import { chromium } from "playwright";

import { makeFixtureRoot, startBackend, writeQueue, writeRegistry } from "../scripts/wiki32-harness.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");
const RUN_ID = "00000000-0000-4000-8000-000000000092";
const TICKET = "WIKI-92";
const SCREENSHOTS = {
  compact: "/tmp/wiki-92-inline-compact.png",
  split: "/tmp/wiki-92-split-pane-tabs.png",
  table: "/tmp/wiki-92-table-sort.png",
  image: "/tmp/wiki-92-image-zoom.png",
  code: "/tmp/wiki-92-code-fold.png",
  narrow: "/tmp/wiki-92-narrow-slideover.png",
};

function pngChunk(type, data) {
  const name = Buffer.from(type, "ascii");
  const size = Buffer.alloc(4);
  size.writeUInt32BE(data.length);
  const checksum = Buffer.alloc(4);
  checksum.writeUInt32BE(crc32(Buffer.concat([name, data])));
  return Buffer.concat([size, name, data, checksum]);
}

function fixturePngBase64() {
  const width = 640;
  const height = 480;
  const scanlines = Buffer.alloc(height * (1 + width * 3));
  for (let y = 0; y < height; y += 1) {
    const row = y * (1 + width * 3);
    for (let x = 0; x < width; x += 1) {
      const offset = row + 1 + x * 3;
      scanlines[offset] = Math.round(40 + 120 * x / width);
      scanlines[offset + 1] = Math.round(70 + 110 * y / height);
      scanlines[offset + 2] = 105;
    }
  }
  const header = Buffer.alloc(13);
  header.writeUInt32BE(width, 0);
  header.writeUInt32BE(height, 4);
  header[8] = 8;
  header[9] = 2;
  return Buffer.concat([
    Buffer.from("89504e470d0a1a0a", "hex"),
    pngChunk("IHDR", header),
    pngChunk("IDAT", deflateSync(scanlines)),
    pngChunk("IEND", Buffer.alloc(0)),
  ]).toString("base64");
}

function artifactInputs() {
  const columns = [
    { key: "id", label: "ID", type: "number" },
    { key: "name", label: "Name", type: "string" },
    { key: "status", label: "Status", type: "string" },
  ];
  const rows = Array.from({ length: 45 }, (_, index) => [index + 1, `artifact-row-${String(index + 1).padStart(2, "0")}`, index % 2 ? "ready" : "queued"]);
  const mermaidLines = ["flowchart LR", ...Array.from({ length: 22 }, (_, index) => `  N${index + 1} --> N${index + 2}`)];
  const code = [
    "export function buildArtifactPanel() {",
    ...Array.from({ length: 126 }, (_, index) => `  const row${index + 1} = ${index + 1};`),
    "  return row126;",
    "}",
  ].join("\n");
  return [
    { kind: "table", title: "Small inline result", payload: { columns, rows: rows.slice(0, 4) } },
    { kind: "table", title: "Large build inventory", payload: { columns, rows } },
    { kind: "mermaid", title: "Artifact dependency map", payload: { source: mermaidLines.join("\n") } },
    { kind: "image", title: "Large release image", payload: { data_base64: fixturePngBase64(), mime: "image/png" } },
    { kind: "code", title: "Artifact panel source", payload: { language: "typescript", filename: "src/artifact-panel.ts", source: code } },
  ];
}

function invokeArtifactTool(fixtures, inputs) {
  const requests = [
    { jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "wiki-92-fixture", version: "1" } } },
    ...inputs.map((input, index) => ({ jsonrpc: "2.0", id: index + 10, method: "tools/call", params: { name: "render_artifact", arguments: input } })),
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
    return { id: response.result.structuredContent.artifact_id, sentinel: response.result.content[0].text };
  });
}

async function writeTranscript(fixtures, inputs, results) {
  const rows = [{ type: "mode", mode: "normal", sessionId: "wiki-92-artifact-panel" }];
  inputs.forEach((input, index) => {
    const id = `toolu_wiki92_${index}`;
    rows.push({ type: "assistant", timestamp: `2026-07-13T15:00:${String(index * 2).padStart(2, "0")}Z`, message: { role: "assistant", content: [{ type: "tool_use", id, name: "mcp__wiki-artifacts__render_artifact", input }] } });
    rows.push({ type: "user", timestamp: `2026-07-13T15:00:${String(index * 2 + 1).padStart(2, "0")}Z`, message: { role: "user", content: [{ type: "tool_result", tool_use_id: id, content: results[index].sentinel }] } });
  });
  const transcript = path.join(fixtures.root, "wiki-92-artifact-panel.jsonl");
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
  const fixtures = makeFixtureRoot("wiki-92-artifact-panel-");
  const inputs = artifactInputs();
  const results = invokeArtifactTool(fixtures, inputs);
  const transcript = await writeTranscript(fixtures, inputs, results);
  writeRegistry(fixtures.registryPath, [[TICKET, transcript]]);
  writeQueue(fixtures.queuePath, TICKET, []);
  const imageIndex = inputs.findIndex((input) => input.kind === "image");
  const liveImage = path.join(fixtures.runtimeDir, "runs", RUN_ID, "artifacts", `${results[imageIndex].id}.png`);
  const archiveArtifacts = path.join(fixtures.root, "archive", TICKET, "20260713-150000", "artifacts");
  await fs.mkdir(archiveArtifacts, { recursive: true });
  await fs.copyFile(liveImage, path.join(archiveArtifacts, `${results[imageIndex].id}.png`));

  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, permissions: ["clipboard-read", "clipboard-write"] });
  const page = await context.newPage();
  page.on("pageerror", (error) => console.error(`[wiki-92-playwright] page error: ${error.message}`));
  page.on("console", (message) => { if (message.type() === "error") console.error(`[wiki-92-playwright] console: ${message.text()}`); });

  const smallId = results[0].id;
  const tableId = results[1].id;
  const mermaidId = results[2].id;
  const imageId = results[3].id;
  const codeId = results[4].id;

  try {
    await page.addInitScript(({ layout }) => {
      localStorage.setItem("wiki-window-layout-v2", JSON.stringify(layout));
      localStorage.setItem("wiki-sidebar-visible", "false");
      localStorage.setItem("wiki-theme", "mono-light");
    }, { layout: sessionLayout() });
    await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
    await page.locator(".session-scroll").waitFor({ state: "visible" });

    const small = page.locator(`[data-artifact-id="${smallId}"]`);
    const largeTable = page.locator(`[data-artifact-id="${tableId}"]`);
    await small.waitFor({ state: "visible" });
    await largeTable.waitFor({ state: "visible" });
    if (await small.getAttribute("data-artifact-compact")) throw new Error("Under-threshold table was compacted");
    if ((await small.locator("tbody tr:not(.artifact-table-spacer)").count()) !== 4) throw new Error("Under-threshold table did not render fully inline");
    if ((await small.getByRole("button", { name: "Open in panel" }).count()) !== 0) throw new Error("Under-threshold artifact gained panel behavior");
    if ((await largeTable.getAttribute("data-artifact-compact")) !== "true") throw new Error("Large table did not compact");
    if ((await largeTable.locator("tbody tr").count()) !== 5) throw new Error("Large table preview did not stop at five rows");
    await largeTable.getByText("+ 40 rows (click to open)").waitFor({ state: "visible" });
    await largeTable.screenshot({ path: SCREENSHOTS.compact });

    await largeTable.getByRole("button", { name: "Open in panel" }).click();
    const panel = page.getByRole("complementary", { name: "Artifact panel" });
    await panel.waitFor({ state: "visible" });
    await panel.getByRole("tab", { name: /Large build inventory/ }).waitFor({ state: "visible" });
    const surfaceBox = await page.locator(".agent-session-surface-row").boundingBox();
    const defaultPanelBox = await panel.boundingBox();
    const defaultRatio = surfaceBox && defaultPanelBox ? defaultPanelBox.width / surfaceBox.width : 0;
    if (Math.abs(defaultRatio - 0.45) > 0.04) throw new Error(`Artifact panel did not default to 55/45: ${defaultRatio}`);
    await largeTable.getByRole("button", { name: "Open in panel" }).click();
    if ((await panel.getByRole("tab", { name: /Large build inventory/ }).count()) !== 1) throw new Error("Opening the same artifact duplicated its tab");

    await setPanelUrl(page, [tableId, mermaidId], mermaidId);
    await panel.getByRole("tab", { name: /Artifact dependency map/ }).waitFor({ state: "visible" });
    if ((await panel.getByRole("tab").count()) !== 2) throw new Error("Second artifact tab did not open");
    await page.screenshot({ path: SCREENSHOTS.split });
    await panel.getByRole("button", { name: /Artifact menu for Artifact dependency map/ }).click();
    const pin = panel.getByRole("menuitem", { name: /Pin to vault/ });
    if (!(await pin.isDisabled()) || (await pin.getAttribute("title")) !== "Coming soon") throw new Error("Pin-to-vault hook is not a disabled Coming soon stub");
    await panel.getByRole("button", { name: /Artifact menu for Artifact dependency map/ }).click();

    const resize = panel.getByLabel("Resize artifact panel");
    const beforeResize = await panel.boundingBox();
    const resizeBox = await resize.boundingBox();
    if (!beforeResize || !resizeBox) throw new Error("Artifact resize divider is not measurable");
    await page.mouse.move(resizeBox.x + 1, resizeBox.y + 100);
    await page.mouse.down();
    await page.mouse.move(resizeBox.x - 70, resizeBox.y + 100, { steps: 4 });
    await page.mouse.up();
    const afterResize = await panel.boundingBox();
    if (!afterResize || afterResize.width <= beforeResize.width + 40) throw new Error("Artifact split-pane divider did not resize the panel");

    await panel.getByRole("tab", { name: /Large build inventory/ }).click();
    const sortId = panel.getByRole("button", { name: "Sort by ID" });
    await sortId.click();
    await sortId.click();
    const firstCell = await panel.locator("tbody tr").first().locator("td").nth(1).innerText();
    if (firstCell !== "45") throw new Error(`Panel table descending sort failed: ${firstCell}`);
    await panel.screenshot({ path: SCREENSHOTS.table });

    await panel.getByRole("tab", { name: /Artifact dependency map/ }).click();
    await panel.getByRole("button", { name: /Close Artifact dependency map/ }).click();
    await panel.getByLabel("Recently closed artifacts").getByRole("button", { name: "Artifact dependency map" }).waitFor({ state: "visible" });
    await panel.getByLabel("Recently closed artifacts").getByRole("button", { name: "Artifact dependency map" }).click();

    await panel.focus();
    await page.keyboard.press("Meta+Shift+BracketLeft");
    if ((await panel.getByRole("tab", { name: /Large build inventory/ }).getAttribute("aria-selected")) !== "true") throw new Error("Previous-tab shortcut did not focus the previous tab");
    await page.keyboard.press("Meta+Shift+BracketRight");
    if ((await panel.getByRole("tab", { name: /Artifact dependency map/ }).getAttribute("aria-selected")) !== "true") throw new Error("Next-tab shortcut did not focus the next tab");
    await page.keyboard.press("Meta+Shift+A");
    await panel.waitFor({ state: "hidden" });
    await page.goBack();
    await panel.waitFor({ state: "visible" });

    await page.reload({ waitUntil: "domcontentloaded" });
    await page.getByRole("complementary", { name: "Artifact panel" }).waitFor({ state: "visible" });
    if ((await page.getByRole("complementary", { name: "Artifact panel" }).getByRole("tab").count()) !== 2) throw new Error("URL deep link did not restore both tabs");

    await setPanelUrl(page, [tableId, mermaidId, imageId], imageId);
    const restoredPanel = page.getByRole("complementary", { name: "Artifact panel" });
    const zoomCanvas = restoredPanel.getByLabel("Image pan and zoom canvas");
    await zoomCanvas.waitFor({ state: "visible" });
    await zoomCanvas.dispatchEvent("wheel", { deltaY: -420 });
    await page.waitForFunction(() => document.querySelector(".artifact-zoom-value")?.textContent !== "100%");
    const zoomText = await restoredPanel.locator(".artifact-zoom-value").innerText();
    if (zoomText === "100%") throw new Error("Image wheel zoom did not change view state");
    await restoredPanel.screenshot({ path: SCREENSHOTS.image });
    await restoredPanel.focus();
    await page.keyboard.press("Meta+0");
    await restoredPanel.getByText("100%", { exact: true }).waitFor({ state: "visible" });

    await setPanelUrl(page, [tableId, mermaidId, imageId, codeId], codeId);
    await restoredPanel.getByRole("button", { name: "Collapse block at line 1" }).click();
    await restoredPanel.getByText(/lines folded/).waitFor({ state: "visible" });
    await restoredPanel.screenshot({ path: SCREENSHOTS.code });
    await restoredPanel.focus();
    await page.keyboard.press("Meta+f");
    await restoredPanel.getByLabel("Find in code").waitFor({ state: "visible" });
    await restoredPanel.getByLabel("Find in code").fill("row126");
    await restoredPanel.getByText("2 matches").waitFor({ state: "visible" });

    await restoredPanel.focus();
    await page.keyboard.press("Meta+w");
    if ((await restoredPanel.getByRole("tab").count()) !== 3) throw new Error("Cmd+W did not close the focused panel tab");
    await restoredPanel.focus();
    await page.keyboard.press("Escape");
    if ((await restoredPanel.getByRole("tab").count()) !== 2) throw new Error("Escape did not close the focused panel tab");

    await setPanelUrl(page, [tableId, imageId], imageId);
    await page.setViewportSize({ width: 800, height: 800 });
    await page.waitForTimeout(200);
    const box = await restoredPanel.boundingBox();
    if (!box || Math.abs(box.x) > 1 || Math.abs(box.y) > 1 || Math.abs(box.width - 800) > 1 || Math.abs(box.height - 800) > 1) {
      throw new Error(`Narrow artifact panel is not fullscreen: ${JSON.stringify(box)}`);
    }
    await page.screenshot({ path: SCREENSHOTS.narrow });

    const url = new URL(page.url());
    for (const key of ["panel", "artifact", "tab", "focus"]) if (!url.searchParams.get(key)) throw new Error(`URL omitted ${key}`);
    await fs.writeFile("/tmp/wiki-92-fixtures.json", JSON.stringify({
      root: fixtures.root,
      registry: fixtures.registryPath,
      transcript,
      queue: fixtures.queuePath,
      runtime: fixtures.runtimeDir,
      archive: path.join(fixtures.root, "archive"),
      status: fixtures.statusDir,
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
}

await main();
