import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

import {
  makeFixtureRoot,
  startBackend,
  writeQueue,
  writeRegistry,
} from "../scripts/wiki32-harness.mjs";

const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || "/tmp/wiki-59-playwright-evidence";

function logStep(message) {
  console.error(`[wiki-59-playwright] ${message}`);
}

function claudeAssistantText(text, ts) {
  return {
    type: "assistant",
    timestamp: ts,
    message: {
      role: "assistant",
      content: [{ type: "text", text }],
    },
  };
}

function claudeToolUse(id, name, input, ts) {
  return {
    type: "assistant",
    timestamp: ts,
    message: {
      role: "assistant",
      content: [{ type: "tool_use", id, name, input }],
    },
  };
}

function claudeToolResult(id, content, ts) {
  return {
    type: "user",
    timestamp: ts,
    message: {
      role: "user",
      content: [{ type: "tool_result", tool_use_id: id, content }],
    },
  };
}

function claudeBashUserRow(input, stdout, ts) {
  return {
    type: "user",
    timestamp: ts,
    message: {
      role: "user",
      content: [
        {
          type: "text",
          text: `<bash-input>${input}</bash-input>\n<bash-stdout>${stdout}</bash-stdout>`,
        },
      ],
    },
  };
}

async function writeShikiFixture(dir) {
  const lines = [
    { type: "mode", mode: "normal", sessionId: "fixture-shiki" },
    { type: "custom-title", customTitle: "WIKI-59 Shiki fixture", sessionId: "fixture-shiki" },
    claudeAssistantText(
      [
        "Here's a small Python example:",
        "",
        "```python",
        "def greet(name: str) -> str:",
        "    # multi-line greeting",
        '    return f"hello, {name}!"',
        "",
        "print(greet('world'))",
        "```",
        "",
        "And a JSON snippet:",
        "",
        "```json",
        '{"lang": "shiki", "ok": true, "count": 42}',
        "```",
      ].join("\n"),
      "2026-07-10T18:00:00Z"
    ),
    claudeBashUserRow(
      "grep -R 'shiki' frontend/src | head -3 && echo done",
      "frontend/src/shiki.tsx:1: import\ndone",
      "2026-07-10T18:00:01Z"
    ),
    claudeToolUse(
      "toolu_bash_1",
      "Bash",
      { command: "ls -la /tmp/shiki-*.log 2>/dev/null || echo empty" },
      "2026-07-10T18:00:02Z"
    ),
    claudeToolResult("toolu_bash_1", "empty\n", "2026-07-10T18:00:03Z"),
    claudeAssistantText("Highlighting works.", "2026-07-10T18:00:04Z"),
  ];
  const target = path.join(dir, "wiki59_shiki.jsonl");
  await fs.writeFile(target, lines.map((line) => JSON.stringify(line)).join("\n") + "\n");
  return target;
}

async function main() {
  logStep("preparing isolated fixtures");
  await fs.mkdir(OUT_DIR, { recursive: true });
  const fixtures = makeFixtureRoot("wiki-59-shiki-");
  const transcript = await writeShikiFixture(fixtures.root);
  writeRegistry(fixtures.registryPath, [["WIKI-59", transcript]]);
  writeQueue(fixtures.queuePath, "WIKI-59", []);

  logStep("starting isolated backend");
  const backend = await startBackend(fixtures);
  logStep(`backend ready at ${backend.baseUrl}`);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 1400 } });

  try {
    logStep("opening WIKI-59 session (light theme)");
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
              layout: { kind: "pane", id: "pane-1", path: "agent://WIKI-59" },
            },
          ],
        })
      );
      localStorage.setItem("wiki-sidebar-visible", "false");
      localStorage.setItem("wiki-theme", "mono-light");
    });
    await page.goto(`${backend.baseUrl}/#/agent/WIKI-59`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".session-scroll");
    await page.waitForSelector(".session-bash-command-code");

    // Give shiki a moment to load themes+langs and re-render.
    await page.waitForFunction(
      () => document.querySelectorAll(".shiki-block .shiki").length >= 3,
      null,
      { timeout: 5000 }
    );

    logStep("capturing light-theme screenshot");
    await page.screenshot({
      path: path.join(OUT_DIR, "wiki59-shiki-light.png"),
      fullPage: true,
    });

    logStep("switching to dark theme + re-highlighting");
    await page.evaluate(() => {
      document.documentElement.dataset.theme = "mono-dark";
      localStorage.setItem("wiki-theme", "mono-dark");
    });
    await page.waitForFunction(
      () =>
        Array.from(document.querySelectorAll(".shiki-block .shiki")).every((el) =>
          el.classList.contains("github-dark")
        ),
      null,
      { timeout: 5000 }
    );
    logStep("capturing dark-theme screenshot");
    await page.screenshot({
      path: path.join(OUT_DIR, "wiki59-shiki-dark.png"),
      fullPage: true,
    });

    const shikiCount = await page.locator(".shiki-block .shiki").count();
    if (shikiCount < 3) {
      throw new Error(`Expected >=3 shiki blocks (2 fences + 1 bash), found ${shikiCount}`);
    }
    logStep(`shiki blocks rendered: ${shikiCount}`);
  } finally {
    await browser.close();
    await backend.stop();
  }

  logStep("wiki-59 verification complete");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
