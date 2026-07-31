import fs from "node:fs/promises";
import { rmSync } from "node:fs";
import { chromium } from "playwright";

import { makeFixtureRoot, startBackend } from "../scripts/wiki32-harness.mjs";

const fixtures = makeFixtureRoot("wiki-154-workspace-readiness-");
let backend;
let browser;

async function runScenario({ failure, customPath }) {
  const context = await browser.newContext();
  const page = await context.newPage();
  let releaseDiscovery;
  const discoveryReleased = new Promise((resolve) => {
    releaseDiscovery = resolve;
  });
  let releaseRefreshDiscovery;
  const refreshDiscoveryReleased = new Promise((resolve) => {
    releaseRefreshDiscovery = resolve;
  });
  let workspaceRequests = 0;
  let submittedWorkdir = null;

  if (!failure) {
    await page.route("**/api/events", async (route) => {
      await route.fulfill({
        status: 200,
        headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" },
        body: 'data: {"type":"session","ticket":"WIKI-REFRESH"}\n\n',
      });
    });
  }
  await page.route("**/api/workspaces", async (route) => {
    workspaceRequests += 1;
    if (failure) {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: "workspace discovery failed" }),
      });
      return;
    }
    if (workspaceRequests === 1) {
      await discoveryReleased;
    } else {
      await refreshDiscoveryReleased;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        workspaces: [{ id: "phoebe", root: "/tmp/phoebe-workspace", live: true }],
      }),
    });
  });
  await page.route("**/api/models", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        models: [{
          id: "claude-sonnet",
          label: "Claude Sonnet",
          kind: "cc",
          provider: "claude",
          supports_reasoning_effort: false,
          default_worker: false,
          default_orchestrator: true,
        }],
      }),
    });
  });
  await page.route("**/api/agents/spawn-orchestrator", async (route) => {
    const body = JSON.parse(route.request().postData() ?? "{}");
    submittedWorkdir = body.workdir;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        window: null,
        run_id: `workspace-readiness-${failure ? "failed" : "delayed"}`,
        log: null,
        prompt_path: null,
        note: "spawned",
      }),
    });
  });

  await page.addInitScript(() => {
    localStorage.setItem("wiki-sidebar-visible", "true");
    localStorage.setItem("wiki-sidebar-tab", "agents");
    localStorage.setItem("wiki-files-workspace", "phoebe");
    localStorage.removeItem("wiki-window-layout-v2");
  });
  await page.goto(`${backend.baseUrl}/`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector('.sidebar-mode[data-mode="agents"]');
  await page.getByRole("button", { name: "Agents", exact: true }).click();
  await page.getByRole("button", { name: "Spawn orchestrator", exact: true }).click();

  const projectDir = page.getByPlaceholder("/tmp/project");
  await projectDir.waitFor();
  await page.getByLabel("Name").fill("wiki-lead");
  const launch = page.getByRole("button", { name: /^Launch$/ });
  if (!(await launch.isDisabled())) {
    throw new Error("launch must stay disabled while no workspace root is ready");
  }
  await projectDir.fill(customPath);

  if (!failure) {
    const workspaceResponse = page.waitForResponse("**/api/workspaces");
    releaseDiscovery();
    await workspaceResponse;
    releaseRefreshDiscovery();
    const advanced = page.getByRole("button", { name: /Advanced/ });
    await advanced.click();
    if ((await projectDir.inputValue()) !== customPath) {
      throw new Error("workspace discovery overwrote the user's project directory");
    }

    const workspaceRequestsBeforeRefresh = workspaceRequests;
    await page.waitForTimeout(1_000);
    if (workspaceRequests !== workspaceRequestsBeforeRefresh) {
      throw new Error(
        `a session refresh retriggered workspace discovery: before=${workspaceRequestsBeforeRefresh} after=${workspaceRequests}`,
      );
    }
    if ((await projectDir.inputValue()) !== customPath || (await launch.isDisabled())) {
      throw new Error("workspace root and launch readiness changed during refresh");
    }
  }

  await page.waitForFunction(() => {
    const button = document.querySelector("button.dialog-confirm");
    return button instanceof HTMLButtonElement && !button.disabled;
  });
  const dialog = page.locator("form.agent-spawn-modal");
  await dialog.evaluate((form) => form.requestSubmit());
  await page.getByRole("button", { name: /Confirm launch/ }).waitFor();
  const spawnResponse = page.waitForResponse("**/api/agents/spawn-orchestrator");
  await dialog.evaluate((form) => form.requestSubmit());
  await spawnResponse;
  if (submittedWorkdir !== customPath) {
    throw new Error(`unexpected workspace submitted: ${submittedWorkdir}`);
  }
  if (submittedWorkdir === "/Users/henry/me/fun/wiki") {
    throw new Error("Wiki fallback must never be submitted");
  }
  releaseRefreshDiscovery();
  await context.close();
}

try {
  await fs.writeFile(fixtures.registryPath, "{}\n");
  await fs.writeFile(fixtures.queuePath, "{}\n");
  backend = await startBackend(fixtures);
  browser = await chromium.launch({ headless: true });
  await runScenario({ failure: false, customPath: "/tmp/custom-delayed-workspace" });
  await runScenario({ failure: true, customPath: "/tmp/custom-failed-workspace" });
} finally {
  if (browser) await browser.close();
  if (backend) await backend.stop();
  rmSync(fixtures.root, { recursive: true, force: true });
}
