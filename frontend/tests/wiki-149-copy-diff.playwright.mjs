import { spawnSync } from "node:child_process";
import fs from "node:fs/promises";
import { existsSync, mkdirSync, rmSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

import { makeFixtureRoot, startBackend } from "../scripts/wiki32-harness.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const PYTHON = [
  process.env.WIKI_PYTHON,
  path.join(ROOT, ".venv", "bin", "python"),
  path.resolve(ROOT, "..", "..", "..", ".venv", "bin", "python"),
].filter(Boolean).find((candidate) => existsSync(candidate));
if (!PYTHON) {
  throw new Error("No python interpreter found for wiki-149 test");
}
const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || "/tmp/wiki-149-evidence";
mkdirSync(OUT_DIR, { recursive: true });

const TICKET = "WIKI-149";
const RUN_ID = "00000000-0000-4000-8000-000000000149";

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

function buildLongDiff() {
  const contextCount = 40;
  const addCount = 40;
  const removeCount = 20;
  const lines = [
    "diff --git a/frontend/src/long.txt b/frontend/src/long.txt",
    "index abcdef1..abcdef2 100644",
    "--- a/frontend/src/long.txt",
    "+++ b/frontend/src/long.txt",
    `@@ -1,${contextCount + removeCount} +1,${contextCount + addCount} @@`,
  ];
  for (let i = 0; i < contextCount; i += 1) lines.push(` context line ${i + 1}`);
  for (let i = 0; i < removeCount; i += 1) lines.push(`-remove line ${i + 1}`);
  for (let i = 0; i < addCount; i += 1) lines.push(`+add line ${i + 1}`);
  return lines.join("\n");
}

const LONG_DIFF_SOURCE = buildLongDiff();

const BINARY_DIFF_SOURCE = [
  "diff --git a/assets/logo.png b/assets/logo.png",
  "index 3333333..4444444 100644",
  "Binary files a/assets/logo.png and b/assets/logo.png differ",
].join("\n");

function invokeFixtureWorker(fixtures, inputs) {
  const requests = [
    {
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: {
        protocolVersion: "2025-06-18",
        capabilities: {},
        clientInfo: { name: "wiki-149-copy-diff-fixture", version: "1" },
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
  const rows = [{ type: "mode", mode: "normal", sessionId: "wiki-149-copy-diff" }];
  inputs.forEach((input, index) => {
    const id = `toolu_wiki149_${index + 1}`;
    rows.push({
      type: "assistant",
      timestamp: `2026-07-22T15:00:${String(index * 2).padStart(2, "0")}Z`,
      message: {
        role: "assistant",
        content: [
          { type: "tool_use", id, name: "mcp__wiki-artifacts__render_artifact", input },
        ],
      },
    });
    rows.push({
      type: "user",
      timestamp: `2026-07-22T15:00:${String(index * 2 + 1).padStart(2, "0")}Z`,
      message: {
        role: "user",
        content: [{ type: "tool_result", tool_use_id: id, content: results[index].sentinel }],
      },
    });
  });
  const target = path.join(fixtures.root, "wiki-149-copy-diff.jsonl");
  await fs.writeFile(target, rows.map((row) => JSON.stringify(row)).join("\n") + "\n");
  return target;
}

async function writeRegistry(fixtures, transcript) {
  const registry = {
    _orchestrators: {
      [TICKET]: {
        window: "@9999",
        spawned_at: "2026-07-22T15:00:00Z",
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

function noteLayout(notePath) {
  return {
    version: 2,
    activeWindowId: "window-0",
    windows: [
      {
        id: "window-0",
        focusedPaneId: "pane-1",
        layout: { kind: "pane", id: "pane-1", path: notePath },
      },
    ],
  };
}

let browser;
let backend;
const fixtures = makeFixtureRoot("wiki-149-copy-diff-");

try {
  const vault = path.join(fixtures.root, "vault");
  await fs.mkdir(vault, { recursive: true });
  const notePath = path.join(vault, "sample.md");
  await fs.writeFile(
    notePath,
    [
      "# Copy demo",
      "",
      "```python",
      "def hello(name):",
      "    return f\"hello {name}\"",
      "```",
    ].join("\n"),
  );

  const inputs = [
    { kind: "diff", title: "WIKI-149 diff", payload: { source: DIFF_SOURCE } },
    { kind: "diff", title: "WIKI-149 long diff", payload: { source: LONG_DIFF_SOURCE } },
    { kind: "diff", title: "WIKI-149 binary diff", payload: { source: BINARY_DIFF_SOURCE } },
  ];
  const results = invokeFixtureWorker(fixtures, inputs);
  const transcript = await writeTranscript(fixtures, inputs, results);
  await writeRegistry(fixtures, transcript);

  backend = await startBackend(fixtures);
  browser = await chromium.launch({ headless: true });

  // --- Copy button on markdown code block (vault note surface) ---
  const notePage = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await notePage.addInitScript(({ layout }) => {
    localStorage.setItem("wiki-window-layout-v2", JSON.stringify(layout));
    localStorage.setItem("wiki-sidebar-visible", "false");
  }, { layout: noteLayout("sample.md") });
  await notePage.addInitScript(() => {
    window.__wiki149Copied = null;
    const original = navigator.clipboard?.writeText?.bind(navigator.clipboard);
    if (navigator.clipboard) {
      navigator.clipboard.writeText = async (text) => {
        window.__wiki149Copied = text;
        if (original) {
          try {
            await original(text);
          } catch {
            /* ignore permission errors under headless */
          }
        }
      };
    }
  });
  await notePage.goto(`${backend.baseUrl}/#/note/sample.md`, { waitUntil: "domcontentloaded" });
  await notePage.waitForSelector(".markdown-preview-view");

  const codeWrap = notePage.locator(".markdown-code-block-wrap").first();
  await codeWrap.waitFor({ state: "visible" });
  const copyPill = codeWrap.locator(".copy-pill");
  await copyPill.waitFor({ state: "attached" });

  const initialOpacity = await copyPill.evaluate((node) => parseFloat(getComputedStyle(node).opacity));
  assert(initialOpacity < 0.5, `copy pill should start hidden, opacity=${initialOpacity}`);

  await codeWrap.hover();
  await notePage.waitForFunction(() => {
    const el = document.querySelector(".markdown-code-block-wrap .copy-pill");
    return el && parseFloat(getComputedStyle(el).opacity) > 0.9;
  }, null, { timeout: 2000 });

  const hoverOpacity = await copyPill.evaluate((node) => parseFloat(getComputedStyle(node).opacity));
  assert(hoverOpacity > 0.9, `copy pill should be visible on hover, opacity=${hoverOpacity}`);

  await copyPill.click();
  await notePage.waitForFunction(() => {
    const el = document.querySelector(".markdown-code-block-wrap .copy-pill");
    return el && el.textContent && el.textContent.trim() === "copied";
  }, null, { timeout: 2000 });
  const copiedText = await copyPill.textContent();
  assert(copiedText?.trim() === "copied", `pill should flip to 'copied', got ${copiedText}`);

  const captured = await notePage.evaluate(() => window.__wiki149Copied);
  assert(
    captured && captured.includes("def hello(name):") && captured.includes('return f"hello {name}"'),
    `clipboard payload should be raw code block contents, got: ${JSON.stringify(captured)}`,
  );

  await notePage.waitForFunction(() => {
    const el = document.querySelector(".markdown-code-block-wrap .copy-pill");
    return el && el.textContent && el.textContent.trim() === "copy";
  }, null, { timeout: 3000 });

  await codeWrap.screenshot({ path: path.join(OUT_DIR, "wiki-149-copy-pill.png") });

  // --- kind: diff artifact renderer ---
  const sessionPage = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  await sessionPage.addInitScript(({ layout }) => {
    localStorage.setItem("wiki-window-layout-v2", JSON.stringify(layout));
    localStorage.setItem("wiki-sidebar-visible", "false");
  }, { layout: sessionLayout() });
  await sessionPage.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
  await sessionPage.locator(".session-scroll").waitFor({ state: "visible" });

  const diffBlock = sessionPage.locator('[data-artifact-kind="diff"]').first();
  await diffBlock.waitFor({ state: "visible" });
  const diffView = diffBlock.locator(".diff-view");
  await diffView.waitFor({ state: "visible" });

  const fileHeaderText = (await diffView.locator(".diff-file-header .diff-file-path").first().textContent())?.trim();
  assert(
    fileHeaderText === "frontend/src/example.tsx",
    `diff file header should show unified path, got ${fileHeaderText}`,
  );

  const hunkHeaders = await diffView.locator(".diff-hunk-header").count();
  assert(hunkHeaders >= 1, `expected at least one hunk header, got ${hunkHeaders}`);

  const addLines = await diffView.locator(".diff-line.is-add").count();
  const removeLines = await diffView.locator(".diff-line.is-remove").count();
  const contextLines = await diffView.locator(".diff-line.is-context").count();
  assert(addLines === 2, `expected 2 add lines, got ${addLines}`);
  assert(removeLines === 1, `expected 1 remove line, got ${removeLines}`);
  assert(contextLines >= 1, `expected context lines, got ${contextLines}`);

  const tones = await diffView.evaluate((root) => {
    const first = (selector) => {
      const el = root.querySelector(selector);
      if (!el) return null;
      const style = getComputedStyle(el);
      return { background: style.backgroundColor, color: style.color };
    };
    return {
      add: first(".diff-line.is-add"),
      remove: first(".diff-line.is-remove"),
      hunk: first(".diff-hunk-header"),
      body: {
        fontFamily: getComputedStyle(root).fontFamily,
        whiteSpace: getComputedStyle(root.querySelector(".diff-line.is-add .diff-code")).whiteSpace,
      },
    };
  });
  assert(tones.add?.background && tones.add.background !== "rgba(0, 0, 0, 0)", `add background should be themed, got ${JSON.stringify(tones.add)}`);
  assert(tones.remove?.background && tones.remove.background !== "rgba(0, 0, 0, 0)", `remove background should be themed, got ${JSON.stringify(tones.remove)}`);
  assert(tones.add.background !== tones.remove.background, `add + remove backgrounds should differ, got ${tones.add.background} vs ${tones.remove.background}`);
  assert(/mono|Consolas|JetBrains|SFMono|Menlo/i.test(tones.body.fontFamily), `diff should be monospace, got ${tones.body.fontFamily}`);
  // WIKI-251: diff bodies soft-wrap so long lines (like the paragraph
   // edit in vault/hot.md) stay readable without a horizontal scrollbar.
  assert(tones.body.whiteSpace === "pre-wrap", `diff body should soft-wrap post-WIKI-251, got ${tones.body.whiteSpace}`);

  const gutterCount = await diffView.locator(".diff-gutter").count();
  assert(gutterCount === 0, `line numbers should be off by default, got ${gutterCount} gutter cells`);

  await diffBlock.screenshot({ path: path.join(OUT_DIR, "wiki-149-diff.png") });

  // --- Artifact panel: line-number toggle + vertical scroll for long diff ---
  const longDiffId = results[1].artifactId;
  const binaryDiffId = results[2].artifactId;
  async function openPanel(page, tabs, focus) {
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

  await openPanel(sessionPage, [longDiffId], longDiffId);
  const artifactPanel = sessionPage.getByRole("complementary", { name: "Artifact panel" });
  await artifactPanel.waitFor({ state: "visible" });
  const panelDiff = artifactPanel.locator(".artifact-detail-diff");
  await panelDiff.waitFor({ state: "visible" });
  const scrollContainer = panelDiff.locator(".artifact-detail-diff-scroll");
  await scrollContainer.waitFor({ state: "visible" });

  const scrollGeometry = await scrollContainer.evaluate((node) => ({
    scrollHeight: node.scrollHeight,
    clientHeight: node.clientHeight,
    computedOverflowY: getComputedStyle(node).overflowY,
  }));
  assert(
    scrollGeometry.scrollHeight > scrollGeometry.clientHeight + 40,
    `long diff should overflow scroll container, geometry=${JSON.stringify(scrollGeometry)}`,
  );
  assert(
    /auto|scroll/.test(scrollGeometry.computedOverflowY),
    `scroll container should be scrollable, got overflow-y=${scrollGeometry.computedOverflowY}`,
  );

  await scrollContainer.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  const scrolled = await scrollContainer.evaluate((node) => ({
    scrollTop: node.scrollTop,
    max: node.scrollHeight - node.clientHeight,
  }));
  assert(
    scrolled.scrollTop > 0 && Math.abs(scrolled.scrollTop - scrolled.max) < 4,
    `scrollTop should reach end of long diff, got ${JSON.stringify(scrolled)}`,
  );

  const viewportVisibility = await scrollContainer.evaluate((node) => {
    const lines = node.querySelectorAll(".diff-line");
    const last = lines[lines.length - 1];
    if (!last) return null;
    const container = node.getBoundingClientRect();
    const lastRect = last.getBoundingClientRect();
    return {
      containerTop: container.top,
      containerBottom: container.bottom,
      lastTop: lastRect.top,
      lastBottom: lastRect.bottom,
    };
  });
  assert(viewportVisibility, "expected a final .diff-line in the scroll container");
  const tolerance = 2;
  assert(
    viewportVisibility.lastBottom <= viewportVisibility.containerBottom + tolerance,
    `final diff line bottom (${viewportVisibility.lastBottom}) should be within container bottom (${viewportVisibility.containerBottom})`,
  );
  assert(
    viewportVisibility.lastTop >= viewportVisibility.containerTop - tolerance,
    `final diff line top (${viewportVisibility.lastTop}) should be within container top (${viewportVisibility.containerTop})`,
  );

  const initialGutter = await panelDiff.locator(".diff-gutter").count();
  assert(initialGutter === 0, `line-number gutter should be off by default in panel, got ${initialGutter}`);

  const toggleButton = artifactPanel.locator("[data-diff-line-numbers-toggle]");
  await toggleButton.waitFor({ state: "visible" });
  assert(
    (await toggleButton.getAttribute("aria-pressed")) === "false",
    "line-number toggle should start aria-pressed=false",
  );
  await toggleButton.click();
  await sessionPage.waitForFunction(() => {
    const gutters = document.querySelectorAll(".artifact-detail-diff .diff-gutter");
    return gutters.length > 0;
  }, null, { timeout: 2000 });
  const enabledGutter = await panelDiff.locator(".diff-gutter").count();
  assert(enabledGutter > 0, `toggling line numbers on should render gutter cells, got ${enabledGutter}`);
  assert(
    (await toggleButton.getAttribute("aria-pressed")) === "true",
    "line-number toggle should flip aria-pressed to true after click",
  );

  // Isolation: switch to a different diff, its toggle should be off.
  const smallDiffId = results[0].artifactId;
  await openPanel(sessionPage, [longDiffId, smallDiffId], smallDiffId);
  await sessionPage.waitForFunction((id) => {
    const panel = document.querySelector('[data-focused-artifact]');
    return panel?.getAttribute("data-focused-artifact") === id;
  }, smallDiffId, { timeout: 3000 });
  const smallToggle = artifactPanel.locator("[data-diff-line-numbers-toggle]");
  await smallToggle.waitFor({ state: "visible" });
  assert(
    (await smallToggle.getAttribute("aria-pressed")) === "false",
    "switching to a different diff should show its own (default off) toggle state, not leak the previous diff's on state",
  );
  const smallGutter = await sessionPage.locator(".artifact-detail-diff .diff-gutter").count();
  assert(
    smallGutter === 0,
    `different diff should have no gutter cells despite previous diff having them on, got ${smallGutter}`,
  );

  // Persistence: switch back to the long diff, its toggle should remain on.
  await openPanel(sessionPage, [longDiffId, smallDiffId], longDiffId);
  await sessionPage.waitForFunction((id) => {
    const panel = document.querySelector('[data-focused-artifact]');
    return panel?.getAttribute("data-focused-artifact") === id;
  }, longDiffId, { timeout: 3000 });
  const longToggleAgain = artifactPanel.locator("[data-diff-line-numbers-toggle]");
  await longToggleAgain.waitFor({ state: "visible" });
  assert(
    (await longToggleAgain.getAttribute("aria-pressed")) === "true",
    "returning to the previously-toggled diff should preserve its on state",
  );
  const persistedGutter = await sessionPage.locator(".artifact-detail-diff .diff-gutter").count();
  assert(
    persistedGutter > 0,
    `returning to previously-toggled diff should re-render gutter cells, got ${persistedGutter}`,
  );

  await openPanel(sessionPage, [binaryDiffId], binaryDiffId);
  await sessionPage.waitForFunction((id) => {
    const kind = document
      .querySelector(".artifact-panel-detail")
      ?.getAttribute("data-artifact-detail-kind");
    return kind === "diff" && !!document.querySelector('.diff-file[data-file-kind="binary"]');
  }, binaryDiffId, { timeout: 4000 });
  const binaryFile = artifactPanel.locator('.diff-file[data-file-kind="binary"]');
  const binaryKindLabel = (await binaryFile.locator(".diff-file-kind").textContent())?.trim();
  assert(binaryKindLabel === "binary", `binary artifact should show kind label, got ${binaryKindLabel}`);
  const binaryPath = (await binaryFile.locator(".diff-file-path").textContent())?.trim();
  assert(binaryPath === "assets/logo.png", `binary artifact should show path, got ${binaryPath}`);
  const binaryMeta = (await binaryFile.locator(".diff-file-meta-line").first().textContent())?.trim();
  assert(
    binaryMeta?.startsWith("Binary files "),
    `binary artifact should render Binary-files meta line, got ${binaryMeta}`,
  );
  const binaryEmpty = await artifactPanel.locator(".diff-view-empty").count();
  assert(binaryEmpty === 0, `binary artifact must not fall back to 'No diff', got ${binaryEmpty} empty nodes`);

  await notePage.close();
  await sessionPage.close();

  // --- Copy button reachable on touch-only viewport (hover: none) ---
  const touchContext = await browser.newContext({
    viewport: { width: 900, height: 1200 },
    hasTouch: true,
    isMobile: true,
    deviceScaleFactor: 2,
  });
  const touchPage = await touchContext.newPage();
  await touchPage.addInitScript(({ layout }) => {
    localStorage.setItem("wiki-window-layout-v2", JSON.stringify(layout));
    localStorage.setItem("wiki-sidebar-visible", "false");
  }, { layout: noteLayout("sample.md") });
  await touchPage.goto(`${backend.baseUrl}/#/note/sample.md`, { waitUntil: "domcontentloaded" });

  await touchPage.waitForFunction(() => {
    return !!document.querySelector(".markdown-code-block-wrap .copy-pill");
  }, null, { timeout: 8000 });

  const hoverNoneMatches = await touchPage.evaluate(() => window.matchMedia("(hover: none)").matches);
  assert(hoverNoneMatches, "touch context should report (hover: none) matches=true");

  const touchStyle = await touchPage.evaluate(() => {
    const node = document.querySelector(".markdown-code-block-wrap .copy-pill");
    if (!node) return null;
    const style = getComputedStyle(node);
    return { opacity: parseFloat(style.opacity), pointerEvents: style.pointerEvents };
  });
  assert(touchStyle, "expected a .copy-pill element under touch viewport");
  assert(
    touchStyle.opacity > 0.9,
    `copy pill should be visible under (hover: none), opacity=${touchStyle.opacity}`,
  );
  assert(
    touchStyle.pointerEvents !== "none",
    `copy pill should be interactive under (hover: none), pointer-events=${touchStyle.pointerEvents}`,
  );

  await touchContext.close();
} finally {
  if (browser) await browser.close();
  if (backend) await backend.stop();
  rmSync(fixtures.root, { recursive: true, force: true });
}
