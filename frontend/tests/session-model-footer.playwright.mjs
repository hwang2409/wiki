import fs from "node:fs/promises";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

import { makeFixtureRoot, startBackend } from "../scripts/wiki32-harness.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || "/tmp/wiki-56-model-footer-evidence";
const RUN_ID = "00000000-0000-4000-8000-000000000056";
const TICKET = "WIKI-56";

function line(value) {
  return `${JSON.stringify(value)}\n`;
}

async function startFakeSupervisor(fixtures, current, registry) {
  const subscribers = new Set();
  const events = [
    {
      seq: 1,
      raw_seq: 1,
      normalized_at: "2026-07-10T17:00:00Z",
      disposition: "rendered",
      kind: "model_changed",
      payload: {
        type: "model_changed",
        from_model: "gpt-5.4",
        to_model: "gpt-5.5",
        message: "model changed to gpt-5.5",
      },
      lifecycle_state: "idle",
    },
  ];

  async function persistState() {
    registry[TICKET].current = { ...current };
    await fs.writeFile(fixtures.registryPath, JSON.stringify(registry, null, 2));
  }

  function publish(event) {
    for (const socket of subscribers) socket.write(line({ event }));
  }

  const server = net.createServer((socket) => {
    let buffer = "";
    socket.on("data", async (chunk) => {
      buffer += chunk.toString();
      const newline = buffer.indexOf("\n");
      if (newline < 0) return;
      const request = JSON.parse(buffer.slice(0, newline));
      const { id, method, params = {} } = request;
      let result;
      if (method === "events/subscribe") {
        subscribers.add(socket);
        socket.write(line({ id, result: { subscribed: true } }));
        socket.on("close", () => subscribers.delete(socket));
        return;
      }
      if (method === "ping") {
        result = { status: "ok", pid: process.pid };
      } else if (method === "run/list") {
        result = { status: "ok", pid: process.pid, runs: [{ ...current, control_attached: true, provider_alive: true }] };
      } else if (method === "run/status") {
        result = { ...current, control_attached: true, provider_alive: true };
      } else if (method === "run/queue") {
        result = { messages: [] };
      } else if (method === "events/read") {
        result = {
          run_id: RUN_ID,
          provider: "codex",
          state: current.state,
          raw_count: 1,
          normalized_count: events.length,
          dispositions: { rendered: 1, summarized: 0, ignored: 0, unknown: 0 },
          pending_requests: [],
          events,
          raw: null,
        };
      } else if (method === "run/queue_model_change") {
        current.desired_model = params.model;
        await persistState();
        result = { status: "queued", desired_model: params.model };
        publish({ type: "session", ticket: TICKET, surface: "session" });
      } else if (method === "run/cancel_model_change") {
        current.desired_model = null;
        await persistState();
        result = { status: "canceled", desired_model: null };
        publish({ type: "session", ticket: TICKET, surface: "session" });
      } else {
        socket.write(line({ id, error: { type: "ValueError", message: `unsupported fixture method: ${method}` } }));
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
  const fixtures = makeFixtureRoot("wiki-56-model-footer-");
  const transcript = path.join(fixtures.root, "codex-native-surfaces.jsonl");
  const rawLog = path.join(fixtures.root, "raw.jsonl");
  await fs.copyFile(
    path.join(ROOT, "backend", "tests", "fixtures", "codex_native_surfaces.jsonl"),
    transcript,
  );
  await fs.writeFile(rawLog, `${JSON.stringify({ seq: 1, payload: { method: "turn/started" } })}\n`);
  const current = {
    ticket: TICKET,
    run_id: RUN_ID,
    provider: "codex",
    kind: "cdx",
    role: "implement",
    model: "gpt-5.4",
    desired_model: null,
    effort: "high",
    worktree: fixtures.root,
    cwd: fixtures.root,
    orch: null,
    state: "working",
    provider_session_id: "fixture-thread-56",
    provider_pid: process.pid,
    transcript,
    log: rawLog,
    window: null,
    spawned_at: "2026-07-10T17:00:00Z",
  };
  const registry = { [TICKET]: { history: [], current } };
  await fs.writeFile(fixtures.registryPath, JSON.stringify(registry, null, 2));
  await fs.writeFile(fixtures.queuePath, "{}\n");

  const supervisor = await startFakeSupervisor(fixtures, current, registry);
  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 1100 } });

  try {
    await page.addInitScript(({ ticket }) => {
      localStorage.setItem("wiki-sidebar-visible", "false");
      localStorage.setItem(
        "wiki-window-layout-v2",
        JSON.stringify({
          version: 2,
          activeWindowId: "window-0",
          windows: [
            {
              id: "window-0",
              focusedPaneId: "pane-1",
              layout: { kind: "pane", id: "pane-1", path: `agent://${ticket}` },
            },
          ],
        }),
      );
    }, { ticket: TICKET });
    await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".session-footer");
    // WIKI-152: Run details now serialises the raw provider payload into a
    // hidden `<pre>` which contains the same substring, so `getByText` needs
    // to be exact to avoid a strict-mode DOM match on both the visible chip
    // and the diagnostics JSON dump.
    await page.getByText("model changed to gpt-5.5", { exact: true }).waitFor();

    const modelTrigger = page.getByRole("button", { name: "Change model" });
    await modelTrigger.click();
    await expectFocused(page, page.getByRole("menuitem").first());
    await page.keyboard.press("Tab");
    await page.getByRole("menu", { name: "Available models" }).waitFor({ state: "detached" });

    await modelTrigger.click();
    await expectFocused(page, page.getByRole("menuitem").first());
    await page.keyboard.press("Shift+Tab");
    await page.getByRole("menu", { name: "Available models" }).waitFor({ state: "detached" });

    await modelTrigger.click();
    await expectFocused(page, page.getByRole("menuitem").first());
    await page.keyboard.press("Escape");
    await page.getByRole("menu", { name: "Available models" }).waitFor({ state: "detached" });

    await modelTrigger.click();
    await expectFocused(page, page.getByRole("menuitem").first());
    const modelOption = page.getByRole("menuitem", { name: /GPT 5\.5/ });
    await modelOption.click();
    await page.getByText("Switch to gpt-5.5 after current turn finishes? Currently on gpt-5.4.").waitFor();
    await page.keyboard.press("Escape");
    await page.locator(".session-model-confirm").waitFor({ state: "detached" });
    await expectFocused(page, modelOption);

    await modelOption.click();
    await page.getByRole("button", { name: "Cancel" }).click();
    await expectFocused(page, modelOption);

    await modelOption.click();
    await page.getByRole("button", { name: "Switch model" }).click();
    await page.waitForFunction(async (ticket) => {
      const response = await fetch(`/api/agents/${ticket}/session`);
      const payload = await response.json();
      return payload.desired_model === "gpt-5.5";
    }, TICKET);
    await page.getByText("queued: gpt-5.5").waitFor();
    await expectFocused(page, modelTrigger);

    await page.getByRole("button", { name: "Cancel queued model change" }).click();
    await page.waitForFunction(async (ticket) => {
      const response = await fetch(`/api/agents/${ticket}/session`);
      const payload = await response.json();
      return payload.desired_model === null;
    }, TICKET);
    await page.getByText("queued: gpt-5.5").waitFor({ state: "detached" });

    await modelTrigger.click();
    await modelOption.click();
    const confirmation = page.locator(".session-model-confirm");
    await confirmation.waitFor();
    await page.setViewportSize({ width: 320, height: 900 });
    const bounds = await confirmation.boundingBox();
    if (!bounds || bounds.x < 56 || bounds.x + bounds.width > 320) {
      throw new Error(`model confirmation clips at 320px: ${JSON.stringify(bounds)}`);
    }
    await page.screenshot({ path: path.join(OUT_DIR, "session-model-footer.png"), fullPage: true });
  } finally {
    await page.close();
    await browser.close();
    await backend.stop();
    await supervisor.stop();
  }
}

async function expectFocused(page, locator) {
  const handle = await locator.elementHandle();
  if (!handle) throw new Error("expected focus target to be mounted");
  try {
    await page.waitForFunction((element) => document.activeElement === element, handle, { timeout: 5_000 });
  } catch (error) {
    const state = await page.evaluate(() => ({
      active: document.activeElement?.outerHTML,
      menu: document.querySelector(".session-model-menu")?.outerHTML,
    }));
    throw new Error(`expected focus target to have focus: ${JSON.stringify(state)}`, { cause: error });
  }
}

await main();
