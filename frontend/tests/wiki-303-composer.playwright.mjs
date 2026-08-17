// WIKI-303: verifies the bb PromptBox parity restyle — the composer input
// row + text-2xs metadata row now live inside a shared `.session-composer-card`
// with a hairline border, `--shadow-lift` and a focus-within ring. Runs the
// four canonical themes at 1440x900, screenshots each, and asserts the new
// structural classes + focus lift + skill-menu pill styling survive.
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
  writeRegistry,
} from "../scripts/wiki32-harness.mjs";

const TICKET = "WIKI-303";
const HERE = path.dirname(new URL(import.meta.url).pathname);
const OUT_DIR = process.env.WIKI_PLAYWRIGHT_OUT_DIR
  || path.join(HERE, "..", ".playwright-mcp", "wiki-303");
mkdirSync(OUT_DIR, { recursive: true });

const THEMES = ["mono-light", "mono-dark", "opencode", "tokyo-night"];

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function logStep(message) {
  console.error(`[wiki-303-playwright] ${message}`);
}

async function main() {
  const fixtures = makeFixtureRoot("wiki-303-composer-");
  const transcript = path.join(fixtures.root, "composer.jsonl");
  await fs.writeFile(
    transcript,
    [
      codexUser("show me the wiki composer", "2026-08-17T15:00:00Z"),
      codexAssistant("bb PromptBox parity landed in WIKI-303.", "2026-08-17T15:00:01Z"),
    ].map((row) => JSON.stringify(row)).join("\n") + "\n",
  );
  writeRegistry(fixtures.registryPath, [[TICKET, transcript]]);
  writeQueue(fixtures.queuePath, TICKET, []);

  const backend = await startBackend(fixtures);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

  try {
    await page.route(`**/api/agents/${TICKET}/session?**`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          version: 2,
          format: "codex",
          path: transcript,
          tokens: null,
          model: "gpt-5.6-sol",
          kind: "cdx",
          provider: "codex",
          tasks: [],
          pr: null,
          session_meta: {},
          dispositions: { rendered: 2, summarized: 0, ignored: 0, unknown: 0 },
          base: 0,
          cursor: 2,
          tail_from: 0,
          events: [
            {
              id: 0,
              kind: "user",
              ts: "2026-08-17T15:00:00Z",
              text: "show me the wiki composer",
              disposition: "rendered",
            },
            {
              id: 1,
              kind: "assistant",
              ts: "2026-08-17T15:00:01Z",
              text: "bb PromptBox parity landed in WIKI-303.",
              disposition: "rendered",
            },
          ],
          patches: [],
          subagents: [],
          queue: [],
          working: false,
        }),
      });
    });

    await page.route("**/api/skills", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          skills: [
            { name: "frontend-design", description: "Design focused interfaces" },
            { name: "handoff", description: "Compact the conversation into a handoff message" },
          ],
        }),
      });
    });

    await page.route("**/api/agents/*/message", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ status: "sent" }),
      });
    });

    await page.addInitScript(({ ticket }) => {
      if (localStorage.getItem("wiki-sidebar-visible") === null) {
        localStorage.setItem("wiki-sidebar-visible", "true");
      }
      if (localStorage.getItem("wiki-window-layout-v2") === null) {
        localStorage.setItem(
          "wiki-window-layout-v2",
          JSON.stringify({
            version: 2,
            activeWindowId: "window-0",
            windows: [
              {
                id: "window-0",
                focusedPaneId: "pane-1",
                layout: { kind: "pane", id: "pane-1", path: `agent://${ticket}` },
              },
            ],
          }),
        );
      }
    }, { ticket: TICKET });

    for (const theme of THEMES) {
      logStep(`theme=${theme}`);
      await page.goto(`${backend.baseUrl}/#/agent/${TICKET}`, { waitUntil: "domcontentloaded" });
      await page.evaluate((themeName) => {
        localStorage.setItem("wiki-theme", themeName);
      }, theme);
      await page.reload({ waitUntil: "domcontentloaded" });
      const appliedTheme = await page.locator("html").getAttribute("data-theme");
      assert(appliedTheme === theme, `[${theme}] expected applied theme, got ${appliedTheme}`);
      await page.locator(".session-composer-card").waitFor({ state: "visible" });
      await page.locator(".session-composer-meta").waitFor({ state: "visible" });
      await page.locator(".session-composer textarea").waitFor({ state: "visible" });
      const composer = page.locator(".session-composer textarea");
      await page.evaluate(() => {
        const active = document.activeElement;
        if (active instanceof HTMLElement) active.blur();
      });
      await page.waitForFunction(
        () => !document.querySelector(".session-composer-card")?.matches(":focus-within"),
      );
      await page.waitForTimeout(200);

      // Structural assertions — the card wraps the input row + meta, and the
      // WIKI-293 focus-lift is now the card's job, not the row's.
      const composerMetrics = await page.evaluate(() => {
        const card = document.querySelector(".session-composer-card");
        const row = card?.querySelector(".session-composer-row");
        const meta = card?.querySelector(".session-composer-meta");
        const footer = card?.querySelector(".session-footer");
        if (!card || !row || !meta || !footer) return null;
        const style = getComputedStyle(card);
        const rowStyle = getComputedStyle(row);
        return {
          cardBorder: style.borderTopStyle,
          cardBorderWidth: style.borderTopWidth,
          cardShadow: style.boxShadow,
          rowBorder: rowStyle.borderTopWidth,
          hasFooter: meta.contains(footer),
        };
      });
      assert(composerMetrics, `[${theme}] card+row+meta must all render`);
      assert(
        composerMetrics.cardBorder === "solid",
        `[${theme}] card border style: ${composerMetrics.cardBorder}`,
      );
      assert(
        composerMetrics.cardBorderWidth === "1px",
        `[${theme}] card border width: ${composerMetrics.cardBorderWidth}`,
      );
      assert(
        composerMetrics.cardShadow !== "none",
        `[${theme}] card must carry --shadow-lift`,
      );
      assert(
        composerMetrics.rowBorder === "0px",
        `[${theme}] row must not draw its own border (${composerMetrics.rowBorder})`,
      );
      assert(composerMetrics.hasFooter, `[${theme}] model footer must live inside the card`);

      // Focus lift on the CARD (bb pattern: focus-within lifts the outer card,
      // not the row inside it). Compare shadow before/after focusing the input.
      const restingShadow = composerMetrics.cardShadow;
      await composer.focus();
      const focusedShadow = await page.evaluate(
        () => getComputedStyle(document.querySelector(".session-composer-card")).boxShadow,
      );
      assert(
        focusedShadow !== restingShadow,
        `[${theme}] focus-within must change the card shadow (was ${restingShadow})`,
      );

      // Skill-menu opens with a bb-menu-row + .prompt-mention-pill preview.
      await composer.fill("/frontend");
      await page.locator(".session-skill-menu").waitFor({ state: "visible" });
      const skillRow = page.locator(".session-skill-item").first();
      const skillClass = await skillRow.getAttribute("class");
      assert(
        skillClass?.includes("bb-menu-row"),
        `[${theme}] skill row must carry bb-menu-row: ${skillClass}`,
      );
      const pillClass = await skillRow.locator(".session-skill-name").first().getAttribute("class");
      assert(
        pillClass?.includes("prompt-mention-pill"),
        `[${theme}] skill name must render as .prompt-mention-pill: ${pillClass}`,
      );
      // Dismiss the menu + drain the textarea for the next iteration.
      await composer.fill("");

      const screenshotPath = path.join(OUT_DIR, `composer-${theme}.png`);
      await page.screenshot({ path: screenshotPath, fullPage: false });
      logStep(`saved ${screenshotPath}`);
    }

    logStep("all themes verified");
  } finally {
    await browser.close();
    backend.stop();
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
