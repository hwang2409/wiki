// WIKI-234/235: asserts that the terminal-native language survives the runs
// declutter: orchestrators disclose active workers, archive groups stay out of
// the sidebar, and session chrome stays flat and rectangular.
import fs from "node:fs/promises";
import { mkdirSync } from "node:fs";
import path from "node:path";
import { chromium } from "playwright";

import {
  codexAssistant,
  codexUser,
  makeFixtureRoot,
  startBackend,
  writeQueue,
} from "../scripts/wiki32-harness.mjs";

const HERE = path.dirname(new URL(import.meta.url).pathname);
const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || path.join(HERE, "evidence", "wiki-235");
mkdirSync(OUT_DIR, { recursive: true });

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function logStep(message) {
  console.error(`[wiki-234-playwright] ${message}`);
}

function isoAtStartOfDayOffset(offsetDays, plusSeconds) {
  const now = new Date();
  const day = new Date(now.getFullYear(), now.getMonth(), now.getDate() - offsetDays);
  return new Date(day.getTime() + plusSeconds * 1000).toISOString();
}

function archiveStamp(iso) {
  return iso.replace(/[-:]/g, "").replace("T", "-").slice(0, 15);
}

let browser;
let backend;
const fixtures = makeFixtureRoot("wiki-234-restyle-");

try {
  const transcript = path.join(fixtures.root, "restyle.jsonl");
  await fs.writeFile(
    transcript,
    [
      { type: "mode", mode: "normal", sessionId: "wiki-234-restyle" },
      codexUser("hey wiki, adopt the opencode look", "2026-07-22T15:00:00Z"),
      codexAssistant("Terminal-native restyle applied.", "2026-07-22T15:00:05Z"),
    ]
      .map((row) => JSON.stringify(row))
      .join("\n") + "\n",
  );

  const workerBase = {
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
    transcript,
    log: null,
    window: null,
    spawned_at: "2026-07-22T15:00:00Z",
  };
  const registry = {
    _orchestrators: {
      wiki: {
        window: "@9999",
        spawned_at: "2026-07-22T14:00:00Z",
        transcript,
      },
    },
    "WIKI-301": { history: [], current: { ...workerBase, ticket: "WIKI-301" } },
    "WIKI-302": { history: [], current: { ...workerBase, ticket: "WIKI-302", state: "blocked" } },
    "WIKI-303": { history: [], current: { ...workerBase, ticket: "WIKI-303", state: "merge-ready" } },
    "FREE-1": { history: [], current: { ...workerBase, ticket: "FREE-1", orch: null } },
  };
  await fs.writeFile(fixtures.registryPath, JSON.stringify(registry, null, 2));
  writeQueue(fixtures.queuePath, "WIKI-301", []);
  for (const ticket of ["WIKI-301", "WIKI-302", "WIKI-303", "FREE-1"]) {
    await fs.writeFile(
      path.join(fixtures.statusDir, `${ticket}.json`),
      JSON.stringify({ state: registry[ticket].current.state, pr: null, step: "seeded", blocker: null }),
    );
  }

  // Three archives spanning the time groups: today, yesterday, earlier.
  const archives = [
    ["ARC-TODAY", isoAtStartOfDayOffset(0, 90)],
    ["ARC-YDAY", isoAtStartOfDayOffset(1, 90)],
    ["ARC-OLD", "2026-07-13T13:00:00.000Z"],
  ];
  for (const [ticket, endedAt] of archives) {
    const sessionDir = path.join(fixtures.root, "archive", ticket, archiveStamp(endedAt));
    await fs.mkdir(sessionDir, { recursive: true });
    await fs.writeFile(
      path.join(sessionDir, "final-status.json"),
      JSON.stringify({ state: "completed", step: "done", pr: null }, null, 2),
    );
    await fs.writeFile(
      path.join(sessionDir, "meta.json"),
      JSON.stringify(
        {
          outcome: "archived",
          ended_at: endedAt,
          worker: { ...workerBase, ticket, orch: null, ended_at: endedAt, outcome: "archived" },
          history: [],
          source: "wiki-234-playwright",
        },
        null,
        2,
      ),
    );
  }

  backend = await startBackend(fixtures);
  browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

  logStep("state 1: default theme is opencode");
  await page.goto(`${backend.baseUrl}/`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("#root > *");
  const theme = await page.evaluate(() => document.documentElement.dataset.theme);
  assert(theme === "opencode", `default theme must be opencode, got ${theme}`);
  const bodyFont = await page.evaluate(() => getComputedStyle(document.body).fontFamily);
  assert(/mono/i.test(bodyFont), `opencode chrome must be mono, got ${bodyFont}`);

  logStep("state 2: orchestrator-first sidebar with persisted disclosure");
  await page.click('[data-testid="workspace-ribbon"] [aria-label="Agent list"]');
  await page.waitForSelector('[data-testid="nav-agents-group-active"]');
  const initialRows = await page.evaluate(() =>
    Array.from(document.querySelectorAll(".nav-agent")).map((el) =>
      el.querySelector(".nav-agent-ticket")?.textContent?.trim() ?? "",
    ),
  );
  assert(
    JSON.stringify(initialRows) === JSON.stringify(["wiki", "FREE-1"]),
    `collapsed sidebar must show orchestrators then ungrouped workers, got ${initialRows.join("/")}`,
  );
  const sidebarText = await page.locator(".nav-agents").innerText();
  for (const removed of ["Today", "Yesterday", "Earlier", "ARC-TODAY", "ARC-YDAY", "ARC-OLD"]) {
    assert(!sidebarText.includes(removed), `archived sidebar chrome must omit ${removed}`);
  }
  assert((await page.locator(".nav-agent-num").count()) === 0, "session gutter numbers must be removed");

  const wikiRow = page.locator('.nav-agent.is-orch', { hasText: "wiki" });
  assert((await wikiRow.getAttribute("aria-expanded")) === "false", "wiki workers must start collapsed");
  await wikiRow.click();
  const wikiWorkers = page.locator('[data-testid="nav-orch-workers-wiki"]');
  await wikiWorkers.waitFor();
  const expandedTickets = await wikiWorkers.locator(".nav-agent-ticket").allInnerTexts();
  assert(
    JSON.stringify(expandedTickets) === JSON.stringify(["WIKI-302", "WIKI-303", "WIKI-301"]),
    `attention order must be blocked, merge-ready, working; got ${expandedTickets.join("/")}`,
  );
  const disclosureStyle = await wikiRow.evaluate((el) => ({
    radius: Number.parseFloat(getComputedStyle(el).borderRadius),
    height: el.getBoundingClientRect().height,
    chevron: el.querySelector(".nav-orch-chevron") !== null,
  }));
  assert(disclosureStyle.radius <= 2, `orchestrator row radius must be <=2px, got ${disclosureStyle.radius}`);
  assert(disclosureStyle.height >= 40, `orchestrator row hit area must be >=40px, got ${disclosureStyle.height}`);
  assert(disclosureStyle.chevron, "orchestrator disclosure needs a visible chevron");

  await page.screenshot({ path: path.join(OUT_DIR, "01-sidebar-normal.png") });
  await page.setViewportSize({ width: 1000, height: 760 });
  const narrowLayout = await page.locator(".nav-agents").evaluate((el) => ({
    clientWidth: el.clientWidth,
    scrollWidth: el.scrollWidth,
  }));
  assert(
    narrowLayout.scrollWidth <= narrowLayout.clientWidth,
    `narrow sidebar must not overflow: ${narrowLayout.scrollWidth} > ${narrowLayout.clientWidth}`,
  );
  await page.screenshot({ path: path.join(OUT_DIR, "02-sidebar-narrow.png") });
  await page.setViewportSize({ width: 1440, height: 900 });

  await wikiRow.click();
  assert((await page.locator('[data-testid="nav-orch-workers-wiki"]').count()) === 0, "second click must collapse workers");
  await wikiRow.click();
  await wikiWorkers.waitFor();
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForSelector('[data-testid="nav-orch-workers-wiki"]');

  logStep("state 3: accent-bar user block + quiet composer strip");
  // The "wiki" orchestrator shares the native transcript fixture, which the
  // backend serves without a live provider attached.
  await page.goto(`${backend.baseUrl}/#/agent/wiki`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".session-scroll");
  const userBlock = page.locator(".session-user", { hasText: "opencode look" });
  await userBlock.waitFor();
  const blockStyle = await userBlock.evaluate((el) => {
    const style = getComputedStyle(el);
    return { borderLeftWidth: style.borderLeftWidth, alignSelf: style.alignSelf };
  });
  assert(
    blockStyle.borderLeftWidth === "3px",
    `user block must carry a 3px left accent bar, got ${blockStyle.borderLeftWidth}`,
  );
  assert(
    blockStyle.alignSelf === "stretch",
    `user block must stretch full width, got ${blockStyle.alignSelf}`,
  );

  await page.waitForSelector(".session-composer-row");
  const composerStyle = await page.evaluate(() => {
    const row = document.querySelector(".session-composer-row");
    const style = getComputedStyle(row);
    return { borderLeftWidth: style.borderLeftWidth, boxShadow: style.boxShadow };
  });
  assert(
    composerStyle.borderLeftWidth === "3px",
    `composer strip must carry a 3px left accent bar, got ${composerStyle.borderLeftWidth}`,
  );

  const hints = await page.locator(".session-footer-hints").innerText();
  for (const key of ["enter", "shift+enter", "esc"]) {
    assert(hints.includes(key), `footer hints must include ${key}, got ${hints}`);
  }
  const composerRadius = await page.locator(".session-composer-row").evaluate((el) =>
    Number.parseFloat(getComputedStyle(el).borderRadius),
  );
  assert(composerRadius <= 2, `session composer radius must be <=2px, got ${composerRadius}`);
  assert((await page.locator('[data-testid="session-run-details"]').count()) === 0,
    "Run details must not render");
  assert((await page.locator(".ticket-cost-strip").count()) === 0,
    "ticket cost strip must not render");
  await page.screenshot({ path: path.join(OUT_DIR, "03-session-surface.png") });

  logStep("state 4: shift+enter hint mirrors composer behavior (working vs idle)");
  // The backend derives `working` from transcript mtime (< 30s) when no
  // supervisor or tmux pane resolves — touch vs backdate drives both states.
  await fs.utimes(transcript, new Date(), new Date());
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForSelector(".session-footer-hints");
  const workingHint = await page.locator(".session-footer-hints").innerText();
  assert(
    /shift\+enter\s+queue/.test(workingHint),
    `working session must advertise queue, got ${workingHint}`,
  );

  const stale = new Date(Date.now() - 120_000);
  await fs.utimes(transcript, stale, stale);
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForSelector(".session-footer-hints");
  const idleHint = await page.locator(".session-footer-hints").innerText();
  assert(
    /shift\+enter\s+newline/.test(idleHint),
    `idle session must advertise newline, got ${idleHint}`,
  );

  logStep("state 5: hint strip stays inside footer bounds at 320px");
  await page.setViewportSize({ width: 320, height: 900 });
  await page.waitForSelector(".session-footer-hints");
  const layout = await page.evaluate(() => {
    const footer = document.querySelector(".session-footer");
    const rect = footer.getBoundingClientRect();
    return {
      scrollWidth: footer.scrollWidth,
      clientWidth: footer.clientWidth,
      footer: { left: rect.left, right: rect.right },
      hints: Array.from(document.querySelectorAll(".session-hint")).map((el) => {
        const r = el.getBoundingClientRect();
        return { left: r.left, right: r.right };
      }),
    };
  });
  assert(
    layout.scrollWidth <= layout.clientWidth,
    `footer must not overflow horizontally at 320px: scrollWidth ${layout.scrollWidth} vs clientWidth ${layout.clientWidth}`,
  );
  assert(layout.hints.length === 3, `all three hints must stay visible, got ${layout.hints.length}`);
  for (const hint of layout.hints) {
    assert(
      hint.left >= layout.footer.left - 0.5 && hint.right <= layout.footer.right + 0.5,
      `hint [${hint.left}, ${hint.right}] must sit inside footer [${layout.footer.left}, ${layout.footer.right}]`,
    );
  }
  await page.screenshot({ path: path.join(OUT_DIR, "04-narrow-footer.png") });

  logStep("PASS");
} finally {
  await browser?.close();
  await backend?.stop();
}
