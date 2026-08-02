// WIKI-234: opencode-inspired restyle. Asserts the terminal-native language
// holds at the structural level: opencode is the default theme, session rows
// carry gutter numbers, history splits into time groups, user turns render as
// left-accent-bar blocks, and the composer strip exposes the model line plus
// keybinding hints.
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
const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || path.join(HERE, "evidence", "wiki-234");
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
    "WIKI-302": { history: [], current: { ...workerBase, ticket: "WIKI-302", state: "idle" } },
    "FREE-1": { history: [], current: { ...workerBase, ticket: "FREE-1", orch: null } },
  };
  await fs.writeFile(fixtures.registryPath, JSON.stringify(registry, null, 2));
  writeQueue(fixtures.queuePath, "WIKI-301", []);
  for (const ticket of ["WIKI-301", "WIKI-302", "FREE-1"]) {
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

  logStep("state 2: numbered sidebar with time-grouped history");
  await page.click('[data-testid="workspace-ribbon"] [aria-label="Agent list"]');
  await page.waitForSelector('[data-testid="nav-agents-group-active"]');
  await page.waitForSelector('[data-testid="nav-agents-group-history"]');
  const groups = await page.evaluate(() =>
    Array.from(document.querySelectorAll(".nav-agents-group-title")).map((el) => ({
      label: (el.firstElementChild?.textContent ?? "").trim(),
      count: (el.querySelector(".nav-agents-group-count")?.textContent ?? "").trim(),
    })),
  );
  const labels = groups.map((group) => group.label);
  assert(
    JSON.stringify(labels) === JSON.stringify(["Active", "Today", "Yesterday", "Earlier"]),
    `expected Active/Today/Yesterday/Earlier groups, got ${labels.join("/")}`,
  );
  const counts = Object.fromEntries(groups.map((group) => [group.label, group.count]));
  assert(counts.Active === "4", `Active count must include orch + workers (4), got ${counts.Active}`);
  assert(counts.Today === "1" && counts.Yesterday === "1" && counts.Earlier === "1",
    `each history group must count 1, got ${JSON.stringify(counts)}`);
  const nums = await page.evaluate(() =>
    Array.from(document.querySelectorAll(".nav-agent .nav-agent-num")).map((el) =>
      (el.textContent ?? "").trim(),
    ),
  );
  assert(
    JSON.stringify(nums) === JSON.stringify(["1", "2", "3", "4", "5", "6"]),
    `gutter numbers must run 1..6 across workers then history, got ${nums.join(",")}`,
  );
  const orchHasNum = await page.evaluate(
    () => document.querySelector(".nav-agent.is-orch .nav-agent-num") !== null,
  );
  assert(!orchHasNum, "orchestrator rows must not carry gutter numbers");
  await page.screenshot({ path: path.join(OUT_DIR, "01-sidebar-groups.png") });

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
  await page.screenshot({ path: path.join(OUT_DIR, "02-session-surface.png") });

  logStep("PASS");
} finally {
  await browser?.close();
  await backend?.stop();
}
