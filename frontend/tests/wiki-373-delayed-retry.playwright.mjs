import fs from "node:fs/promises";
import { chromium } from "playwright";

import {
  makeFixtureRoot,
  startBackend,
  writeQueue,
  writeRegistry,
} from "../scripts/wiki32-harness.mjs";

const TICKET = "WIKI-373-DELAYED-RETRY";

async function main() {
  const fixtures = makeFixtureRoot("wiki-373-delayed-retry-");
  const transcript = fixtures.root + "/codex-empty.jsonl";
  await fs.writeFile(
    transcript,
    JSON.stringify({
      type: "event_msg",
      timestamp: new Date().toISOString(),
      payload: { type: "thread_settings_applied" },
    }) + "\n",
  );
  writeRegistry(fixtures.registryPath, [[TICKET, transcript]]);
  writeQueue(fixtures.queuePath, TICKET, []);

  let sendCount = 0;
  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page.setDefaultTimeout(5_000);

  try {
    await page.clock.install({ time: "2026-08-19T12:00:00Z" });
    await page.route(`**/api/agents/${TICKET}/message`, async (route) => {
      sendCount += 1;
      const body = route.request().postDataJSON();
      await route.fulfill({
        status: sendCount === 1 ? 400 : 200,
        contentType: "application/json",
        body: JSON.stringify(
          sendCount === 1
            ? { detail: "fixture send failure" }
            : { status: "sent", pending_id: body.pending_id },
        ),
      });
    });
    await page.addInitScript(({ ticket }) => {
      localStorage.setItem("wiki-sidebar-visible", "false");
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
    }, { ticket: TICKET });
    await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });

    const composer = page.locator(".session-composer textarea");
    await composer.waitFor();
    await composer.fill("delayed retry");
    await composer.press("Enter");
    const pending = page.locator(".session-scroll .session-pending-user", { hasText: "delayed retry" });
    await pending.getByText("send failed: fixture send failure").waitFor();

    await page.clock.fastForward(45_000);
    await pending.getByRole("button", { name: "Retry send" }).click();
    await pending.locator(".session-pending-status").getByText("sent · waiting for transcript").waitFor();

    await page.clock.fastForward(29_000);
    if (await pending.getAttribute("data-status") !== "sent") {
      throw new Error("delayed retry lost its full recovery window");
    }
    await page.clock.fastForward(2_000);
    await pending.getByText("delivery uncertain").waitFor();
    if (sendCount !== 2) throw new Error(`expected one retry request, got ${sendCount - 1}`);
  } finally {
    await page.close();
    await browser.close();
    await backend.stop();
  }
}

await main();
