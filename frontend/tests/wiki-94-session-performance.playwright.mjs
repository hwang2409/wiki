import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { performance } from "node:perf_hooks";
import { chromium } from "playwright";

import {
  codexAssistant,
  codexUser,
  makeFixtureRoot,
  startBackend,
  writeQueue,
  writeRegistry,
} from "../scripts/wiki32-harness.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const TICKET = "WIKI-94";
const SMALL_TICKET = "WIKI-93";
const EVENT_COUNT = 50_000;

async function writeLargeTranscript(target) {
  const source = path.join(ROOT, "backend", "tests", "fixtures", "codex_native_surfaces.jsonl");
  const seed = (await fs.readFile(source, "utf8")).trim().split("\n").filter(Boolean);
  const rows = Array.from({ length: EVENT_COUNT }, (_, index) => {
    const seedRow = JSON.parse(seed[index % seed.length]);
    return {
      ...codexAssistant(`event-${index}`, "2026-07-13T12:00:00Z"),
      fixture_seed_type: seedRow.type,
    };
  });
  await fs.writeFile(target, `${rows.map((row) => JSON.stringify(row)).join("\n")}\n`);
}

async function main() {
  const fixtures = makeFixtureRoot("wiki-94-session-performance-");
  const largeTranscript = path.join(fixtures.root, "codex-50k.jsonl");
  const smallTranscript = path.join(fixtures.root, "codex-small.jsonl");
  await writeLargeTranscript(largeTranscript);
  await fs.writeFile(
    smallTranscript,
    `${JSON.stringify(codexUser("small fixture", "2026-07-13T11:59:00Z"))}\n`,
  );
  writeRegistry(fixtures.registryPath, [
    [SMALL_TICKET, smallTranscript],
    [TICKET, largeTranscript],
  ]);
  writeQueue(fixtures.queuePath, TICKET, []);

  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  let sessionRequests = 0;
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === `/api/agents/${TICKET}/session`) sessionRequests += 1;
  });

  try {
    const layout = {
      version: 2,
      activeWindowId: "window-0",
      windows: [{
        id: "window-0",
        focusedPaneId: "pane-1",
        layout: { kind: "pane", id: "pane-1", path: `agent://${SMALL_TICKET}` },
      }],
    };
    await page.addInitScript(({ storedLayout }) => {
      localStorage.setItem("wiki-window-layout-v2", JSON.stringify(storedLayout));
      localStorage.setItem("wiki-sidebar-visible", "false");
    }, { storedLayout: layout });
    await page.goto(`${backend.baseUrl}/#/agent/${SMALL_TICKET}`, { waitUntil: "domcontentloaded" });
    await page.locator(".session-scroll").waitFor({ state: "visible" });
    await page.getByText("small fixture").waitFor({ state: "visible" });

    await page.evaluate(() => {
      window.__wiki94TimerGaps = [];
      let last = performance.now();
      window.__wiki94Timer = window.setInterval(() => {
        const now = performance.now();
        window.__wiki94TimerGaps.push(now - last);
        last = now;
      }, 50);
    });

    const responsePromise = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return url.pathname === `/api/agents/${TICKET}/session` && url.searchParams.get("cursor") === "0";
    });
    const started = performance.now();
    await page.evaluate((ticket) => { window.location.hash = `/agent/${ticket}`; }, TICKET);
    await page.getByText("event-49999", { exact: true }).waitFor({ state: "visible" });
    const switchLatency = performance.now() - started;
    const initialResponse = await responsePromise;
    const initial = await initialResponse.json();

    if (switchLatency >= 500) throw new Error(`50k session switch took ${switchLatency.toFixed(1)}ms`);
    if (initial.events.length !== 500) throw new Error(`Initial window returned ${initial.events.length} events`);
    if (initial.base !== EVENT_COUNT - 500 || !initial.has_older) {
      throw new Error(`Initial window contract was wrong: base=${initial.base} has_older=${initial.has_older}`);
    }

    await page.evaluate(({ cursor, path, ticket }) => {
      const poll = () => fetch(
        `/api/agents/${ticket}/session?cursor=${cursor}&path=${encodeURIComponent(path)}`,
      );
      window.__wiki94Poll = window.setInterval(poll, 2_500);
    }, { cursor: initial.cursor, path: initial.path, ticket: TICKET });

    await page.waitForTimeout(5_500);
    const timerGaps = await page.evaluate(() => {
      window.clearInterval(window.__wiki94Timer);
      window.clearInterval(window.__wiki94Poll);
      return window.__wiki94TimerGaps;
    });
    const maxTimerGap = Math.max(...timerGaps);
    if (sessionRequests < 3) throw new Error(`Polling did not continue: ${sessionRequests} session requests`);
    if (maxTimerGap >= 500) throw new Error(`Polling blocked the main thread for ${maxTimerGap.toFixed(1)}ms`);

    const scroll = page.locator(".session-scroll");
    await scroll.evaluate((element) => { element.scrollTop = 0; });
    await page.getByRole("button", { name: "Load older events" }).click();
    await page.getByRole("button", { name: "Loading older events…" }).waitFor({ state: "detached" });
    await scroll.evaluate((element) => { element.scrollTop = 0; });
    await page.getByText("event-49000", { exact: true }).waitFor({ state: "visible" });

    console.error(JSON.stringify({
      fixture: largeTranscript,
      events: EVENT_COUNT,
      initial_events: initial.events.length,
      switch_latency_ms: Number(switchLatency.toFixed(1)),
      max_poll_timer_gap_ms: Number(maxTimerGap.toFixed(1)),
      session_requests: sessionRequests,
    }));
  } finally {
    await page.close();
    await browser.close();
    await backend.stop();
  }
}

await main();
