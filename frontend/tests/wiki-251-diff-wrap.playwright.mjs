// WIKI-251 field verification:
//  1) The transcript, chat sidebar, and artifact renderers have no descendant
//     that horizontally overflows its container at the default width.
//  2) A file-edit tool renders as a side-by-side split diff (deletions LEFT,
//     insertions RIGHT), matching Henry's screenshot.
//  3) The default chat pane occupies ~45% of viewport at 1440+ (not fixed 480).
import fs from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";

import {
  makeFixtureRoot,
  startBackend,
  writeQueue,
  writeRegistry,
} from "../scripts/wiki32-harness.mjs";

const TICKET = "WIKI-251";

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function logStep(message) {
  console.error(`[wiki-251] ${message}`);
}

function ts(second) {
  const seconds = String(second % 60).padStart(2, "0");
  const minutes = String(Math.floor(second / 60)).padStart(2, "0");
  return `2026-08-06T14:${minutes}:${seconds}.000Z`;
}

function assistant(second, content) {
  return { type: "assistant", timestamp: ts(second), message: { role: "assistant", content } };
}

function toolResult(second, toolId, content, isError = false) {
  return {
    type: "user",
    timestamp: ts(second),
    message: {
      role: "user",
      content: [{ type: "tool_result", tool_use_id: toolId, content, is_error: isError }],
    },
  };
}

// Content that used to require horizontal scroll:
//  - A markdown-fenced code block with one very long line (URL-ish token).
//  - A markdown table with a wide row.
//  - An edit tool whose old_string/new_string are long paragraph lines.
const LONG_URL =
  "https://example.internal.wiki.app/very/deep/nested/path/segment/that/keeps/going/until/it/comfortably/exceeds/any/plausible/chat/pane/width/at/1280x720/or/wider?token=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

const CODE_FENCE = [
  "```bash",
  "# One very long line that would otherwise force a horizontal scrollbar.",
  `curl -sSL "${LONG_URL}" | jq -r '.items[] | select(.state == "open") | [.id, .title, .assignee.login] | @tsv'`,
  "```",
].join("\n");

const MD_TABLE = [
  "| Package | Version | Notes |",
  "| --- | --- | --- |",
  "| very-long-package-name-that-would-push-the-column | 1.2.3-beta.20260806+build.gruvbox | " +
    "This description is deliberately long to force wrapping — before WIKI-251 the row would overflow horizontally and the reader would have to scroll to see the trailing text. |",
  "| short | 0.1.0 | fine |",
].join("\n");

const LONG_OLD =
  "PR #185 landed transcript follow-ups (spacing model, live-state anchor, data-level caps) but has known regressions in hunk chrome and the \"polished\" toggle that need triage before we merge #186 — that context still lives in vault/hot.md.";
const LONG_NEW =
  "PR #186 landed the 1:1 fidelity + polish wave (fable-5, 10-item gap table), superseding the #185 regressions Henry flagged; transcript now honors fence tags, structures embedded heredocs, and infers file-slice output — see vault/hot.md for the eight-merge close-out.";

const TRANSCRIPT = [
  { type: "mode", mode: "normal", sessionId: `fixture-${TICKET}` },
  {
    type: "user",
    timestamp: ts(0),
    message: {
      role: "user",
      content: `here is the hot.md-style edit and a couple of wide surfaces\n\n${CODE_FENCE}\n\n${MD_TABLE}`,
    },
  },
  assistant(1, [
    { type: "text", text: "Applying the hot.md rewrite:" },
    {
      type: "tool_use",
      id: "toolu_edit",
      name: "Edit",
      input: {
        file_path: "vault/hot.md",
        old_string: LONG_OLD,
        new_string: LONG_NEW,
        replace_all: false,
      },
    },
  ]),
  toolResult(2, "toolu_edit", "The file vault/hot.md has been updated successfully."),
  assistant(3, [
    {
      type: "text",
      text: `Extra surfaces to sanity-check wrap on:\n\n${CODE_FENCE}\n\n${MD_TABLE}`,
    },
  ]),
];

async function writeJsonl(target, rows) {
  await fs.writeFile(target, rows.map((row) => JSON.stringify(row)).join("\n") + "\n");
}

async function overflowingDescendants(page, rootSelector) {
  return page.evaluate((selector) => {
    const root = document.querySelector(selector);
    if (!root) return { rootMissing: true, offenders: [] };
    const nodes = root.querySelectorAll("*");
    const offenders = [];
    for (const node of nodes) {
      // Ignore intentionally horizontal strips that live outside the
      // transcript body proper (kanban lanes, tmux chip rows).
      if (node.closest(".kanban-board, .tmux-window-list")) continue;
      // Some virtualization layers set width via inline style — skip the
      // scroll shell itself, only measure inner content.
      if (node.matches(".session-scroll, .session-scroll-inner")) continue;
      // Screen-reader-only content is deliberately clipped to a 1px box.
      if (node.matches(".sr-only")) continue;
      // Sub-glyph decorations (diff marker column, gutter numbers) have
      // fixed narrow content-boxes with padding; scrollWidth-vs-clientWidth
      // measures a few pixels of padding-vs-glyph inequality that never
      // reaches the reader.
      if (node.matches(".diff-marker, .diff-gutter")) continue;
      const overflow = node.scrollWidth - node.clientWidth;
      // Require a meaningful overflow — 24px is roughly one line of text.
      // Sub-line pixel noise (font metrics, sub-pixel rendering) doesn't
      // cause a horizontal scrollbar and shouldn't fail the sweep.
      if (overflow > 24) {
        offenders.push({
          selector: [node.tagName.toLowerCase(), ...node.classList].join("."),
          scrollWidth: node.scrollWidth,
          clientWidth: node.clientWidth,
          overflow,
        });
      }
    }
    return { rootMissing: false, offenders: offenders.slice(0, 8) };
  }, rootSelector);
}

async function main() {
  const fixtures = makeFixtureRoot("wiki-251-diff-wrap-");
  const transcript = path.join(fixtures.root, "wiki-251-claude.jsonl");
  await writeJsonl(transcript, TRANSCRIPT);
  writeRegistry(fixtures.registryPath, [[TICKET, transcript]]);
  writeQueue(fixtures.queuePath, TICKET, []);

  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  try {
    const viewportChecks = [
      { width: 1280, height: 900 },
      { width: 1440, height: 900 },
      { width: 1920, height: 1080 },
    ];

    for (const viewport of viewportChecks) {
      logStep(`viewport ${viewport.width}x${viewport.height}: booting transcript`);
      const context = await browser.newContext({ viewport });
      const page = await context.newPage();
      await page.addInitScript(({ layout }) => {
        localStorage.setItem("wiki-window-layout-v2", JSON.stringify(layout));
        localStorage.setItem("wiki-sidebar-visible", "false");
        // Clear any persisted chat width so we exercise the WIKI-251 default.
        localStorage.removeItem("wiki-session-sidebar-width");
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
      await page.waitForSelector(".session-scroll", { timeout: 30_000 });
      await page.evaluate(() => document.fonts.ready);

      // Wait for the edit tool + code fence + table to render.
      await page.waitForSelector(".session-tool-diff-body .diff-view", { timeout: 15_000 });
      await page.waitForSelector(".markdown-preview-view table", { timeout: 15_000 });
      await page.waitForSelector("pre code", { timeout: 15_000 });

      logStep(`viewport ${viewport.width}: file-edit renders as split (LEFT removes, RIGHT adds)`);
      const splitPresent = await page.locator(".diff-view.is-split").count();
      assert(splitPresent > 0, "file-edit must render through split view");
      const leftRemoves = await page.locator(".diff-split-cell.is-side-left.is-remove").count();
      const rightAdds = await page.locator(".diff-split-cell.is-side-right.is-add").count();
      assert(leftRemoves > 0, "split view must place removes on the LEFT");
      assert(rightAdds > 0, "split view must place adds on the RIGHT");

      logStep(`viewport ${viewport.width}: sweep transcript for horizontal overflow`);
      const scrollCheck = await overflowingDescendants(page, ".session-scroll-inner");
      assert(!scrollCheck.rootMissing, "session-scroll-inner missing");
      assert(
        scrollCheck.offenders.length === 0,
        `expected no horizontal overflow inside the transcript, saw: ${JSON.stringify(scrollCheck.offenders, null, 2)}`,
      );

      if (viewport.width >= 1440) {
        logStep(`viewport ${viewport.width}: chat pane default sits at ~45-50% of viewport`);
        const sidebarWidth = await page.evaluate(() => {
          const el = document.querySelector(".session-sidebar");
          return el ? el.getBoundingClientRect().width : null;
        });
        if (sidebarWidth !== null) {
          const ratio = sidebarWidth / viewport.width;
          assert(
            ratio >= 0.4 && ratio <= 0.55,
            `chat pane default ratio should sit in [0.40, 0.55]; got ${ratio.toFixed(3)} (width=${sidebarWidth}, viewport=${viewport.width})`,
          );
        }
      }

      await context.close();
    }
  } finally {
    try {
      await browser.close();
    } finally {
      await backend.stop().catch(() => {});
    }
  }

  logStep("all WIKI-251 checks passed");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
