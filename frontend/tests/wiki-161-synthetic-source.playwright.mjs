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
// R2 review R2: an old sourced composer whose real transcript event has
// fallen out of the window must NOT claim a later identical terminal-
// typed Henry row (composer_messages survives run replacement while the
// transcript window is trimmed).
const STALE_FLEET_ECHO_TS = "2026-07-22T17:00:00Z";
const LATE_HENRY_TS = "2026-07-22T18:00:40Z";
// R2 review R2: archived-source rendering — the composer_messages surface
// serves a sourced row that has no matching real transcript event, so
// the frontend must synthesize a marker (same rendering path an archived
// normalized user turn whose payload carried `source` would exercise).
const ARCHIVED_STEER_TS = "2026-07-22T17:30:00Z";

const HENRY_TEXT = "hi from henry";
const FLEET_TEXT = "[fleet] WIKI-1234 merged";
const FLEET_TEXT_2 = "[fleet] WIKI-1235 blocked";
const STEER_TEXT = "supervisor steer body";
const COLLISION_TEXT = "please check queue";
const STALE_FLEET_TEXT = "[fleet] stale-window ping";
const ARCHIVED_STEER_TEXT = "[archived] historical supervisor steer";

// R3 review R2: real archived-session case. A ticket with no registry
// current row, only an archive directory containing `events.jsonl` with a
// normalized user event whose payload carries `source`. Backend must route
// through main._archived_events_payload() and transcripts.read_session_delta
// on the `codex-normalized` format; the source-stamping in transcripts.py
// then flows the marker through to the frontend renderer.
const ARCHIVED_TICKET = "WIKI-161ARC";
const ARCHIVED_RUN_ID = "00000000-0000-4000-8000-000000000162";
const ARCHIVED_SESSION_DIR_NAME = "20260722-170000";
const ARCHIVED_NORMALIZED_TS = "2026-07-22T17:00:00Z";
const ARCHIVED_ROW_HENRY_TEXT = "archived henry bubble";
const ARCHIVED_ROW_STEER_TEXT = "[archived-steer] historical supervisor push";
const ARCHIVED_ROW_ASSISTANT_TEXT = "archived worker acknowledgement";

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
  // Stale-window fleet composer: sent AND echoed ~1h before the transcript
  // window; a later transcript row has the same text but is genuinely
  // Henry-typed. Correlator's text fallback must reject on the upper
  // timestamp bound and leave the Henry row as a bubble.
  {
    pending_id: "77777777-7777-4777-8777-777777777777",
    text: STALE_FLEET_TEXT,
    sent_at: STALE_FLEET_ECHO_TS,
    echoed_at: STALE_FLEET_ECHO_TS,
    seq: 7,
    source: "fleet-monitor",
  },
  // Archived-source rendering path: no matching transcript event, so the
  // frontend synthesizes the row from composer_messages alone. This
  // exercises the same MessageBlock render path an archived normalized
  // user turn with payload.source would take.
  {
    pending_id: "88888888-8888-4888-8888-888888888888",
    text: ARCHIVED_STEER_TEXT,
    sent_at: ARCHIVED_STEER_TS,
    echoed_at: ARCHIVED_STEER_TS,
    seq: 8,
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
    // Late Henry-typed row whose text matches the STALE_FLEET composer row
    // above. The composer's echoed_at is ~1h before this timestamp, so the
    // correlator's upper timestamp bound must reject the fallback match and
    // leave this row rendered as a Henry bubble.
    codexUser(STALE_FLEET_TEXT, LATE_HENRY_TS),
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

  // R3 review R2: real archived-session fixture. No registry entry for
  // ARCHIVED_TICKET — the backend must fall through to
  // main._archive_hint()/_archived_events_payload() and read the
  // normalized events.jsonl below. Every row is what a codex-normalized
  // events.jsonl looks like after WIKI-161 source stamping (payload.source
  // preserved on the sourced user turn, absent on the unsourced ones).
  const archiveTicketDir = path.join(
    fixtures.root,
    "archive",
    ARCHIVED_TICKET,
    ARCHIVED_SESSION_DIR_NAME,
  );
  await fs.mkdir(archiveTicketDir, { recursive: true });
  await fs.writeFile(
    path.join(archiveTicketDir, "run.json"),
    JSON.stringify({
      run_id: ARCHIVED_RUN_ID,
      provider: "codex",
      kind: "cdx",
      model: "gpt-5.4",
      role: "orchestrator",
      state: "completed",
      spawned_at: ARCHIVED_NORMALIZED_TS,
    }),
  );
  await fs.writeFile(
    path.join(archiveTicketDir, "meta.json"),
    JSON.stringify({
      worker: {
        kind: "cdx",
        model: "gpt-5.4",
        role: "orchestrator",
      },
    }),
  );
  // Marker file the archive-hint uses to classify kind=cdx.
  await fs.writeFile(path.join(archiveTicketDir, "cdx-session.log"), "");
  const archivedEvents = [
    {
      seq: 1,
      raw_seq: 1,
      normalized_at: ARCHIVED_NORMALIZED_TS,
      disposition: "rendered",
      kind: "item_completed",
      payload: {
        method: "item/completed",
        params: {
          item: {
            type: "userMessage",
            content: [{ type: "text", text: ARCHIVED_ROW_HENRY_TEXT }],
          },
        },
      },
      lifecycle_state: null,
    },
    {
      seq: 2,
      raw_seq: 2,
      normalized_at: "2026-07-22T17:00:05Z",
      disposition: "rendered",
      kind: "item_completed",
      payload: {
        method: "item/completed",
        params: {
          item: {
            type: "userMessage",
            content: [{ type: "text", text: ARCHIVED_ROW_STEER_TEXT }],
          },
        },
        // The critical piece: source flows through the codex-normalized
        // parser onto the emitted SessionEvent, and the renderer branches
        // it into the SyntheticSourceRow.
        source: "supervisor-steer",
      },
      lifecycle_state: null,
    },
    {
      seq: 3,
      raw_seq: 3,
      normalized_at: "2026-07-22T17:00:10Z",
      disposition: "rendered",
      kind: "item_completed",
      payload: {
        method: "item/completed",
        params: {
          item: {
            type: "agentMessage",
            text: ARCHIVED_ROW_ASSISTANT_TEXT,
          },
        },
      },
      lifecycle_state: null,
    },
  ];
  await fs.writeFile(
    path.join(archiveTicketDir, "events.jsonl"),
    archivedEvents.map((row) => JSON.stringify(row)).join("\n") + "\n",
  );

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
    // basic fleet turns (WIKI-1234 / WIKI-1235), the fleet half of the
    // identical-text collision pair, and the stale-window fleet composer
    // that synthesizes on its own (no matching transcript row) → four
    // fleet markers total.
    const fleetMarker = page.locator(
      "[data-testid='session-synthetic-source'][data-source='fleet-monitor']",
    );
    await fleetMarker.first().waitFor({ state: "attached" });
    const fleetCount = await fleetMarker.count();
    assert(
      fleetCount === 4,
      `expected 4 fleet-monitor marker rows, saw ${fleetCount}`,
    );
    const steerMarker = page.locator(
      "[data-testid='session-synthetic-source'][data-source='supervisor-steer']",
    );
    await steerMarker.first().waitFor({ state: "attached" });
    // Two supervisor-steer markers: the STEER_TEXT transcript row matched
    // against its composer, and the archived-source composer that has no
    // matching transcript row and synthesizes a marker on its own.
    assert(
      (await steerMarker.count()) === 2,
      `expected 2 supervisor-steer marker rows, saw ${await steerMarker.count()}`,
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

    // R4/R7 + R2 R4: identical-text collision. Not only must there be one
    // bubble + one marker, but the BUBBLE must be the EARLIER (Henry) row
    // and the MARKER must be the LATER (fleet) row. A mutation that
    // simply swapped ownership would still land 1+1; the DOM-order check
    // rules it out. All matching nodes are read from the same scroll, so
    // documentPosition tells us which was rendered above the other.
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
    const collisionOwnership = await page.evaluate((text) => {
      const bubble = Array.from(document.querySelectorAll(".session-user"))
        .find((el) => (el.textContent || "").includes(text));
      const marker = Array.from(
        document.querySelectorAll(
          "[data-testid='session-synthetic-source'][data-source='fleet-monitor']",
        ),
      ).find((el) => (el.textContent || "").includes(text));
      if (!bubble || !marker) return { ok: false, reason: "missing node" };
      const relation = bubble.compareDocumentPosition(marker);
      // Node.DOCUMENT_POSITION_FOLLOWING === 4: marker follows bubble in
      // document order — i.e. bubble is the earlier Henry event, marker
      // is the later fleet event. Any swap flips this bit.
      return {
        ok: (relation & Node.DOCUMENT_POSITION_FOLLOWING) !== 0,
        relation,
      };
    }, COLLISION_TEXT);
    assert(
      collisionOwnership.ok,
      `collision bubble must precede marker in DOM order (Henry earlier, fleet later); ` +
        `saw relation=${collisionOwnership.relation ?? collisionOwnership.reason}`,
    );

    // R2 R2: stale-window fallback. An old sourced composer (echoed_at ~1h
    // before this transcript) whose real event has fallen out of the
    // trimmed window must NOT tag a later identical Henry-typed row. If
    // the upper timestamp bound is missing, the late Henry row is
    // silently converted into a fleet marker and no Henry bubble remains.
    // The composer still surfaces itself as a synthesized marker (that is
    // the intended archived-render path); the failure signature is a
    // missing Henry bubble, not a missing composer marker.
    const staleBubble = await page
      .locator(".session-user", { hasText: STALE_FLEET_TEXT })
      .count();
    assert(
      staleBubble === 1,
      `late Henry row with stale composer match must render as a bubble; saw ${staleBubble}`,
    );
    // The bubble must render AFTER the stale composer's synthesized marker
    // in DOM order (Henry row is at 18:00:40, composer's echoed_at is
    // ~1h earlier). This proves the composer did not steal the bubble.
    const staleOrdering = await page.evaluate((text) => {
      const bubble = Array.from(document.querySelectorAll(".session-user"))
        .find((el) => (el.textContent || "").includes(text));
      const marker = Array.from(
        document.querySelectorAll(
          "[data-testid='session-synthetic-source'][data-source='fleet-monitor']",
        ),
      ).find((el) => (el.textContent || "").includes(text));
      if (!bubble || !marker) return { ok: false, reason: "missing node" };
      const relation = marker.compareDocumentPosition(bubble);
      return {
        ok: (relation & Node.DOCUMENT_POSITION_FOLLOWING) !== 0,
        relation,
      };
    }, STALE_FLEET_TEXT);
    assert(
      staleOrdering.ok,
      `stale-window Henry bubble must follow the archived composer marker; ` +
        `saw ${staleOrdering.relation ?? staleOrdering.reason}`,
    );

    // R2 R4 (archived rendering): the archived supervisor-steer composer
    // has no matching transcript event, so the frontend synthesizes a
    // marker from composer_messages alone. Same MessageBlock render path
    // an archived normalized user turn with payload.source takes.
    const archivedMarker = page.locator(
      "[data-testid='session-synthetic-source'][data-source='supervisor-steer']",
      { hasText: ARCHIVED_STEER_TEXT },
    );
    await archivedMarker.waitFor({ state: "attached" });
    assert(
      (await archivedMarker.count()) === 1,
      "archived-source composer must render exactly one synthetic marker",
    );
    const archivedAsBubble = await page
      .locator(".session-user", { hasText: ARCHIVED_STEER_TEXT })
      .count();
    assert(
      archivedAsBubble === 0,
      "archived-source composer must not render as a Henry bubble",
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

    // R3 review R2: real archived-session case. Navigate to the ticket with
    // NO registry current row — the backend must route through
    // main._archived_events_payload() → transcripts.read_session_delta(
    // "codex-normalized", ...) → the WIKI-161 source-stamping in
    // transcripts._stamp_last_user_event_source(). The archived sourced
    // user event must render as a marker, the unsourced ones as bubbles,
    // and the assistant message as normal assistant content.
    logStep("navigating to archived session");
    await page.goto(`${backend.baseUrl}/#/agent/${ARCHIVED_TICKET}`, {
      waitUntil: "domcontentloaded",
    });
    await page.locator(".session-scroll").waitFor();
    await page.waitForFunction(
      ({ henry, steer, assistant }) => {
        const bodies = Array.from(document.querySelectorAll(".session-scroll *"))
          .map((el) => el.textContent || "")
          .join(" ");
        return (
          bodies.includes(henry) &&
          bodies.includes(steer) &&
          bodies.includes(assistant)
        );
      },
      {
        henry: ARCHIVED_ROW_HENRY_TEXT,
        steer: ARCHIVED_ROW_STEER_TEXT,
        assistant: ARCHIVED_ROW_ASSISTANT_TEXT,
      },
    );

    const archivedHenryBubble = await page
      .locator(".session-user", { hasText: ARCHIVED_ROW_HENRY_TEXT })
      .count();
    assert(
      archivedHenryBubble === 1,
      `archived unsourced user event must render as a bubble; saw ${archivedHenryBubble}`,
    );
    const archivedSteerBubble = await page
      .locator(".session-user", { hasText: ARCHIVED_ROW_STEER_TEXT })
      .count();
    assert(
      archivedSteerBubble === 0,
      `archived sourced user event must NOT render as a bubble; saw ${archivedSteerBubble}`,
    );
    const archivedSteerMarker = page.locator(
      "[data-testid='session-synthetic-source'][data-source='supervisor-steer']",
      { hasText: ARCHIVED_ROW_STEER_TEXT },
    );
    await archivedSteerMarker.waitFor({ state: "attached" });
    assert(
      (await archivedSteerMarker.count()) === 1,
      "archived sourced user event must render as a synthetic marker row",
    );

    // Confirm the backend actually served this via the archive path — the
    // format string identifies which branch produced the payload, so a
    // regression that stopped routing archived requests would surface
    // here even before the DOM check.
    const archivedSessionPayload = await page.evaluate(
      async (ticket) => (await fetch(`/api/agents/${ticket}/session`)).json(),
      ARCHIVED_TICKET,
    );
    assert(
      archivedSessionPayload.format === "provider-events",
      `archived route must return format=provider-events; saw ${archivedSessionPayload.format}`,
    );
    const archivedUserEvents = (archivedSessionPayload.events || []).filter(
      (event) => event.kind === "user",
    );
    const sourceTaggedArchivedEvents = archivedUserEvents.filter(
      (event) => event.source === "supervisor-steer",
    );
    assert(
      sourceTaggedArchivedEvents.length === 1,
      `archived events.jsonl source must land on exactly one SessionEvent; saw ${sourceTaggedArchivedEvents.length}`,
    );

    await page.screenshot({
      path: path.join(OUT_DIR, "wiki-161-archived-session.png"),
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
