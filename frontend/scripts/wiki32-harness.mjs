import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import net from "node:net";

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(__dirname, "..", "..");
const WRAPPER = resolve(ROOT, "scripts", "wiki32_isolated_backend.py");
const FRONTEND_DIST = resolve(ROOT, "frontend", "dist");

function resolvePython() {
  const candidates = [
    process.env.WIKI_PYTHON,
    resolve(ROOT, ".venv", "bin", "python"),
    resolve(ROOT, "..", "..", "..", ".venv", "bin", "python"),
  ].filter(Boolean);
  const found = candidates.find((candidate) => existsSync(candidate));
  if (!found) {
    throw new Error(`No Python runtime found for isolated backend: ${candidates.join(", ")}`);
  }
  return found;
}

const PYTHON = resolvePython();

export function makeFixtureRoot(prefix) {
  const root = mkdtempSync(join(tmpdir(), prefix));
  const statusDir = join(root, "status");
  const sessionsDir = join(root, "sessions");
  mkdirSync(statusDir, { recursive: true });
  mkdirSync(sessionsDir, { recursive: true });
  return {
    root,
    statusDir,
    sessionsDir,
    registryPath: join(root, "agent-registry.json"),
    queuePath: join(root, "wiki-msg-queue.json"),
  };
}

export function writeRegistry(registryPath, tickets) {
  writeFileSync(
    registryPath,
    JSON.stringify(
      {
        _orchestrators: Object.fromEntries(
          tickets.map(([ticket, transcript]) => [
            ticket,
            {
              window: "@9999",
              spawned_at: "2026-07-09T00:00:00Z",
              transcript,
            },
          ])
        ),
      },
      null,
      2
    )
  );
}

export function writeQueue(queuePath, ticket, messages = []) {
  writeFileSync(queuePath, JSON.stringify(messages.length ? { [ticket]: messages } : {}, null, 2));
}

export function defaultWindowLayout() {
  return {
    version: 2,
    activeWindowId: "window-0",
    windows: [
      {
        id: "window-0",
        focusedPaneId: "pane-1",
        layout: { kind: "pane", id: "pane-1", path: "agent://WIKI-32" },
      },
      {
        id: "window-1",
        focusedPaneId: "pane-2",
        layout: { kind: "pane", id: "pane-2", path: "agent://WIKI-33" },
      },
      {
        id: "window-2",
        focusedPaneId: "pane-3",
        layout: {
          kind: "split",
          direction: "row",
          ratio: 0.5,
          first: { kind: "pane", id: "pane-3", path: "agent://WIKI-34" },
          second: { kind: "pane", id: "pane-4", path: "agent://WIKI-35" },
        },
      },
    ],
  };
}

export function codexUser(message, timestamp) {
  return {
    type: "event_msg",
    timestamp,
    payload: { type: "user_message", message },
  };
}

export function codexAssistant(message, timestamp) {
  return {
    type: "event_msg",
    timestamp,
    payload: { type: "agent_message", message },
  };
}

export function codexToolCall(callId, name, argumentsText, timestamp) {
  return {
    type: "response_item",
    timestamp,
    payload: {
      type: "function_call",
      call_id: callId,
      name,
      arguments: argumentsText,
    },
  };
}

export function codexToolOutput(callId, output, timestamp) {
  return {
    type: "response_item",
    timestamp,
    payload: {
      type: "function_call_output",
      call_id: callId,
      output,
    },
  };
}

export async function choosePort() {
  return new Promise((resolvePort, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      server.close(() => resolvePort(address.port));
    });
  });
}

export async function waitForHealth(baseUrl, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`${baseUrl}/health`);
      if (response.ok) return;
    } catch {
      /* backend still booting */
    }
    await new Promise((resolveSleep) => setTimeout(resolveSleep, 250));
  }
  throw new Error(`Timed out waiting for ${baseUrl}/health`);
}

export async function startBackend(fixtures) {
  const port = await choosePort();
  const baseUrl = `http://127.0.0.1:${port}`;
  const child = spawn(
    PYTHON,
    [
      WRAPPER,
      "--port",
      String(port),
      "--registry",
      fixtures.registryPath,
      "--status-dir",
      fixtures.statusDir,
      "--queue",
      fixtures.queuePath,
      "--codex-sessions-dir",
      fixtures.sessionsDir,
    ],
    {
      cwd: ROOT,
      env: { ...process.env, WIKI_FRONTEND_DIST: FRONTEND_DIST },
      stdio: ["ignore", "pipe", "pipe"],
    }
  );
  let stderr = "";
  child.stderr.on("data", (chunk) => {
    stderr += chunk.toString();
  });
  await waitForHealth(baseUrl);
  return {
    baseUrl,
    port,
    process: child,
    async stop() {
      child.kill("SIGTERM");
      await new Promise((resolveStop) => child.once("exit", resolveStop));
      if (child.exitCode && stderr) {
        throw new Error(stderr);
      }
    },
  };
}

export async function openSessionPage(page, baseUrl, layout = defaultWindowLayout()) {
  await page.addInitScript(({ storedLayout }) => {
    localStorage.setItem("wiki-window-layout-v2", JSON.stringify(storedLayout));
    localStorage.setItem("wiki-sidebar-visible", "false");
  }, { storedLayout: layout });
  await page.goto(`${baseUrl}/#/agent/WIKI-32`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".session-scroll");
}
