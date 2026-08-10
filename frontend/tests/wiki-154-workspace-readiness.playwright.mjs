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

async function runTopologyScenario() {
  const context = await browser.newContext();
  const page = await context.newPage();
  let workspaceRequests = 0;

  await page.route("**/api/workspaces", async (route) => {
    workspaceRequests += 1;
    const workspaces =
      workspaceRequests <= 2
        ? [{ id: "wiki", root: "/tmp/wiki", live: true }]
        : workspaceRequests === 3
          ? [
              { id: "wiki", root: "/tmp/wiki", live: true },
              { id: "new-orchestrator", root: "/tmp/new-orchestrator", live: true },
            ]
          : [{ id: "wiki", root: "/tmp/wiki", live: true }];
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ workspaces }),
    });
  });
  await page.addInitScript(() => {
    class TestEventSource {
      static instance;
      onmessage = null;
      constructor() {
        TestEventSource.instance = this;
      }
      close() {}
    }
    window.EventSource = TestEventSource;
    window.__wikiEmitAgentEvent = (payload) => {
      TestEventSource.instance?.onmessage?.({ data: JSON.stringify(payload) });
    };
    localStorage.setItem("wiki-sidebar-visible", "true");
    localStorage.setItem("wiki-sidebar-tab", "agents");
    localStorage.removeItem("wiki-window-layout-v2");
  });
  const initialWorkspaceResponse = page.waitForResponse("**/api/workspaces");
  await page.goto(`${backend.baseUrl}/`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector('.sidebar-mode[data-mode="agents"]');
  await initialWorkspaceResponse;
  await page.getByRole("button", { name: "Files", exact: true }).click();
  await page.waitForFunction(() => document.querySelectorAll("#root select[data-testid=workspace-select] option").length === 1);

  const requestsBeforeSession = workspaceRequests;
  await page.evaluate(() => window.__wikiEmitAgentEvent({ type: "session", ticket: "WIKI-SESSION" }));
  await page.waitForTimeout(500);
  if (workspaceRequests !== requestsBeforeSession) {
    throw new Error("a session event retriggered workspace discovery");
  }

  await page.evaluate(() => window.__wikiEmitAgentEvent({ type: "agents", tickets: ["WIKI-NEW"] }));
  await page.waitForFunction(() => document.querySelector('option[value="new-orchestrator"]') !== null);

  await page.evaluate(() => window.__wikiEmitAgentEvent({ type: "agents", tickets: ["WIKI-NEW"] }));
  await page.waitForFunction(() => document.querySelector('option[value="new-orchestrator"]') === null);
  if (workspaceRequests !== 4) {
    throw new Error(`expected one discovery request per topology event, got ${workspaceRequests}`);
  }
  await context.close();
}

async function runTopologyDialogScenario({ failure }) {
  const context = await browser.newContext();
  const page = await context.newPage();
  let workspaceRequests = 0;
  let releaseRefresh;
  const refreshReleased = new Promise((resolve) => {
    releaseRefresh = resolve;
  });

  await page.route("**/api/workspaces", async (route) => {
    workspaceRequests += 1;
    if (workspaceRequests <= 2) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          workspaces: [{ id: "wiki", root: "/tmp/verified-workspace", live: true }],
        }),
      });
      return;
    }
    if (failure) {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: "topology refresh failed" }),
      });
      return;
    }
    await refreshReleased;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        workspaces: [{ id: "wiki", root: "/tmp/verified-workspace", live: true }],
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
  await page.addInitScript(() => {
    class TestEventSource {
      static instance;
      onmessage = null;
      constructor() {
        TestEventSource.instance = this;
      }
      close() {}
    }
    window.EventSource = TestEventSource;
    window.__wikiEmitAgentEvent = (payload) => {
      TestEventSource.instance?.onmessage?.({ data: JSON.stringify(payload) });
    };
    localStorage.setItem("wiki-sidebar-visible", "true");
    localStorage.setItem("wiki-sidebar-tab", "agents");
    localStorage.removeItem("wiki-window-layout-v2");
  });

  const initialWorkspaceResponse = page.waitForResponse("**/api/workspaces");
  await page.goto(`${backend.baseUrl}/`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector('.sidebar-mode[data-mode="agents"]');
  await initialWorkspaceResponse;
  const agentsWorkspaceResponse = page.waitForResponse("**/api/workspaces");
  await page.getByRole("button", { name: "Agents", exact: true }).click();
  await agentsWorkspaceResponse;
  await page.getByRole("button", { name: "Spawn orchestrator", exact: true }).click();
  await page.getByLabel("Name").fill("wiki-lead");
  await page.getByRole("button", { name: /Advanced/ }).click();

  const projectDir = page.getByPlaceholder("/tmp/project");
  const launch = page.getByRole("button", { name: /^Launch$/ });
  await projectDir.waitFor();
  if ((await projectDir.inputValue()) !== "/tmp/verified-workspace" || (await launch.isDisabled())) {
    throw new Error("verified workspace root did not enable the untouched spawn dialog");
  }

  const refreshRequest = page.waitForRequest("**/api/workspaces");
  const refreshResponse = page.waitForResponse("**/api/workspaces");
  await page.evaluate(() => window.__wikiEmitAgentEvent({ type: "agents", tickets: ["WIKI-REFRESH"] }));
  await refreshRequest;
  if ((await projectDir.inputValue()) !== "/tmp/verified-workspace" || (await launch.isDisabled())) {
    throw new Error(`topology ${failure ? "failure" : "delay"} cleared the verified spawn root`);
  }
  if (failure) {
    await refreshResponse;
  } else {
    releaseRefresh();
    await refreshResponse;
    await page.waitForTimeout(100);
  }
  if ((await projectDir.inputValue()) !== "/tmp/verified-workspace" || (await launch.isDisabled())) {
    throw new Error(`topology ${failure ? "failure" : "delay"} changed spawn readiness`);
  }
  await context.close();
}

async function runProductionNoticeRefreshScenario() {
  const context = await browser.newContext();
  const page = await context.newPage();
  let eventEmitted = false;
  let agentRequests = 0;

  await page.route("**/api/agents**", async (route) => {
    agentRequests += 1;
    const accountNotices = eventEmitted
      ? []
      : [{
          type: "codex_auth_dead_exhausted",
          provider: "codex",
          failure: "auth",
          credential_source: "current",
          exhausted: true,
          tickets: ["WIKI-9"],
          run_ids: { "WIKI-9": "run-current" },
          ts: "2026-07-31T00:00:00Z",
        }];
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        workers: [],
        orchestrators: [],
        archived: [],
        account_notices: accountNotices,
      }),
    });
  });
  await page.route("**/api/workspaces", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        workspaces: [{ id: "wiki", root: "/tmp/wiki", live: true }],
      }),
    });
  });
  await page.route("**/api/models", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ models: [] }),
    });
  });
  await page.addInitScript(() => {
    class TestEventSource {
      static instance;
      onmessage = null;
      constructor() {
        TestEventSource.instance = this;
      }
      close() {}
    }
    window.EventSource = TestEventSource;
    window.__wikiEmitAgentEvent = (payload) => {
      TestEventSource.instance?.onmessage?.({ data: JSON.stringify(payload) });
    };
    localStorage.setItem("wiki-sidebar-visible", "true");
    localStorage.setItem("wiki-sidebar-tab", "agents");
    localStorage.removeItem("wiki-window-layout-v2");
  });

  await page.goto(`${backend.baseUrl}/`, { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: "Agents", exact: true }).click();
  await page.getByText(/Sign in to Codex again/).waitFor();
  const requestsBeforeEvent = agentRequests;
  eventEmitted = true;
  const refetch = page.waitForResponse("**/api/agents**");
  await page.evaluate(() =>
    window.__wikiEmitAgentEvent({
      type: "codex_auth_verified",
      provider: "codex",
      credential_source: "current",
      success: true,
      ticket: "WIKI-9",
      run_id: "run-current",
      ts: "2026-07-31T00:01:00Z",
    }),
  );
  await refetch;
  await page.waitForFunction(() => !document.body.textContent?.includes("Sign in to Codex again"));
  if (agentRequests <= requestsBeforeEvent) {
    throw new Error("codex auth verification did not refetch agents through App");
  }
  await context.close();
}

try {
  await fs.writeFile(fixtures.registryPath, "{}\n");
  await fs.writeFile(fixtures.queuePath, "{}\n");
  backend = await startBackend(fixtures);
  browser = await chromium.launch({ headless: true });
  await runScenario({ failure: false, customPath: "/tmp/custom-delayed-workspace" });
  await runScenario({ failure: true, customPath: "/tmp/custom-failed-workspace" });
  await runTopologyScenario();
  await runTopologyDialogScenario({ failure: false });
  await runTopologyDialogScenario({ failure: true });
  await runProductionNoticeRefreshScenario();
} finally {
  if (browser) await browser.close();
  if (backend) await backend.stop();
  rmSync(fixtures.root, { recursive: true, force: true });
}
