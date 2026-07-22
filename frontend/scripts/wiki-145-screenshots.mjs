import fs from "node:fs/promises";
import { mkdirSync, rmSync } from "node:fs";
import path from "node:path";
import { chromium } from "playwright";

import {
  codexAssistant,
  codexUser,
  makeFixtureRoot,
  startBackend,
  writeQueue,
  writeRegistry,
} from "./wiki32-harness.mjs";

const OUT = "/tmp";
const TICKET = "WIKI-145";
const THEMES = [
  { id: "mono-light", label: "mono-light" },
  { id: "mono-dark", label: "mono-dark" },
];
const fixtures = makeFixtureRoot("wiki-145-screenshots-");

const CHAT_ROWS = [
  { type: "mode", mode: "normal", sessionId: "wiki-145-chat" },
  codexUser("Rewrite this helper to use tabular nums.", "2026-07-22T15:00:00Z"),
  codexAssistant(
    [
      "Here's the tightened helper with `tabular-nums` on the count column.",
      "",
      "```ts",
      "export function fmt(n: number) {",
      "  return n.toLocaleString('en-US');",
      "}",
      "```",
      "",
      "Inline math: the Big-O of the loop is $O(n \\log n)$ once we swap to the priority queue.",
      "",
      "```diff",
      "- <span>{count}</span>",
      "+ <span className=\"tabular-nums\">{count}</span>",
      "```",
      "",
      "Let me know if you want a table view too.",
    ].join("\n"),
    "2026-07-22T15:00:01Z",
  ),
];

const NOTE_MD = `# Wiki refresh II — sample surface

A quick sample note that exercises the reading surface: prose, lists, code, and a diagram.

- Left rail is now transparent and narrower.
- Tabs collapse to a single hairline underline.
- Assistant text flows as book prose — no left rail border.

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

async function takeShot(page, id, theme, surface) {
  await page.evaluate((themeId) => {
    localStorage.setItem("wiki-theme", themeId);
    document.documentElement.dataset.theme = themeId;
  }, theme.id);
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForTimeout(400);
  const out = path.join(OUT, `WIKI-145-screenshots-after-${theme.label}-${surface}.png`);
  await page.screenshot({ path: out, fullPage: false });
  console.log(`saved ${out}`);
}

let browser;
let backend;
try {
  const transcript = path.join(fixtures.root, "chat.jsonl");
  await fs.writeFile(transcript, CHAT_ROWS.map((r) => JSON.stringify(r)).join("\n") + "\n");
  await fs.mkdir(path.join(fixtures.root, "vault"), { recursive: true });
  await fs.writeFile(path.join(fixtures.root, "vault", "wiki145-sample.md"), NOTE_MD);
  writeRegistry(fixtures.registryPath, [[TICKET, transcript]]);
  writeQueue(fixtures.queuePath, TICKET, []);
  backend = await startBackend(fixtures);
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

  for (const theme of THEMES) {
    // (a) chat surface with real conversation.
    await page.addInitScript(({ themeId }) => {
      localStorage.setItem("wiki-sidebar-visible", "true");
      localStorage.setItem("wiki-theme", themeId);
      localStorage.setItem(
        "wiki-window-layout-v2",
        JSON.stringify({
          version: 2,
          activeWindowId: "window-chat",
          windows: [
            {
              id: "window-chat",
              focusedPaneId: "pane-chat",
              layout: { kind: "pane", id: "pane-chat", path: `agent://${"WIKI-145"}` },
            },
          ],
        }),
      );
    }, { themeId: theme.id });
    await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".agent-session-surface, .workspace-leaf", { timeout: 10000 });
    await page.waitForTimeout(600);
    await page.screenshot({
      path: path.join(OUT, `WIKI-145-screenshots-after-${theme.label}-chat.png`),
      fullPage: false,
    });
    console.log(`saved chat ${theme.label}`);

    // (b) note surface.
    await page.goto(`${backend.baseUrl}/#/view/wiki145-sample.md`, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(700);
    await page.screenshot({
      path: path.join(OUT, `WIKI-145-screenshots-after-${theme.label}-note.png`),
      fullPage: false,
    });
    console.log(`saved note ${theme.label}`);

    // (c) dashboard.
    await page.goto(`${backend.baseUrl}/#/dashboard`, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(700);
    await page.screenshot({
      path: path.join(OUT, `WIKI-145-screenshots-after-${theme.label}-dashboard.png`),
      fullPage: false,
    });
    console.log(`saved dashboard ${theme.label}`);

    // (d) artifact panel — reuse chat page and toggle artifact panel keyboard shortcut.
    await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(500);
    // Attempt to open artifact panel; if not available, just capture chat with tabs.
    const artifactVisible = await page.evaluate(() => !!document.querySelector(".artifact-panel-tabs"));
    await page.screenshot({
      path: path.join(OUT, `WIKI-145-screenshots-after-${theme.label}-artifact.png`),
      fullPage: false,
    });
    console.log(`saved artifact ${theme.label} (panel visible: ${artifactVisible})`);
  }
} finally {
  if (browser) await browser.close();
  if (backend) await backend.stop();
  rmSync(fixtures.root, { recursive: true, force: true });
}
