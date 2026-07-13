import fs from "node:fs/promises";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

import { makeFixtureRoot, startBackend } from "../scripts/wiki32-harness.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || "/tmp";
const DETACHED_COPY = "adapter detached — archive to reset";
const DEAD_ORCH = "wiki-78-orch";
const DEAD_WORKER = "WIKI-7801";
const RETRY_WORKER = "WIKI-7803";
const HEALTHY_WORKER = "WIKI-7802";
const RUNS = {
  [DEAD_ORCH]: "00000000-0000-4000-8000-000000000078",
  [DEAD_WORKER]: "00000000-0000-4000-8000-000000000079",
  [RETRY_WORKER]: "00000000-0000-4000-8000-000000000080",
  [HEALTHY_WORKER]: "00000000-0000-4000-8000-000000000081",
};

function logStep(message) {
  console.error(`[wiki-78-playwright] ${message}`);
}

function line(value) {
  return `${JSON.stringify(value)}\n`;
}

function fixtureCurrent(agentId, overrides = {}) {
  const kind = agentId === DEAD_ORCH ? "cc" : "cdx";
  const role = agentId === DEAD_ORCH ? "orchestrator" : "implement";
  return {
    ticket: agentId,
    run_id: RUNS[agentId],
    provider: kind === "cc" ? "claude" : "codex",
    kind,
    role,
    model: kind === "cc" ? "sonnet" : "gpt-5.4",
    effort: kind === "cc" ? null : "high",
    worktree: overrides.worktree,
    cwd: overrides.worktree,
    orch: null,
    state: "working",
    provider_session_id: `fixture-session-${agentId.toLowerCase()}`,
    provider_pid: 4242,
    transcript: null,
    log: overrides.log,
    window: null,
    spawned_at: "2026-07-13T12:00:00Z",
    ...overrides,
  };
}

async function startFakeSupervisor(fixtures, registry) {
  const runtimeByAgent = new Map([
    [
      DEAD_ORCH,
      {
        ...registry[DEAD_ORCH].current,
        state: "completed",
        state_reason: "adapter_lost",
        control_attached: false,
        provider_alive: false,
        provider_pid: null,
      },
    ],
    [
      DEAD_WORKER,
      {
        ...registry[DEAD_WORKER].current,
        state: "working",
        state_reason: null,
        control_attached: false,
        provider_alive: false,
        provider_pid: null,
      },
    ],
    [
      RETRY_WORKER,
      {
        ...registry[RETRY_WORKER].current,
        state: "completed",
        state_reason: "adapter_lost",
        control_attached: false,
        provider_alive: false,
        provider_pid: null,
      },
    ],
    [
      HEALTHY_WORKER,
      {
        ...registry[HEALTHY_WORKER].current,
        state: "working",
        state_reason: null,
        control_attached: true,
        provider_alive: true,
        provider_pid: process.pid,
      },
    ],
  ]);
  let retryFailuresRemaining = 1;

  async function persistRegistry() {
    await fs.writeFile(fixtures.registryPath, JSON.stringify(registry, null, 2));
  }

  async function archiveAgent(agentId) {
    const entry = registry[agentId];
    if (!entry?.current) throw new Error(`missing archive target ${agentId}`);
    const current = entry.current;
    const archivedAt = "2026-07-13T13:00:00Z";
    const sessionDir = path.join(
      fixtures.root,
      "archive",
      agentId,
      "20260713-130000",
    );
    await fs.mkdir(sessionDir, { recursive: true });
    let status = { state: "completed", step: "archived detached run", pr: null };
    try {
      status = JSON.parse(
        await fs.readFile(path.join(fixtures.statusDir, `${agentId}.json`), "utf-8"),
      );
    } catch {
      // Status files are optional for orchestrators.
    }
    await fs.writeFile(path.join(sessionDir, "final-status.json"), JSON.stringify(status, null, 2));
    await fs.writeFile(
      path.join(sessionDir, "meta.json"),
      JSON.stringify(
        {
          outcome: "archived",
          ended_at: archivedAt,
          worker: { ...current, ended_at: archivedAt, outcome: "archived" },
          history: [],
          source: "wiki-78-playwright",
        },
        null,
        2,
      ),
    );
    delete registry[agentId];
    runtimeByAgent.delete(agentId);
    await fs.rm(path.join(fixtures.statusDir, `${agentId}.json`), { force: true });
    await persistRegistry();
    return {
      agent_id: agentId,
      run_id: current.run_id,
      state: "completed",
      state_reason: current.state_reason ?? null,
    };
  }

  const server = net.createServer((socket) => {
    let buffer = "";
    socket.on("data", async (chunk) => {
      buffer += chunk.toString();
      const newline = buffer.indexOf("\n");
      if (newline < 0) return;
      const request = JSON.parse(buffer.slice(0, newline));
      const { id, method, params = {} } = request;

      if (method === "ping") {
        socket.write(line({ id, result: { status: "ok", pid: process.pid } }));
        socket.end();
        return;
      }

      if (method === "run/list") {
        socket.write(
          line({
            id,
            result: {
              status: "ok",
              pid: process.pid,
              runs: Array.from(runtimeByAgent.values()),
            },
          }),
        );
        socket.end();
        return;
      }

      if (method === "run/archive") {
        if (params.agent_id === RETRY_WORKER && retryFailuresRemaining > 0) {
          retryFailuresRemaining -= 1;
          socket.write(
            line({
              id,
              error: {
                type: "ValueError",
                message: "fixture archive failure",
              },
            }),
          );
          socket.end();
          return;
        }
        socket.write(line({ id, result: await archiveAgent(params.agent_id) }));
        socket.end();
        return;
      }

      socket.write(
        line({
          id,
          error: {
            type: "ValueError",
            message: `unsupported fixture method: ${method}`,
          },
        }),
      );
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
      await new Promise((resolve) => server.close(resolve));
      await fs.rm(fixtures.supervisorSocketPath, { force: true });
    },
  };
}

function agentCard(page, ticket) {
  return page.locator(".agent-card:not(.is-archived)", {
    has: page.getByText(ticket, { exact: true }),
  });
}

function orchestratorGroup(page, orchId) {
  return page.locator(".agents-orch-group", {
    has: page.getByText(orchId, { exact: true }),
  });
}

async function main() {
  await fs.mkdir(OUT_DIR, { recursive: true });
  const fixtures = makeFixtureRoot("wiki-78-dead-archive-");
  const rawLog = path.join(fixtures.root, "raw.jsonl");
  await fs.writeFile(rawLog, `${JSON.stringify({ seq: 1, payload: { method: "turn/started" } })}\n`);
  const registry = {
    [DEAD_ORCH]: { history: [], current: fixtureCurrent(DEAD_ORCH, { worktree: fixtures.root, log: rawLog }) },
    [DEAD_WORKER]: {
      history: [],
      current: fixtureCurrent(DEAD_WORKER, { worktree: fixtures.root, log: rawLog }),
    },
    [RETRY_WORKER]: {
      history: [],
      current: fixtureCurrent(RETRY_WORKER, { worktree: fixtures.root, log: rawLog }),
    },
    [HEALTHY_WORKER]: {
      history: [],
      current: fixtureCurrent(HEALTHY_WORKER, { worktree: fixtures.root, log: rawLog }),
    },
  };
  await fs.writeFile(fixtures.registryPath, JSON.stringify(registry, null, 2));
  await fs.writeFile(fixtures.queuePath, "{}\n");
  await fs.writeFile(
    path.join(fixtures.statusDir, `${DEAD_WORKER}.json`),
    JSON.stringify({ state: "working", pr: null, step: "waiting for archive", blocker: null }, null, 2),
  );
  await fs.writeFile(
    path.join(fixtures.statusDir, `${RETRY_WORKER}.json`),
    JSON.stringify({ state: "completed", pr: null, step: "reaped after adapter loss", blocker: null }, null, 2),
  );
  await fs.writeFile(
    path.join(fixtures.statusDir, `${HEALTHY_WORKER}.json`),
    JSON.stringify({ state: "working", pr: null, step: "still healthy", blocker: null }, null, 2),
  );

  logStep("starting isolated backend for dead archive UI");
  const supervisor = await startFakeSupervisor(fixtures, registry);
  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1480, height: 1500 } });

  try {
    await page.goto(`${backend.baseUrl}/#/agents`, { waitUntil: "domcontentloaded" });
    await page.getByText(DETACHED_COPY, { exact: true }).first().waitFor();
    await agentCard(page, DEAD_WORKER).getByRole("button", { name: "Archive", exact: true }).waitFor();
    await orchestratorGroup(page, DEAD_ORCH)
      .getByRole("button", { name: "Archive", exact: true })
      .waitFor();
    await agentCard(page, HEALTHY_WORKER).getByText("working", { exact: true }).waitFor();
    const healthyArchiveButtons = await agentCard(page, HEALTHY_WORKER)
      .getByRole("button", { name: "Archive", exact: true })
      .count();
    if (healthyArchiveButtons !== 0) {
      throw new Error("healthy worker unexpectedly renders Archive");
    }

    await page.screenshot({ path: path.join(OUT_DIR, "wiki-78-before.png"), fullPage: true });

    logStep("verifying inline archive error and retry path");
    const retryCard = agentCard(page, RETRY_WORKER);
    await retryCard.getByRole("button", { name: "Archive", exact: true }).click();
    await retryCard.getByText("fixture archive failure", { exact: true }).waitFor();
    await retryCard.getByRole("button", { name: "Archive", exact: true }).waitFor();
    await retryCard.getByRole("button", { name: "Archive", exact: true }).click();
    await retryCard.waitFor({ state: "detached" });
    await page.getByRole("button", { name: /log/i }).first().waitFor();

    logStep("archiving detached worker");
    await agentCard(page, DEAD_WORKER).getByRole("button", { name: "Archive", exact: true }).click();
    await agentCard(page, DEAD_WORKER).waitFor({ state: "detached" });
    await page.getByText(DEAD_WORKER, { exact: true }).nth(0).waitFor();

    logStep("archiving detached orchestrator");
    await orchestratorGroup(page, DEAD_ORCH).getByRole("button", { name: "Archive", exact: true }).click();
    await orchestratorGroup(page, DEAD_ORCH).waitFor({ state: "detached" });
    await waitForAgents(
      backend.baseUrl,
      (payload) =>
        payload.workers.some((worker) => worker.ticket === HEALTHY_WORKER) &&
        !payload.workers.some((worker) => worker.ticket === DEAD_WORKER) &&
        !payload.workers.some((worker) => worker.ticket === RETRY_WORKER) &&
        !payload.orchestrators.some((orch) => orch.id === DEAD_ORCH) &&
        payload.archived.some((entry) => entry.ticket === DEAD_WORKER) &&
        payload.archived.some((entry) => entry.ticket === RETRY_WORKER) &&
        payload.archived.some((entry) => entry.ticket === DEAD_ORCH),
    );
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.locator(".agents-view .agents-section-head").filter({ hasText: /^archived$/ }).waitFor();
    await page.getByText(RETRY_WORKER, { exact: true }).last().waitFor();
    await page.getByText(DEAD_WORKER, { exact: true }).last().waitFor();
    await page.getByText(DEAD_ORCH, { exact: true }).last().waitFor();

    await page.screenshot({ path: path.join(OUT_DIR, "wiki-78-after.png"), fullPage: true });
  } finally {
    await page.close().catch(() => {});
    await browser.close().catch(() => {});
    await backend.stop().catch(() => {});
    await supervisor.stop().catch(() => {});
  }
}

async function waitForAgents(baseUrl, predicate, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  let lastPayload = null;
  while (Date.now() < deadline) {
    const payload = await fetch(`${baseUrl}/api/agents`).then((response) => response.json());
    lastPayload = payload;
    if (predicate(payload)) return payload;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(
    `Timed out waiting for /api/agents at ${baseUrl}: ${JSON.stringify(lastPayload)}`,
  );
}

await main();
