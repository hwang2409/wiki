import fs from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";

import {
  codexAssistant,
  codexToolCall,
  codexToolOutput,
  makeFixtureRoot,
  startBackend,
  writeQueue,
  writeRegistry,
} from "../scripts/wiki32-harness.mjs";

const SCREENSHOT_PATH = process.env.WIKI_87_SCREENSHOT_PATH || "/tmp/wiki-87-after.png";
const LONG_JSON_LINE = JSON.stringify({ payload: "x".repeat(186) });
const LONG_TOOL_LINE = "T".repeat(200);

function logStep(message) {
  console.error(`[wiki-87-playwright] ${message}`);
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function writeFixture(target) {
  const rows = [
    codexAssistant(
      [
        "Long JSON code fence:",
        "",
        "```json",
        LONG_JSON_LINE,
        "```",
        "",
        "Short JSON code fence:",
        "",
        "```json",
        '{"ok":true}',
        "```",
      ].join("\n"),
      "2026-07-13T12:00:00Z"
    ),
    codexToolCall("call-long-output", "Read", '{"path":"long-output.txt"}', "2026-07-13T12:00:01Z"),
    codexToolOutput("call-long-output", LONG_TOOL_LINE, "2026-07-13T12:00:02Z"),
    codexToolCall("call-short-output", "Read", '{"path":"short-output.txt"}', "2026-07-13T12:00:03Z"),
    codexToolOutput("call-short-output", "short output", "2026-07-13T12:00:04Z"),
  ];
  await fs.writeFile(target, rows.map((row) => JSON.stringify(row)).join("\n") + "\n");
}

async function boxMetrics(locator) {
  return locator.evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      clientWidth: element.clientWidth,
      scrollWidth: element.scrollWidth,
      clientHeight: element.clientHeight,
      scrollHeight: element.scrollHeight,
      overflowX: style.overflowX,
      overflowWrap: style.overflowWrap,
      whiteSpace: style.whiteSpace,
      lineHeight: Number.parseFloat(style.lineHeight),
      paddingBlock: Number.parseFloat(style.paddingTop) + Number.parseFloat(style.paddingBottom),
    };
  });
}

async function textMetrics(locator) {
  return locator.evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      height: element.getBoundingClientRect().height,
      lineHeight: Number.parseFloat(style.lineHeight),
      overflowWrap: style.overflowWrap,
      whiteSpace: style.whiteSpace,
    };
  });
}

async function main() {
  assert(LONG_JSON_LINE.length === 200, `Expected 200-char JSON line, got ${LONG_JSON_LINE.length}`);
  await fs.mkdir(path.dirname(SCREENSHOT_PATH), { recursive: true });
  const fixtures = makeFixtureRoot("wiki-87-code-overflow-");
  const transcriptPath = path.join(fixtures.root, "wiki87_code_overflow.jsonl");
  await writeFixture(transcriptPath);
  writeRegistry(fixtures.registryPath, [["WIKI-87", transcriptPath]]);
  writeQueue(fixtures.queuePath, "WIKI-87", []);

  logStep(`fixture root: ${fixtures.root}`);
  const backend = await startBackend(fixtures);
  logStep(`isolated backend: ${backend.baseUrl}`);
  logStep("launching Chromium");
  const browser = await chromium.launch({ headless: true });
  logStep("Chromium ready");
  const page = await browser.newPage({ viewport: { width: 1100, height: 1000 } });

  try {
    logStep("opening fixture session");
    await page.addInitScript(() => {
      localStorage.setItem(
        "wiki-window-layout-v2",
        JSON.stringify({
          version: 2,
          activeWindowId: "window-0",
          windows: [
            {
              id: "window-0",
              focusedPaneId: "pane-1",
              layout: { kind: "pane", id: "pane-1", path: "agent://WIKI-87" },
            },
          ],
        })
      );
      localStorage.setItem("wiki-sidebar-visible", "false");
    });
    await page.goto(`${backend.baseUrl}/#/agent/WIKI-87`, { waitUntil: "domcontentloaded" });
    logStep("fixture document loaded");
    await page.waitForSelector(".session-scroll");
    logStep("session surface ready");
    logStep("waiting for Shiki fences");
    await page.waitForFunction(
      () => document.querySelectorAll(".session-assistant .shiki-block .shiki").length === 2,
      null,
      { timeout: 5000 }
    );

    await page.locator(".session-activity-head").click();
    const tools = page.locator(".session-tool");
    assert((await tools.count()) === 2, `Expected 2 tool calls, found ${await tools.count()}`);
    for (const tool of await tools.all()) {
      await tool.locator(".session-tool-head").click();
    }
    const toolOutputs = page.locator(".session-tool-output");
    const longToolOutput = toolOutputs.nth(0);
    const shortToolOutput = toolOutputs.nth(1);
    await shortToolOutput.waitFor({ state: "visible" });
    await page.waitForTimeout(250);

    await page.locator(".session-scroll").evaluate((element) => {
      element.scrollTop = 0;
    });
    await page.waitForTimeout(100);
    await page.screenshot({ path: SCREENSHOT_PATH });
    logStep(`screenshot: ${SCREENSHOT_PATH}`);

    const codeBlocks = page.locator(".session-assistant .shiki-block pre");
    const longCode = await boxMetrics(codeBlocks.nth(0));
    const shortCode = await boxMetrics(codeBlocks.nth(1));
    const longCodeText = await textMetrics(codeBlocks.nth(0).locator("code"));
    const output = await boxMetrics(longToolOutput);
    const shortOutput = await boxMetrics(shortToolOutput);

    assert(longCode.overflowX === "auto", `Long code overflow-x is ${longCode.overflowX}`);
    assert(longCode.whiteSpace === "pre", `Long code white-space is ${longCode.whiteSpace}`);
    assert(
      longCode.scrollWidth > longCode.clientWidth,
      `Long code should scroll horizontally: ${JSON.stringify(longCode)}`
    );
    assert(
      longCode.clientWidth === shortCode.clientWidth,
      `Long code escaped its visible container: long=${JSON.stringify(longCode)} short=${JSON.stringify(shortCode)}`
    );
    assert(
      longCodeText.whiteSpace === "pre" && longCodeText.overflowWrap === "normal",
      `Long code permits mid-token wrapping: ${JSON.stringify(longCodeText)}`
    );
    assert(
      longCodeText.height <= longCodeText.lineHeight * 1.5,
      `Long code wrapped vertically: ${JSON.stringify(longCodeText)}`
    );
    assert(
      shortCode.scrollWidth <= shortCode.clientWidth + 1,
      `Short code has a phantom scrollbar: ${JSON.stringify(shortCode)}`
    );
    assert(output.whiteSpace === "pre-wrap", `Tool output white-space is ${output.whiteSpace}`);
    assert(output.overflowWrap === "anywhere", `Tool output overflow-wrap is ${output.overflowWrap}`);
    assert(
      output.scrollWidth <= output.clientWidth + 1 &&
        output.clientHeight - output.paddingBlock > output.lineHeight * 1.5,
      `Tool output should wrap without horizontal clipping: ${JSON.stringify(output)}`
    );
    assert(
      shortOutput.scrollWidth <= shortOutput.clientWidth + 1 &&
        shortOutput.clientHeight - shortOutput.paddingBlock <= shortOutput.lineHeight * 1.5,
      `Short tool output should stay on one line without a scrollbar: ${JSON.stringify(shortOutput)}`
    );

    logStep(`long code metrics: ${JSON.stringify(longCode)}`);
    logStep(`short code metrics: ${JSON.stringify(shortCode)}`);
    logStep(`long code text metrics: ${JSON.stringify(longCodeText)}`);
    logStep(`tool output metrics: ${JSON.stringify(output)}`);
    logStep(`short tool output metrics: ${JSON.stringify(shortOutput)}`);
  } finally {
    await browser.close();
    await backend.stop();
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
