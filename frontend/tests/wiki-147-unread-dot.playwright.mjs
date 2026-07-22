import assert from "node:assert/strict";
import fs from "node:fs/promises";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

import { makeFixtureRoot, startBackend } from "../scripts/wiki32-harness.mjs";

const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || "/tmp/wiki-147-unread-dot";
const WORKER = "WIKI-147";

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

async function seedStatus(fixtures, ticket, mtimeSeconds) {
  const statusPath = path.join(fixtures.statusDir, `${ticket}.json`);
  await fs.writeFile(
    statusPath,
    JSON.stringify({ state: "working", pr: null, step: "editing", blocker: null }),
    "utf-8",
  );
  await fs.utimes(statusPath, mtimeSeconds, mtimeSeconds);
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
  await page.evaluate(async () => {
    await fetch("/api/agents", { cache: "no-store" });
  });
}

async function main() {
  await fs.mkdir(OUT_DIR, { recursive: true });
  const fixtures = makeFixtureRoot("wiki-147-unread-dot-");
  await fs.writeFile(fixtures.queuePath, "{}\n");

  const liveWorker = {
    ticket: WORKER,
    run_id: "00000000-0000-4000-8000-000000000147",
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

  // Seed timestamps in the past so the click's real "now" viewed-mark clears
  // the initial dot, and a follow-up event lands in the past-but-later window.
  const nowSeconds = Math.floor(Date.now() / 1000);
  const initialEvent = nowSeconds - 3600;
  const initialViewedIso = new Date((initialEvent - 3600) * 1000).toISOString();
  await fs.writeFile(
    path.join(fixtures.root, "agent-viewed.json"),
    JSON.stringify({ [WORKER]: initialViewedIso }),
    "utf-8",
  );
  await seedStatus(fixtures, WORKER, initialEvent);
  await fs.writeFile(fixtures.registryPath, JSON.stringify(registry, null, 2));

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
    await page.goto(`${backend.baseUrl}/`, { waitUntil: "domcontentloaded" });
    await page.locator(".nav-agents").waitFor();
    await page.locator(`.nav-agent .nav-agent-ticket:has-text("${WORKER}")`).waitFor();
    await waitForUnread(page, WORKER, 1, "initial fetch should show unread dot");
    await page.screenshot({ path: path.join(OUT_DIR, "1-unread.png"), fullPage: false });

    // Open the session by clicking the row; the sidebar clears optimistically
    // and posts the viewed mark to the backend.
    await page.locator(`.nav-agent:has(.nav-agent-ticket:has-text("${WORKER}"))`).click();
    await waitForUnread(page, WORKER, 0, "click should clear unread optimistically");
    await page.screenshot({ path: path.join(OUT_DIR, "2-cleared.png"), fullPage: false });

    // Confirm the backend persisted the viewed timestamp.
    const viewedAfterClick = await page.evaluate(async () => {
      const res = await fetch("/api/agents", { cache: "no-store" });
      const body = await res.json();
      const row = body.workers.find((worker) => worker.ticket === "WIKI-147");
      return row?.last_viewed_at ?? null;
    });
    assert.ok(viewedAfterClick, "backend should have recorded last_viewed_at after open");

    // Simulate a fresh event landing after the user viewed the session — pin
    // the mtime slightly in the future so it always exceeds the recorded view.
    const newerEvent = Math.floor(Date.now() / 1000) + 30;
    await seedStatus(fixtures, WORKER, newerEvent);
    await pokeAgents(page);

    const agentsAfter = await page.evaluate(async () => {
      const res = await fetch("/api/agents", { cache: "no-store" });
      const body = await res.json();
      return body.workers.find((worker) => worker.ticket === "WIKI-147");
    });
    assert.ok(
      agentsAfter.latest_event_at > agentsAfter.last_viewed_at,
      `expected latest_event_at > last_viewed_at, got ${JSON.stringify(agentsAfter)}`,
    );

    // Reload the page to refetch the sidebar; the dot should return because a
    // new event landed after the recorded view.
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.locator(`.nav-agent .nav-agent-ticket:has-text("${WORKER}")`).waitFor();
    // The row is not the active ticket on reload (we cleared the layout), so
    // the automatic mark-viewed effect will not fire and the dot should reappear.
    await waitForUnread(page, WORKER, 1, "new event should re-show the unread dot after reload");
    await page.screenshot({ path: path.join(OUT_DIR, "3-reappeared.png"), fullPage: false });

    await fs.writeFile(
      path.join(OUT_DIR, "summary.json"),
      JSON.stringify(
        {
          worker: WORKER,
          screenshots: ["1-unread.png", "2-cleared.png", "3-reappeared.png"],
          latest_event_at: agentsAfter.latest_event_at,
          last_viewed_at: agentsAfter.last_viewed_at,
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
