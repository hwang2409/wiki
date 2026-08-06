// WIKI-253: long tool outputs default-collapse to a peek row (chevron +
// preview + line/byte tail); failures render expanded from the start so a
// broken run cannot hide behind a chip while the reader scans downstream
// rows. This fixture exercises both paths against the real transcript.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";

import {
  makeFixtureRoot,
  startBackend,
  writeQueue,
  writeRegistry,
} from "../scripts/wiki32-harness.mjs";

const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || "/tmp/wiki-253-collapse";

function logStep(message) {
  console.error(`[wiki-253-playwright] ${message}`);
}

async function writeJsonl(target, rows) {
  await fs.writeFile(target, `${rows.map((row) => JSON.stringify(row)).join("\n")}\n`);
}

function assistantToolUse(id, name, input, timestamp) {
  return {
    type: "assistant",
    timestamp,
    message: {
      role: "assistant",
      content: [{ type: "tool_use", id, name, input }],
    },
  };
}

function toolResult(id, content, timestamp, isError = false) {
  return {
    type: "user",
    timestamp,
    message: {
      role: "user",
      content: [{ type: "tool_result", tool_use_id: id, content, is_error: isError }],
    },
  };
}

async function openTicket(page, baseUrl, ticket) {
  await page.goto(`${baseUrl}/#/agent/${ticket}`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".session-scroll");
}

async function main() {
  await fs.mkdir(OUT_DIR, { recursive: true });
  const fixtures = makeFixtureRoot("wiki-253-collapse-");

  const longOutput = Array.from({ length: 24 }, (_, i) => `log ${i + 1} :: routine event`).join("\n");
  const longTranscript = path.join(fixtures.root, "long-tool-output.jsonl");
  await writeJsonl(longTranscript, [
    { type: "mode", mode: "normal", sessionId: "fixture-wiki-253-long" },
    {
      type: "user",
      timestamp: "2026-08-07T10:00:00Z",
      message: { role: "user", content: "list logs" },
    },
    assistantToolUse("toolu_long", "Bash", { command: "cat /tmp/log.txt" }, "2026-08-07T10:00:01Z"),
    toolResult("toolu_long", longOutput, "2026-08-07T10:00:02Z"),
  ]);

  const failureTranscript = path.join(fixtures.root, "failed-tool.jsonl");
  const failureOutput = [
    "Traceback (most recent call last):",
    "  File 'runner.py', line 42, in <module>",
    "    main()",
    "  File 'runner.py', line 27, in main",
    "    connect()",
    "ConnectionRefusedError: [Errno 61] Connection refused",
  ].join("\n");
  await writeJsonl(failureTranscript, [
    { type: "mode", mode: "normal", sessionId: "fixture-wiki-253-failed" },
    {
      type: "user",
      timestamp: "2026-08-07T10:05:00Z",
      message: { role: "user", content: "run it" },
    },
    assistantToolUse("toolu_fail", "Bash", { command: "python runner.py" }, "2026-08-07T10:05:01Z"),
    toolResult("toolu_fail", failureOutput, "2026-08-07T10:05:02Z", true),
  ]);

  writeRegistry(fixtures.registryPath, [
    ["WIKI-253A", longTranscript],
    ["WIKI-253B", failureTranscript],
  ]);
  writeQueue(fixtures.queuePath, "WIKI-253A", []);

  logStep(`fixture root: ${fixtures.root}`);
  const backend = await startBackend(fixtures);
  logStep(`backend ready at ${backend.baseUrl}`);

  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1200, height: 900 } });

  try {
    logStep("asserting long tool output collapses to a peek row");
    await openTicket(page, backend.baseUrl, "WIKI-253A");
    // Long output — default-collapsed. The peek row is present, the body
    // preview is absent, and the peek row exposes the size tail so the
    // reader can tell what they are hiding.
    const peek = page.locator(".session-tool-output-peek");
    await peek.first().waitFor({ state: "visible" });
    assert.equal(await peek.getAttribute("aria-expanded"), "false");
    assert.equal(await page.locator(".session-tool-body .transcript-preview").count(), 0);
    const meta = await page.locator(".session-tool-output-peek-meta").first().textContent();
    assert.ok(/\d+ lines · /.test(meta ?? ""), `expected line/size tail, got: ${meta}`);
    await page.screenshot({ path: path.join(OUT_DIR, "collapsed-peek.png"), fullPage: true });

    logStep("clicking the peek row reveals the body and swaps in a collapse affordance");
    await peek.first().click();
    await page.locator(".session-tool-body .transcript-preview").first().waitFor({ state: "visible" });
    assert.equal(await page.locator(".session-tool-output-peek").count(), 0);
    await page.locator(".session-tool-output-collapse").first().waitFor({ state: "visible" });
    await page.screenshot({ path: path.join(OUT_DIR, "expanded-body.png"), fullPage: true });

    logStep("clicking the collapse control returns to the peek row");
    await page.locator(".session-tool-output-collapse").first().click();
    await page.locator(".session-tool-output-peek").first().waitFor({ state: "visible" });

    logStep("asserting failed tool renders expanded from the start");
    await openTicket(page, backend.baseUrl, "WIKI-253B");
    // Failure body renders immediately — no peek row hides a broken run.
    await page.locator(".session-tool-body").first().waitFor({ state: "visible" });
    assert.equal(await page.locator(".session-tool-output-peek").count(), 0);
    // The is-failed marker is on the tool row and the failure output text
    // renders through the error segment path.
    await page.locator(".session-tool.is-failed").first().waitFor({ state: "visible" });
    const failureText = await page
      .locator(".session-tool-output-text, .session-tool-failure-output")
      .first()
      .textContent();
    assert.ok(
      (failureText ?? "").includes("ConnectionRefusedError"),
      `expected failure body inline, got: ${failureText}`,
    );
    await page.screenshot({ path: path.join(OUT_DIR, "failed-expanded.png"), fullPage: true });

    logStep("wiki-253 collapse assertions passed");
  } finally {
    await page.close();
    await browser.close();
    await backend.stop();
  }
}

await main();
