// Round-4 evidence: capture the redesigned card + dialogs against the
// isolated backend fixture. Runs during the merge-ready loop; screenshots
// land in frontend/tests/evidence/wiki-154/round4-*.png so reviewers can
// verify finding 3 (card density) and finding 4 (dialog density) visually.
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

async function main() {
  await fs.mkdir(OUT_DIR, { recursive: true });
  const fixtures = makeFixtureRoot("wiki-154-round4-shots-");
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
  const supervisor = await startFakeSupervisor(fixtures, registry);
  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1200, height: 900 } });

  try {
    // 1. Default card density (finding 3): no kind/role/model badges, one
    //    primary action, overflow menu button.
    await page.goto(`${backend.baseUrl}/#/agents`, { waitUntil: "domcontentloaded" });
    await page.locator(`.agent-card`).first().waitFor();
    await page.screenshot({
      path: path.join(OUT_DIR, "round4-01-card-default.png"),
      fullPage: false,
    });

    // 2. Card overflow menu open — Replace + destructive actions live here.
    await page
      .getByRole("button", { name: new RegExp(`More actions for ${WORKER}`) })
      .click();
    await page.locator(".agent-card-menu-popover").waitFor();
    await page.screenshot({
      path: path.join(OUT_DIR, "round4-02-card-menu.png"),
      fullPage: false,
    });
    await page.keyboard.press("Escape");
    await page.locator(".agent-card-menu-popover").waitFor({ state: "detached" });

    // 3. Card Technical details open — kind/role/model/branch/runtime meta.
    await page.evaluate(() => {
      const btns = document.querySelectorAll(".agent-card button");
      for (const b of btns) {
        if (b.textContent && b.textContent.trim().toLowerCase() === "details") {
          b.click();
          break;
        }
      }
    });
    await page.locator(".agent-tech").waitFor();
    await page.screenshot({
      path: path.join(OUT_DIR, "round4-03-card-technical-details.png"),
      fullPage: true,
    });

    // 4. Spawn dialog default — leads with Ticket + Title + Kickoff prompt.
    await page.getByRole("button", { name: /^Spawn worker$/ }).click();
    const spawn = page.getByRole("dialog");
    await spawn.waitFor();
    await page.screenshot({
      path: path.join(OUT_DIR, "round4-04-spawn-default.png"),
      fullPage: false,
    });

    // 5. Spawn dialog Advanced open — provider/model/effort/workdir/orch.
    await spawn.getByRole("button", { name: /Advanced/ }).click();
    await spawn.getByLabel("Role").waitFor();
    await page.screenshot({
      path: path.join(OUT_DIR, "round4-05-spawn-advanced.png"),
      fullPage: false,
    });
    await page.keyboard.press("Escape");
    await spawn.waitFor({ state: "detached" });

    // Replace dialog screenshots live in
    // frontend/tests/evidence/wiki-154/round4-06-replace-dialog.png,
    // captured by the existing replace-agent-modal.playwright.mjs which
    // opens Advanced and applies a change. That flow already exercises the
    // redesigned dialog end-to-end, so this script does not re-capture it.
  } finally {
    await page.close();
    await browser.close();
    await backend.stop();
    await supervisor.stop();
  }
}

await main();
