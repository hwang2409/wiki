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

const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR || "/tmp/wiki-301-status-chips";
mkdirSync(OUT_DIR, { recursive: true });

const THEMES = ["mono-light", "mono-dark", "opencode", "tokyo-night"];
const TICKETS = ["WIKI-301", "WIKI-297", "WIKI-298", "WIKI-296"];

const fixtures = makeFixtureRoot("wiki-301-status-chips-");
let browser;
let backend;

try {
  const registryRows = [];
  for (const ticket of TICKETS) {
    const transcript = path.join(fixtures.root, `${ticket}.jsonl`);
    await fs.writeFile(
      transcript,
      [
        { type: "mode", mode: "normal", sessionId: ticket },
        codexAssistant(`Body for ${ticket}.`, "2026-08-17T12:00:00Z"),
      ]
        .map((row) => JSON.stringify(row))
        .join("\n") + "\n",
    );
    registryRows.push([ticket, transcript]);
    writeQueue(fixtures.queuePath, ticket, []);
  }
  writeRegistry(fixtures.registryPath, registryRows);

  backend = await startBackend(fixtures);
  browser = await chromium.launch({ headless: true });

  for (const theme of THEMES) {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    await page.addInitScript((themePinned) => {
      localStorage.setItem("wiki-theme", themePinned);
      localStorage.setItem("wiki-sidebar-visible", "true");
      const windows = ["WIKI-301", "WIKI-297", "WIKI-298", "WIKI-296"].map(
        (ticket, index) => ({
          id: `window-${index}`,
          focusedPaneId: `pane-${index}`,
          layout: { kind: "pane", id: `pane-${index}`, path: `agent://${ticket}` },
        }),
      );
      localStorage.setItem(
        "wiki-window-layout-v2",
        JSON.stringify({
          version: 2,
          activeWindowId: "window-0",
          windows,
        }),
      );
    }, theme);
    await page.goto(`${backend.baseUrl}/#/agent/WIKI-301`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".status-bar");
    await page.waitForSelector(".tmux-status-item.is-active");

    const chips = await page.$$eval(".tmux-status-item", (items) =>
      items.map((item) => {
        const style = getComputedStyle(item);
        return {
          text: item.textContent?.trim() ?? "",
          isActive: item.classList.contains("is-active"),
          height: style.height,
          borderRadius: style.borderRadius,
          fontFamily: style.fontFamily,
          fontWeight: style.fontWeight,
          fontSize: style.fontSize,
          backgroundColor: style.backgroundColor,
          color: style.color,
        };
      }),
    );

    if (chips.length !== TICKETS.length) {
      throw new Error(`${theme}: expected ${TICKETS.length} chips, got ${chips.length}`);
    }

    const expectedRadius = await page.evaluate(() => {
      return getComputedStyle(document.documentElement).getPropertyValue("--radius-full").trim();
    });
    for (const chip of chips) {
      if (chip.height !== "28px") throw new Error(`${theme}: chip height ${chip.height} !== 28px`);
      // radius resolves through --radius-full; per-theme overrides are expected
      // (opencode collapses radii to 2px). Just confirm the binding took.
      if (!chip.borderRadius || chip.borderRadius === "8px") {
        throw new Error(
          `${theme}: chip radius ${chip.borderRadius} looks unbound (expected --radius-full=${expectedRadius})`,
        );
      }
      if (chip.fontWeight !== "500") {
        throw new Error(`${theme}: chip font-weight ${chip.fontWeight} !== 500`);
      }
      if (chip.fontSize !== "10px") {
        throw new Error(`${theme}: chip font-size ${chip.fontSize} unexpected (want 10)`);
      }
    }

    const activePip = await page.evaluate(() => {
      const active = document.querySelector(".tmux-status-item.is-active");
      if (!active) return null;
      const style = getComputedStyle(active, "::before");
      return {
        width: style.width,
        height: style.height,
        borderRadius: style.borderRadius,
        backgroundColor: style.backgroundColor,
      };
    });
    if (!activePip) throw new Error(`${theme}: no active pip found`);
    if (activePip.width !== "5px" || activePip.height !== "5px") {
      throw new Error(`${theme}: pip size ${activePip.width}x${activePip.height} !== 5x5`);
    }

    const statusBar = await page.locator(".status-bar").boundingBox();
    if (!statusBar) throw new Error(`${theme}: status bar not measurable`);

    await page.screenshot({
      path: path.join(OUT_DIR, `${theme}-full.png`),
      fullPage: false,
    });
    await page.screenshot({
      path: path.join(OUT_DIR, `${theme}-status.png`),
      clip: {
        x: 0,
        y: Math.max(0, statusBar.y - 8),
        width: 1440,
        height: statusBar.height + 16,
      },
    });

    // hover second chip to verify --state-hover treatment
    await page.locator(".tmux-status-item").nth(1).hover();
    await page.waitForTimeout(200);
    await page.screenshot({
      path: path.join(OUT_DIR, `${theme}-status-hover.png`),
      clip: {
        x: 0,
        y: Math.max(0, statusBar.y - 8),
        width: 1440,
        height: statusBar.height + 16,
      },
    });

    console.log(
      `[${theme}] ${chips.length} chips, active pip ${activePip.width}, bg ${chips[0].backgroundColor}`,
    );
    await page.close();
  }
  console.log(`OK — screenshots in ${OUT_DIR}`);
} finally {
  if (browser) await browser.close();
  if (backend) await backend.stop();
  rmSync(fixtures.root, { recursive: true, force: true });
}
