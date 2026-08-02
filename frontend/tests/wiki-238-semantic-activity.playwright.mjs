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
    codexToolOutput("call-read", "activity group source exact", "2026-08-02T12:00:04.000Z"),
    reasoning(longReasoning, "2026-08-02T12:00:05.000Z"),
    codexToolCall("call-test", "exec_command", '{"cmd":"npm test -- semantic-activity"}', "2026-08-02T12:00:06.000Z"),
    codexToolOutput("call-test", "6 tests passed", "2026-08-02T12:00:09.000Z"),
    codexAssistant("The semantic map and hierarchy pass focused checks.", "2026-08-02T12:00:10.000Z"),
    codexToolCall("call-unknown", "mcp__private__launch_thing", '{"payload":"opaque"}', "2026-08-02T12:00:11.000Z"),
    codexToolOutput("call-unknown", "opaque result", "2026-08-02T12:00:12.000Z"),
    codexAssistant("Unknown tool meaning stays hidden.", "2026-08-02T12:00:13.000Z"),
    reasoning("The first validation attempt failed.", "2026-08-02T12:00:14.000Z"),
    codexToolCall("call-failed", "exec_command", '{"cmd":"npm test -- failing-case"}', "2026-08-02T12:00:15.000Z"),
    failedToolResult("call-failed", "1 test failed", "2026-08-02T12:00:17.000Z"),
    codexAssistant("The failed attempt remains visible.", "2026-08-02T12:00:18.000Z"),
    reasoning("The run needs approval before it can continue.", "2026-08-02T12:00:19.000Z"),
    codexToolCall("call-ask", "AskUserQuestion", '{"question":"Continue with the safe retry?"}', "2026-08-02T12:00:20.000Z"),
    codexAssistant("Approval stays visible as a separate state.", "2026-08-02T12:00:21.000Z"),
    reasoning("The provider started a retry after the failed tool.", "2026-08-02T12:00:22.000Z"),
    codexToolCall("call-retry-failed", "exec_command", '{"cmd":"npm test -- retry-case"}', "2026-08-02T12:00:23.000Z"),
    failedToolResult("call-retry-failed", "retry input failed", "2026-08-02T12:00:25.000Z"),
  ];
  await fs.writeFile(transcript, rows.map((row) => JSON.stringify(row)).join("\n") + "\n");
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
    await assertPrimary(0, "DONE", "running tests");
    await assertPrimary(1, "DONE", "1 tool call");
    await assertPrimary(2, "FAILED", "running tests");
    await assertPrimary(3, "WAITING FOR YOU", "asking for input");
    await assertPrimary(4, "WORKING", "running tests");
    assert((await groups.nth(1).locator(".session-activity-semantic").innerText()) === "1 tool call",
      "unknown tool archetype must use count fallback");
    assert((await groups.nth(0).locator(".session-activity-meta").innerText()).endsWith("8s"),
      "elapsed metadata must include the final tool result time");

    const assertLiveState = async (providerState, pendingRequestCount, working, expected) => {
      runtime = { providerState, pendingRequestCount, working };
      await page.reload({ waitUntil: "domcontentloaded" });
      await page.waitForSelector(".session-activity-head");
      await assertPrimary(4, expected, "running tests");
    };
    await assertLiveState("waiting-approval", 1, true, "WAITING FOR YOU");
    await assertLiveState("working", 1, true, "WAITING FOR YOU");
    await assertLiveState("idle", 1, false, "WAITING FOR YOU");
    await assertLiveState("error", 0, false, "FAILED");
    await assertLiveState("blocked", 0, false, "FAILED");
    await assertLiveState("dead", 1, true, "FAILED");
    await assertLiveState("working", 0, true, "WORKING");
    await page.locator(".session-scroll").screenshot({ path: SCREENSHOTS.collapsedNormal });

    await groups.nth(0).locator(".session-activity-head").click();
    await groups.nth(0).locator(".session-activity-collapsible.is-open .session-activity-row-label").first().waitFor();
    const firstLabels = await groups.nth(0).locator(".session-activity-row-label").allInnerTexts();
    assert(JSON.stringify(firstLabels) === JSON.stringify([
      "REASONING", "TOOL", "RESULT", "REASONING", "TOOL", "RESULT",
    ]),
      `timeline reading order is wrong: ${firstLabels.join("/")}`);

    const firstTool = groups.nth(0).locator(".session-tool").first();
    await firstTool.locator(".session-tool-head").click();
    await firstTool.locator(".session-tool-input-collapsible.is-open .transcript-preview-body").waitFor();
    assert(
      (await firstTool.locator(".session-tool-input-collapsible .transcript-preview-body").innerText()).trim()
        === "frontend/src/session.tsx",
      "expanded tool input must retain exact raw evidence",
    );
    assert(
      (await firstTool.locator(".session-tool-collapsible .transcript-preview-body").innerText()).trim()
        === "activity group source exact",
      "expanded tool output must retain exact raw evidence",
    );

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

    const contrastAudit = {};
    for (const theme of THEMES) {
      await page.evaluate((themeId) => {
        document.documentElement.dataset.theme = themeId;
      }, theme);
      const roles = groups.locator([
        ".session-activity-state",
        ".session-activity-meta",
        ".session-activity-row-label",
        ".session-activity-row-meta",
        ".session-thinking",
      ].join(", "));
      const ratios = [];
      for (let index = 0; index < await roles.count(); index += 1) {
        const role = roles.nth(index);
        const sample = await colors(role);
        const ratio = contrastRatio(sample.foreground, sample.background);
        ratios.push(ratio);
        const roleName = await role.evaluate((element) => `${element.className}: ${element.textContent?.trim()}`);
        assert(ratio >= 4.5,
          `${theme} ${roleName} contrast must be at least 4.5:1, got ${ratio.toFixed(2)} from ${JSON.stringify(sample)}`);
      }
      contrastAudit[theme] = Math.min(...ratios);
    }
    await page.evaluate(() => { document.documentElement.dataset.theme = "opencode"; });

    const normalDensity = await groups.nth(0).evaluate((element) => ({
      clientWidth: element.clientWidth,
      scrollWidth: element.scrollWidth,
      rows: element.querySelectorAll(".session-activity-row").length,
    }));
    assert(normalDensity.scrollWidth <= normalDensity.clientWidth,
      `expanded normal group overflows: ${normalDensity.scrollWidth} > ${normalDensity.clientWidth}`);
    assert(normalDensity.rows === 6, `expected 6 timeline rows, got ${normalDensity.rows}`);
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
    const hitAreas = await groups.nth(0).locator(".session-activity-head, .session-tool-head").evaluateAll(
      (elements) => elements.map((element) => element.getBoundingClientRect().height),
    );
    assert(hitAreas.every((height) => height >= 40), `activity hit area below 40px: ${hitAreas.join(", ")}`);
    await groups.nth(0).screenshot({ path: SCREENSHOTS.expandedNarrow });

    await fs.writeFile(path.join(OUT_DIR, "audit.json"), JSON.stringify({
      screenshots: SCREENSHOTS,
      readingOrder: firstLabels,
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
