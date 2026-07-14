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
const RUN_ID = "00000000-0000-4000-8000-000000000085";
const TICKET = "WIKI-85";
const SCREENSHOTS = Object.fromEntries(
  ["mermaid", "svg", "image", "table", "plot", "code"].map((kind) => [
    kind,
    `/tmp/wiki-85-${kind}.png`,
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
  const bars = [42, 74, 55, 92];
  for (let y = 0; y < height; y += 1) {
    const row = y * (1 + width * 3);
    for (let x = 0; x < width; x += 1) {
      const bar = Math.floor((x - 28) / 82);
      const inBar = bar >= 0 && bar < bars.length && (x - 28) % 82 < 54 && y >= height - bars[bar] - 12;
      const offset = row + 1 + x * 3;
      scanlines[offset] = inBar ? 79 : 232;
      scanlines[offset + 1] = inBar ? 103 : 229;
      scanlines[offset + 2] = inBar ? 86 : 223;
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
  console.error(`[wiki-85-playwright] ${message}`);
}

function artifactInputs() {
  const rows = Array.from({ length: 30 }, (_, index) => [
    index + 1,
    `row-${String(index + 1).padStart(2, "0")}`,
    `2026-07-${String((index % 13) + 1).padStart(2, "0")}T12:00:00Z`,
  ]);
  return [
    {
      kind: "mermaid",
      title: "Release path",
      caption: "A themed dependency flow",
      payload: { source: "flowchart LR\n  Plan --> Build\n  Build --> Ship" },
    },
    {
      kind: "svg",
      title: "Sanitized badge",
      caption: "Untrusted SVG fixture",
      payload: {
        source: [
          '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 360 120">',
          '<rect width="360" height="120" rx="8" fill="#e8e5df"/>',
          '<circle cx="58" cy="60" r="30" fill="#4f6756"/>',
          '<text x="105" y="68" font-size="24" fill="#20241f">Sanitized SVG</text>',
          '<script>alert("wiki-85-xss")</script>',
          '<a href="javascript:alert(\'wiki-85-link\')"><text x="10" y="110">unsafe</text></a>',
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
      caption: "Virtualized, sortable export fixture",
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
      caption: "Vega-Lite with Wiki theme colors",
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
          mark: { type: "bar", cornerRadiusTopLeft: 2, cornerRadiusTopRight: 2 },
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
      caption: "Unified diff rendered through Shiki",
      payload: {
        language: "typescript",
        filename: "src/artifact.ts",
        diff_from: "export const limit = 10;\n",
        source: "export const limit = 20;\nexport const enabled = true;\n",
      },
    },
  ];
}

function invokeFixtureWorker(fixtures, inputs) {
  const requests = [
    {
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "wiki-85-fixture", version: "1" } },
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
    return {
      artifactId: event.id,
      sentinel,
    };
  });
}

async function writeTranscript(fixtures, inputs, results) {
  const rows = [{ type: "mode", mode: "normal", sessionId: "wiki-85-artifacts" }];
  inputs.forEach((input, index) => {
    const id = `toolu_artifact_${index + 1}`;
    rows.push({
      type: "assistant",
      timestamp: `2026-07-13T14:00:${String(index * 2).padStart(2, "0")}Z`,
      message: {
        role: "assistant",
        content: [{ type: "tool_use", id, name: "mcp__wiki-artifacts__render_artifact", input }],
      },
    });
    rows.push({
      type: "user",
      timestamp: `2026-07-13T14:00:${String(index * 2 + 1).padStart(2, "0")}Z`,
      message: {
        role: "user",
        content: [{ type: "tool_result", tool_use_id: id, content: results[index].sentinel }],
      },
    });
  });
  const target = path.join(fixtures.root, "wiki-85-artifacts.jsonl");
  await fs.writeFile(target, rows.map((row) => JSON.stringify(row)).join("\n") + "\n");
  return target;
}

async function writeRegistry(fixtures, transcript, includeCurrent) {
  const registry = {
    _orchestrators: {
      [TICKET]: {
        window: "@9999",
        spawned_at: "2026-07-13T14:00:00Z",
        kind: "cc",
        transcript,
      },
    },
  };
  if (includeCurrent) {
    registry[TICKET] = {
      current: {
        run_id: RUN_ID,
        window: "@9999",
        kind: "cc",
        spawned_at: "2026-07-13T14:00:00Z",
      },
      history: [],
    };
  }
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

async function clipboardText(page) {
  return page.evaluate(() => navigator.clipboard.readText());
}

async function clickDownload(page, block, expectedSuffix) {
  const downloadPromise = page.waitForEvent("download");
  await block.getByRole("button", { name: "Download" }).click();
  const download = await downloadPromise;
  if (!download.suggestedFilename().endsWith(expectedSuffix)) {
    throw new Error(`Expected download suffix ${expectedSuffix}, got ${download.suggestedFilename()}`);
  }
  const failure = await download.failure();
  if (failure) throw new Error(`Download failed: ${failure}`);
}

async function inspectAndClose(block, title) {
  await block.getByRole("button", { name: "Inspect" }).click();
  const inspector = block.locator("xpath=..").getByRole("complementary", { name: "Artifact inspector" });
  await inspector.waitFor({ state: "visible" });
  const raw = await inspector.locator("pre").innerText();
  if (!raw.includes(title) || !raw.includes('"kind": "artifact"')) {
    throw new Error(`Inspector omitted normalized payload for ${title}`);
  }
  await inspector.getByRole("button", { name: "Close artifact inspector" }).click();
}

async function main() {
  logStep("creating isolated MCP, transcript, registry, runtime, and archive fixtures");
  const fixtures = makeFixtureRoot("wiki-85-artifacts-");
  const inputs = artifactInputs();
  const results = invokeFixtureWorker(fixtures, inputs);
  const transcript = await writeTranscript(fixtures, inputs, results);
  await writeRegistry(fixtures, transcript, false);
  writeQueue(fixtures.queuePath, TICKET, []);

  const imageResult = results[inputs.findIndex((input) => input.kind === "image")];
  const liveImage = path.join(
    fixtures.runtimeDir,
    "runs",
    RUN_ID,
    "artifacts",
    `${imageResult.artifactId}.png`
  );
  const archiveArtifacts = path.join(fixtures.root, "archive", TICKET, "20260713-140000", "artifacts");
  const archivedImage = path.join(archiveArtifacts, `${imageResult.artifactId}.png`);
  await fs.mkdir(archiveArtifacts, { recursive: true });
  await fs.copyFile(liveImage, archivedImage);

  logStep("starting the isolated worktree backend");
  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
    acceptDownloads: true,
    permissions: ["clipboard-read", "clipboard-write"],
  });
  const page = await context.newPage();
  let dialogs = 0;
  page.on("console", (message) => {
    if (message.type() === "error") {
      logStep(`browser console: ${message.text()} ${JSON.stringify(message.location())}`);
    }
  });
  page.on("pageerror", (error) => logStep(`browser page error: ${error.message}`));
  page.on("response", (response) => {
    if (response.status() >= 400) logStep(`browser response ${response.status()}: ${response.url()}`);
  });
  page.on("dialog", async (dialog) => {
    dialogs += 1;
    await dialog.dismiss();
  });

  try {
    await page.addInitScript(({ layout }) => {
      localStorage.setItem("wiki-window-layout-v2", JSON.stringify(layout));
      localStorage.setItem("wiki-sidebar-visible", "false");
      localStorage.setItem("wiki-theme", "mono-light");
    }, { layout: sessionLayout() });
    await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
    await page.locator(".session-scroll").waitFor({ state: "visible" });
    await page.locator('[data-artifact-kind="mermaid"] .artifact-mermaid svg').waitFor({ state: "visible" });
    await page.locator('[data-artifact-kind="plot"] .artifact-plot svg').waitFor({ state: "visible" });
    await page.locator('[data-artifact-kind="code"] .shiki').waitFor({ state: "visible" });
    await page.waitForFunction(() => {
      const image = document.querySelector('[data-artifact-kind="image"] img');
      return image instanceof HTMLImageElement && image.complete && image.naturalWidth > 0;
    });

    const blocks = Object.fromEntries(
      inputs.map(({ kind }) => [kind, page.locator(`[data-artifact-kind="${kind}"]`)])
    );
    for (const { kind } of inputs) {
      if ((await blocks[kind].count()) !== 1) throw new Error(`Expected one ${kind} artifact block`);
    }

    logStep("verifying SVG sanitization before any interaction");
    if ((await blocks.svg.locator("script").count()) !== 0) throw new Error("SVG script survived sanitization");
    if ((await blocks.svg.locator('[href^="javascript:"]').count()) !== 0) {
      throw new Error("SVG javascript link survived sanitization");
    }
    if (dialogs !== 0) throw new Error(`SVG XSS opened ${dialogs} dialogs`);

    const expectedCopies = {
      mermaid: inputs[0].payload.source,
      svg: inputs[1].payload.source,
      image: PNG_BASE64,
      plot: JSON.stringify(inputs[4].payload.spec_vega_lite, null, 2),
      code: inputs[5].payload.source,
    };
    for (const kind of ["mermaid", "svg", "image", "plot", "code"]) {
      await blocks[kind].getByRole("button", { name: "Copy" }).click();
      await blocks[kind].getByRole("button", { name: "Copied" }).waitFor({ state: "visible" });
      if ((await clipboardText(page)) !== expectedCopies[kind]) {
        throw new Error(`Clipboard mismatch for ${kind}`);
      }
    }
    await blocks.table.getByRole("button", { name: /^Copy/ }).click();
    await blocks.table.getByRole("menuitem", { name: "Copy as TSV" }).click();
    const tableCopy = await clipboardText(page);
    if (!tableCopy.startsWith("ID\tName\tCreated\n1\trow-01")) throw new Error("Table TSV copy mismatch");

    logStep("verifying sort, inspect, download, and required screenshots for every kind");
    await blocks.table.getByRole("button", { name: "Sort by ID" }).click();
    await blocks.table.getByRole("button", { name: "Sort by ID" }).click();
    const firstTableValue = await blocks.table.locator("tbody tr:not(.artifact-table-spacer) td").first().innerText();
    if (firstTableValue !== "30") throw new Error(`Descending table sort failed: ${firstTableValue}`);

    const suffixes = { mermaid: ".mmd", svg: ".svg", image: ".png", table: ".csv", plot: ".json", code: ".ts" };
    for (const input of inputs) {
      const block = blocks[input.kind];
      await inspectAndClose(block, input.title);
      await clickDownload(page, block, suffixes[input.kind]);
      await block.scrollIntoViewIfNeeded();
      await block.screenshot({ path: SCREENSHOTS[input.kind] });
    }

    logStep("verifying live runtime resolution and archived fallback on the isolated backend");
    const heldArchive = `${archivedImage}.hold`;
    await fs.rename(archivedImage, heldArchive);
    await writeRegistry(fixtures, transcript, true);
    const liveResponse = await page.request.get(
      `${backend.baseUrl}/api/agents/${TICKET}/artifact/${imageResult.artifactId}?live=1`
    );
    await writeRegistry(fixtures, transcript, false);
    await fs.rename(heldArchive, archivedImage);
    if (!liveResponse.ok() || liveResponse.headers()["content-type"] !== "image/png") {
      throw new Error(`Live image resolution failed: ${liveResponse.status()}`);
    }
    if (!Buffer.from(await liveResponse.body()).equals(Buffer.from(PNG_BASE64, "base64"))) {
      throw new Error("Live image bytes changed");
    }
    await fs.unlink(liveImage);
    const archivedResponse = await page.request.get(
      `${backend.baseUrl}/api/agents/${TICKET}/artifact/${imageResult.artifactId}?archived=1`
    );
    if (!archivedResponse.ok() || archivedResponse.headers()["content-type"] !== "image/png") {
      throw new Error(`Archived image fallback failed: ${archivedResponse.status()}`);
    }
    if (!Buffer.from(await archivedResponse.body()).equals(Buffer.from(PNG_BASE64, "base64"))) {
      throw new Error("Archived image bytes changed");
    }
    if (dialogs !== 0) throw new Error(`XSS regression opened ${dialogs} dialogs`);

    const fixtureSummary = {
      root: fixtures.root,
      registry: fixtures.registryPath,
      transcript,
      queue: fixtures.queuePath,
      runtime: fixtures.runtimeDir,
      live_image_removed_after_verification: liveImage,
      archive: path.join(fixtures.root, "archive"),
      archived_image: archivedImage,
      status: fixtures.statusDir,
      codex_sessions: fixtures.sessionsDir,
      tmp: path.join(fixtures.root, "tmp"),
      vault: path.join(fixtures.root, "vault"),
      claude_projects: path.join(fixtures.root, "claude-projects"),
      account_home: path.join(fixtures.root, "account-home"),
      supervisor_socket: fixtures.supervisorSocketPath,
      backend: { url: backend.baseUrl, port: backend.port, pid: backend.process.pid },
      screenshots: SCREENSHOTS,
    };
    await fs.writeFile("/tmp/wiki-85-fixtures.json", JSON.stringify(fixtureSummary, null, 2));
    logStep(`isolated fixture root: ${fixtures.root}`);
  } finally {
    await context.close();
    await browser.close();
    await backend.stop();
  }

  logStep("artifact verification complete");
}

await main();
