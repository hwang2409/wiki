import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { chromium } from "playwright";

import {
  codexAssistant,
  codexUser,
  makeFixtureRoot,
  startBackend,
  writeQueue,
  writeRegistry,
} from "../scripts/wiki32-harness.mjs";

const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || "/tmp/wiki-108-sidebar-pages";
const TICKET = "WIKI-108";
const MAIN_SCREENSHOT = path.join(OUT_DIR, "worker-main-window.png");
const AGENTS_SCREENSHOT = path.join(OUT_DIR, "agents-page-window.png");

function workspaceLayout() {
  return {
    version: 2,
    activeWindowId: "window-0",
    windows: [
      {
        id: "window-0",
        focusedPaneId: "pane-1",
        layout: { kind: "pane", id: "pane-1", path: `agent://${TICKET}` },
      },
    ],
  };
}

function transcript() {
  return [
    codexUser("Keep the focused worker session intact.", "2026-07-14T12:00:00Z"),
    codexAssistant("The worker stays focused in its own workspace window.", "2026-07-14T12:00:01Z"),
  ];
}

async function storedWorkspace(page) {
  return page.evaluate(() => {
    const raw = localStorage.getItem("wiki-window-layout-v2");
    if (!raw) throw new Error("Missing persisted workspace layout");
    return JSON.parse(raw);
  });
}

function windowForUtility(workspace, kind) {
  const expectedPath = `utility://${kind}`;
  return workspace.windows.find(
    (window) => window.layout?.kind === "pane" && window.layout.path === expectedPath,
  );
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function waitForUtilityWindow(page, kind, expectedCount) {
  await page.waitForFunction(
    ({ expectedKind, count }) => {
      const raw = localStorage.getItem("wiki-window-layout-v2");
      if (!raw) return false;
      const workspace = JSON.parse(raw);
      if (workspace.windows.length !== count || window.location.hash !== `#/${expectedKind}`) return false;
      const active = workspace.windows.find((window) => window.id === workspace.activeWindowId);
      return active?.layout?.kind === "pane" && active.layout.path === `utility://${expectedKind}`;
    },
    { expectedKind: kind, count: expectedCount },
  );
}

async function openSidebarPage(page, label, kind, expectedCount) {
  // WIKI-296 folded every destination into the sidebar's Views section, so
  // every label — including the ones WIKI-151 had bucketed under a ribbon
  // overflow menu — is one click on a Views row.
  await page.locator(`[data-testid="sidebar-views"] [aria-label="${label}"]`).click();
  await waitForUtilityWindow(page, kind, expectedCount);
}

async function main() {
  mkdirSync(OUT_DIR, { recursive: true });
  const fixtures = makeFixtureRoot("wiki-108-sidebar-pages-");
  process.env.WIKI_UI_STATE_PATH = path.join(fixtures.root, "ui-state.json");
  process.env.WIKI_TOKEN_CACHE_PATH = path.join(fixtures.root, "token-cache.json");

  const transcriptPath = path.join(fixtures.root, `${TICKET}.jsonl`);
  writeFileSync(transcriptPath, transcript().map((row) => JSON.stringify(row)).join("\n") + "\n");
  writeRegistry(fixtures.registryPath, [[TICKET, transcriptPath]]);
  writeQueue(fixtures.queuePath, TICKET);

  let browser;
  let backend;
  try {
    backend = await startBackend(fixtures);
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));

    await page.addInitScript(({ layout }) => {
      localStorage.setItem("wiki-window-layout-v2", JSON.stringify(layout));
      localStorage.setItem("wiki-sidebar-visible", "true");
    }, { layout: workspaceLayout() });
    await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
    await page.locator(".session-scroll").waitFor({ state: "visible" });
    await page.screenshot({ path: MAIN_SCREENSHOT, fullPage: true });

    const initial = await storedWorkspace(page);
    const originalWindow = JSON.stringify(initial.windows[0]);

    await openSidebarPage(page, "Agents", "agents", 2);
    await page.screenshot({ path: AGENTS_SCREENSHOT, fullPage: true });
    const agentsWindow = await storedWorkspace(page);
    const agentsWindowId = agentsWindow.activeWindowId;
    assert(JSON.stringify(agentsWindow.windows[0]) === originalWindow, "Agents replaced the focused worker window");

    await openSidebarPage(page, "Agents", "agents", 2);
    const refocusedAgents = await storedWorkspace(page);
    assert(refocusedAgents.activeWindowId === agentsWindowId, "Second Agents click did not focus its existing window");
    assert(windowForUtility(refocusedAgents, "agents"), "Agents workspace window was not retained");

    await openSidebarPage(page, "Token usage", "tokens", 3);
    await openSidebarPage(page, "Activity feed", "activity", 4);
    await openSidebarPage(page, "Graph view", "graph", 5);
    await openSidebarPage(page, "Note freshness", "health", 6);

    const allPages = await storedWorkspace(page);
    for (const kind of ["agents", "tokens", "activity", "graph", "health"]) {
      assert(windowForUtility(allPages, kind), `Missing dedicated ${kind} workspace window`);
    }
    assert(JSON.stringify(allPages.windows[0]) === originalWindow, "A sidebar page changed the worker window");

    await page.locator(".tmux-status-item").first().click();
    await page.waitForFunction(
      (ticket) =>
        window.location.hash === `#/agent/${ticket}` &&
        document.querySelector(".agent-session-surface .session-ticket")?.textContent === ticket,
      TICKET,
    );
    const restored = await storedWorkspace(page);
    assert(restored.activeWindowId === "window-0", "Could not return to the original worker window");
    assert(JSON.stringify(restored.windows[0]) === originalWindow, "Restored worker window changed after sidebar navigation");

    const standaloneContext = await browser.newContext({ viewport: { width: 1200, height: 900 } });
    const standalonePage = await standaloneContext.newPage();
    await standalonePage.addInitScript(() => {
      localStorage.setItem("wiki-window-layout-v2", JSON.stringify({ version: 2, activeWindowId: null, windows: [] }));
    });
    await standalonePage.goto(`${backend.baseUrl}/#/agents`, { waitUntil: "domcontentloaded" });
    await waitForUtilityWindow(standalonePage, "agents", 1);
    await standalonePage.locator(".agents-view").waitFor({ state: "visible" });
    await standaloneContext.close();

    if (errors.length > 0) throw new Error(errors.join("\n"));
    console.log("WIKI-108 sidebar page windows: PASS");
    console.log(`WIKI-108 fixtures: ${fixtures.root}`);
    console.log(`WIKI-108 screenshots: ${MAIN_SCREENSHOT}, ${AGENTS_SCREENSHOT}`);
  } finally {
    if (browser) await browser.close();
    if (backend) await backend.stop();
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
