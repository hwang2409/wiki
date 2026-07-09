import { spawnSync } from "node:child_process";
import { rmSync, writeFileSync } from "node:fs";
import fs from "node:fs/promises";
import path from "node:path";
import { performance } from "node:perf_hooks";
import { chromium } from "playwright";

import {
  codexAssistant,
  codexUser,
  makeFixtureRoot,
  startBackend,
} from "../scripts/wiki32-harness.mjs";

const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || "/tmp/wiki-37-terminal-evidence";
const OUT_JSON = process.env.WIKI_PLAYWRIGHT_OUT_JSON || path.join(OUT_DIR, "evidence.json");
const PRIMARY_TICKET = "WIKI-370";
const SECONDARY_TICKET = "WIKI-371";

await fs.mkdir(OUT_DIR, { recursive: true });

function logStep(message) {
  console.error(`[terminal-playwright] ${message}`);
}

function percentile(values, p) {
  const sorted = [...values].sort((a, b) => a - b);
  if (sorted.length === 0) return 0;
  const index = Math.min(sorted.length - 1, Math.ceil((p / 100) * sorted.length) - 1);
  return sorted[index];
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function writeJsonl(filePath, rows) {
  writeFileSync(filePath, rows.map((row) => JSON.stringify(row)).join("\n") + "\n");
}

function writeRegistry(registryPath, transcripts) {
  writeFileSync(
    registryPath,
    JSON.stringify(
      {
        _orchestrators: Object.fromEntries(
          Object.entries(transcripts).map(([ticket, transcript]) => [
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

function buildLayout() {
  return {
    version: 2,
    activeWindowId: "window-0",
    windows: [
      {
        id: "window-0",
        focusedPaneId: "pane-1",
        layout: { kind: "pane", id: "pane-1", path: `agent://${PRIMARY_TICKET}` },
      },
      {
        id: "window-1",
        focusedPaneId: "pane-2",
        layout: { kind: "pane", id: "pane-2", path: `agent://${SECONDARY_TICKET}` },
      },
    ],
  };
}

function buildTranscript(ticket) {
  return [
    {
      type: "session_meta",
      timestamp: "2026-07-09T00:00:00Z",
      payload: {
        id: `sess-${ticket.toLowerCase()}`,
        cwd: process.cwd(),
        model: "gpt-5",
      },
    },
    codexUser(`worker transcript for ticket ${ticket}`, "2026-07-09T00:00:01Z"),
    codexAssistant(
      [
        `${ticket} transcript fixture`,
        "This pane stays read-only during terminal verification.",
        "The terminal should split next to this transcript and keep the window model intact.",
      ].join("\n"),
      "2026-07-09T00:00:02Z"
    ),
  ];
}

async function leader(page, key) {
  await page.keyboard.press("Control+a");
  await page.keyboard.press(key);
}

async function openApp(page, baseUrl) {
  const layout = buildLayout();
  await page.addInitScript(({ storedLayout }) => {
    if (!localStorage.getItem("wiki-window-layout-v2")) {
      localStorage.setItem("wiki-window-layout-v2", JSON.stringify(storedLayout));
    }
    if (!localStorage.getItem("wiki-sidebar-visible")) {
      localStorage.setItem("wiki-sidebar-visible", "false");
    }
    if (!localStorage.getItem("wiki-theme")) {
      localStorage.setItem("wiki-theme", "mono-light");
    }
  }, { storedLayout: layout });
  await page.goto(`${baseUrl}/#/agent/${PRIMARY_TICKET}`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".agent-session-surface-main .session-scroll");
}

async function getTerminalIds(page) {
  return page.evaluate(() => Object.keys(window.__wikiTerminals ?? {}));
}

async function waitForTerminal(page) {
  await page.waitForFunction(() => Object.keys(window.__wikiTerminals ?? {}).length > 0);
  const ids = await getTerminalIds(page);
  return ids[ids.length - 1];
}

async function terminalBufferText(page, terminalId) {
  return page.evaluate((id) => {
    const term = window.__wikiTerminals?.[id]?.terminal;
    if (!term) return "";
    const buffer = term.buffer.active;
    const start = Math.max(0, buffer.baseY - 250);
    const end = buffer.baseY + term.rows;
    const lines = [];
    for (let index = start; index <= end; index += 1) {
      const line = buffer.getLine(index);
      if (line) lines.push(line.translateToString(true));
    }
    return lines.join("\n");
  }, terminalId);
}

async function focusTerminal(page, terminalId) {
  await page.locator(".terminal-pane-host").click();
  await page.evaluate((id) => {
    window.__wikiTerminals?.[id]?.terminal.focus();
  }, terminalId);
  await page.waitForFunction(
    (id) =>
      document.activeElement instanceof HTMLTextAreaElement &&
      document.activeElement.dataset.terminalInput === "true" &&
      document.activeElement.dataset.terminalId === id,
    terminalId
  );
}

async function waitForBufferText(page, terminalId, text, timeout = 10000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const bufferText = await terminalBufferText(page, terminalId);
    if (bufferText.includes(text)) return;
    await page.waitForTimeout(100);
  }
  throw new Error(`Timed out waiting for terminal text: ${text}`);
}

async function runCommand(page, terminalId, command, needle, timeout = 10000) {
  await focusTerminal(page, terminalId);
  await page.keyboard.type(command);
  await page.keyboard.press("Enter");
  await waitForBufferText(page, terminalId, needle, timeout);
  return terminalBufferText(page, terminalId);
}

async function waitForFileBuffer(filePath, timeout = 5000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    try {
      return await fs.readFile(filePath);
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`Timed out waiting for file: ${filePath}`);
}

async function waitForFileText(filePath, timeout = 5000) {
  return (await waitForFileBuffer(filePath, timeout)).toString("utf8");
}

function wsBaseUrl(baseUrl) {
  return baseUrl.replace(/^http/, "ws");
}

async function openDirectTerminalSocket(baseUrl, terminalId) {
  const tokenResponse = await fetch(`${baseUrl}/api/terminal-token`);
  if (!tokenResponse.ok) {
    throw new Error(`terminal token failed: ${tokenResponse.status}`);
  }
  const { token } = await tokenResponse.json();
  return await new Promise((resolve, reject) => {
    const ws = new WebSocket(
      `${wsBaseUrl(baseUrl)}/ws/terminal/${terminalId}?token=${encodeURIComponent(token)}&create=1`
    );
    const cleanup = () => {
      ws.onopen = null;
      ws.onerror = null;
    };
    ws.onopen = () => {
      cleanup();
      resolve(ws);
    };
    ws.onerror = (event) => {
      cleanup();
      reject(new Error(`direct terminal websocket failed: ${String(event.type)}`));
    };
  });
}

async function measureDirectLatency(baseUrl) {
  const terminalId = `wiki37-latency-${Date.now()}`;
  const ws = await openDirectTerminalSocket(baseUrl, terminalId);
  let text = "";
  const listeners = new Set();
  ws.onmessage = async (event) => {
    if (typeof event.data === "string") return;
    const bytes =
      event.data instanceof ArrayBuffer
        ? new Uint8Array(event.data)
        : new Uint8Array(await event.data.arrayBuffer());
    text += Buffer.from(bytes).toString("utf8");
    listeners.forEach((listener) => listener());
  };

  function waitForText(check, timeout = 2000) {
    if (check(text)) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const deadline = setTimeout(() => {
        listeners.delete(listener);
        reject(new Error("Timed out waiting for direct latency echo"));
      }, timeout);
      const listener = () => {
        if (!check(text)) return;
        clearTimeout(deadline);
        listeners.delete(listener);
        resolve();
      };
      listeners.add(listener);
    });
  }

  try {
    await sleep(1500);
    const probe = `latency-${Date.now().toString(36)}-probe`;
    const samples = [];
    let typed = "";
    for (const char of probe) {
      typed += char;
      const started = performance.now();
      ws.send(JSON.stringify({ type: "input", data: char }));
      await waitForText((value) => value.includes(typed));
      samples.push(performance.now() - started);
    }
    ws.send(JSON.stringify({ type: "input", data: "\u0015" }));
    await sleep(100);
    return { samples, p95: percentile(samples.slice(2), 95) };
  } finally {
    ws.close();
    await sleep(100);
  }
}

async function currentLine(page, terminalId) {
  return page.evaluate((id) => {
    const term = window.__wikiTerminals?.[id]?.terminal;
    if (!term) return "";
    const buffer = term.buffer.active;
    return buffer.getLine(buffer.baseY + buffer.cursorY)?.translateToString(true) ?? "";
  }, terminalId);
}

async function measureEchoLatency(page, terminalId) {
  await focusTerminal(page, terminalId);
  await page.keyboard.type("zz");
  await page.waitForTimeout(100);
  await page.keyboard.press("Control+U");
  await page.waitForTimeout(100);

  const baseline = await currentLine(page, terminalId);
  const samples = [];
  let typed = "";
  for (const char of "latency-check-1234") {
    typed += char;
    const started = performance.now();
    await page.keyboard.type(char);
    const expected = baseline + typed;
    const deadline = Date.now() + 1000;
    let matched = false;
    while (Date.now() < deadline) {
      const line = await currentLine(page, terminalId);
      if (line.endsWith(expected)) {
        matched = true;
        break;
      }
      await page.waitForTimeout(5);
    }
    if (!matched) throw new Error(`Timed out waiting for echo: ${expected}`);
    samples.push(performance.now() - started);
  }
  await page.keyboard.press("Control+U");
  await page.waitForTimeout(100);
  return { samples, p95: percentile(samples.slice(2), 95) };
}

async function focusedPaneKind(page) {
  return page.evaluate(() => {
    const focused = document.querySelector(".pane-frame.is-focused");
    if (!focused) return null;
    if (focused.querySelector(".terminal-pane")) return "terminal";
    if (focused.querySelector(".agent-session-surface")) return "agent";
    if (focused.querySelector(".note-view")) return "note";
    return "other";
  });
}

async function activeWindowIndex(page) {
  return page.locator(".tmux-status-item.is-active .tmux-status-index").textContent();
}

async function readMarkerNumber(page, terminalId, prefix) {
  const text = await terminalBufferText(page, terminalId);
  const match = text.match(new RegExp(`${prefix}(\\d+)__`));
  return match ? Number(match[1]) : null;
}

async function frameRoundTrip(page) {
  const started = performance.now();
  await page.evaluate(
    () =>
      new Promise((resolve) => {
        requestAnimationFrame(() => resolve(null));
      })
  );
  return performance.now() - started;
}

async function wsRejectsWithoutToken(page, terminalId) {
  return page.evaluate(
    (id) =>
      new Promise((resolve) => {
        const ws = new WebSocket(`${window.location.origin.replace(/^http/, "ws")}/ws/terminal/${id}`);
        const settle = (value) => resolve(value);
        ws.onopen = () => settle("opened");
        ws.onerror = () => settle("error");
        ws.onclose = () => settle("closed");
      }),
    terminalId
  );
}

function psAlive(pid) {
  if (!pid) return false;
  const result = spawnSync("ps", ["-p", String(pid), "-o", "pid="], { encoding: "utf-8" });
  return result.status === 0 && result.stdout.trim().length > 0;
}

const fixtures = makeFixtureRoot("wiki-37-terminal-");
const transcriptPaths = {
  [PRIMARY_TICKET]: path.join(fixtures.root, `${PRIMARY_TICKET}.jsonl`),
  [SECONDARY_TICKET]: path.join(fixtures.root, `${SECONDARY_TICKET}.jsonl`),
};
const uiStatePath = path.join(fixtures.root, "ui-state.json");
const lightScreenshot = path.join(OUT_DIR, "wiki-37-terminal-light.png");
const darkScreenshot = path.join(OUT_DIR, "wiki-37-terminal-catppuccin.png");
const result = {
  activeWindowAfterTerminalSwitchBack: null,
  colsAfter: null,
  colsBefore: null,
  darkScreenshot,
  fixturePaths: {
    codexSessionsDir: fixtures.sessionsDir,
    queue: fixtures.queuePath,
    registry: fixtures.registryPath,
    root: fixtures.root,
    statusDir: fixtures.statusDir,
    transcripts: transcriptPaths,
    uiState: uiStatePath,
  },
  fixtureRootRemoved: false,
  floodFrameMs: null,
  floodBuffer: null,
  latency: null,
  lightScreenshot,
  lsSawFrontend: false,
  pageErrors: [],
  renderer: null,
  shellPid: null,
  shellPidGoneAfterClose: null,
  terminalId: null,
  themeAfter: null,
  themeBefore: null,
  topSawProcesses: false,
  windowSwitchWorked: false,
  wsWithoutToken: null,
  zoomWorked: false,
};

let backend = null;
let browser = null;

try {
  logStep("writing isolated fixtures");
  process.env.WIKI_UI_STATE_PATH = uiStatePath;
  writeJsonl(transcriptPaths[PRIMARY_TICKET], buildTranscript(PRIMARY_TICKET));
  writeJsonl(transcriptPaths[SECONDARY_TICKET], buildTranscript(SECONDARY_TICKET));
  writeRegistry(fixtures.registryPath, transcriptPaths);
  writeFileSync(fixtures.queuePath, "{}\n");

  logStep("starting isolated backend");
  backend = await startBackend(fixtures);
  logStep(`backend ready at ${backend.baseUrl}`);
  logStep("measuring direct websocket latency");
  result.latency = await measureDirectLatency(backend.baseUrl);
  logStep("launching chromium");
  browser = await chromium.launch({
    headless: true,
    args: ["--use-angle=swiftshader-webgl", "--enable-webgl"],
  });
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
  page.on("console", (message) => {
    if (message.type() === "error") {
      const text = message.text();
      if (text.includes("/ws/terminal/unauthorized-terminal") && text.includes("Unexpected response code: 403")) {
        return;
      }
      result.pageErrors.push(`console:${text}`);
    }
  });
  page.on("pageerror", (error) => {
    result.pageErrors.push(`page:${error.message}`);
  });

  logStep("opening app and creating terminal pane");
  await openApp(page, backend.baseUrl);
  await leader(page, "t");
  await page.waitForFunction(() => document.querySelectorAll(".pane-frame").length >= 2);
  let terminalId = await waitForTerminal(page);
  result.terminalId = terminalId;
  console.error(
    "[terminal-playwright] stored layout after create",
    await page.evaluate(() => localStorage.getItem("wiki-window-layout-v2"))
  );

  await focusTerminal(page, terminalId);
  await page.waitForFunction(() => document.querySelector(".pane-frame.is-focused .terminal-pane-host") !== null);

  logStep("verifying restored dead-session restart affordance");
  await page.reload({ waitUntil: "domcontentloaded" });
  console.error(
    "[terminal-playwright] stored layout after reload",
    await page.evaluate(() => localStorage.getItem("wiki-window-layout-v2"))
  );
  await page.waitForSelector(".terminal-pane-notice");
  await page.getByRole("button", { name: "Restart shell" }).click();
  await page.waitForFunction(() => !document.querySelector(".terminal-pane-overlay"));
  terminalId = await waitForTerminal(page);
  result.terminalId = terminalId;
  await focusTerminal(page, terminalId);
  try {
    await runCommand(page, terminalId, "printf '__READY__\\n'", "__READY__");
  } catch (error) {
    const debugState = await page.evaluate((id) => ({
      activeElement: document.activeElement instanceof HTMLElement
        ? {
            tag: document.activeElement.tagName,
            terminalId: document.activeElement.dataset.terminalId ?? null,
            terminalInput: document.activeElement.dataset.terminalInput ?? null,
          }
        : null,
      socketReadyState: window.__wikiTerminals?.[id]?.socketReadyState?.() ?? null,
      status: window.__wikiTerminals?.[id]?.status?.() ?? null,
    }), terminalId);
    console.error("[terminal-playwright] post-restart debug", JSON.stringify(debugState));
    console.error("[terminal-playwright] post-restart buffer", await terminalBufferText(page, terminalId));
    throw error;
  }

  logStep("running shell commands and latency checks");
  result.renderer = await page.evaluate((id) => window.__wikiTerminals?.[id]?.renderer(), terminalId);

  const lsOutput = await runCommand(page, terminalId, "ls", "frontend");
  result.lsSawFrontend = lsOutput.includes("frontend");

  const topOutput = await runCommand(page, terminalId, "top -l 1 | head", "Processes:");
  result.topSawProcesses = topOutput.includes("Processes:");

  result.themeBefore = await page.evaluate((id) => window.__wikiTerminals?.[id]?.terminal.options.theme, terminalId);

  await page.keyboard.type("printf '__''PID''__%s__\\n' \"$$\"");
  await page.keyboard.press("Enter");
  await waitForBufferText(page, terminalId, "__PID__");
  result.shellPid = await readMarkerNumber(page, terminalId, "__PID__");

  await page.keyboard.type("printf '__''COLS''__%s__\\n' \"$(tput cols)\"");
  await page.keyboard.press("Enter");
  await waitForBufferText(page, terminalId, "__COLS__");
  result.colsBefore = await readMarkerNumber(page, terminalId, "__COLS__");

  await leader(page, "h");
  await page.waitForFunction(() => document.querySelector(".pane-frame.is-focused .agent-session-surface") !== null);
  const paneAfterH = await focusedPaneKind(page);
  await leader(page, "l");
  await page.waitForFunction(() => document.querySelector(".pane-frame.is-focused .terminal-pane") !== null);
  const paneAfterL = await focusedPaneKind(page);

  await leader(page, "1");
  await page.waitForFunction(() => document.querySelector(".tmux-status-item.is-active .tmux-status-index")?.textContent === "1");
  await leader(page, "0");
  await page.waitForFunction(() => document.querySelector(".tmux-status-item.is-active .tmux-status-index")?.textContent === "0");
  result.activeWindowAfterTerminalSwitchBack = await activeWindowIndex(page);
  result.windowSwitchWorked =
    paneAfterH === "agent" && paneAfterL === "terminal" && result.activeWindowAfterTerminalSwitchBack === "0";

  logStep("checking zoom and resize behavior");
  const visibleTerminalHosts = await page.locator(".terminal-pane-host").count();
  if (visibleTerminalHosts === 0) {
    const postSwitchSummary = await page.evaluate(() => ({
      activeWindowIndex:
        document.querySelector(".tmux-status-item.is-active .tmux-status-index")?.textContent ?? null,
      locationHash: window.location.hash,
      storedLayout: localStorage.getItem("wiki-window-layout-v2"),
      paneLabels: [...document.querySelectorAll(".pane-frame")]
        .map((node) => node.getAttribute("data-pane-key"))
        .filter(Boolean),
      terminalTitles: [...document.querySelectorAll(".terminal-pane-title")].map((node) => node.textContent?.trim()),
    }));
    console.error("[terminal-playwright] post-switch summary", JSON.stringify(postSwitchSummary));
  }
  await focusTerminal(page, terminalId);
  const divider = page.locator(".pane-divider.row").first();
  const dividerBeforeZoomCount = await page.locator(".pane-divider.row").count();
  await leader(page, "z");
  await page.waitForFunction(() => document.querySelectorAll(".pane-divider.row").length === 0);
  const dividerDuringZoomCount = await page.locator(".pane-divider.row").count();
  await leader(page, "z");
  await page.waitForFunction(() => document.querySelectorAll(".pane-divider.row").length > 0);
  const dividerAfterZoomCount = await page.locator(".pane-divider.row").count();
  result.zoomWorked =
    dividerBeforeZoomCount > 0 && dividerDuringZoomCount === 0 && dividerAfterZoomCount > 0;

  await page.evaluate(() => {
    const split = document.querySelector(".pane-split.row");
    if (!(split instanceof HTMLElement)) throw new Error("Pane split not found");
    split.style.setProperty("--split-ratio", "0.78");
  });
  await page.waitForTimeout(500);

  await page.keyboard.type("printf '__''COLS''__%s__\\n' \"$(tput cols)\"");
  await page.keyboard.press("Enter");
  await page.waitForTimeout(250);
  result.colsAfter = await readMarkerNumber(page, terminalId, "__COLS__");

  logStep("running flood responsiveness checks");
  await focusTerminal(page, terminalId);
  await page.keyboard.type("yes | head -c 10000000");
  await page.keyboard.press("Enter");
  await page.waitForTimeout(150);
  result.floodFrameMs = await frameRoundTrip(page);
  await page.waitForTimeout(1200);
  result.floodBuffer = await page.evaluate((id) => {
    const term = window.__wikiTerminals?.[id]?.terminal;
    if (!term) return null;
    return {
      baseY: term.buffer.active.baseY,
      length: term.buffer.active.length,
      rows: term.rows,
      scrollback: term.options.scrollback,
    };
  }, terminalId);
  await page.waitForTimeout(500);

  logStep("capturing theme evidence and auth rejection");
  await page.screenshot({ path: lightScreenshot, fullPage: true });
  await page.getByLabel("Settings").click();
  await page.getByRole("radio", { name: "Catppuccin Mocha" }).click();
  await page.getByLabel("Close settings").click();
  await page.waitForFunction(
    (id) => {
      const theme = window.__wikiTerminals?.[id]?.terminal.options.theme;
      return Boolean(theme?.background && theme.background !== "#ffffff");
    },
    terminalId
  );
  result.themeAfter = await page.evaluate((id) => window.__wikiTerminals?.[id]?.terminal.options.theme, terminalId);
  await page.screenshot({ path: darkScreenshot, fullPage: true });

  result.wsWithoutToken = await wsRejectsWithoutToken(page, "unauthorized-terminal");

  logStep("closing pane and checking shell cleanup");
  await leader(page, "x");
  await page.waitForTimeout(500);
  result.shellPidGoneAfterClose = result.shellPid ? !psAlive(result.shellPid) : null;

  if (result.renderer !== "webgl") {
    throw new Error(`Expected WebGL renderer, saw ${result.renderer}`);
  }
  if (!result.lsSawFrontend) {
    throw new Error("Expected ls output to include frontend");
  }
  if (!result.topSawProcesses) {
    throw new Error("Expected top output to include Processes:");
  }
  if (!result.latency || result.latency.p95 >= 30) {
    throw new Error(`Expected p95 latency under 30ms, saw ${result.latency?.p95}`);
  }
  if (!result.windowSwitchWorked) {
    throw new Error("Leader pane/window chords did not move focus as expected");
  }
  if (!result.zoomWorked) {
    throw new Error("Leader zoom chord did not hide and restore the divider");
  }
  if (!result.colsBefore || !result.colsAfter) {
    throw new Error(`Expected tput cols markers, saw ${result.colsBefore} -> ${result.colsAfter}`);
  }
  if (!result.floodBuffer || result.floodBuffer.baseY < 1000) {
    throw new Error("Flood command did not produce enough terminal output to validate scrollback");
  }
  if (result.floodBuffer.length > (result.floodBuffer.scrollback ?? 5000) + result.floodBuffer.rows + 8) {
    throw new Error(`Scrollback cap exceeded: ${JSON.stringify(result.floodBuffer)}`);
  }
  if (result.floodFrameMs >= 500) {
    throw new Error(`UI was unresponsive during flood for ${result.floodFrameMs}ms`);
  }
  if (!(result.wsWithoutToken === "closed" || result.wsWithoutToken === "error")) {
    throw new Error(`Expected unauthorized websocket to fail, saw ${result.wsWithoutToken}`);
  }
  if (result.shellPidGoneAfterClose !== true) {
    throw new Error(`Expected shell pid ${result.shellPid} to exit after pane close`);
  }
  if (!result.themeBefore || !result.themeAfter || result.themeBefore.background === result.themeAfter.background) {
    throw new Error("Expected terminal palette to change after theme switch");
  }
  if (result.pageErrors.length > 0) {
    throw new Error(result.pageErrors.join("\n"));
  }
} finally {
  logStep("shutting down browser/backend and deleting fixture root");
  if (browser) await browser.close().catch(() => {});
  if (backend) await backend.stop().catch(() => {});
  try {
    rmSync(fixtures.root, { force: true, recursive: true });
    result.fixtureRootRemoved = true;
  } catch {
    result.fixtureRootRemoved = false;
  }
}

await fs.writeFile(OUT_JSON, JSON.stringify(result, null, 2));
console.log(JSON.stringify(result, null, 2));
