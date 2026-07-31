// Round-5 evidence: one screenshot per card / dialog family listed in the
// PR family checklist. Captures the isolated backend flow with a live
// worker + orchestrator + history row so reviewers can verify the density
// rules apply uniformly.
import fs from "node:fs/promises";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

import { makeFixtureRoot, startBackend } from "../scripts/wiki32-harness.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const OUT_DIR = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "evidence",
  "wiki-154",
);
const ORCH = "wiki-orch";
const WORKER = "WIKI-42";

function line(value) {
  return `${JSON.stringify(value)}\n`;
}

async function startFakeSupervisor(fixtures, registry) {
  const subscribers = new Set();
  const server = net.createServer((socket) => {
    let buffer = "";
    socket.on("data", async (chunk) => {
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
      const runs = Object.values(registry)
        .filter((entry) => typeof entry === "object" && entry?.current)
        .map((entry) => ({ ...entry.current, control_attached: true, provider_alive: true }));
      let result;
      if (method === "ping") result = { status: "ok", pid: process.pid };
      else if (method === "run/list") result = { status: "ok", pid: process.pid, runs };
      else if (method === "run/queue") result = { messages: [] };
      else if (method === "events/read")
        result = {
          run_id: runs[0]?.run_id ?? null,
          provider: "codex",
          state: "working",
          raw_count: 0,
          normalized_count: 0,
          dispositions: { rendered: 0, summarized: 0, ignored: 0, unknown: 0 },
          pending_requests: [],
          events: [],
          raw: null,
        };
      else result = { messages: [] };
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

async function seedHistoryEntry(fixtures) {
  const archiveRoot = path.join(fixtures.root, "archive", "WIKI-9");
  const session = path.join(archiveRoot, "20260730-180000");
  await fs.mkdir(session, { recursive: true });
  await fs.writeFile(
    path.join(session, "meta.json"),
    JSON.stringify({
      worker: {
        ticket: "WIKI-9",
        kind: "cdx",
        role: "review",
        model: "gpt-5.6-sol",
        orch: null,
      },
    }),
  );
  await fs.writeFile(
    path.join(session, "final-status.json"),
    JSON.stringify({
      outcome: "merged",
      state: "merge-ready",
      pr: "https://github.com/example/pr/9",
      step: "shipped",
    }),
  );
}

async function main() {
  await fs.mkdir(OUT_DIR, { recursive: true });
  const fixtures = makeFixtureRoot("wiki-154-round5-shots-");
  const transcript = path.join(fixtures.root, "claude-native-surfaces.jsonl");
  const raw = path.join(fixtures.root, "raw.jsonl");
  await fs.copyFile(
    path.join(ROOT, "backend", "tests", "fixtures", "claude_native_surfaces.jsonl"),
    transcript,
  );
  await fs.writeFile(raw, "");
  const registry = {
    _orchestrators: {
      [ORCH]: {
        id: ORCH,
        window: null,
        cwd: fixtures.root,
        kind: "cc",
        model: "opus",
        session_id: "orch-session-1",
      },
    },
    [WORKER]: {
      history: [],
      current: {
        ticket: WORKER,
        run_id: "00000000-0000-4000-8000-000000000042",
        provider: "codex",
        kind: "cdx",
        role: "implement",
        model: "gpt-5.4",
        effort: "high",
        worktree: fixtures.root,
        cwd: fixtures.root,
        orch: ORCH,
        state: "working",
        provider_session_id: "worker-session-42",
        provider_pid: process.pid,
        control_attached: true,
        transcript,
        log: raw,
        window: null,
        spawned_at: "2026-07-31T18:00:00Z",
      },
    },
  };
  await fs.writeFile(fixtures.registryPath, JSON.stringify(registry, null, 2));
  await fs.writeFile(fixtures.queuePath, "{}\n");
  await seedHistoryEntry(fixtures);
  const supervisor = await startFakeSupervisor(fixtures, registry);
  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });

  try {
    // 1. Live worker card (default).
    await page.goto(`${backend.baseUrl}/#/agents`, { waitUntil: "domcontentloaded" });
    await page.locator(".agent-card").first().waitFor();
    await page.screenshot({
      path: path.join(OUT_DIR, "round5-01-worker-card-default.png"),
      fullPage: false,
    });

    // 2. Worker card overflow menu open.
    await page
      .getByRole("button", { name: new RegExp(`More actions for ${WORKER}`) })
      .click();
    await page.locator(".agent-card-menu-popover").waitFor();
    await page.screenshot({
      path: path.join(OUT_DIR, "round5-02-worker-card-menu.png"),
      fullPage: false,
    });
    await page.keyboard.press("Escape");
    await page.locator(".agent-card-menu-popover").waitFor({ state: "detached" });

    // 3. Orchestrator row default + overflow menu open (shows density rule
    //    applied to orch family).
    await page
      .getByRole("button", { name: new RegExp(`More actions for ${ORCH}`) })
      .click();
    await page.locator(".agent-card-menu-popover").waitFor();
    await page.screenshot({
      path: path.join(OUT_DIR, "round5-03-orchestrator-row-menu.png"),
      fullPage: false,
    });
    await page.keyboard.press("Escape");
    await page.locator(".agent-card-menu-popover").waitFor({ state: "detached" });

    // 4. History row default (quiet outcome/date + View transcript).
    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
    await page.locator(".agent-card.is-archived").first().waitFor();
    await page.screenshot({
      path: path.join(OUT_DIR, "round5-04-history-row.png"),
      fullPage: true,
    });

    // 5. Spawn worker dialog default (Ticket + Title + Kickoff lead).
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.getByRole("button", { name: /^Spawn worker$/ }).click();
    let dlg = page.getByRole("dialog");
    await dlg.waitFor();
    await page.screenshot({
      path: path.join(OUT_DIR, "round5-05-spawn-worker-default.png"),
      fullPage: false,
    });
    await page.keyboard.press("Escape");
    await dlg.waitFor({ state: "detached" });

    // 6. Spawn orchestrator dialog default (Name + Goal lead).
    await page.getByRole("button", { name: /^Spawn orchestrator$/ }).click();
    dlg = page.getByRole("dialog");
    await dlg.waitFor();
    await page.screenshot({
      path: path.join(OUT_DIR, "round5-06-spawn-orchestrator-default.png"),
      fullPage: false,
    });
    // 7. Spawn orchestrator Advanced open (Project dir + Provider + Model +
    //    Effort revealed).
    await dlg.getByRole("button", { name: /Advanced/ }).click();
    await dlg.getByLabel("Provider").waitFor();
    await page.screenshot({
      path: path.join(OUT_DIR, "round5-07-spawn-orchestrator-advanced.png"),
      fullPage: false,
    });
  } finally {
    await page.close();
    await browser.close();
    await backend.stop();
    await supervisor.stop();
  }
}

await main();
