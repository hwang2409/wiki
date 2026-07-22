import { spawnSync } from "node:child_process";
import fs from "node:fs/promises";
import { mkdirSync, rmSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

import { makeFixtureRoot, startBackend } from "../scripts/wiki32-harness.mjs";

import { existsSync } from "node:fs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const PYTHON = [
  process.env.WIKI_PYTHON,
  path.join(ROOT, ".venv", "bin", "python"),
  path.resolve(ROOT, "..", "..", "..", ".venv", "bin", "python"),
].filter(Boolean).find((candidate) => existsSync(candidate));
if (!PYTHON) {
  throw new Error("No python interpreter found for wiki-143-diff test");
}
const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || "/tmp/wiki-143-diff-evidence";
mkdirSync(OUT_DIR, { recursive: true });

const TICKET = "WIKI-143";
const RUN_ID = "00000000-0000-4000-8000-000000000143";

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

const DIFF_SOURCE = [
  "diff --git a/frontend/src/example.tsx b/frontend/src/example.tsx",
  "index 111111..222222 100644",
  "--- a/frontend/src/example.tsx",
  "+++ b/frontend/src/example.tsx",
  "@@ -1,4 +1,5 @@",
  " import { StatusBadge } from './status-badge';",
  "-export const width = 720;",
  "+export const width = 760;",
  "+export const readable = true;",
  " export const enabled = true;",
].join("\n");

function buildLargeDiff(lineTarget) {
  const header = [
    "diff --git a/frontend/src/large.tsx b/frontend/src/large.tsx",
    "index aaaaaa..bbbbbb 100644",
    "--- a/frontend/src/large.tsx",
    "+++ b/frontend/src/large.tsx",
    `@@ -1,${lineTarget} +1,${lineTarget} @@`,
  ];
  const body = [];
  for (let index = 0; index < lineTarget; index += 1) {
    body.push(`-export const line${index} = ${index};`);
    body.push(`+export const line${index} = ${index + 1};`);
  }
  return [...header, ...body].join("\n");
}

const LARGE_DIFF_SOURCE = buildLargeDiff(80);

function invokeFixtureWorker(fixtures, inputs) {
  const requests = [
    {
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: {
        protocolVersion: "2025-06-18",
        capabilities: {},
        clientInfo: { name: "wiki-143-diff-fixture", version: "1" },
      },
    },
    ...inputs.map((input, index) => ({
      jsonrpc: "2.0",
      id: 10 + index,
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
  const rows = [{ type: "mode", mode: "normal", sessionId: "wiki-143-diff" }];
  inputs.forEach((input, index) => {
    const id = `toolu_diff_${index + 1}`;
    rows.push({
      type: "assistant",
      timestamp: `2026-07-22T14:00:${String(index * 2).padStart(2, "0")}Z`,
      message: {
        role: "assistant",
        content: [
          { type: "tool_use", id, name: "mcp__wiki-artifacts__render_artifact", input },
        ],
      },
    });
    rows.push({
      type: "user",
      timestamp: `2026-07-22T14:00:${String(index * 2 + 1).padStart(2, "0")}Z`,
      message: {
        role: "user",
        content: [{ type: "tool_result", tool_use_id: id, content: results[index].sentinel }],
      },
    });
  });
  const target = path.join(fixtures.root, "wiki-143-diff.jsonl");
  await fs.writeFile(target, rows.map((row) => JSON.stringify(row)).join("\n") + "\n");
  return target;
}

async function writeRegistry(fixtures, transcript) {
  const registry = {
    _orchestrators: {
      [TICKET]: {
        window: "@9999",
        spawned_at: "2026-07-22T14:00:00Z",
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

let browser;
let backend;
const fixtures = makeFixtureRoot("wiki-143-diff-");

try {
  const inputs = [
    { kind: "code", title: "Diff sample", payload: { language: "diff", source: DIFF_SOURCE } },
    { kind: "code", title: "Oversized diff", payload: { language: "diff", source: LARGE_DIFF_SOURCE } },
  ];
  const results = invokeFixtureWorker(fixtures, inputs);
  const transcript = await writeTranscript(fixtures, inputs, results);
  await writeRegistry(fixtures, transcript);

  backend = await startBackend(fixtures);
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });

  await page.addInitScript(({ layout }) => {
    localStorage.setItem("wiki-window-layout-v2", JSON.stringify(layout));
    localStorage.setItem("wiki-sidebar-visible", "false");
  }, { layout: sessionLayout() });
  await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
  await page.locator(".session-scroll").waitFor({ state: "visible" });

  const codeBlocks = page.locator('[data-artifact-kind="code"]');
  await codeBlocks.first().waitFor({ state: "visible" });
  const blockCount = await codeBlocks.count();
  assert(blockCount === 2, `expected 2 code artifacts (small + oversized diff), got ${blockCount}`);

  const smallBlock = codeBlocks.nth(0);
  await smallBlock.locator(".wiki-diff").waitFor({ state: "visible" });
  const inserts = await smallBlock.locator(".wiki-diff .diff-code-insert").count();
  const deletes = await smallBlock.locator(".wiki-diff .diff-code-delete").count();
  assert(inserts >= 1, `expected inserts >=1 on small diff, got ${inserts}`);
  assert(deletes >= 1, `expected deletes >=1 on small diff, got ${deletes}`);

  const themedColors = await smallBlock.locator(".wiki-diff").evaluate((node) => {
    const style = getComputedStyle(node);
    return {
      insertVar: style.getPropertyValue("--diff-code-insert-background-color").trim(),
      deleteVar: style.getPropertyValue("--diff-code-delete-background-color").trim(),
      fontFamily: style.fontFamily,
    };
  });
  assert(themedColors.insertVar.length > 0, "wiki-diff missing --diff-code-insert-background-color override");
  assert(themedColors.deleteVar.length > 0, "wiki-diff missing --diff-code-delete-background-color override");
  assert(
    /mono|Consolas|JetBrains|SFMono|Menlo/i.test(themedColors.fontFamily),
    `wiki-diff should inherit monospace font, got ${themedColors.fontFamily}`,
  );

  const oversizedBlock = codeBlocks.nth(1);
  await oversizedBlock.waitFor({ state: "visible" });
  const oversizedCompact = await oversizedBlock.getAttribute("data-artifact-compact");
  assert(oversizedCompact === "true", `oversized diff should compact, got data-artifact-compact=${oversizedCompact}`);
  const compactDiff = await oversizedBlock.locator(".artifact-compact-diff .wiki-diff").count();
  const compactCode = await oversizedBlock.locator(".artifact-compact-code").count();
  assert(compactDiff >= 1, `oversized diff should render via compact DiffRenderer, got .artifact-compact-diff count=${compactDiff}`);
  assert(compactCode === 0, `oversized diff must NOT fall through to .artifact-compact-code, got count=${compactCode}`);

  await smallBlock.screenshot({ path: path.join(OUT_DIR, "wiki-143-diff-small.png") });
  await oversizedBlock.screenshot({ path: path.join(OUT_DIR, "wiki-143-diff-oversized.png") });

  await oversizedBlock.getByRole("button", { name: "Open in panel" }).click();
  const panel = page.getByRole("complementary", { name: "Artifact panel" });
  await panel.waitFor({ state: "visible" });
  await panel.locator(".artifact-detail-diff .wiki-diff").waitFor({ state: "visible" });
  const panelInserts = await panel.locator(".artifact-detail-diff .wiki-diff .diff-code-insert").count();
  assert(panelInserts >= 1, `panel diff detail expected inserts >=1, got ${panelInserts}`);
  const panelMissing = await panel.locator(".artifact-panel-missing").count();
  assert(panelMissing === 0, `panel should route diff kind; got .artifact-panel-missing count=${panelMissing}`);

  await panel.screenshot({ path: path.join(OUT_DIR, "wiki-143-diff-panel.png") });

  const fileListResult = await page.evaluate(() => {
    const host = document.createElement("div");
    host.className = "artifact-file-list-host";
    document.body.append(host);
    const list = document.createElement("ul");
    list.className = "artifact-file-list";
    const files = [
      { path: "src/status-badge.tsx", label: "src/status-badge.tsx", status: "added" },
      { path: "src/branch-pill.tsx", label: "src/branch-pill.tsx", status: "added" },
    ];
    for (const entry of files) {
      const item = document.createElement("li");
      item.className = "artifact-file-list-item";
      const btn = document.createElement("button");
      btn.className = "artifact-file-list-button";
      btn.type = "button";
      btn.title = entry.path;
      btn.addEventListener("click", () => {
        document.body.dataset.wiki143Clicked = entry.path;
      });
      const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      icon.setAttribute("class", "artifact-file-list-icon lucide lucide-file");
      icon.setAttribute("width", "12");
      icon.setAttribute("height", "12");
      const label = document.createElement("span");
      label.className = "artifact-file-list-label";
      label.textContent = entry.label;
      const status = document.createElement("span");
      status.className = "artifact-file-list-status";
      status.textContent = entry.status;
      btn.append(icon, label, status);
      item.append(btn);
      list.append(item);
    }
    host.append(list);
    const iconCount = list.querySelectorAll(".artifact-file-list-icon").length;
    return { iconCount, clickable: Boolean(list.querySelector("button")) };
  });
  assert(fileListResult.iconCount === 2, `expected 2 file icons, got ${fileListResult.iconCount}`);
  assert(fileListResult.clickable, "file-list buttons missing");

  await page.locator(".artifact-file-list button").first().click();
  const clickedAfter = await page.evaluate(() => document.body.dataset.wiki143Clicked ?? null);
  assert(clickedAfter === "src/status-badge.tsx", `click handler not fired, dataset=${clickedAfter}`);

  await page.locator(".artifact-file-list").screenshot({ path: path.join(OUT_DIR, "wiki-143-file-list.png") });
} finally {
  if (browser) await browser.close();
  if (backend) await backend.stop();
  rmSync(fixtures.root, { recursive: true, force: true });
}
