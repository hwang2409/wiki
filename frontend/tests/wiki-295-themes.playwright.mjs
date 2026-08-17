// WIKI-295 visual verification: load the app across 4 themes at 1440x900,
// screenshot each, and assert there is no horizontal scroll on the shell.
// The token change is CSS-only (mono canvas/ink derivation + new shell
// tokens); this test guards against a layout regression from a bad
// color-mix or a typo that pulls a length token.
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

const TICKET = "WIKI-295";
const OUT_DIR =
  process.env.WIKI_PLAYWRIGHT_OUT_DIR || ".playwright-mcp/wiki-295";
mkdirSync(OUT_DIR, { recursive: true });

const THEMES = ["mono-light", "mono-dark", "opencode", "tokyo-night"];

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

let browser;
let backend;
const fixtures = makeFixtureRoot("wiki-295-themes-");

try {
  const transcript = path.join(fixtures.root, "wiki-295.jsonl");
  await fs.writeFile(
    transcript,
    [
      { type: "mode", mode: "normal", sessionId: "wiki-295" },
      codexAssistant("Theme verification chat body.", "2026-08-17T12:00:00Z"),
    ]
      .map((row) => JSON.stringify(row))
      .join("\n") + "\n",
  );
  writeRegistry(fixtures.registryPath, [[TICKET, transcript]]);
  writeQueue(fixtures.queuePath, TICKET, []);

  backend = await startBackend(fixtures);
  browser = await chromium.launch({ headless: true });

  for (const theme of THEMES) {
    const page = await browser.newPage({
      viewport: { width: 1440, height: 900 },
    });
    await page.addInitScript((themeId) => {
      localStorage.setItem("wiki-theme", themeId);
      localStorage.setItem("wiki-sidebar-visible", "true");
      localStorage.setItem(
        "wiki-window-layout-v2",
        JSON.stringify({
          version: 2,
          activeWindowId: "window-0",
          windows: [
            {
              id: "window-0",
              focusedPaneId: "pane-1",
              layout: {
                kind: "pane",
                id: "pane-1",
                path: "agent://WIKI-295",
              },
            },
          ],
        }),
      );
    }, theme);
    await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, {
      waitUntil: "domcontentloaded",
    });
    await page.locator(".agent-session-surface").waitFor({ state: "visible" });
    const measurements = await page.evaluate(() => {
      const style = getComputedStyle(document.documentElement);
      return {
        scrollWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
        theme: document.documentElement.dataset.theme,
        borderSeam: style.getPropertyValue("--border-seam").trim(),
        surfaceRecessed: style.getPropertyValue("--surface-recessed").trim(),
        surfaceSelected: style.getPropertyValue("--surface-selected").trim(),
        stateHover: style.getPropertyValue("--state-hover").trim(),
        sidebar: style.getPropertyValue("--sidebar").trim(),
        shadowLift: style.getPropertyValue("--shadow-lift").trim(),
        timelineAccent: style.getPropertyValue("--timeline-accent").trim(),
        prMerged: style.getPropertyValue("--pr-merged").trim(),
        pillSurface: style.getPropertyValue("--pill-surface").trim(),
        readbackFg: style.getPropertyValue("--readback-foreground").trim(),
      };
    });
    assert(
      measurements.theme === theme,
      `theme ${theme}: dataset expected ${theme}, got ${measurements.theme}`,
    );
    assert(
      measurements.scrollWidth <= measurements.clientWidth,
      `theme ${theme}: horizontal scroll — scrollWidth=${measurements.scrollWidth} > clientWidth=${measurements.clientWidth}`,
    );
    for (const [key, value] of Object.entries(measurements)) {
      if (
        key === "scrollWidth" ||
        key === "clientWidth" ||
        key === "theme"
      )
        continue;
      assert(
        value.length > 0,
        `theme ${theme}: token --${key} is empty`,
      );
    }
    await page.screenshot({
      path: path.join(OUT_DIR, `${theme}.png`),
      fullPage: false,
    });
    await page.close();
  }
} finally {
  if (browser) await browser.close();
  if (backend) await backend.stop();
  rmSync(fixtures.root, { recursive: true, force: true });
}
