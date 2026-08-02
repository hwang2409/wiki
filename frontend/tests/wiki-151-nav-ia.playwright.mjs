import fs from "node:fs/promises";
import { mkdirSync, rmSync } from "node:fs";
import path from "node:path";
import { chromium } from "playwright";

import {
  codexAssistant,
  makeFixtureRoot,
  startBackend,
  writeQueue,
  writeRegistry,
} from "../scripts/wiki32-harness.mjs";

const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || "/tmp/wiki-151-nav-ia";
mkdirSync(OUT_DIR, { recursive: true });

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

let browser;
let backend;
const fixtures = makeFixtureRoot("wiki-151-nav-ia-");

async function ribbonZoneCount(page) {
  return page.evaluate(() => {
    const ribbon = document.querySelector('[data-testid="workspace-ribbon"]');
    if (!(ribbon instanceof HTMLElement)) return -1;
    return ribbon.querySelectorAll(".ribbon-zone").length;
  });
}

async function ribbonZoneNames(page) {
  return page.evaluate(() => {
    const ribbon = document.querySelector('[data-testid="workspace-ribbon"]');
    if (!(ribbon instanceof HTMLElement)) return [];
    return Array.from(ribbon.querySelectorAll(".ribbon-zone")).map((zone) =>
      zone.getAttribute("data-zone"),
    );
  });
}

async function sidebarModeTitle(page) {
  return page.evaluate(() => {
    const title = document.querySelector(".sidebar-mode-title-label");
    return title instanceof HTMLElement ? title.textContent?.trim() ?? "" : null;
  });
}

async function sidebarModeTitleY(page) {
  return page.evaluate(() => {
    const title = document.querySelector(".sidebar-mode-title");
    if (!(title instanceof HTMLElement)) return null;
    return Math.round(title.getBoundingClientRect().top);
  });
}

async function switchSidebarTo(page, tab) {
  const label =
    tab === "files" ? "Files" : tab === "search" ? "Search" : "Agent list";
  await page.click(`[data-testid="workspace-ribbon"] [aria-label="${label}"]`);
  await page.waitForSelector(`.sidebar-mode[data-mode="${tab}"]`);
}

try {
  // A live worker (working state) — must be sorted before an idle one; also a
  // blocked worker (highest attention) — must be sorted first regardless of
  // recency. Verifies WIKI-151 attention-recency sort.
  const activeTranscript = path.join(fixtures.root, "active.jsonl");
  await fs.writeFile(
    activeTranscript,
    [
      { type: "mode", mode: "normal", sessionId: "wiki-151-active" },
      codexAssistant("Nav IA fixture.", "2026-07-22T15:00:00Z"),
    ]
      .map((row) => JSON.stringify(row))
      .join("\n") + "\n",
  );

  const workerLive = {
    ticket: "WIKI-151-LIVE",
    provider: "codex",
    kind: "cdx",
    role: "implement",
    state: "working",
    orch: "wiki",
    run_id: null,
    provider_pid: process.pid,
    control_attached: true,
    provider_session_id: null,
    worktree: fixtures.root,
    cwd: fixtures.root,
    model: "sol",
    effort: null,
    transcript: null,
    log: null,
    window: null,
    spawned_at: "2026-07-22T15:00:00Z",
  };
  const workerBlocked = { ...workerLive, ticket: "WIKI-151-BLOCK", state: "blocked" };
  const workerIdle = { ...workerLive, ticket: "WIKI-151-IDLE", state: "idle" };

  const registry = {
    _orchestrators: {
      wiki: {
        window: "@9999",
        spawned_at: "2026-07-22T14:00:00Z",
        transcript: activeTranscript,
      },
    },
    "WIKI-151-LIVE": { history: [], current: workerLive },
    "WIKI-151-BLOCK": { history: [], current: workerBlocked },
    "WIKI-151-IDLE": { history: [], current: workerIdle },
  };
  await fs.writeFile(fixtures.registryPath, JSON.stringify(registry, null, 2));
  writeQueue(fixtures.queuePath, "WIKI-151-LIVE", []);

  // Seed status files for the three workers (fresh mtime → recency after
  // attention).
  for (const [ticket, state] of [
    ["WIKI-151-LIVE", "working"],
    ["WIKI-151-BLOCK", "blocked"],
    ["WIKI-151-IDLE", "idle"],
  ]) {
    await fs.writeFile(
      path.join(fixtures.statusDir, `${ticket}.json`),
      JSON.stringify({ state, pr: null, step: "seeded", blocker: null }),
    );
  }

  backend = await startBackend(fixtures);
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  page.on("console", (msg) => {
    if (msg.type() === "error") console.error("[browser]", msg.text());
  });
  page.on("pageerror", (err) => console.error("[browser pageerror]", err.message));

  await page.addInitScript(() => {
    localStorage.setItem("wiki-sidebar-visible", "true");
    if (!localStorage.getItem("wiki-sidebar-tab")) {
      localStorage.setItem("wiki-sidebar-tab", "files");
    }
    // WIKI-151 HIGH#1 regression: pre-seed the persisted files-workspace to
    // an unavailable id so we exercise the "preserve unavailable selection"
    // branch. The test later asserts activeWorkspace stayed "phoebe" instead
    // of silently swapping onto "wiki".
    if (!localStorage.getItem("wiki-files-workspace")) {
      localStorage.setItem("wiki-files-workspace", "phoebe");
    }
  });

  // WIKI-151 round-2 HIGH#1: seed a NON-EMPTY vault so the wiki tree would
  // populate `tree.files/folders` under a naïve fallback. If the unavailable
  // branch is missing (or rendered after the tree check), phoebe-selected
  // would silently show these wiki notes.
  await page.route("**/api/notes", async (route) => {
    if (route.request().method() !== "GET") {
      await route.fallback();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([
        {
          id: "seed-wiki-note",
          path: "seed.md",
          title: "Seed",
          created_at: "2026-07-22T15:00:00Z",
          updated_at: "2026-07-22T15:00:00Z",
          excerpt: null,
          links: [],
          backlinks: [],
          size: 32,
        },
      ]),
    });
  });

  // Mock /api/workspaces with one live + one unavailable root so we can verify
  // the friendly-label + unavailable badge treatments without needing real
  // discovery.
  await page.route("**/api/workspaces", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        workspaces: [
          { id: "wiki", root: "/Users/henry/me/fun/wiki", live: true },
          { id: "phoebe", root: "/Users/henry/work/phoebe", live: false },
        ],
      }),
    });
  });

  await page.goto(`${backend.baseUrl}/`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".app-container");
  await page.waitForSelector('[data-testid="workspace-ribbon"]');

  // 1. Ribbon: exactly three zones, ordered content / run-management / app-controls.
  const zoneCount = await ribbonZoneCount(page);
  assert(zoneCount === 3, `expected 3 ribbon zones, got ${zoneCount}`);
  const zoneNames = await ribbonZoneNames(page);
  assert(
    JSON.stringify(zoneNames) === JSON.stringify(["content", "run-management", "app-controls"]),
    `unexpected ribbon zone order: ${zoneNames.join(",")}`,
  );

  // 2. Ribbon overflow menu: opens, contains ≥ one secondary utility, primary
  // destinations (new note, files, agents, settings, theme) remain 1-click.
  const primaryLabels = [
    "New note",
    "Files",
    "Search",
    "Agent list",
    "Agents",
    "Settings",
  ];
  for (const label of primaryLabels) {
    const count = await page
      .locator(`[data-testid="workspace-ribbon"] [aria-label="${label}"]`)
      .count();
    assert(count === 1, `ribbon primary "${label}" expected exactly 1, got ${count}`);
  }
  await page.click('[data-testid="ribbon-more-button"]');
  await page.waitForSelector('[data-testid="ribbon-more-menu"]');
  const overflowItems = await page.locator('[data-testid="ribbon-more-menu"] [role="menuitem"]').count();
  assert(overflowItems >= 3, `overflow menu expected ≥ 3 items, got ${overflowItems}`);
  // Close menu again by clicking the button.
  await page.click('[data-testid="ribbon-more-button"]');

  // 3. Sidebar shell: mode-title present in all three modes; align at same
  // vertical offset across modes (single-line header, no stacked headers).
  await page.waitForSelector('.sidebar-mode[data-mode="files"]');
  const filesTitle = await sidebarModeTitle(page);
  assert(filesTitle === "Files", `files mode title expected "Files", got ${filesTitle}`);
  const filesTitleY = await sidebarModeTitleY(page);

  await switchSidebarTo(page, "search");
  const searchTitle = await sidebarModeTitle(page);
  assert(searchTitle === "Search", `search mode title expected "Search", got ${searchTitle}`);
  const searchTitleY = await sidebarModeTitleY(page);

  await switchSidebarTo(page, "agents");
  const agentsTitle = await sidebarModeTitle(page);
  assert(agentsTitle === "Runs", `agents mode title expected "Runs", got ${agentsTitle}`);
  const agentsTitleY = await sidebarModeTitleY(page);

  assert(
    filesTitleY !== null && searchTitleY !== null && agentsTitleY !== null,
    "sidebar mode titles must render in every mode",
  );
  assert(
    Math.abs(filesTitleY - searchTitleY) <= 1 && Math.abs(filesTitleY - agentsTitleY) <= 1,
    `sidebar mode titles must align vertically: files=${filesTitleY} search=${searchTitleY} agents=${agentsTitleY}`,
  );

  // 4. Tab header removed (WIKI-151 doctrine — bottom-rail tabs are canonical).
  const tabHeaderCount = await page
    .locator(".workspace-tab-header")
    .count();
  assert(tabHeaderCount === 0, `WIKI-151 removed .workspace-tab-header, got ${tabHeaderCount}`);

  // 5. Session list under agents mode: assert group headers, attention-recency
  // sort, standardized row anatomy, exactly ONE dot per row.
  await page.waitForSelector('[data-testid="nav-agents-group-active"]');
  const groupTexts = await page.evaluate(() =>
    Array.from(document.querySelectorAll(".nav-agents-group-title")).map((el) =>
      (el.firstElementChild?.textContent ?? "").trim(),
    ),
  );
  assert(groupTexts.includes("Active"), `expected Active group, got ${groupTexts.join("/")}`);

  // WIKI-235: orchestrator children start collapsed. Expand the wiki group
  // before checking WIKI-151 row anatomy and attention ordering.
  await page.getByRole("button", { name: "Expand wiki workers" }).click();
  await page.waitForSelector('[data-testid="nav-orch-workers-wiki"]');

  const rowOrder = await page.evaluate(() =>
    Array.from(document.querySelectorAll(".nav-agent"))
      .filter((el) => !el.classList.contains("is-orch"))
      .map((el) => ({
        ticket: el.querySelector(".nav-agent-ticket")?.textContent?.trim() ?? "",
        state: el.getAttribute("data-state"),
        dotCount: el.querySelectorAll(
          '.nav-agent-unread, .nav-agent-viewed-failure, .nav-agent-dot',
        ).length,
      })),
  );
  const rowsByTicket = Object.fromEntries(rowOrder.map((r) => [r.ticket, r]));
  for (const ticket of ["WIKI-151-BLOCK", "WIKI-151-LIVE", "WIKI-151-IDLE"]) {
    assert(rowsByTicket[ticket], `expected row for ${ticket}`);
    assert(
      rowsByTicket[ticket].dotCount <= 1,
      `${ticket} must render ≤ 1 dot, got ${rowsByTicket[ticket].dotCount}`,
    );
    // WIKI-151 removed the state dot entirely; runtime state lives in the meta
    // text color. Every row must have zero dot elements when unread + failure
    // are absent (fixture rows have never been viewed via the sidebar).
    assert(
      rowsByTicket[ticket].dotCount === 0,
      `${ticket} must not carry a legacy state dot (got ${rowsByTicket[ticket].dotCount}); ` +
        `runtime state should live in meta text only`,
    );
  }
  const activeWorkerTickets = rowOrder
    .filter((r) => r.state !== "archived" && r.state !== "orchestrator")
    .map((r) => r.ticket);
  const idxBlock = activeWorkerTickets.indexOf("WIKI-151-BLOCK");
  const idxLive = activeWorkerTickets.indexOf("WIKI-151-LIVE");
  const idxIdle = activeWorkerTickets.indexOf("WIKI-151-IDLE");
  assert(idxBlock >= 0 && idxLive >= 0 && idxIdle >= 0, "all three workers must be listed");
  assert(
    idxBlock < idxLive && idxLive < idxIdle,
    `attention sort broken: block=${idxBlock} live=${idxLive} idle=${idxIdle}`,
  );

  // 6. Workspace selector (files mode): friendly label + abbreviated path;
  // unavailable roots must be marked visibly. Go back to files first.
  await switchSidebarTo(page, "files");
  await page.waitForSelector('[data-testid="workspace-select"]');
  const optionLabels = await page.evaluate(() =>
    Array.from(document.querySelectorAll('[data-testid="workspace-select"] option')).map((opt) => ({
      text: opt.textContent ?? "",
      disabled: opt.hasAttribute("disabled"),
      value: opt.value,
    })),
  );
  const wikiOpt = optionLabels.find((o) => o.value === "wiki");
  const phoebeOpt = optionLabels.find((o) => o.value === "phoebe");
  assert(wikiOpt, "wiki workspace option missing");
  assert(phoebeOpt, "phoebe workspace option missing");
  assert(
    wikiOpt.text.startsWith("wiki") && wikiOpt.text.includes("/"),
    `wiki friendly label must include an abbreviated path hint, got "${wikiOpt.text}"`,
  );
  assert(
    phoebeOpt.text.includes("(unavailable)"),
    `unavailable workspace must be marked "(unavailable)", got "${phoebeOpt.text}"`,
  );

  // WIKI-151 HIGH#1 regression: the persisted `phoebe` selection must remain
  // active even though discovery reports live:false. Prior code silently
  // fell back to "wiki" here, and Playwright would have observed the wiki
  // option as the <select>'s current value.
  const selectedValue = await page.locator('[data-testid="workspace-select"]').inputValue();
  assert(
    selectedValue === "phoebe",
    `persisted unavailable workspace must stay selected — expected "phoebe", got "${selectedValue}"`,
  );

  // WIKI-151 round-2 HIGH#1 regression: with a NON-EMPTY vault (seeded above)
  // and the selection stuck on unavailable phoebe, the file explorer body
  // MUST render the "Workspace unavailable" state — NOT the wiki tree the
  // frontend loaded from /api/notes. Previously the render checked
  // `tree.files/folders` before workspaceUnavailable, so the seeded wiki
  // note leaked into a phoebe-selected sidebar.
  const unavailableBodyCount = await page
    .locator('[data-testid="workspace-unavailable"]')
    .count();
  assert(
    unavailableBodyCount === 1,
    `unavailable workspace body missing: expected 1 [data-testid="workspace-unavailable"], got ${unavailableBodyCount}`,
  );
  const leakedTreeItems = await page
    .locator('.sidebar-mode[data-mode="files"] .nav-files-container .tree-item')
    .count();
  assert(
    leakedTreeItems === 0,
    `wiki tree items must not leak into a phoebe-selected sidebar — saw ${leakedTreeItems}`,
  );

  await page.screenshot({ path: path.join(OUT_DIR, "nav-ia.png") });

  // 7. File explorer retry: enable the all-files loader (so the effect at
  // App.tsx:1619 actually fires) on a LIVE workspace, intercept
  // /api/files/tree to fail, then click Retry and prove a SECOND request was
  // issued. The previous version of this block never enabled all-files
  // loading and never reached the failure branch — meaning it silently
  // passed even when retry was broken (HIGH#2).
  let treeCalls = 0;
  await page.unroute("**/api/files/tree*");
  await page.route("**/api/files/tree*", async (route) => {
    treeCalls += 1;
    await route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({ detail: `boom-${treeCalls}` }),
    });
  });
  // Switch to the live wiki workspace so the effect actually runs (the
  // unavailable phoebe workspace short-circuits the fetch), then enable
  // showAllFiles.
  await page.selectOption('[data-testid="workspace-select"]', "wiki");
  await page.evaluate(() => {
    localStorage.setItem("wiki-show-all-files", "true");
    localStorage.setItem("wiki-sidebar-tab", "files");
    localStorage.setItem("wiki-files-workspace", "wiki");
  });
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForSelector('.sidebar-mode[data-mode="files"]');
  await page.waitForSelector('[data-testid="files-fetch-error"]', { timeout: 15_000 });
  assert(treeCalls >= 1, `expected initial /api/files/tree fetch, saw ${treeCalls}`);
  const callsBeforeRetry = treeCalls;

  // Click Retry — this must re-issue the request EXACTLY once. Round-2
  // review HIGH#2: the prior implementation bumped shared refreshTick,
  // which also re-ran workspace-discovery; discovery then triggered a
  // second file request in the same click (observed before=1 after=3).
  // The file-scoped nonce must keep this exactly-one.
  await page.locator('[data-testid="files-fetch-error"] .nav-inline-retry').click();
  const deadline = Date.now() + 8_000;
  while (treeCalls <= callsBeforeRetry && Date.now() < deadline) {
    await page.waitForTimeout(100);
  }
  // Settle for a beat to let any straggler request land — then assert exact
  // count. If the retry fanout leaks back, treeCalls will exceed
  // callsBeforeRetry + 1 here.
  await page.waitForTimeout(1_000);
  assert(
    treeCalls === callsBeforeRetry + 1,
    `Retry must issue exactly ONE additional /api/files/tree — before=${callsBeforeRetry} after=${treeCalls}`,
  );

  // 8. Archived-only state: no active rows render, but the copy must not say
  // the user has never had a run. Archived sessions remain available outside
  // this sidebar.
  await page.route("**/api/agents", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        workers: [],
        archived: [
          {
            ticket: "WIKI-ARCHIVED",
            archived_at: "2026-07-20T12:00:00Z",
            kind: "cdx",
            role: "implement",
            model: "sol",
            outcome: "completed",
            state: "completed",
            pr: null,
            step: "done",
          },
        ],
        orchestrators: [],
        deploy_timestamp: null,
      }),
    });
  });
  await page.evaluate(() => localStorage.setItem("wiki-sidebar-tab", "agents"));
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForSelector('.sidebar-mode[data-mode="agents"]');
  await page.waitForSelector('[data-testid="nav-agents-empty"]', { timeout: 10_000 });
  const emptyTitle = await page.locator(".nav-empty-title").textContent();
  assert(emptyTitle?.trim() === "No active runs", `empty state title expected "No active runs", got ${emptyTitle}`);
  const primary = await page.locator('[data-testid="nav-agents-empty-primary"]').count();
  assert(primary === 1, `expected 1 primary CTA, got ${primary}`);
  const secondary = await page.locator('[data-testid="nav-agents-empty-secondary"]').count();
  assert(secondary === 1, `expected 1 secondary link, got ${secondary}`);

  await page.screenshot({ path: path.join(OUT_DIR, "empty-cta.png") });

  await fs.writeFile(
    path.join(OUT_DIR, "summary.json"),
    JSON.stringify(
      {
        ribbon_zones: zoneNames,
        overflow_items: overflowItems,
        sidebar_mode_titles: { files: filesTitle, search: searchTitle, agents: agentsTitle },
        sort_order: activeWorkerTickets,
        workspace_labels: optionLabels,
      },
      null,
      2,
    ),
  );
} finally {
  if (browser) await browser.close();
  if (backend) await backend.stop();
  rmSync(fixtures.root, { recursive: true, force: true });
}
