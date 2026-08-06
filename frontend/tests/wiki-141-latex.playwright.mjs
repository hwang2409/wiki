import fs from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";

import {
  codexAssistant,
  makeFixtureRoot,
  startBackend,
  writeQueue,
  writeRegistry,
} from "../scripts/wiki32-harness.mjs";

// Keep rehypeKatex before raw-HTML escaping. The mixed message below guards
// both transforms and protects this order if either plugin becomes broader.
const TICKET = "WIKI-141";
const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || "/tmp/wiki-141-playwright-evidence";

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

const mathMarkdown = [
  "# Math fixture",
  "",
  "Inline math: \\(E = mc^2\\)",
  "",
  "$$",
  "\\int_0^1 x^2 dx",
  "$$",
  "",
  "Plain text $foo",
].join("\n");

const transcriptMarkdown = [
  'Raw HTML stays literal: <span data-wiki-141-probe="live">raw probe</span>',
  "",
  mathMarkdown,
].join("\n");

async function main() {
  await fs.mkdir(OUT_DIR, { recursive: true });
  const fixtures = makeFixtureRoot("wiki-141-latex-");
  const vault = path.join(fixtures.root, "vault");
  const notePath = path.join(vault, "math.md");
  const transcript = path.join(fixtures.root, "wiki-141-latex.jsonl");
  await fs.mkdir(vault, { recursive: true });
  await fs.writeFile(notePath, mathMarkdown);
  await fs.writeFile(
    transcript,
    [
      { type: "mode", mode: "normal", sessionId: "wiki-141-latex" },
      codexAssistant(transcriptMarkdown, "2026-07-21T18:00:00Z"),
    ]
      .map((row) => JSON.stringify(row))
      .join("\n") + "\n",
  );
  writeRegistry(fixtures.registryPath, [[TICKET, transcript]]);
  writeQueue(fixtures.queuePath, TICKET, []);

  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 1200 } });

  try {
    await page.addInitScript(() => {
      localStorage.setItem("wiki-sidebar-visible", "false");
      localStorage.setItem(
        "wiki-window-layout-v2",
        JSON.stringify({
          version: 2,
          activeWindowId: "window-0",
          windows: [
            {
              id: "window-0",
              focusedPaneId: "pane-1",
              layout: { kind: "pane", id: "pane-1", path: "math.md" },
            },
          ],
        }),
      );
    });

    await page.goto(`${backend.baseUrl}/#/note/math.md`, { waitUntil: "domcontentloaded" });
    const note = page.locator(".markdown-preview-view");
    await note.waitFor({ state: "visible" });
    await note.locator(".katex").first().waitFor({ state: "visible" });
    await note.locator(".katex-display").waitFor({ state: "visible" });
    assert(await note.locator(".katex").count() >= 2, "note inline and block math did not render");
    assert((await note.innerText()).includes("Plain text $foo"), "unclosed note dollar stayed false-positive math");

    await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
    const assistant = page.locator(".session-assistant").first();
    await assistant.waitFor({ state: "visible" });
    await assistant.locator(".katex").first().waitFor({ state: "visible" });
    await assistant.locator(".katex-display").waitFor({ state: "visible" });
    const escapedRawHtml = assistant.locator("code.transcript-raw-html").first();
    await escapedRawHtml.waitFor({ state: "visible" });
    assert(await assistant.locator(".katex").count() >= 2, "transcript inline and block math did not render");
    const escapedRawText = (await assistant.locator("code.transcript-raw-html").allTextContents()).join("");
    assert(
      escapedRawText.includes('<span data-wiki-141-probe="live">') &&
        escapedRawText.includes("</span>"),
      `raw transcript HTML was not preserved as literal text: ${escapedRawText}`,
    );
    assert((await assistant.innerText()).includes("raw probe"), "raw transcript text content disappeared");
    assert(await assistant.locator('span[data-wiki-141-probe="live"]').count() === 0, "raw transcript HTML became live markup");
    assert((await assistant.innerText()).includes("Plain text $foo"), "unclosed transcript dollar stayed false-positive math");

    await page.locator(".session-scroll").screenshot({ path: path.join(OUT_DIR, "wiki-141-latex.png") });
  } finally {
    await browser.close();
    await backend.stop();
  }
}

await main();
