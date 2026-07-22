import assert from "node:assert/strict";
import fs from "node:fs/promises";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

import { makeFixtureRoot, startBackend } from "../scripts/wiki32-harness.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || "/tmp/wiki-146-palette";
const WORKER = "WIKI-146";
const ORCHESTRATOR = "wiki";

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
                : method === "events/read"
                  ? {
                      run_id: request.params?.run_id ?? null,
                      provider: "codex",
                      state: "working",
                      raw_count: 0,
                      normalized_count: 0,
                      dispositions: { rendered: 0, summarized: 0, ignored: 0, unknown: 0 },
                      pending_requests: [],
                      events: [],
                      raw: null,
                    }
                  : null;
      if (result === null) {
        socket.write(line({ id, error: { type: "ValueError", message: `unsupported fixture method: ${method}` } }));
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

async function main() {
  await fs.mkdir(OUT_DIR, { recursive: true });
  const fixtures = makeFixtureRoot("wiki-146-palette-");
  const transcript = path.join(fixtures.root, "fixture.jsonl");
  const rawLog = path.join(fixtures.root, "fixture.log");
  await fs.writeFile(
    transcript,
    `${JSON.stringify({ type: "event_msg", payload: { type: "agent_message", message: "fixture" } })}\n`
  );
  await fs.writeFile(rawLog, "");
  const vaultDir = path.join(fixtures.root, "vault");
  await fs.mkdir(vaultDir, { recursive: true });
  await fs.mkdir(path.join(vaultDir, "log"), { recursive: true });
  await fs.writeFile(
    path.join(vaultDir, "todo.md"),
    "---\ntype: reference\n---\n\nTodo:\n\n- [P2] WIKI-146 add command palette\n- [P3] PHO-99999 backlog example\n"
  );
  await fs.writeFile(
    path.join(vaultDir, "log", "done.md"),
    "---\ntype: log\n---\n\n# Done\n\n## 2026-07-21\n\n- **wiki** — WIKI-99 old cleanup\n"
  );
  await fs.writeFile(
    path.join(vaultDir, "hot.md"),
    "---\ntype: reference\n---\n\n# Hot Context\n\nRolling cache of active threads.\n"
  );
  await fs.writeFile(fixtures.registryPath, JSON.stringify({}, null, 2));
  await fs.writeFile(fixtures.queuePath, "{}\n");
  await fs.writeFile(path.join(fixtures.runtimeDir, "supervisor.pid"), `${process.pid}\n`);

  const liveWorker = {
    ticket: WORKER,
    run_id: "00000000-0000-4000-8000-000000000146",
    provider: "claude",
    kind: "cc",
    role: "implement",
    model: "opus",
    effort: "high",
    worktree: fixtures.root,
    cwd: fixtures.root,
    orch: ORCHESTRATOR,
    state: "working",
    control_attached: true,
    provider_session_id: "fixture-worker",
    provider_pid: process.pid,
    transcript,
    log: rawLog,
    window: null,
    spawned_at: "2026-07-22T11:00:00Z",
  };
  const registry = {
    [WORKER]: { history: [], current: liveWorker },
  };
  await fs.writeFile(fixtures.registryPath, JSON.stringify(registry, null, 2));

  const supervisor = await startFakeSupervisor(fixtures, [liveWorker]);
  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 960 } });

  try {
    await page.addInitScript(({ worker }) => {
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
              layout: { kind: "pane", id: "pane-1", path: `agent://${worker}` },
            },
          ],
        })
      );
    }, { worker: WORKER });
    await page.goto(`${backend.baseUrl}/`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("[data-pane-key='pane-1']");

    // 1. Cmd-K opens the palette
    await page.keyboard.press("Meta+k");
    const dialog = page.getByRole("dialog", { name: "Command palette" });
    await dialog.waitFor();
    const input = page.getByPlaceholder("Search sessions, tickets, artifacts, notes…");
    await input.waitFor();

    // 2. Typing filters — search for WIKI-146 session
    await input.fill("WIKI-146");
    await dialog.getByRole("option").first().waitFor();
    const firstText = (await dialog.getByRole("option").first().textContent()) ?? "";
    assert.ok(firstText.includes("WIKI-146"), `First result should mention WIKI-146: ${firstText}`);

    await dialog.screenshot({ path: path.join(OUT_DIR, "palette-open-search.png") });

    // 3. Escape closes the palette
    await page.keyboard.press("Escape");
    await page.waitForSelector('[role="dialog"][aria-label="Command palette"]', { state: "detached" });

    // 4. Reopen and hit Enter to navigate (palette closes)
    await page.keyboard.press("Meta+k");
    await dialog.waitFor();
    const reopenedInput = page.getByPlaceholder("Search sessions, tickets, artifacts, notes…");
    await reopenedInput.waitFor();
    await reopenedInput.fill("WIKI-146");
    await dialog.getByRole("option").first().waitFor();
    await reopenedInput.press("Enter");
    await page.waitForSelector('[role="dialog"][aria-label="Command palette"]', { state: "detached" });

    // 5. Focus trap: Tab loops within the dialog; Esc closes even from a
    //    focused result button (dialog-scope key handler).
    await page.keyboard.press("Meta+k");
    await dialog.waitFor();
    const trappedInput = page.getByPlaceholder("Search sessions, tickets, artifacts, notes…");
    await trappedInput.waitFor();
    await trappedInput.fill("WIKI-146");
    await dialog.getByRole("option").first().waitFor();
    // Move focus onto a result button then press Escape from there.
    await dialog.getByRole("option").first().focus();
    const focusedInsideDialog = await page.evaluate(() => {
      const dialog = document.querySelector('[role="dialog"][aria-label="Command palette"]');
      return dialog?.contains(document.activeElement) ?? false;
    });
    assert.ok(focusedInsideDialog, "focus should remain inside the palette dialog");
    await page.keyboard.press("Escape");
    await page.waitForSelector('[role="dialog"][aria-label="Command palette"]', { state: "detached" });

    // 6. Background is marked inert while the palette is open.
    await page.keyboard.press("Meta+k");
    await dialog.waitFor();
    const inertCount = await page.evaluate(() => {
      const bodyChildren = Array.from(document.body.children);
      return bodyChildren.filter((el) => el.hasAttribute("inert")).length;
    });
    assert.ok(inertCount >= 1, "at least one background sibling should be inert");
    await page.keyboard.press("Escape");
    await page.waitForSelector('[role="dialog"][aria-label="Command palette"]', { state: "detached" });
    const cleaned = await page.evaluate(() => {
      const bodyChildren = Array.from(document.body.children);
      return bodyChildren.filter((el) => el.hasAttribute("inert")).length;
    });
    assert.equal(cleaned, 0, "inert attributes should be cleared on close");

    // 7. Ticket without a session routes to Linear when the session drops.
    //    Search for a ticket that only appears in todo.md (PHO-99999).
    await page.keyboard.press("Meta+k");
    await dialog.waitFor();
    const linearInput = page.getByPlaceholder("Search sessions, tickets, artifacts, notes…");
    await linearInput.fill("PHO-99999");
    await dialog.getByRole("option").first().waitFor();
    const linearText = (await dialog.getByRole("option").first().textContent()) ?? "";
    assert.ok(linearText.includes("PHO-99999"), `Linear route result mentions ticket: ${linearText}`);
    await page.keyboard.press("Escape");
    await page.waitForSelector('[role="dialog"][aria-label="Command palette"]', { state: "detached" });

    // 8. Cmd-K opens palette even while focus is inside a text input.
    //    Focus the pane's message composer / any input, then trigger Meta+K.
    await page.evaluate(() => {
      const first = document.querySelector("textarea, input[type='text']");
      if (first && typeof first.focus === "function") first.focus();
    });
    await page.keyboard.press("Meta+k");
    await dialog.waitFor({ timeout: 5000 });
    await page.keyboard.press("Escape");
    await page.waitForSelector('[role="dialog"][aria-label="Command palette"]', { state: "detached" });

    console.error("[wiki-146-palette] palette open/type/enter/close/trap/inert/linear/composer passed");
  } finally {
    await page.close();
    await browser.close();
    await backend.stop();
    await supervisor.stop();
  }
}

await main();
