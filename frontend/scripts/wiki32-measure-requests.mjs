import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { chromium } from "playwright";

import {
  codexAssistant,
  codexUser,
  makeFixtureRoot,
  openSessionPage,
  startBackend,
  writeQueue,
  writeRegistry,
} from "./wiki32-harness.mjs";

async function main() {
  const fixtures = makeFixtureRoot("wiki32-requests-");
  const transcript = join(fixtures.root, "wiki32-main.jsonl");
  writeFileSync(
    transcript,
    [
      codexUser("ticket WIKI-32 warmup", "2026-07-09T00:00:00Z"),
      codexAssistant("steady transcript body", "2026-07-09T00:00:01Z"),
    ]
      .map((row) => JSON.stringify(row))
      .join("\n") + "\n"
  );
  writeRegistry(fixtures.registryPath, [
    ["WIKI-32", transcript],
    ["WIKI-33", transcript],
    ["WIKI-34", transcript],
  ]);
  writeQueue(fixtures.queuePath, "WIKI-32");

  const backend = await startBackend(fixtures);
  const browser = await chromium.launch();
  const page = await browser.newPage();
  const requests = [];

  page.on("response", (response) => {
    if (!response.url().includes("/api/agents/WIKI-32/session")) return;
    requests.push(response.url());
  });

  try {
    await openSessionPage(page, backend.baseUrl);
    await page.waitForTimeout(15000);
    console.log(
      JSON.stringify(
        {
          metric: "requests15s",
          count: requests.length,
          urls: requests,
        },
        null,
        2
      )
    );
  } finally {
    await browser.close();
    await backend.stop();
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
