import { mkdirSync, readFileSync, rmSync } from "node:fs";
import path from "node:path";
import { chromium } from "playwright";

import { makeFixtureRoot, startBackend } from "../scripts/wiki32-harness.mjs";

const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || "/tmp/wiki-143-badges-evidence";
mkdirSync(OUT_DIR, { recursive: true });
const fixtures = makeFixtureRoot("wiki-143-badges-");

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

const STATES = [
  "working",
  "merge-ready",
  "blocked",
  "abandoned",
  "idle",
  "waiting-approval",
  "dead",
  "interrupted",
  "ok",
  "warning",
  "error",
];

const FLAGGED_FILES = [
  "src/session.tsx",
  "src/App.tsx",
  "src/artifact-block.tsx",
  "src/switcher.tsx",
  "src/pane.tsx",
  "src/terminal-pane.tsx",
  "src/agent-session-surface.tsx",
  "src/agents.tsx",
  "src/github-preview.tsx",
  "src/agent-pr-review.tsx",
  "src/activity.tsx",
];

// After the sweep, no source file should still render `.agent-state` or
// `.pr-review-pill` inline as those got unified behind StatusBadge/BranchPill.
const BANNED_PATTERNS = [/className=[^}]*agent-state/, /className=[^}]*pr-review-pill/];

let browser;
let backend;

try {
  const repoRoot = path.resolve(new URL(".", import.meta.url).pathname, "..");
  for (const rel of FLAGGED_FILES) {
    const abs = path.join(repoRoot, rel);
    const source = readFileSync(abs, "utf8");
    for (const pattern of BANNED_PATTERNS) {
      assert(!pattern.test(source), `${rel} still contains ad-hoc badge/pill class matching ${pattern}`);
    }
  }

  backend = await startBackend(fixtures);
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  await page.addInitScript(() => {
    localStorage.setItem("wiki-theme", "gruvbox-dark");
  });
  await page.goto(`${backend.baseUrl}/`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector(":root");

  const rendered = await page.evaluate(
    ({ states }) => {
      const host = document.createElement("div");
      host.id = "wiki-143-badge-host";
      host.style.padding = "20px";
      host.style.display = "flex";
      host.style.gap = "8px";
      document.body.append(host);
      const colors = {};
      const shapes = {};
      for (const state of states) {
        const badge = document.createElement("span");
        badge.className = `status-badge is-${state}`;
        badge.setAttribute("data-state", state);
        badge.textContent = state;
        host.append(badge);
        const style = getComputedStyle(badge);
        colors[state] = style.color;
        shapes[state] = {
          borderRadius: style.borderRadius,
          fontSize: style.fontSize,
          fontWeight: style.fontWeight,
        };
      }
      const branch = document.createElement("span");
      branch.className = "branch-pill";
      const icon = document.createElement("span");
      icon.className = "branch-pill-icon";
      const label = document.createElement("span");
      label.className = "branch-pill-label";
      label.textContent = "wiki-143-frontend-refresh";
      branch.append(icon, label);
      host.append(branch);
      const branchStyle = getComputedStyle(branch);
      const labelStyle = getComputedStyle(label);
      const result = {
        colors,
        shapes,
        branch: {
          fontFamily: branchStyle.fontFamily,
          labelFontFamily: labelStyle.fontFamily,
          borderRadius: branchStyle.borderRadius,
          hasIcon: Boolean(branch.querySelector(".branch-pill-icon")),
        },
      };
      host.remove();
      return result;
    },
    { states: STATES },
  );

  const uniqueColors = new Set(
    STATES.map((state) => rendered.colors[state]).filter((value) => value && value !== ""),
  );
  assert(
    uniqueColors.size >= 6,
    `status-badge states should map to distinct accent colors; only saw ${uniqueColors.size} unique colors across ${STATES.length}`,
  );

  for (const state of STATES) {
    assert(
      rendered.shapes[state].borderRadius === "999px",
      `state ${state} expected pill (999px) radius, got ${rendered.shapes[state].borderRadius}`,
    );
    assert(
      rendered.shapes[state].fontSize === "10px",
      `state ${state} expected 10px (chrome tier), got ${rendered.shapes[state].fontSize}`,
    );
    assert(
      rendered.shapes[state].fontWeight === "500",
      `state ${state} expected 500 (fw-medium), got ${rendered.shapes[state].fontWeight}`,
    );
  }

  assert(rendered.branch.hasIcon, "branch-pill did not render icon slot");
  assert(
    rendered.branch.borderRadius === "6px",
    `branch-pill expected 6px (radius-sm), got ${rendered.branch.borderRadius}`,
  );
  assert(
    /mono/i.test(rendered.branch.labelFontFamily) || /Consolas|JetBrains|SFMono|Menlo/i.test(rendered.branch.labelFontFamily),
    `branch-pill label expected monospace font, got ${rendered.branch.labelFontFamily}`,
  );

  await page.screenshot({ path: path.join(OUT_DIR, "wiki-143-badges.png") });
} finally {
  if (browser) await browser.close();
  if (backend) await backend.stop();
  rmSync(fixtures.root, { recursive: true, force: true });
}
