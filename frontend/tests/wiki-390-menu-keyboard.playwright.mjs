import { writeFileSync } from "node:fs";
import path from "node:path";
import { chromium } from "playwright";

import {
  makeFixtureRoot,
  startBackend,
  writeQueue,
  writeRegistry,
} from "../scripts/wiki32-harness.mjs";

const TICKET = "WIKI-390";

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function artifactTranscript() {
  const source = Array.from({ length: 120 }, (_, index) => `const line${index + 1} = ${index + 1};`).join("\n");
  const artifacts = [
    ["11111111-1111-4111-8111-111111111111", "First artifact"],
    ["22222222-2222-4222-8222-222222222222", "Second artifact"],
  ];
  const rows = [{ type: "mode", mode: "normal", sessionId: "wiki-390-keyboard" }];
  for (const [index, [id, title]] of artifacts.entries()) {
    const toolId = `toolu_wiki390_${index}`;
    rows.push({
      type: "assistant",
      timestamp: `2026-08-26T12:00:0${index * 2}Z`,
      message: {
        role: "assistant",
        content: [{
          type: "tool_use",
          id: toolId,
          name: "mcp__wiki-artifacts__render_artifact",
          input: { kind: "code", title, payload: { language: "typescript", filename: `${title}.ts`, source } },
        }],
      },
    });
    rows.push({
      type: "user",
      timestamp: `2026-08-26T12:00:0${index * 2 + 1}Z`,
      message: {
        role: "user",
        content: [{
          type: "tool_result",
          tool_use_id: toolId,
          content: JSON.stringify({ artifact_id: id, ok: true }),
        }],
      },
    });
  }
  return rows;
}

const fixtures = makeFixtureRoot("wiki-390-menu-keyboard-");
const transcriptPath = path.join(fixtures.root, "WIKI-390.jsonl");
writeFileSync(transcriptPath, `${artifactTranscript().map((row) => JSON.stringify(row)).join("\n")}\n`);
writeRegistry(fixtures.registryPath, [[TICKET, transcriptPath]]);
writeQueue(fixtures.queuePath, TICKET, []);

const agentPayload = {
  workers: [{
    ticket: "WIKI-390-WORKER",
    registered: true,
    window: null,
    window_alive: true,
    run_id: "run-worker",
    runtime_state: "working",
    control_attached: true,
    provider_pid: 390,
    kind: "cdx",
    role: "implement",
    model: "gpt-5",
    effort: "medium",
    worktree: "/tmp/wiki-390-worker",
    log: null,
    orch: null,
    session: null,
    spawned_at: "2026-08-26T12:00:00Z",
    state: "working",
    pr: null,
    step: "keyboard behavior",
    blocker: null,
    status_age_seconds: 1,
    latest_event_at: null,
    latest_event_seq: null,
    last_viewed_at: null,
    last_viewed_seq: null,
  }],
  orchestrators: [{
    id: "WIKI-390-ORCH",
    window: null,
    window_alive: true,
    run_id: "run-orchestrator",
    runtime_state: "working",
    control_attached: true,
    provider_pid: 391,
    cwd: "/tmp/wiki-390-orchestrator",
    kind: "cc",
    model: "claude",
    effort: null,
    spawned_at: "2026-08-26T12:00:00Z",
    transcript_exists: true,
    log: null,
  }],
  archived: [],
  error: null,
  account_notices: [],
};

let browser;
let backend;

try {
  backend = await startBackend(fixtures);
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });

  const sessionPage = await context.newPage();
  await sessionPage.addInitScript(() => {
    localStorage.setItem("wiki-window-layout-v2", JSON.stringify({
      version: 2,
      activeWindowId: "window-0",
      windows: [{
        id: "window-0",
        focusedPaneId: "pane-1",
        layout: { kind: "pane", id: "pane-1", path: "agent://WIKI-390" },
      }],
    }));
    localStorage.setItem("wiki-sidebar-visible", "false");
  });
  await sessionPage.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
  await sessionPage.locator(".session-scroll").waitFor();
  const openButtons = sessionPage.getByRole("button", { name: "Open in panel" });
  await sessionPage.locator(".artifact-block").first().waitFor();
  await openButtons.first().waitFor();
  assert(await openButtons.count() === 2, "fixture should expose two panel triggers");

  await openButtons.first().focus();
  await sessionPage.keyboard.press("Enter");
  const artifactPanel = sessionPage.getByRole("complementary", { name: "Artifact panel" });
  await artifactPanel.waitFor();
  await openButtons.nth(1).focus();
  await sessionPage.keyboard.press("Enter");
  const tabs = artifactPanel.getByRole("tab");
  await tabs.nth(1).waitFor();
  await tabs.first().focus();
  await sessionPage.keyboard.press("ArrowRight");
  await sessionPage.waitForFunction(() => document.activeElement?.getAttribute("role") === "tab" && document.activeElement?.textContent?.trim() === "Second artifact");

  await artifactPanel.getByRole("button", { name: "Close First artifact" }).click({ force: true });
  await sessionPage.waitForFunction(() => document.activeElement?.getAttribute("role") === "tab" && document.activeElement?.textContent?.trim() === "Second artifact");
  await artifactPanel.getByRole("button", { name: "Close Second artifact" }).click({ force: true });
  await sessionPage.waitForFunction(() => document.activeElement?.getAttribute("aria-label") === "Artifact panel");

  const artifactMenuTrigger = artifactPanel.getByRole("button", { name: "Artifact panel menu" });
  await artifactMenuTrigger.focus();
  await sessionPage.keyboard.press("Enter");
  const artifactMenu = artifactPanel.getByRole("menu");
  const artifactMenuItems = artifactMenu.getByRole("menuitem");
  await artifactMenuItems.nth(1).waitFor();
  await sessionPage.waitForFunction(() => document.activeElement?.textContent?.trim() === "Second artifact");
  await sessionPage.keyboard.press("ArrowDown");
  await sessionPage.waitForFunction(() => document.activeElement?.textContent?.trim() === "First artifact");
  await sessionPage.keyboard.press("Escape");
  await sessionPage.waitForFunction(() => document.activeElement?.getAttribute("aria-label") === "Artifact panel menu");
  await artifactPanel.getByRole("button", { name: "Close artifact panel" }).click();
  await sessionPage.waitForFunction(() => document.activeElement?.textContent?.trim() === "Open in panel");

  const agentsPage = await context.newPage();
  await agentsPage.route("**/api/agents*", async (route) => {
    await route.fulfill({ json: agentPayload });
  });
  await agentsPage.addInitScript(() => localStorage.setItem("wiki-sidebar-visible", "true"));
  await agentsPage.goto(`${backend.baseUrl}/#/agents`, { waitUntil: "domcontentloaded" });
  await agentsPage.locator(".agents-view").waitFor();

  const workerTrigger = agentsPage.getByRole("button", { name: "More actions for WIKI-390-WORKER" });
  await workerTrigger.focus();
  await agentsPage.keyboard.press("Enter");
  const workerMenu = agentsPage.locator('[data-agent-menu-for="WIKI-390-WORKER"] [role="menu"]');
  await workerMenu.getByRole("menuitem").nth(1).waitFor();
  await agentsPage.waitForFunction(() => document.activeElement?.textContent?.trim() === "Stop");
  await agentsPage.keyboard.press("ArrowDown");
  await agentsPage.waitForFunction(() => document.activeElement?.textContent?.trim() === "Replace");
  await agentsPage.keyboard.press("Home");
  await agentsPage.waitForFunction(() => document.activeElement?.textContent?.trim() === "Stop");
  await agentsPage.keyboard.press("Escape");
  await agentsPage.waitForFunction(() => document.activeElement?.getAttribute("aria-label") === "More actions for WIKI-390-WORKER");
  await agentsPage.keyboard.press("Enter");
  await workerMenu.getByRole("menuitem").first().waitFor();
  await agentsPage.waitForFunction(() => document.activeElement?.textContent?.trim() === "Stop");
  await agentsPage.keyboard.press("Tab");
  await workerMenu.waitFor({ state: "detached" });

  const orchestratorTrigger = agentsPage.getByRole("button", { name: "More actions for WIKI-390-ORCH" });
  await orchestratorTrigger.focus();
  await agentsPage.keyboard.press("Enter");
  const orchestratorMenu = agentsPage.locator('[data-agent-menu-for="WIKI-390-ORCH"] [role="menu"]');
  await orchestratorMenu.getByRole("menuitem").nth(1).waitFor();
  await agentsPage.waitForFunction(() => document.activeElement?.textContent?.trim() === "Stop");
  await agentsPage.keyboard.press("ArrowDown");
  await agentsPage.waitForFunction(() => document.activeElement?.textContent?.trim() === "Replace");
  await agentsPage.keyboard.press("Escape");
  await agentsPage.waitForFunction(() => document.activeElement?.getAttribute("aria-label") === "More actions for WIKI-390-ORCH");

  console.log("WIKI-390 menu keyboard: PASS");
} finally {
  if (browser) await browser.close();
  if (backend) await backend.stop();
}
