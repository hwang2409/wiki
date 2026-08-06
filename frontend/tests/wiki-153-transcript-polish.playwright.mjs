import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

import {
  defaultWindowLayout,
  makeFixtureRoot,
  startBackend,
  writeQueue,
  writeRegistry,
} from "../scripts/wiki32-harness.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || "/tmp/wiki-153-playwright-evidence";
const TICKET = "WIKI-153";

// WIKI-222: long fixtures must exceed STREAM_CLAMP_LINES (40) so the clip +
// expand affordance still engages under the generous shared threshold.
const LONG_INPUT_LINES = 22;
const LONG_OUTPUT_LINES = 48;

function logStep(message) {
  console.error(`[wiki-153] ${message}`);
}

function makeLongLines(count, prefix) {
  return Array.from({ length: count }, (_, index) => `${prefix} ${String(index + 1).padStart(2, "0")}`).join("\n");
}

const READ_INPUT = JSON.stringify({ path: "backend/app/agent_runtime/store.py" });
const FAILED_OUTPUT = [
  "Error: Traceback (most recent call last):",
  ...Array.from({ length: LONG_OUTPUT_LINES }, (_, index) => `  File \"store.py\", line ${index + 1}: raised failure ${index + 1}`),
  "IOError: fixture failure line",
].join("\n");

const ANSI_STDOUT = [
  "[32mOK[0m first line",
  ...Array.from({ length: 8 }, (_, index) => `[36mout[0m line ${index + 1}`),
  "final ok line",
].join("\n");

const ANSI_STDERR = [
  "[31mERR[0m opening file",
  "permission denied: /tmp/does-not-exist",
].join("\n");

const BASH_COMMAND_TEXT = [
  `ls -la /tmp/wiki-153-fixture \\`,
  ...Array.from({ length: 10 }, (_, index) => `  --flag-${index + 1}=value_${index + 1} \\`),
  `  && cat /tmp/wiki-153-fixture/large.log | head -${LONG_OUTPUT_LINES}`,
].join("\n");

const BASH_TOOL_INPUT_LINES = 48;
const BASH_TOOL_INPUT = [
  `#!/usr/bin/env bash`,
  `set -euo pipefail`,
  ...Array.from({ length: BASH_TOOL_INPUT_LINES }, (_, index) => `echo "wiki-153 tool step ${index + 1}"`),
  `exit 0`,
].join("\n");

const GH_PREVIEW_URL = "https://github.com/hwang2409/wiki/pull/122";
const GH_MIX_OUTPUT_TAIL_MARKER = "wiki-153 gh mix tail marker";
const GH_MIX_ANSI_HEAD_MARKER = "wiki-153 gh mix ansi head";
const GH_MIX_ANSI_TAIL_MARKER = "wiki-153 gh mix ansi tail";
const GH_MIX_OUTPUT = [
  `[32m${GH_MIX_ANSI_HEAD_MARKER}[0m`,
  `opened ${GH_PREVIEW_URL}`,
  ...Array.from({ length: LONG_OUTPUT_LINES }, (_, index) => `  gh-mix log line ${String(index + 1).padStart(2, "0")}`),
  `[31m${GH_MIX_ANSI_TAIL_MARKER}[0m`,
  GH_MIX_OUTPUT_TAIL_MARKER,
].join("\n");

const CLAUDE_TRANSCRIPT_ROWS = [
  { type: "mode", mode: "normal", sessionId: "fixture-wiki-153" },
  { type: "permission-mode", permissionMode: "bypassPermissions", sessionId: "fixture-wiki-153" },
  { type: "custom-title", customTitle: "WIKI-153 fixture", sessionId: "fixture-wiki-153" },
  { type: "agent-name", agentName: "wiki-153 worker", sessionId: "fixture-wiki-153" },
  {
    type: "system",
    subtype: "api_error",
    timestamp: "2026-07-22T18:00:00Z",
    error: { formatted: "529 Overloaded — provider throttled" },
    retryInMs: 900,
  },
  {
    type: "system",
    subtype: "stop_hook_summary",
    timestamp: "2026-07-22T18:00:01Z",
    hookCount: 1,
    level: "info",
    preventedContinuation: false,
  },
  {
    type: "assistant",
    timestamp: "2026-07-22T18:00:02Z",
    message: {
      role: "assistant",
      content: [
        {
          type: "tool_use",
          id: "toolu_read_fixture",
          name: "Read",
          input: {
            path: "backend/app/agent_runtime/store.py",
            preview: makeLongLines(LONG_INPUT_LINES, "line"),
          },
        },
      ],
    },
  },
  {
    type: "user",
    timestamp: "2026-07-22T18:00:03Z",
    message: {
      role: "user",
      content: [
        {
          type: "tool_result",
          tool_use_id: "toolu_read_fixture",
          content: FAILED_OUTPUT,
          is_error: true,
        },
      ],
    },
  },
  {
    type: "assistant",
    timestamp: "2026-07-22T18:00:03.500Z",
    message: {
      role: "assistant",
      content: [
        {
          type: "tool_use",
          id: "toolu_bash_fixture",
          name: "Bash",
          input: {
            command: BASH_TOOL_INPUT,
            description: "wiki-153 bash tool fixture",
          },
        },
      ],
    },
  },
  {
    type: "user",
    timestamp: "2026-07-22T18:00:03.700Z",
    message: {
      role: "user",
      content: [
        {
          type: "tool_result",
          tool_use_id: "toolu_bash_fixture",
          content: "wiki-153 bash tool ran ok",
        },
      ],
    },
  },
  {
    type: "assistant",
    timestamp: "2026-07-22T18:00:03.800Z",
    message: {
      role: "assistant",
      content: [
        {
          type: "tool_use",
          id: "toolu_bash_ghmix",
          name: "Bash",
          input: {
            command: "gh pr view 122 --json url",
            description: "wiki-153 gh mix fixture",
          },
        },
      ],
    },
  },
  {
    type: "user",
    timestamp: "2026-07-22T18:00:03.900Z",
    message: {
      role: "user",
      content: [
        {
          type: "tool_result",
          tool_use_id: "toolu_bash_ghmix",
          content: GH_MIX_OUTPUT,
        },
      ],
    },
  },
  {
    type: "user",
    timestamp: "2026-07-22T18:00:04Z",
    message: {
      role: "user",
      content: [
        {
          type: "text",
          text: `<bash-input>${BASH_COMMAND_TEXT}</bash-input><bash-stdout>${ANSI_STDOUT}</bash-stdout><bash-stderr>${ANSI_STDERR}</bash-stderr>`,
        },
      ],
    },
  },
  {
    type: "assistant",
    timestamp: "2026-07-22T18:00:04.100Z",
    message: {
      role: "assistant",
      content: [
        {
          type: "tool_use",
          id: "toolu_edit_font_fixture",
          name: "Edit",
          input: {
            file_path: "frontend/src/session.tsx",
            old_string: "const before = true;",
            new_string: "const after = true;",
          },
        },
      ],
    },
  },
  {
    type: "user",
    timestamp: "2026-07-22T18:00:04.200Z",
    message: {
      role: "user",
      content: [
        {
          type: "tool_result",
          tool_use_id: "toolu_edit_font_fixture",
          content: "Applied edit",
        },
      ],
    },
  },
];

const PROVIDER_PENDING_REQUEST = {
  request_id: 42,
  request_kind: "item/tool/requestUserInput",
  received_at: "2026-07-22T18:00:05Z",
  raw_seq: 123,
  payload: {
    params: {
      questions: [
        {
          id: "q1",
          question: "Which scope should we ship?",
          header: "Scope",
          options: [
            { label: "Full ticket" },
            { label: "First slice only" },
          ],
        },
      ],
    },
  },
};

async function writeJsonl(target, rows) {
  await fs.writeFile(target, rows.map((row) => JSON.stringify(row)).join("\n") + "\n");
}

async function expectVisibleText(page, selector, text) {
  const locator = page.locator(selector, { hasText: text }).first();
  await locator.waitFor({ state: "visible" });
  return locator;
}

async function main() {
  logStep("preparing fixture root");
  await fs.mkdir(OUT_DIR, { recursive: true });
  const fixtures = makeFixtureRoot("wiki-153-");
  const transcript = path.join(fixtures.root, "wiki-153-claude.jsonl");
  await writeJsonl(transcript, CLAUDE_TRANSCRIPT_ROWS);
  writeRegistry(fixtures.registryPath, [[TICKET, transcript]]);
  writeQueue(fixtures.queuePath, TICKET, []);

  logStep("starting isolated backend");
  const backend = await startBackend(fixtures);
  logStep(`backend live at ${backend.baseUrl}`);
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1600 },
    permissions: ["clipboard-read", "clipboard-write"],
  });
  const page = await context.newPage();

  // WIKI-252: prove no tool-output GitHub URL triggers the metadata backend.
  // The wiki-153 fixture only references GH_PREVIEW_URL from the gh-mix tool
  // output (no prose card renders it), so ANY hit on /api/gh/preview for that
  // URL means <GhPreviewCard> mounted somewhere it must not.
  const ghPreviewRequests = [];
  page.on("request", (req) => {
    const url = req.url();
    if (url.includes("/api/gh/preview")) ghPreviewRequests.push(url);
  });

  await page.route(`**/api/agents/${TICKET}/session*`, async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    const response = await route.fetch();
    const body = await response.json();
    if (!body.provider_inspector) {
      body.provider_inspector = {
        run_id: `fixture-run-${TICKET}`,
        provider: "claude",
        state: "waiting-approval",
        raw_count: 1,
        normalized_count: 0,
        dispositions: { rendered: 0, summarized: 0, ignored: 0, unknown: 0 },
        pending_requests: [],
        events: [],
      };
    }
    body.provider_inspector.state = "waiting-approval";
    body.provider_inspector.pending_requests = [PROVIDER_PENDING_REQUEST];
    body.provider_inspector.events = [
      ...(body.provider_inspector.events ?? []),
      {
        seq: 900,
        kind: "model_changed",
        normalized_at: "2026-07-22T18:00:06Z",
        payload: {
          to_model: "claude-opus-4-7",
          message: "model changed to claude-opus-4-7",
        },
      },
    ];
    await route.fulfill({
      status: response.status(),
      headers: response.headers(),
      contentType: "application/json",
      body: JSON.stringify(body),
    });
  });

  try {
    logStep("opening session");
    const layout = {
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
    void defaultWindowLayout;
    await page.addInitScript(({ storedLayout }) => {
      localStorage.setItem("wiki-window-layout-v2", JSON.stringify(storedLayout));
      localStorage.setItem("wiki-sidebar-visible", "false");
    }, { storedLayout: layout });
    await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".session-scroll");

    logStep("hook messages: whitelist vs hidden");
    await expectVisibleText(page, ".session-marker.is-error", "529 Overloaded");
    await expectVisibleText(page, ".session-marker.is-info", "permissions");
    await expectVisibleText(page, ".session-marker.is-info", "model changed to claude-opus-4-7");
    // Non-whitelisted markers render as visible info rows, never hidden
    // "run detail" chips (Henry prefers verbose tool-reference-style detail).
    const hiddenMarkers = await page.locator(".session-marker-hidden").count();
    if (hiddenMarkers !== 0) {
      throw new Error("non-whitelisted markers must render visibly, not as hidden run-detail chips");
    }
    await expectVisibleText(page, ".session-marker.is-info", "stop hook");

    logStep("tool call: expand activity, assert failed output body visible");
    // WIKI-244: activity groups and tool bodies are open by default.
    await page.locator(".session-activity > .session-activity-row").first().waitFor({ state: "visible" });
    const failedTool = page.locator(".session-tool.is-failed").first();
    await failedTool.waitFor({ state: "visible" });
    // WIKI-253: long failed tool outputs now render through the block path
    // (the render-layer collapse gate promotes any long tool output to a
    // block-shaped renderer), but error-tone output is excluded from the
    // collapse — the body reads directly, no peek row hides a broken run.
    const failedBody = failedTool.locator(".session-tool-body, .session-tool-inline-result").first();
    await failedBody.waitFor({ state: "visible" });
    const failedText = await failedBody.innerText();
    if (!failedText.includes("IOError: fixture failure line")) {
      throw new Error("expanded failed tool should render its semantic error body");
    }
    if ((await failedTool.locator(".session-tool-output-peek").count()) !== 0) {
      throw new Error("WIKI-253: error-tone output must never collapse behind a peek row");
    }

    logStep("bash tool call: one block with a muted title and output");
    const bashTool = page.locator(".session-tool", { has: page.locator(".session-tool-summary", { hasText: /wiki-153 tool step/ }) }).first();
    await bashTool.waitFor({ state: "visible" });
    const bashToolBlock = bashTool.locator(".session-tool-block-body").first();
    await bashToolBlock.waitFor({ state: "visible" });
    await bashToolBlock.locator(".session-tool-block-title", { hasText: "$" }).waitFor({ state: "visible" });
    await bashToolBlock.locator(".session-tool-block-title.is-command").waitFor({ state: "visible" });
    if ((await bashToolBlock.locator(".transcript-preview-summary, .transcript-chip").count()) !== 0) {
      throw new Error("tier-2 bash blocks must not show byte summaries or persistent controls");
    }

    logStep("gh-preview mixed with long output: peek row hides body, one click reveals it");
    const ghMixTool = page.locator(".session-tool", { has: page.locator(".session-tool-summary", { hasText: /gh pr view 122/ }) }).first();
    await ghMixTool.waitFor({ state: "visible" });
    // WIKI-253: long tool outputs collapse behind a peek row by default. The
    // body is not in the DOM before expansion; the peek row is the affordance.
    const ghMixPeek = ghMixTool.locator(".session-tool-output-peek").first();
    await ghMixPeek.waitFor({ state: "visible" });
    // Expand once so the body renders (we still need to assert its content
    // matches the anchor / no-card / no-fetch invariants below).
    await ghMixPeek.click();
    const ghMixOutput = ghMixTool.locator(".session-tool-block-preview").first();
    await ghMixOutput.waitFor({ state: "visible" });
    const ghMixBody = ghMixOutput.locator(".transcript-preview-body.is-custom").first();
    await ghMixBody.waitFor({ state: "visible" });
    // WIKI-252: tool output renders GitHub URLs as plain external-link anchors,
    // never as a metadata card unfurl (that stays on the prose surface). We
    // check three things so the assertion is mutation-sensitive at every layer:
    //   1) the plain anchor is present with the exact source href;
    //   2) no GhPreviewCard DOM node exists for the tool-output URL;
    //   3) no backend metadata fetch was made for the tool-output URL. The
    //      pending-fetch fallback of GhPreviewCard renders the SAME anchor, so
    //      the network-call check is what distinguishes card from plain path.
    const ghMixAnchor = ghMixOutput
      .locator(`a.external-link[href='${GH_PREVIEW_URL}']`)
      .first();
    await ghMixAnchor.waitFor({ state: "visible" });
    if ((await ghMixAnchor.getAttribute("target")) !== "_blank") {
      throw new Error("WIKI-252: tool-output GitHub anchor must open in new tab (target='_blank')");
    }
    const ghMixAnchorRel = (await ghMixAnchor.getAttribute("rel")) ?? "";
    if (!/\bnoopener\b/.test(ghMixAnchorRel) || !/\bnoreferrer\b/.test(ghMixAnchorRel)) {
      throw new Error("WIKI-252: tool-output GitHub anchor must set rel='noopener noreferrer'");
    }
    if ((await ghMixOutput.locator(`a.gh-preview-card[href='${GH_PREVIEW_URL}']`).count()) !== 0) {
      throw new Error("WIKI-252: tool output must not render a gh-preview-card unfurl");
    }
    const encodedPreviewUrl = encodeURIComponent(GH_PREVIEW_URL);
    const toolPreviewFetches = ghPreviewRequests.filter(
      (u) => u.includes(encodedPreviewUrl) || u.includes(GH_PREVIEW_URL)
    );
    if (toolPreviewFetches.length !== 0) {
      throw new Error(
        `WIKI-252: tool output triggered ${toolPreviewFetches.length} /api/gh/preview fetch(es) for ${GH_PREVIEW_URL} — must be zero (that fetch is prose-surface only)`
      );
    }
    const ghMixBodyText = await ghMixBody.innerText();
    if (ghMixBodyText.includes("\x1b[")) {
      throw new Error("gh-preview text segments must strip ANSI escapes via renderAnsi, not render them raw");
    }
    if (!ghMixBodyText.includes(GH_MIX_ANSI_HEAD_MARKER)) {
      throw new Error("gh-preview mixed output should include the ANSI head marker text");
    }
    if (!ghMixBodyText.includes(GH_MIX_OUTPUT_TAIL_MARKER)) {
      throw new Error("expanded gh-preview mixed output should include the tail marker");
    }
    if (!ghMixBodyText.includes(GH_MIX_ANSI_TAIL_MARKER)) {
      throw new Error("expanded gh-preview mixed output should include the ANSI tail marker text");
    }
    await ghMixBody.locator(".session-tool-output-text .ansi-fg-2").first().waitFor({ state: "visible" });
    await ghMixBody.locator(".session-tool-output-text .ansi-fg-1").first().waitFor({ state: "visible" });
    // WIKI-253: the "collapse output" affordance appears when the block is
    // expanded past the height gate; clicking it returns to the peek row.
    await ghMixTool.locator(".session-tool-output-collapse").click();
    await ghMixTool.locator(".session-tool-output-peek").first().waitFor({ state: "visible" });

    logStep("bash block has no persistent wrap or copy chrome");

    logStep("bash block: one title and one capped output body");
    const bashBlock = page.locator(".session-bash").first();
    await bashBlock.waitFor({ state: "visible" });
    await bashBlock.locator(".session-tool-block-title", { hasText: "$" }).waitFor({ state: "visible" });
    await bashBlock.locator(".session-tool-block-title.is-command").waitFor({ state: "visible" });
    const bashOutput = bashBlock.locator(".session-tool-block-preview").first();
    await bashOutput.waitFor({ state: "visible" });
    if ((await bashBlock.locator(".transcript-preview-summary, .transcript-chip").count()) !== 0) {
      throw new Error("bash blocks must have no byte summary or persistent controls");
    }
    await bashOutput.locator(".ansi-fg-2, .ansi-fg-6").first().waitFor({ state: "visible" });
    await bashOutput.locator(".transcript-preview-more").click();
    await bashOutput.locator(".ansi-fg-1").first().waitFor({ state: "visible" });

    logStep("diff body keeps the monospace root font at runtime");
    const diffLine = page.locator(".session-tool-diff-body .diff-line").first();
    await diffLine.waitFor({ state: "visible" });
    const diffFont = await diffLine.evaluate((element) => {
      const root = getComputedStyle(document.documentElement);
      return {
        line: getComputedStyle(element).fontFamily,
        root: root.getPropertyValue("--font-monospace").trim(),
      };
    });
    if (diffFont.line !== diffFont.root) {
      throw new Error(`rendered diff lines must use the monospace root, got ${diffFont.line} instead of ${diffFont.root}`);
    }

    logStep("action-required card: question visible, kind hidden");
    // WIKI-152: Action required is promoted to the top-level chrome; there is
    // no inspector toggle to open first, and the "Action required" label
    // lives on the wrapping panel head, not the individual request card.
    const actionPanel = page.locator('[data-testid="session-action-required"]').first();
    await actionPanel.waitFor({ state: "visible" });
    await actionPanel.locator(".session-action-required-title", { hasText: "Action required" }).waitFor();
    const providerCard = actionPanel.locator(".session-provider-request").first();
    await providerCard.waitFor({ state: "visible" });
    // R1-04: the per-request Details disclosure is gone. Run details is now
    // the single diagnostics home; the pending card must not render kind,
    // request id, or raw payload — those live in Run details' event log
    // cross-referenced by raw_seq.
    if ((await providerCard.locator(".session-provider-request-details").count()) !== 0) {
      throw new Error("R1-04: per-card Details disclosure must be removed — Run details is the diagnostics home");
    }
    const cardText = await providerCard.innerText();
    for (const forbidden of ["item/tool/requestUserInput", "kind", "request id"]) {
      if (cardText.toLowerCase().includes(forbidden.toLowerCase())) {
        throw new Error(`R1-04: pending card must not leak "${forbidden}" (got: ${cardText})`);
      }
    }
    await providerCard.locator(".session-provider-question", { hasText: "Which scope" }).waitFor({ state: "visible" });

    logStep("capturing screenshot");
    await page.screenshot({ path: path.join(OUT_DIR, "wiki-153-transcript-polish.png"), fullPage: true });
    logStep("verification complete");
  } finally {
    await page.close();
    await context.close();
    await browser.close();
    await backend.stop();
  }
}

await main();
