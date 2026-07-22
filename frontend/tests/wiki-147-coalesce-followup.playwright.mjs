import assert from "node:assert/strict";
import fs from "node:fs/promises";
import net from "node:net";
import path from "node:path";
import { chromium } from "playwright";

import { makeFixtureRoot, startBackend } from "../scripts/wiki32-harness.mjs";

const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || "/tmp/wiki-147-coalesce-followup";
const WORKER = "WIKI-147-H1";
const RUN_ID = "00000000-0000-4000-8000-000000000148";

function line(value) {
  return `${JSON.stringify(value)}\n`;
}

/**
 * Same fake supervisor shape as wiki-147-unread-dot: respond to non-SSE JSON-RPC
 * requests one-shot, hold SSE subscribers open, and stream `{event: ...}\n`
 * frames on demand.
 */
async function startFakeSupervisor(fixtures, runs) {
  const subscribers = new Set();
  const server = net.createServer((socket) => {
    let buffer = "";
    let subscribed = false;
    socket.on("data", (chunk) => {
      buffer += chunk.toString();
      let newline;
      while (!subscribed && (newline = buffer.indexOf("\n")) >= 0) {
        const requestLine = buffer.slice(0, newline);
        buffer = buffer.slice(newline + 1);
        if (!requestLine.trim()) continue;
        const request = JSON.parse(requestLine);
        const { id, method } = request;
        if (method === "events/subscribe") {
          subscribed = true;
          subscribers.add(socket);
          socket.write(line({ id, result: { subscribed: true } }));
          socket.on("close", () => subscribers.delete(socket));
          return;
        }
        const result =
          method === "ping"
            ? { status: "ok", pid: process.pid }
            : method === "run/list"
              ? { status: "ok", pid: process.pid, runs }
              : method === "run/status"
                ? runs.find((run) => run.run_id === request.params?.run_id) ?? null
                : method === "run/queue"
                  ? { messages: [] }
                  : null;
        if (result === null) {
          socket.write(line({ id, error: { type: "ValueError", message: `unsupported: ${method}` } }));
        } else {
          socket.write(line({ id, result }));
        }
        socket.end();
        return;
      }
    });
  });
  await fs.rm(fixtures.supervisorSocketPath, { force: true });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(fixtures.supervisorSocketPath, resolve);
  });
  return {
    subscribers,
    emit(event) {
      const frame = line({ event });
      for (const socket of subscribers) {
        try {
          socket.write(frame);
        } catch {
          /* subscriber gone */
        }
      }
    },
    async waitForSubscriber(timeoutMs = 5_000) {
      const deadline = Date.now() + timeoutMs;
      while (subscribers.size === 0) {
        if (Date.now() >= deadline) throw new Error("no supervisor subscriber");
        await new Promise((resolve) => setTimeout(resolve, 25));
      }
    },
    async stop() {
      for (const socket of subscribers) socket.destroy();
      await new Promise((resolve) => server.close(resolve));
      await fs.rm(fixtures.supervisorSocketPath, { force: true });
    },
  };
}

async function appendDurableEvent(fixtures, runId, seq, createdAt) {
  const runDir = path.join(fixtures.runtimeDir, "runs", runId);
  await fs.mkdir(runDir, { recursive: true });
  const payload = {
    run_id: runId,
    normalized_event_count: seq,
    updated_at: new Date().toISOString(),
  };
  if (createdAt) payload.created_at = createdAt;
  await fs.writeFile(path.join(runDir, "run.json"), JSON.stringify(payload), "utf-8");
}

async function main() {
  await fs.mkdir(OUT_DIR, { recursive: true });
  const fixtures = makeFixtureRoot("wiki-147-coalesce-followup-");
  await fs.writeFile(fixtures.queuePath, "{}\n");

  const deployAt = new Date("2026-07-21T00:00:00Z").toISOString();
  const runCreatedAt = new Date("2026-07-21T00:00:01Z").toISOString();
  await fs.writeFile(path.join(fixtures.root, "deploy-timestamp.txt"), deployAt, "utf-8");

  const liveWorker = {
    ticket: WORKER,
    run_id: RUN_ID,
    provider: "claude",
    kind: "cc",
    role: "implement",
    model: "sonnet",
    effort: null,
    worktree: fixtures.root,
    cwd: fixtures.root,
    orch: null,
    state: "working",
    control_attached: true,
    provider_session_id: "fixture-worker",
    provider_pid: process.pid,
    transcript: null,
    log: null,
    window: null,
    spawned_at: "2026-07-21T12:00:00Z",
  };
  const registry = { [WORKER]: { history: [], current: liveWorker } };
  await fs.writeFile(fixtures.registryPath, JSON.stringify(registry, null, 2));
  await fs.writeFile(
    path.join(fixtures.statusDir, `${WORKER}.json`),
    JSON.stringify({ state: "working", pr: null, step: "editing", blocker: null }),
    "utf-8",
  );

  await appendDurableEvent(fixtures, RUN_ID, 1, runCreatedAt);

  const supervisor = await startFakeSupervisor(fixtures, [liveWorker]);
  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 960 } });

  const viewedRoute = `**/api/agents/runs/${RUN_ID}/viewed`;
  // Attempts observed by the intercept — every call to markRunViewed goes
  // through page.route below, so this counts fetches, not just the ones we
  // held for coalescing. WIKI-147 R5 H1 bounds this at MAX_ATTEMPTS = 3.
  let attemptCount = 0;
  const attemptedSeqs = [];
  /** @type {{ resolve: () => void, request: import('playwright').Request } | null} */
  let pendingHold = null;
  /** @type {(() => void) | null} */
  let holdResolvedNotifier = null;

  // R7: instead of listening for network responses + sleeping, we wait for
  // the React component itself to publish a UI-owned signal that the
  // coalesce-during-flight branch actually ran. `window.__wiki147CoalesceObserved`
  // is set INSIDE the effect's `if (existing controller)` branch — so its
  // presence + targetSeq >= 12 proves React committed the bumped target
  // while the first POST was still in flight. The fresh-cycle-after-cleanup
  // path never enters that branch and never sets this signal.

  await page.route(viewedRoute, async (route) => {
    attemptCount += 1;
    const request = route.request();
    let seq = null;
    try {
      const body = JSON.parse(request.postData() ?? "{}");
      seq = typeof body?.seq === "number" ? body.seq : null;
    } catch {
      /* body omitted */
    }
    attemptedSeqs.push(seq);
    if (attemptCount === 1) {
      // Hold the first attempt so the SSE burst can bump targetSeq while
      // this POST is still in flight. That's the exact R4 coalesce → R5
      // follow-up race we need to regress-guard.
      await new Promise((resolve) => {
        pendingHold = { resolve, request };
        if (holdResolvedNotifier) holdResolvedNotifier();
      });
    }
    await route.continue();
  });

  try {
    await page.addInitScript(() => {
      localStorage.setItem("wiki-sidebar-visible", "true");
      localStorage.setItem("wiki-sidebar-tab", "agents");
      localStorage.removeItem("wiki-window-layout-v2");
    });
    await page.goto(`${backend.baseUrl}/`, { waitUntil: "domcontentloaded" });
    await page.locator(".nav-agents").waitFor();
    await page
      .locator(`.nav-agent .nav-agent-ticket:has-text("${WORKER}")`)
      .waitFor();

    // Click the row to make it the activeTicket so the mark-viewed effect
    // will fire. This kicks off the first (held) POST for seq=1.
    await page.locator(`.nav-agent:has(.nav-agent-ticket:has-text("${WORKER}"))`).click();
    await supervisor.waitForSubscriber();

    // Wait for the first POST to arrive at our route intercept.
    await new Promise((resolve) => {
      if (pendingHold) {
        resolve();
      } else {
        holdResolvedNotifier = resolve;
      }
    });
    assert.equal(attemptCount, 1, "first POST must be in flight before the burst");
    assert.equal(attemptedSeqs[0], 1, "initial POST targets seq=1");

    // SSE burst: bump seq to 4, 7, 12 while the first POST is held.
    // Each bump advances the runs/<id>/run.json normalized_event_count AND
    // fires an SSE session event so the UI refetches, feeding the effect a
    // higher observedSeq. R4 coalescing collapses this into a single
    // in-flight controller with targetSeq=12.
    for (const seq of [4, 7, 12]) {
      await appendDurableEvent(fixtures, RUN_ID, seq, runCreatedAt);
      supervisor.emit({ type: "session", ticket: WORKER, surface: "agents", run_id: RUN_ID });
      // Small yield so the SSE event dispatches an /api/agents fetch.
      await new Promise((resolve) => setTimeout(resolve, 40));
    }

    // R7 tightening: wait for the React component to publish the coalesce
    // signal with targetSeq >= 12 WHILE the first POST is still held. This
    // is a UI-owned signal (set inside the effect's `if (existing controller)`
    // branch, in the same synchronous tick that bumps existing.targetSeq).
    // It fires only on the coalesce-during-flight path — the pre-R5 buggy
    // path (fresh POST after controller cleanup) never enters that branch
    // and never sets this signal.
    assert.ok(pendingHold, "first POST must still be held before UI signals coalesce=12");
    const observeDeadline = Date.now() + 6_000;
    let coalesceSignal = null;
    while (Date.now() < observeDeadline) {
      coalesceSignal = await page.evaluate(() => window.__wiki147CoalesceObserved ?? null);
      if (coalesceSignal && coalesceSignal.targetSeq >= 12) break;
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    assert.ok(
      coalesceSignal && coalesceSignal.targetSeq >= 12,
      `React coalesce signal must reach targetSeq>=12 while first POST held. saw=${JSON.stringify(coalesceSignal)}`,
    );
    assert.equal(
      coalesceSignal.runId,
      RUN_ID,
      `coalesce signal must be for our run. saw=${JSON.stringify(coalesceSignal)}`,
    );
    assert.ok(
      coalesceSignal.count >= 1,
      `coalesce branch must have run at least once. saw=${JSON.stringify(coalesceSignal)}`,
    );
    assert.ok(pendingHold, "first POST must still be held once coalesce signal fires");
    // Attempt count must still be 1 — during the held first POST the coalesce
    // path updates targetSeq inside the inflight controller, it must NOT fire
    // a new POST. If it did, that would be the pre-R5 double-POST bug.
    assert.equal(
      attemptCount,
      1,
      `no second POST may fire while first is held (coalesce in-flight). attempts=${JSON.stringify(attemptedSeqs)}`,
    );

    // Now release the held POST — the R5 follow-up path must observe
    // targetSeq(12) > seq(1) after success and issue exactly one
    // coalesced follow-up POST at seq=12.
    assert.ok(pendingHold, "expected first POST still held");
    pendingHold.resolve();
    pendingHold = null;

    // Wait for the server to persist seq=12 (or higher via the SSE stream).
    const deadline = Date.now() + 8_000;
    let lastServer = null;
    while (Date.now() < deadline) {
      const snapshot = await page.evaluate(async () => {
        const res = await fetch("/api/agents", { cache: "no-store" });
        return res.json();
      });
      const row = snapshot.workers.find((worker) => worker.ticket === WORKER);
      lastServer = row?.last_viewed_seq ?? null;
      if (lastServer !== null && lastServer >= 12) break;
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    assert.equal(
      lastServer,
      12,
      `follow-up POST must persist coalesced max seq. saw last_viewed_seq=${lastServer}, attempts=${JSON.stringify(attemptedSeqs)}`,
    );

    // Full coalesce contract: observed POST sequence must be EXACTLY
    // [1, 12] — the initial in-flight POST at seq=1, then one coalesced
    // follow-up at the SSE max seq=12. No third POST, no reordering, no
    // intermediate seq. This is strictly tighter than the earlier
    // "<=3 attempts and includes(12)" formulation, which allowed
    // [1, 12, 12] and other pathological retry chains to pass.
    assert.deepStrictEqual(
      attemptedSeqs,
      [1, 12],
      `POST sequence must be exactly [1, 12]. saw ${JSON.stringify(attemptedSeqs)}`,
    );

    // The failed-indicator must NOT appear — the burst was fully absorbed.
    const failedCount = await page.evaluate(
      (t) => {
        const rows = Array.from(document.querySelectorAll(".nav-agent"));
        const row = rows.find((el) => el.querySelector(".nav-agent-ticket")?.textContent === t);
        return row?.querySelectorAll('[data-testid="nav-agent-viewed-failed"]').length ?? -1;
      },
      WORKER,
    );
    assert.equal(failedCount, 0, "successful follow-up must not raise the failed indicator");

    await fs.writeFile(
      path.join(OUT_DIR, "summary.json"),
      JSON.stringify(
        {
          worker: WORKER,
          attempts: attemptCount,
          attempted_seqs: attemptedSeqs,
          server_last_viewed_seq: lastServer,
        },
        null,
        2,
      ),
    );
    console.error("[wiki-147-coalesce-followup] passed");
  } finally {
    if (pendingHold) pendingHold.resolve();
    await page.unroute(viewedRoute).catch(() => {});
    await page.close();
    await browser.close();
    await backend.stop();
    await supervisor.stop();
  }
}

await main();
