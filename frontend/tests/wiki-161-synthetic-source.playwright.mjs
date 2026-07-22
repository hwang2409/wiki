import fs from "node:fs/promises";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

import {
  codexUser,
  makeFixtureRoot,
  startBackend,
} from "../scripts/wiki32-harness.mjs";

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
// R4/R7: identical human + synthetic sends inside the 2s composer-match
// window must not swap. The human turn arrives first; the fleet turn
// arrives ~1.5s later with the same body.
const COLLISION_HUMAN_TS = "2026-07-22T18:00:20Z";
const COLLISION_FLEET_TS = "2026-07-22T18:00:21.500Z";

const HENRY_TEXT = "hi from henry";
const FLEET_TEXT = "[fleet] WIKI-1234 merged";
const FLEET_TEXT_2 = "[fleet] WIKI-1235 blocked";
const STEER_TEXT = "supervisor steer body";
const COLLISION_TEXT = "please check queue";

// Native transcript is the authoritative event stream in this fixture; the
// events/read RPC only needs to expose `composer_messages` to the frontend.
const NORMALIZED_EVENTS = [];

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
  // Collision pair: same text, both composer rows. The Henry (unsourced)
  // row is chronologically FIRST — FIFO reservation must claim the earlier
  // transcript event for the Henry composer, leaving the fleet event to
  // pick up the second transcript row.
  {
    pending_id: "55555555-5555-4555-8555-555555555555",
    text: COLLISION_TEXT,
    sent_at: COLLISION_HUMAN_TS,
    echoed_at: COLLISION_HUMAN_TS,
    seq: 5,
  },
  {
    pending_id: "66666666-6666-4666-8666-666666666666",
    text: COLLISION_TEXT,
    sent_at: COLLISION_FLEET_TS,
    echoed_at: COLLISION_FLEET_TS,
    seq: 6,
    source: "fleet-monitor",
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
  // Native transcript with real user turns so the frontend actually runs
  // `applyComposerSources` against transcript events. Without this the
  // composer_messages surface synthesizes every row and the correlator
  // path (finding R4/R7) is never exercised.
  const transcriptRows = [
    codexUser(HENRY_TEXT, HENRY_TS),
    codexUser(FLEET_TEXT, FLEET_TS),
    codexUser(FLEET_TEXT_2, FLEET_TS_2),
    codexUser(STEER_TEXT, STEER_TS),
    codexUser(COLLISION_TEXT, COLLISION_HUMAN_TS),
    codexUser(COLLISION_TEXT, COLLISION_FLEET_TS),
  ];
  await fs.writeFile(
    transcript,
    transcriptRows.map((row) => JSON.stringify(row)).join("\n") + "\n",
  );
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

    // Wait for every transcript user turn to render (either as a Henry
    // bubble or as a synthetic marker row).
    await page.waitForFunction((expected) => {
      const bubbles = document.querySelectorAll(".session-user").length;
      const markers = document.querySelectorAll(
        "[data-testid='session-synthetic-source']",
      ).length;
      return bubbles + markers >= expected;
    }, transcriptRows.length);

    // Regression: Henry's turn (no source) still renders as a normal bubble.
    const henryBubble = page.locator(".session-user", { hasText: HENRY_TEXT });
    await henryBubble.waitFor({ state: "attached" });
    assert(
      (await henryBubble.count()) === 1,
      "expected a single normal user bubble for Henry's turn",
    );

    // Source-tagged turns render as marker rows, not user bubbles. Two
    // fleet turns (WIKI-1234 / WIKI-1235) plus the fleet half of the
    // identical-text collision pair → three fleet markers total.
    const fleetMarker = page.locator(
      "[data-testid='session-synthetic-source'][data-source='fleet-monitor']",
    );
    await fleetMarker.first().waitFor({ state: "attached" });
    const fleetCount = await fleetMarker.count();
    assert(
      fleetCount === 3,
      `expected 3 fleet-monitor marker rows, saw ${fleetCount}`,
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

    // R4/R7: identical-text collision — Henry's earlier transcript row must
    // stay a bubble; only the second (fleet) row becomes a marker. If the
    // correlator swapped them, the bubble would carry the fleet timestamp
    // and no fleet marker would exist for the collision text.
    const collisionBubbles = await page
      .locator(".session-user", { hasText: COLLISION_TEXT })
      .count();
    assert(
      collisionBubbles === 1,
      `collision text should render exactly one Henry bubble, saw ${collisionBubbles}`,
    );
    const collisionMarkers = await page
      .locator(
        "[data-testid='session-synthetic-source'][data-source='fleet-monitor']",
        { hasText: COLLISION_TEXT },
      )
      .count();
    assert(
      collisionMarkers === 1,
      `collision text should render exactly one fleet marker, saw ${collisionMarkers}`,
    );

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
