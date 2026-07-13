import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

import {
  codexAssistant,
  makeFixtureRoot,
  startBackend,
  writeQueue,
  writeRegistry,
} from "../scripts/wiki32-harness.mjs";

const TICKET = "WIKI-98";
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || "/tmp/wiki-98-playwright-evidence";

function logStep(message) {
  console.error(`[wiki-98-playwright] ${message}`);
}

async function writeTranscript(target) {
  const rows = [
    { type: "mode", mode: "normal", sessionId: "wiki-98-raw-html" },
    codexAssistant(
      [
        "Raw blocks stay visible:",
        "",
        "<details><summary>Expandable label</summary>Expandable body</details>",
        "",
        "A forced break<br>continues here.",
        "",
        "Water is H<sub>2</sub>O.",
        "",
        "<script>window.__wiki98Xss = true</script>",
        "",
        "```html",
        "<details>fenced code stays code</details>",
        "```",
        "",
        "Inline `<sub>inline code stays code</sub>` remains inline code.",
      ].join("\n"),
      "2026-07-13T18:00:00Z"
    ),
  ];
  await fs.writeFile(target, rows.map((row) => JSON.stringify(row)).join("\n") + "\n");
}

async function main() {
  logStep("preparing isolated fixtures");
  await fs.mkdir(OUT_DIR, { recursive: true });
  const fixtures = makeFixtureRoot("wiki-98-raw-html-");
  const transcript = path.join(fixtures.root, "wiki-98-raw-html.jsonl");
  await writeTranscript(transcript);
  const uiDemoDir = path.join(fixtures.root, "vault", "meta");
  await fs.mkdir(uiDemoDir, { recursive: true });
  await fs.copyFile(path.join(ROOT, "vault", "meta", "ui-demo.md"), path.join(uiDemoDir, "ui-demo.md"));
  writeRegistry(fixtures.registryPath, [[TICKET, transcript]]);
  writeQueue(fixtures.queuePath, TICKET, []);

  logStep("starting isolated backend");
  const backend = await startBackend(fixtures);
  logStep(`backend ready at ${backend.baseUrl}`);
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
              layout: { kind: "pane", id: "pane-1", path: "agent://WIKI-98" },
            },
          ],
        })
      );
    });
    await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".session-scroll");

    const assistant = page.locator(".session-assistant").first();
    await assistant.waitFor({ state: "visible" });
    for (const literal of ["<details>", "<br>", "<sub>", "<script>"]) {
      await assistant
        .locator("code.transcript-raw-html", { hasText: literal })
        .first()
        .waitFor({ state: "visible" });
    }

    for (const tag of ["details", "summary", "br", "sub", "script"]) {
      const count = await assistant.locator(tag).count();
      if (count !== 0) {
        throw new Error(`Expected raw <${tag}> to remain inert, found ${count} live elements`);
      }
    }
    const xssRan = await page.evaluate(() => window.__wiki98Xss === true);
    if (xssRan) throw new Error("Raw transcript HTML executed");

    await assistant
      .locator("pre code", { hasText: "<details>fenced code stays code</details>" })
      .waitFor({ state: "visible" });
    await assistant
      .locator("code:not(.transcript-raw-html)", { hasText: "<sub>inline code stays code</sub>" })
      .waitFor({ state: "visible" });

    const screenshotPath = path.join(OUT_DIR, "wiki-98-raw-html.png");
    logStep("capturing transcript screenshot");
    await page.locator(".session-scroll").screenshot({ path: screenshotPath });

    logStep("verifying the isolated ui-demo note remains spec-strict");
    await page.goto(`${backend.baseUrl}/#/note/meta/ui-demo.md`, { waitUntil: "domcontentloaded" });
    const note = page.locator(".markdown-reading-view");
    await note.waitFor({ state: "visible" });
    await note.getByRole("heading", { name: "Text styling" }).waitFor({ state: "visible" });
    await note.locator("mark", { hasText: "highlighted text" }).waitFor({ state: "visible" });
    await note.locator('.callout[data-callout="tip"]').waitFor({ state: "visible" });
    await note.locator("table", { hasText: "Startup" }).waitFor({ state: "visible" });
    const transcriptRawNodes = await note.locator("code.transcript-raw-html").count();
    if (transcriptRawNodes !== 0) {
      throw new Error("Vault note renderer unexpectedly used transcript raw-HTML handling");
    }
  } finally {
    await browser.close();
    await backend.stop();
  }
}

await main();
