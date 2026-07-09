import { appendFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { chromium } from "playwright";

import {
  codexAssistant,
  codexToolCall,
  codexToolOutput,
  makeFixtureRoot,
  openSessionPage,
  startBackend,
  writeQueue,
  writeRegistry,
} from "./wiki32-harness.mjs";

function appendRow(path, row) {
  appendFileSync(path, `${JSON.stringify(row)}\n`);
}

async function main() {
  const fixtures = makeFixtureRoot("wiki32-payload-");
  const transcript = join(fixtures.root, "wiki32-main.jsonl");
  const largeTail = Array.from({ length: 72 }, (_, index) =>
    codexAssistant(`tail-${index} ${"x".repeat(7000)}`, `2026-07-09T00:00:${String(index + 1).padStart(2, "0")}Z`)
  );
  writeFileSync(
    transcript,
    [codexToolCall("call-1", "exec_command", "echo hi", "2026-07-09T00:00:00Z"), ...largeTail]
      .map((row) => JSON.stringify(row))
      .join("\n") + "\n"
  );
  writeRegistry(fixtures.registryPath, [["WIKI-32", transcript]]);
  writeQueue(fixtures.queuePath, "WIKI-32");

  const backend = await startBackend(fixtures);
  const browser = await chromium.launch();
  const page = await browser.newPage();
  const responseSizes = [];

  page.on("response", async (response) => {
    if (!response.url().includes("/api/agents/WIKI-32/session")) return;
    const text = await response.text();
    responseSizes.push(Buffer.byteLength(text));
  });

  const timers = [
    setTimeout(() => {
      appendRow(transcript, codexToolOutput("call-1", "done\nexited with code 0", "2026-07-09T00:01:30Z"));
    }, 3500),
    setTimeout(() => {
      appendRow(transcript, codexAssistant("post-patch live tail", "2026-07-09T00:01:33Z"));
    }, 7000),
  ];

  try {
    await openSessionPage(page, backend.baseUrl);
    await page.waitForTimeout(15000);
    const steadyState = responseSizes.slice(1);
    console.log(
      JSON.stringify(
        {
          metric: "steadyStateSessionBytes",
          firstLoadBytes: responseSizes[0] ?? 0,
          maxSteadyStateBytes: steadyState.length ? Math.max(...steadyState) : 0,
          steadyStateBytes: steadyState,
        },
        null,
        2
      )
    );
  } finally {
    timers.forEach((timer) => clearTimeout(timer));
    await browser.close();
    await backend.stop();
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
