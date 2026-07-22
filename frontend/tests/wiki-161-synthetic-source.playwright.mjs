import fs from "node:fs/promises";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

import { makeFixtureRoot, startBackend } from "../scripts/wiki32-harness.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || "/tmp/wiki-161-playwright-evidence";
const RUN_ID = "00000000-0000-4000-8000-000000000161";
const TICKET = "WIKI-161";

function logStep(message) {
  console.error(`[wiki-161] ${message}`);
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function line(value) {
  return `${JSON.stringify(value)}\n`;
}

const HENRY_TS = "2026-07-22T18:00:00Z";
const FLEET_TS = "2026-07-22T18:00:05Z";
const FLEET_TS_2 = "2026-07-22T18:00:07Z";
const STEER_TS = "2026-07-22T18:00:10Z";

const HENRY_TEXT = "hi from henry";
const FLEET_TEXT = "[fleet] WIKI-1234 merged";
const FLEET_TEXT_2 = "[fleet] WIKI-1235 blocked";
const STEER_TEXT = "supervisor steer body";

const NORMALIZED_EVENTS = [
  {
    seq: 1,
    raw_seq: 1,
    normalized_at: HENRY_TS,
    disposition: "rendered",
    kind: "codex_user",
    payload: {
      method: "item/completed",
      params: {
        item: {
          type: "userMessage",
          content: [{ type: "text", text: HENRY_TEXT }],
        },
      },
    },
    lifecycle_state: null,
  },
  {
    seq: 2,
    raw_seq: 2,
    normalized_at: FLEET_TS,
    disposition: "rendered",
    kind: "codex_user",
    payload: {
      method: "item/completed",
      params: {
        item: {
          type: "userMessage",
          content: [{ type: "text", text: FLEET_TEXT }],
        },
      },
    },
    lifecycle_state: null,
  },
  {
    seq: 3,
    raw_seq: 3,
    normalized_at: FLEET_TS_2,
    disposition: "rendered",
    kind: "codex_user",
    payload: {
      method: "item/completed",
      params: {
        item: {
          type: "userMessage",
          content: [{ type: "text", text: FLEET_TEXT_2 }],
        },
      },
    },
    lifecycle_state: null,
  },
  {
    seq: 4,
    raw_seq: 4,
    normalized_at: STEER_TS,
    disposition: "rendered",
    kind: "codex_user",
    payload: {
      method: "item/completed",
      params: {
        item: {
          type: "userMessage",
          content: [{ type: "text", text: STEER_TEXT }],
        },
      },
    },
    lifecycle_state: null,
  },
];

const COMPOSER_MESSAGES = [
  // Henry — no source, matches real user event → should render as bubble.
  {
    pending_id: "11111111-1111-4111-8111-111111111111",
    text: HENRY_TEXT,
    sent_at: HENRY_TS,
    echoed_at: HENRY_TS,
    seq: 1,
  },
  {
    pending_id: "22222222-2222-4222-8222-222222222222",
    text: FLEET_TEXT,
    sent_at: FLEET_TS,
    echoed_at: FLEET_TS,
    seq: 2,
    source: "fleet-monitor",
  },
  {
    pending_id: "33333333-3333-4333-8333-333333333333",
    text: FLEET_TEXT_2,
    sent_at: FLEET_TS_2,
    echoed_at: FLEET_TS_2,
    seq: 3,
    source: "fleet-monitor",
  },
  {
    pending_id: "44444444-4444-4444-8444-444444444444",
    text: STEER_TEXT,
    sent_at: STEER_TS,
    echoed_at: STEER_TS,
    seq: 4,
    source: "supervisor-steer",
  },
];

async function startFakeSupervisor(fixtures, current) {
  const subscribers = new Set();

  const runtimeRow = () => ({
    ...current,
    control_attached: true,
    provider_alive: true,
  });

  const server = net.createServer((socket) => {
    let buffer = "";
    socket.on("data", async (chunk) => {
      buffer += chunk.toString();
      const newline = buffer.indexOf("\n");
      if (newline < 0) return;
      const request = JSON.parse(buffer.slice(0, newline));
      const { id, method, params = {} } = request;
      if (method === "events/subscribe") {
        subscribers.add(socket);
        socket.write(line({ id, result: { subscribed: true } }));
        socket.on("close", () => subscribers.delete(socket));
        return;
      }
      let result;
      if (method === "ping") {
        result = { status: "ok", pid: process.pid };
      } else if (method === "run/list") {
        result = { status: "ok", pid: process.pid, runs: [runtimeRow()] };
      } else if (method === "run/status") {
        result = runtimeRow();
      } else if (method === "events/read") {
        result = {
          run_id: RUN_ID,
          provider: "codex",
          state: current.state,
          raw_count: NORMALIZED_EVENTS.length,
          normalized_count: NORMALIZED_EVENTS.length,
          dispositions: {
            rendered: NORMALIZED_EVENTS.length,
            summarized: 0,
            ignored: 0,
            unknown: 0,
          },
          pending_requests: [],
          composer_messages: COMPOSER_MESSAGES,
          events: NORMALIZED_EVENTS,
          raw: params.include_raw ? [] : null,
        };
      } else if (method === "run/queue") {
        result = { messages: [] };
      } else if (method === "run/send_now") {
        result = { status: "sent" };
      } else {
        socket.write(
          line({
            id,
            error: {
              type: "ValueError",
              message: `unsupported fixture method: ${method}`,
            },
          }),
        );
        socket.end();
        return;
      }
      socket.write(line({ id, result }));
      socket.end();
    });
  });

  await fs.rm(fixtures.supervisorSocketPath, { force: true });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(fixtures.supervisorSocketPath, resolve);
  });
  return {
    async stop() {
      for (const socket of subscribers) socket.destroy();
      await new Promise((resolve) => server.close(resolve));
      await fs.rm(fixtures.supervisorSocketPath, { force: true });
    },
  };
}

async function main() {
  await fs.mkdir(OUT_DIR, { recursive: true });
  const fixtures = makeFixtureRoot("wiki-161-synthetic-source-");
  const transcript = path.join(fixtures.root, "codex-source.jsonl");
  const rawLog = path.join(fixtures.root, "raw.jsonl");
  await fs.writeFile(transcript, "");
  await fs.writeFile(rawLog, "");

  const current = {
    ticket: TICKET,
    run_id: RUN_ID,
    provider: "codex",
    kind: "cdx",
    role: "orchestrator",
    model: "gpt-5.4",
    effort: "high",
    worktree: fixtures.root,
    cwd: fixtures.root,
    orch: null,
    state: "working",
    provider_session_id: "fixture-161",
    provider_pid: process.pid,
    transcript,
    log: rawLog,
    window: null,
    spawned_at: HENRY_TS,
  };
  const registry = { [TICKET]: { history: [], current } };
  await fs.writeFile(fixtures.registryPath, JSON.stringify(registry, null, 2));
  await fs.writeFile(fixtures.queuePath, "{}\n");

  logStep("starting fake supervisor + backend");
  const supervisor = await startFakeSupervisor(fixtures, current);
  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1200, height: 1200 } });

  try {
    await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, {
      waitUntil: "domcontentloaded",
    });
    await page.locator(".session-scroll").waitFor();

    // Wait for all four user events to render.
    await page.waitForFunction((expected) => {
      const bubbles = document.querySelectorAll(".session-user").length;
      const markers = document.querySelectorAll(
        "[data-testid='session-synthetic-source']",
      ).length;
      return bubbles + markers >= expected;
    }, NORMALIZED_EVENTS.length);

    // Regression: Henry's turn (no source) still renders as a normal bubble.
    const henryBubble = page.locator(".session-user", { hasText: HENRY_TEXT });
    await henryBubble.waitFor({ state: "attached" });
    assert(
      (await henryBubble.count()) === 1,
      "expected a single normal user bubble for Henry's turn",
    );

    // Source-tagged turns render as marker rows, not user bubbles.
    const fleetMarker = page.locator(
      "[data-testid='session-synthetic-source'][data-source='fleet-monitor']",
    );
    await fleetMarker.first().waitFor({ state: "attached" });
    const fleetCount = await fleetMarker.count();
    assert(
      fleetCount === 2,
      `expected 2 fleet-monitor marker rows, saw ${fleetCount}`,
    );
    const steerMarker = page.locator(
      "[data-testid='session-synthetic-source'][data-source='supervisor-steer']",
    );
    await steerMarker.first().waitFor({ state: "attached" });
    assert(
      (await steerMarker.count()) === 1,
      "expected a single supervisor-steer marker row",
    );

    // Marker rows carry a plain-text source chip, not an avatar/bubble.
    const chipText = await fleetMarker.first().locator(".session-synthetic-source-chip").innerText();
    assert(
      chipText === "[fleet-monitor]",
      `expected chip text '[fleet-monitor]', saw '${chipText}'`,
    );

    // Marker rows must NOT render as .session-user bubbles.
    const fleetAsBubble = await page
      .locator(".session-user", { hasText: FLEET_TEXT })
      .count();
    assert(fleetAsBubble === 0, "fleet marker leaked into a .session-user bubble");
    const steerAsBubble = await page
      .locator(".session-user", { hasText: STEER_TEXT })
      .count();
    assert(steerAsBubble === 0, "steer marker leaked into a .session-user bubble");

    // Multiple synthetic messages back-to-back render as separate marker rows,
    // not clustered as one user thread. Each row has its own DOM node.
    assert(
      fleetCount >= 2 && fleetMarker.first() !== fleetMarker.nth(1),
      "consecutive fleet markers should render as distinct rows",
    );

    // Marker rows must remain left-aligned (not `align-self: flex-end`) so they
    // do not read as Henry-authored messages.
    const alignment = await fleetMarker.first().evaluate((element) => {
      return getComputedStyle(element).alignSelf;
    });
    assert(
      alignment !== "flex-end" && alignment !== "end",
      `marker row aligned as user bubble: alignSelf=${alignment}`,
    );

    await page.screenshot({
      path: path.join(OUT_DIR, "wiki-161-synthetic-source.png"),
      fullPage: true,
    });

    logStep("all synthetic-source assertions passed");
  } finally {
    await page.close();
    await browser.close();
    await backend.stop();
    await supervisor.stop();
  }
}

await main();
