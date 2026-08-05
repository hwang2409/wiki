import fs from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";

import {
  codexAssistant,
  codexToolCall,
  codexToolOutput,
  codexUser,
  makeFixtureRoot,
  startBackend,
  writeQueue,
  writeRegistry,
} from "../scripts/wiki32-harness.mjs";

const TICKET = "WIKI-238";
const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || "/tmp/wiki-238-semantic-activity";
const SCREENSHOTS = {
  collapsedNormal: path.join(OUT_DIR, "collapsed-normal.png"),
  expandedNormal: path.join(OUT_DIR, "expanded-normal.png"),
  collapsedNarrow: path.join(OUT_DIR, "collapsed-narrow.png"),
  expandedNarrow: path.join(OUT_DIR, "expanded-narrow.png"),
};
const THEMES = [
  "mono-light",
  "opencode",
  "mono-dark",
  "gruvbox-dark",
  "gruvbox-light",
  "vscode-dark-plus",
  "solarized-dark",
  "solarized-light",
  "dracula",
  "nord",
  "one-dark",
  "tokyo-night",
  "catppuccin-mocha",
];
const ESSENTIAL_CONTRAST_ROLES = [
  ["state done", ".session-activity-state:not(.is-working):not(.is-failed):not(.is-interrupted):not(.is-waiting-for-you)"],
  ["state failed", ".session-activity-state.is-failed"],
  ["state interrupted", ".session-activity-state.is-interrupted"],
  ["state waiting", ".session-activity-state.is-waiting-for-you"],
  ["semantic summary", ".session-activity-semantic"],
  ["activity metadata", ".session-activity-meta"],
  ["timeline metadata", ".session-activity-row-meta"],
  ["reasoning", ".session-thinking"],
  ["tool summary", ".session-tool-summary"],
  ["result status", ".session-tool-result-state"],
  ["result correlation", ".session-tool-result-summary"],
  ["raw preview label", ".transcript-preview-label"],
  ["raw preview body", ".transcript-preview-body"],
  ["failure badge", ".session-tool-err"],
];
const WORKING_CONTRAST_ROLE = ["state working", ".session-activity-state.is-working"];

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function reasoning(text, timestamp) {
  return {
    timestamp,
    type: "response_item",
    payload: {
      type: "reasoning",
      id: `reasoning-${timestamp}`,
      summary: [{ text }],
    },
  };
}

function failedToolResult(callId, text, timestamp) {
  return {
    timestamp,
    type: "event_msg",
    payload: {
      type: "mcp_tool_call_end",
      call_id: callId,
      result: { Err: { content: [{ type: "text", text }] } },
    },
  };
}

function contrastRatio(foreground, background) {
  const channel = (value) => {
    const normalized = value / 255;
    return normalized <= 0.04045 ? normalized / 12.92 : ((normalized + 0.055) / 1.055) ** 2.4;
  };
  const luminance = (color) => 0.2126 * channel(color[0]) + 0.7152 * channel(color[1]) + 0.0722 * channel(color[2]);
  const light = Math.max(luminance(foreground), luminance(background));
  const dark = Math.min(luminance(foreground), luminance(background));
  return (light + 0.05) / (dark + 0.05);
}

async function colors(locator) {
  return locator.evaluate((element) => {
    const parse = (value) => {
      const values = (value.match(/[\d.]+/g) ?? []).map(Number);
      if (value.startsWith("color(srgb")) {
        return { rgb: values.slice(0, 3).map((channel) => channel * 255), alpha: values[3] ?? 1 };
      }
      return { rgb: values.slice(0, 3), alpha: values[3] ?? 1 };
    };
    let node = element;
    const layers = [];
    while (node instanceof Element) {
      const layer = parse(getComputedStyle(node).backgroundColor);
      if (layer.rgb.length === 3 && layer.alpha > 0) layers.push(layer);
      node = node.parentElement;
    }
    let background = [0, 0, 0];
    for (const layer of layers.reverse()) {
      background = layer.rgb.map((channel, index) => channel * layer.alpha + background[index] * (1 - layer.alpha));
    }
    return {
      background,
      foreground: parse(getComputedStyle(element).color).rgb,
    };
  });
}

async function main() {
  await fs.mkdir(OUT_DIR, { recursive: true });
  const fixtures = makeFixtureRoot("wiki-238-semantic-activity-");
  const transcript = path.join(fixtures.root, "semantic-activity.jsonl");
  const longReasoning = Array.from(
    { length: 18 },
    (_, index) => `Evidence line ${index + 1}: compare the parser event with the visible transcript before changing the hierarchy.`,
  ).join("\n");
  const rows = [
    codexUser("make model work readable without hiding evidence", "2026-08-02T12:00:00.000Z"),
    reasoning("I will inspect the transcript renderer and its tests.", "2026-08-02T12:00:01.000Z"),
    codexToolCall("call-read", "Read", '{"path":"frontend/src/session.tsx"}', "2026-08-02T12:00:02.000Z"),
    codexToolOutput("call-read", "activity group source exact", "2026-08-02T12:00:03.000Z"),
    reasoning(longReasoning, "2026-08-02T12:00:04.000Z"),
    codexToolCall("call-order-a", "Read", '{"path":"frontend/src/agent-events.ts"}', "2026-08-02T12:00:05.000Z"),
    codexToolCall("call-order-b", "exec_command", '{"cmd":"npm test -- order-b"}', "2026-08-02T12:00:06.000Z"),
    codexToolOutput("call-order-b", "order B finished", "2026-08-02T12:00:07.000Z"),
    codexToolOutput("call-order-a", "order A finished", "2026-08-02T12:00:08.000Z"),
    codexAssistant("The semantic map and hierarchy pass focused checks.", "2026-08-02T12:00:09.000Z"),
    codexToolCall("call-unknown", "mcp__private__launch_thing", '{"payload":"opaque"}', "2026-08-02T12:00:10.000Z"),
    codexToolOutput("call-unknown", "opaque result", "2026-08-02T12:00:11.000Z"),
    codexAssistant("Unknown tool meaning stays hidden.", "2026-08-02T12:00:12.000Z"),
    reasoning("The first validation attempt failed.", "2026-08-02T12:00:13.000Z"),
    codexToolCall("call-failed", "exec_command", '{"cmd":"npm test -- failing-case"}', "2026-08-02T12:00:14.000Z"),
    failedToolResult("call-failed", "1 test failed", "2026-08-02T12:00:16.000Z"),
    codexAssistant("The failed attempt remains visible.", "2026-08-02T12:00:17.000Z"),
    reasoning("The run needs approval before it can continue.", "2026-08-02T12:00:18.000Z"),
    codexToolCall("call-ask", "AskUserQuestion", '{"question":"Continue with the safe retry?"}', "2026-08-02T12:00:19.000Z"),
    codexAssistant("Approval stays visible as a separate state.", "2026-08-02T12:00:20.000Z"),
    reasoning("The first retry failed.", "2026-08-02T12:00:21.000Z"),
    codexToolCall("call-retry-failed", "exec_command", '{"cmd":"npm test -- retry-case"}', "2026-08-02T12:00:22.000Z"),
    failedToolResult("call-retry-failed", "retry input failed", "2026-08-02T12:00:24.000Z"),
    reasoning("The provider started a safe retry.", "2026-08-02T12:00:25.000Z"),
    codexToolCall("call-retry-success", "exec_command", '{"cmd":"npm test -- retry-case"}', "2026-08-02T12:00:26.000Z"),
    codexToolOutput("call-retry-success", "retry passed\nexited with code 0", "2026-08-02T12:00:28.000Z"),
    codexAssistant("The successful retry completed the turn.", "2026-08-02T12:00:29.000Z"),
    codexUser("start the next turn", "2026-08-02T12:00:30.000Z"),
  ];
  const writeTranscript = async (extraRows = []) => {
    await fs.writeFile(
      transcript,
      [...rows, ...extraRows].map((row) => JSON.stringify(row)).join("\n") + "\n",
    );
  };
  await writeTranscript();
  writeRegistry(fixtures.registryPath, [[TICKET, transcript]]);
  writeQueue(fixtures.queuePath, TICKET, []);

  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1200 } });
  let runtime = { providerState: "working", pendingRequestCount: 0, working: true };

  try {
    await page.route(`**/api/agents/${TICKET}/session**`, async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      body.working = runtime.working;
      body.provider_inspector = {
        run_id: "wiki-238-fixture",
        provider: "codex",
        state: runtime.providerState,
        raw_count: 0,
        normalized_count: 0,
        dispositions: body.dispositions,
        pending_requests: Array.from({ length: runtime.pendingRequestCount }, (_, index) => ({
          request_id: `approval-${index}`,
          request_kind: "approval",
          received_at: "2026-08-02T12:00:26.000Z",
          raw_seq: index,
          payload: { question: "Continue?" },
        })),
        events: [],
        raw: [],
      };
      await route.fulfill({
        status: response.status(),
        headers: response.headers(),
        contentType: "application/json",
        body: JSON.stringify(body),
      });
    });
    await page.addInitScript((ticket) => {
      localStorage.setItem("wiki-sidebar-visible", "false");
      localStorage.setItem("wiki-theme", "opencode");
      localStorage.setItem(
        "wiki-window-layout-v2",
        JSON.stringify({
          version: 2,
          activeWindowId: "window-0",
          windows: [{
            id: "window-0",
            focusedPaneId: "pane-1",
            layout: { kind: "pane", id: "pane-1", path: `agent://${ticket}` },
          }],
        }),
      );
    }, TICKET);
    await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".session-activity-head");
    await page.evaluate(() => document.fonts.ready);

    const groups = page.locator(".session-activity");
    assert((await groups.count()) === 5, `expected 5 activity groups, got ${await groups.count()}`);

    const assertPrimary = async (index, state, summary) => {
      const primary = groups.nth(index).locator(".session-activity-primary");
      const children = await primary.locator(":scope > span").allInnerTexts();
      assert(children[0] === state, `group ${index} state must render first: ${children.join(" / ")}`);
      assert(children[1] === summary, `group ${index} summary must follow state: ${children.join(" / ")}`);
    };
    const assertPrimaryState = async (index, state) => {
      const actual = await groups.nth(index).locator(".session-activity-state").innerText();
      assert(actual === state, `group ${index} state mismatch: expected ${state}, got ${actual}`);
    };
    await assertPrimary(0, "DONE", "running tests");
    await assertPrimary(1, "DONE", "1 tool call");
    await assertPrimary(2, "FAILED", "running tests");
    await assertPrimary(3, "WAITING FOR YOU", "asking for input");
    await assertPrimary(4, "DONE", "running tests");
    assert((await groups.nth(1).locator(".session-activity-semantic").innerText()) === "1 tool call",
      "unknown tool archetype must use count fallback");
    assert((await groups.nth(0).locator(".session-activity-meta").innerText()).endsWith("7s"),
      "elapsed metadata must include the final tool result time");
    assert((await groups.nth(2).locator(".session-activity-meta").innerText()).endsWith("3s"),
      "failed event-message results must retain their completion time");

    runtime = { providerState: "idle", pendingRequestCount: 0, working: false };
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector(".session-activity-head");
    await assertPrimary(4, "DONE", "running tests");
    assert((await page.locator(".session-turn-live-state").count()) === 0,
      "idle provider state must not render a current-turn placeholder");

    const assertLiveState = async (providerState, pendingRequestCount, working, expected) => {
      runtime = { providerState, pendingRequestCount, working };
      await page.reload({ waitUntil: "domcontentloaded" });
      await page.waitForSelector(".session-activity-head");
      await assertPrimary(4, "DONE", "running tests");
      const liveState = page.locator(".session-turn-live-state .session-activity-state");
      assert((await liveState.count()) === 1, "current turn must render one separate live-state placeholder");
      assert((await liveState.innerText()) === expected,
        `current turn state mismatch: expected ${expected}, got ${await liveState.innerText()}`);
    };
    await assertLiveState("waiting-approval", 1, true, "WAITING FOR YOU");
    await assertLiveState("working", 1, true, "WAITING FOR YOU");
    await assertLiveState("idle", 1, false, "WAITING FOR YOU");
    await assertLiveState("error", 0, false, "FAILED");
    await assertLiveState("blocked", 0, false, "FAILED");
    await assertLiveState("dead", 1, true, "FAILED");
    await assertLiveState("working", 0, true, "WORKING");

    const pendingInterruptedTool = codexToolCall(
      "call-interrupted-pending",
      "Read",
      '{"path":"frontend/src/agent-events.ts"}',
      "2026-08-02T12:00:31.000Z",
    );
    runtime = { providerState: "interrupted", pendingRequestCount: 0, working: false };
    await writeTranscript([pendingInterruptedTool]);
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector(".session-activity-head");
    await assertPrimaryState(5, "INTERRUPTED");

    const completedInterruptedTool = codexToolCall(
      "call-interrupted-completed",
      "Read",
      '{"path":"frontend/src/session.tsx"}',
      "2026-08-02T12:00:33.000Z",
    );
    const completedInterruptedResult = codexToolOutput(
      "call-interrupted-completed",
      "session source exact",
      "2026-08-02T12:00:34.000Z",
    );
    await writeTranscript([completedInterruptedTool, completedInterruptedResult]);
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector(".session-activity-head");
    await assertPrimaryState(5, "INTERRUPTED");

    await page.locator(".session-scroll").screenshot({ path: SCREENSHOTS.collapsedNormal });

    // WIKI-244: activity groups open by default and the label gutter is gone —
    // reading order is asserted from the flat row classes instead.
    await groups.nth(0).locator(".session-activity-collapsible.is-open .session-activity-row").first().waitFor();
    const firstKinds = await groups.nth(0).locator(".session-activity-body > .session-activity-row").evaluateAll(
      (rows) => rows.map((row) => (
        row.classList.contains("is-reasoning")
          ? "REASONING"
          : row.classList.contains("is-result")
            ? "RESULT"
            : "TOOL"
      )),
    );
    assert(JSON.stringify(firstKinds) === JSON.stringify([
      "REASONING", "TOOL", "RESULT", "REASONING", "TOOL", "TOOL", "RESULT", "RESULT",
    ]),
      `timeline reading order is wrong: ${firstKinds.join("/")}`);
    const resultOutputs = await groups.nth(0).locator(
      ".session-activity-row.is-result .transcript-preview-body",
    ).evaluateAll((elements) => elements.map((element) => element.textContent?.trim() ?? ""));
    assert(JSON.stringify(resultOutputs) === JSON.stringify([
      "activity group source exact", "order B finished", "order A finished",
    ]), `result completion order is wrong: ${resultOutputs.join(" / ")}`);
    const resultSummaries = await groups.nth(0).locator(".session-tool-result-summary").allInnerTexts();
    assert(JSON.stringify(resultSummaries) === JSON.stringify([
      "read session.tsx", "npm test -- order-b", "read agent-events.ts",
    ]), `result-to-call correlation order is wrong: ${resultSummaries.join(" / ")}`);

    const firstTool = groups.nth(0).locator(".session-tool").first();
    const firstToolId = await firstTool.getAttribute("data-tool-event-id");
    assert(firstToolId, "first tool must expose its event identity for result correlation");
    const firstResult = groups.nth(0).locator(
      `.session-activity-row.is-result[data-tool-event-id="${firstToolId}"]`,
    );
    // WIKI-244: no click needed — single-line raw input renders as an inline
    // dim detail on the call row; output is always visible in the result row.
    assert(
      (await firstTool.locator(".session-tool-detail").innerText()).trim()
        === "frontend/src/session.tsx",
      "tool row must retain exact raw input evidence inline",
    );
    assert(
      (await firstResult.locator(".transcript-preview-body").innerText()).trim()
        === "activity group source exact",
      "always-visible tool output must retain exact raw evidence",
    );
    assert((await firstResult.locator(".session-tool-result-state").innerText()).toLowerCase() === "completed",
      "ok=null results must render a neutral completed label");
    assert((await firstResult.locator(".session-activity-row-meta").innerText()) === "unknown",
      "ok=null results must not invent an ok outcome");

    const longThinking = groups.nth(0).locator(".session-thinking").nth(1);
    const reasoningStyle = await longThinking.evaluate((element) => {
      const style = getComputedStyle(element);
      return {
        fontFamily: style.fontFamily,
        fontSize: Number.parseFloat(style.fontSize),
        fontStyle: style.fontStyle,
        height: element.getBoundingClientRect().height,
      };
    });
    assert(reasoningStyle.fontStyle === "normal", `reasoning must not be italic: ${reasoningStyle.fontStyle}`);
    assert(reasoningStyle.fontSize >= 13.5, `reasoning text is too small: ${reasoningStyle.fontSize}`);
    assert(reasoningStyle.height > 200, `long reasoning did not remain readable: ${reasoningStyle.height}`);
    const assistantStyle = await page.locator(".session-assistant").first().evaluate((element) => {
      const style = getComputedStyle(element);
      return { family: style.fontFamily, size: Number.parseFloat(style.fontSize) };
    });
    const metadataStyle = await groups.nth(0).locator(".session-activity-meta").evaluate((element) => {
      const style = getComputedStyle(element);
      return { family: style.fontFamily, size: Number.parseFloat(style.fontSize) };
    });
    assert(assistantStyle.family !== metadataStyle.family,
      `assistant prose and metadata need distinct type roles: ${JSON.stringify({ assistantStyle, metadataStyle })}`);
    assert(assistantStyle.size > metadataStyle.size, "assistant prose must be larger than metadata");

    await page.mouse.move(0, 0);
    const contrastAudit = {};
    for (const theme of THEMES) {
      await page.evaluate((themeId) => {
        document.documentElement.dataset.theme = themeId;
      }, theme);
      const themeAudit = {};
      for (const [roleName, selector] of ESSENTIAL_CONTRAST_ROLES) {
        const roles = page.locator(selector);
        const count = await roles.count();
        assert(count > 0, `${theme} contrast role has no rendered sample: ${roleName} (${selector})`);
        const ratios = [];
        for (let index = 0; index < count; index += 1) {
          const role = roles.nth(index);
          const sample = await colors(role);
          const ratio = contrastRatio(sample.foreground, sample.background);
          ratios.push(ratio);
          assert(ratio >= 4.5,
            `${theme} ${roleName} contrast must be at least 4.5:1, got ${ratio.toFixed(2)} from ${JSON.stringify(sample)}`);
        }
        themeAudit[roleName] = Math.min(...ratios);
      }
      contrastAudit[theme] = themeAudit;
    }

    await writeTranscript();
    runtime = { providerState: "working", pendingRequestCount: 0, working: true };
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector(".session-turn-live-state .session-activity-state.is-working");
    const [workingRoleName, workingSelector] = WORKING_CONTRAST_ROLE;
    for (const theme of THEMES) {
      await page.evaluate((themeId) => {
        document.documentElement.dataset.theme = themeId;
      }, theme);
      const workingRole = page.locator(workingSelector);
      const sample = await colors(workingRole);
      const ratio = contrastRatio(sample.foreground, sample.background);
      assert(ratio >= 4.5,
        `${theme} ${workingRoleName} contrast must be at least 4.5:1, got ${ratio.toFixed(2)} from ${JSON.stringify(sample)}`);
      contrastAudit[theme][workingRoleName] = ratio;
    }
    await page.evaluate(() => { document.documentElement.dataset.theme = "opencode"; });
    const firstActivityHead = groups.nth(0).locator(".session-activity-head");
    if (await firstActivityHead.getAttribute("aria-expanded") !== "true") {
      await firstActivityHead.click();
    }
    await groups.nth(0).locator('.session-activity-head[aria-expanded="true"]').waitFor();

    const normalDensity = await groups.nth(0).evaluate((element) => ({
      clientWidth: element.clientWidth,
      scrollWidth: element.scrollWidth,
      rows: element.querySelectorAll(".session-activity-row").length,
    }));
    assert(normalDensity.scrollWidth <= normalDensity.clientWidth,
      `expanded normal group overflows: ${normalDensity.scrollWidth} > ${normalDensity.clientWidth}`);
    assert(normalDensity.rows === 8, `expected 8 timeline rows, got ${normalDensity.rows}`);
    await groups.nth(0).screenshot({ path: SCREENSHOTS.expandedNormal });

    await groups.nth(0).locator(".session-activity-head").click();
    await page.setViewportSize({ width: 910, height: 900 });
    await page.waitForTimeout(300);
    await page.screenshot({ path: SCREENSHOTS.collapsedNarrow });
    await page.setViewportSize({ width: 910, height: 1400 });
    await page.locator(".session-scroll").evaluate((element) => { element.scrollTop = 0; });
    await groups.nth(0).scrollIntoViewIfNeeded();
    await groups.nth(0).locator(".session-activity-head").click();
    await groups.nth(0).locator('.session-activity-head[aria-expanded="true"]').waitFor();
    await groups.nth(0).locator(".session-activity-collapsible.is-open .session-thinking").first().waitFor();
    await groups.nth(0).scrollIntoViewIfNeeded();
    const narrowDensity = await groups.nth(0).evaluate((element) => ({
      clientWidth: element.clientWidth,
      scrollWidth: element.scrollWidth,
    }));
    assert(narrowDensity.scrollWidth <= narrowDensity.clientWidth,
      `expanded narrow group overflows: ${narrowDensity.scrollWidth} > ${narrowDensity.clientWidth}`);
    // WIKI-244: tool rows are read-only text (no toggle), so only the
    // interactive activity head keeps the 40px hit-area requirement.
    const hitAreas = await groups.nth(0).locator(".session-activity-head").evaluateAll(
      (elements) => elements.map((element) => element.getBoundingClientRect().height),
    );
    assert(hitAreas.every((height) => height >= 40), `activity hit area below 40px: ${hitAreas.join(", ")}`);
    await groups.nth(0).screenshot({ path: SCREENSHOTS.expandedNarrow });

    await fs.writeFile(path.join(OUT_DIR, "audit.json"), JSON.stringify({
      screenshots: SCREENSHOTS,
      readingOrder: firstKinds,
      reasoningStyle,
      typeRoles: { assistant: assistantStyle, metadata: metadataStyle },
      minimumContrastByTheme: contrastAudit,
      density: { normal: normalDensity, narrow: narrowDensity },
      hitAreas,
    }, null, 2));
  } finally {
    await browser.close();
    await backend.stop();
  }
}

await main();
