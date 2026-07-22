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

async function startFakeSupervisor(fixtures, runs) {
  const subscribers = new Set();
  const server = net.createServer((socket) => {
    let buffer = "";
    socket.on("data", (chunk) => {
      buffer += chunk.toString();
      const newline = buffer.indexOf("\n");
      if (newline < 0) return;
      const request = JSON.parse(buffer.slice(0, newline));
      const { id, method } = request;
      if (method === "events/subscribe") {
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

/**
 * Simulate a real provider event landing in the durable supervisor store.
 * This is the SAME data path the production supervisor writes into after
 * every `RunStore.append_normalized(...)` — the backend derives freshness
 * from `run.json` (WIKI-147 R2 B2 contract). Tests must NOT touch the
 * status file mtime as a proxy for freshness.
 */
async function appendDurableEvent(fixtures, runId, seq) {
  const runDir = path.join(fixtures.runtimeDir, "runs", runId);
  await fs.mkdir(runDir, { recursive: true });
  const runPath = path.join(runDir, "run.json");
  const updatedAt = new Date().toISOString();
  await fs.writeFile(
    runPath,
    JSON.stringify({
      run_id: runId,
      normalized_event_count: seq,
      updated_at: updatedAt,
    }),
    "utf-8",
  );
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

async function waitForUnread(page, ticket, want, message) {
  const deadline = Date.now() + 5_000;
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

    // Scenario 1: session created AFTER first migration (i.e., after backend
    // has migrated the currently-empty runs dir). It must show unread on
    // first appearance — the WIKI-147 R2 B1 contract.
    await page.goto(`${backend.baseUrl}/`, { waitUntil: "domcontentloaded" });
    await page.locator(".nav-agents").waitFor();
    // The initial /api/agents call triggers the one-time migration on the
    // empty runs dir. Wait for the viewed store file to prove migration ran.
    await pokeAgents(page);
    // Now create the run in the durable store — POST-migration.
    await appendDurableEvent(fixtures, RUN_ID, 1);
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.locator(`.nav-agent .nav-agent-ticket:has-text("${WORKER}")`).waitFor();
    await waitForUnread(page, WORKER, 1, "new post-migration session must show unread");
    await page.screenshot({ path: path.join(OUT_DIR, "1-unread.png"), fullPage: false });

    // Scenario 2: open the row → optimistic clear + POST /viewed with the
    // observed seq. Dot must disappear without waiting for the network.
    await page.locator(`.nav-agent:has(.nav-agent-ticket:has-text("${WORKER}"))`).click();
    await waitForUnread(page, WORKER, 0, "click should clear unread optimistically");
    await page.screenshot({ path: path.join(OUT_DIR, "2-cleared.png"), fullPage: false });

    // Confirm the backend persisted the viewed seq.
    const persisted = await pokeAgents(page);
    const rowAfterView = persisted.workers.find((worker) => worker.ticket === WORKER);
    assert.equal(rowAfterView.last_viewed_seq, 1, "backend must persist last_viewed_seq");
    assert.equal(rowAfterView.latest_event_seq, 1);

    // Scenario 3: a REAL provider event lands in the durable store — the
    // supervisor's append_normalized path advances normalized_event_count
    // and updated_at in run.json. NO status-file mtime touch.
    await appendDurableEvent(fixtures, RUN_ID, 2);

    const advanced = await pokeAgents(page);
    const rowAdvanced = advanced.workers.find((worker) => worker.ticket === WORKER);
    assert.equal(rowAdvanced.latest_event_seq, 2);
    assert.equal(rowAdvanced.last_viewed_seq, 1);

    // Clear the URL hash + saved layout so the ticket is NOT active on
    // reload. Otherwise the active-ticket effect would immediately re-mark
    // the row viewed and hide the dot we're trying to observe.
    await page.evaluate(() => {
      history.replaceState(null, "", "/");
      localStorage.removeItem("wiki-window-layout-v2");
    });
    // Reload — sidebar must show the dot again because seq advanced past
    // the recorded viewed seq.
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.locator(`.nav-agent .nav-agent-ticket:has-text("${WORKER}")`).waitFor();
    await waitForUnread(
      page,
      WORKER,
      1,
      "real durable event should re-show the unread dot without touching status mtime",
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
          screenshots: ["1-unread.png", "2-cleared.png", "3-reappeared.png"],
          scenarios: [
            "post-migration new session shows unread",
            "open clears dot optimistically + server persists",
            "durable seq advance re-shows dot without status mtime touch",
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
