import fs from "node:fs/promises";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

import { makeFixtureRoot, startBackend } from "../scripts/wiki32-harness.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || "/tmp/wiki-42-headless-evidence";
const RUN_ID = "00000000-0000-4000-8000-000000000042";
const TICKET = "WIKI-42";

function logStep(message) {
  console.error(`[wiki-42-playwright] ${message}`);
}

function line(value) {
  return `${JSON.stringify(value)}\n`;
}

async function startFakeSupervisor(fixtures, current, registry) {
  const subscribers = new Set();
  const raw = [
    { seq: 1, payload: { method: "turn/started" } },
    { seq: 2, payload: { method: "item/tool/requestUserInput" } },
    { seq: 3, payload: { method: "context/compacted" } },
    { seq: 4, payload: { method: "thread/status/changed" } },
  ];
  const events = [
    {
      seq: 1,
      raw_seq: 1,
      normalized_at: "2026-07-09T20:00:00Z",
      disposition: "rendered",
      kind: "turn_started",
      payload: raw[0].payload,
      lifecycle_state: "working",
    },
    {
      seq: 2,
      raw_seq: 2,
      normalized_at: "2026-07-09T20:00:01Z",
      disposition: "rendered",
      kind: "approval",
      payload: {
        method: "item/tool/requestUserInput",
        id: 0,
        params: {
          questions: [
            {
              id: "scope",
              header: "Scope",
              question: "Which scope?",
              options: [
                { label: "Full brief", description: "Complete the ticket." },
                { label: "First slice", description: "Stop after the first boundary." },
              ],
            },
          ],
        },
      },
      lifecycle_state: "waiting-approval",
    },
    {
      seq: 3,
      raw_seq: 3,
      normalized_at: "2026-07-09T20:00:02Z",
      disposition: "rendered",
      kind: "context_compacted",
      payload: raw[2].payload,
      lifecycle_state: null,
    },
    {
      seq: 4,
      raw_seq: 4,
      normalized_at: "2026-07-09T20:00:03Z",
      disposition: "summarized",
      kind: "thread_status_changed",
      payload: raw[3].payload,
      lifecycle_state: "waiting-approval",
    },
  ];
  let pendingRequests = [
    {
      request_id: 0,
      request_kind: "item/tool/requestUserInput",
      received_at: "2026-07-09T20:00:01Z",
      raw_seq: 2,
      payload: events[1].payload,
    },
  ];
  let capturedResponse = null;

  async function persistState() {
    registry[TICKET].current = { ...current };
    await fs.writeFile(fixtures.registryPath, JSON.stringify(registry, null, 2));
  }

  function runtimeRow() {
    const terminal = current.state === "dead" || current.state === "completed";
    return {
      ...current,
      control_attached: !terminal,
      provider_alive: !terminal,
    };
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
      if (method === "events/subscribe") {
        subscribers.add(socket);
        socket.write(line({ id, result: { subscribed: true } }));
        socket.on("close", () => subscribers.delete(socket));
        return;
      }

      let result;
      if (method === "ping") {
        result = { status: "ok", pid: process.pid };
      } else if (method === "run/list") {
        result = { status: "ok", pid: process.pid, runs: [runtimeRow()] };
      } else if (method === "run/status") {
        result = runtimeRow();
      } else if (method === "events/read") {
        result = {
          run_id: RUN_ID,
          provider: "codex",
          state: current.state,
          raw_count: raw.length,
          normalized_count: events.length,
          dispositions: { rendered: 3, summarized: 1, ignored: 0, unknown: 0 },
          pending_requests: pendingRequests,
          events,
          raw: params.include_raw ? raw : null,
        };
      } else if (method === "run/queue") {
        result = { messages: [] };
      } else if (method === "run/send_now") {
        result = { status: "sent" };
      } else if (method === "run/respond") {
        capturedResponse = params;
        pendingRequests = pendingRequests.filter(
          (request) => request.request_id !== params.request_id,
        );
        current.state = "working";
        await persistState();
        result = runtimeRow();
        publish({ type: "session", ticket: TICKET, surface: "session" });
      } else if (["run/interrupt", "run/resume", "run/stop", "run/archive"].includes(method)) {
        current.state = {
          "run/interrupt": "interrupted",
          "run/resume": "working",
          "run/stop": "dead",
          "run/archive": "completed",
        }[method];
        await persistState();
        result = runtimeRow();
        publish({ type: "agents", tickets: [TICKET], surface: "agents" });
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
    response() {
      return capturedResponse;
    },
    async stop() {
      for (const socket of subscribers) socket.destroy();
      await new Promise((resolve) => server.close(resolve));
      await fs.rm(fixtures.supervisorSocketPath, { force: true });
    },
  };
}

async function main() {
  await fs.mkdir(OUT_DIR, { recursive: true });
  const fixtures = makeFixtureRoot("wiki-42-headless-ui-");
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
    effort: "high",
    worktree: fixtures.root,
    cwd: fixtures.root,
    orch: null,
    state: "waiting-approval",
    provider_session_id: "fixture-thread-42",
    provider_pid: process.pid,
    transcript,
    log: rawLog,
    window: null,
    spawned_at: "2026-07-09T20:00:00Z",
  };
  const registry = { [TICKET]: { history: [], current } };
  await fs.writeFile(fixtures.registryPath, JSON.stringify(registry, null, 2));
  await fs.writeFile(fixtures.queuePath, "{}\n");

  logStep("starting isolated fake supervisor and backend with TMUX empty");
  const supervisor = await startFakeSupervisor(fixtures, current, registry);
  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1500 } });

  try {
    await page.goto(`${backend.baseUrl}/#/agents`, { waitUntil: "domcontentloaded" });
    await page.getByText(TICKET, { exact: true }).first().waitFor();
    await page.getByText("run 00000000 · waiting-approval", { exact: true }).waitFor();
    const agentsPayload = await page.evaluate(async () => (await fetch("/api/agents")).json());
    logStep(`agents payload: ${JSON.stringify(agentsPayload)}`);
    logStep(`visible controls: ${(await page.locator("button").allTextContents()).join(" | ")}`);
    await page.getByRole("button", { name: "Interrupt", exact: true }).waitFor();
    await page.screenshot({ path: path.join(OUT_DIR, "agents-headless-before.png"), fullPage: true });

    logStep("verifying exact composer POST response through supervisor");
    const composerResult = await page.evaluate(async (ticket) => {
      const response = await fetch(`/api/agents/${ticket}/message`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: "fixture steering", mode: "now" }),
      });
      return response.json();
    }, TICKET);
    if (JSON.stringify(composerResult) !== JSON.stringify({ status: "sent" })) {
      throw new Error(`composer response changed: ${JSON.stringify(composerResult)}`);
    }

    logStep("opening provider action surface");
    await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
    // WIKI-152: "Action required" surfaces at the top of the chrome outside
    // any disclosure, so it is available for approval without extra clicks.
    await page.getByText("Action required", { exact: true }).waitFor();
    // WIKI-235 removes diagnostic Run details from the session. Raw events
    // remain available through the events API, which this test checks below.
    if ((await page.locator('[data-testid="session-run-details"]').count()) !== 0) {
      throw new Error("WIKI-235: Run details must not render");
    }
    await page.screenshot({ path: path.join(OUT_DIR, "session-provider-action.png"), fullPage: true });

    logStep("answering captured Codex requestUserInput through the adapter route");
    await page.locator(".session-provider-question select").selectOption("Full brief");
    await page.getByRole("button", { name: "Send answers", exact: true }).click();
    await page.waitForFunction(() => !document.body.textContent?.includes("Action required"));
    const response = supervisor.response();
    const expectedResponse = {
      agent_id: TICKET,
      request_id: 0,
      response: { answers: { scope: { answers: ["Full brief"] } } },
    };
    if (JSON.stringify(response) !== JSON.stringify(expectedResponse)) {
      throw new Error(`provider response changed: ${JSON.stringify(response)}`);
    }

    const rawInspector = await page.evaluate(async (ticket) => {
      const response = await fetch(`/api/agents/${ticket}/events?include_raw=true`);
      return response.json();
    }, TICKET);
    if (rawInspector.raw?.length !== 4 || rawInspector.events?.length !== 4) {
      throw new Error(`raw inspector mismatch: ${JSON.stringify(rawInspector)}`);
    }

    logStep("interrupting through the agents controller after approval resolution");
    await page.goto(`${backend.baseUrl}/#/agents`, { waitUntil: "domcontentloaded" });
    await page.getByText("run 00000000 · working", { exact: true }).waitFor();
    await page.getByRole("button", { name: "Interrupt", exact: true }).click();
    await page.getByText("interrupt", { exact: false }).first().waitFor();
    await page.getByText("run 00000000 · interrupted", { exact: true }).waitFor();
    await page.screenshot({ path: path.join(OUT_DIR, "agents-headless-after.png"), fullPage: true });

    await fs.writeFile(
      path.join(OUT_DIR, "summary.json"),
      JSON.stringify(
        {
          tmux: "empty",
          composer: composerResult,
          provider_response: response,
          raw_count: rawInspector.raw_count,
          normalized_count: rawInspector.normalized_count,
          screenshots: [
            "agents-headless-before.png",
            "agents-headless-after.png",
            "session-provider-action.png",
          ],
        },
        null,
        2,
      ),
    );
    logStep(`evidence written to ${OUT_DIR}`);
  } finally {
    await page.close();
    await browser.close();
    await backend.stop();
    await supervisor.stop();
  }
}

await main();
