import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";
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
  const socketSuffix = basename(root).slice(-12);
  const statusDir = join(root, "status");
  const sessionsDir = join(root, "sessions");
  const runtimeDir = join(root, "runtime");
  const workgraphsDir = join(root, "workgraphs");
  mkdirSync(statusDir, { recursive: true });
  mkdirSync(sessionsDir, { recursive: true });
  mkdirSync(runtimeDir, { recursive: true });
  mkdirSync(workgraphsDir, { recursive: true });
  return {
    root,
    statusDir,
    sessionsDir,
    runtimeDir,
    workgraphsDir,
    // AF_UNIX paths are short on macOS; keep the isolated socket under /tmp.
    supervisorSocketPath: join("/tmp", `wiki-${socketSuffix}.sock`),
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

function childHasExited(child) {
  return child.exitCode !== null || child.signalCode !== null;
}

async function terminateChild(child) {
  if (childHasExited(child)) return;
  await new Promise((resolveStop, rejectStop) => {
    let settled = false;
    const cleanup = () => {
      child.off("exit", onExit);
      child.off("error", onError);
    };
    const finish = (callback, value) => {
      if (settled) return;
      settled = true;
      cleanup();
      callback(value);
    };
    const onExit = () => finish(resolveStop);
    const onError = (error) => finish(rejectStop, error);
    child.once("exit", onExit);
    child.once("error", onError);
    if (childHasExited(child)) {
      onExit();
      return;
    }
    try {
      const signaled = child.kill("SIGTERM");
      if (childHasExited(child)) {
        onExit();
      } else if (!signaled) {
        finish(rejectStop, new Error("Could not stop isolated backend process"));
      }
    } catch (error) {
      finish(rejectStop, error);
    }
  });
}

export async function startBackend(
  fixtures,
  {
    chooseBackendPort = choosePort,
    healthTimeoutMs = 15000,
    spawnProcess = spawn,
    waitForReady = waitForHealth,
  } = {},
) {
  const port = await chooseBackendPort();
  const baseUrl = `http://127.0.0.1:${port}`;
  const child = spawnProcess(
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
      env: {
        ...process.env,
        TMUX: "",
        TMUX_PANE: "",
        WIKI_FRONTEND_DIST: FRONTEND_DIST,
        WIKI_SUPERVISOR_SOCKET_PATH: fixtures.supervisorSocketPath,
      },
      stdio: ["ignore", "pipe", "pipe"],
    }
  );
  let stderr = "";
  child.stderr.on("data", (chunk) => {
    stderr += chunk.toString();
  });
  try {
    await waitForReady(baseUrl, healthTimeoutMs);
  } catch (error) {
    try {
      await terminateChild(child);
    } catch (cleanupError) {
      throw new AggregateError(
        [error, cleanupError],
        "Isolated backend failed health check and cleanup",
      );
    }
    throw error;
  }
  let stopPromise = null;
  return {
    baseUrl,
    port,
    process: child,
    stop() {
      stopPromise ??= (async () => {
        await terminateChild(child);
        if (child.exitCode && stderr) {
          throw new Error(stderr);
        }
      })();
      return stopPromise;
    },
  };
}

async function cleanupBackendBrowserFixture({ backend, browser, page }) {
  const cleanup = await Promise.allSettled([
    (async () => {
      try {
        if (page && !page.isClosed()) {
          try {
            await page.unrouteAll({ behavior: "ignoreErrors" });
          } finally {
            await page.close();
          }
        }
      } finally {
        await browser?.close();
      }
    })(),
    backend.stop(),
  ]);
  const errors = cleanup
    .filter((result) => result.status === "rejected")
    .map((result) => result.reason);
  if (errors.length > 0) {
    throw new AggregateError(errors, "Backend browser fixture cleanup failed");
  }
}

export async function startBackendBrowserFixture({
  createPage,
  launchBrowser,
  startBackendProcess,
}) {
  const backend = await startBackendProcess();
  let browser = null;
  let page = null;
  try {
    browser = await launchBrowser();
    page = await createPage(browser);
  } catch (error) {
    try {
      await cleanupBackendBrowserFixture({ backend, browser, page });
    } catch (cleanupError) {
      throw new AggregateError(
        [error, cleanupError],
        "Backend browser fixture failed startup and cleanup",
      );
    }
    throw error;
  }
  let stopPromise = null;
  return {
    backend,
    browser,
    page,
    stop() {
      stopPromise ??= cleanupBackendBrowserFixture({ backend, browser, page });
      return stopPromise;
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
