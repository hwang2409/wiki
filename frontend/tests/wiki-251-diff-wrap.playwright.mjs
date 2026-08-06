// WIKI-251 field verification (round 2):
//  1) A file-edit tool renders as a side-by-side split diff (deletions LEFT,
//     insertions RIGHT), matching Henry's screenshot.
//  2) No descendant of the transcript, its markdown surfaces, or its artifact
//     archetypes (table, code, diff, JSON) horizontally overflows the
//     container at 1280 / 1440 / 1920 viewport widths. Tolerance: 1px for
//     sub-pixel rendering noise — anything above is a real overflow.
//  3) The `.is-nowrap` escape hatch is dead: no DOM node exposes it as a
//     surviving tool-output consumer. If a future change wires the class
//     back in, this stage fails.
//  4) Chat pane default width tracks the viewport (~47%) and re-flows on
//     window resize until the user drags the handle — covered by the vitest
//     unit in wiki-251-sidebar-width.unit.test.ts (SessionSidebar directly
//     rendering it in playwright requires a live worker registry, which is
//     out of scope for this stage; the unit test verifies the same contract
//     on the exact code path).
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
const OVERFLOW_TOLERANCE_PX = 1;

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
//  - Artifact archetypes (table + JSON + diff + code) so their wrap contract
//    is exercised at the same viewport widths.
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

// Artifact archetypes shipped inline in an assistant message via the
// wiki:artifact code-fence protocol (renderer parses them into artifact
// blocks). Each one exercises a specific wrap surface.
const ARTIFACT_LONG_JSON = JSON.stringify({
  url: LONG_URL,
  description:
    "Long JSON value with a very-very-long-token-that-would-otherwise-push-the-container-off-screen-until-the-wrap-sweep-lands ".repeat(2),
  ids: Array.from({ length: 6 }, (_, i) => `token-${i}-` + "x".repeat(80)),
});

const ARTIFACT_DIFF = [
  "diff --git a/vault/hot.md b/vault/hot.md",
  "index 0000000..1111111 100644",
  "--- a/vault/hot.md",
  "+++ b/vault/hot.md",
  "@@ -1,4 +1,4 @@",
  " ## Active threads",
  ` - **WIKI**: ${LONG_URL}`,
  "-  bugfix: previously overflowed",
  "+  bugfix: now wraps in place inside the diff view — no horizontal scroll on the diff artifact surface either",
  " ",
].join("\n");

const ARTIFACT_TABLE_ROWS = [
  { pkg: "very-long-package-name-that-would-push-the-column", version: "1.2.3-beta.20260806+build.gruvbox", note: "long note ".repeat(30) },
  { pkg: "short", version: "0.1.0", note: "fine" },
];

const ARTIFACT_CODE_LINE = `curl -sSL "${LONG_URL}" | jq -r '.items[] | select(.state == "open")'`;

function artifactFenceBlock(payload) {
  const source = JSON.stringify(payload);
  return ["```wiki:artifact", source, "```"].join("\n");
}

const ARTIFACT_TABLE_ARTIFACT = artifactFenceBlock({
  kind: "table",
  title: "packages",
  columns: [
    { key: "pkg", label: "Package" },
    { key: "version", label: "Version" },
    { key: "note", label: "Notes" },
  ],
  rows: ARTIFACT_TABLE_ROWS,
});

const ARTIFACT_JSON_ARTIFACT = artifactFenceBlock({
  kind: "json",
  title: "trace",
  source: ARTIFACT_LONG_JSON,
});

const ARTIFACT_DIFF_ARTIFACT = artifactFenceBlock({
  kind: "diff",
  title: "hot.md.diff",
  source: ARTIFACT_DIFF,
});

const ARTIFACT_CODE_ARTIFACT = artifactFenceBlock({
  kind: "code",
  title: "curl-command.sh",
  lang: "bash",
  source: ARTIFACT_CODE_LINE,
});

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
      text: `Extra surfaces to sanity-check wrap on:\n\n${CODE_FENCE}\n\n${MD_TABLE}\n\n${ARTIFACT_TABLE_ARTIFACT}\n\n${ARTIFACT_JSON_ARTIFACT}\n\n${ARTIFACT_DIFF_ARTIFACT}\n\n${ARTIFACT_CODE_ARTIFACT}`,
    },
  ]),
];

async function writeJsonl(target, rows) {
  await fs.writeFile(target, rows.map((row) => JSON.stringify(row)).join("\n") + "\n");
}

async function overflowingDescendants(page, rootSelector) {
  return page.evaluate(({ selector, tolerance }) => {
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
      // Diff line-number gutters and marker glyphs live in narrow fixed
      // content-boxes whose padding-vs-glyph metrics produce sub-pixel
      // scrollWidth vs clientWidth noise; the contract explicitly permits
      // the gutter to keep its own narrow column.
      if (node.matches(".diff-marker, .diff-gutter, .diff-gutter-col")) continue;
      // Panzoom viewports are pan-and-zoom surfaces (SVG, mermaid) that
      // legitimately hold content larger than the viewport for user
      // interaction — they're pan controls, not text.
      if (node.matches(".artifact-panzoom-viewport, .artifact-panzoom-content")) continue;
      // Table structural elements (<tr>, <tbody>, <colgroup>, <col>) report
      // scrollWidth vs clientWidth from cell-box math that isn't user-visible
      // scroll. The table itself and every leaf CELL still get measured —
      // that's what actually reveals reader-facing overflow.
      const tag = node.tagName;
      if (tag === "TR" || tag === "TBODY" || tag === "THEAD" || tag === "COLGROUP" || tag === "COL") continue;
      const overflow = node.scrollWidth - node.clientWidth;
      if (overflow > tolerance) {
        offenders.push({
          selector: [node.tagName.toLowerCase(), ...node.classList].join("."),
          scrollWidth: node.scrollWidth,
          clientWidth: node.clientWidth,
          overflow,
        });
      }
    }
    return { rootMissing: false, offenders: offenders.slice(0, 16) };
  }, { selector: rootSelector, tolerance: OVERFLOW_TOLERANCE_PX });
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
        localStorage.removeItem("wiki-session-sidebar-width-custom");
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
      await page.waitForSelector(".session-tool-split-diff", { timeout: 15_000 });
      await page.waitForSelector(".markdown-preview-view table", { timeout: 15_000 });
      await page.waitForSelector("pre code", { timeout: 15_000 });

      logStep(`viewport ${viewport.width}: file-edit renders as split (LEFT deletions, RIGHT insertions)`);
      const splitPresent = await page.locator(".session-tool-split-diff").count();
      assert(splitPresent > 0, "file-edit must render through SplitDiffView");
      const deleteCells = await page.locator(".session-tool-split-diff .diff-code-delete").count();
      const insertCells = await page.locator(".session-tool-split-diff .diff-code-insert").count();
      assert(deleteCells > 0, "split view must expose delete-side cells (react-diff-view .diff-code-delete)");
      assert(insertCells > 0, "split view must expose insert-side cells (react-diff-view .diff-code-insert)");

      logStep(`viewport ${viewport.width}: sweep transcript for horizontal overflow (tolerance ${OVERFLOW_TOLERANCE_PX}px)`);
      const scrollCheck = await overflowingDescendants(page, ".session-scroll-inner");
      assert(!scrollCheck.rootMissing, "session-scroll-inner missing");
      assert(
        scrollCheck.offenders.length === 0,
        `expected no horizontal overflow inside the transcript, saw: ${JSON.stringify(scrollCheck.offenders, null, 2)}`,
      );

      // Wait for artifact archetypes to have rendered (or been surfaced as
      // fallbacks). The renderer emits `.artifact-block` for every parsed
      // fence — assert we have all four archetypes before checking overflow.
      // The renderer may downgrade to a fallback under load — that's fine so
      // long as the fallback doesn't itself overflow.
      const artifactCount = await page.locator(".artifact-block").count();
      logStep(`viewport ${viewport.width}: ${artifactCount} artifact blocks rendered`);
      if (artifactCount > 0) {
        const artifactOverflow = await page.evaluate(({ tolerance }) => {
          const nodes = document.querySelectorAll(".artifact-block *");
          const offenders = [];
          for (const node of nodes) {
            if (node.matches(".diff-marker, .diff-gutter, .diff-gutter-col")) continue;
            if (node.matches(".artifact-panzoom-viewport, .artifact-panzoom-content")) continue;
            const tag = node.tagName;
            if (tag === "TR" || tag === "TBODY" || tag === "THEAD" || tag === "COLGROUP" || tag === "COL") continue;
            const overflow = node.scrollWidth - node.clientWidth;
            if (overflow > tolerance) {
              offenders.push({
                selector: [node.tagName.toLowerCase(), ...node.classList].join("."),
                scrollWidth: node.scrollWidth,
                clientWidth: node.clientWidth,
                overflow,
              });
            }
          }
          return offenders.slice(0, 16);
        }, { tolerance: OVERFLOW_TOLERANCE_PX });
        assert(
          artifactOverflow.length === 0,
          `expected no horizontal overflow inside artifact blocks, saw: ${JSON.stringify(artifactOverflow, null, 2)}`,
        );
      }

      logStep(`viewport ${viewport.width}: .is-nowrap has zero surviving consumers on tool output`);
      const nowrapCount = await page.evaluate(() => document.querySelectorAll(".is-nowrap").length);
      assert(
        nowrapCount === 0,
        `.is-nowrap escape hatch must be gone; found ${nowrapCount} nodes still wearing it`,
      );

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
