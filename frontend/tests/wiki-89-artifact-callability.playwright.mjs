import { spawn } from "node:child_process";
import fs from "node:fs/promises";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

import {
  makeFixtureRoot,
  startBackend,
  writeQueue,
} from "../scripts/wiki32-harness.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const PYTHON = path.join(ROOT, ".venv", "bin", "python");
const FIXTURES = path.join(ROOT, "backend", "tests", "fixtures", "agent_runtime");
const PROMPT =
  'Call render_artifact with kind mermaid and payload source "graph TD; A-->B", then stop.';
const PROVIDERS = [
  { ticket: "WIKI-89-CDX", kind: "cdx", model: "gpt-5.4", effort: "low" },
  { ticket: "WIKI-89-CC", kind: "cc", model: "sonnet", effort: null },
];

function logStep(message) {
  console.error(`[wiki-89-playwright] ${message}`);
}

async function writeProviderWrappers(fixtures) {
  const bin = path.join(fixtures.root, "bin");
  await fs.mkdir(bin, { recursive: true });
  const wrappers = {
    codex: "FAKE_CODEX_SCRIPT",
    claude: "FAKE_CLAUDE_SCRIPT",
  };
  for (const [name, scriptVariable] of Object.entries(wrappers)) {
    const target = path.join(bin, name);
    await fs.writeFile(
      target,
      `#!/bin/sh\nexec "$FAKE_PYTHON" -u "$${scriptVariable}" "$@"\n`,
    );
    await fs.chmod(target, 0o755);
  }
  return bin;
}

async function waitForSupervisor(socketPath, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await new Promise((resolve, reject) => {
        const socket = net.createConnection(socketPath);
        let buffer = "";
        socket.once("error", reject);
        socket.on("data", (chunk) => {
          buffer += chunk.toString();
          const newline = buffer.indexOf("\n");
          if (newline < 0) return;
          socket.end();
          resolve(JSON.parse(buffer.slice(0, newline)));
        });
        socket.once("connect", () => {
          socket.write(`${JSON.stringify({ id: "health", method: "ping", params: {} })}\n`);
        });
      });
      if (response?.result?.runtime_fingerprint) return;
    } catch {
      // Supervisor is still starting.
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`Timed out waiting for supervisor socket ${socketPath}`);
}

async function startSupervisor(fixtures) {
  const bin = await writeProviderWrappers(fixtures);
  const protocolLog = path.join(fixtures.root, "provider-protocol.jsonl");
  const child = spawn(
    PYTHON,
    [
      "-m",
      "backend.app.agent_runtime.daemon",
      "--runtime-dir",
      fixtures.runtimeDir,
      "--socket",
      fixtures.supervisorSocketPath,
      "--registry",
      fixtures.registryPath,
    ],
    {
      cwd: ROOT,
      env: {
        ...process.env,
        PATH: `${bin}:${process.env.PATH || ""}`,
        HOME: path.join(fixtures.root, "home"),
        CODEX_HOME: path.join(fixtures.root, "codex-home"),
        CLAUDE_CONFIG_DIR: path.join(fixtures.root, "claude"),
        FAKE_ARTIFACT_TOOL: "1",
        FAKE_CODEX_SCRIPT: path.join(FIXTURES, "fake_codex_app_server.py"),
        FAKE_CLAUDE_SCRIPT: path.join(FIXTURES, "fake_claude_stream.py"),
        FAKE_CODEX_TRANSCRIPT_DIR: fixtures.sessionsDir,
        FAKE_PROTOCOL_LOG: protocolLog,
        FAKE_PYTHON: PYTHON,
        TMUX: "",
        TMUX_PANE: "",
        WIKI_AGENT_ARCHIVE_DIR: path.join(fixtures.root, "archive"),
        WIKI_AGENT_REGISTRY_PATH: fixtures.registryPath,
        WIKI_AGENT_RUNTIME_DIR: fixtures.runtimeDir,
        WIKI_AGENT_STATUS_DIR: fixtures.statusDir,
        WIKI_CODEX_ACCOUNTS_DIR: path.join(fixtures.root, "codex-accounts"),
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  let stderr = "";
  child.stderr.on("data", (chunk) => {
    stderr += chunk.toString();
  });
  await waitForSupervisor(fixtures.supervisorSocketPath);
  return {
    process: child,
    protocolLog,
    async stop() {
      if (child.exitCode === null) {
        child.kill("SIGTERM");
        await new Promise((resolve) => child.once("exit", resolve));
      }
      if (child.exitCode && stderr) throw new Error(stderr);
    },
  };
}

function artifactToolName(row) {
  const payload = row?.payload || {};
  const item = payload?.params?.item || {};
  if (item.type === "mcpToolCall") return item.tool;
  if (payload.type !== "assistant") return null;
  const toolUse = (payload.message?.content || []).find((block) => block?.type === "tool_use");
  return toolUse?.name?.split("__").at(-1) || null;
}

async function waitForArtifactFrame(fixtures, runId, timeoutMs = 15000) {
  const rawPath = path.join(fixtures.runtimeDir, "runs", runId, "raw.jsonl");
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const rows = (await fs.readFile(rawPath, "utf8"))
        .trim()
        .split("\n")
        .filter(Boolean)
        .map((line) => JSON.parse(line));
      if (rows.some((row) => artifactToolName(row) === "render_artifact")) {
        return rawPath;
      }
    } catch {
      // Run storage is still being created.
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`Run ${runId} never emitted a render_artifact tool frame`);
}

function sessionLayout(ticket) {
  return {
    version: 2,
    activeWindowId: "window-0",
    windows: [
      {
        id: "window-0",
        focusedPaneId: "pane-1",
        layout: { kind: "pane", id: "pane-1", path: `agent://${ticket}` },
      },
    ],
  };
}

async function main() {
  logStep("creating isolated registry, runtime, auth, transcript, and provider fixtures");
  const fixtures = makeFixtureRoot("wiki-89-callability-");
  await fs.writeFile(
    fixtures.registryPath,
    JSON.stringify({
      _orchestrators: {
        "WIKI-89": {
          window: "@9999",
          spawned_at: "2026-07-13T00:00:00Z",
        },
      },
    }),
  );
  writeQueue(fixtures.queuePath, "WIKI-89", []);

  const supervisor = await startSupervisor(fixtures);
  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();

  try {
    for (const provider of PROVIDERS) {
      logStep(`spawning ${provider.kind} through ${backend.baseUrl}/api/agents/spawn`);
      const response = await page.request.post(`${backend.baseUrl}/api/agents/spawn`, {
        data: {
          ticket: provider.ticket,
          kind: provider.kind,
          role: "implement",
          model: provider.model,
          effort: provider.effort,
          workdir: ROOT,
          orch: null,
          prompt: PROMPT,
        },
      });
      if (!response.ok()) {
        throw new Error(`${provider.ticket} spawn failed: ${response.status()} ${await response.text()}`);
      }
      const spawned = await response.json();
      const rawPath = await waitForArtifactFrame(fixtures, spawned.run_id);

      await page.addInitScript(({ layout }) => {
        localStorage.setItem("wiki-window-layout-v2", JSON.stringify(layout));
        localStorage.setItem("wiki-sidebar-visible", "false");
        localStorage.setItem("wiki-theme", "mono-light");
      }, { layout: sessionLayout(provider.ticket) });
      await page.goto(`${backend.baseUrl}/#/agent/${provider.ticket}`, {
        // The session event stream is intentionally long-lived, so this app
        // never reaches Playwright's networkidle state.
        waitUntil: "domcontentloaded",
      });
      const artifact = page.locator('[data-artifact-kind="mermaid"]');
      await artifact.locator(".artifact-mermaid svg").waitFor({ state: "visible" });
      await artifact.scrollIntoViewIfNeeded();
      const screenshot = `/tmp/wiki-89-${provider.kind}.png`;
      await page.screenshot({ path: screenshot, fullPage: true });
      logStep(`${provider.kind} raw frame: ${rawPath}`);
      logStep(`${provider.kind} screenshot: ${screenshot}`);
    }

    await fs.writeFile(
      "/tmp/wiki-89-fixtures.json",
      JSON.stringify(
        {
          root: fixtures.root,
          registry: fixtures.registryPath,
          runtime: fixtures.runtimeDir,
          status: fixtures.statusDir,
          queue: fixtures.queuePath,
          sessions: fixtures.sessionsDir,
          supervisor_socket: fixtures.supervisorSocketPath,
          provider_protocol: supervisor.protocolLog,
          backend: backend.baseUrl,
          screenshots: {
            cdx: "/tmp/wiki-89-cdx.png",
            cc: "/tmp/wiki-89-cc.png",
          },
        },
        null,
        2,
      ),
    );
    logStep(`isolated fixture root: ${fixtures.root}`);
  } finally {
    await context.close();
    await browser.close();
    await backend.stop();
    await supervisor.stop();
  }
}

await main();
