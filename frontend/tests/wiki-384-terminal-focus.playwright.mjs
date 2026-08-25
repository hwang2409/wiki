import { writeFileSync } from "node:fs";
import fs from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";

import { codexAssistant, codexUser, makeFixtureRoot, startBackend } from "../scripts/wiki32-harness.mjs";

const TICKET_ONE = "WIKI-384";
const TICKET_TWO = "WIKI-385";
const TICKET_THREE = "WIKI-386";

process.env.SHELL = "/bin/bash";

function writeJsonl(filePath, rows) {
  writeFileSync(filePath, rows.map((row) => JSON.stringify(row)).join("\n") + "\n");
}

function transcript(ticket) {
  return [
    {
      type: "session_meta",
      timestamp: "2026-08-25T00:00:00Z",
      payload: { id: `sess-${ticket.toLowerCase()}`, cwd: process.cwd(), model: "gpt-5" },
    },
    codexUser(`focus fixture for ${ticket}`, "2026-08-25T00:00:01Z"),
    codexAssistant(`focus fixture transcript for ${ticket}`, "2026-08-25T00:00:02Z"),
  ];
}

function writeRegistry(fixtures, transcripts) {
  const entries = Object.fromEntries(
    Object.entries(transcripts).map(([ticket, transcriptPath]) => [
      ticket,
      { window: "@9999", spawned_at: "2026-08-25T00:00:00Z", transcript: transcriptPath },
    ])
  );
  writeFileSync(fixtures.registryPath, JSON.stringify({ _orchestrators: entries }, null, 2));
  writeFileSync(fixtures.queuePath, "{}\n");
}

async function leader(page, key) {
  await page.keyboard.press("Control+a");
  await page.keyboard.press(key);
}

async function assertTerminalFocus(page, terminalId, paneId) {
  await page.waitForFunction(
    ({ id, key }) =>
      document.activeElement instanceof HTMLTextAreaElement &&
      document.activeElement.dataset.terminalInput === "true" &&
      document.activeElement.dataset.terminalId === id &&
      document.activeElement.closest(`[data-pane-key="${key}"]`) !== null,
    { id: terminalId, key: paneId }
  );
}

async function assertFrameFocus(page, paneId) {
  await page.waitForFunction(
    (key) =>
      document.activeElement instanceof HTMLDivElement &&
      document.activeElement.classList.contains("pane-frame") &&
      document.activeElement.dataset.paneKey === key,
    paneId
  );
}

async function createTerminal(page) {
  const previousIds = await page.evaluate(() => Object.keys(window.__wikiTerminals ?? {}));
  await leader(page, "t");
  await page.waitForFunction(
    (ids) => Object.keys(window.__wikiTerminals ?? {}).some((id) => !ids.includes(id)),
    previousIds
  );
  const terminalId = await page.evaluate(
    (ids) => Object.keys(window.__wikiTerminals ?? {}).find((id) => !ids.includes(id)),
    previousIds
  );
  if (!terminalId) throw new Error("Terminal id was not created");
  await page.waitForFunction(
    (id) => window.__wikiTerminals?.[id]?.status?.() === "live",
    terminalId
  );
  const paneId = await page.evaluate((id) => {
    const pane = [...document.querySelectorAll(".terminal-pane")].find(
      (candidate) => candidate.getAttribute("aria-label") === `Terminal: ${id}`
    );
    return pane?.closest(".pane-frame")?.getAttribute("data-pane-key") ?? null;
  }, terminalId);
  if (!paneId) throw new Error(`Terminal pane frame was not found for ${terminalId}`);
  return { paneId, terminalId };
}

const fixtures = makeFixtureRoot("wiki-384-focus-");
const transcriptOne = path.join(fixtures.root, "WIKI-384.jsonl");
const transcriptTwo = path.join(fixtures.root, "WIKI-385.jsonl");
const transcriptThree = path.join(fixtures.root, "WIKI-386.jsonl");
writeJsonl(transcriptOne, transcript(TICKET_ONE));
writeJsonl(transcriptTwo, transcript(TICKET_TWO));
writeJsonl(transcriptThree, transcript(TICKET_THREE));
writeRegistry(fixtures, {
  [TICKET_ONE]: transcriptOne,
  [TICKET_TWO]: transcriptTwo,
  [TICKET_THREE]: transcriptThree,
});

const layout = {
  version: 2,
  activeWindowId: "window-0",
  windows: [
    {
      id: "window-0",
      focusedPaneId: "pane-agent-one",
      layout: {
        kind: "split",
        direction: "row",
        ratio: 0.5,
        first: { kind: "pane", id: "pane-agent-one", path: `agent://${TICKET_ONE}` },
        second: { kind: "pane", id: "pane-agent-two", path: `agent://${TICKET_TWO}` },
      },
    },
    {
      id: "window-1",
      focusedPaneId: "pane-agent-three",
      layout: { kind: "pane", id: "pane-agent-three", path: `agent://${TICKET_THREE}` },
    },
  ],
};

let backend = null;
let browser = null;
const result = {
  leaderTerminalFocus: false,
  nonTerminalFrameFocus: false,
  windowSwitchTerminalFocus: false,
  paneCloseTerminalFocus: false,
  pageErrors: [],
};

try {
  backend = await startBackend(fixtures);
  browser = await chromium.launch({
    headless: true,
    args: ["--use-angle=swiftshader-webgl", "--enable-webgl"],
  });
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
  page.on("pageerror", (error) => result.pageErrors.push(error.message));
  await page.addInitScript((storedLayout) => {
    localStorage.setItem("wiki-window-layout-v2", JSON.stringify(storedLayout));
    localStorage.setItem("wiki-sidebar-visible", "false");
  }, layout);

  await page.goto(`${backend.baseUrl}/#/agent/${TICKET_ONE}`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".agent-session-surface-main .session-scroll");

  const terminalOne = await createTerminal(page);
  await leader(page, "k");
  await assertFrameFocus(page, "pane-agent-one");
  await leader(page, "j");
  await assertTerminalFocus(page, terminalOne.terminalId, terminalOne.paneId);
  result.leaderTerminalFocus = true;

  await page.locator(".tmux-status-item").nth(1).click();
  const terminalTwo = await createTerminal(page);
  await page.locator(".tmux-status-item").nth(0).click();
  await page.locator(".tmux-status-item").nth(1).click();
  await assertTerminalFocus(page, terminalTwo.terminalId, terminalTwo.paneId);
  result.windowSwitchTerminalFocus = true;

  await leader(page, "k");
  await assertFrameFocus(page, "pane-agent-three");
  result.nonTerminalFrameFocus = true;
  await leader(page, "x");
  await assertTerminalFocus(page, terminalTwo.terminalId, terminalTwo.paneId);
  result.paneCloseTerminalFocus = true;

  if (result.pageErrors.length > 0) throw new Error(result.pageErrors.join("\n"));
} finally {
  if (browser) await browser.close().catch(() => {});
  if (backend) await backend.stop().catch(() => {});
  await fs.rm(fixtures.root, { force: true, recursive: true }).catch(() => {});
}

console.log(JSON.stringify(result, null, 2));
