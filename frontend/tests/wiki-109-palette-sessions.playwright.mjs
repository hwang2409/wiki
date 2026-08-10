import assert from "node:assert/strict";
import fs from "node:fs/promises";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

import { makeFixtureRoot, startBackend } from "../scripts/wiki32-harness.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || "/tmp/wiki-109-palette-sessions";
const WORKER = "WIKI-109";
const ORCHESTRATOR = "phoebe";

function line(value) {
  return `${JSON.stringify(value)}\n`;
}

function collectPanes(node, panes = []) {
  if (node.kind === "pane") {
    panes.push(node);
    return panes;
  }
  collectPanes(node.first, panes);
  collectPanes(node.second, panes);
  return panes;
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

async function storedLayout(page) {
  return page.evaluate(() => JSON.parse(localStorage.getItem("wiki-window-layout-v2") || "{}"));
}

async function waitForLayout(page, predicate, message) {
  const deadline = Date.now() + 5_000;
  let lastLayout = null;
  while (Date.now() < deadline) {
    const layout = await storedLayout(page);
    lastLayout = layout;
    if (predicate(layout)) return layout;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error(`Timed out waiting for layout: ${message}; last=${JSON.stringify(lastLayout)}`);
}

function windowById(layout, id) {
  return layout.windows.find((window) => window.id === id) ?? null;
}

async function openSwitcher(page, query) {
  await page.keyboard.press("Meta+p");
  const dialog = page.getByRole("dialog", { name: "Quick switcher" });
  await dialog.waitFor();
  const input = dialog.getByPlaceholder("Find a note, file, or session...");
  await input.fill(query);
  return dialog;
}

async function main() {
  await fs.mkdir(OUT_DIR, { recursive: true });
  const fixtures = makeFixtureRoot("wiki-109-palette-sessions-");
  const transcript = path.join(fixtures.root, "fixture.jsonl");
  const rawLog = path.join(fixtures.root, "fixture.log");
  await fs.writeFile(transcript, `${JSON.stringify({ type: "event_msg", payload: { type: "agent_message", message: "fixture" } })}\n`);
  await fs.writeFile(rawLog, "");
  await fs.mkdir(path.join(fixtures.root, "vault"), { recursive: true });
  await fs.writeFile(path.join(fixtures.root, "vault", "scratch.md"), "# Scratch\n");
  await fs.writeFile(path.join(fixtures.runtimeDir, "supervisor.pid"), `${process.pid}\n`);

  const liveWorker = {
    ticket: WORKER,
    run_id: "00000000-0000-4000-8000-000000000109",
    provider: "codex",
    kind: "cdx",
    role: "implement",
    model: "gpt-5.4",
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
    spawned_at: "2026-07-14T12:00:00Z",
  };
  const liveOrchestrator = {
    ticket: ORCHESTRATOR,
    run_id: "00000000-0000-4000-8000-000000000110",
    provider: "claude",
    kind: "cc",
    role: "orchestrator",
    model: "opus",
    effort: null,
    worktree: fixtures.root,
    cwd: fixtures.root,
    orch: null,
    state: "working",
    control_attached: true,
    provider_session_id: "fixture-orchestrator",
    provider_pid: process.pid,
    transcript,
    log: rawLog,
    window: null,
    spawned_at: "2026-07-14T12:00:00Z",
  };
  const deadWorker = {
    ...liveWorker,
    ticket: "WIKI-DEAD",
    run_id: "00000000-0000-4000-8000-000000000111",
    control_attached: false,
    state: "dead",
  };
  const registry = {
    [WORKER]: { history: [], current: liveWorker },
    [ORCHESTRATOR]: { history: [], current: liveOrchestrator },
    [deadWorker.ticket]: { history: [], current: deadWorker },
  };
  await fs.writeFile(fixtures.registryPath, JSON.stringify(registry, null, 2));
  await fs.writeFile(fixtures.queuePath, "{}\n");

  const supervisor = await startFakeSupervisor(fixtures, [liveWorker, liveOrchestrator, deadWorker]);
  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 960 } });

  try {
    await page.addInitScript(({ worker, orchestrator }) => {
      localStorage.setItem("wiki-sidebar-visible", "false");
      localStorage.setItem(
        "wiki-window-layout-v2",
        JSON.stringify({
          version: 2,
          activeWindowId: "window-source",
          windows: [
            {
              id: "window-source",
              focusedPaneId: "pane-worker",
              layout: { kind: "pane", id: "pane-worker", path: `agent://${worker}` },
            },
            {
              id: "window-target",
              focusedPaneId: "pane-note",
              layout: { kind: "pane", id: "pane-note", path: "scratch.md" },
            },
            {
              id: "window-orchestrator",
              focusedPaneId: "pane-orchestrator",
              layout: { kind: "pane", id: "pane-orchestrator", path: `agent://${orchestrator}` },
            },
          ],
        }),
      );
    }, { worker: WORKER, orchestrator: ORCHESTRATOR });
    await page.goto(`${backend.baseUrl}/#/agent/${WORKER}`, { waitUntil: "domcontentloaded" });
    await page.locator("[data-pane-key='pane-worker']").waitFor();

    const deadPalette = await openSwitcher(page, "dead-session");
    await deadPalette.locator(".quick-switcher-loading").waitFor({ state: "detached" });
    await deadPalette.getByText("No matches", { exact: true }).waitFor();
    await page.keyboard.press("Escape");

    const orchPalette = await openSwitcher(page, "phoebe");
    await orchPalette.getByText("Sessions", { exact: true }).waitFor();
    await orchPalette.getByText(ORCHESTRATOR, { exact: true }).waitFor();
    await orchPalette.getByText("orchestrator", { exact: true }).waitFor();
    await orchPalette.getByPlaceholder("Find a note, file, or session...").press("Enter");
    let layout = await waitForLayout(
      page,
      (current) => current.activeWindowId === "window-orchestrator",
      "orchestrator selection focuses its current window",
    );
    assert.deepEqual(collectPanes(windowById(layout, "window-orchestrator").layout), [
      { kind: "pane", id: "pane-orchestrator", path: `agent://${ORCHESTRATOR}` },
    ]);

    const workerPalette = await openSwitcher(page, "WIKI-109");
    await workerPalette.getByText(WORKER, { exact: true }).waitFor();
    await workerPalette.getByPlaceholder("Find a note, file, or session...").press("Enter");
    layout = await waitForLayout(
      page,
      (current) => current.activeWindowId === "window-source",
      "session selection from a non-blank pane focuses the source",
    );
    const sourceBeforeMove = windowById(layout, "window-source");
    assert.ok(sourceBeforeMove, "non-blank selection must not remove the source window");
    assert.deepEqual(
      collectPanes(sourceBeforeMove.layout),
      [{ kind: "pane", id: "pane-worker", path: `agent://${WORKER}` }],
      "non-blank session selection must focus without moving its pane",
    );

    await page.keyboard.press("Control+a");
    await page.keyboard.press("l");
    await waitForLayout(page, (current) => current.activeWindowId === "window-target", "move to target window");
    await page.keyboard.press("Control+a");
    await page.keyboard.press("p");
    layout = await waitForLayout(
      page,
      (current) => collectPanes(windowById(current, "window-target").layout).some((pane) => pane.path === null),
      "C-a p creates a blank pane",
    );
    const blankPane = collectPanes(windowById(layout, "window-target").layout).find((pane) => pane.path === null);
    assert.ok(blankPane, "blank pane placeholder was not persisted");
    await page.getByText("New pane", { exact: true }).waitFor();

    await page.keyboard.press("Meta+p");
    await page.keyboard.press("Escape");
    layout = await waitForLayout(
      page,
      (current) => collectPanes(windowById(current, "window-target").layout).some((pane) => pane.path === null),
      "Escape keeps the blank pane in the layout",
    );
    assert.ok(layout, "blank pane was removed by Escape");

    const movePalette = await openSwitcher(page, "WIKI-109");
    await movePalette.getByText(WORKER, { exact: true }).waitFor();
    await movePalette.screenshot({ path: path.join(OUT_DIR, "palette-session-results.png") });
    const moveInput = movePalette.getByPlaceholder("Find a note, file, or session...");
    await moveInput.focus();
    await moveInput.press("Enter");
    layout = await waitForLayout(
      page,
      (current) => {
        const target = windowById(current, "window-target");
        return target && collectPanes(target.layout).some((pane) => pane.path === `agent://${WORKER}`);
      },
      "session moves into the focused blank pane",
    );

    assert.equal(windowById(layout, "window-source"), null, "source window should vacate after its only pane moves");
    const targetPanes = collectPanes(windowById(layout, "window-target").layout);
    assert.deepEqual(
      targetPanes.map((pane) => pane.path).sort(),
      [`agent://${WORKER}`, "scratch.md"].sort(),
      "target layout should contain the moved session and original note only",
    );
    assert.equal(
      targetPanes.find((pane) => pane.path === `agent://${WORKER}`).id,
      "pane-worker",
      "the moved session must retain its original pane state key",
    );
    assert.equal(new Set(targetPanes.map((pane) => pane.id)).size, targetPanes.length, "layout has a ghost pane id");
    await page.screenshot({ path: path.join(OUT_DIR, "blank-pane-session-moved.png"), fullPage: true });

    await fs.writeFile(
      path.join(OUT_DIR, "summary.json"),
      JSON.stringify(
        {
          fixtures: {
            root: fixtures.root,
            registry: fixtures.registryPath,
            statusDir: fixtures.statusDir,
            runtimeDir: fixtures.runtimeDir,
            vault: path.join(fixtures.root, "vault"),
          },
          screenshots: ["palette-session-results.png", "blank-pane-session-moved.png"],
        },
        null,
        2,
      ),
    );
    console.error("[wiki-109-playwright] session palette and blank-pane placement passed");
  } finally {
    await page.close();
    await browser.close();
    await backend.stop();
    await supervisor.stop();
  }
}

await main();
