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
  collapsedNormal: path.join(OUT_DIR, "overview-normal.png"),
  expandedNormal: path.join(OUT_DIR, "expanded-normal.png"),
  
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
  // WIKI-249: status words and the metadata chip left the row (state = color,
  // OpenCode index.tsx:1867-1874); the glyph column and inline result carry
  // the same scan roles now.
  ["inline result metadata", ".session-tool-inline-result"],
  ["reasoning", ".session-thinking"],
  ["tool summary", ".session-tool:not(.is-failed) .session-tool-summary"],
  ["tool glyph", ".session-tool:not(.is-failed) .session-tool-icon-text"],
  ["tool target", ".session-tool:not(.is-failed) .session-tool-target"],
  ["raw preview body", ".transcript-preview-body"],
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
    await page.waitForSelector('.session-virtual-row[data-row-kind="activity"] .session-activity-row');
    await page.evaluate(() => document.fonts.ready);

    const activityUnits = page.locator('.session-virtual-row[data-row-kind="activity"]');
    assert((await activityUnits.count()) === 14, `expected 14 per-event activity rows, got ${await activityUnits.count()}`);

    for (let index = 0; index < await activityUnits.count(); index += 1) {
      const unit = activityUnits.nth(index);
      assert(await unit.locator(":scope > .session-activity > .session-activity-row").count() === 1,
        `activity row ${index} must expose one direct per-event unit`);
      assert(await unit.locator(":scope > .session-activity-head, :scope > .session-activity-body").count() === 0,
        `activity row ${index} must not render aggregate chrome`);
    }

    runtime = { providerState: "idle", pendingRequestCount: 0, working: false };
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector('.session-virtual-row[data-row-kind="activity"] .session-activity-row');
    assert((await page.locator(".session-turn-live-state").count()) === 0,
      "idle provider state must not render a current-turn placeholder");

    const assertLiveState = async (providerState, pendingRequestCount, working, expected) => {
      runtime = { providerState, pendingRequestCount, working };
      await page.reload({ waitUntil: "domcontentloaded" });
      await page.waitForSelector('.session-virtual-row[data-row-kind="activity"] .session-activity-row');
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

    const composerRow = page.locator(".session-composer-row").first();
    const composerInput = page.locator(".session-composer textarea").first();
    const restingComposer = await composerRow.evaluate((element) => {
      const style = getComputedStyle(element);
      return { background: style.backgroundColor, border: style.borderLeftColor };
    });
    await composerInput.focus();
    const focusedComposer = await composerRow.evaluate((element) => {
      const style = getComputedStyle(element);
      return { background: style.backgroundColor, border: style.borderLeftColor };
    });
    assert(
      restingComposer.background !== focusedComposer.background || restingComposer.border !== focusedComposer.border,
      `focused composer must differ from resting composer: ${JSON.stringify({ restingComposer, focusedComposer })}`,
    );
    await page.emulateMedia({ forcedColors: "active" });
    const forcedColorsOutline = await composerInput.evaluate((element) => getComputedStyle(element).outlineStyle);
    assert(forcedColorsOutline !== "none", "forced-colors focus must retain a visible outline");
    await page.emulateMedia({ forcedColors: "none" });

    const pendingInterruptedTool = codexToolCall(
      "call-interrupted-pending",
      "Read",
      '{"path":"frontend/src/agent-events.ts"}',
      "2026-08-02T12:00:31.000Z",
    );
    runtime = { providerState: "interrupted", pendingRequestCount: 0, working: false };
    await writeTranscript([pendingInterruptedTool]);
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector('.session-virtual-row[data-row-kind="activity"] .session-activity-row');
    const pendingState = page.locator(".session-turn-live-state .session-activity-state");
    assert((await pendingState.count()) === 1 && (await pendingState.innerText()) === "INTERRUPTED",
      "pending interrupted runs must retain a quiet interrupted state");

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
    const interruptedAssistant = codexAssistant(
      "The interrupted boundary is an assistant row.",
      "2026-08-02T12:00:35.000Z",
    );
    await writeTranscript([completedInterruptedTool, completedInterruptedResult, interruptedAssistant]);
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector('.session-virtual-row[data-row-kind="activity"] .session-activity-row');
    const completedState = page.locator(".session-turn-live-state .session-activity-state");
    assert((await completedState.count()) === 1 && (await completedState.innerText()) === "INTERRUPTED",
      "completed interrupted runs must retain a quiet interrupted state");
    const liveRow = page.locator(".session-turn-live-state").locator(
      "xpath=ancestor::div[contains(@class, 'session-virtual-row')][1]",
    );
    assert(await liveRow.locator(".session-assistant").count() === 1,
      "interrupted state must attach to the final assistant row of the current turn");
    await page.locator(".session-scroll").screenshot({ path: SCREENSHOTS.collapsedNormal });
    await page.locator(".session-scroll").evaluate((element) => { element.scrollTop = 0; });
    await page.waitForTimeout(100);

    // WIKI-244: the trace is always visible (no collapse state exists) and
    // the label gutter is gone — reading order comes from flat row classes.
    const firstKinds = await activityUnits.evaluateAll(
      (units) => units.map((unit) => {
        const row = unit.querySelector(".session-activity-row");
        return (
        row.classList.contains("is-reasoning")
          ? "REASONING"
          : "TOOL"
        );
      }),
    );
    assert(JSON.stringify(firstKinds) === JSON.stringify([
      "REASONING", "TOOL", "REASONING", "TOOL", "TOOL", "TOOL", "REASONING",
      "TOOL", "REASONING", "TOOL", "REASONING", "TOOL", "REASONING", "TOOL", "TOOL",
    ]),
      `timeline reading order is wrong: ${firstKinds.join("/")}`);
    const toolRows = page.locator(".session-tool");
    const resultSummaries = (await page.locator(".session-tool-summary").allInnerTexts())
      .map((text) => text.replace(/\s+/g, " ").trim());
    const expectedSummaries = ["read session.tsx", "read agent-events.ts", "npm test -- order-b"];
    assert(JSON.stringify(resultSummaries.slice(0, expectedSummaries.length)) === JSON.stringify(expectedSummaries),
      `tool-to-output correlation order is wrong: ${resultSummaries.join(" / ")}`);

    const firstTool = toolRows.first();
    const firstToolId = await firstTool.getAttribute("data-tool-event-id");
    assert(firstToolId, "first tool must expose its event identity for result correlation");
    // WIKI-245: one unit holds the call, status, and output. Single-line raw
    // input stays inline, and output remains visible without a click.
    assert(
      (await firstTool.locator(".session-tool-detail").innerText()).trim()
        === "frontend/src/session.tsx",
      "tool row must retain exact raw input evidence inline",
    );
    const firstRawToggle = firstTool.locator(".session-tool-raw-toggle");
    assert(await firstRawToggle.count() === 1, "first per-event tool row must expose raw disclosure");
    await firstRawToggle.click();
    assert(
      (await firstTool.locator(".session-tool-raw .transcript-preview-body").innerText()).trim()
        === "activity group source exact",
      "raw disclosure must retain exact tool output on its owning per-event row",
    );
    const orderB = toolRows.nth(2);
    assert(
      (await orderB.locator(".session-tool-inline-result, .session-tool-body .transcript-preview-body").innerText()).trim()
        === "order B finished",
      "tool output must remain paired with the exact owning per-event row",
    );
    // WIKI-249: state is the row's color, never a word (OpenCode
    // index.tsx:1867-1874). The neutral completion stays for screen readers.
    assert((await firstTool.locator(".session-tool-status").count()) === 0,
      "status words must not render in the visual row");
    assert(/\bis-completed\b/.test(await firstTool.getAttribute("class") ?? ""),
      "ok=null tools must carry the neutral completed state class");
    assert((await firstTool.locator(".sr-only").innerText()).trim().toLowerCase() === "completed",
      "ok=null completion must stay exposed to screen readers");

    const longThinkingHead = page.locator(".session-thinking-head").nth(1);
    assert(await longThinkingHead.getAttribute("aria-expanded") === "false",
      "thinking should start as one quiet collapsed row");
    await longThinkingHead.click();
    const longThinking = page.locator(".session-thinking").first();
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
    assert(reasoningStyle.height > 200, `expanded reasoning did not remain readable: ${reasoningStyle.height}`);
    // WIKI-245: thought rows start collapsed, then expose the full body.
    assert((await page.locator(".session-thinking .stream-clamp, .session-thinking .stream-clamp-toggle").count()) === 0,
      "thinking must not render through an interactive clamp");
    const longThinkingText = await longThinking.innerText();
    assert(longThinkingText.includes("Evidence line 18"),
      "expanded reasoning must be fully readable after one click");
    const assistantStyle = await page.locator(".session-assistant").first().evaluate((element) => {
      const style = getComputedStyle(element);
      return { family: style.fontFamily, size: Number.parseFloat(style.fontSize) };
    });
    const metadataStyle = await page.locator(".session-tool-inline-result").first().evaluate((element) => {
      const style = getComputedStyle(element);
      return { family: style.fontFamily, size: Number.parseFloat(style.fontSize) };
    });
    assert(assistantStyle.family !== metadataStyle.family,
      `assistant prose and metadata need distinct type roles: ${JSON.stringify({ assistantStyle, metadataStyle })}`);
    assert(assistantStyle.size > metadataStyle.size, "assistant prose must be larger than metadata");

    await page.mouse.move(0, 0);
    await page.locator(".session-scroll").evaluate((element) => { element.scrollTop = element.scrollHeight; });
    await page.waitForTimeout(100);
    const rawToggle = page.locator(".session-tool-raw-toggle").last();
    if (await rawToggle.count()) {
      await rawToggle.click();
      await page.locator(".transcript-preview-body").last().waitFor();
    }
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
    // WIKI-244: the trace has no collapse control anywhere — assert nothing
    // interactive can hide it, then measure density on the always-open body.
    const hidingControls = await page.locator(
      ".session-activity-head button, button.session-activity-head",
    ).count();
    assert(hidingControls === 0, `activity group must expose no disclosure control, found ${hidingControls}`);

    const normalDensity = await page.locator(".session-scroll").evaluate((element) => ({
      clientWidth: element.clientWidth,
      scrollWidth: element.scrollWidth,
      rows: element.querySelectorAll('.session-virtual-row[data-row-kind="activity"] .session-activity-row').length,
    }));
    assert(normalDensity.scrollWidth <= normalDensity.clientWidth,
      `expanded normal group overflows: ${normalDensity.scrollWidth} > ${normalDensity.clientWidth}`);
    assert(normalDensity.rows === 14, `expected 14 per-event timeline rows in the base fixture, got ${normalDensity.rows}`);
    await page.locator(".session-scroll").screenshot({ path: SCREENSHOTS.expandedNormal });

    await page.setViewportSize({ width: 910, height: 1400 });
    await page.locator(".session-scroll").evaluate((element) => { element.scrollTop = 0; });
    await activityUnits.first().scrollIntoViewIfNeeded();
    await page.locator(".session-thinking-head").first().click();
    await page.locator(".session-thinking").first().waitFor();
    const narrowDensity = await page.locator(".session-scroll").evaluate((element) => ({
      clientWidth: element.clientWidth,
      scrollWidth: element.scrollWidth,
    }));
    assert(narrowDensity.scrollWidth <= narrowDensity.clientWidth,
      `expanded narrow group overflows: ${narrowDensity.scrollWidth} > ${narrowDensity.clientWidth}`);
    // WIKI-244: the trace has no interactive heads left; hit-area minimums
    // apply only to real controls (chips inside BoundedPreview keep theirs).
    await page.locator(".session-scroll").screenshot({ path: SCREENSHOTS.expandedNarrow });

    await fs.writeFile(path.join(OUT_DIR, "audit.json"), JSON.stringify({
      screenshots: SCREENSHOTS,
      readingOrder: firstKinds,
      reasoningStyle,
      typeRoles: { assistant: assistantStyle, metadata: metadataStyle },
      minimumContrastByTheme: contrastAudit,
      density: { normal: normalDensity, narrow: narrowDensity },
    }, null, 2));
  } finally {
    await browser.close();
    await backend.stop();
  }
}

await main();
