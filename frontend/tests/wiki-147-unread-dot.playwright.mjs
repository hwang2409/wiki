import assert from "node:assert/strict";
import fs from "node:fs/promises";
import net from "node:net";
import path from "node:path";
import { chromium } from "playwright";

import { makeFixtureRoot, startBackend } from "../scripts/wiki32-harness.mjs";

const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || "/tmp/wiki-147-unread-dot";
const WORKER = "WIKI-147";
const RUN_ID = "00000000-0000-4000-8000-000000000147";

function line(value) {
  return `${JSON.stringify(value)}\n`;
}

/**
 * Fake supervisor speaking the same UDS wire protocol as the real one — respond
 * to `events/subscribe` with `{result: {subscribed: true}}` on the same line the
 * request arrived, then hold the socket open and stream `{event: {...}}\n`
 * frames whenever `emit()` is called. Non-subscription requests get one-shot
 * responses like the previous fixture.
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
          /* subscriber already gone */
        }
      }
    },
    async waitForSubscriber(timeoutMs = 5_000) {
      const deadline = Date.now() + timeoutMs;
      while (subscribers.size === 0) {
        if (Date.now() >= deadline) {
          throw new Error("timed out waiting for supervisor subscriber");
        }
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

/**
 * Simulate a real provider event landing in the durable supervisor store.
 * This is the SAME data path the production supervisor writes into after
 * every `RunStore.append_normalized(...)` — the backend derives freshness
 * from `run.json` (WIKI-147 R2 B2 contract). Tests must NOT touch the
 * status file mtime as a proxy for freshness.
 */
async function appendDurableEvent(fixtures, runId, seq, createdAt) {
  const runDir = path.join(fixtures.runtimeDir, "runs", runId);
  await fs.mkdir(runDir, { recursive: true });
  const runPath = path.join(runDir, "run.json");
  const updatedAt = new Date().toISOString();
  const payload = {
    run_id: runId,
    normalized_event_count: seq,
    updated_at: updatedAt,
  };
  if (createdAt) payload.created_at = createdAt;
  await fs.writeFile(runPath, JSON.stringify(payload), "utf-8");
  return updatedAt;
}

async function seedStatus(fixtures, ticket) {
  const statusPath = path.join(fixtures.statusDir, `${ticket}.json`);
  await fs.writeFile(
    statusPath,
    JSON.stringify({ state: "working", pr: null, step: "editing", blocker: null }),
    "utf-8",
  );
  return statusPath;
}

async function unreadCount(page, ticket) {
  return page.evaluate((t) => {
    const rows = Array.from(document.querySelectorAll(".nav-agent"));
    const row = rows.find((el) => el.querySelector(".nav-agent-ticket")?.textContent === t);
    if (!row) return -1;
    return row.querySelectorAll('[data-testid="nav-agent-unread"]').length;
  }, ticket);
}

async function waitForUnread(page, ticket, want, message, timeoutMs = 5_000) {
  const deadline = Date.now() + timeoutMs;
  let last = null;
  while (Date.now() < deadline) {
    last = await unreadCount(page, ticket);
    if (last === want) return;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error(`${message}: expected unread=${want}, saw=${last}`);
}

async function pokeAgents(page) {
  return page.evaluate(async () => {
    const res = await fetch("/api/agents", { cache: "no-store" });
    return res.json();
  });
}

async function main() {
  await fs.mkdir(OUT_DIR, { recursive: true });
  const fixtures = makeFixtureRoot("wiki-147-unread-dot-");
  await fs.writeFile(fixtures.queuePath, "{}\n");

  // Freeze the deploy cutoff BEFORE the backend boots so we can classify the
  // seeded run as post-deploy by writing its created_at strictly after this
  // timestamp. Matches the WIKI-147 R2 B1 contract: the marker is stamped at
  // startup, not on first request.
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
  await seedStatus(fixtures, WORKER);

  const supervisor = await startFakeSupervisor(fixtures, [liveWorker]);
  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 960 } });

  try {
    await page.addInitScript(() => {
      localStorage.setItem("wiki-sidebar-visible", "true");
      localStorage.setItem("wiki-sidebar-tab", "agents");
      localStorage.removeItem("wiki-window-layout-v2");
    });

    // Scenario 1: a run whose created_at is strictly AFTER the deploy cutoff
    // must render unread on first paint. WIKI-147 R2 B1 contract.
    await appendDurableEvent(fixtures, RUN_ID, 1, runCreatedAt);
    await page.goto(`${backend.baseUrl}/`, { waitUntil: "domcontentloaded" });
    await page.locator(".nav-agents").waitFor();
    await page.locator(`.nav-agent .nav-agent-ticket:has-text("${WORKER}")`).waitFor();
    await waitForUnread(page, WORKER, 1, "post-deploy session must show unread on first paint");
    await page.screenshot({ path: path.join(OUT_DIR, "1-unread.png"), fullPage: false });

    // Backend must have subscribed to the fake supervisor once the browser's
    // EventSource opened. That's the plumbing we're about to exercise in
    // scenario 3.
    await supervisor.waitForSubscriber();

    // Scenario 2: open the row → optimistic clear + POST /viewed with the
    // observed seq. Dot must disappear without waiting for the network.
    await page.locator(`.nav-agent:has(.nav-agent-ticket:has-text("${WORKER}"))`).click();
    await waitForUnread(page, WORKER, 0, "click should clear unread optimistically");
    await page.screenshot({ path: path.join(OUT_DIR, "2-cleared.png"), fullPage: false });

    const persisted = await pokeAgents(page);
    const rowAfterView = persisted.workers.find((worker) => worker.ticket === WORKER);
    assert.equal(rowAfterView.last_viewed_seq, 1, "backend must persist last_viewed_seq");
    assert.equal(rowAfterView.latest_event_seq, 1);

    // Navigate away from the ticket so the sidebar's active-ticket effect
    // does NOT auto-mark it viewed when the next event arrives. Without
    // this, the dot would flash and disappear before the assertion runs.
    // The activity route deliberately has no run_id, so no mark-viewed
    // POST is scheduled.
    await page.evaluate(() => {
      window.location.hash = "#/activity";
    });
    await page.waitForFunction(() => {
      const active = document.querySelector(".nav-agent.is-active");
      return active === null;
    });

    // Scenario 3: exercise the FULL supervisor -> SSE -> UI re-fetch path,
    // WITHOUT a page reload. This is the R2 H2 contract — the previous
    // rewrite mutated run.json + reloaded, so a regression that broke SSE
    // freshness (like R1 B2) could silently pass. Now:
    //   supervisor.emit(session event)
    //     -> backend's /api/events forwards it
    //     -> App.tsx onmessage with type='session'
    //     -> refreshTick advances
    //     -> AgentsSidebar re-fetches /api/agents
    //     -> row's latest_event_seq advances past last_viewed_seq
    //     -> dot re-appears
    // The durable seq bump is written FIRST so the re-fetch has fresh data.
    await appendDurableEvent(fixtures, RUN_ID, 2, runCreatedAt);
    supervisor.emit({
      type: "session",
      ticket: WORKER,
      surface: "agents",
      run_id: RUN_ID,
    });

    await waitForUnread(
      page,
      WORKER,
      1,
      "SSE-delivered session event must re-show the unread dot without a page reload",
      8_000,
    );
    await page.screenshot({ path: path.join(OUT_DIR, "3-reappeared.png"), fullPage: false });

    // Scenario 4: a11y — the accessible name of the row includes "unread"
    // when the dot is present, so a screen reader announces it.
    const accessibleName = await page.evaluate((t) => {
      const rows = Array.from(document.querySelectorAll(".nav-agent"));
      const row = rows.find((el) => el.querySelector(".nav-agent-ticket")?.textContent === t);
      return row?.textContent?.trim() ?? "";
    }, WORKER);
    assert.ok(
      accessibleName.includes("unread"),
      `expected accessible text to include "unread", got: ${accessibleName}`,
    );

    // Scenario 5: POST /viewed with an unknown run_id must 404.
    const notFoundStatus = await page.evaluate(async () => {
      const res = await fetch("/api/agents/runs/00000000-0000-4000-8000-999999999999/viewed", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      return res.status;
    });
    assert.equal(notFoundStatus, 404, "POST /viewed for unknown run_id must 404");

    await fs.writeFile(
      path.join(OUT_DIR, "summary.json"),
      JSON.stringify(
        {
          worker: WORKER,
          run_id: RUN_ID,
          deploy_timestamp: deployAt,
          run_created_at: runCreatedAt,
          screenshots: ["1-unread.png", "2-cleared.png", "3-reappeared.png"],
          scenarios: [
            "post-deploy new session shows unread on first paint",
            "open clears dot optimistically + server persists",
            "SSE session event advances seq and re-shows dot (no reload)",
            "accessible name includes 'unread'",
            "unknown run_id -> 404",
          ],
        },
        null,
        2,
      ),
    );
    console.error("[wiki-147-playwright] unread dot flow passed");
  } finally {
    await page.close();
    await browser.close();
    await backend.stop();
    await supervisor.stop();
  }
}

await main();
