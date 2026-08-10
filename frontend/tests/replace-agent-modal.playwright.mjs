import fs from "node:fs/promises";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

import { makeFixtureRoot, startBackend } from "../scripts/wiki32-harness.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const ORCH = "wiki-test";
const SCREENSHOT = process.env.WIKI_81_SCREENSHOT || "/tmp/wiki-81-replace-modal.png";

function line(value) {
  return `${JSON.stringify(value)}\n`;
}

async function startFakeSupervisor(fixtures, current, registry) {
  const calls = [];
  const subscribers = new Set();
  let replacement = 81;

  async function persist() {
    registry[ORCH].current = { ...current };
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
        result = {
          status: "ok",
          pid: process.pid,
          runs: [{ ...current, control_attached: true, provider_alive: true }],
        };
      } else if (method === "run/queue") {
        result = { messages: [] };
      } else if (method === "events/read") {
        result = {
          run_id: current.run_id,
          provider: current.provider,
          state: current.state,
          raw_count: 0,
          normalized_count: 0,
          dispositions: { rendered: 0, summarized: 0, ignored: 0, unknown: 0 },
          pending_requests: [],
          events: [],
          raw: null,
        };
      } else if (method === "run/replace") {
        calls.push({ ...params });
        replacement += 1;
        current.run_id = `00000000-0000-4000-8000-${String(replacement).padStart(12, "0")}`;
        current.provider = params.provider;
        current.kind = params.provider === "codex" ? "cdx" : "cc";
        current.model = params.model;
        current.effort = params.effort ?? null;
        current.provider_pid = process.pid;
        current.provider_session_id = `fixture-session-${replacement}`;
        await persist();
        result = { ...current, agent_id: ORCH };
        publish({ type: "agents", tickets: [ORCH], surface: "agents" });
      } else {
        socket.write(
          line({ id, error: { type: "ValueError", message: `unsupported fixture method: ${method}` } }),
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
    calls,
    async stop() {
      for (const socket of subscribers) socket.destroy();
      await new Promise((resolve) => server.close(resolve));
      await fs.rm(fixtures.supervisorSocketPath, { force: true });
    },
  };
}

async function main() {
  const fixtures = makeFixtureRoot("wiki-81-replace-modal-");
  const transcript = path.join(fixtures.root, "claude-native-surfaces.jsonl");
  const rawLog = path.join(fixtures.root, "raw.jsonl");
  await fs.copyFile(
    path.join(ROOT, "backend", "tests", "fixtures", "claude_native_surfaces.jsonl"),
    transcript,
  );
  await fs.writeFile(rawLog, "");
  const current = {
    ticket: ORCH,
    run_id: "00000000-0000-4000-8000-000000000081",
    provider: "codex",
    kind: "cdx",
    role: "orchestrator",
    model: "gpt-5.4",
    effort: "low",
    worktree: fixtures.root,
    cwd: fixtures.root,
    orch: null,
    state: "working",
    provider_session_id: "fixture-session-81",
    provider_pid: process.pid,
    transcript,
    log: rawLog,
    window: null,
    spawned_at: "2026-07-10T18:00:00Z",
  };
  const registry = { [ORCH]: { history: [], current } };
  await fs.writeFile(fixtures.registryPath, JSON.stringify(registry, null, 2));
  await fs.writeFile(fixtures.queuePath, "{}\n");

  const supervisor = await startFakeSupervisor(fixtures, current, registry);
  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });

  try {
    await page.goto(`${backend.baseUrl}/#/agents`, { waitUntil: "domcontentloaded" });
    await page.locator(".agents-orch-group").waitFor();
    // Replace lives inside the row overflow menu after the WIKI-154 round-5
    // orchestrator-row density sweep. Open it first, then click Replace.
    await page
      .locator(".agents-orch-group")
      .getByRole("button", { name: new RegExp(`More actions for ${ORCH}`) })
      .click();
    await page.getByRole("menuitem", { name: "Replace" }).click();
    const dialog = page.getByRole("dialog", { name: `Replace ${ORCH}` });
    await dialog.waitFor();
    // Provider/model/effort now live inside Advanced; open it to interact
    // with them (WIKI-154 finding 4 redesign).
    await dialog.getByRole("button", { name: /Advanced/ }).click();
    await dialog.getByRole("button", { name: "Codex", exact: true }).waitFor();
    if ((await dialog.getByLabel("Model").inputValue()) !== "gpt-5.4") {
      throw new Error("Replace modal did not preserve the current model");
    }
    if ((await dialog.getByLabel("Reasoning effort").inputValue()) !== "low") {
      throw new Error("Replace modal did not preserve the current effort");
    }

    await dialog.getByRole("button", { name: "Claude" }).click();
    await dialog.getByLabel("Reasoning effort").waitFor({ state: "detached" });
    if ((await dialog.getByLabel("Model").inputValue()) !== "opus") {
      throw new Error("Orchestrator kind toggle did not select the Claude default");
    }
    await dialog.getByLabel("Model").selectOption("sonnet-4.6");
    await page.screenshot({ path: SCREENSHOT, fullPage: true });
    await dialog.getByRole("button", { name: "Replace agent" }).click();
    await page.getByText(`replaced ${ORCH}`).waitFor();

    if (supervisor.calls[0]?.provider !== "claude" || supervisor.calls[0]?.model !== "sonnet-4.6") {
      throw new Error(`Unexpected Claude Replace payload: ${JSON.stringify(supervisor.calls[0])}`);
    }

    await page.goto(`${backend.baseUrl}/#/agent/${ORCH}`, { waitUntil: "domcontentloaded" });
    await page.locator(".agent-session-surface-head").waitFor();
    await page.getByRole("button", { name: "Replace" }).click();
    const sessionDialog = page.getByRole("dialog", { name: `Replace ${ORCH}` });
    await sessionDialog.getByRole("button", { name: /Advanced/ }).click();
    await sessionDialog.getByRole("button", { name: "Codex", exact: true }).click();
    await sessionDialog.getByLabel("Reasoning effort").waitFor();
    await sessionDialog.getByLabel("Model").selectOption("gpt-5.4");
    await sessionDialog.getByLabel("Reasoning effort").selectOption("xhigh");
    await sessionDialog.getByRole("button", { name: "Replace agent" }).click();
    await sessionDialog.waitFor({ state: "detached" });

    if (
      supervisor.calls[1]?.provider !== "codex" ||
      supervisor.calls[1]?.model !== "gpt-5.4" ||
      supervisor.calls[1]?.effort !== "xhigh"
    ) {
      throw new Error(`Unexpected Codex Replace payload: ${JSON.stringify(supervisor.calls[1])}`);
    }
  } finally {
    await page.close();
    await browser.close();
    await backend.stop();
    await supervisor.stop();
  }
}

await main();
