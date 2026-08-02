import fs from "node:fs/promises";
import { rmSync } from "node:fs";
import { chromium } from "playwright";

import { makeFixtureRoot, startBackend } from "../scripts/wiki32-harness.mjs";

const fixtures = makeFixtureRoot("wiki-154-agents-cold-start-");
let backend;
let browser;

try {
  await fs.writeFile(fixtures.registryPath, "{}\n");
  await fs.writeFile(fixtures.queuePath, "{}\n");
  backend = await startBackend(fixtures);
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  let workspaceRequests = 0;

  await page.route("**/api/workspaces", async (route) => {
    workspaceRequests += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        workspaces: [{ id: "wiki", root: "/tmp/wiki-154-active-workspace", live: true }],
      }),
    });
  });
  await page.route("**/api/agents/spawn-orchestrator", async (route) => {
    const request = route.request();
    const body = JSON.parse(request.postData() ?? "{}");
    if (body.workdir !== "/tmp/wiki-154-active-workspace") {
      throw new Error(`unexpected orchestrator workdir: ${body.workdir}`);
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        window: null,
        run_id: "cold-start-orchestrator-run",
        log: null,
        prompt_path: null,
        note: "spawned",
      }),
    });
  });

  await page.addInitScript(() => {
    localStorage.setItem("wiki-sidebar-visible", "true");
    localStorage.setItem("wiki-sidebar-tab", "agents");
    localStorage.removeItem("wiki-window-layout-v2");
  });
  await page.goto(`${backend.baseUrl}/`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector('.sidebar-mode[data-mode="agents"]');
  await page.getByRole("button", { name: "Agents", exact: true }).click();
  await page.getByRole("button", { name: "Spawn orchestrator", exact: true }).click();

  await page.getByLabel("Name").fill("wiki-lead");
  await page.getByLabel("Initial goal").fill("prepare the fleet");
  await page.getByRole("button", { name: /^Launch$/ }).click();
  await page.getByRole("button", { name: /Confirm launch/ }).waitFor();

  // Name and goal alone reach confirmation. The discovered root supplies the
  // hidden project directory used by the final spawn request.
  const spawnResponse = page.waitForResponse("**/api/agents/spawn-orchestrator");
  await page.getByRole("button", { name: /Confirm launch/ }).click();
  await spawnResponse;
  if (workspaceRequests < 1) {
    throw new Error("Agents cold start did not request workspace metadata");
  }
} finally {
  if (browser) await browser.close();
  if (backend) await backend.stop();
  rmSync(fixtures.root, { recursive: true, force: true });
}
