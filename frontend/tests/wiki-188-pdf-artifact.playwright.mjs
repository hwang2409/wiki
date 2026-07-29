import { spawnSync } from "node:child_process";
import fs from "node:fs/promises";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

import {
  makeFixtureRoot,
  startBackend,
  writeQueue,
} from "../scripts/wiki32-harness.mjs";
import { buildFixturePdf } from "./fixtures/build-pdf-fixture.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
function resolvePython() {
  const candidates = [
    process.env.WIKI_PYTHON,
    path.join(ROOT, ".venv", "bin", "python"),
    path.resolve(ROOT, "..", "..", "..", ".venv", "bin", "python"),
  ].filter(Boolean);
  const found = candidates.find((candidate) => existsSync(candidate));
  if (!found) throw new Error(`No Python runtime found: ${candidates.join(", ")}`);
  return found;
}
const PYTHON = resolvePython();
const RUN_ID = "00000000-0000-4000-8000-000000000188";
const TICKET = "WIKI-188";
const COMPACT_SCREENSHOT = "/tmp/wiki-188-pdf-compact.png";
const DETAIL_SCREENSHOT = "/tmp/wiki-188-pdf-detail.png";

const PAGE_TEXTS = [
  "Wiki silky pdf first page fixture",
  "Second page: page navigation target",
  "Third page contains a unique marker: terrarium",
  "Fourth summary page for zoom check",
];
const UNIQUE_WORD = "terrarium";

function logStep(message) {
  console.error(`[wiki-188-pdf-playwright] ${message}`);
}

function invokeFixtureWorker(fixtures, pdfBytes) {
  const requests = [
    {
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: {
        protocolVersion: "2025-06-18",
        capabilities: {},
        clientInfo: { name: "wiki-188-fixture", version: "1" },
      },
    },
    {
      jsonrpc: "2.0",
      id: 10,
      method: "tools/call",
      params: {
        name: "render_artifact",
        arguments: {
          kind: "pdf",
          title: "Silky pdf fixture",
          caption: "Multi-page pdf artifact for WIKI-188",
          payload: { data_base64: pdfBytes.toString("base64") },
        },
      },
    },
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
  const response = responses[1];
  if (response.result?.isError) throw new Error(response.result.content?.[0]?.text || "pdf artifact rejected");
  const sentinel = response.result.content[0].text;
  const event = JSON.parse(sentinel.slice("<<wiki-artifact:v1>>".length, -"<<end>>".length));
  return { artifactId: event.id, sentinel };
}

async function writeTranscript(fixtures, sentinel) {
  const rows = [
    { type: "mode", mode: "normal", sessionId: "wiki-188-pdf" },
    {
      type: "assistant",
      timestamp: "2026-07-29T14:00:00Z",
      message: {
        role: "assistant",
        content: [
          {
            type: "tool_use",
            id: "toolu_pdf_artifact",
            name: "mcp__wiki-artifacts__render_artifact",
            input: {
              kind: "pdf",
              title: "Silky pdf fixture",
              caption: "Multi-page pdf artifact for WIKI-188",
              payload: {
                data_base64: "<omitted-from-transcript>",
              },
            },
          },
        ],
      },
    },
    {
      type: "user",
      timestamp: "2026-07-29T14:00:01Z",
      message: {
        role: "user",
        content: [{ type: "tool_result", tool_use_id: "toolu_pdf_artifact", content: sentinel }],
      },
    },
  ];
  const target = path.join(fixtures.root, "wiki-188-pdf.jsonl");
  await fs.writeFile(target, rows.map((row) => JSON.stringify(row)).join("\n") + "\n");
  return target;
}

async function writeRegistry(fixtures, transcript) {
  await fs.writeFile(
    fixtures.registryPath,
    JSON.stringify(
      {
        _orchestrators: {
          [TICKET]: {
            window: "@9999",
            spawned_at: "2026-07-29T14:00:00Z",
            kind: "cc",
            transcript,
          },
        },
      },
      null,
      2,
    ),
  );
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

async function main() {
  logStep("building pdf fixture bytes");
  const pdfBytes = buildFixturePdf(PAGE_TEXTS);
  logStep("creating isolated backend fixtures");
  const fixtures = makeFixtureRoot("wiki-188-pdf-");
  const { artifactId, sentinel } = invokeFixtureWorker(fixtures, pdfBytes);
  const transcript = await writeTranscript(fixtures, sentinel);
  await writeRegistry(fixtures, transcript);
  writeQueue(fixtures.queuePath, TICKET, []);

  const livePdf = path.join(
    fixtures.runtimeDir,
    "runs",
    RUN_ID,
    "artifacts",
    `${artifactId}.pdf`,
  );
  const archiveArtifacts = path.join(
    fixtures.root,
    "archive",
    TICKET,
    "20260729-140000",
    "artifacts",
  );
  await fs.mkdir(archiveArtifacts, { recursive: true });
  await fs.copyFile(livePdf, path.join(archiveArtifacts, `${artifactId}.pdf`));

  logStep("starting the isolated worktree backend");
  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  page.on("pageerror", (error) => logStep(`browser page error: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error") logStep(`browser console: ${message.text()}`);
  });
  page.on("response", (response) => {
    if (response.status() >= 400) logStep(`browser response ${response.status()}: ${response.url()}`);
  });

  try {
    await page.addInitScript(({ layout }) => {
      localStorage.setItem("wiki-window-layout-v2", JSON.stringify(layout));
      localStorage.setItem("wiki-sidebar-visible", "false");
      localStorage.setItem("wiki-theme", "mono-light");
    }, { layout: sessionLayout() });

    await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
    await page.locator(".session-scroll").waitFor({ state: "visible" });

    const openStart = Date.now();
    const block = page.locator('[data-artifact-kind="pdf"]');
    await block.waitFor({ state: "visible" });

    const compactCanvas = block.locator(".artifact-pdf-compact-canvas");
    await compactCanvas.waitFor({ state: "visible" });
    await page.waitForFunction(() => {
      const canvas = document.querySelector('[data-artifact-kind="pdf"] .artifact-pdf-compact-canvas');
      return canvas instanceof HTMLCanvasElement && canvas.width > 0 && canvas.height > 0;
    });
    const compactOpenMs = Date.now() - openStart;
    logStep(`compact preview rendered in ${compactOpenMs}ms`);
    if (compactOpenMs > 4000) throw new Error(`compact preview took ${compactOpenMs}ms — over 4s budget`);

    const compactMeta = await block.locator(".artifact-pdf-compact-meta").innerText();
    if (!compactMeta.includes(`page 1 of ${PAGE_TEXTS.length}`)) {
      throw new Error(`compact meta unexpected: ${JSON.stringify(compactMeta)}`);
    }
    await block.screenshot({ path: COMPACT_SCREENSHOT });

    logStep("opening detail panel via Show all");
    await block.getByRole("button", { name: "Show all" }).click();
    await block.getByRole("button", { name: /Open in panel/ }).click();
    const panel = page.locator(".artifact-pdf-detail");
    await panel.waitFor({ state: "visible" });
    await panel.locator(".artifact-pdf-page-canvas").waitFor({ state: "visible" });
    await page.waitForFunction(() => {
      const canvas = document.querySelector('.artifact-pdf-page-canvas');
      return canvas instanceof HTMLCanvasElement && canvas.width > 0;
    });

    const initialIndicator = await panel.locator(".artifact-pdf-page-indicator").innerText();
    if (!initialIndicator.includes(`page 1 of ${PAGE_TEXTS.length}`)) {
      throw new Error(`initial page indicator wrong: ${initialIndicator}`);
    }

    logStep("verifying text layer contains selectable text");
    await page.waitForFunction(
      (needle) => document.querySelector(".artifact-pdf-page-textlayer")?.textContent?.includes(needle) ?? false,
      "silky",
    );

    logStep("advancing to next page and checking indicator");
    await panel.locator("[data-pdf-next]").click();
    await page.waitForFunction(() => {
      const label = document.querySelector(".artifact-pdf-page-indicator");
      return label?.textContent?.includes("page 2 of");
    });

    logStep("using cmd+f + find field to locate unique marker");
    await panel.getByRole("button", { name: /find/i }).click();
    const findInput = panel.locator('input[aria-label="Find in PDF"]');
    await findInput.fill(UNIQUE_WORD);
    await findInput.press("Enter");
    await page.waitForFunction(() => {
      const label = document.querySelector(".artifact-pdf-page-indicator");
      return label?.textContent?.includes("page 3 of");
    });
    const matchCount = await panel.locator(".artifact-pdf-find span.tabular-nums").innerText();
    if (!/^1\/1$/.test(matchCount.trim())) {
      throw new Error(`unique match count unexpected: ${JSON.stringify(matchCount)}`);
    }

    logStep("zooming in via toolbar and verifying zoom label change");
    const zoomBefore = await panel.locator(".artifact-pdf-zoom-value").innerText();
    await panel.locator("[data-pdf-zoom-in]").click();
    await page.waitForFunction((previous) => {
      const label = document.querySelector(".artifact-pdf-zoom-value")?.textContent ?? "";
      return label && label !== previous;
    }, zoomBefore);

    await panel.screenshot({ path: DETAIL_SCREENSHOT });

    const summary = {
      root: fixtures.root,
      artifact_id: artifactId,
      pdf_bytes: pdfBytes.length,
      compact_screenshot: COMPACT_SCREENSHOT,
      detail_screenshot: DETAIL_SCREENSHOT,
      compact_open_ms: compactOpenMs,
    };
    await fs.writeFile("/tmp/wiki-188-fixtures.json", JSON.stringify(summary, null, 2));
    logStep(`fixture summary at /tmp/wiki-188-fixtures.json`);
  } finally {
    await context.close();
    await browser.close();
    await backend.stop();
  }

  logStep("pdf artifact verification complete");
}

await main();
