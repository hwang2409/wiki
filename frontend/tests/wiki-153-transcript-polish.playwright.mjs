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

const LONG_INPUT_LINES = 22;
const LONG_OUTPUT_LINES = 30;

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

const BASH_TOOL_INPUT_LINES = 34;
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

    logStep("tool call: expand activity, assert failed + bounded preview");
    const activityToggle = page.locator(".session-activity-head").first();
    await activityToggle.waitFor({ state: "visible" });
    await activityToggle.click();
    const failedTool = page.locator(".session-tool", { has: page.locator(".session-tool-err", { hasText: "failed" }) }).first();
    await failedTool.waitFor({ state: "visible" });
    await failedTool.locator(".session-tool-head").click();

    const outputPreview = failedTool.locator(".transcript-preview.is-error").first();
    await outputPreview.waitFor({ state: "visible" });
    await outputPreview.locator(".transcript-preview-label", { hasText: "output" }).waitFor();
    const summary = await outputPreview.locator(".transcript-preview-summary").first().innerText();
    if (!/\d+ lines · /.test(summary)) throw new Error(`summary label malformed: ${summary}`);
    await outputPreview.locator(".transcript-preview-more").waitFor({ state: "visible" });
    const bodyBefore = await outputPreview.locator(".transcript-preview-body").innerText();
    if (bodyBefore.includes(`IOError: fixture failure line`)) {
      throw new Error("bounded preview should hide the final line before expand");
    }
    await outputPreview.locator(".transcript-chip", { hasText: "expand" }).click();
    await outputPreview.locator(".transcript-chip", { hasText: "collapse" }).waitFor({ state: "visible" });
    const bodyAfter = await outputPreview.locator(".transcript-preview-body").innerText();
    if (!bodyAfter.includes("IOError: fixture failure line")) {
      throw new Error("expanded preview should include the final line");
    }
    await outputPreview.locator(".transcript-chip", { hasText: "copy" }).click();
    await outputPreview.locator(".transcript-chip", { hasText: "copied" }).waitFor({ state: "visible" });

    logStep("bash tool call: input routed through BoundedPreview + shiki");
    const bashTool = page.locator(".session-tool", { has: page.locator(".session-tool-summary", { hasText: /wiki-153 tool step/ }) }).first();
    await bashTool.waitFor({ state: "visible" });
    await bashTool.locator(".session-tool-head").click();
    const bashToolInput = bashTool.locator(".transcript-preview", { has: page.locator(".transcript-preview-label", { hasText: "input" }) }).first();
    await bashToolInput.waitFor({ state: "visible" });
    await bashToolInput.locator(".shiki-block[data-lang='bash']").waitFor({ state: "visible" });
    const bashToolSummary = await bashToolInput.locator(".transcript-preview-summary").first().innerText();
    if (!/\d+ lines · /.test(bashToolSummary)) throw new Error(`bash tool input summary malformed: ${bashToolSummary}`);
    await bashToolInput.locator(".transcript-chip", { hasText: "copy" }).waitFor({ state: "visible" });
    await bashToolInput.locator(".transcript-chip", { hasText: "expand" }).waitFor({ state: "visible" });

    logStep("gh-preview mixed with long output: clip bounds output, expand reveals tail");
    const ghMixTool = page.locator(".session-tool", { has: page.locator(".session-tool-summary", { hasText: /gh pr view 122/ }) }).first();
    await ghMixTool.waitFor({ state: "visible" });
    await ghMixTool.locator(".session-tool-head").click();
    const ghMixOutput = ghMixTool.locator(".transcript-preview", { has: page.locator(".transcript-preview-label", { hasText: "output" }) }).first();
    await ghMixOutput.waitFor({ state: "visible" });
    const ghMixBody = ghMixOutput.locator(".transcript-preview-body.is-custom").first();
    await ghMixBody.waitFor({ state: "visible" });
    await ghMixOutput.locator(".transcript-preview-more").waitFor({ state: "visible" });
    const ghMixBodyBefore = await ghMixBody.innerText();
    if (ghMixBodyBefore.includes(GH_MIX_OUTPUT_TAIL_MARKER)) {
      throw new Error("gh-preview mixed output should hide tail marker before expand — renderBody must clip via BoundedPreview text");
    }
    await ghMixOutput.locator(`a.external-link[href='${GH_PREVIEW_URL}'], a.gh-preview-card[href='${GH_PREVIEW_URL}']`).first().waitFor({ state: "visible" });
    await ghMixOutput.locator(".transcript-chip", { hasText: /^(nowrap|wrap)$/ }).waitFor({ state: "visible" });
    if (ghMixBodyBefore.includes("\x1b[")) {
      throw new Error("gh-preview text segments must strip ANSI escapes via renderAnsi, not render them raw");
    }
    if (!ghMixBodyBefore.includes(GH_MIX_ANSI_HEAD_MARKER)) {
      throw new Error("gh-preview mixed output should include the ANSI head marker text");
    }
    await ghMixBody.locator(".session-tool-output-text .ansi-fg-2").first().waitFor({ state: "visible" });
    await ghMixOutput.locator(".transcript-chip", { hasText: "expand" }).click();
    await ghMixOutput.locator(".transcript-chip", { hasText: "collapse" }).waitFor({ state: "visible" });
    const ghMixBodyAfter = await ghMixBody.innerText();
    if (!ghMixBodyAfter.includes(GH_MIX_OUTPUT_TAIL_MARKER)) {
      throw new Error("expanded gh-preview mixed output should include the tail marker");
    }
    if (ghMixBodyAfter.includes("\x1b[")) {
      throw new Error("expanded gh-preview text segments must strip ANSI escapes via renderAnsi");
    }
    if (!ghMixBodyAfter.includes(GH_MIX_ANSI_TAIL_MARKER)) {
      throw new Error("expanded gh-preview mixed output should include the ANSI tail marker text");
    }
    await ghMixBody.locator(".session-tool-output-text .ansi-fg-1").first().waitFor({ state: "visible" });
    await ghMixOutput.locator(".transcript-chip", { hasText: "collapse" }).click();

    logStep("custom-body wrap chip: available on bash tool input");
    await bashToolInput.locator(".transcript-chip", { hasText: /^(nowrap|wrap)$/ }).waitFor({ state: "visible" });

    logStep("bash block: three labelled sections, ansi preserved");
    const bashBlock = page.locator(".session-bash").first();
    await bashBlock.waitFor({ state: "visible" });
    const commandSection = bashBlock.locator(".transcript-preview", { has: page.locator(".transcript-preview-label", { hasText: "command" }) }).first();
    await commandSection.waitFor({ state: "visible" });
    await commandSection.locator(".session-bash-command .shiki-block[data-lang='bash']").waitFor({ state: "visible" });
    const commandSummary = await commandSection.locator(".transcript-preview-summary").first().innerText();
    if (!/\d+ lines · /.test(commandSummary)) throw new Error(`bash command summary malformed: ${commandSummary}`);
    await commandSection.locator(".transcript-chip", { hasText: "copy" }).waitFor({ state: "visible" });
    const outputSection = bashBlock.locator(".transcript-preview", { has: page.locator(".transcript-preview-label", { hasText: "output" }) }).first();
    await outputSection.waitFor({ state: "visible" });
    const errorSection = bashBlock.locator(".transcript-preview.is-error", { has: page.locator(".transcript-preview-label", { hasText: "error" }) }).first();
    await errorSection.waitFor({ state: "visible" });
    const bashSummary = await outputSection.locator(".transcript-preview-summary").first().innerText();
    if (!/\d+ lines · /.test(bashSummary)) throw new Error(`bash summary malformed: ${bashSummary}`);
    await outputSection.locator(".ansi-fg-2, .ansi-fg-6").first().waitFor({ state: "visible" });
    await errorSection.locator(".ansi-fg-1").first().waitFor({ state: "visible" });

    logStep("action-required card: question visible, kind hidden");
    const inspectorToggle = page.locator(".session-provider-inspector-head").first();
    await inspectorToggle.waitFor({ state: "visible" });
    const inspectorClass = await page.locator(".session-provider-inspector").first().getAttribute("class");
    if (!inspectorClass || !inspectorClass.includes("is-open")) {
      await inspectorToggle.click();
    }
    const providerCard = page.locator(".session-provider-request").first();
    await providerCard.waitFor({ state: "visible" });
    await providerCard.locator(".session-provider-request-head", { hasText: "Action required" }).waitFor();
    const kindVisibleDefault = await providerCard.locator(":scope > .session-provider-request-head", { hasText: "item/tool/requestUserInput" }).count();
    if (kindVisibleDefault !== 0) {
      throw new Error("request kind should be hidden by default");
    }
    await providerCard.locator(".session-provider-question", { hasText: "Which scope" }).waitFor({ state: "visible" });
    const detailsSummary = providerCard.locator(".session-provider-request-details > summary", { hasText: "Details" });
    await detailsSummary.waitFor({ state: "visible" });
    const preBeforeOpen = await providerCard.locator(".session-provider-request-details > pre").isVisible();
    if (preBeforeOpen) throw new Error("raw payload should be hidden before Details expanded");
    await detailsSummary.click();
    await providerCard.locator(".session-provider-request-meta dd", { hasText: "item/tool/requestUserInput" }).waitFor({ state: "visible" });
    await providerCard.locator(".session-provider-request-meta dd", { hasText: "42" }).waitFor({ state: "visible" });
    await providerCard.locator(".session-provider-request-details > pre").waitFor({ state: "visible" });

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
