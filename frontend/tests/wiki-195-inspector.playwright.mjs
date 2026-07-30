import { spawnSync } from "node:child_process";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { crc32, deflateSync } from "node:zlib";
import { chromium } from "playwright";

import {
  makeFixtureRoot,
  startBackend,
  writeQueue,
} from "../scripts/wiki32-harness.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");
const RUN_ID = "00000000-0000-4000-8000-000000000195";
const TICKET = "WIKI-195";
const SCREENSHOTS = Object.fromEntries(
  ["mermaid", "svg", "image", "table", "plot", "code"].map((kind) => [
    kind,
    `/tmp/wiki-195-inspector-${kind}.png`,
  ])
);

function pngChunk(type, data) {
  const name = Buffer.from(type, "ascii");
  const size = Buffer.alloc(4);
  size.writeUInt32BE(data.length);
  const checksum = Buffer.alloc(4);
  checksum.writeUInt32BE(crc32(Buffer.concat([name, data])));
  return Buffer.concat([size, name, data, checksum]);
}

function fixturePngBase64() {
  const width = 360;
  const height = 120;
  const scanlines = Buffer.alloc(height * (1 + width * 3));
  for (let y = 0; y < height; y += 1) {
    const row = y * (1 + width * 3);
    for (let x = 0; x < width; x += 1) {
      const offset = row + 1 + x * 3;
      scanlines[offset] = (x * 255 / width) | 0;
      scanlines[offset + 1] = 180;
      scanlines[offset + 2] = (y * 255 / height) | 0;
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

const PNG_BASE64 = fixturePngBase64();

function logStep(message) {
  console.error(`[wiki-195-playwright] ${message}`);
}

const MERMAID_SOURCE = "flowchart LR\n  Plan --> Build\n  Build --> Ship";

const PNG_SIGNATURE = [137, 80, 78, 71, 13, 10, 26, 10];

async function generateBrowserImageFixtures(page) {
  return page.evaluate(() => {
    const canvas = document.createElement("canvas");
    canvas.width = 96;
    canvas.height = 64;
    const context = canvas.getContext("2d");
    context.fillStyle = "#4f6756";
    context.fillRect(0, 0, 96, 64);
    context.fillStyle = "#e8e5df";
    context.fillRect(16, 16, 64, 32);
    const strip = (dataUrl) => dataUrl.slice(dataUrl.indexOf(",") + 1);
    return {
      jpeg: strip(canvas.toDataURL("image/jpeg", 0.9)),
      webp: strip(canvas.toDataURL("image/webp", 0.9)),
    };
  });
}

async function assertClipboardHoldsPng(page, label) {
  const signature = await page.evaluate(async () => {
    const items = await navigator.clipboard.read();
    const item = items[0];
    if (!item.types.includes("image/png")) {
      throw new Error(`clipboard types are ${item.types.join(", ")}`);
    }
    const blob = await item.getType("image/png");
    const bytes = new Uint8Array(await blob.arrayBuffer());
    return Array.from(bytes.slice(0, 8));
  });
  if (JSON.stringify(signature) !== JSON.stringify(PNG_SIGNATURE)) {
    throw new Error(`${label}: clipboard payload is not a PNG (${signature.join(",")})`);
  }
}

async function copyImageAndAssertPng(page, label) {
  await page.locator(".artifact-inspector").getByTitle("Copy image").click();
  await page.locator(".artifact-inspector").getByText("Copied", { exact: true }).waitFor({ state: "visible" });
  await assertClipboardHoldsPng(page, label);
}

function artifactInputs(browserImages) {
  const rows = Array.from({ length: 8 }, (_, index) => [
    index + 1,
    `row-${String(index + 1).padStart(2, "0")}`,
    `2026-07-${String((index % 13) + 1).padStart(2, "0")}T12:00:00Z`,
  ]);
  return [
    {
      kind: "mermaid",
      title: "Release path",
      caption: "A themed dependency flow",
      payload: { source: MERMAID_SOURCE },
    },
    {
      kind: "svg",
      title: "Sanitized badge",
      caption: "SVG fixture",
      payload: {
        source: [
          '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 360 120">',
          '<rect width="360" height="120" rx="8" fill="#e8e5df"/>',
          '<text x="105" y="68" font-size="24" fill="#20241f">Inspector SVG</text>',
          "</svg>",
        ].join(""),
      },
    },
    {
      kind: "image",
      title: "Pixel fixture",
      caption: "Stored bytes served by the backend",
      payload: { data_base64: PNG_BASE64, mime: "image/png" },
    },
    {
      kind: "table",
      title: "Build inventory",
      caption: "Sortable export fixture",
      payload: {
        columns: [
          { key: "id", label: "ID", type: "number" },
          { key: "name", label: "Name", type: "string" },
          { key: "created", label: "Created", type: "date" },
        ],
        rows,
      },
    },
    {
      kind: "plot",
      title: "Weekly throughput",
      caption: "Vega-Lite fixture",
      payload: {
        spec_vega_lite: {
          $schema: "https://vega.github.io/schema/vega-lite/v5.json",
          width: "container",
          height: 220,
          data: {
            values: [
              { week: "W1", value: 4 },
              { week: "W2", value: 7 },
              { week: "W3", value: 5 },
              { week: "W4", value: 11 },
            ],
          },
          mark: { type: "bar" },
          encoding: {
            x: { field: "week", type: "ordinal", title: null },
            y: { field: "value", type: "quantitative", title: "Artifacts" },
          },
        },
      },
    },
    {
      kind: "code",
      title: "Parser change",
      caption: "Plain code fixture",
      payload: {
        language: "typescript",
        filename: "src/artifact.ts",
        source: "export const limit = 20;\nexport const enabled = true;\n",
      },
    },
    {
      kind: "image",
      title: "JPEG fixture",
      caption: "Copy-image conversion source",
      payload: { data_base64: browserImages.jpeg, mime: "image/jpeg" },
    },
    {
      kind: "image",
      title: "WebP fixture",
      caption: "Copy-image conversion source",
      payload: { data_base64: browserImages.webp, mime: "image/webp" },
    },
  ];
}

function invokeFixtureWorker(fixtures, inputs) {
  const requests = [
    {
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "wiki-195-fixture", version: "1" } },
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
    env: {
      ...process.env,
      WIKI_AGENT_RUNTIME_DIR: fixtures.runtimeDir,
      WIKI_RUN_ID: RUN_ID,
    },
    input: requests.map((request) => JSON.stringify(request)).join("\n") + "\n",
    encoding: "utf8",
  });
  if (result.status !== 0) {
    throw new Error(`artifact fixture worker failed: ${result.stderr || result.stdout}`);
  }
  const responses = result.stdout.trim().split("\n").map((line) => JSON.parse(line));
  return responses.slice(1).map((response) => {
    if (response.result?.isError) throw new Error(response.result.content?.[0]?.text || "artifact rejected");
    const sentinel = response.result.content[0].text;
    const event = JSON.parse(sentinel.slice("<<wiki-artifact:v1>>".length, -"<<end>>".length));
    return { artifactId: event.id, sentinel };
  });
}

async function writeTranscript(fixtures, inputs, results) {
  const rows = [{ type: "mode", mode: "normal", sessionId: "wiki-195-inspector" }];
  inputs.forEach((input, index) => {
    const id = `toolu_artifact_${index + 1}`;
    rows.push({
      type: "assistant",
      timestamp: `2026-07-30T14:00:${String(index * 2).padStart(2, "0")}Z`,
      message: {
        role: "assistant",
        content: [{ type: "tool_use", id, name: "mcp__wiki-artifacts__render_artifact", input }],
      },
    });
    rows.push({
      type: "user",
      timestamp: `2026-07-30T14:00:${String(index * 2 + 1).padStart(2, "0")}Z`,
      message: {
        role: "user",
        content: [{ type: "tool_result", tool_use_id: id, content: results[index].sentinel }],
      },
    });
  });
  const target = path.join(fixtures.root, "wiki-195-inspector.jsonl");
  await fs.writeFile(target, rows.map((row) => JSON.stringify(row)).join("\n") + "\n");
  return target;
}

async function writeRegistry(fixtures, transcript) {
  const registry = {
    _orchestrators: {
      [TICKET]: {
        window: "@9999",
        spawned_at: "2026-07-30T14:00:00Z",
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

async function expectInspectorTitle(page, title) {
  const heading = page.locator(".artifact-inspector .artifact-inspector-title");
  await heading.waitFor({ state: "visible" });
  const text = await heading.innerText();
  if (text !== title) throw new Error(`Inspector title mismatch: got "${text}", expected "${title}"`);
}

async function expectClosed(page) {
  if ((await page.locator(".artifact-inspector").count()) !== 0) {
    throw new Error("Inspector still open");
  }
}

async function main() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
    acceptDownloads: true,
    permissions: ["clipboard-read", "clipboard-write"],
  });
  const page = await context.newPage();
  page.on("pageerror", (error) => logStep(`browser page error: ${error.message}`));

  logStep("generating jpeg/webp fixture bytes in the browser");
  const browserImages = await generateBrowserImageFixtures(page);

  logStep("creating isolated fixtures for eight artifacts across six kinds");
  const fixtures = makeFixtureRoot("wiki-195-inspector-");
  const inputs = artifactInputs(browserImages);
  const results = invokeFixtureWorker(fixtures, inputs);
  const transcript = await writeTranscript(fixtures, inputs, results);
  await writeRegistry(fixtures, transcript);
  writeQueue(fixtures.queuePath, TICKET, []);

  // The backend scrubs image bytes out of the event payload; serve every
  // stored artifact from the archived-run path like wiki-85 does.
  const runArtifacts = path.join(fixtures.runtimeDir, "runs", RUN_ID, "artifacts");
  const archiveArtifacts = path.join(fixtures.root, "archive", TICKET, "20260730-140000", "artifacts");
  await fs.mkdir(archiveArtifacts, { recursive: true });
  for (const file of await fs.readdir(runArtifacts)) {
    await fs.copyFile(path.join(runArtifacts, file), path.join(archiveArtifacts, file));
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
    await page.locator('[data-artifact-kind="mermaid"] .artifact-mermaid svg').waitFor({ state: "visible" });
    await page.locator('[data-artifact-kind="code"] .shiki').waitFor({ state: "visible" });

    logStep("cmd+enter with no focused/hovered artifact opens the most recent artifact");
    await page.locator(".session-scroll").click({ position: { x: 8, y: 8 } });
    await page.keyboard.press("ControlOrMeta+Enter");
    await expectInspectorTitle(page, "WebP fixture");
    await page.locator(".artifact-inspector img").first().waitFor({ state: "visible" });

    logStep("escape closes the inspector");
    await page.keyboard.press("Escape");
    await expectClosed(page);

    logStep("cmd+enter opens the hovered artifact");
    await page.locator('[data-artifact-kind="mermaid"]').hover();
    await page.keyboard.press("ControlOrMeta+Enter");
    await expectInspectorTitle(page, "Release path");
    await page.locator(".artifact-inspector .artifact-mermaid svg").waitFor({ state: "visible" });
    // Let the 160ms enter fade finish so screenshots capture the settled sheet.
    await page.waitForTimeout(300);
    await page.screenshot({ path: SCREENSHOTS.mermaid });

    logStep("arrow keys walk sibling artifacts in stream order with wrap-around");
    const arrowExpectations = [
      ["Sanitized badge", ".artifact-inspector .artifact-svg svg", SCREENSHOTS.svg],
      ["Pixel fixture", ".artifact-inspector img", SCREENSHOTS.image],
      ["Build inventory", ".artifact-inspector table", SCREENSHOTS.table],
      ["Weekly throughput", ".artifact-inspector .artifact-plot svg", SCREENSHOTS.plot],
      ["Parser change", ".artifact-inspector .artifact-code-detail", SCREENSHOTS.code],
      ["JPEG fixture", ".artifact-inspector img", null],
      ["WebP fixture", ".artifact-inspector img", null],
      ["Release path", ".artifact-inspector .artifact-mermaid svg", null],
    ];
    for (const [title, selector, screenshot] of arrowExpectations) {
      await page.keyboard.press("ArrowRight");
      await expectInspectorTitle(page, title);
      await page.locator(selector).first().waitFor({ state: "visible" });
      if (screenshot) await page.screenshot({ path: screenshot });
    }
    await page.keyboard.press("ArrowLeft");
    await expectInspectorTitle(page, "WebP fixture");

    logStep("copy source copies the raw payload");
    await page.keyboard.press("ArrowRight");
    await expectInspectorTitle(page, "Release path");
    await page.locator(".artifact-inspector").getByTitle("Copy raw payload").click();
    const copied = await page.evaluate(() => navigator.clipboard.readText());
    if (copied !== MERMAID_SOURCE) throw new Error(`Copy source mismatch: ${copied}`);

    logStep("copy image appears only on image artifacts");
    if ((await page.locator(".artifact-inspector").getByTitle("Copy image").count()) !== 0) {
      throw new Error("Copy image offered for a non-image artifact");
    }
    await page.keyboard.press("ArrowRight");
    await page.keyboard.press("ArrowRight");
    await expectInspectorTitle(page, "Pixel fixture");
    await page.locator(".artifact-inspector").getByTitle("Copy image").waitFor({ state: "visible" });

    logStep("copy image puts image/png bytes on the clipboard for a file-backed PNG");
    await copyImageAndAssertPng(page, "PNG artifact");

    logStep("copy image converts JPEG and WebP artifacts to image/png");
    for (const title of ["Build inventory", "Weekly throughput", "Parser change", "JPEG fixture"]) {
      await page.keyboard.press("ArrowRight");
      await expectInspectorTitle(page, title);
    }
    await copyImageAndAssertPng(page, "JPEG artifact");
    await page.keyboard.press("ArrowRight");
    await expectInspectorTitle(page, "WebP fixture");
    await copyImageAndAssertPng(page, "WebP artifact");
    for (const title of ["JPEG fixture", "Parser change", "Weekly throughput", "Build inventory", "Pixel fixture"]) {
      await page.keyboard.press("ArrowLeft");
      await expectInspectorTitle(page, title);
    }

    logStep("download works from the inspector chrome");
    const downloadPromise = page.waitForEvent("download");
    await page.locator(".artifact-inspector").getByTitle("Download").click();
    const download = await downloadPromise;
    if (!download.suggestedFilename().endsWith(".png")) {
      throw new Error(`Unexpected download name: ${download.suggestedFilename()}`);
    }

    logStep("tab focus stays trapped inside the dialog");
    for (let i = 0; i < 12; i += 1) {
      await page.keyboard.press("Tab");
      const inside = await page.evaluate(() =>
        Boolean(document.querySelector(".artifact-inspector")?.contains(document.activeElement)),
      );
      if (!inside) throw new Error("Tab focus escaped the inspector");
    }

    logStep("close button dismisses and focus returns to the page");
    await page.locator(".artifact-inspector").getByTitle("Close (Esc)").click();
    await expectClosed(page);

    logStep("block fullscreen button opens that artifact");
    await page.locator('[data-artifact-kind="table"]').getByTitle("Fullscreen (⌘↩)").click();
    await expectInspectorTitle(page, "Build inventory");
    await page.locator(".artifact-inspector table").waitFor({ state: "visible" });
    await page.keyboard.press("Escape");
    await expectClosed(page);

    logStep("image expand click routes to the inspector, not the legacy lightbox");
    await page.locator('[data-artifact-kind="image"] .artifact-image-expand').first().click();
    await expectInspectorTitle(page, "Pixel fixture");
    if ((await page.locator(".artifact-lightbox").count()) !== 0) {
      throw new Error("Legacy lightbox opened for an artifact image");
    }
    await page.keyboard.press("Escape");
    await expectClosed(page);

    logStep(`isolated fixture root: ${fixtures.root}`);
  } finally {
    await context.close();
    await browser.close();
    await backend.stop();
  }

  logStep("inspector verification complete");
}

await main();
