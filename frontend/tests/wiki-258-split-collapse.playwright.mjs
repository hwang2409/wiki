// WIKI-258 field verification: a SHORT-content edit tool (the repro that
// collapsed to ~1ch-wide columns) renders a readable split diff in the
// transcript — two equal code columns filling the container, narrow gutters.
// Root cause was the legacy .diff-line { display: flex } rule leaking onto
// react-diff-view's <tr class="diff-line"> rows, detaching cells from the
// table columns; overflow-wrap: anywhere then let them shrink to min-content.
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

import {
  makeFixtureRoot,
  startBackend,
  writeQueue,
  writeRegistry,
} from "../scripts/wiki32-harness.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const UPDATE_EVIDENCE = process.env.WIKI_UPDATE_EVIDENCE === "1";
const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR
  || (UPDATE_EVIDENCE
    ? path.join(HERE, "evidence", "wiki-258")
    : await fs.mkdtemp(path.join(os.tmpdir(), "wiki-258-evidence-")));
const TICKET = "WIKI-258";

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function logStep(message) {
  console.error(`[wiki-258] ${message}`);
}

function ts(second) {
  const seconds = String(second % 60).padStart(2, "0");
  const minutes = String(Math.floor(second / 60)).padStart(2, "0");
  return `2026-08-06T14:${minutes}:${seconds}.000Z`;
}

// The exact repro shape from Henry's screenshot: a one-token edit. Its
// min-content width is ~1ch under overflow-wrap: anywhere, so any regression
// that re-detaches cells from the table columns collapses it again.
const TRANSCRIPT = [
  { type: "mode", mode: "normal", sessionId: `fixture-${TICKET}` },
  {
    type: "user",
    timestamp: ts(0),
    message: { role: "user", content: "update the watchlist" },
  },
  {
    type: "assistant",
    timestamp: ts(1),
    message: {
      role: "assistant",
      content: [
        { type: "text", text: "Updating the watchlist:" },
        {
          type: "tool_use",
          id: "toolu_edit",
          name: "Edit",
          input: {
            file_path: "/tmp/agent-status/.phoebe-dev-watchlist",
            old_string: "PHO-15330",
            new_string: "PHO-15336",
            replace_all: false,
          },
        },
      ],
    },
  },
  {
    type: "user",
    timestamp: ts(2),
    message: {
      role: "user",
      content: [
        {
          type: "tool_result",
          tool_use_id: "toolu_edit",
          content: "The file /tmp/agent-status/.phoebe-dev-watchlist has been updated successfully.",
          is_error: false,
        },
      ],
    },
  },
];

async function main() {
  const fixtures = makeFixtureRoot("wiki-258-split-collapse-");
  const transcript = path.join(fixtures.root, "wiki-258-claude.jsonl");
  await fs.writeFile(transcript, TRANSCRIPT.map((row) => JSON.stringify(row)).join("\n") + "\n");
  writeRegistry(fixtures.registryPath, [[TICKET, transcript]]);
  writeQueue(fixtures.queuePath, TICKET, []);
  await fs.mkdir(OUT_DIR, { recursive: true });

  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  try {
    for (const viewport of [{ width: 1280, height: 900 }, { width: 1920, height: 1080 }]) {
      logStep(`viewport ${viewport.width}x${viewport.height}: booting transcript`);
      const context = await browser.newContext({ viewport });
      const page = await context.newPage();
      await page.addInitScript(({ layout }) => {
        localStorage.setItem("wiki-window-layout-v2", JSON.stringify(layout));
        localStorage.setItem("wiki-sidebar-visible", "false");
      }, {
        layout: {
          version: 2,
          activeWindowId: "window-0",
          windows: [
            { id: "window-0", focusedPaneId: "pane-1", layout: { kind: "pane", id: "pane-1", path: `agent://${TICKET}` } },
          ],
        },
      });
      await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
      await page.waitForSelector(".session-tool-split-diff table.diff", { timeout: 30_000 });
      await page.evaluate(() => document.fonts.ready);

      const metrics = await page.evaluate(() => {
        const container = document.querySelector(".session-tool-split-diff .split-diff-file");
        const table = container.querySelector("table.diff");
        const row = table.querySelector("tr.diff-line");
        const codeCells = Array.from(row.querySelectorAll("td.diff-code"));
        const gutterCells = Array.from(row.querySelectorAll("td.diff-gutter"));
        return {
          rowDisplay: getComputedStyle(row).display,
          containerWidth: container.getBoundingClientRect().width,
          tableWidth: table.getBoundingClientRect().width,
          codeWidths: codeCells.map((cell) => cell.getBoundingClientRect().width),
          gutterWidths: gutterCells.map((cell) => cell.getBoundingClientRect().width),
        };
      });
      logStep(`viewport ${viewport.width}: ${JSON.stringify(metrics)}`);

      // The row must lay out as a table row — display: flex on the <tr> is
      // exactly the WIKI-258 regression.
      assert(
        metrics.rowDisplay === "table-row",
        `tr.diff-line must be display: table-row; saw ${metrics.rowDisplay}`,
      );
      // Table fills its container (width: 100% resolved, not min-content).
      assert(
        metrics.tableWidth >= metrics.containerWidth * 0.9,
        `split table must fill the container; table=${metrics.tableWidth} container=${metrics.containerWidth}`,
      );
      // Two code columns, each a sane fraction of the table — the collapse
      // rendered them at ~16px (about 2% of the table).
      assert(metrics.codeWidths.length === 2, `expected 2 code cells, saw ${metrics.codeWidths.length}`);
      for (const width of metrics.codeWidths) {
        assert(
          width >= metrics.tableWidth * 0.3,
          `each code column must be >=30% of the table; saw ${width} of ${metrics.tableWidth}`,
        );
      }
      assert(
        Math.abs(metrics.codeWidths[0] - metrics.codeWidths[1]) <= 1,
        `code columns must be equal width; saw ${metrics.codeWidths.join(" vs ")}`,
      );
      // Gutters stay narrow.
      for (const width of metrics.gutterWidths) {
        assert(width <= 80, `gutters must stay narrow (<=80px); saw ${width}`);
      }

      const diffShot = path.join(OUT_DIR, `split-diff-${viewport.width}.png`);
      await page.locator(".session-tool-body.session-tool-diff-body").screenshot({ path: diffShot });
      const pageShot = path.join(OUT_DIR, `transcript-${viewport.width}.png`);
      await page.screenshot({ path: pageShot });
      logStep(`viewport ${viewport.width}: evidence ${diffShot}`);
      await context.close();
    }
  } finally {
    try {
      await browser.close();
    } finally {
      await backend.stop().catch(() => {});
    }
  }

  logStep(`all WIKI-258 checks passed; evidence in ${OUT_DIR}`);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
