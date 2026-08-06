// WIKI-251 field verification (round 3):
//  1) A file-edit tool renders as a side-by-side split diff (deletions LEFT,
//     insertions RIGHT), matching Henry's screenshot.
//  2) No descendant of the transcript, its markdown surfaces, or its artifact
//     archetypes (table, code, diff, JSON) horizontally overflows the
//     container at 1280 / 1440 / 1920 viewport widths. Tolerance: 1px for
//     sub-pixel rendering noise — anything above is a real overflow.
//  3) All four artifact archetypes (table, code, diff, JSON) render; a
//     missing kind fails LOUDLY (no silent skips).
//  4) The `.is-nowrap` escape hatch is dead: no DOM node exposes it as a
//     surviving tool-output consumer. If a future change wires the class
//     back in, this stage fails.
//  5) Chat pane default width tracks the viewport (~45-50%) at
//     1280/1440/1920/2560 via pure CSS (no JS resize listener needed for
//     the default path); persists user drag; re-hydrates the persisted px
//     on reload. Sidebar mounts via `#/agents/<TICKET>` — a live orchestrator
//     entry in the fixture registry pops it open, and the sidebar-visible
//     guard asserts the pane is on-screen before we measure.
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

// Artifact archetypes emitted through the render_artifact MCP tool. Each
// tool_use registers a pending artifact; the matching tool_result closes it
// with `{"ok": true, "artifact_id": "..."}` — the transcript parser rebuilds
// the artifact from the tool input and emits a `kind: "artifact"` event.
const LONG_JSON_DATA = {
  url: LONG_URL,
  description:
    "Long JSON value with a very-very-long-token-that-would-otherwise-push-the-container-off-screen-until-the-wrap-sweep-lands ".repeat(2),
  ids: Array.from({ length: 6 }, (_, i) => `token-${i}-` + "x".repeat(80)),
};

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

// artifact_from_text validates the `id` as a canonical UUID; supply
// deterministic UUIDs so the fixture is reproducible.
const ARTIFACT_IDS = {
  table: "11111111-1111-4111-8111-111111111111",
  json: "22222222-2222-4222-8222-222222222222",
  diff: "33333333-3333-4333-8333-333333333333",
  code: "44444444-4444-4444-8444-444444444444",
};

function renderArtifactPair(callId, artifactId, second, kind, title, payload) {
  return [
    {
      type: "assistant",
      timestamp: ts(second),
      message: {
        role: "assistant",
        content: [
          {
            type: "tool_use",
            id: callId,
            name: "mcp__wiki-artifacts__render_artifact",
            input: { kind, payload, title },
          },
        ],
      },
    },
    toolResult(
      second + 1,
      callId,
      JSON.stringify({ ok: true, artifact_id: artifactId }),
    ),
  ];
}

const ARTIFACT_TABLE_CALLS = renderArtifactPair(
  "art_table",
  ARTIFACT_IDS.table,
  5,
  "table",
  "packages",
  {
    columns: [
      { key: "pkg", label: "Package", type: "string" },
      { key: "version", label: "Version", type: "string" },
      { key: "note", label: "Notes", type: "string" },
    ],
    rows: ARTIFACT_TABLE_ROWS.map((row) => [row.pkg, row.version, row.note]),
  },
);

const ARTIFACT_JSON_CALLS = renderArtifactPair(
  "art_json",
  ARTIFACT_IDS.json,
  7,
  "json",
  "trace",
  { json_data: LONG_JSON_DATA },
);

const ARTIFACT_DIFF_CALLS = renderArtifactPair(
  "art_diff",
  ARTIFACT_IDS.diff,
  9,
  "diff",
  "hot.md.diff",
  { source: ARTIFACT_DIFF },
);

const ARTIFACT_CODE_CALLS = renderArtifactPair(
  "art_code",
  ARTIFACT_IDS.code,
  11,
  "code",
  "curl-command.sh",
  { language: "bash", source: ARTIFACT_CODE_LINE, filename: "curl-command.sh" },
);

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
  ...ARTIFACT_TABLE_CALLS,
  ...ARTIFACT_JSON_CALLS,
  ...ARTIFACT_DIFF_CALLS,
  ...ARTIFACT_CODE_CALLS,
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
      // Elements that intentionally clip with text-overflow: ellipsis (tool
      // titles, inline result summaries) report scrollWidth > clientWidth by
      // design — the DOM keeps the full text for accessibility, the box shows
      // an ellipsis. That is NOT a horizontal-scroll defect.
      const style = getComputedStyle(node);
      if (
        style.textOverflow === "ellipsis"
        || style.overflowX === "hidden"
        || style.overflow === "hidden"
      ) {
        continue;
      }
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

      // HIGH-R3-2 field verification: Shiki tokens must render inside BOTH
      // delete and insert cells so the split-diff view keeps WIKI-249 syntax
      // highlighting parity with the unified diff. A regression that dropped
      // token wiring surfaces as zero token spans on either side.
      // Give the async Shiki highlighter time to resolve and re-render tokens
      // inside the split-diff cells (bundle load can take several seconds).
      await page.waitForFunction(() => {
        const del = document.querySelectorAll(".session-tool-split-diff .diff-code-delete span").length;
        const ins = document.querySelectorAll(".session-tool-split-diff .diff-code-insert span").length;
        return del > 0 && ins > 0;
      }, null, { timeout: 20_000 });
      const tokenCounts = await page.evaluate(() => ({
        del: document.querySelectorAll(".session-tool-split-diff .diff-code-delete span").length,
        ins: document.querySelectorAll(".session-tool-split-diff .diff-code-insert span").length,
        delColored: document.querySelectorAll(".session-tool-split-diff .diff-code-delete span[style*='color']").length,
        insColored: document.querySelectorAll(".session-tool-split-diff .diff-code-insert span[style*='color']").length,
      }));
      assert(
        tokenCounts.del > 0 && tokenCounts.ins > 0,
        `Shiki tokens must appear in both split-diff sides; saw delete=${tokenCounts.del} insert=${tokenCounts.ins}`,
      );
      logStep(`viewport ${viewport.width}: Shiki tokens present in split-diff (delete=${tokenCounts.del}, insert=${tokenCounts.ins}, coloredDel=${tokenCounts.delColored}, coloredIns=${tokenCounts.insColored})`);

      logStep(`viewport ${viewport.width}: sweep transcript for horizontal overflow (tolerance ${OVERFLOW_TOLERANCE_PX}px)`);
      const scrollCheck = await overflowingDescendants(page, ".session-scroll-inner");
      assert(!scrollCheck.rootMissing, "session-scroll-inner missing");
      assert(
        scrollCheck.offenders.length === 0,
        `expected no horizontal overflow inside the transcript, saw: ${JSON.stringify(scrollCheck.offenders, null, 2)}`,
      );

      // Require ALL FOUR artifact archetypes to render. A missing kind is a
      // regression in the renderer, not something to silently skip past.
      await page.waitForSelector(".artifact-block", { timeout: 15_000 });
      const artifactKinds = await page.evaluate(() => {
        const nodes = Array.from(document.querySelectorAll(".artifact-block"));
        return nodes.map((node) => node.getAttribute("data-artifact-kind") ?? node.className ?? "");
      });
      const REQUIRED_KINDS = ["table", "code", "diff", "json"];
      const missingKinds = REQUIRED_KINDS.filter(
        (kind) => !artifactKinds.some((entry) => entry.includes(kind)),
      );
      assert(
        missingKinds.length === 0,
        `artifact kinds must all render; missing: ${missingKinds.join(", ")}, saw: ${JSON.stringify(artifactKinds)}`,
      );
      logStep(`viewport ${viewport.width}: ${artifactKinds.length} artifact blocks rendered (${REQUIRED_KINDS.join(", ")} all present)`);
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

      logStep(`viewport ${viewport.width}: .is-nowrap has zero surviving consumers on tool output`);
      const nowrapCount = await page.evaluate(() => document.querySelectorAll(".is-nowrap").length);
      assert(
        nowrapCount === 0,
        `.is-nowrap escape hatch must be gone; found ${nowrapCount} nodes still wearing it`,
      );

      await context.close();
    }

    // HIGH-R3-4: chat-pane default width tracks the viewport at ~45-50% via
    // pure CSS at 1280/1440/1920/2560, plus resize / drag / reload survival.
    const widthChecks = [
      { width: 1280, height: 900 },
      { width: 1440, height: 900 },
      { width: 1920, height: 1080 },
      { width: 2560, height: 1440 },
    ];
    for (const viewport of widthChecks) {
      logStep(`sidebar width @ ${viewport.width}x${viewport.height}: fresh session, no persisted width`);
      const context = await browser.newContext({ viewport });
      const page = await context.newPage();
      await page.addInitScript(() => {
        localStorage.setItem("wiki-sidebar-visible", "false");
        localStorage.removeItem("wiki-session-sidebar-width");
        localStorage.removeItem("wiki-session-sidebar-width-custom");
      });
      await page.goto(`${backend.baseUrl}/#/agents`, { waitUntil: "domcontentloaded" });
      const sessionButton = page
        .locator(".agents-orch-actions .agent-log-toggle", { hasText: "session" })
        .first();
      await sessionButton.waitFor({ timeout: 30_000 });
      await sessionButton.click();
      const sidebar = page.locator(".session-sidebar").first();
      await sidebar.waitFor({ timeout: 15_000 });
      // Sidebar-visible guard: fail loudly if the pane never mounted.
      const sidebarVisible = await sidebar.isVisible();
      assert(sidebarVisible, `.session-sidebar must be visible before we measure at ${viewport.width}`);
      const measured = await sidebar.evaluate((node) => node.getBoundingClientRect().width);
      const ratio = measured / viewport.width;
      assert(
        ratio >= 0.44 && ratio <= 0.5,
        `default sidebar width at ${viewport.width} must sit at 45-50% of viewport; measured ${measured}px (${(ratio * 100).toFixed(1)}%)`,
      );
      const widthMode = await sidebar.getAttribute("data-width-mode");
      assert(widthMode === "responsive", `expected responsive width mode; saw ${widthMode}`);

      await context.close();
    }

    // Resize survival: fresh session at 1280, then resize to 1920 and confirm
    // the pane re-flowed via CSS (no JS resize listener needed in this branch).
    logStep(`sidebar width: resize survival (1280 -> 1920)`);
    {
      const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
      const page = await context.newPage();
      await page.addInitScript(() => {
        localStorage.setItem("wiki-sidebar-visible", "false");
        localStorage.removeItem("wiki-session-sidebar-width");
        localStorage.removeItem("wiki-session-sidebar-width-custom");
      });
      await page.goto(`${backend.baseUrl}/#/agents`, { waitUntil: "domcontentloaded" });
      const sessionButton = page.locator(".agents-orch-actions .agent-log-toggle", { hasText: "session" }).first();
      await sessionButton.waitFor({ timeout: 30_000 });
      await sessionButton.click();
      const sidebar = page.locator(".session-sidebar").first();
      await sidebar.waitFor({ timeout: 15_000 });
      const before = await sidebar.evaluate((node) => node.getBoundingClientRect().width);
      assert(before / 1280 >= 0.44 && before / 1280 <= 0.5, `1280 pre-resize ratio out of band: ${before}`);
      await page.setViewportSize({ width: 1920, height: 1080 });
      // Give the browser a beat to re-layout.
      await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
      const after = await sidebar.evaluate((node) => node.getBoundingClientRect().width);
      const afterRatio = after / 1920;
      assert(
        afterRatio >= 0.44 && afterRatio <= 0.5,
        `after resize to 1920, sidebar must still sit at 45-50% (proving CSS-relative default); measured ${after}px (${(afterRatio * 100).toFixed(1)}%)`,
      );
      assert(after > before, `wider viewport must yield wider pane; before=${before} after=${after}`);
      await context.close();
    }

    // Drag survival: drag the resize handle to a custom px, verify localStorage
    // captures the custom flag + width, then reload and verify the persisted
    // px is restored (data-width-mode="custom") instead of the CSS default.
    logStep(`sidebar width: drag -> persist -> reload -> restore`);
    {
      const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
      const page = await context.newPage();
      // Init script runs on every navigation (including reload) — gate it on
      // a marker key so the reset only wipes the sidebar width on FIRST boot.
      // After a drag persists the custom width, a reload preserves both.
      await page.addInitScript(() => {
        if (!localStorage.getItem("wiki-251-fixture-inited")) {
          localStorage.setItem("wiki-sidebar-visible", "false");
          localStorage.removeItem("wiki-session-sidebar-width");
          localStorage.removeItem("wiki-session-sidebar-width-custom");
          localStorage.setItem("wiki-251-fixture-inited", "1");
        }
      });
      await page.goto(`${backend.baseUrl}/#/agents`, { waitUntil: "domcontentloaded" });
      const sessionButton = page.locator(".agents-orch-actions .agent-log-toggle", { hasText: "session" }).first();
      await sessionButton.waitFor({ timeout: 30_000 });
      await sessionButton.click();
      const sidebar = page.locator(".session-sidebar").first();
      await sidebar.waitFor({ timeout: 15_000 });
      const handle = sidebar.locator(".session-resize").first();
      await handle.waitFor({ timeout: 5_000 });
      const handleBox = await handle.boundingBox();
      assert(handleBox, ".session-resize handle box must be measurable");
      // Drag the handle toward the left — the pane grows to the RIGHT edge of
      // the viewport, so a left-drag widens it. Target 900px width at 1440.
      const targetWidth = 900;
      const targetX = 1440 - targetWidth;
      const startX = handleBox.x + handleBox.width / 2;
      const startY = handleBox.y + handleBox.height / 2;
      await page.mouse.move(startX, startY);
      await page.mouse.down();
      await page.mouse.move(targetX, startY, { steps: 8 });
      await page.mouse.up();
      // Give React a frame to commit the drag-end state.
      await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
      const stored = await page.evaluate(() => ({
        width: localStorage.getItem("wiki-session-sidebar-width"),
        custom: localStorage.getItem("wiki-session-sidebar-width-custom"),
      }));
      assert(stored.custom === "1", `drag must set the custom flag; saw ${stored.custom}`);
      const storedPx = Number(stored.width);
      assert(storedPx > 0, `drag must persist a positive px width; saw ${stored.width}`);
      const afterMode = await sidebar.getAttribute("data-width-mode");
      assert(afterMode === "custom", `after drag data-width-mode must be "custom"; saw ${afterMode}`);
      const afterWidth = await sidebar.evaluate((node) => node.getBoundingClientRect().width);
      assert(
        Math.abs(afterWidth - storedPx) <= 3,
        `sidebar width after drag must match persisted px (${storedPx}); measured ${afterWidth}`,
      );

      // Reload and verify persisted custom width is honored (no CSS default).
      await page.reload({ waitUntil: "domcontentloaded" });
      const reopen = page.locator(".agents-orch-actions .agent-log-toggle", { hasText: "session" }).first();
      await reopen.waitFor({ timeout: 30_000 });
      await reopen.click();
      const sidebarAfterReload = page.locator(".session-sidebar").first();
      await sidebarAfterReload.waitFor({ timeout: 15_000 });
      const restoredMode = await sidebarAfterReload.getAttribute("data-width-mode");
      assert(restoredMode === "custom", `after reload the persisted custom flag must survive; saw ${restoredMode}`);
      const restoredWidth = await sidebarAfterReload.evaluate((node) => node.getBoundingClientRect().width);
      assert(
        Math.abs(restoredWidth - storedPx) <= 3,
        `after reload sidebar width must equal persisted ${storedPx}; measured ${restoredWidth}`,
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
