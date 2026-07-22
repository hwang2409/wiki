import { spawnSync } from "node:child_process";
import fs from "node:fs/promises";
import { mkdirSync, rmSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

import {
  codexAssistant,
  codexUser,
  makeFixtureRoot,
  startBackend,
  writeQueue,
  writeRegistry,
} from "./wiki32-harness.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");
const OUT = "/tmp";
const TICKET = "WIKI-145";
const RUN_ID = "00000000-0000-4000-8000-000000000145";
const PHASE = (process.env.WIKI_145_SCREENSHOT_PHASE || "after").toLowerCase();
const THEMES = [
  { id: "mono-light", label: "mono-light" },
  { id: "mono-dark", label: "mono-dark" },
];

// -------- fixture setup --------

function artifactInputs() {
  const diffLines = [];
  for (let i = 0; i < 120; i += 1) {
    diffLines.push(`- old line ${i}`);
    diffLines.push(`+ new line ${i}`);
  }
  return [
    {
      kind: "code",
      title: "Parser change",
      caption: "Unified diff (oversized fixture, opens in panel)",
      payload: {
        language: "typescript",
        filename: "src/artifact.ts",
        source: diffLines.join("\n") + "\n",
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
      params: {
        protocolVersion: "2025-06-18",
        capabilities: {},
        clientInfo: { name: "wiki-145-fixture", version: "1" },
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
    input: requests.map((r) => JSON.stringify(r)).join("\n") + "\n",
    encoding: "utf8",
  });
  if (result.status !== 0) {
    throw new Error(`artifact fixture worker failed: ${result.stderr || result.stdout}`);
  }
  const responses = result.stdout.trim().split("\n").map((line) => JSON.parse(line));
  return responses.slice(1).map((response) => {
    const sentinel = response.result.content[0].text;
    const event = JSON.parse(sentinel.slice("<<wiki-artifact:v1>>".length, -"<<end>>".length));
    return { artifactId: event.id, sentinel };
  });
}

const CHAT_MD = [
  "Here's the tightened helper.",
  "",
  "```ts",
  "export function fmt(n: number) {",
  "  return n.toLocaleString('en-US');",
  "}",
  "```",
  "",
  "Inline math: $O(n \\log n)$ once we swap to the priority queue.",
  "",
  "Let me know if you want a table view too.",
].join("\n");

async function buildTranscript(fixtures, artifactResults) {
  const rows = [
    { type: "mode", mode: "normal", sessionId: "wiki-145-chat" },
    codexUser("Rewrite this helper to use tabular nums.", "2026-07-22T15:00:00Z"),
    codexAssistant(CHAT_MD, "2026-07-22T15:00:01Z"),
    {
      type: "assistant",
      timestamp: "2026-07-22T15:00:02Z",
      message: {
        role: "assistant",
        content: [
          {
            type: "tool_use",
            id: "toolu_wiki145_diff",
            name: "mcp__wiki-artifacts__render_artifact",
            input: artifactInputs()[0],
          },
        ],
      },
    },
    {
      type: "user",
      timestamp: "2026-07-22T15:00:03Z",
      message: {
        role: "user",
        content: [
          {
            type: "tool_result",
            tool_use_id: "toolu_wiki145_diff",
            content: artifactResults[0].sentinel,
          },
        ],
      },
    },
  ];
  const transcript = path.join(fixtures.root, "wiki-145-chat.jsonl");
  await fs.writeFile(transcript, rows.map((r) => JSON.stringify(r)).join("\n") + "\n");
  return transcript;
}

const NOTE_MD = `# Wiki refresh II — sample note

Prose flows book-like, no card wrapper.

- Left rail is transparent and narrow.
- Tabs collapse to a single hairline underline.
- Assistant text has no left border rail.

## Math sample

Block math renders via rehype-katex:

$$
\\int_0^\\pi \\sin x \\; dx = 2
$$

## Diagram

\`\`\`mermaid
flowchart LR
  A[Input] --> B[Transform]
  B --> C[Output]
\`\`\`

## Code block

\`\`\`ts
export const READABLE = 760;
\`\`\`
`;

// -------- theme + probe helpers --------

const EXPECTED_BG = {
  "mono-light": "#ffffff",
  "mono-dark": "#111111",
};

async function verifyTheme(page, themeId) {
  const observed = await page.evaluate(() => {
    const cs = getComputedStyle(document.documentElement);
    return {
      bg: cs.getPropertyValue("--background-primary").trim().toLowerCase(),
      bodyBg: getComputedStyle(document.body).backgroundColor,
      dataTheme: document.documentElement.dataset.theme || null,
    };
  });
  const expected = EXPECTED_BG[themeId];
  if (!expected) throw new Error(`no expected bg for theme ${themeId}`);
  if (observed.bg !== expected) {
    throw new Error(
      `theme ${themeId}: expected --background-primary ${expected}, got ${observed.bg} (data-theme=${observed.dataTheme}, bodyBg=${observed.bodyBg})`,
    );
  }
  return observed;
}

async function newContextForTheme(browser, themeId) {
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
  });
  await context.addInitScript(({ id, layoutKey, ticket }) => {
    localStorage.setItem("wiki-theme", id);
    localStorage.setItem("wiki-sidebar-visible", "true");
    localStorage.setItem(
      layoutKey,
      JSON.stringify({
        version: 2,
        activeWindowId: "window-chat",
        windows: [
          {
            id: "window-chat",
            focusedPaneId: "pane-chat",
            layout: { kind: "pane", id: "pane-chat", path: `agent://${ticket}` },
          },
        ],
      }),
    );
  }, { id: themeId, layoutKey: "wiki-window-layout-v2", ticket: TICKET });
  return context;
}

async function captureSurface(page, themeLabel, surface) {
  const out = path.join(OUT, `WIKI-145-screenshots-${PHASE}-${themeLabel}-${surface}.png`);
  await page.screenshot({ path: out, fullPage: false });
  const stat = await fs.stat(out);
  console.log(`  ${surface}: ${out} (${stat.size} bytes)`);
  return out;
}

async function gotoAndWait(page, backend, hash, selector, description) {
  await page.goto(`${backend.baseUrl}/${hash}`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector(selector, { timeout: 15000, state: "visible" }).catch((err) => {
    throw new Error(
      `${description}: expected selector "${selector}" never appeared at ${hash}: ${err.message}`,
    );
  });
}

// -------- main --------

const fixtures = makeFixtureRoot("wiki-145-screenshots-");
let browser;
let backend;
try {
  await fs.mkdir(path.join(fixtures.root, "vault"), { recursive: true });
  await fs.writeFile(path.join(fixtures.root, "vault", "wiki145-sample.md"), NOTE_MD);

  const artifactResults = invokeFixtureWorker(fixtures, artifactInputs());
  const transcript = await buildTranscript(fixtures, artifactResults);
  writeRegistry(fixtures.registryPath, [[TICKET, transcript]]);
  writeQueue(fixtures.queuePath, TICKET, []);

  backend = await startBackend(fixtures);
  browser = await chromium.launch({ headless: true });

  for (const theme of THEMES) {
    console.log(`\ntheme=${theme.label}`);
    const context = await newContextForTheme(browser, theme.id);
    const page = await context.newPage();

    // (a) chat: assert message and artifact chip present.
    await gotoAndWait(
      page,
      backend,
      `#/agent/${TICKET}`,
      ".session-scroll-inner",
      "chat surface",
    );
    await page.waitForSelector(".session-assistant", { timeout: 15000 });
    await verifyTheme(page, theme.id);
    await page.waitForTimeout(400);
    await captureSurface(page, theme.label, "chat");

    // (b) note: real hash route, wait for the markdown reading view.
    await gotoAndWait(
      page,
      backend,
      "#/note/wiki145-sample.md",
      ".markdown-preview-view, .markdown-reading-view",
      "note surface",
    );
    await page.waitForSelector(".inline-title", { timeout: 15000 });
    await verifyTheme(page, theme.id);
    await page.waitForTimeout(500);
    await captureSurface(page, theme.label, "note");

    // (c) dashboard: real hash route, wait for dashboard-header.
    await gotoAndWait(page, backend, "#/dashboard", ".dashboard-header", "dashboard surface");
    await verifyTheme(page, theme.id);
    await page.waitForTimeout(400);
    await captureSurface(page, theme.label, "dashboard");

    // (d) artifact panel: open via URL query params (panelStateFromUrl restores on mount).
    const artifactId = artifactResults[0].artifactId;
    const artifactHash = `?panel=${TICKET}&artifact=${artifactId}&tab=${artifactId}&focus=${artifactId}#/agent/${TICKET}`;
    await gotoAndWait(
      page,
      backend,
      artifactHash,
      ".artifact-panel-tabs .artifact-panel-tab.is-active",
      "artifact surface",
    );
    await verifyTheme(page, theme.id);
    await page.waitForTimeout(500);
    await captureSurface(page, theme.label, "artifact");

    await context.close();
  }
} finally {
  if (browser) await browser.close();
  if (backend) await backend.stop();
  rmSync(fixtures.root, { recursive: true, force: true });
}
