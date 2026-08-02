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
    codexToolOutput("call-read", "activity group source", "2026-08-02T12:00:04.000Z"),
    codexAssistant("The current group leads with counts instead of meaning.", "2026-08-02T12:00:05.000Z"),
    reasoning(longReasoning, "2026-08-02T12:00:06.000Z"),
    codexToolCall("call-test", "exec_command", '{"cmd":"npm test -- semantic-activity"}', "2026-08-02T12:00:07.000Z"),
    codexToolOutput("call-test", "5 tests passed", "2026-08-02T12:00:09.000Z"),
    codexAssistant("The semantic map and hierarchy pass focused checks.", "2026-08-02T12:00:10.000Z"),
    codexToolCall("call-unknown", "mcp__private__launch_thing", '{"payload":"opaque"}', "2026-08-02T12:00:11.000Z"),
    codexToolOutput("call-unknown", "opaque result", "2026-08-02T12:00:12.000Z"),
  ];
  await fs.writeFile(transcript, rows.map((row) => JSON.stringify(row)).join("\n") + "\n");
  writeRegistry(fixtures.registryPath, [[TICKET, transcript]]);
  writeQueue(fixtures.queuePath, TICKET, []);

  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1200 } });

  try {
    await page.route(`**/api/agents/${TICKET}/session**`, async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      body.working = true;
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
    assert((await groups.count()) === 3, `expected 3 activity groups, got ${await groups.count()}`);
    await groups.nth(0).getByText("reading session.tsx", { exact: true }).waitFor();
    await groups.nth(1).getByText("running tests", { exact: true }).waitFor();
    await groups.nth(2).getByText("1 tool call", { exact: true }).waitFor();
    assert((await groups.nth(2).locator(".session-activity-semantic").innerText()) === "1 tool call",
      "unknown tool archetype must use count fallback");
    assert((await groups.nth(2).locator(".session-activity-state").innerText()) === "WORKING",
      "latest activity must show working before counts");
    await page.locator(".session-scroll").screenshot({ path: SCREENSHOTS.collapsedNormal });

    await groups.nth(0).locator(".session-activity-head").click();
    await groups.nth(1).locator(".session-activity-head").evaluate((element) => element.click());
    await groups.nth(1).locator('.session-activity-head[aria-expanded="true"]').waitFor();
    await groups.nth(0).locator(".session-activity-collapsible.is-open .session-activity-row-label").first().waitFor();
    const firstLabels = await groups.nth(0).locator(".session-activity-row-label").allInnerTexts();
    assert(JSON.stringify(firstLabels) === JSON.stringify(["REASONING", "TOOL", "RESULT"]),
      `timeline reading order is wrong: ${firstLabels.join("/")}`);
    const longThinking = groups.nth(1).locator(".session-thinking");
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

    for (const [name, locator] of [
      ["activity metadata", groups.nth(0).locator(".session-activity-meta")],
      ["timeline label", groups.nth(0).locator(".session-activity-row-label").first()],
      ["reasoning", groups.nth(0).locator(".session-thinking")],
    ]) {
      const sample = await colors(locator);
      const ratio = contrastRatio(sample.foreground, sample.background);
      assert(ratio >= 4.5,
        `${name} contrast must be at least 4.5:1, got ${ratio.toFixed(2)} from ${JSON.stringify(sample)}`);
    }
    const normalDensity = await groups.nth(1).evaluate((element) => ({
      clientWidth: element.clientWidth,
      scrollWidth: element.scrollWidth,
      rows: element.querySelectorAll(".session-activity-row").length,
    }));
    assert(normalDensity.scrollWidth <= normalDensity.clientWidth,
      `expanded normal group overflows: ${normalDensity.scrollWidth} > ${normalDensity.clientWidth}`);
    assert(normalDensity.rows === 3, `expected 3 timeline rows, got ${normalDensity.rows}`);
    await groups.nth(1).screenshot({ path: SCREENSHOTS.expandedNormal });

    await groups.nth(0).locator(".session-activity-head").click();
    await groups.nth(1).locator(".session-activity-head").click();
    await page.setViewportSize({ width: 910, height: 900 });
    await page.waitForTimeout(300);
    await page.screenshot({ path: SCREENSHOTS.collapsedNarrow });
    await groups.nth(1).locator(".session-activity-head").evaluate((element) => element.click());
    await groups.nth(1).locator('.session-activity-head[aria-expanded="true"]').waitFor();
    await groups.nth(1).locator(".session-activity-collapsible.is-open .session-thinking").waitFor();
    const narrowDensity = await groups.nth(1).evaluate((element) => ({
      clientWidth: element.clientWidth,
      scrollWidth: element.scrollWidth,
    }));
    assert(narrowDensity.scrollWidth <= narrowDensity.clientWidth,
      `expanded narrow group overflows: ${narrowDensity.scrollWidth} > ${narrowDensity.clientWidth}`);
    const hitAreas = await groups.nth(1).locator(".session-activity-head, .session-tool-head").evaluateAll(
      (elements) => elements.map((element) => element.getBoundingClientRect().height),
    );
    assert(hitAreas.every((height) => height >= 40), `activity hit area below 40px: ${hitAreas.join(", ")}`);
    await groups.nth(1).screenshot({ path: SCREENSHOTS.expandedNarrow });

    await fs.writeFile(path.join(OUT_DIR, "audit.json"), JSON.stringify({
      screenshots: SCREENSHOTS,
      readingOrder: firstLabels,
      reasoningStyle,
      typeRoles: { assistant: assistantStyle, metadata: metadataStyle },
      density: { normal: normalDensity, narrow: narrowDensity },
      hitAreas,
    }, null, 2));
  } finally {
    await browser.close();
    await backend.stop();
  }
}

await main();
