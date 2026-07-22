import { mkdirSync, readFileSync, rmSync } from "node:fs";
import path from "node:path";
import { chromium } from "playwright";

import { makeFixtureRoot, startBackend } from "../scripts/wiki32-harness.mjs";

const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || "/tmp/wiki-144-badges-evidence";
mkdirSync(OUT_DIR, { recursive: true });
const fixtures = makeFixtureRoot("wiki-144-badges-");

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

// After WIKI-144, the ad-hoc `.agent-chip` class is retired: every kind/role/model
// tag on session headers, dashboard rows, and artifact rows renders via
// <StatusBadge state="neutral|faint"> instead. Source-level guard catches
// regressions where a caller reintroduces the old class.
const FLAGGED_FILES = [
  "src/agent-session-surface.tsx",
  "src/agents.tsx",
  "src/session.tsx",
  "src/artifact-panel.tsx",
  "src/artifact-block.tsx",
  "src/dashboard.tsx",
];

const BANNED_PATTERNS = [/className=[^}]*\bagent-chip\b/];

const TAG_STATES = ["neutral", "faint"];

let browser;
let backend;

try {
  const repoRoot = path.resolve(new URL(".", import.meta.url).pathname, "..");
  for (const rel of FLAGGED_FILES) {
    const abs = path.join(repoRoot, rel);
    const source = readFileSync(abs, "utf8");
    for (const pattern of BANNED_PATTERNS) {
      assert(!pattern.test(source), `${rel} still contains ad-hoc badge class matching ${pattern}`);
    }
  }

  backend = await startBackend(fixtures);
  browser = await chromium.launch({ headless: true });

  for (const theme of ["gruvbox-dark", "mono-light"]) {
    const page = await browser.newPage({ viewport: { width: 1200, height: 700 } });
    await page.addInitScript((themeName) => {
      localStorage.setItem("wiki-theme", themeName);
    }, theme);
    await page.goto(`${backend.baseUrl}/`, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => {
      const probe = document.createElement("span");
      probe.className = "status-badge is-working";
      document.body.append(probe);
      const computed = window.getComputedStyle(probe).borderTopLeftRadius;
      probe.remove();
      return computed === "999px";
    });

    const rendered = await page.evaluate(({ tagStates }) => {
      const host = document.createElement("div");
      host.id = "wiki-144-badge-host";
      host.style.padding = "24px";
      host.style.display = "flex";
      host.style.flexDirection = "column";
      host.style.gap = "12px";

      const row = document.createElement("div");
      row.style.display = "flex";
      row.style.gap = "8px";
      row.style.alignItems = "center";

      const kinds = { kind: "claude", role: "implement", model: "opus-4-7" };
      const tones = { kind: "neutral", role: "neutral", model: "faint" };
      const measured = {};
      host.append(row);
      document.body.append(host);
      for (const [label, text] of Object.entries(kinds)) {
        const badge = document.createElement("span");
        badge.className = `status-badge is-${tones[label]} is-compact`;
        badge.setAttribute("data-state", tones[label]);
        badge.textContent = text;
        row.append(badge);
        const style = getComputedStyle(badge);
        measured[label] = {
          color: style.color,
          background: style.backgroundColor,
          borderRadius: style.borderTopLeftRadius,
          fontSize: style.fontSize,
        };
      }
      const result = { measured };
      // Sanity-check that both ambient tones exist and share the same box shape:
      result.uniqueColors = new Set(
        tagStates.map((state) => {
          const el = document.createElement("span");
          el.className = `status-badge is-${state}`;
          el.textContent = state;
          document.body.append(el);
          const color = getComputedStyle(el).color;
          el.remove();
          return color;
        }),
      ).size;
      host.remove();
      return result;
    }, { tagStates: TAG_STATES });

    assert(
      rendered.uniqueColors >= 2,
      `neutral vs faint tones should render as distinct colors, only saw ${rendered.uniqueColors}`,
    );
    for (const [label, style] of Object.entries(rendered.measured)) {
      assert(
        style.background === "rgba(0, 0, 0, 0)" || style.background === "transparent",
        `${label} badge should have no background chrome, got ${style.background}`,
      );
      assert(
        style.borderRadius === "999px",
        `${label} badge should keep pill radius, got ${style.borderRadius}`,
      );
    }

    await page.screenshot({ path: path.join(OUT_DIR, `wiki-144-badges-${theme}.png`) });
    await page.close();
  }
} finally {
  if (browser) await browser.close();
  if (backend) await backend.stop();
  rmSync(fixtures.root, { recursive: true, force: true });
}
