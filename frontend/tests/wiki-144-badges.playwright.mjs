import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { chromium } from "playwright";

import {
  makeFixtureRoot,
  openSessionPage,
  startBackend,
  writeQueue,
} from "../scripts/wiki32-harness.mjs";

const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || "/tmp/wiki-144-badges-evidence";
mkdirSync(OUT_DIR, { recursive: true });
const fixtures = makeFixtureRoot("wiki-144-badges-");

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

// WIKI-144 retires the ad-hoc `.agent-chip` class across the surfaces we touch.
// Guard the source so a regression can't quietly re-introduce it.
const FLAGGED_FILES = [
  "src/agent-session-surface.tsx",
  "src/agents.tsx",
  "src/session.tsx",
  "src/artifact-panel.tsx",
  "src/artifact-block.tsx",
  "src/artifact-detail/file-list.tsx",
  "src/dashboard.tsx",
];

const BANNED_PATTERNS = [/className=[^}]*\bagent-chip\b/];

const DASHBOARD_PAYLOAD = {
  tickets: [
    {
      ticket: "WIKI-144",
      description: "unify status badges",
      pr: null,
      repo: null,
      enriched: true,
      status: "merge-ready",
      detail: "WIKI-144 fixture",
      date: "2026-07-22T12:00:00Z",
      live: true,
      role: "implement",
      kind: "cc",
    },
    {
      ticket: "WIKI-143",
      description: "frontend refresh",
      pr: "https://github.com/hwang2409/wiki/pull/115",
      repo: "hwang2409/wiki",
      enriched: true,
      status: "merged",
      detail: "merged",
      date: "2026-07-21T12:00:00Z",
      live: false,
      role: "implement",
      kind: "cc",
    },
    {
      ticket: "WIKI-100",
      description: "fixture blocked entry",
      pr: null,
      repo: null,
      enriched: false,
      status: "blocked",
      detail: "example blocker",
      date: "2026-07-20T12:00:00Z",
      live: true,
      role: "review",
      kind: "cdx",
    },
  ],
  repo_allowlist: [],
};

// Fixture claude transcript: one assistant text turn to prime the session header,
// followed by a `render_artifact` file-list call so the artifact row surface has
// three real StatusBadge instances (added/modified/removed).
const TRANSCRIPT_LINES = [
  {
    type: "assistant",
    timestamp: "2026-07-22T12:00:00Z",
    message: {
      role: "assistant",
      content: [{ type: "text", text: "WIKI-144 fixture session." }],
    },
  },
  {
    type: "assistant",
    timestamp: "2026-07-22T12:00:01Z",
    message: {
      role: "assistant",
      content: [
        {
          type: "tool_use",
          id: "toolu_wiki144_files",
          name: "mcp__wiki-artifacts__render_artifact",
          input: {
            kind: "file-list",
            title: "WIKI-144 changed files",
            payload: {
              files: [
                { path: "src/added.tsx", status: "added" },
                { path: "src/modified.tsx", status: "modified" },
                { path: "src/removed.tsx", status: "removed" },
              ],
            },
          },
        },
      ],
    },
  },
  {
    type: "user",
    timestamp: "2026-07-22T12:00:02Z",
    message: {
      role: "user",
      content: [
        {
          type: "tool_result",
          tool_use_id: "toolu_wiki144_files",
          content: JSON.stringify({
            artifact_id: "22222222-2222-2222-2222-222222222222",
            ok: true,
          }),
        },
      ],
    },
  },
];

function relLuminance([r, g, b]) {
  const linear = (v) => {
    const s = v / 255;
    return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
  };
  return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b);
}

function contrastRatio(fg, bg) {
  const l1 = relLuminance(fg);
  const l2 = relLuminance(bg);
  const lighter = Math.max(l1, l2);
  const darker = Math.min(l1, l2);
  return (lighter + 0.05) / (darker + 0.05);
}

function parseRgb(color) {
  const match = color.match(/rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/i);
  if (!match) throw new Error(`cannot parse color ${color}`);
  return [Number(match[1]), Number(match[2]), Number(match[3])];
}

const transcriptPath = path.join(fixtures.root, "wiki-144-fixture.jsonl");
writeFileSync(
  transcriptPath,
  TRANSCRIPT_LINES.map((row) => JSON.stringify(row)).join("\n") + "\n",
);
// Custom registry: include kind/role/model so the session-header renders every
// StatusBadge variant we want to assert against (kind + role neutral, model faint).
writeFileSync(
  fixtures.registryPath,
  JSON.stringify(
    {
      _orchestrators: {
        "WIKI-32": {
          window: "@9999",
          spawned_at: "2026-07-09T00:00:00Z",
          transcript: transcriptPath,
          kind: "cc",
          role: "implement",
          model: "opus-4-7",
        },
      },
    },
    null,
    2,
  ),
);
writeQueue(fixtures.queuePath, "WIKI-32", []);

const repoRoot = path.resolve(new URL(".", import.meta.url).pathname, "..");
for (const rel of FLAGGED_FILES) {
  const abs = path.join(repoRoot, rel);
  const source = readFileSync(abs, "utf8");
  for (const pattern of BANNED_PATTERNS) {
    assert(
      !pattern.test(source),
      `${rel} still contains ad-hoc badge class matching ${pattern}`,
    );
  }
}

let browser;
let backend;

try {
  backend = await startBackend(fixtures);
  browser = await chromium.launch({ headless: true });

  for (const theme of ["gruvbox-dark", "mono-light"]) {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1200 } });
    await context.addInitScript((themeName) => {
      localStorage.clear();
      localStorage.setItem("wiki-theme", themeName);
      localStorage.setItem("wiki-sidebar-visible", "false");
      // Force empty dashboard filters so the fixture rows never get hidden by
      // a persisted filter set (defensive: some upstream state leaks between
      // pages under the shared browser).
      localStorage.setItem(
        "wiki-dashboard-filters",
        JSON.stringify({ projects: [], states: [], dateFrom: null, dateTo: null }),
      );
    }, theme);
    const page = await context.newPage();
    await context.route("**/api/dashboard/tickets", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(DASHBOARD_PAYLOAD),
      });
    });

    // Dashboard: seed a solo utility://dashboard pane so DashboardView mounts
    // on first paint, then let the intercepted /api/dashboard/tickets response
    // populate the rows.
    await context.addInitScript(({ layout }) => {
      localStorage.setItem("wiki-window-layout-v2", JSON.stringify(layout));
    }, {
      layout: {
        version: 2,
        activeWindowId: "window-0",
        windows: [
          {
            id: "window-0",
            focusedPaneId: "pane-1",
            layout: { kind: "pane", id: "pane-1", path: "utility://dashboard" },
          },
        ],
      },
    });
    await page.goto(`${backend.baseUrl}/#/dashboard`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".dashboard-table-scroll .status-badge");
    const dashCount = await page.locator(".dashboard-table-scroll .status-badge").count();
    assert(
      dashCount >= DASHBOARD_PAYLOAD.tickets.length,
      `expected >=${DASHBOARD_PAYLOAD.tickets.length} dashboard status badges, saw ${dashCount}`,
    );
    await page.screenshot({
      path: path.join(OUT_DIR, `wiki-144-dashboard-${theme}.png`),
      fullPage: false,
    });

    // Session header + artifact rows: navigate to the fixture WIKI-32 pane.
    await openSessionPage(page, backend.baseUrl, {
      version: 2,
      activeWindowId: "window-0",
      windows: [
        {
          id: "window-0",
          focusedPaneId: "pane-1",
          layout: { kind: "pane", id: "pane-1", path: "agent://WIKI-32" },
        },
      ],
    });
    await page.waitForSelector(".session-header .status-badge");
    const headerCount = await page.locator(".session-header .status-badge").count();
    assert(
      headerCount >= 1,
      `expected >=1 session-header status badges (kind/role/model), saw ${headerCount}`,
    );

    await page.waitForSelector(".artifact-file-list .status-badge", { timeout: 15000 });
    const fileBadgeCount = await page
      .locator(".artifact-file-list .status-badge")
      .count();
    assert(
      fileBadgeCount >= 3,
      `expected >=3 artifact file-list status badges (added/modified/removed), saw ${fileBadgeCount}`,
    );

    // M1 contrast assertion: compact `is-faint` badges must clear WCAG-AA 4.5:1
    // in the two default themes the reviewer measured.
    const contrast = await page.evaluate(() => {
      const badge = document.querySelector(
        ".session-header .status-badge.is-faint.is-compact",
      );
      if (!badge) return null;
      let ancestorBg = "rgba(0, 0, 0, 0)";
      let cursor = badge.parentElement;
      while (cursor) {
        const bg = getComputedStyle(cursor).backgroundColor;
        if (bg && bg !== "rgba(0, 0, 0, 0)" && bg !== "transparent") {
          ancestorBg = bg;
          break;
        }
        cursor = cursor.parentElement;
      }
      const badgeStyle = getComputedStyle(badge);
      return {
        color: badgeStyle.color,
        background: ancestorBg,
        fontSize: badgeStyle.fontSize,
      };
    });
    assert(contrast, "compact faint badge not present in session header after render");
    const ratio = contrastRatio(
      parseRgb(contrast.color),
      parseRgb(contrast.background),
    );
    assert(
      ratio >= 4.5,
      `compact faint badge contrast ${ratio.toFixed(2)}:1 fails WCAG AA in ${theme} ` +
        `(fg=${contrast.color} bg=${contrast.background} fs=${contrast.fontSize})`,
    );

    await page.screenshot({
      path: path.join(OUT_DIR, `wiki-144-session-${theme}.png`),
      fullPage: false,
    });
    await page.close();
    await context.close();
  }
} finally {
  if (browser) await browser.close();
  if (backend) await backend.stop();
  rmSync(fixtures.root, { recursive: true, force: true });
}
